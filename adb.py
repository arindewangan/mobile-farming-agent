"""
Thin async wrapper around the `adb` CLI.

Every function targets a specific device via `-s <serial>` so one agent can own
many phones. All calls are async subprocesses so a slow device never blocks the
agent's event loop.
"""
from __future__ import annotations

import asyncio
import io
import re
from typing import Optional

from PIL import Image

ADB = "adb"


async def _run(args: list[str], input_bytes: Optional[bytes] = None, timeout: float = 30.0):
    proc = await asyncio.create_subprocess_exec(
        ADB,
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.PIPE if input_bytes is not None else None,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(input=input_bytes), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode or 0, out, err


async def reconnect_offline() -> None:
    """Watchdog: re-establish any devices stuck 'offline' (the bare-board bench
    drops USB under load). No-op when nothing is offline."""
    _, out, _ = await _run(["devices"], timeout=10)
    for line in out.decode(errors="replace").splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "offline":
            try:
                await _run(["-s", parts[0], "reconnect"], timeout=10)
            except Exception:  # noqa: BLE001
                pass


async def list_serials() -> list[str]:
    """Serials of devices currently in the `device` (ready) state."""
    _, out, _ = await _run(["devices"])
    serials = []
    for line in out.decode(errors="replace").splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            serials.append(parts[0])
    return serials


async def getprop(serial: str, prop: str) -> str:
    _, out, _ = await _run(["-s", serial, "shell", "getprop", prop])
    return out.decode(errors="replace").strip()


async def connect_wifi(host: str, port: int = 5555) -> dict:
    """Add a device over the network — it must already have ADB-over-WiFi
    enabled (either `enable_tcpip` below, run once over USB, or the phone's
    own Developer Options > Wireless debugging on Android 11+). Idempotent:
    reconnecting an already-connected device just reports 'already connected'.

    adb's own connect timeout (esp. on an unreachable/filtered host) can run
    close to the OS TCP timeout, so this uses a generous timeout AND catches
    it explicitly — asyncio.TimeoutError has no message by default, which
    would otherwise surface to the operator as a blank error."""
    try:
        rc, out, err = await _run(["connect", f"{host}:{port}"], timeout=25)
    except asyncio.TimeoutError:
        return {"ok": False, "error": "timed out reaching the host", "detail": "timed out reaching the host"}
    text = (out + err).decode(errors="replace").strip()
    ok = rc == 0 and ("connected to" in text.lower() or "already connected" in text.lower())
    return {"ok": ok, "error": None if ok else (text or "connect failed"), "detail": text}


async def disconnect_wifi(host: str, port: int = 5555) -> dict:
    try:
        rc, out, err = await _run(["disconnect", f"{host}:{port}"], timeout=15)
    except asyncio.TimeoutError:
        return {"ok": False, "error": "timed out", "detail": "timed out"}
    text = (out + err).decode(errors="replace").strip()
    return {"ok": rc == 0, "error": None if rc == 0 else (text or "disconnect failed"), "detail": text}


async def enable_tcpip(serial: str, port: int = 5555) -> dict:
    """Switch an already-USB-attached device into network-ADB mode and report
    its IP, so the operator can then unplug it and `connect_wifi` to that IP.
    Needs the device to already be visible over USB (or already reachable)."""
    rc, out, err = await _run(["-s", serial, "tcpip", str(port)], timeout=15)
    if rc != 0:
        text = (out + err).decode(errors="replace").strip()
        return {"ok": False, "error": text or "tcpip mode failed", "detail": text}
    wifi_ip = await _wifi_ip(serial)
    detail = f"listening on {wifi_ip}:{port}" if wifi_ip else "enabled, but couldn't read the WiFi IP"
    return {"ok": True, "ip": wifi_ip, "port": port, "detail": detail}


async def _wifi_ip(serial: str) -> str | None:
    r = await _run(["-s", serial, "shell", "ip", "route", "get", "1"], timeout=10)
    out = r[1].decode(errors="replace")
    # "1.0.0.0 via 192.168.1.1 dev wlan0 src 192.168.1.42 ..." — take the src addr
    m = re.search(r"src (\d+\.\d+\.\d+\.\d+)", out)
    return m.group(1) if m else None


async def screen_size(serial: str) -> tuple[int, int]:
    """Physical screen size in pixels via `wm size`. Falls back to 1080x1920."""
    _, out, _ = await _run(["-s", serial, "shell", "wm", "size"])
    text = out.decode(errors="replace")
    # Prefer "Override size" if present, else "Physical size".
    m = re.search(r"Override size:\s*(\d+)x(\d+)", text) or re.search(
        r"Physical size:\s*(\d+)x(\d+)", text
    )
    if m:
        return int(m.group(1)), int(m.group(2))
    return 1080, 1920


async def device_info(serial: str) -> dict:
    model = await getprop(serial, "ro.product.model")
    android = await getprop(serial, "ro.build.version.release")
    sdk = await getprop(serial, "ro.build.version.sdk")
    abi = await getprop(serial, "ro.product.cpu.abi")
    w, h = await screen_size(serial)
    return {
        "model": model or "unknown",
        "android": android or "?",
        "sdk": sdk or "?",
        "abi": abi or "?",
        "width": w,
        "height": h,
        "state": "device",
    }


async def shell(serial: str, cmd: str) -> dict:
    rc, out, err = await _run(["-s", serial, "shell", cmd], timeout=60)
    return {
        "ok": rc == 0,
        "rc": rc,
        "stdout": out.decode(errors="replace"),
        "stderr": err.decode(errors="replace"),
    }


async def install(serial: str, apk_path: str) -> dict:
    rc, out, err = await _run(["-s", serial, "install", "-r", apk_path], timeout=300)
    output = (out + err).decode(errors="replace")
    return {"ok": rc == 0 and "Success" in output, "output": output}


async def apk_paths(serial: str, package: str) -> list[str]:
    """On-device APK path(s) for an installed package — a modern app is often a
    base.apk PLUS split_config.* APKs, so this returns a list."""
    rc, out, _ = await _run(["-s", serial, "shell", "pm", "path", package], timeout=20)
    return [ln.split("package:", 1)[1].strip()
            for ln in out.decode(errors="replace").splitlines() if ln.startswith("package:")]


async def clone_package(from_serial: str, to_serials: list, package: str, timeout: float = 600.0) -> dict:
    """Copy an installed package's APK(s) from one device to others — no
    download, it's the source device's own (Google-signed) binary. Pulls every
    part (base + splits) to a temp dir on the agent host, then installs them on
    each target with install-multiple (or install for a single APK). Used to
    clone a modern WebView from a device that has it onto ones stuck on an old
    version. `-r` reinstalls keeping data, `-d` allows the version-code check to
    pass for a system-app update."""
    import os
    import shutil
    import tempfile
    paths = await apk_paths(from_serial, package)
    if not paths:
        return {"ok": False, "error": f"{package} not installed on source {from_serial}"}
    work = tempfile.mkdtemp(prefix="mf_clone_")
    local: list[str] = []
    try:
        for i, p in enumerate(paths):
            dest = os.path.join(work, f"part{i}.apk")
            rc, out, err = await _run(["-s", from_serial, "pull", p, dest], timeout=240)
            if rc != 0 or not os.path.exists(dest):
                return {"ok": False, "error": f"pull {p} failed: {(out + err).decode(errors='replace')[:200]}"}
            local.append(dest)
        results: dict = {}
        for ser in to_serials:
            if len(local) == 1:
                rc, out, err = await _run(["-s", ser, "install", "-r", "-d", local[0]], timeout=300)
            else:
                rc, out, err = await _run(["-s", ser, "install-multiple", "-r", "-d", *local], timeout=360)
            output = (out + err).decode(errors="replace").strip()
            results[ser] = {"ok": rc == 0 and "Success" in output, "output": output[:300]}
        ok = all(r["ok"] for r in results.values()) if results else False
        return {"ok": ok, "package": package, "source": from_serial,
                "parts": len(local), "results": results}
    finally:
        shutil.rmtree(work, ignore_errors=True)


async def tap(serial: str, x: int, y: int) -> dict:
    rc, _, err = await _run(["-s", serial, "shell", "input", "tap", str(x), str(y)])
    return {"ok": rc == 0, "error": err.decode(errors="replace")}


async def swipe(serial: str, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 200) -> dict:
    rc, _, err = await _run(
        ["-s", serial, "shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(duration_ms)]
    )
    return {"ok": rc == 0, "error": err.decode(errors="replace")}


async def input_text(serial: str, text: str) -> dict:
    # The argument is parsed by the DEVICE'S shell, so any shell metacharacter
    # (; & | < > ( ) ' " ` $ etc.) is interpreted there — escaping only spaces
    # both breaks ordinary text (ampersands, quotes, parentheses in a search
    # query or password) and is a command-injection vector. Wrap the whole
    # payload in single quotes for the device shell (escaping embedded single
    # quotes the '\'' way), and substitute spaces with %s INSIDE the quotes,
    # since `input text` itself wants spaces as %s.
    escaped = text.replace("'", "'\\''").replace(" ", "%s")
    rc, _, err = await _run(["-s", serial, "shell", "input", "text", f"'{escaped}'"])
    return {"ok": rc == 0, "error": err.decode(errors="replace")}


async def keyevent(serial: str, keycode: str) -> dict:
    rc, _, err = await _run(["-s", serial, "shell", "input", "keyevent", keycode])
    return {"ok": rc == 0, "error": err.decode(errors="replace")}


async def reboot(serial: str) -> dict:
    rc, _, err = await _run(["-s", serial, "reboot"], timeout=20)
    return {"ok": rc == 0, "error": err.decode(errors="replace")}


# ---- network proxy (no-root: global HTTP proxy via settings) --------------
async def set_proxy(serial: str, host: str, port: int) -> dict:
    r = await shell(serial, f"settings put global http_proxy {host}:{port}")
    return {"ok": r["ok"], "proxy": f"{host}:{port}", "output": r["stdout"] + r["stderr"]}


async def clear_proxy(serial: str) -> dict:
    # ":0" first is the reliable way to disable; then remove the key entirely.
    await shell(serial, "settings put global http_proxy :0")
    await shell(serial, "settings delete global http_proxy")
    return {"ok": True, "proxy": None}


async def get_proxy(serial: str) -> dict:
    r = await shell(serial, "settings get global http_proxy")
    val = r["stdout"].strip()
    if val in ("", "null", ":0"):
        val = None
    return {"ok": True, "proxy": val}


# ---- app / profile helpers ------------------------------------------------
async def list_apps(serial: str, third_party: bool = True) -> dict:
    flag = "-3" if third_party else ""
    r = await shell(serial, f"pm list packages {flag}")
    pkgs = [
        ln.replace("package:", "").strip()
        for ln in r["stdout"].splitlines()
        if ln.startswith("package:")
    ]
    return {"ok": True, "packages": sorted(pkgs)}


async def launch_package(serial: str, pkg: str) -> dict:
    r = await shell(serial, f"monkey -p {pkg} -c android.intent.category.LAUNCHER 1")
    ok = "No activities found" not in (r["stdout"] + r["stderr"])
    return {"ok": ok, "output": (r["stdout"] + r["stderr"]).strip()}


async def force_stop(serial: str, pkg: str) -> dict:
    r = await shell(serial, f"am force-stop {pkg}")
    return {"ok": r["ok"]}


# ---- clean-slate / restore defaults ---------------------------------------
async def _clear_recents(serial: str) -> str:
    """Empty the recents/overview. `am kill-all` frees memory but leaves the
    overview thumbnails, so we open the overview and tap its 'Close all' button,
    located via uiautomator so it works in any orientation / launcher layout."""
    await keyevent(serial, "KEYCODE_APP_SWITCH")
    await asyncio.sleep(1.3)
    await shell(serial, "uiautomator dump /sdcard/mf_ui.xml")
    xml = (await shell(serial, "cat /sdcard/mf_ui.xml"))["stdout"]
    for node in re.findall(r"<node\b[^>]*?/>", xml):
        low = node.lower()
        if 'close all' in low or 'clear all' in low:
            m = re.search(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', node)
            if m:
                x1, y1, x2, y2 = map(int, m.groups())
                await tap(serial, (x1 + x2) // 2, (y1 + y2) // 2)
                await asyncio.sleep(0.6)
                await keyevent(serial, "KEYCODE_HOME")
                return "closed all recents"
    await keyevent(serial, "KEYCODE_HOME")  # empty (no button) — just leave overview
    return "recents already empty"


async def set_orientation(serial: str, mode: str = "portrait") -> dict:
    """Lock (or free) the display orientation. NB some apps — notably the YouTube
    player — request sensor orientation and, on a physically tilted bench phone,
    flip to landscape *overriding* the system lock. Re-asserting the lock while
    the app is foreground forces it back and holds, so callers should apply this
    AFTER launching the app (and periodically). Non-root, no reboot.

      portrait  → auto-rotate off, user_rotation 0
      landscape → auto-rotate off, user_rotation 1
      auto      → auto-rotate on (follow the sensor)
    """
    mode = (mode or "portrait").lower()
    if mode == "auto":
        await shell(serial, "settings put system accelerometer_rotation 1")
    else:
        rot = 1 if mode in ("landscape", "land") else 0
        await shell(serial, "settings put system accelerometer_rotation 0")
        await shell(serial, f"settings put system user_rotation {rot}")
    cur = (await shell(serial, "dumpsys window | grep mCurrentRotation")).get("stdout", "")
    m = re.search(r"ROTATION_(\d+)", cur)
    return {"ok": True, "mode": mode, "rotation": int(m.group(1)) if m else None}


async def logcat_dump(serial: str, lines: int = 400) -> dict:
    """One-shot recent logcat (last N lines) — a dump, not a live stream, so
    it's cheap enough to poll on a timer from the Logs panel."""
    lines = max(50, min(int(lines or 400), 4000))
    rc, out, err = await _run(["-s", serial, "logcat", "-d", "-t", str(lines), "-v", "threadtime"], timeout=20)
    if rc != 0 and not out:
        return {"ok": False, "text": err.decode(errors="replace")}
    return {"ok": True, "text": out.decode(errors="replace")}


async def orientation_status(serial: str) -> dict:
    """Read-only: is auto-rotate on, and what's the current on-screen rotation.
    Does not change anything (unlike set_orientation) — used by status checks."""
    auto = (await shell(serial, "settings get system accelerometer_rotation")).get("stdout", "").strip()
    auto_on = auto == "1"
    cur = (await shell(serial, "dumpsys window | grep mCurrentRotation")).get("stdout", "")
    m = re.search(r"ROTATION_(\d+)", cur)
    rot = int(m.group(1)) if m else None
    return {"auto_rotate": auto_on, "rotation": rot, "portrait": rot == 0}


async def restore_defaults(serial: str) -> dict:
    """Return a device to a clean bench default: auto-rotate off (locked portrait),
    home screen, recents cleared, background apps killed, notification shade
    collapsed, stay-awake while charging. Each sub-step reports its own status."""
    steps: list[dict] = []

    async def _do(desc: str, cmd: str) -> None:
        r = await shell(serial, cmd)
        steps.append({"step": desc, "ok": r["ok"]})

    await _do("auto-rotate off", "settings put system accelerometer_rotation 0")
    await _do("lock portrait", "settings put system user_rotation 0")
    await keyevent(serial, "KEYCODE_HOME")
    steps.append({"step": "home screen", "ok": True})
    try:
        detail = await _clear_recents(serial)
        steps.append({"step": "clear recents", "ok": True, "detail": detail})
    except Exception as e:  # noqa: BLE001
        steps.append({"step": "clear recents", "ok": False, "detail": str(e)})
    await _do("kill background apps", "am kill-all")
    await _do("collapse status bar", "cmd statusbar collapse")
    await _do("stay awake while charging", "svc power stayon true")
    await keyevent(serial, "KEYCODE_HOME")
    return {"ok": all(s["ok"] for s in steps), "steps": steps}


# ---- emulator console passthrough -----------------------------------------
async def emu(serial: str, args: str) -> dict:
    """`adb emu <args>` — emulator-only. e.g. 'network delay gprs',
    'network speed edge', 'geo fix <lon> <lat>'. No-ops/errors on real devices.
    """
    rc, out, err = await _run(["-s", serial, "emu", *args.split()], timeout=15)
    output = (out + err).decode(errors="replace").strip()
    ok = rc == 0 and "KO" not in output
    return {"ok": ok, "output": output}


async def start_screenrecord(
    serial: str, bit_rate: int = 8_000_000, time_limit: int = 180
) -> asyncio.subprocess.Process:
    """Spawn `screenrecord` emitting a raw H.264 stream to stdout.

    `exec-out` keeps the byte stream binary-clean (no shell CRLF mangling).
    `--time-limit` maxes at 180s on most devices, so the agent restarts the
    process on EOF. stderr is discarded to avoid a full-pipe deadlock.
    """
    return await asyncio.create_subprocess_exec(
        ADB,
        "-s",
        serial,
        "exec-out",
        "screenrecord",
        "--output-format=h264",
        "--bit-rate",
        str(bit_rate),
        "--time-limit",
        str(time_limit),
        "-",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )


async def screencap_full_jpeg(serial: str, quality: int = 85) -> Optional[bytes]:
    """Full-resolution JPEG for test-run evidence (vs the downscaled mirror one)."""
    rc, out, _ = await _run(["-s", serial, "exec-out", "screencap", "-p"], timeout=15)
    if rc != 0 or not out:
        return None
    try:
        img = Image.open(io.BytesIO(out)).convert("RGB")
    except Exception:
        return None
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


async def screencap_jpeg(serial: str, max_width: int = 480, quality: int = 55) -> Optional[bytes]:
    """Capture the screen and return a downscaled JPEG (bandwidth-friendly).

    `exec-out screencap -p` returns raw PNG bytes with no shell mangling.
    Pillow downscales + re-encodes to keep frames small for the browser.
    """
    rc, out, _ = await _run(["-s", serial, "exec-out", "screencap", "-p"], timeout=15)
    if rc != 0 or not out:
        return None
    try:
        img = Image.open(io.BytesIO(out)).convert("RGB")
    except Exception:
        return None
    if img.width > max_width:
        ratio = max_width / img.width
        img = img.resize((max_width, int(img.height * ratio)))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()
