"""
Human-like input generation — the behavioral-realism layer.

Robotic input (instant straight-line swipes, zero-dwell taps, uniform typing)
is the single biggest behavioral bot-tell. This module produces motion that
looks human: curved (Bézier) swipes with ease-in/out velocity and jitter, taps
with landing scatter + press dwell + micro-movement, and typing with realistic
per-character cadence, natural pauses, and occasional typos that get corrected.

Gestures need multi-point touch, so they run over the scrcpy control channel
(ScrcpyControl.down/move/up). Typing runs over adb (per-character). Everything
is randomized per call — no two gestures are identical.
"""
from __future__ import annotations

import asyncio
import math
import random

import adb

# Adjacent keys on a QWERTY layout — used to make typos land on a neighbour,
# the way a real thumb slips, not a random character.
_ADJ = {
    "q": "wa", "w": "qeas", "e": "wrsd", "r": "etdf", "t": "ryfg", "y": "tugh",
    "u": "yijh", "i": "uojk", "o": "ipkl", "p": "ol", "a": "qwsz", "s": "awedxz",
    "d": "serfcx", "f": "drtgvc", "g": "ftyhbv", "h": "gyujnb", "j": "huiknm",
    "k": "jiolm", "l": "kop", "z": "asx", "x": "zsdc", "c": "xdfv", "v": "cfgb",
    "b": "vghn", "n": "bhjm", "m": "njk",
}


def _cubic(p0, p1, p2, p3, t):
    mt = 1 - t
    a, b, c, d = mt * mt * mt, 3 * mt * mt * t, 3 * mt * t * t, t * t * t
    return (a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0],
            a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1])


def _ease(t: float) -> float:
    """Ease-in-out — a finger accelerates then decelerates, never constant speed."""
    return 3 * t * t - 2 * t * t * t


def think_time(content_hint: int = 0) -> float:
    """A human 'reads' before acting. Returns seconds proportional to on-screen
    content plus noise; use before a deliberate action."""
    base = random.uniform(0.35, 1.4)
    return base + min(3.5, content_hint / 400.0) * random.uniform(0.5, 1.2)


async def human_tap(ctrl, x: float, y: float) -> None:
    """Tap with landing scatter, press dwell, and micro-movement during the hold."""
    jx, jy = x + random.gauss(0, 2.6), y + random.gauss(0, 2.6)
    await ctrl.down(jx, jy)
    await asyncio.sleep(random.uniform(0.045, 0.14))          # press dwell
    if random.random() < 0.5:                                  # slight finger creep
        await ctrl.move(jx + random.gauss(0, 1.2), jy + random.gauss(0, 1.2))
        await asyncio.sleep(random.uniform(0.01, 0.04))
    await ctrl.up(jx + random.gauss(0, 1.0), jy + random.gauss(0, 1.0))


async def human_swipe(ctrl, x1: float, y1: float, x2: float, y2: float,
                      duration: float | None = None) -> None:
    """Curved, variable-velocity swipe with jitter and a small overshoot+settle."""
    dx, dy = x2 - x1, y2 - y1
    dist = math.hypot(dx, dy)
    if duration is None:
        duration = max(0.22, dist / random.uniform(1500, 2700))  # human flick speed

    # Two control points offset perpendicular to the path → a natural arc.
    if dist > 1:
        nx, ny = -dy / dist, dx / dist
    else:
        nx, ny = 0.0, 0.0
    bow = random.uniform(-0.18, 0.18) * dist
    c1 = (x1 + dx * 0.30 + nx * bow * 0.7, y1 + dy * 0.30 + ny * bow * 0.7)
    c2 = (x1 + dx * 0.68 + nx * bow, y1 + dy * 0.68 + ny * bow)

    steps = max(14, int(duration / 0.012))
    await ctrl.down(x1, y1)
    for i in range(1, steps + 1):
        t = _ease(i / steps)
        px, py = _cubic((x1, y1), c1, c2, (x2, y2), t)
        await ctrl.move(px + random.gauss(0, 1.1), py + random.gauss(0, 1.1))
        await asyncio.sleep(duration / steps * random.uniform(0.65, 1.4))

    # Overshoot a touch past the target, then settle back — like a real flick.
    if dist > 120 and random.random() < 0.6:
        ox, oy = x2 + dx / dist * random.uniform(4, 16), y2 + dy / dist * random.uniform(4, 16)
        await ctrl.move(ox, oy)
        await asyncio.sleep(random.uniform(0.02, 0.05))
        await ctrl.move(x2, y2)
        await asyncio.sleep(random.uniform(0.01, 0.03))
    await ctrl.up(x2, y2)


