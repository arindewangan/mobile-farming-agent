"""
YouTube behaviour **profile** — every tunable that used to be a magic number or
an inline list now lives here, in one heavily-commented place, so the flows in
``youtube.py`` are pure logic and the *behaviour* is data you can change without
touching code.

Three layers, deep-merged in order (later wins), so you can tune at any scope:

  1. ``DEFAULT_PROFILE`` below            — the shipped defaults (identical to the
                                            old hard-coded values, so nothing
                                            changes until you override something).
  2. ``agent/yt_profile.json`` (optional) — drop this file next to the agent to
                                            override globally for the whole fleet,
                                            no redeploy, no Python.  Only the keys
                                            you include are overridden.
  3. per-run overrides                    — a ``profile`` dict passed in the run
                                            payload (from the workflow node's
                                            "profile (JSON)" field), so a single
                                            campaign can run hotter/cooler/quieter
                                            than the fleet default.

Everything is organised by concern:

  * ``tempo``     — how pauses are scaled/jittered per style (calm/normal/quick/random).
  * ``selectors`` — the on-screen text / resource-ids the flows look for (popups to
                    dismiss, ad-skip labels, hard blockers, page markers, …).  Add a
                    new popup text or a new blocker here, not in code.
  * ``geometry``  — where things are, as *fractions* of the screen (0..1), so it
                    scales across devices — player position, seek zones, the
                    seekbar line, comment-scroll band, OCR regions, shorts gestures.
  * ``watch``     — the watch-loop: per-decision probabilities (like/seek/comment/…)
                    and every timing range.
  * ``shorts``    — the shorts loop probabilities + timings.
  * ``search`` / ``open`` / ``close`` — search typing/submit, app-open glance, and
                    how the app is left (home / recents-swipe / leave open).

Probabilities are 0..1.  ``[a, b]`` pairs are inclusive uniform ranges.  Times are
seconds.  Coordinates are fractions of width/height.
"""
from __future__ import annotations

import copy
import json
import os

