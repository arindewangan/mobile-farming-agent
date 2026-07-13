"""
Google sign-in/sign-out UI automation: the resilience fixes made after a
real bulk sign-in run failed silently on real devices — a screen-wake/unlock
guard before every UI-driven flow (uiautomator dump returns nothing useful
against a sleeping display, which used to surface as a confusing "email
field never appeared"), the fragile fixed-sleep-then-check-once wait for the
account-type picker replaced with a proper poll, and diagnostic
`visible_texts` attached to every "unknown screen" failure so a stuck run is
debuggable from the API response alone.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import adb
import detect
import droidrun
import googleaccounts
import recipeui
import vision

pytestmark = pytest.mark.asyncio


def _ui_state(matches: dict, texts: list[str] | None = None):
    """Builds a fake recipeui.ui_state() response. `matches` maps a needle
    string to (x, y) or None (not present)."""
    async def fake(serial, queries=None):
        out = {}
        for q in (queries or list(matches.keys())):
            xy = matches.get(q)
            out[q] = {"present": xy is not None, "x": xy[0] if xy else None, "y": xy[1] if xy else None}
        return {"ok": True, "matches": out, "texts": texts or []}
    return fake


def _stub_dump_diagnostic(monkeypatch, result=None):
    """_diagnostic() also calls recipeui.ui_dump_diagnostic() — stub it so
    failure-path tests don't need a real uiautomator dump."""
    monkeypatch.setattr(recipeui, "ui_dump_diagnostic", AsyncMock(
        return_value=result or {"ok": True, "node_count": 0, "text_matches": [], "clickable_nodes": []}))


@pytest.fixture(autouse=True)
def stub_adb(monkeypatch):
    """Every test gets adb.shell/tap/swipe/keyevent/input_text as no-op-success
    stubs, with dumpsys power reporting "awake" by default so _ensure_ready's
    wake path isn't exercised unless a test explicitly overrides it, and
    dumpsys activity reporting no known foreground app (empty stdout) so
    _assert_in_flow's off-flow guard doesn't trip by default. Also stubs
    humanize.human_type to skip real per-character typing delays, and every
    tier-2/tier-3 dependency (adb.screencap_full_jpeg, vision.ocr_find,
    detect.classify_screen, adb.logcat_dump, droidrun.run_task) to fail/no-op
    by default so existing flow tests never make a real screencap/Ollama/
    Portal call — tests that specifically exercise the recovery tiers
    override these."""
    async def fake_shell(serial, cmd):
        if cmd == "dumpsys power":
            return {"ok": True, "rc": 0, "stdout": "mWakefulness=Awake", "stderr": ""}
        if cmd == "dumpsys account":
            return {"ok": True, "rc": 0, "stdout": "", "stderr": ""}
        return {"ok": True, "rc": 0, "stdout": "", "stderr": ""}
    monkeypatch.setattr(detect, "classify_screen", AsyncMock(return_value={"ok": False, "error": "no ollama in tests"}))
    monkeypatch.setattr(droidrun, "run_task", AsyncMock(side_effect=RuntimeError("no portal in tests")))
    monkeypatch.setattr(vision, "ocr_find", AsyncMock(return_value=None))
    monkeypatch.setattr(adb, "shell", AsyncMock(side_effect=fake_shell))
    monkeypatch.setattr(adb, "tap", AsyncMock(return_value={"ok": True}))
    monkeypatch.setattr(adb, "swipe", AsyncMock(return_value={"ok": True}))
    monkeypatch.setattr(adb, "keyevent", AsyncMock(return_value={"ok": True}))
    monkeypatch.setattr(adb, "input_text", AsyncMock(return_value={"ok": True}))
    monkeypatch.setattr(adb, "screencap_full_jpeg", AsyncMock(return_value=b"fake-jpeg-bytes"))
    monkeypatch.setattr(adb, "logcat_dump", AsyncMock(return_value={"ok": True, "text": ""}))
    monkeypatch.setattr(googleaccounts.humanize, "human_type", AsyncMock(return_value={"ok": True, "typed": 0}))


# ---- dismiss-prompt handling ------------------------------------------------
async def test_dismiss_known_popups_scrolls_instead_of_tapping_a_disabled_button(monkeypatch):
    """Regression test for a real failure found via ui_dump diagnostics on
    physical hardware: the intro screen's "NEXT" button is a real native
    widget with sane bounds but enabled=false — a tap on it is a silent
    no-op. _dismiss_known_popups must scroll instead of tapping when the
    matched node isn't yet interactive."""
    async def fake_ui_state(serial, queries=None):
        matches = {q: {"present": q == "Next", "x": 888, "y": 1668, "text": "NEXT",
                        "enabled": "false" if q == "Next" else "true"} for q in (queries or [])}
        return {"ok": True, "matches": matches, "texts": []}
    monkeypatch.setattr(recipeui, "ui_state", fake_ui_state)

    dismissed = await googleaccounts._dismiss_known_popups("S1")
    assert dismissed is True
    adb.tap.assert_not_awaited()  # a tap on a disabled button is a no-op — must not tap
    adb.swipe.assert_awaited()  # scrolls the body to enable it instead