async def human_scroll(ctrl, width: int, height: int, direction: str = "up",
                       amount: float = 1.0) -> None:
    """A natural content scroll (fling) in a direction, starting from a random
    point in the safe centre band. direction: up|down|left|right."""
    cx = width * random.uniform(0.35, 0.65)
    cy = height * random.uniform(0.4, 0.6)
    span = height * 0.5 * amount * random.uniform(0.8, 1.2)
    hspan = width * 0.5 * amount * random.uniform(0.8, 1.2)
    if direction == "up":       x1, y1, x2, y2 = cx, cy + span / 2, cx, cy - span / 2
    elif direction == "down":   x1, y1, x2, y2 = cx, cy - span / 2, cx, cy + span / 2
    elif direction == "left":   x1, y1, x2, y2 = cx + hspan / 2, cy, cx - hspan / 2, cy
    else:                       x1, y1, x2, y2 = cx - hspan / 2, cy, cx + hspan / 2, cy
    await human_swipe(ctrl, x1, y1, x2, y2)


async def watch_feed(ctrl, width: int, height: int, videos: int = 5,
                     min_watch: float = 8.0, max_watch: float = 45.0) -> dict:
    """Watch a short-form vertical video feed (Shorts / Reels / TikTok) like a human:
    variable watch time per clip, occasional early skip, pause/resume, the odd like,
    then a swipe to the next. Returns a summary of what it did."""
    watched = 0
    liked = 0
    for _ in range(videos):
        target = random.uniform(min_watch, max_watch)
        if random.random() < 0.22:                     # ~1 in 5: not interested, bail early
            target = random.uniform(1.5, min_watch)
        elapsed = 0.0
        while elapsed < target:
            dt = min(random.uniform(1.5, 5.0), target - elapsed)
            await asyncio.sleep(dt)
            elapsed += dt
            if random.random() < 0.12:                 # pause, look away, resume
                await human_tap(ctrl, width * 0.5, height * 0.5)
                await asyncio.sleep(random.uniform(0.6, 3.5))
                await human_tap(ctrl, width * 0.5, height * 0.5)
        if random.random() < 0.2:                       # double-tap like
            await human_tap(ctrl, width * 0.5, height * 0.55)
            await asyncio.sleep(random.uniform(0.05, 0.15))
            await human_tap(ctrl, width * 0.5, height * 0.55)
            liked += 1
        watched += 1
        await human_scroll(ctrl, width, height, "up", 1.0)   # next video
        await asyncio.sleep(random.uniform(0.4, 2.2))         # settle before next
    return {"ok": True, "watched": watched, "liked": liked}


async def human_type(serial: str, text: str, typo_rate: float = 0.04) -> dict:
    """Type text char-by-char with human cadence, natural pauses, and self-corrected
    typos. Uses adb input (per keystroke) so it works without scrcpy."""
    for ch in text:
        # Occasional typo on a letter: hit a neighbour, notice, backspace, fix.
        if ch.lower() in _ADJ and random.random() < typo_rate:
            wrong = random.choice(_ADJ[ch.lower()])
            if ch.isupper():
                wrong = wrong.upper()
            await adb.input_text(serial, wrong)
            await asyncio.sleep(random.uniform(0.12, 0.4))   # notice the mistake
            await adb.keyevent(serial, "KEYCODE_DEL")
            await asyncio.sleep(random.uniform(0.06, 0.18))

        await adb.input_text(serial, ch)

        # Per-character delay: base cadence + longer after spaces/punctuation.
        d = random.gauss(0.11, 0.045)
        if ch == " ":
            d += random.uniform(0.03, 0.12)
        elif ch in ".,!?":
            d += random.uniform(0.08, 0.25)
        if random.random() < 0.05:                            # occasional think-pause
            d += random.uniform(0.3, 0.9)
        await asyncio.sleep(max(0.02, d))
    return {"ok": True, "typed": len(text)}
