"""
Leak-proof device-wide proxy tunnel via Clash Meta for Android (mihomo).

Why this exists
---------------
Android's built-in ``settings global http_proxy`` is HTTP-only, carries no auth,
and is trivially bypassed (it is a *hint*, not a route) — useless for the auth'd
SOCKS5 proxies we run and impossible to make leak-proof. The only no-root way to
route **all** of a phone's traffic through an upstream proxy with no leaks is a
full-tunnel ``VpnService``. Clash Meta provides exactly that: it builds a tun
interface, captures every packet (v4 + v6), resolves DNS inside the tunnel
(fake-ip, so no plaintext DNS ever hits the local ISP resolver), and forwards
everything to our SOCKS5 upstream.

Proven live on a Samsung S8 / Android 9 (SM-G9500):
  * exit IP flips to the proxy's country, real ISP IP fully replaced
  * ``curl -6`` fails (IPv6 default route is ``::/0 unreachable`` → no v6 leak)
  * hostnames resolve to 198.18.x.x fake-ip → no DNS leak
  * with always-on + lockdown armed, killing the core fails **closed** (a tunnel
    drop blocks traffic instead of leaking the real IP)

Honest limitation: this makes egress *leak-proof*, not *undetectable*. A
datacenter/hosting proxy IP still scores as "proxy/VPN" on IP-reputation
services (proxycheck.io, ip-api ``proxy`` flag). FUD requires residential/mobile
upstreams — that is a proxy-sourcing problem, not a tunnel problem.

Design
------
Each device keeps a **single** persistent Clash profile named ``mf-proxy`` whose
source is ``file:///sdcard/mf-proxy.yaml``. Switching proxies = overwrite that
file + re-import + restart core; the profile stays selected so no per-switch UI
tap is needed. First-time ``onboard`` does the one interactive selection tap
(driven headlessly via uiautomator) and arms the kill-switch (needs one reboot,
because Android only applies always-on lockdown when the *system* owns the VPN
session, which happens at boot).
"""
from __future__ import annotations

import asyncio
import http.server
import json
import os
import re
import threading
import urllib.parse
from typing import Optional

import adb

PKG = "com.github.metacubex.clash.meta"
EXT = f"{PKG}/com.github.kr328.clash.ExternalControlActivity"
PROFILES_ACT = f"{PKG}/com.github.kr328.clash.ProfilesActivity"
ACTION_START = "com.github.metacubex.clash.meta.action.START_CLASH"
ACTION_STOP = "com.github.metacubex.clash.meta.action.STOP_CLASH"

APK_PATH = os.path.join(os.path.dirname(__file__), "vendor", "clash",
                        "cmfa-2.11.31-arm64.apk")
PROFILE_NAME = "mf-proxy"
DEVICE_CFG = "/sdcard/mf-proxy.yaml"
IP_ECHO = "http://ip-api.com/json?fields=query,country,regionName,city,isp,proxy,hosting,mobile"


# --------------------------------------------------------------------------- #
# config generation
# --------------------------------------------------------------------------- #
def gen_config(proxy: dict) -> str:
    """Render a hardened mihomo YAML for one upstream proxy.

    ``proxy`` = {host, port, type, username?, password?}. ``type`` starting with
    "socks" → socks5, else http. DNS is fake-ip over DoH (TCP/443) which is
    itself matched by ``MATCH,PROXY`` and thus tunnelled — no plaintext DNS
    leaves the device. ``ipv6: false`` + the tun's unreachable v6 route + VPN
    lockdown together guarantee no v6 leak.
    """
    ptype = str(proxy.get("type", "socks5")).lower()
    is_socks = ptype.startswith("socks")
    lines = [
        f'  - name: "{PROFILE_NAME}"',
        f'    type: {"socks5" if is_socks else "http"}',
        f'    server: {proxy["host"]}',
        f'    port: {int(proxy["port"])}',
    ]
    if proxy.get("username"):
        lines.append(f'    username: {proxy["username"]}')
        lines.append(f'    password: {proxy.get("password") or ""}')
    if is_socks:
        lines.append("    udp: true")
    else:
        lines.append("    tls: false")
    proxy_block = "\n".join(lines)
    return f"""mixed-port: 7890
allow-lan: false
mode: rule
log-level: warning
ipv6: false
dns:
  enable: true
  ipv6: false
  enhanced-mode: fake-ip
  fake-ip-range: 198.18.0.1/16
  nameserver:
    - https://1.1.1.1/dns-query
    - https://8.8.8.8/dns-query
  fallback:
    - https://1.1.1.1/dns-query
proxies:
{proxy_block}
proxy-groups:
  - name: "PROXY"
    type: select
    proxies: ["{PROFILE_NAME}"]
rules:
  - MATCH,PROXY
"""


