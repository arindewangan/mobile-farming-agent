"""
Getting past ads, and not counting them as watch time.

WHAT WAS WRONG
The old path was a single `_skip_ad()` fired ~2-4s after playback started, then
a coin-flip re-check per segment tick. Three failures came out of that:

  * YouTube's skip control is not live for roughly the first five seconds, so
    the one early attempt reliably missed;
  * `start = _now()` was stamped BEFORE the ad was dealt with, so a reported
    "watched 25s" could be 15s of ad and 10s of video — the number the whole
    fleet is measured on was not measuring what it claimed;
  * one skip was treated as "ads are done", so the second ad of a pod became
    the thing being measured.

WHAT THE SELECTORS MUST BE
Resource-ids, not text. YouTube removed the countdown from the skip button on
mobile in October 2024, which silently broke every text-matching harness, and
text never worked in a non-English locale at all.
"""
from __future__ import annotations

import inspect

import youtube
import yt_config


class TestSelectors:
    def _sel(self, name):
        return yt_config.load_profile({})["selectors"][name]

    def test_the_skip_control_is_matched_by_resource_id(self):
        assert "id/skip_ad_button" in self._sel("skip")

    def test_the_ad_indicator_is_matched_by_resource_id(self):
        """A separate signal from the skip button: it answers "is an ad up",
        which is what unskippable ads and pods are detected with."""
        assert "id/ad_progress_text" in self._sel("ad_markers")

    def test_the_resource_id_is_tried_before_any_text(self):
        """Text is a fallback only. If a stale English string matched first on a
        localized device it would mask the id having stopped resolving."""
        skip = self._sel("skip")
        assert skip.index("id/skip_ad_button") == 0


class TestSkipIsOnlyTappedWhenTappable:
    def test_a_disabled_control_is_not_tapped(self):
        """"Present" is not "tappable" — the control exists before it goes live,
        and tapping it then does nothing while the caller believes it worked."""
        src = inspect.getsource(youtube._skip_ad)
        assert 'enabled' in src and 'false' in src

    def test_ocr_is_no_longer_used_for_skipping(self):
        """It cost a 1-3s OCR pass on the bridge per attempt and matched English
        text that YouTube stopped rendering."""
        assert "ocr_find" not in inspect.getsource(youtube._skip_ad)


class TestAdClearingLoop:
    def _src(self):
        return inspect.getsource(youtube._clear_ads)

    def test_it_polls_through_the_disabled_window(self):
        """~5s before the control goes live; one early attempt always missed."""
        assert youtube._SKIP_WAIT_S >= 5.0
        assert youtube._SKIP_POLL_S <= 1.0

    def test_an_unskippable_ad_is_waited_out_not_polled_forever(self):
        """Polling for a button that will never exist is the "spinning" failure.
        The ad ENDING is a signal that actually arrives."""
        src = self._src()
        assert "_AD_WAIT_S" in src
        assert youtube._AD_WAIT_S >= 30.0        # covers a 15-30s non-skippable

    def test_it_requires_consecutive_clear_polls(self):
        """Right after a skip there is a frame with neither ad nor player chrome.
        Believing that single read is how ad 2 of a pod became "content"."""
        assert youtube._AD_CLEAR_POLLS >= 2
        assert "clear >= _AD_CLEAR_POLLS" in self._src()

    def test_pods_are_bounded(self):
        src = self._src()
        assert "pods > _AD_POD_MAX" in src
        assert youtube._AD_POD_MAX >= 2

    def test_it_cannot_loop_forever(self):
        assert "while _now() - t0 <" in self._src()

    def test_a_double_tap_is_debounced(self):
        """A second tap lands on the video itself and pauses it — or opens the
        advertiser's page."""
        assert youtube._SKIP_DEBOUNCE_S >= 1.0
        assert "_SKIP_DEBOUNCE_S" in self._src()


class TestWatchTimeExcludesAds:
    def _src(self):
        return inspect.getsource(youtube._watch_video)

    def test_the_clock_starts_after_ads_are_cleared(self):
        """The headline correctness bug: reported watch time included ad time."""
        src = self._src()
        assert src.index("_clear_ads") < src.index("start = _now()"), \
            "the watch clock must not start until the pre-roll is gone"

    def test_a_midroll_extends_the_target(self):
        """Otherwise a mid-roll silently eats the content watch that was asked
        for, and the video is left before the requested duration was seen."""
        assert "target += _now() - t_ad" in self._src()

    def test_the_midroll_path_uses_the_pod_aware_clearer(self):
        src = self._src()
        assert "_clear_ads(serial, ctrl, bh)" in src


class TestPlaybackDetection:
    def test_buffering_counts_as_playing(self):
        """state 6 is BUFFERING. Treating it as stopped fails a video that is
        merely loading on a slow proxy."""
        src = inspect.getsource(youtube._is_playing)
        assert "state=6" in src and "state=3" in src

    def test_it_reads_the_media_session_not_the_screen(self):
        """Every UI signal proves the watch PAGE rendered, which is exactly the
        state a stalled video also reaches."""
        assert "media_session" in inspect.getsource(youtube._is_playing)


class TestNoBlindTapping:
    """A device that had FINISHED its videos was found sitting in the Clock app.

    Every engagement branch in the watch loop taps at fixed player coordinates.
    When the player is not on screen — video ended, ad took over, flow drifted —
    those taps land on whatever is there instead, and can leave the phone in an
    unrelated app. Which then looks, to anyone glancing at the fleet, exactly
    like the automation went haywire.
    """

    def _src(self):
        import inspect
        return inspect.getsource(youtube._watch_video)

    def test_the_loop_checks_playback_before_tapping(self):
        src = self._src()
        assert src.index("_is_playing(serial)") < src.index("roll = random.random()")

    def test_a_paused_player_is_waited_on_not_abandoned(self):
        """Paused or buffering is still the watch page — leaving would cut a
        video short for a stall that resolves itself."""
        assert "_on_watch_page(serial, bh)" in self._src()

    def test_a_lost_player_ends_the_loop_rather_than_tapping(self):
        src = self._src()
        assert "player_lost" in src

    def test_the_check_uses_the_media_session_not_the_screen(self):
        import inspect
        assert "media_session" in inspect.getsource(youtube._is_playing)

    def test_a_finished_video_is_detected_not_idled_through(self):
        """YouTube's end screen keeps Like/Share/Subscribe, so "still on the
        watch page" cannot distinguish a pause from a finished video. Measured:
        a 36s video with a 300s target idled for the full target and then blew
        the run's time budget."""
        src = self._src()
        assert "_STALL_TICKS" in src and "video_ended" in src

    def test_a_short_stall_is_tolerated(self):
        """A buffer or a pause clears in seconds; breaking on the first quiet
        tick would cut healthy videos short."""
        assert youtube._STALL_TICKS >= 2

    def test_the_stall_counter_resets_on_playback(self):
        assert "stalled = 0" in self._src()
