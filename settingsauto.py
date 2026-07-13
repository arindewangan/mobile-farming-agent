"""
Common Settings app actions — no root. Well-known `android.settings.*`
intents plus a handful of safe `settings`/`svc`/`pm` shell commands (Wi-Fi
toggle, airplane mode, clear app data, per-app permission grant/revoke).
"""
from __future__ import annotations

import adb

# name -> intent action, for jumping straight to a settings screen.
PAGES = {
    "wifi": "android.settings.WIFI_SETTINGS",
    "bluetooth": "android.settings.BLUETOOTH_SETTINGS",
    "apps": "android.settings.APPLICATION_SETTINGS",
    "accounts": "android.settings.SYNC_SETTINGS",
    "date_time": "android.settings.DATE_SETTINGS",
    "locale": "android.settings.LOCALE_SETTINGS",
    "display": "android.settings.DISPLAY_SETTINGS",
    "storage": "android.settings.INTERNAL_STORAGE_SETTINGS",
    "battery": "android.settings.BATTERY_SAVER_SETTINGS",
    "security": "android.settings.SECURITY_SETTINGS",
    "developer": "android.settings.APPLICATION_DEVELOPMENT_SETTINGS",
    "main": "android.settings.SETTINGS",
}


async def open_page(serial: str, page: str) -> dict:
    action = PAGES.get(page)
    if not action:
        return {"ok": False, "error": f"unknown settings page '{page}' — known: {sorted(PAGES)}"}
    r = await adb.shell(serial, f"am start -a {action}")
    return {"ok": r["ok"], "page": page}


async def open_app_settings(serial: str, package: str) -> dict:
    """The per-app 'App info' screen (permissions, storage, force stop)."""
    r = await adb.shell(
        serial, f"am start -a android.settings.APPLICATION_DETAILS_SETTINGS -d package:{package}")
    return {"ok": r["ok"], "package": package}


async def set_wifi(serial: str, enabled: bool) -> dict:
    r = await adb.shell(serial, f"svc wifi {'enable' if enabled else 'disable'}")
    return {"ok": r["ok"], "wifi": enabled}


async def set_airplane_mode(serial: str, enabled: bool) -> dict:
    await adb.shell(serial, f"settings put global airplane_mode_on {1 if enabled else 0}")
    r = await adb.shell(
        serial, f"am broadcast -a android.intent.action.AIRPLANE_MODE --ez state {'true' if enabled else 'false'}")
    return {"ok": r["ok"], "airplane_mode": enabled}


async def clear_app_data(serial: str, package: str) -> dict:
    """`pm clear` — resets an app to first-install state (its own data only,
    no root, no effect on other apps)."""
    r = await adb.shell(serial, f"pm clear {package}")
    ok = "Success" in r.get("stdout", "")
    return {"ok": ok, "package": package, "detail": r.get("stdout", "").strip()}


async def set_app_permission(serial: str, package: str, permission: str, grant: bool) -> dict:
    verb = "grant" if grant else "revoke"
    r = await adb.shell(serial, f"pm {verb} {package} {permission}")
    return {"ok": r["ok"], "package": package, "permission": permission, "granted": grant}