# --------------------------------------------------------------------------- #
# install / consent
# --------------------------------------------------------------------------- #
async def is_installed(serial: str) -> bool:
    r = await adb.shell(serial, f"pm list packages {PKG}")
    return PKG in r["stdout"]


async def install(serial: str) -> dict:
    """Install Clash Meta and pre-grant VPN consent so the connect dialog never
    blocks headless start."""
    if not os.path.exists(APK_PATH):
        return {"ok": False, "error": f"APK missing at {APK_PATH}"}
    if not await is_installed(serial):
        res = await adb.install(serial, APK_PATH)
        if not res["ok"]:
            return {"ok": False, "error": "apk install failed", "detail": res}
    # ACTIVATE_VPN allow => VpnService.prepare() returns null => no user dialog
    await adb.shell(serial, f"appops set {PKG} ACTIVATE_VPN allow")
    return {"ok": True, "installed": True}


# --------------------------------------------------------------------------- #
# uiautomator helpers (headless taps)
# --------------------------------------------------------------------------- #
async def _dump_ui(serial: str) -> str:
    await adb.shell(serial, "uiautomator dump /sdcard/mf_ui.xml")
    r = await adb.shell(serial, "cat /sdcard/mf_ui.xml")
    return r["stdout"]


def _center(bounds: str) -> Optional[tuple[int, int]]:
    m = re.search(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
    if not m:
        return None
    x1, y1, x2, y2 = map(int, m.groups())
    return (x1 + x2) // 2, (y1 + y2) // 2


def _find_bounds(xml: str, *, desc: str = "", text: str = "") -> Optional[str]:
    for node in re.findall(r"<node[^>]*/>", xml):
        if desc and f'content-desc="{desc}"' not in node:
            continue
        if text and f'text="{text}"' not in node:
            continue
        m = re.search(r'bounds="(\[[^"]*\])"', node)
        if m:
            return m.group(1)
    return None


async def _tap_desc(serial: str, desc: str) -> bool:
    c = _center(_find_bounds(await _dump_ui(serial), desc=desc) or "")
    if not c:
        return False
    await adb.tap(serial, *c)
    return True


# --------------------------------------------------------------------------- #
# profile import / selection
# --------------------------------------------------------------------------- #
def _serve_bytes(data: bytes) -> tuple[int, http.server.HTTPServer]:
    """Serve `data` for any GET on an ephemeral localhost port (a per-call server
    so concurrent onboards never collide). Returns (port, httpd)."""
    class _H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "text/yaml")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):  # silence
            pass

    httpd = http.server.HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd.server_address[1], httpd


async def import_profile(serial: str, config_yaml: str) -> dict:
    """Import the config as the ``mf-proxy`` profile and COMMIT it.

    Served over HTTP via ``adb reverse`` rather than a ``file://`` path: CMFA
    declares no storage permission, so it cannot read a file on /sdcard and the
    import lands 'Unsaved' (unselectable → tunnel never starts). A localhost HTTP
    source works with no permissions. The PropertiesActivity Save action is then
    tapped (polled until it renders) to actually persist the profile."""
    data = config_yaml.encode()
    port, httpd = _serve_bytes(data)
    try:
        await adb._run(["-s", serial, "reverse", f"tcp:{port}", f"tcp:{port}"])
        url = urllib.parse.quote(f"http://127.0.0.1:{port}/{PROFILE_NAME}.yaml", safe="")
        await adb.shell(
            serial,
            f"am start -a android.intent.action.VIEW "
            f"-d 'clash://install-config?url={url}&name={PROFILE_NAME}' -n {EXT}",
        )
        saved = False
        for _ in range(6):  # wait for PropertiesActivity to render, then Save
            await asyncio.sleep(1.2)
            b = _find_bounds(await _dump_ui(serial), desc="Save")
            if b:
                await adb.tap(serial, *_center(b))
                await asyncio.sleep(2)
                saved = True
                break
        return {"ok": saved, "saved": saved}
    finally:
        try:
            await adb._run(["-s", serial, "reverse", "--remove", f"tcp:{port}"])
        except Exception:  # noqa: BLE001
            pass
        httpd.shutdown()


