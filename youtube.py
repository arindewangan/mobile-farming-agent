"""
Human-like YouTube behaviour flows (FUD-oriented) with robust screen handling.

Everything here is deliberately noisy and non-deterministic — variable watch
durations, reading pauses before typing, typos, curved finger swipes, occasional
comment-scrolling / seeks / likes, ad-skip + popup handling, and randomised
navigation. The touch + timing + navigation trace is meant to be statistically
indistinguishable from a real person holding the phone.

**Nothing here is hard-coded.** Every probability, timing range, screen
coordinate and on-screen selector comes from a *behaviour profile*
(``yt_config.py``) that is resolved per run:

    DEFAULT_PROFILE  ←  agent/yt_profile.json (optional, fleet-wide)  ←  payload "profile"

so you tune behaviour as data — hotter engagement, calmer tempo, a new popup to
dismiss, a new blocker to bail on, a different player geometry for a new device —
without editing this file. The resolved profile is carried on the ``Behavior``
object as ``bh.prof`` and read through the tiny ``bh.sel/geo/wp/wr`` accessors.

Two robustness layers make it survive the real world:
  * A per-run ``Behavior`` tempo (calm/normal/quick/random) scales AND jitters
    every delay, so no fixed cadence is ever repeated.
  * ``_ensure`` is a screen guard used at every phase: it transparently dismisses
    interstitials, relaunches if we fell out of the app, and — when it hits a
    screen it cannot get past (sign-in, captcha, update wall, crash, no network) —
    aborts with a clear "screen changed — manual fix required: <reason>".

Public flows: watch_link / search_watch / channel_watch / shorts.
"""
from __future__ import annotations

import asyncio
import random
import re
import sys
import time
from urllib.parse import quote, quote_plus

import adb
import detect
import droidrun
import humanize
import recipeui
import vision
import yt_config


def _log(serial: str, msg: str) -> None:
    """Operational phase log — lets an operator see where a flow is / why it stopped."""
    print(f"[yt {serial[:10]}] {msg}", file=sys.stderr, flush=True)


# ── Checkpoints ──────────────────────────────────────────────────────────────
# The flows already guard themselves: _ensure waits for expected markers and
# gives up with "unexpected screen". What that could never say is HOW FAR the
# flow got. A run log reading "screen changed — manual fix required: unexpected
# screen" is the same text whether YouTube never opened, the channel page never
# loaded, or playback failed after everything else worked — three different
# problems with three different fixes, reported identically.
#
# So each flow now marks named stages as it passes them, and every failure
# carries the trail. "opened → channel loaded" followed by a failure says the
# video never started; no marks at all says YouTube never came up.
#
# Deliberately just strings appended to a per-serial list: this runs on the Pi,
# where the compute-split rule keeps everything cheap. No model, no screenshot,
# no extra ADB round trip — a checkpoint costs one list append.
_checkpoints: dict[str, list[str]] = {}

# Serials whose trail belongs to a SESSION rather than to a single flow.
#
# `session` runs many videos by calling the single-video flows, and each of
# those opens with its own _cp_reset — so every video wiped the trail of the one
# before it. A six-video session that failed on the last one reported the same
# trail as one that failed instantly, losing exactly the fact that mattered:
# five videos had already played. Most recipes are `session`, so this was the
# common case, not an edge one.
_cp_owned: set[str] = set()


def _cp_reset(serial: str, *, own: bool = False) -> None:
    """Start a fresh trail. `own=True` claims it for a session, after which the
    sub-flows' own resets are ignored until _cp_release."""
    if own:
        _cp_owned.add(serial)
    elif serial in _cp_owned:
        return
    _checkpoints[serial] = []


def _cp_release(serial: str) -> None:
    _cp_owned.discard(serial)


def _cp(serial: str, name: str) -> None:
    """Mark a stage as reached."""
    _checkpoints.setdefault(serial, []).append(name)
    _log(serial, f"✓ {name}")


def _cp_trail(serial: str) -> list[str]:
    return list(_checkpoints.get(serial, []))


PKG = "com.google.android.youtube"
_GOOGLE_PKGS = {"com.google.android.gms", "com.google.android.gsf",
                "com.android.vending", "com.google.android.googlequicksearchbox"}

# per-device current search query, so _tap_first_suggestion knows what to match
_CUR_QUERY: dict[str, str] = {}
_NODE_BOUNDS = re.compile(r'text="([^"]*)"[^>]*?bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"')

# Text of the cookie-consent gate. The gate is a webview with an EMPTY a11y tree,
# so it is only visible to OCR — never to uiautomator/_ui. "a google company" is the
# subtitle under the YouTube logo at the very top of the gate: unique to this
# interstitial and visible in every scroll position / orientation while it's up, so
# it's the most reliable single marker (the heading/body only show near the top).
_CONSENT_MARKERS = ("a google company", "before you continue", "we use cookies",
                    "deliver and maintain google")


def _now() -> float:
    return time.monotonic()


def _rng(pair, default=(0.0, 0.0)) -> float:
    """uniform() over a [a,b] profile range, tolerant of a missing/short value."""
    try:
        a, b = pair
    except (TypeError, ValueError):
        a, b = default
    return random.uniform(a, b)


# --------------------------------------------------------------- behavior ----
class Behavior:
    """Per-run tempo + budget + resolved profile. Scales and jitters every delay so
    the cadence is never mechanical; 'random' re-rolls the multiplier on every pause
    so there is no repeating rhythm across actions, sessions, or devices."""

    def __init__(self, style: str = "random", max_run_s: float | None = None,
                 orientation: str = "portrait", engage: set | None = None,
                 llm_fallback: bool = False, detect_blocks: bool = False,
                 profile: dict | None = None) -> None:
        self.prof = profile or yt_config.load_profile()
        self._tempo = self.prof["tempo"]
        self.style = (style or "random").lower()
        self._base = self._tempo["styles"].get(self.style, 1.0)
        self.start = _now()
        self.deadline = (self.start + max_run_s) if max_run_s else None
        self.orientation = (orientation or "portrait").lower()
        # which engagement actions are allowed (like/subscribe/comment/seek); None = all
        self.engage = engage
        self.llm_fallback = bool(llm_fallback)
        self.detect_blocks = bool(detect_blocks)

    # -- profile accessors (keep the flow code terse and typo-safe) --
    def sel(self, name: str) -> list:
        return self.prof["selectors"].get(name, [])

    def geo(self, name: str, default=0.0):
        return self.prof["geometry"].get(name, default)

    def wp(self, name: str) -> float:
        return self.prof["watch"]["p"].get(name, 0.0)

    def wr(self, name: str):
        return self.prof["watch"].get(name)

    def sp(self, name: str) -> float:
        return self.prof["shorts"]["p"].get(name, 0.0)

    # -- tempo --
    def _mult(self) -> float:
        if self.style == "random":
            return random.choice(self._tempo["random_buckets"]) * _rng(self._tempo["random_jitter"], (0.8, 1.2))
        return self._base * _rng(self._tempo["style_jitter"], (0.82, 1.2))

    async def pause(self, a: float, b: float) -> None:
        await asyncio.sleep(max(0.05, random.uniform(a, b) * self._mult()))

    async def pause_r(self, name: str, section: str = "watch") -> None:
        """Pause using a named range from the profile (e.g. ('watch','pause_look_s'))."""
        rng = self.prof.get(section, {}).get(name, [0.4, 1.0])
        await self.pause(rng[0], rng[1])

    def roll(self, p: float) -> bool:
        return random.random() < p

    def expired(self) -> bool:
        return self.deadline is not None and _now() >= self.deadline

    def remaining(self) -> float | None:
        return None if self.deadline is None else max(0.0, self.deadline - _now())

    def think(self, content: int = 0) -> float:
        j = self._tempo.get("think_random_jitter", [0.7, 1.8])
        return humanize.think_time(content) * (random.uniform(j[0], j[1]) if self.style == "random" else self._base)


async def _set_orientation(serial: str, mode: str = "portrait") -> None:
    """Apply + hold the desired display orientation. YouTube requests sensor
    orientation and tilted bench phones flip to landscape, overriding the system
    lock — re-asserting while YouTube is foreground forces it back and holds."""
    if mode == "auto":
        await adb.set_orientation(serial, "auto")
        return
    await adb.set_orientation(serial, mode)
    await asyncio.sleep(random.uniform(0.6, 1.1))


async def _current_rotation(serial: str) -> int:
    """0=portrait, 1=90°, 2=180°, 3=270°. Reads the live window rotation."""
    r = await adb.shell(serial, "dumpsys window | grep -Eo 'mCurRotation=ROTATION_[0-9]+|mRotation=[0-9]+' | head -1")
    s = r.get("stdout", "")
    m = re.search(r"ROTATION_(\d+)|=(\d+)", s)
    if not m:
        return 0
    v = int(m.group(1) or m.group(2))
    return {90: 1, 180: 2, 270: 3}.get(v, v if v < 4 else 0)


async def _force_portrait(serial: str, bh: "Behavior") -> None:
    """Set portrait AND verify the frame actually rotated back (YouTube's shorts/
    home request sensor orientation on this tilted phone and flip to landscape,
    which throws off blind positional taps). Re-asserts until rotation reads 0."""
    if bh.orientation == "auto":
        return
    for _ in range(4):
        await adb.set_orientation(serial, bh.orientation)
        await asyncio.sleep(0.7)
        rot = await _current_rotation(serial)
        want = 0 if bh.orientation == "portrait" else 1
        if rot == want:
            return


# ---------------------------------------------------------------- helpers ----
async def _ui(serial: str, *queries: str) -> dict:
    st = await recipeui.ui_state(serial, list(queries))
    return st.get("matches", {})