async def test_dismiss_known_popups_prefers_close_over_a_disabled_next(monkeypatch):
    """Regression test for the actual real-hardware failure, root-caused via
    live ui_dump diagnostics: the intro screen shows both a "NEXT" button
    (present, sane bounds, enabled=false — never becomes enabled no matter
    how long you wait or scroll) and a "Close" button (enabled=true) that's
    the one that actually dismisses the screen. _dismiss_known_popups must
    tap "Close", not get stuck scrolling for "Next"."""
    async def fake_ui_state(serial, queries=None):
        matches = {}
        for q in (queries or []):
            if q == "Close":
                matches[q] = {"present": True, "x": 864, "y": 1189, "text": "Close", "enabled": "true"}
            elif q == "Next":
                matches[q] = {"present": True, "x": 888, "y": 1668, "text": "NEXT", "enabled": "false"}
            else:
                matches[q] = {"present": False, "x": None, "y": None, "text": "", "enabled": "true"}
        return {"ok": True, "matches": matches, "texts": []}
    monkeypatch.setattr(recipeui, "ui_state", fake_ui_state)

    dismissed = await googleaccounts._dismiss_known_popups("S1")
    assert dismissed is True
    adb.tap.assert_awaited_with("S1", 864, 1189)  # taps Close, never touches the disabled Next
    adb.swipe.assert_not_awaited()


async def test_dismiss_known_popups_taps_normally_when_enabled(monkeypatch):
    async def fake_ui_state(serial, queries=None):
        matches = {q: {"present": q == "OK", "x": 100, "y": 200, "text": "OK", "enabled": "true"}
                   for q in (queries or [])}
        return {"ok": True, "matches": matches, "texts": []}
    monkeypatch.setattr(recipeui, "ui_state", fake_ui_state)

    dismissed = await googleaccounts._dismiss_known_popups("S1")
    assert dismissed is True
    adb.tap.assert_awaited_with("S1", 100, 200)


async def test_dismiss_known_popups_taps_through_a_stale_expired_session_screen(monkeypatch):
    """Regression test for a real screen found live: a leftover session from
    a previous partial attempt shows "You're not signed in — Your session
    ended because there was no activity. Try signing in again." with a
    "TRY AGAIN" button — must be recognized as just another interstitial."""
    async def fake_ui_state(serial, queries=None):
        matches = {q: {"present": q == "Try again", "x": 865, "y": 1668, "text": "TRY AGAIN", "enabled": "true"}
                   for q in (queries or [])}
        return {"ok": True, "matches": matches, "texts": []}
    monkeypatch.setattr(recipeui, "ui_state", fake_ui_state)

    dismissed = await googleaccounts._dismiss_known_popups("S1")
    assert dismissed is True
    adb.tap.assert_awaited_with("S1", 865, 1668)


async def test_wait_for_any_taps_through_the_intro_consent_screen(monkeypatch):
    """Regression test for a real failure found on physical hardware: an
    intro splash ("Your device works better with a Google Account: ...")
    shows up before the picker/email field on some builds, with a plain
    "Next" advance button — _wait_for_any must dismiss it and keep polling
    instead of timing out waiting for the email field directly."""
    state = {"tapped": False}

    async def fake_ui_state(serial, queries=None):
        queries = queries or []
        hits = {"Email or phone": (100, 200)} if state["tapped"] else {"Next": (500, 1800)}
        matches = {q: ({"present": True, "x": hits[q][0], "y": hits[q][1]} if q in hits
                        else {"present": False, "x": None, "y": None}) for q in queries}
        return {"ok": True, "matches": matches, "texts": []}
    monkeypatch.setattr(recipeui, "ui_state", fake_ui_state)

    async def fake_tap(serial, x, y):
        if (x, y) == (500, 1800):
            state["tapped"] = True
        return {"ok": True}
    monkeypatch.setattr(adb, "tap", AsyncMock(side_effect=fake_tap))

    hit = await googleaccounts._wait_for_any("S1", googleaccounts._EMAIL_FIELD, 5)
    assert hit is not None
    assert hit["needle"] == "Email or phone"
    assert state["tapped"] is True


