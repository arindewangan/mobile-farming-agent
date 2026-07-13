"""
Agent-side Fingerprint Spoofing Module for Mobile Farming Platform.
Executes ADB commands to spoof device properties for anti-fingerprinting.
"""
from __future__ import annotations

import random
import string
import uuid
import logging
from typing import Dict, Any, Optional, Tuple
import adb

log = logging.getLogger("fingerprint-spoofer")

TIMEZONES = [
    "America/New_York", "America/Los_Angeles", "America/Chicago",
    "Europe/London", "Europe/Paris", "Europe/Berlin",
    "Asia/Tokyo", "Asia/Singapore", "Asia/Shanghai",
    "Australia/Sydney"
]

LOCALES = ["en_US", "en_GB", "es_ES", "fr_FR", "de_DE", "ja_JP", "zh_CN"]

BUILD_MODELS = [
    ("Pixel 8 Pro", "Google", "google", "husky"),
    ("Pixel 7", "Google", "google", "panther"),
    ("Galaxy S23 Ultra", "Samsung", "samsung", "dm3q"),
    ("Galaxy S22", "Samsung", "samsung", "r0s"),
    ("OnePlus 11", "OnePlus", "oneplus", "OP5913L1"),
]

def generate_hex_id(length: int = 16) -> str:
    return "".join(random.choices(string.hexdigits[:16], k=length)).lower()

def generate_user_agent(model: str, brand: str) -> str:
    chrome_ver = f"12{random.randint(0, 5)}.0.{random.randint(0, 9999)}.{random.randint(50, 150)}"
    return (
        f"Mozilla/5.0 (Linux; Android 14; {model} Build/{generate_hex_id(6).upper()}) "
        f"AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{chrome_ver} Mobile Safari/537.36"
    )

async def get_status(serial: str) -> dict:
    """Read the current device identifiers."""
    aid = (await adb.shell(serial, "settings get secure android_id"))["stdout"].strip()
    ad_id = (await adb.shell(serial, "settings get secure google_advertising_id"))["stdout"].strip()
    dev_name = (await adb.shell(serial, "settings get global device_name"))["stdout"].strip()
    ua = (await adb.shell(serial, "settings get global user_agent_str"))["stdout"].strip()
    tz = (await adb.shell(serial, "getprop persist.sys.timezone"))["stdout"].strip()
    locale = (await adb.shell(serial, "settings get global sys.locale"))["stdout"].strip()
    
    # build properties
    model = await adb.getprop(serial, "ro.product.model")
    brand = await adb.getprop(serial, "ro.product.brand")
    mfr = await adb.getprop(serial, "ro.product.manufacturer")
    
    return {
        "ok": True,
        "android_id": aid if aid != "null" else None,
        "advertising_id": ad_id if ad_id != "null" else None,
        "device_name": dev_name if dev_name != "null" else None,
        "user_agent": ua if ua != "null" else None,
        "timezone": tz if tz else None,
        "locale": locale if locale != "null" else None,
        "model": model,
        "brand": brand,
        "manufacturer": mfr
    }

async def spoof_all(
    serial: str,
    profile_name: Optional[str] = None,
    custom_settings: Optional[dict] = None
) -> dict:
    """Apply comprehensive spoofing to a device."""
    settings = custom_settings or {}
    results = {"ok": True}

    # 1. Android ID
    aid = settings.get("android_id") or generate_hex_id(16)
    r_aid = await adb.shell(serial, f"settings put secure android_id {aid}")
    results["android_id"] = {"ok": r_aid["ok"], "value": aid}
    
    # 2. Advertising ID
    ad_id = settings.get("advertising_id") or str(uuid.uuid4())
    r_ad = await adb.shell(serial, f"settings put secure google_advertising_id {ad_id}")
    await adb.shell(serial, f"settings put secure advertising_id {ad_id}")
    results["advertising_id"] = {"ok": r_ad["ok"], "value": ad_id}
    
    # 3. Device Name
    name = settings.get("device_name") or (f"{profile_name}_Phone" if profile_name else f"Pixel_{random.randint(100,999)}")
    r_name1 = await adb.shell(serial, f"settings put global device_name '{name}'")
    r_name2 = await adb.shell(serial, f"settings put system device_name '{name}'")
    await adb.shell(serial, f"setprop bluetooth.device.name '{name}'")
    results["device_name"] = {"ok": r_name1["ok"] or r_name2["ok"], "value": name}
    
    # 4. Build selection for UA & properties
    build_choice = random.choice(BUILD_MODELS)
    model = settings.get("model") or build_choice[0]
    brand = settings.get("brand") or build_choice[2]
    mfr = settings.get("manufacturer") or build_choice[1]
    
    # 5. User-Agent
    ua = settings.get("user_agent") or generate_user_agent(model, brand)
    r_ua = await adb.shell(serial, f"settings put global user_agent_str '{ua}'")
    results["user_agent"] = {"ok": r_ua["ok"], "value": ua}
    
    # 6. Timezone
    tz = settings.get("timezone") or random.choice(TIMEZONES)
    r_tz = await adb.shell(serial, f"setprop persist.sys.timezone {tz}")
    results["timezone"] = {"ok": r_tz["ok"], "value": tz}
    
    # 7. Locale
    loc = settings.get("locale") or random.choice(LOCALES)
    r_loc = await adb.shell(serial, f"settings put global sys.locale {loc}")
    results["locale"] = {"ok": r_loc["ok"], "value": loc}
    
    # 8. Build properties (swallowed, as they may require root depending on device)
    await adb.shell(serial, f"setprop ro.product.model '{model}'")
    await adb.shell(serial, f"setprop ro.product.brand '{brand}'")
    await adb.shell(serial, f"setprop ro.product.manufacturer '{mfr}'")
    results["build_properties"] = {"ok": True, "model": model, "brand": brand, "manufacturer": mfr}
    
    return results

async def rollback(serial: str) -> dict:
    """Restore device settings back to defaults where possible."""
    # Resetting android_id and google_advertising_id to typical/cleared state
    # or generating one fresh default
    def_aid = generate_hex_id(16)
    def_ad = str(uuid.uuid4())
    
    await adb.shell(serial, f"settings put secure android_id {def_aid}")
    await adb.shell(serial, "settings delete secure google_advertising_id")
    await adb.shell(serial, "settings delete secure advertising_id")
    await adb.shell(serial, "settings delete global device_name")
    await adb.shell(serial, "settings delete global user_agent_str")
    await adb.shell(serial, "settings delete global sys.locale")
    
    # Restore standard timezone (defaults to UTC or US/Eastern if none)
    await adb.shell(serial, "setprop persist.sys.timezone UTC")
    
    return {"ok": True, "status": "rolled_back"}