async def _click(serial: str, x: float, y: float) -> None:
    """Tap via adb `input tap` (uses CURRENT display-orientation coordinates, exactly
    what uiautomator reports — so it lands even when YouTube forces landscape). A few
    px of scatter keeps it human."""
    await adb.shell(serial, f"input tap {int(x) + random.randint(-4, 4)} {int(y) + random.randint(-4, 4)}")
    await asyncio.sleep(random.uniform(0.04, 0.13))


async def _tap(serial: str, ctrl, m: dict | None) -> bool:
    if m and m.get("present") and m.get("x") is not None:
        await _click(serial, m["x"], m["y"])
        return True
    return False


async def _find_and_tap(serial: str, ctrl, *queries: str, ocr: bool = False) -> bool:
    matches = await _ui(serial, *queries)
    for q in queries:
        if await _tap(serial, ctrl, matches.get(q)):
            return True
    if ocr:  # no-LLM fallback: read it off the pixels
        for q in queries:
            if q.startswith("id/"):
                continue
            hit = await vision.ocr_find(serial, q)
            if hit:
                _log(serial, f"OCR fallback found '{q}' @ ({hit['x']},{hit['y']})")
                await _click(serial, hit["x"], hit["y"])
                return True
    return False


async def _foreground_pkg(serial: str) -> str:
    r = await adb.shell(serial, "dumpsys window | grep -E 'mCurrentFocus|mFocusedApp'")
    m = re.search(r"([a-zA-Z][a-zA-Z0-9_.]+)/[a-zA-Z0-9_.$]+", r.get("stdout", ""))
    return m.group(1) if m else ""


async def _skip_ad(serial: str, ctrl, bh: "Behavior") -> bool:
    """Tap the ad-skip button the moment it's tappable — via the a11y tree, then OCR
    (the skip button is often a graphic the tree doesn't expose)."""
    skip = bh.sel("skip")
    matches = await _ui(serial, *skip)
    for q in skip:
        m = matches.get(q)
        if m and m.get("present") and m.get("x") is not None:
            await _click(serial, m["x"], m["y"])
            await asyncio.sleep(random.uniform(0.3, 0.8))
            return True
    reg = bh.geo("skip_region", [0.5, 0.45, 1.0, 0.95])
    hit = await vision.ocr_find(serial, "Skip", region=vision.region(*reg))
    if hit:
        await _click(serial, hit["x"], hit["y"])
        await asyncio.sleep(random.uniform(0.3, 0.8))
        return True
    return False


# ------------------------------------------------ player controls (rich) -----
_DEFAULT_ENGAGE = {"like", "comment", "seek", "replay"}  # subscribe is opt-in


def _allow(bh: "Behavior", action: str) -> bool:
    eng = bh.engage if bh.engage is not None else _DEFAULT_ENGAGE
    return action in eng


async def _double_tap(serial: str, x, y) -> None:
    await _click(serial, x, y)
    await asyncio.sleep(random.uniform(0.06, 0.15))
    await _click(serial, x, y)


async def _seek_double(serial: str, ctrl, bh: "Behavior", direction: str = "fwd") -> None:
    """Double-tap the fwd/back zone of the player to jump ±10s (YouTube's gesture)."""
    x = ctrl.width * (bh.geo("seek_fwd_x", 0.85) if direction == "fwd" else bh.geo("seek_back_x", 0.15))
    await _double_tap(serial, x, ctrl.height * bh.geo("player_cy", 0.16))


async def _seek_slider(serial: str, ctrl, to_frac: float, bh: "Behavior") -> None:
    """Scrub by DRAGGING the progress bar to a position (like grabbing the slider)."""
    await _click(serial, ctrl.width * bh.geo("player_cx", 0.5), ctrl.height * bh.geo("player_cy", 0.16))
    await bh.pause(0.3, 0.7)
    y = int(ctrl.height * bh.geo("seekbar_y", 0.255))
    x1 = int(ctrl.width * random.uniform(0.2, 0.5))
    x2 = int(ctrl.width * min(0.97, max(0.03, to_frac)))
    await adb.swipe(serial, x1, y, x2, y, random.randint(300, 700))
    await bh.pause(0.3, 0.8)


async def _do_like(serial: str, ctrl, bh: "Behavior") -> bool:
    if await _find_and_tap(serial, ctrl, *bh.sel("like")):
        return True
    # Fallback is just a double-tap on the player center, which only toggles the
    # controls overlay — it is NOT a like action, so report it as a miss rather
    # than a false success.
    await _double_tap(serial, ctrl.width * bh.geo("player_cx", 0.5), ctrl.height * bh.geo("player_cy", 0.16))
    return False


async def _do_subscribe(serial: str, ctrl, bh: "Behavior") -> bool:
    return await _find_and_tap(serial, ctrl, *bh.sel("subscribe"), ocr=True)


async def _open_comments(serial: str, ctrl, bh: "Behavior") -> bool:
    """Open the comments panel, read a few, then close it."""
    if not await _find_and_tap(serial, ctrl, *bh.sel("comments_open"), ocr=True):
        return False
    await bh.pause(0.8, 1.8)
    fy, ty = bh.geo("comment_swipe_from_y", 0.75), bh.geo("comment_swipe_to_y", 0.42)
    for _ in range(random.randint(2, 4)):
        await humanize.human_swipe(ctrl, ctrl.width * 0.5, ctrl.height * fy, ctrl.width * 0.5, ctrl.height * ty)
        await bh.pause(0.8, 2.2)
    await adb.keyevent(serial, "KEYCODE_BACK")
    await bh.pause(0.5, 1.2)
    return True


async def _peek_description(serial: str, ctrl, bh: "Behavior") -> bool:
    """Expand the title/description card, read a beat, then collapse — a very human
    'what is this' glance. Config-gated by watch.p.peek_description."""
    await _click(serial, ctrl.width * bh.geo("peek_x", 0.5), ctrl.height * bh.geo("peek_y", 0.30))
    await bh.pause_r("peek_read_s", "watch")
    await adb.keyevent(serial, "KEYCODE_BACK")
    await bh.pause(0.3, 0.9)
    return True


async def _watch_related(serial: str, ctrl, bh: "Behavior") -> bool:
    """Scroll to the up-next / related list under the player and open one — an
    autoplay-style chain into the next video. Confirms the new watch page."""
    w, h = ctrl.width, ctrl.height
    await humanize.human_scroll(ctrl, w, h, "up", random.uniform(0.6, 1.05))
    await bh.pause(0.8, 1.9)
    await _click(serial, w * random.uniform(0.3, 0.62), h * random.uniform(0.34, 0.6))
    for _ in range(6):
        await bh.pause(0.6, 1.1)
        if await _on_watch_page(serial, bh):
            return True
    await adb.keyevent(serial, "KEYCODE_BACK")
    await bh.pause(0.6, 1.2)
    return False


async def _maybe_chain(serial: str, ctrl, bh: "Behavior") -> int:
    """After a watch, sometimes ride the recommended chain for 1-2 more videos
    (bounded by the run's remaining time). Returns how many were chained."""
    chained = 0
    while (bh.wp("watch_related") and bh.roll(bh.wp("watch_related")) and not bh.expired()
           and chained < 2 and (bh.remaining() is None or bh.remaining() > 25)):
        if not await _watch_related(serial, ctrl, bh):
            break
        rem = bh.remaining()
        seg = min(random.uniform(25, 70), (rem - 6) if rem is not None else 70)
        _log(serial, f"chaining related video ({chained + 1})")
        await _watch_video(serial, ctrl, bh, seg, allow_boredom=True)
        chained += 1
    return chained


async def _replay(serial: str, ctrl, bh: "Behavior") -> bool:
    """When a video ends a big replay control appears — hit it to replay."""
    rsel = bh.sel("replay")
    m = await _ui(serial, *rsel)
    c = next((m[q] for q in rsel if m.get(q)), None)
    if c and c.get("present") and c.get("x") is not None:
        await _click(serial, c["x"], c["y"])
        return True
    # Fallback is a single tap on the player center, which only toggles the
    # controls overlay — nothing actually replayed, so report a miss.
    await _click(serial, ctrl.width * bh.geo("player_cx", 0.5), ctrl.height * bh.geo("player_cy", 0.16))
    return False


# --------------------------------------------------------------- the guard ---
async def _ensure(serial: str, ctrl, bh: Behavior, *expect: str, timeout: float = 20.0) -> dict:
    """Wait until one of `expect` markers is on screen, transparently handling
    whatever else shows up. Returns {'ok': True} or a terminal
    {'ok': False, 'status': 'blocked'|'left_app'|'unknown', 'reason': ...}."""
    blockers = [tuple(b) for b in bh.sel("blockers")]
    block_q = [q for q, _ in blockers]
    dismiss = bh.sel("dismiss")
    deadline = _now() + timeout
    net_retries = 0
    while _now() < deadline:
        m = await _ui(serial, *expect, *block_q, *dismiss)

        if any(m.get(q, {}).get("present") for q in expect):        # reached target
            return {"ok": True}

        hit = next(((q, r) for q, r in blockers if m.get(q, {}).get("present")), None)
        if hit:
            _, reason = hit
            if reason == "no network" and net_retries < 3:          # often transient
                net_retries += 1
                await _find_and_tap(serial, ctrl, *bh.sel("retry"))
                await bh.pause(2.5, 4.5)
                continue
            return {"ok": False, "status": "blocked", "reason": reason}

        dq = next((q for q in dismiss if m.get(q, {}).get("present")), None)
        if dq:
            await _tap(serial, ctrl, m[dq])
            await bh.pause(0.4, 1.0)
            continue

        fg = await _foreground_pkg(serial)                          # fell out of YT?
        if fg and fg != PKG:
            if fg in _GOOGLE_PKGS:
                return {"ok": False, "status": "blocked", "reason": "account / consent screen"}
            await adb.launch_package(serial, PKG)
            await bh.pause(1.0, 2.0)
            continue

        await bh.pause(0.6, 1.2)

    seen = await _visible_texts(serial)
    _log(serial, f"guard timeout waiting for {list(expect)} — visible: {seen}")
    # Carry BOTH what we wanted and what was actually there. This used to go only
    # to the Pi's stderr, which an operator using the web UI cannot read — so the
    # single most useful fact about the failure ("it was sitting on a sign-in
    # wall", "the feed was empty") was computed, logged, and thrown away, leaving
    # a bare "unexpected screen" that is the same text for every cause.
    return {"ok": False, "status": "unknown", "reason": "unexpected screen",
            "expected": list(expect), "seen": seen}