DEFAULT_PROFILE: dict = {
    # -------------------------------------------------------------- tempo ----
    "tempo": {
        # multiplier applied to every pause; higher = slower, calmer, longer dwells.
        "styles": {"calm": 1.55, "slow": 1.55, "normal": 1.0, "quick": 0.68, "fast": 0.68},
        # the 'random' style re-rolls a fresh multiplier on EVERY pause (no cadence
        # ever repeats): pick one bucket, times a small jitter.
        "random_buckets": [0.55, 0.75, 1.0, 1.3, 1.7, 2.1],
        "random_jitter": [0.8, 1.2],
        # fixed styles (calm/normal/quick) also jitter each pause a little.
        "style_jitter": [0.82, 1.2],
        # "think" time before deliberate actions (typing, picking) scales like this.
        "think_random_jitter": [0.7, 1.8],
    },

    # ---------------------------------------------------------- selectors ----
    # Text / content-desc / resource-id substrings. uiautomator matches these; OCR
    # is used as a fallback for the plain-text ones.
    "selectors": {
        # popups a person would just clear — dismissed opportunistically by the guard
        "dismiss": [
            "No thanks", "NO THANKS", "Not now", "NOT NOW", "Skip trial", "No, thanks",
            "Dismiss", "Got it", "GOT IT", "Ask me later", "Later", "Maybe later",
            "Cancel", "Turn off", "Continue watching", "Yes, I'm still watching",
            "Keep watching", "Accept all", "I agree", "Allow", "OK", "Continue",
        ],
        # ad-skip button text/desc — tapped the instant it becomes tappable
        "skip": ["Skip Ads", "Skip Ad", "Skip ad", "Skip ads", "SKIP AD", "Skip"],
        # hard blockers we CANNOT pass → abort with this reason so a human steps in.
        # [query-substring, human-reason]. Keep specific to avoid false positives.
        "blockers": [
            ["verify it's you", "account verification (verify it's you)"],
            ["Verify it's you", "account verification (verify it's you)"],
            ["unusual traffic", "captcha / unusual traffic"],
            ["not a robot", "captcha (are you a robot)"],
            ["Confirm you're not a robot", "captcha"],
            ["Verify you're human", "human verification"],
            ["Enter your password", "sign-in required"],
            ["Couldn't sign you in", "sign-in error"],
            ["Choose an account", "account picker / sign-in"],
            ["Sign in to YouTube", "sign-in required"],
            ["You're signed out", "signed out"],
            ["keeps stopping", "app crashed (keeps stopping)"],
            ["isn't responding", "app not responding (ANR)"],
            ["has stopped", "app crashed"],
            ["Update YouTube", "app update required"],
            ["Get the new YouTube", "app update required"],
            ["no longer supported", "app version unsupported"],
            ["You're offline", "no network"],
            ["check your network connection", "no network"],
            ["No internet", "no network"],
            ["No connection", "no network"],
        ],
        # "we're on a watch page" markers
        "watch_markers": ["id/watch_player", "id/player", "Like this video", "Share",
                          "id/engagement_panel", "Subscribe", "Comments", "Save", "Report"],
        # "we're on a search RESULTS page" markers (not the suggestions dropdown)
        "results_markers": [" views", " ago", "No results", "Sponsored", "Subscribe", "Filters"],
        # "we're on the Home feed" markers
        "home_markers": ["Search YouTube", "Home", "Shorts", "Subscriptions"],
        # the home search bar
        "search_hints": ["Search YouTube", "id/search", "Search"],
        # the focused search text field
        "search_edit": ["id/search_edit_text", "Search YouTube", "id/search"],
        # things that mark a result cell as an AD (skip it when picking)
        "ad_markers": ["Sponsored", "Install", "Ad ·", "Ad·", "Get app"],
        # a11y probes whose presence means "a real video cell" (its centre opens it)
        "cell_probes": [" views", " watching", " ago"],
        # OCR tokens that mark an organic result (read off pixels if the tree misses)
        "cell_ocr_tokens": ["views", "watching", "ago"],
        "like": ["Like this video", "like this video"],
        "subscribe": ["Subscribe"],
        "comments_open": ["Comments", "id/comments_entry_point"],
        "replay": ["Replay", "id/player_control_replay_button"],
        "retry": ["Retry", "RETRY", "Try again", "TRY AGAIN"],
        "chooser_app": ["YouTube"],
        "chooser_once": ["Just once", "JUST ONCE"],
        "channel_open": ["subscribers", "Verified", "channel"],
        "channel_videos_tab": ["Videos", "VIDEOS"],
        "channel_shorts_tab": ["Shorts"],
        "shorts_entry": ["Shorts"],
        "shorts_markers": ["Shorts", "like", "Subscribe", "Comment"],
    },

    # ---------------------------------------------------------- geometry ----
    # Fractions of the scrcpy control surface (0..1). Player lives in the top band
    # on a portrait watch page.
    "geometry": {
        "player_cx": 0.5, "player_cy": 0.16,         # centre of the video (tap = show controls)
        "seekbar_y": 0.255,                           # the scrub line, for slider seeks
        "seek_fwd_x": 0.85, "seek_back_x": 0.15,      # double-tap zones for ±10s
        "comment_swipe_from_y": 0.75, "comment_swipe_to_y": 0.42,   # scrolling comments
        "results_region": [0.0, 0.14, 1.0, 0.96],     # OCR band for result 'views'
        "skip_region": [0.5, 0.45, 1.0, 0.95],        # OCR band for 'Skip'
        "suggestion_region": [0.0, 0.1, 1.0, 0.45],   # OCR band for the first suggestion
        "first_result_x": [0.2, 0.4], "first_result_y": [0.2, 0.34],  # positional fallback tap
        "channel_thumb_x": [0.12, 0.22], "channel_thumb_y": [0.24, 0.32],
        "shorts_tap_x": 0.5, "shorts_tap_y": 0.5,     # pause/resume tap on a short
        "shorts_like_x": 0.5, "shorts_like_y": 0.55,  # double-tap-to-like a short
        "shorts_swipe_x": [0.45, 0.55], "shorts_swipe_from_y": 0.78, "shorts_swipe_to_y": 0.22,
        "peek_x": 0.5, "peek_y": 0.30,                # tap the title/description to expand
    },

    # ------------------------------------------------------------- watch ----
    "watch": {
        # per-decision probabilities inside the watch loop
        "p": {
            "boredom": 0.18,            # abandon early like a bored viewer
            "skip_ad_recheck": 0.5,     # re-check for a mid-roll skip each tick
            "pause_resume": 0.14,       # pause, look away, resume
            "seek": 0.12,               # skip ahead (double-tap or slider)
            "seek_back": 0.05,          # skip back a bit
            "comment": 0.12,            # open comments, read, close
            "like": 0.09,               # like the video
            "subscribe_slot": 0.06,     # reach the subscribe decision
            "subscribe_gate": 0.40,     # ...and actually do it this often
            "seek_slider_vs_double": 0.5,  # of seeks, how many use the slider vs double-tap
            "replay": 0.22,             # replay when it ends
            "peek_description": 0.10,   # expand the title/description, read, collapse
            "watch_related": 0.12,      # after finishing, ride the up-next/related chain
        },
        "segment_s": [3.5, 11.0],       # watch this long between decisions (×tempo)
        "watch_variance": [0.85, 1.12], # actual target = watch_s × uniform(this)
        "boredom_frac": [0.0, 0.45],    # bored cut = uniform(5, watch_s×this[1])
        "pause_look_s": [1.2, 5.0],     # how long the "look away" pause lasts
        "seek_slider_to": [0.3, 0.85],  # scrub target fraction of the bar
        "reorient_every_s": 20,         # re-assert portrait at most this often
        "replay_extra_s": [5, 20],      # watch a bit more after a replay
        "initial_settle_s": [1.5, 4.0], # settle before the first ad-check
        "peek_read_s": [1.0, 3.0],      # how long to read the expanded description
    },

    # ------------------------------------------------------------ shorts ----
    "shorts": {
        "p": {
            "short_watch": 0.22,        # sometimes barely watch a short and swipe on
            "double_tap_pause": 0.08,   # tap-pause-resume mid-short
            "like": 0.16,               # double-tap like
        },
        "segment_s": [5.0, 30.0],       # dwell on a short (×tempo)
        "short_frac_s": [1.5, 5.0],     # the "barely watched" dwell
        "step_s": [1.2, 4.0],           # inner sleep granularity
    },

    # ------------------------------------------------------------ search ----
    "search": {
        "typo_rate": 0.05,              # human_type typo injection rate
        "type_attempts": 2,             # retype if the box didn't take it
        "submit_attempts": 3,           # suggestion-tap / ENTER retries
        "suggestion_y_min": 230,        # a suggestion row sits between these y px
        "suggestion_y_max": 900,
    },

    # -------------------------------------------------------------- open ----
    "open": {
        "glance_prob": 0.35,            # glance at the home feed before acting
        "glance_frac": [0.3, 0.7],      # scroll fraction of the glance
        "forcestop_pause_s": [0.8, 1.7],
        "prelaunch_pause_s": [0.8, 1.7],
    },

    # ------------------------------------------------------------- close ----
    # how the app is left. home_prob + recents_prob must be ≤ 1; the remainder is
    # "just leave it open" (like locking the phone mid-app). Never force-stop.
    "close": {"home_prob": 0.6, "recents_prob": 0.28},
}


