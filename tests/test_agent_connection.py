"""
Agent._serve_connection / listen()'s duplicate-connection handling.

Root-caused live: a self-update push left the Edge Devices connection stuck
on "duplicate — this device is already connected elsewhere" for minutes
straight after the agent restarted, with no way to recover short of
restarting the process by hand. The old handler() unconditionally rejected
ANY second connection while self.ws was set — including a legitimate
reconnect from the very same registered backend connection, whose old TCP
session had gone half-open (no clean close ever arrived) rather than a
genuinely different second client. These tests cover the _ws_closed event
lifecycle _serve_connection now maintains, which the supersede logic in
listen()'s handler waits on before accepting a replacement connection.
"""
from __future__ import annotations

import asyncio

import pytest

import agent as agent_mod

pytestmark = pytest.mark.asyncio


class _FakeWS:
    """An async-iterable fake WebSocket: yields nothing, ends immediately
    (as if the peer closed right after registering) — enough to drive
    _serve_connection through one full lifecycle without any real I/O."""
    def __aiter__(self):
        return self

    async def __anext__(self):
        # Yield control at least once first, so tasks scheduled just before
        # this loop starts (poller/heartbeat/watchdog) actually get to run
        # their first line before _serve_connection's finally cancels them.
        await asyncio.sleep(0)
        raise StopAsyncIteration


def _make_agent(monkeypatch) -> agent_mod.Agent:
    a = agent_mod.Agent("ws://unused", "test-agent")
    monkeypatch.setattr(a, "refresh_devices", _async_noop)
    monkeypatch.setattr(a, "_visible_devices", lambda: {})
    monkeypatch.setattr(a, "_send", _async_noop_arg)
    return a


async def _async_noop(*a, **kw):
    return None


async def _async_noop_arg(*a, **kw):
    return None


async def test_ws_closed_is_set_before_any_connection_is_served(monkeypatch):
    a = _make_agent(monkeypatch)
    assert a._ws_closed.is_set()
    assert a.ws is None


async def test_serve_connection_clears_then_resets_ws_closed(monkeypatch):
    a = _make_agent(monkeypatch)
    seen_clear_during_serve = {}

    async def spy_poll_loop():
        # runs concurrently with the main _serve_connection body — long
        # enough to observe the mid-flight state before cancellation
        seen_clear_during_serve["cleared"] = not a._ws_closed.is_set()
        seen_clear_during_serve["ws_is_set"] = a.ws is not None
        await asyncio.sleep(10)

    monkeypatch.setattr(a, "_poll_loop", spy_poll_loop)
    monkeypatch.setattr(a, "_heartbeat_loop", spy_poll_loop)
    monkeypatch.setattr(a, "_tunnel_watchdog", spy_poll_loop)

    await a._serve_connection(_FakeWS())

    assert seen_clear_during_serve == {"cleared": True, "ws_is_set": True}
    assert a._ws_closed.is_set()  # cleared again once the connection ends
    assert a.ws is None
    assert a.current_peer is None


async def test_serve_connection_clears_ws_closed_even_on_error(monkeypatch):
    a = _make_agent(monkeypatch)

    async def failing_register():
        raise RuntimeError("boom")

    monkeypatch.setattr(a, "_register", failing_register)
    monkeypatch.setattr(a, "_poll_loop", _async_noop)
    monkeypatch.setattr(a, "_heartbeat_loop", _async_noop)
    monkeypatch.setattr(a, "_tunnel_watchdog", _async_noop)

    with pytest.raises(RuntimeError):
        await a._serve_connection(_FakeWS())

    assert a._ws_closed.is_set()  # still cleaned up despite the failure
    assert a.ws is None


# ---- _is_same_connection (the supersede decision) ----------------------------
async def test_is_same_connection_true_for_a_reconnect_from_the_same_registered_peer():
    a = agent_mod.Agent("ws://unused", "test-agent")
    a.current_peer = {"connection_id": 7, "connection_name": "Garage Farm Pi"}
    assert a._is_same_connection(7) is True


async def test_is_same_connection_false_for_a_genuinely_different_connection():
    a = agent_mod.Agent("ws://unused", "test-agent")
    a.current_peer = {"connection_id": 7, "connection_name": "Garage Farm Pi"}
    assert a._is_same_connection(9) is False


async def test_is_same_connection_false_when_nothing_is_currently_connected():
    a = agent_mod.Agent("ws://unused", "test-agent")
    assert a.current_peer is None
    assert a._is_same_connection(7) is False
