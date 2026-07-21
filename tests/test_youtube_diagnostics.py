"""
Failure reporting: does the message say enough to act on?

Every flow here already stops when it should. What it could not do was explain
itself. A real failure on the fleet read, in full:

    screen changed — manual fix required: unexpected screen (reached: youtube opened)

That single sentence is emitted for a sign-in wall, an empty feed, a crashed
app, and a slow proxy — and it names neither the flow that failed nor what was
on the screen at the time. Diagnosing it took an hour of reading source and
re-running the recipe, which is precisely the cost these tests exist to prevent.

Three defects, each pinned below:
  1. a `session` sub-flow wiped the checkpoint trail, so "5 videos played, the
     6th failed" reported identically to "nothing ever worked";
  2. `_ensure` computed the visible on-screen text and threw it away into a log
     the operator cannot read;
  3. the abort text never named the flow, which for a session is unanswerable
     from the recipe alone.
"""
from __future__ import annotations

import youtube


class TestSessionOwnsItsTrail:
    """The bug that made most recipes undiagnosable — `session` backs the
    majority of them, and every sub-flow reset wiped the history."""

    def setup_method(self):
        youtube._checkpoints.clear()
        youtube._cp_owned.clear()

    def test_a_sub_flow_cannot_wipe_a_session_trail(self):
        youtube._cp_reset("S1", own=True)
        youtube._cp("S1", "video 1/3 ok")
        youtube._cp_reset("S1")                  # what channel_watch does on entry
        youtube._cp("S1", "youtube opened")
        assert youtube._cp_trail("S1") == ["video 1/3 ok", "youtube opened"]

    def test_a_standalone_flow_still_starts_clean(self):
        """Ownership must not leak: a single-video run after a session has to
        begin from an empty trail, or it inherits the session's history."""
        youtube._cp_reset("S1", own=True)
        youtube._cp("S1", "video 1/3 ok")
        youtube._cp_release("S1")
        youtube._cp_reset("S1")
        assert youtube._cp_trail("S1") == []

    def test_ownership_is_per_device(self):
        youtube._cp_reset("S1", own=True)
        youtube._cp_reset("S2")
        youtube._cp("S2", "a")
        youtube._cp_reset("S2")                  # S2 is not owned — wipes
        assert youtube._cp_trail("S2") == []

    def test_release_is_safe_when_nothing_was_claimed(self):
        youtube._cp_release("never-seen")        # must not raise


class TestAbortExplainsItself:
    def setup_method(self):
        youtube._checkpoints.clear()
        youtube._cp_owned.clear()

    def test_the_flow_is_named(self):
        """For a `session` the failing flow is a SUB-flow, so it cannot be
        inferred from the recipe."""
        out = youtube._abort("channel_watch", {"status": "unknown", "reason": "unexpected screen"})
        assert "[channel_watch]" in out["detail"]

    def test_what_was_on_screen_survives_into_the_message(self):
        """The whole point: 'unexpected screen' plus 'Sign in to continue' is
        actionable; 'unexpected screen' alone is not."""
        out = youtube._abort("channel_watch", {
            "status": "unknown", "reason": "unexpected screen",
            "seen": ["Sign in to continue", "Use another account"]})
        assert "Sign in to continue" in out["detail"]

    def test_what_it_was_waiting_for_survives_too(self):
        out = youtube._abort("channel_watch", {
            "status": "unknown", "reason": "unexpected screen",
            "expected": ["watch_markers"]})
        assert "watch_markers" in out["detail"]

    def test_the_trail_is_still_included(self):
        youtube._cp("S1", "youtube opened")
        youtube._cp("S1", "channel page reached")
        out = youtube._abort("channel_watch", {"status": "unknown"}, "S1")
        assert "youtube opened → channel page reached" in out["detail"]

    def test_no_trail_says_so_explicitly(self):
        """An empty trail is a fact, not an absence — it means the app never
        came up, which is a different fix from a flow that got partway."""
        out = youtube._abort("channel_watch", {"status": "unknown"}, "S1")
        assert "no stage reached" in out["detail"]

    def test_a_blocked_account_still_reads_as_blocked(self):
        out = youtube._abort("channel_watch", {"status": "blocked", "reason": "sign-in wall"})
        assert out["quarantine"] is True
        assert "quarantined" in out["detail"]

    def test_screen_text_is_capped_so_the_message_stays_readable(self):
        out = youtube._abort("f", {"seen": [f"line{i}" for i in range(30)]})
        assert out["detail"].count("line") <= 6

    def test_empty_strings_are_not_rendered_as_separators(self):
        out = youtube._abort("f", {"seen": ["", "  ", "real"], "expected": [""]})
        assert "on screen: real" in out["detail"]
        assert "waiting for" not in out["detail"]
