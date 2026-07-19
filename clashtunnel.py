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
import time
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


_profile_seq = 0


def _unique_profile_name() -> str:
    """A fresh CMFA profile name for every import.

    Re-importing under a CONSTANT name is the bug that made proxy switches
    silently no-op: an old profile of that name is already the selected radio,
    so select_profile()'s 'already checked -> return' short-circuit fires and
    the newly-imported config never becomes active (device keeps its old exit
    IP, or stays fail-closed on a dead proxy). A unique name each time means
    the new profile is never pre-selected, so it actually gets tapped/loaded.
    time + an in-process counter keeps it unique even for back-to-back calls."""
    global _profile_seq
    _profile_seq += 1
    return f"mf-{int(time.time())}-{_profile_seq}"


# --------------------------------------------------------------------------- #
# config generation
# --------------------------------------------------------------------------- #
# How each proxy type spells "the credential". Anything not listed here is
# still supported — its settings just come through `extra` verbatim.
_AUTH_KEYS = {
    "http": ("username", "password"), "https": ("username", "password"),
    "socks5": ("username", "password"), "socks4": ("username", "password"),
    "ss": (None, "password"), "trojan": (None, "password"),
    "hysteria": (None, "password"), "hysteria2": (None, "password"),
    "snell": (None, "psk"),
}
_UDP_TYPES = {"socks5", "ss", "ssr", "vmess", "vless", "trojan",
              "hysteria", "hysteria2", "tuic", "wireguard", "snell"}


def _yaml_scalar(v) -> str:
    """Render a Python value as a YAML scalar. Strings are quoted (so a password
    of `@bc#123` or a bare `yes` can't be reinterpreted as YAML syntax)."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _yaml_lines(obj, indent: int) -> list[str]:
    """Emit nested dict/list settings (ws-opts, reality-opts, headers, ...) so
    protocol-specific config survives round-tripping without a schema for it."""
    pad = " " * indent
    out: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                out.append(f"{pad}{k}:")
                out += _yaml_lines(v, indent + 2)
            else:
                out.append(f"{pad}{k}: {_yaml_scalar(v)}")
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, (dict, list)):
                out.append(f"{pad}-")
                out += _yaml_lines(item, indent + 2)
            else:
                out.append(f"{pad}- {_yaml_scalar(item)}")
    return out


def _proxy_block(proxy: dict) -> str:
    """Render ANY supported proxy as a mihomo `proxies:` entry.

    host/port/type plus the type's credential fields are mapped explicitly; every
    other protocol setting (uuid, cipher, sni, network, ws-opts, obfs, flow, ...)
    is carried in `extra` and emitted as-is. That means a proxy type we have
    never special-cased still works as long as the operator supplies its fields —
    which is the whole point of "use any type of proxy"."""
    ptype = str(proxy.get("type") or "http").lower()
    if ptype in ("socks", "socks5h"):
        ptype = "socks5"
    if ptype == "auto":                 # never resolved by a health check — assume http
        ptype = "http"
    extra = dict(proxy.get("extra") or {})

    lines = [
        f'  - name: "{PROFILE_NAME}"',
        f"    type: {ptype}",
        f'    server: {proxy["host"]}',
        f'    port: {int(proxy["port"])}',
    ]
    user_key, pass_key = _AUTH_KEYS.get(ptype, ("username", "password"))
    if user_key and proxy.get("username") and user_key not in extra:
        lines.append(f'    {user_key}: {_yaml_scalar(proxy["username"])}')
    if pass_key and proxy.get("password") and pass_key not in extra:
        lines.append(f'    {pass_key}: {_yaml_scalar(proxy["password"])}')
    if ptype in _UDP_TYPES and "udp" not in extra:
        lines.append("    udp: true")
    if ptype == "http" and "tls" not in extra:
        lines.append("    tls: false")
    lines += _yaml_lines(extra, 4)
    return "\n".join(lines)


def gen_config(proxy: dict) -> str:
    """Render a hardened mihomo YAML for one upstream proxy.

    ``proxy`` = {host, port, type, username?, password?, extra?} and may be ANY
    type mihomo can dial (http/socks5/ss/vmess/vless/trojan/hysteria2/tuic/...);
    see _proxy_block. DNS is fake-ip over DoH (TCP/443) which is
    itself matched by ``MATCH,PROXY`` and thus tunnelled — no plaintext DNS
    leaves the device. ``ipv6: false`` + the tun's unreachable v6 route + VPN
    lockdown together guarantee no v6 leak.

    QUIC (UDP/443) is REJECTed so YouTube/Chromium fall back to HTTPS-over-TCP.
    UDP egress needs a *real* IP (fake-ip's hostname-forwarding doesn't apply to
    the proxy's UDP relay), which forces a real DoH lookup — and that DoH CONNECT
    is refused through these proxies, so QUIC video + consent submission would
    hang. On TCP the hostname is forwarded to the proxy, which resolves it itself
    (verified: a ``socks5h`` curl through every node succeeds), so everything
    works without the device ever needing a working DoH path.
    """
    proxy_block = _proxy_block(proxy)
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
  - AND,((NETWORK,UDP),(DST-PORT,443)),REJECT
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
def _serve_bytes(data: bytes) -> tuple[int, http.server.HTTPServer, dict]:
    """Serve `data` for any GET on an ephemeral localhost port (a per-call server
    so concurrent onboards never collide). Returns (port, httpd, stats).

    `stats["hits"]` counts how many times the device actually fetched the
    config — the difference between "we asked CMFA to import" and "CMFA really
    pulled the bytes". Without it, a broken `adb reverse` looks identical to a
    successful import that just didn't get saved."""
    stats = {"hits": 0}

    class _H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            stats["hits"] += 1
            self.send_response(200)
            self.send_header("Content-Type", "text/yaml")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):  # silence
            pass

    httpd = http.server.HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd.server_address[1], httpd, stats


