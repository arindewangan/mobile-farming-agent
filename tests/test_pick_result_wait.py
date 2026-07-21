"""
The cold-list race: picking at a page whose list has not loaded.

A real failure, in the flow's own words — the guard wanted a video player and
the screen held only the channel header and the bottom nav bar:

    waiting for: id/watch_player, id/player, Like this video, Share
    on screen:   HypeGaming, Home, Shorts, Subscriptions, You

The channel page was up; its video list was not. `_pick_result` tapped at it
anyway, all three tiers missed, and the flow reported "unexpected screen".

Five of `_pick_result`'s six call sites reached it after nothing but a fixed
pause or a scroll, so the wait belongs INSIDE it rather than at each caller.

The property that must not regress: absent a11y cells do NOT mean an empty
page. Some results pages are a11y-blind, which is the whole reason the OCR and
positional tiers exist. So the wait is a delay, never a gate — after waiting,
`_pick_result` must still run its tiers. A test suite that only checked "does it
wait" would happily pass an implementation that returns False early and breaks
every blind page on the fleet.
"""
from __future__ import annotations

import asyncio
import inspect

import youtube


def _src() -> str:
    return inspect.getsource(youtube._pick_result)


class TestTheWaitExists:
    def test_pick_result_waits_for_content_before_tapping(self):
        src = _src()
        assert "_await_content" in src, \
            "_pick_result must wait for the list before tapping at it"

    def test_the_wait_happens_before_the_first_tier(self):
        """Waiting after the a11y tier has already missed would be pointless —
        the miss is the thing being prevented."""
        src = _src()
        assert src.index("_await_content") < src.index("cell_ocr_tokens"), \
            "the wait must precede the picking tiers"

    def test_there_is_a_nudge_and_a_second_wait(self):
        """A single wait is not enough: the lazy fetch behind a tab switch often
        does not start until something scrolls."""
        src = _src()
        assert src.count("_await_content") >= 2, \
            "a nudge scroll plus a second wait is what actually recovers the page"


class TestItIsADelayNotAGate:
    """The regression that would be invisible in staging and expensive on the
    fleet: making the wait authoritative breaks every a11y-blind page."""

    def test_pick_result_never_returns_early_on_a_missing_list(self):
        src = _src()
        head = src.split("async def _try_open", 1)[0]
        assert "return False" not in head, \
            "an absent cell probe means 'maybe a11y-blind', not 'empty' — " \
            "returning early here disables the OCR and positional tiers"

    def test_the_ocr_and_positional_tiers_are_still_present(self):
        src = _src()
        assert "cell_ocr_tokens" in src           # tier 2
        assert "results_region" in src            # tier 2 region
        assert "for fy in" in src                 # tier 3

    def test_a_blind_page_is_logged_rather_than_treated_as_a_failure(self):
        assert "a11y-blind" in _src()


class TestWaitBudget:
    def test_the_timeouts_are_named_and_bounded(self):
        """Unbounded waiting would turn a slow proxy into a hung session; these
        are called in a loop, once per video."""
        assert 0 < youtube._PICK_WAIT_S <= 20
        assert 0 < youtube._PICK_RETRY_S <= 15

    def test_a_healthy_page_pays_almost_nothing(self):
        """_await_content returns the moment content is present, so the wait
        must cost a warm device one UI read — not a fixed sleep."""
        calls = {"n": 0}

        class _BH:
            async def pause(self, a=0, b=0):
                calls["n"] += 1

        async def _fake_ui(serial, *queries):
            return {q: {"present": True} for q in queries}

        orig = youtube._ui
        youtube._ui = _fake_ui
        try:
            got = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
                youtube._await_content("S1", _BH(), ["id/cell"], timeout=10.0))
        finally:
            youtube._ui = orig
        assert got is True
        assert calls["n"] == 0, "content already present must not cost a single pause"


class TestCheckpointTellsTheTruth:
    def test_channel_watch_records_whether_the_list_loaded(self):
        """'channel page reached' followed by a failed pick is ambiguous. The
        trail has to distinguish a page that loaded from one that never did."""
        src = inspect.getsource(youtube.channel_watch)
        assert "list still empty" in src
        assert "listed = await _await_content" in src

    def test_the_await_result_is_no_longer_discarded(self):
        """A bare `await _await_content(...)` statement throws the answer away —
        which is exactly how the original race stayed invisible."""
        bare = [l.strip() for l in inspect.getsource(youtube.channel_watch).splitlines()
                if l.strip().startswith("await _await_content")]
        assert bare == [], f"return value dropped at: {bare}"
