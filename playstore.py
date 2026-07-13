"""
Install an app from the Play Store by package name — no root. Opens the
Store's own listing page via a market:// deep link and taps the visible
Install button, then polls until the package actually lands on the device.

Distinct from the existing `install` action (adb `pm install` of a
sideloaded APK file) — this one goes through the real Play Store UI, the
same path a person tapping "Install" would take.
"""
from __future__ import annotations

import asyncio
import time

import adb


async def _installed(serial: str, package: str) -> bool:
    r = await adb.shell(serial, f"pm path {package}")
    return "package:" in r.get("stdout", "")


async def install_app(serial: str, package: str, timeout: float = 180.0) -> dict:
    if await _installed(serial, package):
        return {"ok": True, "package": package, "detail": "already installed — no-op"}

    await adb.shell(serial, f"am start -a android.intent.action.VIEW -d market://details?id={package}")
    await asyncio.sleep(2.5)

    import recipeui  # local import: only this path needs the uiautomator dump helper

    tapped_install = False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await _installed(serial, package):
            return {"ok": True, "package": package, "detail": "installed"}
        st = await recipeui.ui_state(
            serial, ["Install", "Open", "This app is not available", "isn't compatible", "Uninstall"])
        m = st.get("matches", {})
        if m.get("This app is not available", {}).get("present") or m.get("isn't compatible", {}).get("present"):
            return {"ok": False, "error": "app not available for this device/account/region"}
        if not tapped_install and m.get("Install", {}).get("present"):
            hit = m["Install"]
            await adb.tap(serial, hit["x"], hit["y"])
            tapped_install = True
            await asyncio.sleep(1.5)
            continue
        if tapped_install and (m.get("Open", {}).get("present") or m.get("Uninstall", {}).get("present")):
            # The button flipped from Install to Open/Uninstall — installed,
            # even if `pm path` hasn't caught up by the next poll.
            return {"ok": True, "package": package, "detail": "installed"}
        await asyncio.sleep(2.0)

    return {"ok": False, "error": f"timed out waiting for {package} to install"}