async def import_profile(serial: str, config_yaml: str, profile_name: str = PROFILE_NAME) -> dict:
    """Import the config as a CMFA profile named ``profile_name`` and COMMIT it.

    Served over HTTP via ``adb reverse`` rather than a ``file://`` path: CMFA
    declares no storage permission, so it cannot read a file on /sdcard and the
    import lands 'Unsaved' (unselectable → tunnel never starts). A localhost HTTP
    source works with no permissions. The PropertiesActivity Save action is then
    tapped (polled until it renders) to actually persist the profile.

    ``profile_name`` should be unique per import (see _unique_profile_name) so
    a proxy switch produces a brand-new, not-yet-selected profile — otherwise
    select_profile() can't tell it apart from the old one and never activates
    it."""
    data = config_yaml.encode()
    port, httpd, stats = _serve_bytes(data)
    rev_ok, rev_err = False, ""
    try:
        try:
            rc, _out, err = await adb._run(["-s", serial, "reverse", f"tcp:{port}", f"tcp:{port}"])
            rev_ok = rc == 0
            rev_err = (err or b"").decode(errors="replace").strip()[:200]
        except Exception as e:  # noqa: BLE001
            rev_ok, rev_err = False, str(e)[:200]
        url = urllib.parse.quote(f"http://127.0.0.1:{port}/{profile_name}.yaml", safe="")
        await adb.shell(
            serial,
            f"am start -a android.intent.action.VIEW "
            f"-d 'clash://install-config?url={url}&name={profile_name}' -n {EXT}",
        )
        saved = False
        for _ in range(6):  # wait for PropertiesActivity to render, then Save
            await asyncio.sleep(1.2)
            b = _find_bounds(await _dump_ui(serial), desc="Save")
            if b:
                await adb.tap(serial, *_center(b))
                saved = True
                break
        # CMFA renders the properties form WITHOUT downloading anything — it
        # fetches the config only when the profile is committed, which lags the
        # Save tap by several seconds. The old code slept a flat 2s and then let
        # `finally` tear down the HTTP server + adb reverse, so the download hit
        # a dead port and the profile was silently never created (fetched=0,
        # profiles list unchanged). Hold everything open until the device has
        # actually pulled the bytes.
        if saved:
            for _ in range(60):                 # up to ~30s
                if stats["hits"] > 0:
                    break
                await asyncio.sleep(0.5)
            await asyncio.sleep(2)              # let CMFA finish writing it out
        # Diagnostics: distinguish "reverse failed" / "device never fetched the
        # config" / "Save button never rendered" — these fail identically today.
        return {"ok": saved and stats["hits"] > 0, "saved": saved,
                "reverse_ok": rev_ok, "reverse_err": rev_err,
                "fetched": stats["hits"], "profile": profile_name}
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