async def test_email_field_wins_over_the_account_picker_when_both_present(monkeypatch):
    """Regression test for a real failure found via live diagnostics: the
    real sign-in form (a WebView hosting Google's actual accounts.google.com
    page, confirmed live — its email input's resource-id is "identifierId")
    also shows a "Google" logo, which matches the account-type-picker
    needle. Checking the picker needle first made the automation latch onto
    the harmless logo and never see the email field that was on screen the
    whole time. sign_in() now checks _EMAIL_FIELD first so it wins whenever
    both are simultaneously present."""
    async def fake_ui_state(serial, queries=None):
        matches = {}
        for q in (queries or []):
            if q == "identifierId":
                matches[q] = {"present": True, "x": 500, "y": 900, "text": "", "enabled": "true"}
            elif q == "Google":
                matches[q] = {"present": True, "x": 540, "y": 276, "text": "Google", "enabled": "true"}
            else:
                matches[q] = {"present": False, "x": None, "y": None, "text": "", "enabled": "true"}
        return {"ok": True, "matches": matches, "texts": []}
    monkeypatch.setattr(recipeui, "ui_state", fake_ui_state)

    hit = await googleaccounts._wait_for_any(
        "S1", googleaccounts._EMAIL_FIELD + googleaccounts._ACCOUNT_TYPE_PICKER, 5)
    assert hit is not None
    assert hit["needle"] == "identifierId"


# ---- _dismiss_keyboard -------------------------------------------------------
async def test_dismiss_keyboard_sends_back_when_ime_is_shown(monkeypatch):
    """Regression test for a real failure found live: typing into the
    sign-in form's email field opened the software keyboard, which visually
    covered the form's own "Next" button — a tap there landed on a keyboard
    key instead (confirmed: it typed a stray "." rather than advancing)."""
    async def shell(serial, cmd):
        if cmd == "dumpsys input_method":
            return {"ok": True, "rc": 0, "stdout": "mShowRequested=true mInputShown=true", "stderr": ""}
        return {"ok": True, "rc": 0, "stdout": "", "stderr": ""}
    monkeypatch.setattr(adb, "shell", AsyncMock(side_effect=shell))

    await googleaccounts._dismiss_keyboard("S1")
    adb.keyevent.assert_awaited_with("S1", "KEYCODE_BACK")


async def test_dismiss_keyboard_is_a_noop_when_ime_already_hidden():
    await googleaccounts._dismiss_keyboard("S1")  # default stub: mInputShown absent
    adb.keyevent.assert_not_awaited()


# ---- _ensure_ready ----------------------------------------------------------
async def test_ensure_ready_wakes_a_sleeping_screen(monkeypatch):
    async def asleep_shell(serial, cmd):
        if cmd == "dumpsys power":
            return {"ok": True, "rc": 0, "stdout": "mWakefulness=Asleep", "stderr": ""}
        return {"ok": True, "rc": 0, "stdout": "", "stderr": ""}
    monkeypatch.setattr(adb, "shell", AsyncMock(side_effect=asleep_shell))

    await googleaccounts._ensure_ready("S1")
    adb.keyevent.assert_awaited_with("S1", "KEYCODE_WAKEUP")
    adb.swipe.assert_awaited()  # always swipes past a possible lock screen


async def test_ensure_ready_skips_wakeup_keyevent_when_already_awake():
    await googleaccounts._ensure_ready("S1")
    adb.keyevent.assert_not_awaited()
    adb.swipe.assert_awaited()  # still swipes — harmless no-op if already unlocked


# ---- sign_in ------------------------------------------------------------------
async def test_sign_in_is_a_noop_when_already_signed_in(monkeypatch):
    async def shell(serial, cmd):
        if cmd == "dumpsys account":
            return {"ok": True, "rc": 0, "stdout": "Account {name=a@example.com, type=com.google}", "stderr": ""}
        return {"ok": True, "rc": 0, "stdout": "", "stderr": ""}
    monkeypatch.setattr(adb, "shell", AsyncMock(side_effect=shell))

    result = await googleaccounts.sign_in("S1", "a@example.com", "pw")
    assert result == {"ok": True, "email": "a@example.com", "detail": "already signed in — no-op"}
    adb.keyevent.assert_not_awaited()  # never even entered the UI flow