# -------------------------------------------------------------- personas ----
# Named engagement presets — profile overrides that turn the 4 flows into ~any
# viewing persona. Selected per run via payload "persona"; a per-run "profile"
# override still wins on top. This is how one small engine expresses many scripts.
PERSONAS: dict = {
    "casual": {},   # the shipped defaults — a balanced, unremarkable viewer
    "engaged": {"watch": {"p": {"like": 0.5, "comment": 0.4, "subscribe_slot": 0.3,
                                "subscribe_gate": 0.7, "boredom": 0.04, "peek_description": 0.4,
                                "replay": 0.3, "watch_related": 0.2}, "watch_variance": [0.9, 1.15]}},
    "completionist": {"watch": {"p": {"boredom": 0.03, "like": 0.4, "comment": 0.22, "replay": 0.35,
                                      "peek_description": 0.3, "seek": 0.06}, "watch_variance": [0.95, 1.15]}},
    "lurker": {"watch": {"p": {"like": 0.03, "comment": 0.02, "subscribe_slot": 0.0, "seek": 0.05,
                               "boredom": 0.4, "peek_description": 0.04, "replay": 0.05, "watch_related": 0.05}}},
    "skimmer": {"watch": {"p": {"seek": 0.5, "seek_back": 0.08, "boredom": 0.3, "like": 0.1,
                                "comment": 0.03, "seek_slider_vs_double": 0.5}, "segment_s": [2.0, 6.5]}},
    "distracted": {"watch": {"p": {"pause_resume": 0.5, "like": 0.15, "seek_back": 0.12},
                             "pause_look_s": [2.0, 9.0]}},
    "rewatcher": {"watch": {"p": {"seek_back": 0.3, "seek": 0.1, "replay": 0.5, "like": 0.5,
                                  "boredom": 0.05}}},
    "binger": {"watch": {"p": {"watch_related": 0.6, "like": 0.35, "boredom": 0.1, "comment": 0.15}},
               "shorts": {"p": {"short_watch": 0.15, "like": 0.28, "double_tap_pause": 0.2},
                          "segment_s": [8.0, 35.0]}},
    "doomscroll": {"shorts": {"p": {"short_watch": 0.65, "like": 0.08, "double_tap_pause": 0.03},
                              "segment_s": [3.0, 12.0]}},
}


def persona_overrides(name: str, extra: dict | None = None) -> dict:
    """Merge a named persona's overrides with an optional per-run override dict."""
    return _deep_merge(PERSONAS.get(name or "", {}), extra or {})


def _deep_merge(base: dict, over: dict) -> dict:
    """Recursively merge ``over`` onto a copy of ``base`` (dict values merge; lists
    and scalars replace wholesale, so a caller can swap a whole selector list)."""
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


_FILE = os.path.join(os.path.dirname(__file__), "yt_profile.json")
_file_cache: dict | None = None
_file_mtime: float = 0.0


def _file_overrides() -> dict:
    """Load agent/yt_profile.json if present (hot: re-read when the file changes)."""
    global _file_cache, _file_mtime
    try:
        mt = os.path.getmtime(_FILE)
    except OSError:
        _file_cache, _file_mtime = None, 0.0
        return {}
    if _file_cache is None or mt != _file_mtime:
        try:
            with open(_FILE, encoding="utf-8") as fh:
                _file_cache = json.load(fh)
            _file_mtime = mt
        except Exception:  # noqa: BLE001 — a malformed file must never break a run
            _file_cache = {}
    return _file_cache or {}


def load_profile(overrides: dict | None = None) -> dict:
    """Resolve the effective profile: DEFAULT ← yt_profile.json ← per-run overrides."""
    prof = _deep_merge(DEFAULT_PROFILE, _file_overrides())
    if overrides:
        prof = _deep_merge(prof, overrides)
    return prof