async def _open_profiles(serial: str) -> bool:
    """Navigate to ProfilesActivity (not exported, so go via MainActivity)."""
    await adb.launch_package(serial, PKG)
    await asyncio.sleep(2.5)
    b = _find_bounds(await _dump_ui(serial), text="Profile")
    if not b:
        return False
    await adb.tap(serial, *_center(b))
    await asyncio.sleep(2)
    return True


def _profile_rows(xml: str) -> list[tuple[str, tuple[int, int]]]:
    """(name, tap-point) for every profile row currently on screen."""
    rows: list[tuple[str, tuple[int, int]]] = []
    for node in re.findall(r"<node\b[^>]*?/>", xml):
        if "TextView" not in node:
            continue
        m = re.search(r'text="(mf-[^"]*)"', node)
        if not m:
            continue
        c = _center(re.search(r'bounds="(\[[^"]*\])"', node).group(1))
        if c:
            rows.append((m.group(1), c))
    return rows


async def prune_profiles(serial: str, keep: str, max_delete: int = 30) -> dict:
    """Delete every saved profile except `keep`.

    Each proxy switch imports a new profile, and CMFA never removes the old one.
    After ~20 switches the list is long enough that the newly-imported profile
    sits below the fold, select_profile can't reach it, and EVERY further
    re-point silently fails with "exit IP did not match proxy" — the device is
    then stuck on whatever proxy it had, permanently. Observed on real devices
    at 22 profiles.

    Best-effort UI automation (CMFA offers no intent for this): long-press a row
    → tap Delete → confirm. Any step that can't find its target ends the pass
    rather than blind-tapping, and a failure here never fails the switch — a
    cluttered list is a slow problem, a mis-tap is an immediate one.

    KNOWN LIMITATION: on the CMFA builds this fleet runs, long-pressing a
    profile row opens no menu at all — only "Close" and "New" exist — so this
    deletes nothing and reports deleted=0 with a reason. It is kept because the
    gesture is harmless and a future build may add the menu, but do not treat a
    successful call as evidence the list was pruned.

    The remedy that does work is a full reset of the app:
        adb shell pm clear com.github.metacubex.clash.meta
        adb shell appops set com.github.metacubex.clash.meta ACTIVATE_VPN allow
    followed by a fresh onboard. Disable the tunnel first — clearing the app
    while the always-on lockdown is armed strands the device with no route.
    """
    deleted, attempts = 0, 0
    no_menu = False
    if not await _open_profiles(serial):
        return {"ok": False, "deleted": 0, "error": "could not open profiles"}
    while attempts < max_delete:
        attempts += 1
        before = [r for r in _profile_rows(await _dump_ui(serial)) if r[0] != keep]
        if not before:
            break
        name, (x, y) = before[0]
        await adb.shell(serial, f"input swipe {x} {y} {x} {y} 900")   # long-press
        await asyncio.sleep(1.2)
        xml = await _dump_ui(serial)
        target = (_find_bounds(xml, text="Delete") or _find_bounds(xml, desc="Delete")
                  or _find_bounds(xml, text="Remove") or _find_bounds(xml, desc="Remove"))
        if not target:
            await adb.keyevent(serial, "KEYCODE_BACK")   # no menu — don't guess
            no_menu = True
            break
        await adb.tap(serial, *_center(target))
        await asyncio.sleep(1.0)
        # confirmation dialog, if this build shows one
        xml = await _dump_ui(serial)
        for label in ("DELETE", "Delete", "OK", "Yes"):
            b = _find_bounds(xml, text=label)
            if b:
                await adb.tap(serial, *_center(b))
                break
        await asyncio.sleep(1.2)
        # Confirm the row actually went. Counting the attempt instead of the
        # result is how this came to report "deleted: 22" for a device whose
        # list never changed, which sent a real investigation the wrong way.
        after = [r[0] for r in _profile_rows(await _dump_ui(serial)) if r[0] != keep]
        if name in after:
            no_menu = True          # tapped something, nothing was removed
            break
        deleted += 1
    out = {"ok": deleted > 0 or not no_menu, "deleted": deleted, "kept": keep}
    if no_menu:
        out["error"] = ("this CMFA build exposes no delete action on a profile row — "
                        "pm clear + re-onboard is the working reset")
    return out


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
    # Every proxy switch imports ANOTHER profile, so this list only grows — the
    # profile we just imported is often below the fold, where a single
    # screen-dump can't see it (import succeeds, selection silently fails, and
    # the device keeps its old exit). Scroll until it comes into view.
    for _ in range(8):
        if _profile_radio(await _dump_ui(serial), name) is not None:
            break
        await adb.swipe(serial, 540, 1500, 540, 500, 300)
        await asyncio.sleep(1.2)
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