async def test_sign_in_skips_the_account_type_picker_when_absent(monkeypatch):
    monkeypatch.setattr(recipeui, "ui_state", _ui_state({"Email or phone": (100, 200)}))
    monkeypatch.setattr(googleaccounts, "_wait_for_any", AsyncMock(side_effect=[
        {"needle": "Email or phone", "x": 100, "y": 200},  # first wait: picker+email → straight to email
        None,  # Next button not found — fine, optional
        None,  # password/block wait — force an early "unknown" exit for this test
    ]))

    result = await googleaccounts.sign_in("S1", "a@example.com", "pw")
    assert result["state"] == "unknown"
    assert "visible_texts" in result


async def test_sign_in_taps_the_picker_when_present(monkeypatch):
    calls = []

    async def fake_wait(serial, needles, timeout):
        calls.append(list(needles))
        if len(calls) == 1:
            return {"needle": "Google", "x": 10, "y": 20}  # picker shown first
        if len(calls) == 2:
            return {"needle": "Email or phone", "x": 100, "y": 200}
        return None  # everything after: time out to keep the test short

    monkeypatch.setattr(googleaccounts, "_wait_for_any", fake_wait)
    result = await googleaccounts.sign_in("S1", "a@example.com", "pw")
    # first call included the picker needles, second call was the dedicated
    # post-tap email-field wait
    assert "Google" in calls[0]
    assert calls[1] == googleaccounts._EMAIL_FIELD
    assert result["ok"] is False  # times out later in the (shortened) test flow


async def test_sign_in_reports_unknown_state_with_visible_texts_when_email_field_never_appears(monkeypatch):
    monkeypatch.setattr(googleaccounts, "_wait_for_any", AsyncMock(return_value=None))
    monkeypatch.setattr(recipeui, "ui_state", _ui_state({}, texts=["Some unexpected screen", "Try again"]))
    dump = {"ok": True, "node_count": 3, "text_matches": [], "clickable_nodes": [
        {"text": "", "resource-id": "", "content-desc": "", "class": "android.webkit.WebView",
         "clickable": "false", "enabled": "true", "bounds": "[0,0][1080,2000]", "package": "com.google", "center": [540, 1000]},
    ]}
    _stub_dump_diagnostic(monkeypatch, dump)

    result = await googleaccounts.sign_in("S1", "a@example.com", "pw")
    assert result["ok"] is False
    assert result["quarantine"] is False
    assert result["state"] == "unknown"
    assert result["reason"] == "email field never appeared"
    assert result["visible_texts"] == ["Some unexpected screen", "Try again"]
    assert result["ui_dump"] == dump
    assert [t["tier"] for t in result["recovery_trail"]] == ["ocr_vision", "droidrun"]


async def test_sign_in_detects_a_hard_block_between_email_and_password(monkeypatch):
    calls = []

    async def fake_wait(serial, needles, timeout):
        calls.append(needles)
        if len(calls) == 1:
            return {"needle": "Email or phone", "x": 100, "y": 200}
        if len(calls) == 2:
            return None  # no "Next" button found — fine
        if len(calls) == 3:
            return {"needle": "Wrong password", "x": 0, "y": 0}
        raise AssertionError("should have returned before a 4th wait")

    monkeypatch.setattr(googleaccounts, "_wait_for_any", fake_wait)
    result = await googleaccounts.sign_in("S1", "a@example.com", "wrongpw")
    assert result == {"ok": False, "quarantine": True, "state": "blocked", "reason": "Wrong password"}


async def test_sign_in_detects_the_embedded_browser_security_block_after_password(monkeypatch):
    """Regression test for a real failure found live: the automation reached
    the actual password screen with the correct email pre-filled, but
    timed out afterward instead of finishing or reporting a block — none of
    the previous _BLOCK_PHRASES matched. The most likely explanation is
    Google's dedicated anti-automation block for embedded-WebView sign-in
    flows ("This browser or app may not be secure"), which wasn't in the
    list at all."""
    seq = [
        {"needle": "Email or phone", "x": 100, "y": 200},        # picker/email wait
        None,                                                     # Next after email
        {"needle": "password", "x": 100, "y": 300},               # password/block wait
        None,                                                     # Next after password
    ]

    async def fake_wait(serial, needles, timeout):
        return seq.pop(0) if seq else None

    monkeypatch.setattr(googleaccounts, "_wait_for_any", fake_wait)
    monkeypatch.setattr(googleaccounts, "_find", AsyncMock(
        return_value={"needle": "This browser or app may not be secure", "x": 0, "y": 0}))
    monkeypatch.setattr(googleaccounts, "_dismiss_known_popups", AsyncMock(return_value=False))

    result = await googleaccounts.sign_in("S1", "a@example.com", "pw")
    assert result == {"ok": False, "quarantine": True, "state": "blocked",
                       "reason": "This browser or app may not be secure"}