async def _visible_texts(serial: str) -> list[str]:
    st = await recipeui.ui_state(serial, [])
    return st.get("texts", [])[:12]


def _abort(flow: str, st: dict, serial: str | None = None) -> dict:
    reason = st.get("reason") or st.get("status") or "unexpected screen"
    blocked = st.get("status") == "blocked" or st.get("quarantine")
    trail = _cp_trail(serial) if serial else []
    # What the guard was waiting for, and what was actually on screen. Without
    # these "unexpected screen" is identical for a sign-in wall, an empty feed
    # and a crashed app — three different problems, one message.
    # strip() before the emptiness test: a whitespace-only node is truthy and
    # would render as a blank entry between two commas.
    seen = [str(s).strip() for s in (st.get("seen") or []) if str(s).strip()][:6]
    want = [str(w).strip() for w in (st.get("expected") or []) if str(w).strip()][:4]
    if want:
        reason = f"{reason} (waiting for: {', '.join(want)})"
    if seen:
        reason = f"{reason} — on screen: {', '.join(seen)}"
    # Put the trail IN the reason, not just alongside it: the backend surfaces
    # `reason` in the run log, and a field nobody reads helps nobody.
    if trail:
        reason = f"{reason} (reached: {' → '.join(trail)})"
    elif serial:
        reason = f"{reason} (no stage reached — the app never came up)"
    # Name the flow. A `session` runs many videos through the single-video
    # flows, so "which flow failed" is not answerable from the recipe alone —
    # and the answer was already in this dict, just never in the text an
    # operator reads.
    return {
        "ok": False, "flow": flow, "status": st.get("status", "unknown"),
        "manual": True, "reason": reason, "checkpoints": trail,
        "quarantine": bool(blocked), "seen": seen,
        "state": st.get("state") or ("blocked" if blocked else None),
        "detail": (f"account blocked — quarantined: [{flow}] {reason}" if blocked
                   else f"screen changed — manual fix required: [{flow}] {reason}"),
    }


async def _recover(serial: str, ctrl, bh: Behavior, st: dict, *expect: str, goal: str = "") -> dict:
    """Given a failed _ensure, decide what to do: hard block → quarantine; unknown
    screen → (LLM tier) hand `goal` to the DroidRun agent and/or run the vision
    classifier. Returns {ok:True} if recovered, else a terminal dict."""
    status = st.get("status")
    # Evidence from the failed _ensure — what it waited for and what was on
    # screen. Every return below rebuilt this dict from scratch and dropped
    # both, so the on-screen text made it as far as _recover and no further:
    # _reach -> _recover is the ordinary failure path, which made the whole
    # thing inert exactly where it was needed.
    ev = {"seen": st.get("seen"), "expected": st.get("expected")}
    if status == "blocked":
        return {"ok": False, "status": "blocked", "quarantine": True,
                "state": "blocked", "reason": st.get("reason"), **ev}

    if bh.detect_blocks and status in ("unknown", "left_app"):
        try:
            cls = await detect.classify_screen(serial)
            if cls.get("ok") and cls.get("blocked"):
                _log(serial, f"vision classifier: {cls.get('state')} ({cls.get('reason')})")
                return {"ok": False, "status": "blocked", "quarantine": True,
                        "state": cls.get("state"),
                        "reason": cls.get("reason") or cls.get("state"), **ev}
        except Exception as e:  # noqa: BLE001
            _log(serial, f"detect error: {e}")

    if bh.llm_fallback and status in ("unknown", "left_app"):
        _log(serial, f"escalating to LLM agent (goal: {goal[:60]})")
        try:
            await droidrun.run_task(
                serial, goal or "You are in the YouTube app. Dismiss any dialog and return to the main content.",
                vision=True, steps=8, timeout=180)
        except Exception as e:  # noqa: BLE001
            _log(serial, f"LLM escalation error: {e}")
        st2 = await _ensure(serial, ctrl, bh, *expect, timeout=12)
        if st2.get("ok"):
            _log(serial, "recovered via LLM agent")
            return {"ok": True, "recovered": "llm"}

    return {"ok": False, "status": status, "quarantine": False,
            "reason": st.get("reason"), **ev}


async def _reach(serial: str, ctrl, bh: Behavior, *expect: str, goal: str = "", timeout: float = 18.0) -> dict:
    st = await _ensure(serial, ctrl, bh, *expect, timeout=timeout)
    if st.get("ok"):
        return st
    return await _recover(serial, ctrl, bh, st, *expect, goal=goal)


async def _await_content(serial: str, bh: Behavior, probes: list[str], timeout: float = 20.0) -> bool:
    """Wait for a list to actually contain rows, rather than sleeping and hoping.

    Screen chrome — a nav bar, a search box, a channel header — renders the
    moment an activity is up, before a single row of content has arrived. On a
    warm app that gap is imperceptible, which is why a fixed `pause(1.5, 3.0)`
    looked sufficient. On a cold start behind a proxy it is several seconds,
    and a flow that starts tapping inside that window finds nothing to tap,
    wanders, and reports "unexpected screen" from wherever it ended up. That is
    what made channel_watch fail 3/4 right after a restore_defaults (which
    force-stops YouTube) while warm devices passed 3/4 on the same recipe.

    `probes` should be the markers the NEXT step actually needs — cell_probes
    for a video list, channel_open for channel rows — so "content is here"
    means the thing the flow is about to use, not merely that pixels changed.

    Returns whether content arrived. Callers proceed either way: an empty list
    is the flow's own recovery problem, and refusing to continue would turn a
    slow network into a hard failure.
    """
    deadline = _now() + timeout
    while _now() < deadline:
        m = await _ui(serial, *probes)
        if any(m.get(q, {}).get("present") for q in probes):
            return True
        await bh.pause(0.5, 1.0)
    return False


# ------------------------------------------------------ human app open/close --
async def _open_app(serial: str, ctrl, bh: Behavior) -> dict:
    """Open YouTube to a clean Home feed (force-restart so we don't resume a prior
    watch/search page; a force-stop is invisible to the app)."""
    o = bh.prof["open"]
    await _set_orientation(serial, bh.orientation)
    await adb.force_stop(serial, PKG)
    await bh.pause(*o.get("forcestop_pause_s", [0.8, 1.7]))
    await adb.launch_package(serial, PKG)
    st = await _ensure(serial, ctrl, bh, *bh.sel("home_markers"), timeout=25)
    if not st["ok"]:
        return st
    # Chrome is up; the feed may not be. Costs a warm device one UI read.
    if not await _await_content(serial, bh, bh.sel("cell_probes"), timeout=o.get("feed_timeout_s", 20.0)):
        _log(serial, "home feed still empty after wait — continuing, expect a rough pick")
    await _set_orientation(serial, bh.orientation)
    await asyncio.sleep(bh.think(300))
    if bh.roll(o.get("glance_prob", 0.35)):        # glance at the feed like a person
        gf = o.get("glance_frac", [0.3, 0.7])
        await humanize.human_scroll(ctrl, ctrl.width, ctrl.height, "up", random.uniform(gf[0], gf[1]))
        await bh.pause(0.8, 2.4)
        await humanize.human_scroll(ctrl, ctrl.width, ctrl.height, "down", random.uniform(gf[0], gf[1]))
        await bh.pause(0.4, 1.2)
    return {"ok": True}


async def _close_app(serial: str, ctrl, bh: Behavior) -> None:
    """Leave the app like a person — usually Home, sometimes swipe out of recents,
    occasionally leave it open. Never force-stop (bots force-stop)."""
    c = bh.prof["close"]
    home_p = c.get("home_prob", 0.6)
    recents_p = c.get("recents_prob", 0.28)
    r = random.random()
    if r < home_p:
        await adb.keyevent(serial, "KEYCODE_HOME")
    elif r < home_p + recents_p:
        await adb.keyevent(serial, "KEYCODE_APP_SWITCH")
        await bh.pause(0.6, 1.3)
        await humanize.human_swipe(ctrl, ctrl.width * 0.5, ctrl.height * 0.55, ctrl.width * 0.5, ctrl.height * 0.08)
        await bh.pause(0.3, 0.8)
        await adb.keyevent(serial, "KEYCODE_HOME")
    # else: just leave it — like locking the phone mid-app


# --------------------------------------------------- URL-intent navigation ---
# This YouTube build barely exposes the Home/search UI to uiautomator (no search
# icon, no result-card text). So we NAVIGATE with VIEW intents — the reliable path
# — and only interact on the watch page, which IS well-exposed.
def _yt_search_url(query: str, sp: str = "") -> str:
    u = f"https://www.youtube.com/results?search_query={quote_plus(query)}"
    return u + (f"&sp={sp}" if sp else "")