def _profile_radio(xml: str, name: str) -> Optional[tuple[int, int, bool]]:
    """(x, y, checked) of the RadioButton in the profile row whose name matches."""
    ny = None
    for node in re.findall(r"<node\b[^>]*?/>", xml):
        if f'text="{name}"' in node and "TextView" in node:
            m = re.search(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node)
            if m:
                ny = (int(m.group(2)) + int(m.group(4))) // 2
    if ny is None:
        return None
    best, bestd = None, 10 ** 9
    for node in re.findall(r"<node\b[^>]*?/>", xml):
        if "RadioButton" in node:
            m = re.search(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node)
            if not m:
                continue
            x1, y1, x2, y2 = map(int, m.groups())
            ry = (y1 + y2) // 2
            if abs(ry - ny) < bestd:
                bestd = abs(ry - ny)
                best = ((x1 + x2) // 2, ry, 'checked="true"' in node)
    return best


async def select_profile(serial: str, name: str = PROFILE_NAME) -> bool:
    """Activate the named profile. ProfilesActivity is NOT exported (am start on
    it throws SecurityException), so reach it through MainActivity's 'Profile'
    entry, then tap the profile's RadioButton and verify it became checked —
    retrying, since a saved-but-inactive profile's radio can take a couple taps."""
    await adb.launch_package(serial, PKG)             # monkey -> MainActivity
    await asyncio.sleep(2.5)
    b = _find_bounds(await _dump_ui(serial), text="Profile")
    if not b:
        return False
    await adb.tap(serial, *_center(b))                # -> ProfilesActivity
    await asyncio.sleep(2)
    for _ in range(4):
        r = _profile_radio(await _dump_ui(serial), name)
        if r is None:
            return False
        if r[2]:                                       # already checked
            return True
        await adb.tap(serial, r[0], r[1])
        await asyncio.sleep(1.5)
    r = _profile_radio(await _dump_ui(serial), name)
    return bool(r and r[2])


# --------------------------------------------------------------------------- #
# core control
# --------------------------------------------------------------------------- #
async def start(serial: str) -> dict:
    await adb.shell(serial, f"am start -a {ACTION_START} -n {EXT}")
    return {"ok": True}


async def stop(serial: str) -> dict:
    await adb.shell(serial, f"am start -a {ACTION_STOP} -n {EXT}")
    return {"ok": True}


async def tun_up(serial: str) -> bool:
    r = await adb.shell(serial, "ip addr show tun0 2>/dev/null | grep inet")
    return "inet " in r["stdout"]


async def verify_exit(serial: str, timeout: float = 22.0) -> dict:
    """Ask the *device itself* (through whatever routing is live) what its public
    IP is. When the tunnel is up this is the proxy's egress; if it leaks it is
    the real ISP IP — which is exactly what makes this a trustworthy check."""
    r = await adb.shell(serial, f"curl -s --max-time {int(timeout)} '{IP_ECHO}'")
    try:
        d = json.loads(r["stdout"].strip())
        return {
            "ok": True, "ip": d.get("query"), "country": d.get("country"),
            "city": d.get("city"), "isp": d.get("isp"),
            "flagged_proxy": bool(d.get("proxy")), "hosting": bool(d.get("hosting")),
        }
    except Exception:  # noqa: BLE001
        return {"ok": False, "ip": None, "raw": r["stdout"][:200]}


# --------------------------------------------------------------------------- #
# kill-switch (always-on VPN + lockdown)
# --------------------------------------------------------------------------- #
async def is_lockdown_armed(serial: str) -> bool:
    app = (await adb.shell(serial, "settings get secure always_on_vpn_app"))["stdout"].strip()
    lock = (await adb.shell(serial, "settings get secure always_on_vpn_lockdown"))["stdout"].strip()
    return app == PKG and lock == "1"


async def arm_lockdown(serial: str, reboot: bool = True) -> dict:
    """Persist always-on VPN + lockdown in Settings.Secure (the namespace the
    framework's Vpn service reads at boot) then reboot so the *system* creates
    the always-on managed session — the only way lockdown actually enforces on
    Android 9. After this, a core crash fails closed."""
    await adb.shell(serial, f"settings put secure always_on_vpn_app {PKG}")
    await adb.shell(serial, "settings put secure always_on_vpn_lockdown 1")
    if not reboot:
        return {"ok": True, "armed": True, "reboot_pending": True}
    await adb.reboot(serial)
    await adb._run(["-s", serial, "wait-for-device"], timeout=180)
    for _ in range(40):
        bc = await adb.getprop(serial, "sys.boot_completed")
        if bc == "1":
            break
        await asyncio.sleep(3)
    await asyncio.sleep(5)
    await adb.keyevent(serial, "KEYCODE_WAKEUP")
    await adb.keyevent(serial, "82")
    return {"ok": await is_lockdown_armed(serial), "armed": True, "rebooted": True}


async def disarm_lockdown(serial: str) -> dict:
    await adb.shell(serial, "settings delete secure always_on_vpn_app")
    await adb.shell(serial, "settings delete secure always_on_vpn_lockdown")
    return {"ok": True}


# --------------------------------------------------------------------------- #
# high-level operations
# --------------------------------------------------------------------------- #
async def set_proxy(serial: str, proxy: dict, retries: int = 2) -> dict:
    """Point an already-onboarded device at ``proxy`` and prove egress.

    Overwrites the profile config, restarts the core, then verifies the device's
    exit IP == the proxy host. If it doesn't match (e.g. profile not selected),
    it re-selects and retries — so the returned result reflects reality, never a
    hopeful assumption."""
    if not await is_installed(serial):
        return {"ok": False, "error": "clash not installed — run onboard first"}
    await import_profile(serial, gen_config(proxy))
    for attempt in range(retries + 1):
        await stop(serial)
        await asyncio.sleep(1.5)
        await start(serial)
        await asyncio.sleep(5)
        exit_info = await verify_exit(serial)
        if exit_info.get("ip") == proxy["host"]:
            return {"ok": True, "connected": True, "exit": exit_info,
                    "lockdown": await is_lockdown_armed(serial)}
        await select_profile(serial)  # self-correct a missed selection
    return {"ok": False, "connected": False, "exit": exit_info,
            "expected": proxy["host"],
            "error": "exit IP did not match proxy (profile not active?)"}


async def onboard(serial: str, proxy: dict) -> dict:
    """First-time setup for a device: install, import+select the profile, bring
    the tunnel up, then arm the fail-closed kill-switch (one reboot). After the
    reboot it self-heals — re-selecting the profile and restarting the core until
    the device's exit IP actually matches the proxy."""
    inst = await install(serial)
    if not inst["ok"]:
        return inst
    imp = await import_profile(serial, gen_config(proxy))
    await select_profile(serial)
    await start(serial)
    await asyncio.sleep(5)
    before = await verify_exit(serial)
    lock = await arm_lockdown(serial, reboot=True)
    after = {"ip": None}
    for _ in range(3):  # after reboot: start, verify, re-select on miss
        await start(serial)
        await asyncio.sleep(6)
        after = await verify_exit(serial)
        if after.get("ip") == proxy["host"]:
            break
        await select_profile(serial)
    return {
        "ok": after.get("ip") == proxy["host"],
        "exit": after, "exit_before_reboot": before,
        "lockdown_armed": lock.get("ok"),
        "connected": after.get("ip") == proxy["host"],
        "imported": imp.get("saved"),
    }


async def status(serial: str) -> dict:
    return {
        "ok": True,
        "installed": await is_installed(serial),
        "tun_up": await tun_up(serial),
        "lockdown_armed": await is_lockdown_armed(serial),
        "exit": await verify_exit(serial),
    }


async def is_fail_closed(serial: str) -> bool:
    """Fail-closed = the kill-switch is armed but the tunnel is down, so ALL
    traffic is blocked (no internet). This is the state the watchdog repairs.
    A device with no lockdown and no tunnel is just direct networking — fine."""
    if not await tun_up(serial):
        return await is_lockdown_armed(serial)
    return False


async def heal(serial: str) -> dict:
    """Recover a fail-closed device by restarting the Clash core. The profile
    persists across restarts, so a plain START usually revives it; if the tunnel
    still doesn't come up, re-select the profile and restart once more. Returns
    {healed, action, tun_up}. Never reboots — that's the heavier /tunnel/recover."""
    if not await is_lockdown_armed(serial):
        return {"ok": False, "healed": False, "reason": "not armed"}
    if await tun_up(serial):
        return {"ok": True, "healed": True, "reason": "already up"}

    # attempt 1: just restart the core
    await start(serial)
    await asyncio.sleep(6)
    if await tun_up(serial):
        return {"ok": True, "healed": True, "action": "restart", "tun_up": True}

    # attempt 2: re-select the profile (a deselected profile → core won't route) then restart
    await select_profile(serial)
    await start(serial)
    await asyncio.sleep(6)
    up = await tun_up(serial)
    return {"ok": up, "healed": up, "action": "reselect+restart", "tun_up": up}


async def disable(serial: str) -> dict:
    """Tear down protection: stop the core and remove the kill-switch so the
    device returns to normal (direct) networking."""
    await stop(serial)
    await disarm_lockdown(serial)
    return {"ok": True}