async def test_sign_in_full_success_path(monkeypatch):
    seq = [
        {"needle": "Email or phone", "x": 100, "y": 200},  # picker/email wait
        None,                                               # Next after email
        {"needle": "Enter your password", "x": 100, "y": 300},  # password/block wait
        None,                                               # Next after password
    ]

    async def fake_wait(serial, needles, timeout):
        return seq.pop(0) if seq else None

    monkeypatch.setattr(googleaccounts, "_wait_for_any", fake_wait)
    monkeypatch.setattr(googleaccounts, "_find", AsyncMock(return_value=None))  # no block phrase in the post-password loop
    monkeypatch.setattr(googleaccounts, "_dismiss_known_popups", AsyncMock(return_value=False))

    accounts_state = {"seen": False}

    async def shell(serial, cmd):
        if cmd == "dumpsys account":
            if accounts_state["seen"]:
                return {"ok": True, "rc": 0, "stdout": "Account {name=a@example.com, type=com.google}", "stderr": ""}
            accounts_state["seen"] = True  # not signed in yet on the pre-flight check, signed in after
            return {"ok": True, "rc": 0, "stdout": "", "stderr": ""}
        return {"ok": True, "rc": 0, "stdout": "", "stderr": ""}
    monkeypatch.setattr(adb, "shell", AsyncMock(side_effect=shell))

    result = await googleaccounts.sign_in("S1", "a@example.com", "pw")
    assert result == {"ok": True, "email": "a@example.com", "detail": "signed in"}


# ---- off-flow guard (_assert_in_flow / _OffFlowError) -----------------------
async def test_sign_in_reports_cleanly_when_the_flow_leaves_google_settings(monkeypatch):
    """Regression test for a real failure found live twice: once the
    deterministic wait stops finding any expected screen, blindly tapping
    whatever matches a dismiss-prompt string can wander into an unrelated
    app entirely — confirmed live it ended up in Samsung's Finder/search
    overlay. _assert_in_flow must stop the loop and report cleanly instead
    of continuing to tap around on the wrong app."""
    async def shell(serial, cmd):
        if cmd == "dumpsys account":
            return {"ok": True, "rc": 0, "stdout": "", "stderr": ""}
        if "mResumedActivity" in cmd:
            return {"ok": True, "rc": 0, "stdout": "    mResumedActivity: ActivityRecord{a1 u0 com.samsung.android.app.galaxyfinder/.GalaxyFinderActivity t4}", "stderr": ""}
        return {"ok": True, "rc": 0, "stdout": "", "stderr": ""}
    monkeypatch.setattr(adb, "shell", AsyncMock(side_effect=shell))
    monkeypatch.setattr(recipeui, "ui_state", _ui_state({}))  # nothing ever matches deterministically

    result = await googleaccounts.sign_in("S1", "a@example.com", "pw")
    assert result["ok"] is False
    assert "com.samsung.android.app.galaxyfinder" in result["reason"]
    assert "visible_texts" in result  # still gets the usual diagnostic attached


async def test_assert_in_flow_is_a_noop_inside_expected_packages(monkeypatch):
    async def shell(serial, cmd):
        if "mResumedActivity" in cmd:
            return {"ok": True, "rc": 0, "stdout": "    mResumedActivity: ActivityRecord{a1 u0 com.google.android.gms/.auth.uiflows.minutemaid.MinuteMaidActivity t3}", "stderr": ""}
        return {"ok": True, "rc": 0, "stdout": "", "stderr": ""}
    monkeypatch.setattr(adb, "shell", AsyncMock(side_effect=shell))
    await googleaccounts._assert_in_flow("S1")  # must not raise


# ---- tier 1: _wait_for_any is purely deterministic ---------------------------
async def test_wait_for_any_never_touches_ocr_or_vision(monkeypatch):
    """Tier 1 must stay cheap: no screenshots, no models. A custom-rendered
    element with no useful accessibility node is tier 2/3's job now, not
    something tier 1 quietly falls back to on its own."""
    monkeypatch.setattr(recipeui, "ui_state", _ui_state({}))
    hit = await googleaccounts._wait_for_any("S1", ["Email or phone"], 1)
    assert hit is None
    vision.ocr_find.assert_not_awaited()
    detect.classify_screen.assert_not_awaited()
    adb.screencap_full_jpeg.assert_not_awaited()


# ---- tier 2: OCR + vision-classify on one shared screenshot -------------------
async def test_tier2_finds_the_needle_via_ocr_without_calling_vision(monkeypatch):
    """OCR is checked first — cheap, no LLM. If it finds the needle, the
    vision classifier (the more expensive call) never needs to run."""
    monkeypatch.setattr(vision, "ocr_find", AsyncMock(
        return_value={"text": "Email or phone", "x": 111, "y": 222, "conf": 0.91}))

    result = await googleaccounts._tier2_ocr_and_vision("S1", ["Email or phone"])
    assert result == {"hit": {"needle": "Email or phone", "present": True, "x": 111, "y": 222,
                               "text": "Email or phone", "enabled": "true"}}
    detect.classify_screen.assert_not_awaited()