def _channel_url(channel: str) -> str:
    # channel/short_id values come straight from a workflow node's payload
    # (attacker/user-controlled) — percent-encode every user-supplied segment
    # before it becomes part of a URL that reaches _open_url. A raw "http..."
    # value is a caller-supplied literal URL, not a bare segment, so it's left
    # alone here; _open_url's own quoting is what keeps that branch safe.
    c = channel.strip()
    if c.startswith("http"):
        return c
    if c.startswith("@"):
        return f"https://www.youtube.com/{quote(c, safe='@')}"
    if c.startswith("UC") and len(c) > 20:
        return f"https://www.youtube.com/channel/{quote(c, safe='')}"
    return f"https://www.youtube.com/@{quote(c.lstrip('@'), safe='')}"


async def _open_url(serial: str, url: str) -> None:
    """Deep-link into the YouTube app via a VIEW intent (bypasses the flaky UI).
    Wraps the URL in SINGLE quotes and strips any embedded single quote —
    the same safe pattern watch_link uses — rather than double quotes, because
    POSIX shells still perform $(...)/backtick command substitution INSIDE
    double quotes (the remote `adb shell` runs this through /system/bin/sh -c).
    Single-quoting makes everything inside literal, so even an un-encoded or
    unexpected value can't break out and run a shell command on the device."""
    q = url.replace("'", "")
    r = await adb.shell(serial, f"am start -a android.intent.action.VIEW -d '{q}' "
                                f"-n {PKG}/com.google.android.youtube.UrlActivity")
    if "Error" in (r.get("stdout", "") + r.get("stderr", "")):
        await adb.shell(serial, f"am start -a android.intent.action.VIEW -d '{q}' {PKG}")


async def _consent_up(serial: str, jpeg: bytes | None = None) -> bool:
    """OCR-detect the cookie-consent gate. It is a webview with an EMPTY a11y tree,
    so pixels are the ONLY reliable signal — its heading/body text, or the two
    standalone consent buttons at the bottom of the page."""
    texts = await vision.ocr_read(serial, jpeg=jpeg)
    joined = " ".join(t["text"].lower() for t in texts)
    if any(mk in joined for mk in _CONSENT_MARKERS):
        return True
    return any(t["text"].strip().lower().rstrip(".") in ("reject all", "accept all")
               for t in texts)


def _consent_button(texts: list[dict], label: str) -> dict | None:
    """The real consent button is a SHORT standalone OCR token ('Reject all'),
    NOT the body sentences that quote "Accept all"/"Reject all" — so match the
    whole token exactly rather than as a substring."""
    for t in texts:
        if t["text"].strip().lower().rstrip(".") == label:
            return t
    return None


async def _dismiss_consent(serial: str, ctrl, bh: Behavior) -> bool:
    """Dismiss Google/YouTube's cookie-consent gate ("Before you continue to
    YouTube …") when it's up — it blocks every flow until handled. The gate is a
    webview whose buttons are NOT in the a11y tree and sit at the bottom of a long
    page, so DETECT it by OCR and TAP 'Reject all' (privacy-preserving; 'Accept
    all' only as a fallback) off the pixels. One-time per device — the app
    remembers the choice, so it never shows again."""
    if not await _consent_up(serial):
        return False
    _log(serial, "consent gate up — scrolling to the buttons to tap 'Reject all'")
    W, H = ctrl.width, ctrl.height
    prev = None
    for _ in range(8):                       # button block is at the bottom of a long page
        texts = await vision.ocr_read(serial)
        rej = _consent_button(texts, "reject all")
        acc = _consent_button(texts, "accept all")
        # Tap only once we're at the button block — both buttons visible, or 'Reject
        # all' alongside the 'More options' anchor that sits just below it. That way a
        # mid-page paragraph quoting a label can never be mistaken for the button.
        at_block = rej is not None and (acc is not None or _consent_button(texts, "more options") is not None)
        if at_block:
            target, label = (rej, "Reject all") if rej else (acc, "Accept all")
            await _click(serial, target["x"], target["y"])
            await bh.pause(1.8, 3.2)
            cleared = not await _consent_up(serial)
            _log(serial, f"tapped '{label}' — consent {'cleared' if cleared else 'still up, retrying'}")
            if cleared:
                return True
        sig = tuple(t["text"] for t in texts)
        if sig == prev:                      # reached the bottom, nothing left to scroll
            break
        prev = sig
        await humanize.human_scroll(ctrl, W, H, "up", 0.8)
        await bh.pause(0.5, 1.1)
    return not await _consent_up(serial)


async def _await_online(serial: str, ctrl, bh: Behavior, timeout: float = 22.0) -> dict:
    """After navigation, make sure YouTube is actually online. A dead device proxy
    shows the 'You're offline' wall while the OS still pings — RETRY a few times,
    then bail with a clear reason instead of pretending to watch a blank screen."""
    offline_q = ["You're offline", "No internet", "check your network connection", "No connection"]
    deadline = _now() + timeout
    retried = 0
    while _now() < deadline:
        m = await _ui(serial, *offline_q, "RETRY", "Retry")
        if any(m.get(q, {}).get("present") for q in offline_q):
            if retried < 3:
                retried += 1
                _log(serial, f"offline wall — retry {retried}")
                await _find_and_tap(serial, ctrl, "RETRY", "Retry", "Try again", "TRY AGAIN")
                await bh.pause(2.5, 4.5)
                continue
            return {"ok": False, "status": "blocked",
                    "reason": "device offline — app can't reach the internet (dead proxy? check tunnel/network)"}
        await _dismiss_consent(serial, ctrl, bh)   # clear the cookie-consent gate if up
        return {"ok": True}
    await _dismiss_consent(serial, ctrl, bh)
    return {"ok": True}


# ------------------------------------------------------------ watch loop ----
async def _watch_video(serial: str, ctrl, bh: Behavior, watch_s: float, *, allow_boredom: bool = True) -> dict:
    """Watch the currently-playing video like a human for ~watch_s seconds.
    Every probability + timing below is read from the profile (bh.wp / bh.wr)."""
    await _set_orientation(serial, bh.orientation)
    w, h = ctrl.width, ctrl.height
    target = max(5.0, watch_s * _rng(bh.wr("watch_variance"), (0.85, 1.12)))
    if allow_boredom and bh.roll(bh.wp("boredom")):
        bf = bh.wr("boredom_frac") or [0.0, 0.45]
        target = random.uniform(5.0, max(7.0, watch_s * bf[1]))
    if bh.remaining() is not None:
        target = min(target, max(3.0, bh.remaining() - 3))

    # cumulative decision thresholds, derived from the individual profile probs so
    # each is independently tunable while preserving the original ordering.
    t_pause = bh.wp("pause_resume")
    t_seek = t_pause + bh.wp("seek")
    t_seekb = t_seek + bh.wp("seek_back")
    t_comment = t_seekb + bh.wp("comment")
    t_like = t_comment + bh.wp("like")
    t_sub = t_like + bh.wp("subscribe_slot")
    t_peek = t_sub + bh.wp("peek_description")

    start = _now()
    did = {"like": False, "subscribe": False, "comments": False, "peek": False}
    actions: list[str] = []
    last_orient = _now()

    await bh.pause(*(bh.wr("initial_settle_s") or [1.5, 4.0]))
    if await _skip_ad(serial, ctrl, bh):
        actions.append("skip_ad")

    seg = bh.wr("segment_s") or [3.5, 11.0]
    while _now() - start < target and not bh.expired():
        dt = min(random.uniform(seg[0], seg[1]) * bh._mult(), target - (_now() - start))
        if dt <= 0:
            break
        await asyncio.sleep(dt)

        if bh.orientation != "auto" and _now() - last_orient > (bh.wr("reorient_every_s") or 20):
            await adb.set_orientation(serial, bh.orientation)
            last_orient = _now()

        if bh.roll(bh.wp("skip_ad_recheck")) and await _skip_ad(serial, ctrl, bh):
            actions.append("skip_ad")
            continue

        roll = random.random()
        if roll < t_pause:                                       # pause, look away, resume
            await _double_tap(serial, w * bh.geo("player_cx", 0.5), h * bh.geo("player_cy", 0.16))
            await bh.pause(*(bh.wr("pause_look_s") or [1.2, 5.0]))
            await _double_tap(serial, w * bh.geo("player_cx", 0.5), h * bh.geo("player_cy", 0.16))
            actions.append("pause_resume")
        elif roll < t_seek and _allow(bh, "seek"):               # skip ahead
            if bh.roll(bh.wp("seek_slider_vs_double")):
                await _seek_slider(serial, ctrl, _rng(bh.wr("seek_slider_to"), (0.3, 0.85)), bh)
                actions.append("seek_slider")
            else:
                await _seek_double(serial, ctrl, bh, "fwd")
                actions.append("seek_double")
        elif roll < t_seekb and _allow(bh, "seek"):              # skip back a bit
            await _seek_double(serial, ctrl, bh, "back")
            actions.append("seek_back")
        elif roll < t_comment and not did["comments"] and _allow(bh, "comment"):
            if await _open_comments(serial, ctrl, bh):
                did["comments"] = True
                actions.append("comments")
        elif roll < t_like and not did["like"] and _allow(bh, "like"):
            if await _do_like(serial, ctrl, bh):
                did["like"] = True
                actions.append("like")
        elif roll < t_sub and not did["subscribe"] and _allow(bh, "subscribe") and bh.roll(bh.wp("subscribe_gate")):
            if await _do_subscribe(serial, ctrl, bh):
                did["subscribe"] = True
                actions.append("subscribe")
        elif roll < t_peek and not did["peek"]:                  # glance at the description
            if await _peek_description(serial, ctrl, bh):
                did["peek"] = True
                actions.append("peek")
        # else: just keep watching

    if _allow(bh, "replay") and bh.roll(bh.wp("replay")) and not bh.expired():
        if await _replay(serial, ctrl, bh):
            actions.append("replay")
            rem = bh.remaining()
            ex = bh.wr("replay_extra_s") or [5, 20]
            extra = min(random.uniform(ex[0], ex[1]), (rem - 2) if rem is not None else ex[1])
            if extra > 0:
                await asyncio.sleep(extra)

    return {"ok": True, "watched_s": round(_now() - start, 1),
            "liked": did["like"], "subscribed": did["subscribe"], "actions": actions}


