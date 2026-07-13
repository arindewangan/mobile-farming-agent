"""
Continuous crash/ANR monitoring — before this, logcat was only ever an
on-demand dump for the Logs panel; nothing watched for a crash loop quietly
killing a run in the background.

Polls Android's crash log buffer (app crashes) and the main buffer's
ActivityManager entries (ANRs) every CHECK_INTERVAL_SEC, dedupes against
what's already been reported (a small per-device rolling signature set, NOT
a `-T` timestamp cursor — logcat buffers are bounded and cheap enough to
re-dump at this interval, and this sidesteps any agent/device clock-skew a
time filter would risk), and reports genuinely new events to the control
plane so a crash loop shows up in that device's flag history instead of
silently killing runs unnoticed.
"""
from __future__ import annotations

import asyncio
import hashlib
from typing import Awaitable, Callable

import adb

CHECK_INTERVAL_SEC = 30.0
_SEEN_CAP = 300  # per-device rolling dedupe window

_seen: dict[str, set[str]] = {}
_watchers: dict[str, asyncio.Task] = {}

OnEvent = Callable[[str, dict], Awaitable[None]]


def _sig(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()[:16]


_CRASH_WINDOW_LINES = 5  # marker line + up to 4 following (Process:, exception, first frame)


def _parse_crashes(text: str) -> list[dict]:
    """Each app crash logs 'FATAL EXCEPTION: <thread>' then, within the next
    couple of lines, 'Process: <pkg>, PID: <n>'. Scans a FIXED NUMBER OF
    LINES after each marker (capped at the next marker, if closer) — not a
    fixed character count and not "everything up to the next marker".
    Either of those makes a crash's signature depend on whatever unrelated
    text happens to follow it in the buffer (a later, unrelated crash
    appended further down shifts where "the next marker" falls, which
    changes an EARLIER crash's own captured text and therefore its dedupe
    hash — a real bug this exact scenario caught in test_crashmonitor.py).
    A fixed line count anchored to the marker itself has no such
    dependency on what comes later."""
    lines = text.splitlines()
    marker_lines = [i for i, ln in enumerate(lines) if "FATAL EXCEPTION" in ln]

    out: list[dict] = []
    for n, li in enumerate(marker_lines):
        next_li = marker_lines[n + 1] if n + 1 < len(marker_lines) else len(lines)
        window_lines = lines[li:min(next_li, li + _CRASH_WINDOW_LINES)]
        window = "\n".join(window_lines)
        pkg = "unknown"
        for wl in window_lines:
            if "Process:" in wl:
                rest = wl.split("Process:", 1)[1].strip()
                pkg = rest.split(",")[0].strip() or "unknown"
                break
        out.append({"kind": "crash", "package": pkg, "detail": window.replace("\n", " ").strip()[:300]})
    return out


def _parse_anrs(text: str) -> list[dict]:
    out: list[dict] = []
    idx = 0
    while True:
        i = text.find("ANR in ", idx)
        if i == -1:
            break
        line_end = text.find("\n", i)
        line = text[i:line_end if line_end != -1 else None]
        idx = i + len("ANR in ")
        pkg = line[len("ANR in "):].split()[0].split("(")[0].strip().rstrip(",") or "unknown"
        out.append({"kind": "anr", "package": pkg, "detail": line.strip()[:300]})
    return out


async def _poll_once(serial: str) -> list[dict]:
    seen = _seen.setdefault(serial, set())
    events: list[dict] = []

    crash_out = (await adb.shell(serial, "logcat -b crash -d -v brief")).get("stdout", "")
    anr_out = (await adb.shell(serial, "logcat -b main -d -v brief -s ActivityManager:I")).get("stdout", "")

    for ev in _parse_crashes(crash_out) + _parse_anrs(anr_out):
        sig = _sig(ev["detail"])
        if sig in seen:
            continue
        seen.add(sig)
        events.append(ev)

    if len(seen) > _SEEN_CAP:
        # Sets don't preserve insertion order — this trims arbitrarily, which
        # is fine, it's a dedupe cap, not a log; worst case a very old signal
        # could theoretically repeat after the cap evicts it.
        _seen[serial] = set(list(seen)[-_SEEN_CAP:])
    return events


async def _watch(serial: str, on_event: OnEvent) -> None:
    # First poll only establishes the dedupe baseline — a crash from before
    # monitoring started shouldn't be reported as "new" the instant it does.
    try:
        await _poll_once(serial)
    except Exception:  # noqa: BLE001
        pass
    while True:
        await asyncio.sleep(CHECK_INTERVAL_SEC)
        try:
            for ev in await _poll_once(serial):
                await on_event(serial, ev)
        except Exception:  # noqa: BLE001
            pass


def start(serial: str, on_event: OnEvent) -> None:
    if serial in _watchers and not _watchers[serial].done():
        return
    _watchers[serial] = asyncio.create_task(_watch(serial, on_event))


def stop(serial: str) -> None:
    t = _watchers.pop(serial, None)
    if t:
        t.cancel()
    _seen.pop(serial, None)