async def test_tier2_shares_one_screenshot_between_ocr_and_vision(monkeypatch):
    """Both checks must run against the SAME captured screenshot — no
    duplicate screencap when OCR finds nothing and vision-classify is the
    next thing tried."""
    await googleaccounts._tier2_ocr_and_vision("S1", ["Email or phone"])
    adb.screencap_full_jpeg.assert_awaited_once()
    vision.ocr_find.assert_awaited_with("S1", "Email or phone", jpeg=b"fake-jpeg-bytes")
    detect.classify_screen.assert_awaited_with("S1", jpg=b"fake-jpeg-bytes")


async def test_tier2_returns_blocked_when_vision_classifier_flags_a_real_block(monkeypatch):
    """When OCR finds nothing either, the vision classifier gets one look
    before tier 2 gives up — catching a real block (CAPTCHA, a verification
    challenge) that free-text matching was never going to name in advance."""
    monkeypatch.setattr(detect, "classify_screen", AsyncMock(
        return_value={"ok": True, "state": "captcha", "reason": "puzzle challenge shown", "blocked": True}))

    result = await googleaccounts._tier2_ocr_and_vision("S1", googleaccounts._EMAIL_FIELD)
    assert result == {"blocked": True, "state": "captcha", "reason": "puzzle challenge shown"}


async def test_tier2_returns_vision_state_when_nothing_helps():
    result = await googleaccounts._tier2_ocr_and_vision("S1", googleaccounts._EMAIL_FIELD)
    assert result == {"vision_state": None, "vision_error": "no ollama in tests"}


async def test_tier2_reports_a_screencap_failure_explicitly(monkeypatch):
    monkeypatch.setattr(adb, "screencap_full_jpeg", AsyncMock(return_value=None))
    result = await googleaccounts._tier2_ocr_and_vision("S1", googleaccounts._EMAIL_FIELD)
    assert result == {"error": "screencap failed"}
    vision.ocr_find.assert_not_awaited()


async def test_tier2_uses_the_assigned_vision_model_when_given():
    """AI Model Registry assignment, resolved backend-side and passed down
    per-call, must override detect.py's own hardcoded default model."""
    await googleaccounts._tier2_ocr_and_vision(
        "S1", googleaccounts._EMAIL_FIELD,
        vision_model={"provider": "ollama", "model_tag": "gemma3:4b", "base_url": "http://10.0.0.5:11434"})
    call = detect.classify_screen.call_args
    assert call.kwargs.get("model") == "gemma3:4b"
    assert call.kwargs.get("base_url") == "http://10.0.0.5:11434"


async def test_tier2_falls_back_to_the_default_model_when_unassigned():
    """No registry assignment (vision_model=None) — an unconfigured farm
    must keep working exactly as before this registry existed."""
    await googleaccounts._tier2_ocr_and_vision("S1", googleaccounts._EMAIL_FIELD, vision_model=None)
    call = detect.classify_screen.call_args
    assert "model" not in call.kwargs  # detect.py's own DEFAULT_VISION_MODEL applies


# ---- tier 3: DroidRun + a large LLM, enriched with diagnostics ---------------
async def test_tier3_uses_the_configurable_recovery_model_by_default():
    assert googleaccounts._RECOVERY_MODEL == "qwen3"  # bigger than tier 2's lighter classifier model


async def test_tier3_uses_droidrun_to_unstick_then_rechecks_deterministically(monkeypatch):
    """The on-device DroidRun agent gets a plain-language "unstick"
    instruction — never asked to type credentials, only to dismiss/
    navigate — and the deterministic matcher gets one more try afterward."""
    monkeypatch.setattr(droidrun, "run_task", AsyncMock(return_value={"success": True, "message": "dismissed a dialog"}))
    monkeypatch.setattr(googleaccounts, "_wait_for_any", AsyncMock(
        return_value={"needle": "Email or phone", "x": 10, "y": 20}))

    result = await googleaccounts._tier3_droidrun(
        "S1", googleaccounts._EMAIL_FIELD, "dismiss and navigate", "email field never appeared", {})
    assert result["hit"] == {"needle": "Email or phone", "x": 10, "y": 20}
    assert result["model"] == "qwen3"
    assert result["droidrun_success"] is True
    droidrun.run_task.assert_awaited_once()
    call = droidrun.run_task.call_args
    assert call.args[0] == "S1"
    assert "dismiss and navigate" in call.args[1]
    assert "email field never appeared" in call.args[1]  # prior failure reason passed as context
    assert call.kwargs.get("vision") is True
    assert call.kwargs.get("model") == "qwen3"