# ----------------------------------------------------- search / pick ---------
async def _on_watch_page(serial: str, bh: "Behavior") -> bool:
    m = await _ui(serial, *bh.sel("watch_markers"))
    return any(v.get("present") for v in m.values())


async def _pick_result(serial: str, ctrl, bh: Behavior) -> bool:
    """Open a real video from a results / channel-videos list. Robust to a
    uiautomator-blind results page: scroll past the top Sponsored/app-install ad,
    then open a candidate via a11y cell (if exposed) → OCR of view/age metadata
    (tapping the thumbnail just above it) → positional rows. Confirms the watch
    page rendered (`_on_watch_page` works reliably) and backs out on a miss."""
    w, h = ctrl.width, ctrl.height
    await _force_portrait(serial, bh)      # positional taps assume portrait coords

    async def _try_open(x, y) -> bool:
        await _click(serial, x, y)
        for _ in range(7):
            await bh.pause(0.6, 1.1)
            if await _on_watch_page(serial, bh):
                return True
        await adb.keyevent(serial, "KEYCODE_BACK")   # not a video — back to results
        await bh.pause(0.8, 1.5)
        return False

    # the first slot is almost always a Sponsored app-install ad → scroll past it,
    # like a person skimming to the real results.
    await humanize.human_scroll(ctrl, w, h, "up", random.uniform(0.4, 0.7))
    await bh.pause(0.8, 1.7)
    if bh.roll(0.45):                                 # sometimes browse a little more
        await humanize.human_scroll(ctrl, w, h, "up", random.uniform(0.25, 0.5))
        await bh.pause(0.7, 1.6)

    # 1) a11y video cell (works on versions that expose it)
    for probe in bh.sel("cell_probes"):
        m = await _ui(serial, probe)
        c = m.get(probe)
        if c and c.get("present") and c.get("x") is not None:
            if await _try_open(c["x"], c["y"]):
                return True
    # 2) OCR the results band for a view/age token, then tap the THUMBNAIL above it
    reg = bh.geo("results_region", [0.0, 0.12, 1.0, 0.9])
    for token in bh.sel("cell_ocr_tokens"):
        hit = await vision.ocr_find(serial, token, region=vision.region(*reg))
        if hit and await _try_open(hit["x"], hit["y"] - int(h * 0.07)):
            return True
    # 3) positional: try a few thumbnail rows down the page, scrolling between tries
    for fy in (0.34, 0.5, 0.66):
        if await _try_open(w * random.uniform(0.3, 0.6), h * fy):
            return True
        await humanize.human_scroll(ctrl, w, h, "up", random.uniform(0.3, 0.5))
        await bh.pause(0.6, 1.3)
    return False


async def _typed_ok(serial: str, query: str) -> bool:
    needle = query.strip()[:6].lower()
    if not needle:
        return True
    st = await recipeui.ui_state(serial, [])
    return any(needle in (t or "").lower() for t in st.get("texts", []))


async def _results_present(serial: str, bh: "Behavior") -> bool:
    rm = bh.sel("results_markers")
    m = await _ui(serial, *rm)
    return any(m.get(q, {}).get("present") for q in rm)


async def _await_results(serial: str, bh: Behavior, timeout: float) -> bool:
    deadline = _now() + timeout
    while _now() < deadline:
        if await _results_present(serial, bh):
            return True
        await bh.pause(0.6, 1.1)
    return False


async def _do_search(serial: str, ctrl, bh: Behavior, query: str) -> bool:
    sc = bh.prof["search"]
    gs = await _ensure(serial, ctrl, bh, *bh.sel("search_hints"), timeout=12)
    if not gs["ok"]:
        return False
    opened = False
    for i in range(3):
        if await _find_and_tap(serial, ctrl, *bh.sel("search_hints")[:2], ocr=(i == 2)):
            opened = True
            break
        await bh.pause(0.6, 1.1)
    if not opened:
        _log(serial, "search bar tap failed")
        return False
    _log(serial, "search opened")
    await _ensure(serial, ctrl, bh, *bh.sel("search_edit"), timeout=6)
    await bh.pause(0.7, 1.4)
    for attempt in range(int(sc.get("type_attempts", 2))):
        if attempt > 0:
            # A prior attempt left partial/garbled text sitting in the field —
            # clear it before retyping, else the new query gets appended onto
            # the old one (e.g. "pythopython tutorial") instead of replacing it.
            await adb.keyevent(serial, "KEYCODE_MOVE_END")
            await adb.keyevent(serial, " ".join(["KEYCODE_DEL"] * 40))
            await bh.pause(0.2, 0.5)
        await humanize.human_type(serial, query, typo_rate=float(sc.get("typo_rate", 0.05)))
        await bh.pause(0.5, 1.2)
        if await _typed_ok(serial, query):
            break
        await _find_and_tap(serial, ctrl, *bh.sel("search_edit"))
        await bh.pause(0.6, 1.1)
    _CUR_QUERY[serial] = query
    # Submit by TAPPING THE FIRST SUGGESTION ROW (the Samsung IME swallows ENTER
    # inconsistently, but tapping a suggestion always runs the search).
    for attempt in range(int(sc.get("submit_attempts", 3))):
        tapped = await _tap_first_suggestion(serial, ctrl, bh)
        if tapped and await _await_results(serial, bh, 8):
            _log(serial, f"submitted via suggestion tap (attempt {attempt + 1})")
            return True
        await adb.keyevent(serial, "KEYCODE_ENTER")
        if await _await_results(serial, bh, 4):
            _log(serial, "submitted via ENTER")
            return True
        await _find_and_tap(serial, ctrl, *bh.sel("search_edit")[:2])
        await bh.pause(0.6, 1.1)
    _log(serial, "submit failed")
    return False


async def _first_suggestion_center(serial: str, bh: "Behavior", query: str) -> tuple[int, int] | None:
    """Locate the topmost search-suggestion row (a text node containing the query,
    below the search box) and return its tappable center."""
    await adb.shell(serial, "uiautomator dump /sdcard/mf_sg.xml")
    xml = (await adb.shell(serial, "cat /sdcard/mf_sg.xml")).get("stdout", "")
    ql = query.strip().lower()
    y_min = bh.prof["search"].get("suggestion_y_min", 230)
    y_max = bh.prof["search"].get("suggestion_y_max", 900)
    best = None
    for m in _NODE_BOUNDS.finditer(xml):
        t = m.group(1)
        y1, y2 = int(m.group(3)), int(m.group(5))
        cy = (y1 + y2) // 2
        if ql in t.lower() and y_min < cy < y_max:
            cx = (int(m.group(2)) + int(m.group(4))) // 2
            if best is None or cy < best[1]:
                best = (cx, cy)
    if best is None:
        reg = bh.geo("suggestion_region", [0.0, 0.1, 1.0, 0.45])
        hit = await vision.ocr_find(serial, query, region=vision.region(*reg))
        if hit:
            best = (hit["x"], hit["y"])
    return best


async def _tap_first_suggestion(serial: str, ctrl, bh: Behavior) -> bool:
    c = await _first_suggestion_center(serial, bh, _CUR_QUERY.get(serial, ""))
    if c:
        await _click(serial, c[0], c[1])
        await bh.pause(0.3, 0.7)
        return True
    return False


# --------------------------------------------------------------- flows -------
async def watch_link(serial: str, ctrl, p: dict, bh: Behavior) -> dict:
    _cp_reset(serial)
    url = str(p["url"]).strip()
    watch_s = _watch_s_pick(p.get("watch_s", 90))   # accepts a fixed number OR a [min,max] range
    await _set_orientation(serial, bh.orientation)
    q = url.replace("'", "")
    r = await adb.shell(serial,
                        f"am start -a android.intent.action.VIEW -d '{q}' "
                        f"-n {PKG}/com.google.android.youtube.UrlActivity")
    if "Error" in (r.get("stdout", "") + r.get("stderr", "")):
        await adb.shell(serial, f"am start -a android.intent.action.VIEW -d '{q}'")
        await _wait_yt_chooser(serial, ctrl, bh)
    st = await _reach(serial, ctrl, bh, "id/watch_player", "Share", "Subscribe", "id/player",
                      goal="You are in the YouTube app. Make sure the video is open and playing on the watch page.",
                      timeout=22)
    if not st["ok"]:
        return _abort("watch_link", st, serial)
    await _set_orientation(serial, bh.orientation)
    _cp(serial, "video opened")
    res = await _watch_video(serial, ctrl, bh, watch_s, allow_boredom=False)
    _cp(serial, "watched")
    chained = await _maybe_chain(serial, ctrl, bh)
    await _close_app(serial, ctrl, bh)
    watched = float(res.get("watched_s") or 0)
    if watched <= 0:
        _log(serial, "FAILED: opened the link but nothing played")
        return _abort("watch_link",
                      {"status": "no_playback", "reason": "opened the link but nothing played"},
                      serial)
    return {"ok": True, "flow": "watch_link", "url": url, "chained": chained,
            "checkpoints": _cp_trail(serial), **res}


async def _wait_yt_chooser(serial: str, ctrl, bh: Behavior) -> bool:
    app, once = bh.sel("chooser_app"), bh.sel("chooser_once")
    m = await _ui(serial, *app, *once)
    if any(m.get(q, {}).get("present") for q in app + once):
        await _find_and_tap(serial, ctrl, *app)
        await bh.pause(0.3, 0.7)
        await _find_and_tap(serial, ctrl, *once)
        return True
    return False


