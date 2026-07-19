"""
Waiting for a list to actually have rows in it.

channel_watch failed 3/4 immediately after a restore_defaults (which
force-stops YouTube) while the same recipe passed 3/4 on warm devices. The
cause was not a missing or blocking screen: the cold-start home screen renders
perfectly, nav bar and all. It just has no videos in it yet, because the feed
is still coming down through the proxy. The flow's fixed `pause(1.5, 3.0)`
expired first, the picker found nothing to tap, and the failure surfaced from
wherever the flow wandered to as a misleading "unexpected screen".

_await_content replaces sleeping-and-hoping with waiting for the markers the
next step actually needs. These tests pin the three behaviours that matter:
it returns as soon as content lands (so warm devices pay nothing), it gives up
rather than hanging, and — the one that keeps a slow network from becoming a
hard failure — a timeout is a soft signal, not an exception.
"""
from __future__ import annotations

import pytest

import recipeui
import youtube

pytestmark = pytest.mark.asyncio


class _Behavior:
    """Minimal stand-in for Behavior: _await_content only uses pause()."""
    def __init__(self):
        self.pauses = 0

    async def pause(self, lo, hi):
        self.pauses += 1


def _ui_returning(monkeypatch, sequence):
    """recipeui.ui_state yielding one canned result per call. Each entry is the
    list of probe strings present on that poll; [] means nothing yet."""
    calls = {"n": 0}

    async def fake_ui_state(serial, queries=None):
        present = sequence[min(calls["n"], len(sequence) - 1)]
        calls["n"] += 1
        return {"ok": True, "matches": {
            q: {"present": q in present, "x": 10, "y": 20} for q in (queries or [])}}

    monkeypatch.setattr(recipeui, "ui_state", fake_ui_state)
    return calls


async def test_returns_immediately_when_content_is_already_there(monkeypatch):
    """The warm-device path. A device whose feed is already loaded must not pay
    for this check beyond a single UI read — otherwise the fix for cold starts
    would slow down every run that never needed it."""
    calls = _ui_returning(monkeypatch, [[" views"]])
    bh = _Behavior()
    assert await youtube._await_content("S1", bh, [" views", " ago"], timeout=10) is True
    assert calls["n"] == 1
    assert bh.pauses == 0          # never slept


async def test_waits_for_content_that_arrives_late(monkeypatch):
    """The cold-start path: three empty polls, then the feed lands."""
    calls = _ui_returning(monkeypatch, [[], [], [], [" ago"]])
    assert await youtube._await_content("S1", _Behavior(), [" views", " ago"], timeout=10) is True
    assert calls["n"] == 4


async def test_gives_up_rather_than_hanging(monkeypatch):
    """A feed that never loads must end the wait, not block the run forever."""
    _ui_returning(monkeypatch, [[]])
    assert await youtube._await_content("S1", _Behavior(), [" views"], timeout=0.2) is False


async def test_a_timeout_is_a_soft_signal_not_an_exception(monkeypatch):
    """Callers proceed on False and let the flow's own recovery handle an empty
    list. If this raised instead, a slow network would become a hard failure —
    which is the opposite of the point."""
    _ui_returning(monkeypatch, [[]])
    result = await youtube._await_content("S1", _Behavior(), [" views"], timeout=0.2)
    assert result is False          # returned, did not raise


async def test_any_one_probe_is_enough(monkeypatch):
    """Probes are alternatives, not requirements — a Shorts-heavy shelf may show
    " watching" without " views". Demanding all of them would stall on a page
    that is in fact fully loaded."""
    _ui_returning(monkeypatch, [[" watching"]])
    assert await youtube._await_content(
        "S1", _Behavior(), [" views", " watching", " ago"], timeout=5) is True