async def test_tier3_includes_recent_logcat_warnings_in_the_goal(monkeypatch):
    monkeypatch.setattr(adb, "logcat_dump", AsyncMock(
        return_value={"ok": True, "text": "01-01 00:00:00.000 1 1 W SomeTag: something went sideways"}))
    monkeypatch.setattr(droidrun, "run_task", AsyncMock(return_value={"success": False, "message": ""}))

    await googleaccounts._tier3_droidrun("S1", googleaccounts._EMAIL_FIELD, "goal", "reason", {})
    call = droidrun.run_task.call_args
    assert "something went sideways" in call.args[1]


async def test_tier3_reports_an_error_explicitly_when_droidrun_is_unavailable():
    result = await googleaccounts._tier3_droidrun("S1", googleaccounts._EMAIL_FIELD, "goal", "reason", {})
    assert result["error"] == "no portal in tests"
    assert result["model"] == "qwen3"
    assert "logcat_tail" in result


async def test_tier3_uses_the_assigned_recovery_model_when_given(monkeypatch):
    """AI Model Registry assignment overrides the env-var default — this is
    the tier that has to plan/act, not just classify, so it also accepts
    cloud providers (unlike tier 2's Ollama-only vision classifier)."""
    monkeypatch.setattr(droidrun, "run_task", AsyncMock(return_value={"success": True, "message": ""}))

    result = await googleaccounts._tier3_droidrun(
        "S1", googleaccounts._EMAIL_FIELD, "goal", "reason", {},
        recovery_model={"provider": "openai", "model_tag": "gpt-4o-mini", "base_url": None})
    assert result["model"] == "gpt-4o-mini"
    call = droidrun.run_task.call_args
    assert call.kwargs.get("provider") == "openai"
    assert call.kwargs.get("model") == "gpt-4o-mini"


# ---- _recover (the tier 2 → tier 3 orchestrator) ------------------------------
async def test_recover_succeeds_at_tier2_without_ever_reaching_droidrun(monkeypatch):
    monkeypatch.setattr(vision, "ocr_find", AsyncMock(
        return_value={"text": "Email or phone", "x": 10, "y": 20, "conf": 0.9}))

    result = await googleaccounts._recover("S1", googleaccounts._EMAIL_FIELD, "goal")
    assert result["ok"] is True
    assert result["hit"]["needle"] == "Email or phone"
    assert len(result["trail"]) == 1
    assert result["trail"][0]["tier"] == "ocr_vision"
    droidrun.run_task.assert_not_awaited()


async def test_recover_returns_blocked_from_tier2_without_escalating_to_tier3(monkeypatch):
    monkeypatch.setattr(detect, "classify_screen", AsyncMock(
        return_value={"ok": True, "state": "captcha", "reason": "puzzle challenge shown", "blocked": True}))

    result = await googleaccounts._recover("S1", googleaccounts._EMAIL_FIELD, "goal")
    assert result == {"ok": False, "blocked": True, "state": "captcha", "reason": "puzzle challenge shown",
                       "trail": result["trail"]}
    assert len(result["trail"]) == 1
    droidrun.run_task.assert_not_awaited()  # a confirmed block ends the escalation — no need for tier 3


async def test_recover_escalates_to_tier3_when_tier2_finds_nothing(monkeypatch):
    monkeypatch.setattr(droidrun, "run_task", AsyncMock(return_value={"success": True, "message": ""}))
    monkeypatch.setattr(googleaccounts, "_wait_for_any", AsyncMock(
        return_value={"needle": "Email or phone", "x": 10, "y": 20}))

    result = await googleaccounts._recover("S1", googleaccounts._EMAIL_FIELD, "dismiss and navigate")
    assert result["ok"] is True
    assert result["hit"] == {"needle": "Email or phone", "x": 10, "y": 20}
    assert [t["tier"] for t in result["trail"]] == ["ocr_vision", "droidrun"]


async def test_recover_returns_a_full_trail_when_nothing_helps():
    result = await googleaccounts._recover("S1", googleaccounts._EMAIL_FIELD, "goal")
    assert result["ok"] is False
    assert result["blocked"] is False
    assert [t["tier"] for t in result["trail"]] == ["ocr_vision", "droidrun"]
    assert result["trail"][1]["model"] == "qwen3"  # exactly the "so you know where it failed" trail