async def search_watch(serial: str, ctrl, p: dict, bh: Behavior) -> dict:
    _cp_reset(serial)
    _log(serial, f"search_watch start (behavior={bh.style})")
    query = str(p["query"])
    sort = str(p.get("sort", "") or "")   # ""=relevance, "date","viewcount","rating"
    sp = {"date": "CAQ%3D", "viewcount": "CAM%3D", "rating": "CAE%3D"}.get(sort, "")
    await _set_orientation(serial, bh.orientation)
    await adb.force_stop(serial, PKG)
    await bh.pause(0.8, 1.7)
    _log(serial, f"opening results for '{query}'")
    await _open_url(serial, _yt_search_url(query, sp))
    on = await _await_online(serial, ctrl, bh, timeout=25)
    if not on["ok"]:
        return _abort("search_watch", on, serial)
    _cp(serial, "youtube opened")
    await _set_orientation(serial, bh.orientation)
    await bh.pause(1.4, 3.0)                 # let cards render
    await asyncio.sleep(bh.think(400))       # a human scans the results
    _log(serial, "results up; picking a video")
    on_watch = await _pick_result(serial, ctrl, bh)
    if not on_watch:
        st = await _reach(serial, ctrl, bh, *bh.sel("watch_markers"),
                          goal="You are on a YouTube search results page. Open a video so it plays.", timeout=15)
        if not st["ok"]:
            return _abort("search_watch", st, serial)
    _log(serial, "watching")
    res = await _watch_video(serial, ctrl, bh, _watch_s_pick(p.get("watch_s", 90)))
    chained = await _maybe_chain(serial, ctrl, bh)
    await _close_app(serial, ctrl, bh)
    _log(serial, f"done: watched {res.get('watched_s')}s ({res.get('actions')}) chained={chained}")
    watched = float(res.get("watched_s") or 0)
    if not (on_watch and watched > 0):
        why = ("never opened a video from the results" if not on_watch
               else f"opened a video but watched {watched:g}s")
        _log(serial, f"FAILED: {why}")
        return _abort("search_watch", {"status": "no_playback", "reason": why}, serial)
    return {"ok": True, "flow": "search_watch", "query": query, "opened_video": on_watch,
            "chained": chained, "checkpoints": _cp_trail(serial), **res}


async def channel_watch(serial: str, ctrl, p: dict, bh: Behavior) -> dict:
    _log(serial, "channel_watch start")
    _cp_reset(serial)
    channel = str(p.get("channel") or p.get("query") or "").strip()
    if not channel:
        return _abort("channel_watch", {"status": "unknown", "reason": "no channel given"})
    tab = str(p.get("tab", "Videos") or "Videos")   # Videos | Shorts | Live | Playlists
    await _set_orientation(serial, bh.orientation)
    await adb.force_stop(serial, PKG)
    await bh.pause(0.8, 1.7)
    # NB: a bare "@handle" URL (youtube.com/@Name) is NOT handled by the app's
    # UrlActivity — it punts to the system browser (Samsung Internet), which then
    # traps the flow. Only real URLs / channel-IDs deep-link cleanly; route @handles
    # (and plain names) through in-app search instead, which is reliable.
    direct = channel.startswith(("http", "UC"))
    if direct:
        _log(serial, f"opening channel {channel}")
        await _open_url(serial, _channel_url(channel))
    else:
        # a plain name → open results filtered to CHANNELS, then tap the top channel
        _log(serial, f"searching channels for '{channel}'")
        await _open_url(serial, _yt_search_url(channel.lstrip("@"), "EgIQAg%3D%3D"))
    on = await _await_online(serial, ctrl, bh, timeout=25)
    if not on["ok"]:
        return _abort("channel_watch", on, serial)
    _cp(serial, "youtube opened")
    await _set_orientation(serial, bh.orientation)
    await bh.pause(1.5, 3.0)
    if not direct:
        # This tap is POSITIONAL, so it must not happen until the channel rows
        # are actually rendered — otherwise it lands on an empty list and the
        # flow proceeds believing it opened a channel.
        if not await _await_content(serial, bh, bh.sel("channel_open"), timeout=15.0):
            _log(serial, "channel results still empty — tapping the first row anyway")
        # tap the first channel row (top of the channel-filtered results), positional
        await _click(serial, ctrl.width * random.uniform(0.25, 0.45), ctrl.height * random.uniform(0.24, 0.32))
        await bh.pause(1.5, 3.0)
    # open the requested tab, then browse + pick a video
    await _find_and_tap(serial, ctrl, tab, tab.upper(), ocr=True)
    await bh.pause(0.9, 2.0)
    # The tab switch repopulates the list; wait for real cells before scrolling
    # and picking, so a cold load doesn't send the picker at an empty page.
    await _await_content(serial, bh, bh.sel("cell_probes"), timeout=15.0)
    await humanize.human_scroll(ctrl, ctrl.width, ctrl.height, "up", random.uniform(0.25, 0.7))
    await bh.pause(0.6, 1.8)
    _cp(serial, "channel page reached")
    on_watch = await _pick_result(serial, ctrl, bh)
    if not on_watch and not bh.expired():
        # A channel page often opens on a non-list state first (a pinned/loading
        # header, a Shorts shelf, or a full-width promo) so the first pick can
        # miss. Scroll into the video list and try the picker once more before
        # falling back to recovery — this is the "unexpected screen" case.
        _log(serial, "channel_watch: first pick missed — scrolling into the list and retrying")
        await humanize.human_scroll(ctrl, ctrl.width, ctrl.height, "up", random.uniform(0.4, 0.8))
        await bh.pause(1.0, 2.2)
        on_watch = await _pick_result(serial, ctrl, bh)
    if not on_watch:
        st = await _reach(serial, ctrl, bh, *bh.sel("watch_markers"),
                          goal="You are on a YouTube channel. Open one of the channel's videos so it plays.", timeout=20)
        if not st["ok"]:
            return _abort("channel_watch", st, serial)
    _cp(serial, "video opened")
    res = await _watch_video(serial, ctrl, bh, _watch_s_pick(p.get("watch_s", 90)))
    _cp(serial, "watched")
    await _close_app(serial, ctrl, bh)

    # ok reflects whether a video ACTUALLY played, not whether the function
    # reached its end. This used to be an unconditional True: when
    # _pick_result found nothing and the _reach recovery merely believed it was
    # on a watch page, _watch_video would idle against a static screen for the
    # full duration and the flow reported success. That is the "it opens the
    # channel and then does nothing" failure — reported as a green tick, which
    # is worse than failing, because nobody goes looking.
    watched = float(res.get("watched_s") or 0)
    played = bool(on_watch) and watched > 0
    if not played:
        why = ("never opened a video from the channel page"
               if not on_watch else f"opened a video but watched {watched:g}s")
        _log(serial, f"FAILED: {why}")
        return _abort("channel_watch", {"status": "no_playback", "reason": why}, serial)

    _log(serial, f"done: watched {watched:g}s")
    return {"ok": True, "flow": "channel_watch", "channel": channel,
            "opened_video": on_watch, "checkpoints": _cp_trail(serial), **res}


async def channel_binge(serial: str, ctrl, p: dict, bh: Behavior) -> dict:
    _cp_reset(serial)
    """Watch several of ONE channel's videos IN ORDER, navigating its Videos tab in
    place: open the channel once, then watch → back → advance down the list → watch
    the next. Progresses through the tab's order (newest first) rather than
    re-opening + re-picking (which can repeat). Stops at ``count`` videos or the
    ``max_run_s`` deadline. On a pick miss it hands off to the LLM (if enabled)."""
    channel = str(p.get("channel") or p.get("query") or "").strip()
    if not channel:
        return _abort("channel_binge", {"status": "unknown", "reason": "no channel given"})
    count = max(1, int(p.get("count", 5)))
    tab = str(p.get("tab", "Videos") or "Videos")
    ws = p.get("watch_s", 180)
    await _set_orientation(serial, bh.orientation)
    await adb.force_stop(serial, PKG)
    await bh.pause(0.8, 1.7)
    # NB: a bare "@handle" URL (youtube.com/@Name) is NOT handled by the app's
    # UrlActivity — it punts to the system browser (Samsung Internet), which then
    # traps the flow. Only real URLs / channel-IDs deep-link cleanly; route @handles
    # (and plain names) through in-app search instead, which is reliable.
    direct = channel.startswith(("http", "UC"))
    await _open_url(serial, _channel_url(channel) if direct else _yt_search_url(channel.lstrip("@"), "EgIQAg%3D%3D"))
    on = await _await_online(serial, ctrl, bh, timeout=25)
    if not on["ok"]:
        return _abort("channel_binge", on, serial)
    _cp(serial, "youtube opened")
    await _set_orientation(serial, bh.orientation)
    await bh.pause(1.5, 3.0)
    if not direct:                                   # a plain name → tap the top channel row
        await _click(serial, ctrl.width * random.uniform(0.25, 0.45),
                     ctrl.height * random.uniform(0.24, 0.32))
        await bh.pause(1.5, 3.0)
    await _find_and_tap(serial, ctrl, tab, tab.upper(), ocr=True)
    await bh.pause(0.9, 2.0)

    watched = []
    for i in range(count):
        if bh.expired():
            break
        if i > 0:
            # advance down the Videos list so the next (unwatched) video sits on top
            await humanize.human_scroll(ctrl, ctrl.width, ctrl.height, "up",
                                        random.uniform(0.45, 0.7))
            await bh.pause(0.6, 1.5)
        opened = await _pick_result(serial, ctrl, bh)
        if not opened and not bh.expired():          # first pick can miss on a mid-load / promo header
            await humanize.human_scroll(ctrl, ctrl.width, ctrl.height, "up", random.uniform(0.4, 0.8))
            await bh.pause(1.0, 2.0)
            opened = await _pick_result(serial, ctrl, bh)
        if not opened:
            st = await _reach(serial, ctrl, bh, *bh.sel("watch_markers"),
                              goal="Open the next video in this channel's Videos list.", timeout=20)
            if not st["ok"]:
                break
        await _set_orientation(serial, bh.orientation)
        res = await _watch_video(serial, ctrl, bh, _watch_s_pick(ws))
        watched.append({"n": i + 1, "watched_s": res.get("watched_s")})
        _log(serial, f"channel_binge {i+1}/{count}: watched {res.get('watched_s')}s")
        await adb.keyevent(serial, "KEYCODE_BACK")    # back to the Videos list
        await bh.pause(1.2, 2.8)
    await _close_app(serial, ctrl, bh)
    return {"ok": bool(watched), "flow": "channel_binge", "channel": channel,
            "count": len(watched), "detail": watched}