async def _drop_protection(serial: str) -> None:
    """Fully tear down the VPN so a config import can actually download.

    Clearing the always-on settings alone is NOT enough: an already-established
    always-on session keeps its lockdown routing in place, which still blocks
    CMFA's own config fetch (the import then reports fetched=0 and imports
    nothing). Force-stopping the app kills the VpnService for real — safe only
    AFTER always_on_vpn_app is cleared, otherwise Android just respawns it."""
    await disarm_lockdown(serial)
    await stop(serial)
    await adb.shell(serial, f"am force-stop {PKG}")
    await asyncio.sleep(5)


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
    # Android's always-on-VPN lockdown blocks CMFA's OWN config download, so an
    # import on an already-protected device fetches nothing (fetched=0) and
    # silently keeps the previous profile — which is why every proxy switch was
    # a no-op and half the fleet stayed stranded on dead exits. onboard only
    # worked because it imports BEFORE it arms the kill-switch. Drop lockdown
    # for the import, then restore it.
    was_armed = await is_lockdown_armed(serial)
    if was_armed:
        await _drop_protection(serial)
    exit_info: dict = {}
    try:
        pname = _unique_profile_name()
        imp = await import_profile(serial, gen_config(proxy), profile_name=pname)
        selected = await select_profile(serial, pname)   # activate the fresh profile
        for attempt in range(retries + 1):
            await stop(serial)
            await asyncio.sleep(1.5)
            await start(serial)
            await asyncio.sleep(5)
            exit_info = await verify_exit(serial)
            if exit_info.get("ip") == proxy["host"]:
                # Switch is confirmed working — now clear the old profiles so the
                # list can't grow until select_profile stops being able to reach
                # the newest entry. Best-effort: never let cleanup fail a switch.
                pruned = None
                try:
                    pruned = await prune_profiles(serial, keep=pname)
                except Exception:  # noqa: BLE001
                    pruned = {"ok": False}
                return {"ok": True, "connected": True, "exit": exit_info,
                        "import": imp, "selected": selected, "relockdown": was_armed,
                        "pruned": pruned}
            selected = await select_profile(serial, pname)  # self-correct a missed selection
        return {"ok": False, "connected": False, "exit": exit_info,
                "expected": proxy["host"], "import": imp, "selected": selected,
                "relockdown": was_armed,
                "error": "exit IP did not match proxy (profile not active?)"}
    finally:
        if was_armed:
            # Re-arm without rebooting — the tunnel is already back up and the
            # setting persists, so a later boot enforces the kill-switch again.
            await arm_lockdown(serial, reboot=False)


async def onboard(serial: str, proxy: dict) -> dict:
    """First-time setup for a device: install, import+select the profile, bring
    the tunnel up, then arm the fail-closed kill-switch (one reboot). After the
    reboot it self-heals — re-selecting the profile and restarting the core until
    the device's exit IP actually matches the proxy."""
    inst = await install(serial)
    if not inst["ok"]:
        return inst
    # A RE-onboard hits an already-locked-down device, where the kill-switch
    # blocks CMFA's config download exactly as in set_proxy — so drop it first.
    # (arm_lockdown below re-arms it as part of the normal flow.)
    if await is_lockdown_armed(serial):
        await _drop_protection(serial)
    pname = _unique_profile_name()
    imp = await import_profile(serial, gen_config(proxy), profile_name=pname)
    await select_profile(serial, pname)
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
        await select_profile(serial, pname)
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