async def test_sign_in_threads_model_config_all_the_way_to_recover(monkeypatch):
    """The vision_model/recovery_model params sign_in() takes (resolved
    backend-side from the AI Model Registry, passed down per-call by
    agent.py's dispatcher) must actually reach _recover() — not get lost
    somewhere in _sign_in_flow's plumbing."""
    monkeypatch.setattr(googleaccounts, "_wait_for_any", AsyncMock(return_value=None))
    recover_mock = AsyncMock(return_value={"ok": False, "blocked": False, "trail": []})
    monkeypatch.setattr(googleaccounts, "_recover", recover_mock)

    vision_model = {"provider": "ollama", "model_tag": "gemma3:4b", "base_url": None}
    recovery_model = {"provider": "ollama", "model_tag": "qwen3", "base_url": None}
    await googleaccounts.sign_in("S1", "a@example.com", "pw", vision_model=vision_model, recovery_model=recovery_model)

    call = recover_mock.call_args
    assert call.args[-2] == vision_model
    assert call.args[-1] == recovery_model


async def test_sign_in_recovers_via_droidrun_when_email_field_first_missing(monkeypatch):
    """End-to-end: the first deterministic wait for the email field times
    out, tier 2 (OCR/vision) finds nothing, but tier 3's DroidRun dismisses
    whatever was in the way and the retried wait then finds the field —
    sign_in should proceed normally from there rather than failing outright."""
    calls = []

    async def fake_wait(serial, needles, timeout):
        calls.append(list(needles))
        if len(calls) == 1:
            return None  # initial wait: nothing found
        if len(calls) == 2:
            return {"needle": "identifierId", "x": 500, "y": 900}  # tier 3's retry succeeds
        return None  # everything after: time out to keep the test short

    monkeypatch.setattr(googleaccounts, "_wait_for_any", fake_wait)
    monkeypatch.setattr(droidrun, "run_task", AsyncMock(return_value={"success": True, "message": ""}))

    result = await googleaccounts.sign_in("S1", "a@example.com", "pw")
    assert result["state"] == "unknown"  # still fails later (shortened test flow), but got past the email step
    assert result["reason"] != "email field never appeared"


async def test_sign_in_attaches_the_recovery_trail_to_a_final_failure(monkeypatch):
    """Every plain-failure diagnostic should show exactly what tier 2/3 saw
    and did, not just that recovery "didn't work"."""
    monkeypatch.setattr(googleaccounts, "_wait_for_any", AsyncMock(return_value=None))

    result = await googleaccounts.sign_in("S1", "a@example.com", "pw")
    assert result["reason"] == "email field never appeared"
    assert "recovery_trail" in result
    assert [t["tier"] for t in result["recovery_trail"]] == ["ocr_vision", "droidrun"]


# ---- sign_out -----------------------------------------------------------------
async def test_sign_out_is_a_noop_when_nothing_signed_in():
    result = await googleaccounts.sign_out("S1")
    assert result == {"ok": True, "detail": "no Google account signed in — no-op"}
    adb.keyevent.assert_not_awaited()


async def test_sign_out_rejects_an_email_that_isnt_present(monkeypatch):
    async def shell(serial, cmd):
        if cmd == "dumpsys account":
            return {"ok": True, "rc": 0, "stdout": "Account {name=a@example.com, type=com.google}", "stderr": ""}
        return {"ok": True, "rc": 0, "stdout": "", "stderr": ""}
    monkeypatch.setattr(adb, "shell", AsyncMock(side_effect=shell))

    result = await googleaccounts.sign_out("S1", email="nope@example.com")
    assert result == {"ok": False, "error": "nope@example.com is not signed in on this device"}


async def test_sign_out_calls_ensure_ready_before_opening_the_ui(monkeypatch):
    async def shell(serial, cmd):
        if cmd == "dumpsys account":
            return {"ok": True, "rc": 0, "stdout": "Account {name=a@example.com, type=com.google}", "stderr": ""}
        if cmd == "dumpsys power":
            return {"ok": True, "rc": 0, "stdout": "mWakefulness=Asleep", "stderr": ""}
        return {"ok": True, "rc": 0, "stdout": "", "stderr": ""}
    monkeypatch.setattr(adb, "shell", AsyncMock(side_effect=shell))
    monkeypatch.setattr(googleaccounts, "_wait_for_any", AsyncMock(return_value=None))

    result = await googleaccounts.sign_out("S1")
    adb.keyevent.assert_awaited_with("S1", "KEYCODE_WAKEUP")  # _ensure_ready ran
    assert result["ok"] is False
    assert "visible_texts" in result