async def shorts(serial: str, ctrl, p: dict, bh: Behavior) -> dict:
    _cp_reset(serial)
    _log(serial, "shorts start")
    duration_s = float(p.get("duration_s", 120))
    channel = (p.get("channel") or "").strip() or None
    short_id = (p.get("short_id") or "").strip() or None
    w, h = ctrl.width, ctrl.height
    await _set_orientation(serial, bh.orientation)
    await adb.force_stop(serial, PKG)
    await bh.pause(0.8, 1.7)

    if short_id:                                    # deep-link a specific short → feed
        await _open_url(serial, f"https://www.youtube.com/shorts/{quote(short_id, safe='')}")
        on = await _await_online(serial, ctrl, bh, timeout=25)
        if not on["ok"]:
            return _abort("shorts", on, serial)
    elif channel:                                   # a channel's Shorts grid
        await _open_url(serial, _channel_url(channel))
        on = await _await_online(serial, ctrl, bh, timeout=25)
        if not on["ok"]:
            return _abort("shorts", on, serial)
        await bh.pause(1.5, 3.0)
        await _find_and_tap(serial, ctrl, *bh.sel("channel_shorts_tab"), ocr=True)
        await bh.pause(1.0, 2.0)
        await _pick_result(serial, ctrl, bh)        # open the first short of the grid
    else:                                           # the main Shorts feed via deep-link
        # `/shorts` (no id) opens the reel player directly — portrait-locked by the
        # app, so it dodges the tilted-phone landscape flip entirely (far more
        # reliable than a blind positional Shorts-tab tap).
        await _open_url(serial, "https://www.youtube.com/shorts")
        on = await _await_online(serial, ctrl, bh, timeout=25)
        if not on["ok"]:
            return _abort("shorts", on, serial)

    await _force_portrait(serial, bh)
    st = await _reach(serial, ctrl, bh, "reel_watch_player", "reel_recycler", "id/reel_time_bar",
                      *bh.sel("shorts_markers"),
                      goal="Open YouTube Shorts and start playing a short video.", timeout=16)
    if not st["ok"]:
        return _abort("shorts", st, serial)

    seg_r = bh.prof["shorts"].get("segment_s", [5.0, 30.0])
    short_r = bh.prof["shorts"].get("short_frac_s", [1.5, 5.0])
    step_r = bh.prof["shorts"].get("step_s", [1.2, 4.0])
    tap_x, tap_y = bh.geo("shorts_tap_x", 0.5), bh.geo("shorts_tap_y", 0.5)
    like_x, like_y = bh.geo("shorts_like_x", 0.5), bh.geo("shorts_like_y", 0.55)
    sw_x = bh.geo("shorts_swipe_x", [0.45, 0.55])
    sw_fy, sw_ty = bh.geo("shorts_swipe_from_y", 0.78), bh.geo("shorts_swipe_to_y", 0.22)

    start = _now()
    watched = liked = 0
    last_orient = _now()
    while _now() - start < duration_s and not bh.expired():
        # keep the reel upright — a tilted phone flips to landscape, which turns the
        # vertical "next short" swipe into a mis-gesture that drifts out of the feed.
        if bh.orientation != "auto" and _now() - last_orient > 12:
            await _force_portrait(serial, bh)
            last_orient = _now()
        seg = random.uniform(seg_r[0], seg_r[1]) * bh._mult()
        if bh.roll(bh.sp("short_watch")):
            seg = random.uniform(short_r[0], short_r[1])
        seg = min(seg, duration_s - (_now() - start))
        if seg <= 0:
            break
        elapsed = 0.0
        while elapsed < seg:
            step = min(random.uniform(step_r[0], step_r[1]), seg - elapsed)
            await asyncio.sleep(step)
            elapsed += step
            if bh.roll(bh.sp("double_tap_pause")):
                await humanize.human_tap(ctrl, w * tap_x, h * tap_y)
                await bh.pause(0.5, 2.4)
                await humanize.human_tap(ctrl, w * tap_x, h * tap_y)
        if bh.roll(bh.sp("like")):
            await humanize.human_tap(ctrl, w * like_x, h * like_y)
            await asyncio.sleep(random.uniform(0.05, 0.14))
            await humanize.human_tap(ctrl, w * like_x, h * like_y)
            liked += 1
        watched += 1
        await humanize.human_swipe(ctrl, w * random.uniform(sw_x[0], sw_x[1]), h * sw_fy,
                                   w * random.uniform(sw_x[0], sw_x[1]), h * sw_ty)
        await bh.pause(0.4, 1.8)

    await _close_app(serial, ctrl, bh)
    # Zero shorts swiped means the reel never came up — the loop simply never
    # ran. Reporting that as success is the same lie as channel_watch's.
    if watched <= 0:
        _log(serial, "FAILED: no shorts were watched")
        return _abort("shorts", {"status": "no_playback", "reason": "no shorts were watched"},
                      serial)
    return {"ok": True, "flow": "shorts", "watched": watched, "liked": liked,
            "checkpoints": _cp_trail(serial),
            "duration_s": round(_now() - start, 1), "channel": channel}


def _watch_s_pick(spec) -> float:
    """A single-video watch time from either a fixed number or a [min,max] range
    (so each video in a session gets its own varied duration).

    Also accepts "min,max" as a STRING. Recipe variables substitute as text, so
    a {{watch_s}} of "15,30" arrives here as a string and used to raise
    ValueError mid-flow — the run died on a value the UI had happily accepted.
    Parsing it here keeps the failure out of the operator's way rather than
    forcing every recipe author to know the internal type.
    """
    if isinstance(spec, str) and "," in spec:
        try:
            lo, hi = (float(x.strip()) for x in spec.split(",", 1))
            spec = [lo, hi]
        except ValueError:
            pass          # fall through to the numeric path, which reports it
    if isinstance(spec, (list, tuple)) and len(spec) == 2:
        return random.uniform(float(spec[0]), float(spec[1]))
    return float(spec or 120)


async def session(serial: str, ctrl, p: dict, bh: Behavior) -> dict:
    # Claim the trail so the per-video sub-flows below cannot wipe it.
    _cp_reset(serial, own=True)
    """Watch a SESSION of many videos in a chosen pattern, reusing the single-video
    flows so every video keeps the full human behaviour (pause/resume/skip/like,
    varied watch time, boredom). Stops at ``count`` videos or the ``max_run_s``
    session deadline, whichever comes first, with a human gap between videos.

    patterns:
      channels  — watch videos from ``channels`` (cycled; set mix/order=random)
      mix       — interleave videos across several ``channels`` round-robin
      search    — watch videos for each of ``queries`` (interest-based)
      random    — watch varied videos (random ``queries`` or a broad default pool)
      links     — watch a list of specific ``urls`` (order sequential|random)
    Watch time: ``watch_s`` may be a number or a [min,max] range (varied per video)."""
    pattern = str(p.get("pattern", "search")).lower()
    count = max(1, int(p.get("count", 3)))
    order = str(p.get("order", "sequential")).lower()
    ws = p.get("watch_s", 120)
    channels = list(p.get("channels") or ([p["channel"]] if p.get("channel") else []))
    queries = list(p.get("queries") or ([p["query"]] if p.get("query") else []))
    urls = list(p.get("urls") or ([p["url"]] if p.get("url") else []))

    plan: list[tuple] = []
    if pattern in ("channels", "channel", "mix"):
        if not channels:
            return _abort("session", {"status": "unknown", "reason": "pattern needs 'channels'"})
        for i in range(count):
            if pattern == "mix" or p.get("mix"):
                ch = channels[i % len(channels)]                 # round-robin interleave
            elif order == "random":
                ch = random.choice(channels)
            else:
                ch = channels[i % len(channels)]                 # cycle through the list
            plan.append((channel_watch, {"channel": ch, "tab": p.get("tab", "Videos")}))
    elif pattern in ("search", "interest"):
        if not queries:
            return _abort("session", {"status": "unknown", "reason": "pattern needs 'queries'"})
        for i in range(count):
            q = random.choice(queries) if order == "random" else queries[i % len(queries)]
            plan.append((search_watch, {"query": q, "sort": p.get("sort", "")}))
    elif pattern == "random":
        pool = queries or ["music", "news today", "documentary", "gaming highlights",
                           "podcast", "movie trailer", "how to", "travel vlog", "live"]
        for _ in range(count):
            plan.append((search_watch, {"query": random.choice(pool),
                                        "sort": random.choice(["", "date", "viewcount"])}))
    elif pattern == "links":
        if not urls:
            return _abort("session", {"status": "unknown", "reason": "pattern needs 'urls'"})
        seq = list(urls)
        if order == "random":
            random.shuffle(seq)
        for u in seq[:count if count < len(seq) else len(seq)]:
            plan.append((watch_link, {"url": u}))
    else:
        return _abort("session", {"status": "unknown", "reason": f"unknown pattern '{pattern}'"})

    watched: list[dict] = []
    try:
        for i, (fn, sub) in enumerate(plan, 1):
            if bh.expired():
                # Running out of the session budget is a NORMAL ending, not a
                # fault — but it is invisible unless it is said, and a session
                # that stopped at 2 of 6 looks identical to one that failed.
                _log(serial, f"session: max_run_s reached after {len(watched)} video(s)")
                _cp(serial, f"stopped: out of time after {len(watched)}/{len(plan)}")
                break
            _cp(serial, f"video {i}/{len(plan)} start")
            sub = {**sub, "watch_s": _watch_s_pick(ws)}
            try:
                r = await fn(serial, ctrl, sub, bh)
            except Exception as e:  # noqa: BLE001
                r = {"ok": False, "error": str(e)}
            ok = bool(r.get("ok"))
            watched.append({"n": i, "flow": r.get("flow"), "ok": ok,
                            "watched_s": r.get("watched_s"),
                            # Keep WHY a video failed. Without it a session
                            # reports "2 of 6 ok" and nothing about the four.
                            "reason": None if ok else (r.get("reason") or r.get("error")),
                            "checkpoints": r.get("checkpoints")})
            _cp(serial, f"video {i}/{len(plan)} {'ok' if ok else 'FAILED'}")
            _log(serial, f"session {i}/{len(plan)}: {r.get('flow')} ok={ok}")
            if i < len(plan) and not bh.expired():
                await bh.pause(1.5, 5.0)      # a human gap before the next video
    finally:
        _cp_release(serial)

    failed = [w for w in watched if not w["ok"]]
    out = {"ok": any(w["ok"] for w in watched), "flow": "session", "pattern": pattern,
           "videos_ok": sum(1 for w in watched if w["ok"]), "planned": len(plan),
           "checkpoints": _cp_trail(serial), "detail": watched}
    # A session that watched NOTHING must not read as a bland "0 of 6" — say why
    # the first failure happened, in the field the run log actually surfaces.
    if failed and not out["ok"]:
        out["reason"] = f"0 of {len(plan)} videos played — first failure: {failed[0].get('reason') or 'unknown'}"
    return out


