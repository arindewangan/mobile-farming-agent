"""
Device app management — install the platform's required apps, remove leftovers
from the (now-removed) profile system, and report device readiness.

Required apps (what a farm device needs to be fully usable):
  * AI Portal  (com.mobilerun.portal)         — accessibility service for AI-task automation
  * Clash Meta (com.github.metacubex.clash.meta) — the leak-proof proxy tunnel engine

Forbidden leftover:
  * BlackBox   (top.niunaijun.blackbox)       — the old app-virtualization engine; the
    profile system was removed, so this is dead weight and gets uninstalled.

`install_required` only puts the APKs on the device + preps it; configuring the
proxy tunnel (assign a proxy, arm the kill-switch) is a separate step
(clashtunnel / "Protect all").
"""
from __future__ import annotations

import adb
import clashtunnel
import droidrun

BLACKBOX_PKG = "top.niunaijun.blackbox"
REQUIRED = [
    ("AI Portal", "com.mobilerun.portal", "AI-task automation"),
    ("Clash Meta", "com.github.metacubex.clash.meta", "leak-proof proxy tunnel"),
]


async def _installed(serial: str, pkg: str) -> bool:
    r = await adb.shell(serial, f"pm path {pkg}")
    return "package:" in r.get("stdout", "")


async def remove_blackbox(serial: str) -> dict:
    """Uninstall the BlackBox engine (profile system is gone). Idempotent."""
    if not await _installed(serial, BLACKBOX_PKG):
        return {"ok": True, "removed": False, "detail": "already absent"}
    rc, out, err = await adb._run(["-s", serial, "uninstall", BLACKBOX_PKG], timeout=60)
    ok = rc == 0 and "Success" in (out + err).decode(errors="replace")
    return {"ok": ok, "removed": ok, "detail": "uninstalled" if ok else (out + err).decode(errors="replace")[-140:]}


async def install_required(serial: str) -> dict:
    """Install every required app + prep the device. Reports each app's status."""
    apps: list[dict] = []
    # AI Portal (installs the app + enables its accessibility service)
    try:
        await droidrun.setup(serial)
        p = await droidrun.ping(serial)
        apps.append({"name": "AI Portal", "ok": bool(p.get("ok")),
                     "detail": "installed + accessibility on" if p.get("ok") else "installed (a11y not confirmed)"})
    except Exception as e:  # noqa: BLE001
        apps.append({"name": "AI Portal", "ok": False, "detail": str(e)})
    # Clash Meta (installs APK + pre-grants VPN consent; NOT configured yet)
    try:
        r = await clashtunnel.install(serial)
        apps.append({"name": "Clash Meta", "ok": bool(r.get("ok")),
                     "detail": "installed (configure via Protect)" if r.get("ok") else r.get("error", "install failed")})
    except Exception as e:  # noqa: BLE001
        apps.append({"name": "Clash Meta", "ok": False, "detail": str(e)})
    # Prep: keep awake while charging, long screen timeout (bench boards)
    try:
        await adb.shell(serial, "svc power stayon true")
        await adb.shell(serial, "settings put system screen_off_timeout 1800000")
        apps.append({"name": "Prep (stay-awake)", "ok": True, "detail": "stay-awake + long screen timeout"})
    except Exception as e:  # noqa: BLE001
        apps.append({"name": "Prep (stay-awake)", "ok": False, "detail": str(e)})
    return {"ok": all(a["ok"] for a in apps), "apps": apps}


async def required_status(serial: str) -> dict:
    """Is the device ready: required apps installed, BlackBox gone, device live.
    Shaped like the preflight result ({ok, checks}) so the panel renders it."""
    checks: list[dict] = []

    resp = "mf_ok" in (await adb.shell(serial, "echo mf_ok")).get("stdout", "")
    checks.append({"name": "device responsive", "ok": resp, "detail": "adb responds" if resp else "no response",
                   "level": "required"})
    if not resp:
        return {"ok": False, "ready": False, "checks": checks}

    bb = await _installed(serial, BLACKBOX_PKG)
    checks.append({"name": "BlackBox removed", "ok": not bb,
                   "detail": "absent" if not bb else "still installed — remove it", "level": "required"})

    for name, pkg, purpose in REQUIRED:
        ins = await _installed(serial, pkg)
        detail = "installed" if ins else "NOT installed"
        if name == "AI Portal" and ins:
            try:
                detail = "installed + a11y on" if (await droidrun.ping(serial)).get("ok") else "installed (a11y off)"
            except Exception:  # noqa: BLE001
                pass
        checks.append({"name": name, "ok": ins, "detail": f"{detail} — {purpose}", "level": "required"})

    ready = all(c["ok"] for c in checks)
    return {"ok": ready, "ready": ready, "checks": checks}
