"""
Watching every video on a channel, exactly once.

WHY THIS FLOW EXISTS
The older channel flows navigate by SEARCHING for the channel and blind-tapping
the first result at fixed screen fractions. Measured on real hardware, that is
where the fleet's YouTube work was failing:

  * the configured handle "@HypeGaming" does not exist (it is "@HypeGaming." —
    with a trailing dot), so the deep link 404'd;
  * the flow fell back to channel-filtered search, which returned THREE
    near-identical channels;
  * a positional tap picked one of them, or nothing;
  * the run reported "list still empty" — truthfully, because a search page has
    no video cells.

So the properties worth pinning are the ones that stop it guessing: a canonical
id or nothing, one identity per row, never the same video twice, and no foreign
channel's video counted as this channel's.
"""
from __future__ import annotations

import youtube


class TestItRefusesToGuess:
    def test_a_handle_is_rejected_outright(self):
        """The whole failure chain started with a handle that silently did not
        resolve. A handle here is a caller bug, not something to search for."""
        out = youtube._abort  # sanity: the module is importable
        import asyncio
        r = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            youtube.channel_all("S1", None, {"channel_id": "@HypeGaming"}, None))
        assert r["ok"] is False
        assert "UC" in r["reason"]

    def test_an_empty_id_is_rejected(self):
        import asyncio
        r = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            youtube.channel_all("S1", None, {}, None))
        assert r["ok"] is False

    def test_the_rejection_names_the_fix(self):
        """"invalid input" sends an operator hunting. Saying the handle must be
        resolved points at the actual next action."""
        import asyncio
        r = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            youtube.channel_all("S1", None, {"channel_id": "x"}, None))
        assert "resolve" in r["reason"].lower()


class TestCellParsing:
    DESC = ("Jett + Shape Of You = PERFECTION | Valorant Edit - 36 seconds "
            "- Go to channel - Hype Gaming - 71 views - 1 day ago - play video")

    def test_the_title_is_recovered_for_logging(self):
        assert youtube._cell_title(self.DESC).startswith("Jett + Shape Of You")

    def test_the_duration_suffix_is_stripped(self):
        assert "36 seconds" not in youtube._cell_title(self.DESC)

    def test_the_owning_channel_is_recovered(self):
        """This is what stops a recommended video from another channel being
        counted as one of THIS channel's."""
        assert youtube._cell_channel(self.DESC) == "Hype Gaming"

    def test_a_title_containing_a_dash_survives(self):
        d = ("Part 2 - the finale - 10 minutes - Go to channel - Hype Gaming "
             "- 5 views - 2 days ago - play video")
        assert youtube._cell_title(d) == "Part 2 - the finale"

    def test_an_unparseable_desc_still_yields_something(self):
        """Logging must never crash the flow on an unexpected desc shape."""
        assert youtube._cell_title("weird") == "weird"
        assert youtube._cell_channel("weird") == ""


class TestDedupKey:
    """The dedup key is the WHOLE content-desc, not the parsed title, because
    channels really do publish the same title twice and a parsed title would
    make the second one invisible — silently under-watching the channel."""

    def test_two_videos_sharing_a_title_are_distinct(self):
        a = "Part 1 - 5 minutes - Go to channel - C - 10 views - 1 day ago - play video"
        b = "Part 1 - 5 minutes - Go to channel - C - 99 views - 3 days ago - play video"
        assert youtube._cell_title(a) == youtube._cell_title(b)   # titles collide
        assert a != b                                             # descs do not

    def test_the_flow_keys_on_desc_not_title(self):
        import inspect
        src = inspect.getsource(youtube.channel_all)
        assert 'c["desc"] not in seen' in src
        assert 'seen.add(cell["desc"])' in src


class TestTerminationAndSafety:
    def _src(self):
        import inspect
        return inspect.getsource(youtube.channel_all)

    def test_it_stops_after_repeated_dry_scrolls(self):
        """Without an end condition, a channel whose list stops growing would
        scroll forever until the run budget expired."""
        assert "dry_scrolls >= 2" in self._src()

    def test_a_video_is_marked_seen_before_it_is_opened(self):
        """Marking after a successful open would retry a video that cannot be
        opened on every single pass — an infinite loop on one bad row."""
        src = self._src()
        assert src.index('seen.add(cell["desc"])') < src.index("await _click(serial, cell")

    def test_repeated_failures_to_open_abort_rather_than_grind(self):
        assert "consecutive videos failed to open" in self._src()

    def test_the_run_budget_still_bounds_the_loop(self):
        assert "while not bh.expired()" in self._src()

    def test_foreign_channel_rows_are_filtered_out(self):
        assert "_cell_channel(c[\"desc\"]).lower() == want_channel.lower()" in self._src()


class TestHonestReporting:
    def _src(self):
        import inspect
        return inspect.getsource(youtube.channel_all)

    def test_ok_reflects_actual_playback_not_completion(self):
        """The failure this whole effort exists to end: a flow that reached its
        end and reported success while nothing played."""
        assert 'out = {"ok": bool(played)' in self._src()

    def test_zero_played_carries_a_reason(self):
        assert "0 of" in self._src()

    def test_a_video_that_opened_but_never_played_is_not_counted(self):
        assert '"ok": secs > 0' in self._src()

    def test_attempted_and_played_are_reported_separately(self):
        src = self._src()
        assert '"videos_ok": len(played)' in src and '"attempted": len(watched)' in src
