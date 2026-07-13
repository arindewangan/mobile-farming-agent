"""
Device onboarding — turn a fresh board into a farm-ready device in one call.

Installs and configures everything the platform needs, each step reporting its own
status so a partial failure is visible:
  • Portal        — mobilerun accessibility Portal for AI UI automation, a11y enabled
  • prep          — keep the screen awake while charging (bench boards), sane timeout

Deliberately does NOT disable system animations — real phones animate, and we want
the fleet to look human.
"""
from __future__ import annotations

import adb
import droidrun


async def install_portal(serial: str) -> dict:
    try:
        await droidrun.setup(serial)
        ok = await droidrun.ping(serial)
        return {"name": "portal", "ok": bool(ok.get("ok")),
                "detail": "Portal installed + accessibility enabled" if ok.get("ok") else "installed; a11y not confirmed"}
    except Exception as e:  # noqa: BLE001
        return {"name": "portal", "ok": False, "detail": str(e)}


async def prep_device(serial: str) -> dict:
    try:
        await adb.shell(serial, "svc power stayon true")
        await adb.shell(serial, "settings put system screen_off_timeout 1800000")  # 30 min
        return {"name": "prep", "ok": True, "detail": "stay-awake while charging, long screen timeout"}
    except Exception as e:  # noqa: BLE001
        return {"name": "prep", "ok": False, "detail": str(e)}


async def setup_device(serial: str, options: dict | None = None) -> dict:
    """Run the selected onboarding steps in order. options: portal/prep."""
    o = options or {}
    steps = []
    if o.get("portal", True):
        steps.append(await install_portal(serial))
    if o.get("prep", True):
        steps.append(await prep_device(serial))
    return {"ok": all(s["ok"] for s in steps) if steps else False, "serial": serial, "steps": steps}