# ---------------------------------------------------------- dispatch ---------
async def _proxy_dns_failing(serial: str) -> bool:
    """True if the device's Clash/CMFA tunnel is failing DNS right now. When the
    assigned upstream proxy is dead/bandwidth-capped, the tunnel logs a flood of
    'all DNS requests failed' — which is why a consent 'Reject all' POST (and video
    playback) can't complete. Distinguishes 'the tap didn't work' (our problem) from
    'the proxy is down' (infrastructure) so the operator fixes the right thing."""
    r = await adb.shell(serial, "logcat -d -t 250 -v brief 2>/dev/null | grep -c 'all DNS requests failed'")
    try:
        return int((r.get("stdout", "") or "0").strip()) >= 3
    except ValueError:
        return False


async def onboard(serial: str, ctrl, p: dict, bh: Behavior) -> dict:
    """One-time device prep so signed-out YouTube flows run unblocked. There is no
    Google account on these devices (and none is needed — every watch/search/binge
    flow works signed-out); the ONLY thing blocking them is the cookie-consent gate.
    This:
      1. locks portrait and stops YouTube silently re-enabling auto-rotate on cold
         launch (so blind positional taps land where OCR sees them),
      2. opens YouTube to a clean state,
      3. dismisses the consent gate by tapping 'Reject all',
      4. confirms the gate is gone and YouTube is foreground.
    Idempotent: a device already past consent just verifies and returns ok."""
    _log(serial, "onboard start")
    # Belt-and-suspenders: block YouTube from turning auto-rotate back on. _force_portrait
    # re-asserts the lock post-launch regardless, but this keeps idle devices portrait too.
    await adb.shell(serial, f"appops set {PKG} WRITE_SETTINGS ignore")
    await _set_orientation(serial, "portrait")
    # The consent 'Reject all' POST goes through the device's proxy and its success is
    # flaky per exit node / moment (some proxies Google accepts on the first tap; others
    # spin and reset). Consent is one-time + persisted locally, so a device only needs
    # ONE success — retry the WHOLE cycle (fresh relaunch → fresh consent page → fresh
    # submission) a few times. A cooperating proxy clears on attempt 1.
    attempts = max(1, int(p.get("consent_attempts", 4)))
    dismissed = still_gate = False
    attempt = 0
    for attempt in range(1, attempts + 1):
        # Respect the run's time budget. On a device whose exit node never
        # completes Google's consent POST, consent NEVER clears, so without this
        # onboard would burn every attempt and blow past the dispatch timeout
        # (the "agent did not answer" hang) instead of giving up at max_run_s.
        if attempt > 1 and bh.expired():
            _log(serial, f"onboard: time budget reached — stopping after {attempt - 1} attempt(s)")
            break
        await adb.force_stop(serial, PKG)
        await bh.pause(0.8, 1.6)
        await adb.launch_package(serial, PKG)
        await asyncio.sleep(bh.think(300))
        await _force_portrait(serial, bh)          # YouTube flips landscape on cold start
        # The consent WebView is network-bound and can take several seconds to
        # render — but never wait longer than the budget that's left.
        gate = False
        deadline = _now() + min(20.0, bh.remaining() or 20.0)
        while _now() < deadline:
            if await _consent_up(serial):
                gate = True
                break
            await asyncio.sleep(1.0)
        if not gate:                               # already past consent (or it cleared)
            _log(serial, "no consent gate detected — already past it")
            still_gate = False
            break
        await _force_portrait(serial, bh)          # portrait so OCR coords tap correctly
        dismissed = await _dismiss_consent(serial, ctrl, bh)
        await _force_portrait(serial, bh)
        still_gate = await _consent_up(serial)
        if not still_gate:
            _log(serial, f"consent cleared on attempt {attempt}/{attempts}")
            break
        _log(serial, f"consent still up after attempt {attempt}/{attempts} — retrying fresh")
        await bh.pause(2.0, 4.0)
    # This YouTube build barely exposes the Home feed to a11y, so don't hard-gate on
    # home_markers — the real success signal is "consent gone AND still in YouTube".
    fg = await _foreground_pkg(serial)
    ok = (fg == PKG) and not still_gate
    res = {"ok": ok, "flow": "onboard", "consent_dismissed": dismissed,
           "consent_cleared": not still_gate, "foreground": fg, "portrait_locked": True,
           "attempts": attempt}
    if not ok:
        res["status"] = "blocked"
        if still_gate and await _proxy_dns_failing(serial):
            # Tap registered but the consent POST never completes AND the tunnel is
            # flooding DNS failures — the assigned exit node won't complete Google's
            # consent flow. Infrastructure, not automation: reassign to another proxy.
            res["reason"] = (f"consent tap registered but its submission didn't complete after "
                             f"{attempt} attempts — the device's proxy exit node won't complete "
                             f"Google's consent flow. Reassign to a different proxy.")
            res["proxy_dns_failing"] = True
        else:
            res["reason"] = (f"consent gate still up after {attempt} Reject-all attempts"
                             if still_gate else f"not in YouTube after onboard (foreground={fg})")
    _log(serial, f"onboard {'ok' if ok else 'FAILED'} in {attempt} attempt(s) "
                 f"(cleared={not still_gate} fg={fg})")
    return res


_FLOWS = {"watch_link": watch_link, "search_watch": search_watch,
          "channel_watch": channel_watch, "channel_binge": channel_binge,
          "shorts": shorts, "session": session, "onboard": onboard}


def _opt_float(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _parse_profile(v) -> dict | None:
    """A per-run profile override: a dict, or a JSON string (from the node field)."""
    if isinstance(v, dict):
        return v
    if isinstance(v, str) and v.strip():
        try:
            import json
            o = json.loads(v)
            return o if isinstance(o, dict) else None
        except Exception:  # noqa: BLE001 — bad JSON just falls back to defaults
            return None
    return None


async def run(serial: str, ctrl, payload: dict) -> dict:
    """Entry point for the agent's `youtube` action. payload = {flow, ...,
    behavior?, max_run_s?, orientation?, engage?, llm_fallback?, detect?, profile?}."""
    flow = payload.get("flow")
    fn = _FLOWS.get(flow)
    if fn is None:
        return {"ok": False, "error": f"unknown youtube flow '{flow}'"}
    eng = payload.get("engage")
    if isinstance(eng, str):
        eng = {e.strip().lower() for e in eng.split(",") if e.strip()}
    elif isinstance(eng, list):
        eng = {str(e).lower() for e in eng}

    def _truthy(v):
        return v in (True, "true", "1", 1, "yes", "on")

    # persona = a named engagement preset; an explicit `profile` override wins on top.
    overrides = yt_config.persona_overrides(payload.get("persona") or "", _parse_profile(payload.get("profile")))
    profile = yt_config.load_profile(overrides)
    bh = Behavior(style=payload.get("behavior") or payload.get("style") or "random",
                  max_run_s=_opt_float(payload.get("max_run_s")),
                  orientation=payload.get("orientation") or "portrait",
                  engage=eng,
                  llm_fallback=_truthy(payload.get("llm_fallback")),
                  detect_blocks=_truthy(payload.get("detect")),
                  profile=profile)
    try:
        res = await fn(serial, ctrl, payload, bh)
        res.setdefault("behavior", bh.style)
        return res
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "flow": flow, "error": str(e)}
