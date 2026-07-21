"""
The time budget has to be enforced, not merely offered.

THE FAILURE
`bh.expired()` is advisory — flows consult it at loop boundaries, so anything
that blocks INSIDE a single operation ignores it. Measured on the fleet: a
device configured with max_run_s=2400 was still inside its flow when the backend
gave up at 2640s ("Agent did not answer 'youtube'"). It had held one of only two
concurrency slots for forty-four minutes and starved the other eighteen devices
queued behind it — turning a 100-minute fleet run into a failure.

So the deadline is enforced at the dispatch boundary, the one point a flow
cannot skip, and a flow that blows it reports a timeout instead of the call
appearing to die.
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

import youtube


class TestEnforcement:
    def test_the_flow_is_run_under_a_timeout(self):
        """The whole bug: `await fn(...)` with nothing bounding it."""
        src = inspect.getsource(youtube.run)
        assert "asyncio.wait_for(" in src

    def test_a_flow_without_max_run_s_is_still_bounded(self):
        """An unbounded flow is how a device held a slot for 44 minutes."""
        assert youtube._HARD_CAP_DEFAULT_S > 0
        assert "or _HARD_CAP_DEFAULT_S" in inspect.getsource(youtube.run)

    def test_the_hard_cap_sits_under_the_backend_dispatch_timeout(self):
        """The backend waits budget + 240s. The agent must give up FIRST, so the
        operator sees "flow exceeded its budget" — which names the cause — and
        not "agent did not answer", which blames the connection."""
        assert youtube._HARD_CAP_MARGIN_S < 240

    def test_the_margin_allows_a_flow_to_finish_its_own_cleanup(self):
        """bh.expired() should normally end a flow first; the hard cap is the
        backstop, so it must not fire the instant the soft budget is reached."""
        assert youtube._HARD_CAP_MARGIN_S >= 30


@pytest.mark.asyncio
class TestTimeoutBehaviour:
    async def _run_hanging_flow(self, monkeypatch, payload):
        async def hangs(serial, ctrl, p, bh):
            await asyncio.sleep(999)
        monkeypatch.setitem(youtube._FLOWS, "hangs", hangs)
        monkeypatch.setattr(youtube, "_HARD_CAP_DEFAULT_S", 0.01)
        monkeypatch.setattr(youtube, "_HARD_CAP_MARGIN_S", 0.01)

        async def noop(*a, **kw):
            return {"ok": True}
        monkeypatch.setattr(youtube.adb, "force_stop", noop)
        return await youtube.run("S1", None, {"flow": "hangs", **payload})

    async def test_a_hanging_flow_returns_rather_than_blocking_forever(self, monkeypatch):
        r = await self._run_hanging_flow(monkeypatch, {})
        assert r["ok"] is False
        assert r["timed_out"] is True

    async def test_the_reason_names_the_budget_not_the_connection(self, monkeypatch):
        """"Agent did not answer" sends an operator to the network. "Exceeded
        its budget" sends them to the recipe, which is where the fix is."""
        r = await self._run_hanging_flow(monkeypatch, {})
        assert "budget" in r["reason"]
        assert "budget" in r["detail"]

    async def test_the_trail_survives_the_timeout(self, monkeypatch):
        """How far it got is the most useful thing about a hang."""
        youtube._cp_reset("S1")
        youtube._cp("S1", "youtube opened")
        r = await self._run_hanging_flow(monkeypatch, {})
        assert "youtube opened" in r["detail"]

    async def test_the_device_is_left_in_a_known_state(self, monkeypatch):
        """Abandoning a flow mid-way leaves whatever screen it was stuck on, and
        the next run inherits it — which is how phones ended up parked on stale
        channel pages."""
        stopped = []

        async def force_stop(serial, pkg):
            stopped.append(pkg)
            return {"ok": True}

        async def hangs(serial, ctrl, p, bh):
            await asyncio.sleep(999)
        monkeypatch.setitem(youtube._FLOWS, "hangs", hangs)
        monkeypatch.setattr(youtube, "_HARD_CAP_DEFAULT_S", 0.01)
        monkeypatch.setattr(youtube, "_HARD_CAP_MARGIN_S", 0.01)
        monkeypatch.setattr(youtube.adb, "force_stop", force_stop)
        await youtube.run("S1", None, {"flow": "hangs"})
        assert stopped == [youtube.PKG]

    async def test_a_cleanup_failure_does_not_swallow_the_report(self, monkeypatch):
        """Best-effort cleanup must never cost the operator the diagnosis."""
        async def boom(*a, **kw):
            raise RuntimeError("device gone")

        async def hangs(serial, ctrl, p, bh):
            await asyncio.sleep(999)
        monkeypatch.setitem(youtube._FLOWS, "hangs", hangs)
        monkeypatch.setattr(youtube, "_HARD_CAP_DEFAULT_S", 0.01)
        monkeypatch.setattr(youtube, "_HARD_CAP_MARGIN_S", 0.01)
        monkeypatch.setattr(youtube.adb, "force_stop", boom)
        r = await youtube.run("S1", None, {"flow": "hangs"})
        assert r["timed_out"] is True

    async def test_a_normal_flow_is_untouched(self, monkeypatch):
        async def quick(serial, ctrl, p, bh):
            return {"ok": True, "flow": "quick"}
        monkeypatch.setitem(youtube._FLOWS, "quick", quick)
        r = await youtube.run("S1", None, {"flow": "quick", "max_run_s": 60})
        assert r["ok"] is True and "timed_out" not in r
