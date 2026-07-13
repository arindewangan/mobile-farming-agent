"""
Device readiness / preflight — verify a device is in a runnable state BEFORE
firing automation, so a recipe doesn't fail halfway on a locked screen, a dead
battery, or a missing dependency.

Returns a structured checklist. Each check has a `level`:
  * required — must pass for the device to run automation at all (responsive,
    awake, unlocked, has storage). These decide the overall `ok`.
  * info     — advisory / feature-specific (battery low, AI Portal for ai_task
    recipes, proxy tunnel state). Reported but don't block.

Kept fast: no egress curl (the tunnel check reads local state only).
"""
from __future__ import annotations

import re

import adb
import clashtunnel
import droidrun


async def check(serial: str) -> dict:
    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str, level: str = "required") -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail, "level": level})

    # 1) responsive — if adb shell is dead, nothing else matters
    r = await adb.shell(serial, "echo mf_ready")
    resp = r["ok"] and "mf_ready" in r["stdout"]
    add("device responsive", resp, "adb shell responds" if resp else "no adb response")
    if not resp:
        return {"ok": False, "checks": checks}

    # 2) screen awake
    pw = (await adb.shell(serial, "dumpsys power | grep -m2 -E 'mWakefulness=|Display Power'"))["stdout"]
    awake = "Awake" in pw or "state=ON" in pw
    add("screen awake", awake, "awake" if awake else "asleep — wake it first")

    # 3) unlocked (no keyguard covering the screen)
    kg = (await adb.shell(serial, "dumpsys window 2>/dev/null | grep -m1 mDreamingLockscreen"))["stdout"]
    locked = "mDreamingLockscreen=true" in kg
    add("unlocked", not locked, "keyguard up — unlock needed" if locked else "unlocked")

    # 3b) orientation — must be locked to portrait (auto-rotate off). Flagged as
    # required even if auto-rotate is currently ON, since that's exactly the
    # broken state the operator needs to fix (via the orientation toggle).
    try:
        o = await adb.orientation_status(serial)
        port_ok = bool(o.get("portrait")) and not o.get("auto_rotate")
        if o.get("auto_rotate"):
            odetail = "auto-rotate is ON — lock to portrait"
        elif not o.get("portrait"):
            odetail = f"rotated (ROTATION_{o.get('rotation')}) — not portrait"
        else:
            odetail = "locked to portrait"
        add("orientation", port_ok, odetail)
    except Exception as e:  # noqa: BLE001
        add("orientation", False, str(e))

    # 4) storage headroom on /data
    dfout = (await adb.shell(serial, "df /data | tail -1"))["stdout"]
    m = re.search(r"(\d+)%", dfout)
    used = int(m.group(1)) if m else 0
    add("storage", used < 95, f"{used}% used" if m else "unknown")

    # 5) battery + thermal (info — bench boards charge, but flag a low or hot one).
    # One dumpsys call for both: `dumpsys battery` reports temperature in tenths
    # of a degree C (e.g. "temperature: 320" = 32.0°C) alongside level.
    bat = (await adb.shell(serial, "dumpsys battery"))["stdout"]
    bm = re.search(r"level:\s*(\d+)", bat)
    lvl = int(bm.group(1)) if bm else 100
    add("battery", lvl >= 15, f"{lvl}%", level="info")

    tm = re.search(r"temperature:\s*(\d+)", bat)
    temp_c = int(tm.group(1)) / 10 if tm else None
    if temp_c is not None:
        # >45°C is where phone SoCs commonly start throttling — worth a flag
        # well before it gets there, since a run degrades silently, not loudly.
        add("thermal", temp_c < 45, f"{temp_c:.1f}°C", level="info")

    # 5b) memory pressure — MemAvailable/MemTotal, same no-root /proc read used
    # by the control-plane host (retention.py's disk check is the analogous
    # host-side signal; this is the on-device one).
    mem = (await adb.shell(serial, "cat /proc/meminfo"))["stdout"]
    mt_m = re.search(r"MemTotal:\s*(\d+)", mem)
    ma_m = re.search(r"MemAvailable:\s*(\d+)", mem)
    if mt_m and ma_m and int(mt_m.group(1)):
        mem_used_pct = round(100 * (1 - int(ma_m.group(1)) / int(mt_m.group(1))), 1)
        add("memory", mem_used_pct < 90, f"{mem_used_pct}% used", level="info")

    # 5c) CPU load — dumpsys cpuinfo's summary "NN% TOTAL" line.
    cpu = (await adb.shell(serial, "dumpsys cpuinfo | grep -m1 TOTAL"))["stdout"]
    cm = re.search(r"(\d+)%\s*TOTAL", cpu)
    if cm:
        cpu_pct = int(cm.group(1))
        add("cpu load", cpu_pct < 90, f"{cpu_pct}%", level="info")

    # 5d) recent app crashes — Android keeps a dedicated crash log buffer
    # (separate from the main log, ANRs are NOT in it — those live in the
    # main/system buffer instead, which is what crashmonitor.py's continuous
    # watcher polls) that survives independently; any entry means something
    # crashed since the buffer last rotated. Advisory only — a crash from
    # days ago shouldn't block a new run, but it's worth surfacing instead of
    # staying invisible until a run mysteriously fails.
    crash = (await adb.shell(serial, "logcat -b crash -d -v brief"))["stdout"]
    crash_count = crash.count("FATAL EXCEPTION")
    add("recent crashes", crash_count == 0,
        "none" if crash_count == 0 else f"{crash_count} crash entr{'y' if crash_count == 1 else 'ies'} in the log buffer",
        level="info")

    # 6) AI Portal — required only for ai_task steps/recipes
    try:
        pr = await droidrun.ping(serial)
        portal = bool(pr.get("ok"))
    except Exception:  # noqa: BLE001
        portal = False
    add("AI Portal (ai_task)", portal,
        "installed + accessibility on" if portal else "not set up (only needed for AI-task recipes)",
        level="info")

    # 7) proxy tunnel — local state only (installed / running / kill-switch)
    try:
        installed = await clashtunnel.is_installed(serial)
        tun = await clashtunnel.tun_up(serial) if installed else False
        lock = await clashtunnel.is_lockdown_armed(serial) if installed else False
        detail = ("not installed" if not installed
                  else (f"up{' + kill-switch armed' if lock else ' (no kill-switch)'}" if tun
                        else "installed but not running"))
    except Exception as e:  # noqa: BLE001
        tun, detail = False, str(e)
    add("proxy tunnel", tun, detail, level="info")

    overall = all(c["ok"] for c in checks if c["level"] == "required")
    return {"ok": overall, "checks": checks}
