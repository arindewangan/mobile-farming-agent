"""
Google account sign-in / sign-out — no root, driven through the standard OS
"Add account" flow (Settings > Accounts), for credentials the operator
already owns (imported into the backend's account pool, not created here).

Follows the same block-detection shape as youtube.py's screen guard: a hard
block from Google's own sign-in flow (repeated wrong password, "couldn't
find your account", a verification challenge) returns {"quarantine": True,
...} so it flows through the existing auto-quarantine path in recipes.py
with no separate plumbing needed.
"""
from __future__ import annotations

import asyncio
import os
import re
import time

import adb
import humanize

ADD_ACCOUNT_INTENT = "android.settings.ADD_ACCOUNT_SETTINGS"
ACCOUNTS_INTENT = "android.settings.SYNC_SETTINGS"  # Settings > Accounts list

_EMAIL_FIELD = [
    "Email or phone", "email address",
    # The real sign-in form (confirmed live on Samsung Android 9 hardware)
    # renders as a WebView hosting Google's actual accounts.google.com sign-in
    # page — the email <input>'s accessibility node has empty text (no
    # typed value yet) and its placeholder isn't exposed as matchable text
    # either, but its DOM id survives as the node's resource-id, which
    # _matches() also checks. Far more stable than any locale-dependent
    # placeholder string.
    "identifierId",
]
_PASSWORD_FIELD = ["Enter your password", "password"]
_NEXT = ["Next"]
_DISMISS_PROMPTS = [
    # "Close" is listed first deliberately: on the "Your device works better
    # with a Google Account: ..." intro splash (confirmed on real Samsung
    # Android 9 hardware via live ui_dump diagnostics), the on-screen "NEXT"
    # button is present with sane bounds but enabled=false and *never*
    # becomes enabled — waiting, scrolling, none of it helps. "Close" is the
    # button that's actually meant to dismiss this screen, and tapping it
    # advances straight to the real sign-in form. Since _find() returns the
    # first needle in this list that's present on screen, "Close" wins over
    # "Next" whenever both are showing, so the disabled decoy is never the
    # one that gets tapped.
    "Close",
    "Not now", "Skip", "No thanks", "Remind me later", "I agree", "Accept", "Agree", "More", "OK",
    "Continue", "Got it", "Maybe later", "Turn on backup", "Done",
    # A stale/expired session left over from a previous attempt (confirmed
    # live: "You're not signed in — Your session ended because there was no
    # activity. Try signing in again.") re-shows the same add-account intent
    # from scratch — tapping through it is enough for the flow to recover on
    # its own the next time round.
    "Try again",
    # Kept for screens where "Next" genuinely is the correct/enabled advance
    # button (not the intro splash above).
    "Next",
]
_ACCOUNT_TYPE_PICKER = ["Google", "Personal (Google)"]

# Known hard-block phrasing on Google's own sign-in flow — a real ban/
# challenge signal, distinct from "the automation got lost" (unknown screen).
_BLOCK_PHRASES = [
    "Couldn't find your Google Account",
    "Wrong password",
    "couldn't sign you in",
    "verify it's you",
    "Verify your identity",
    "this device isn't registered",
    "unusual activity",
    "too many attempts",
    "This account has been disabled",
    # Google's dedicated anti-automation block for exactly this shape of
    # flow — an embedded WebView driving the sign-in — distinct from a
    # credential problem. Live testing reached the real password screen
    # with the correct email pre-filled, then the field went back to empty
    # with no phrase in this list matching, which is consistent with this
    # block firing after the password was submitted.
    "This browser or app may not be secure",
    "may not be secure",
]

_ACCOUNT_RE = re.compile(r"Account \{name=([^,]+@[^,]+), type=com\.google\}")

# Packages the sign-in/sign-out flow is expected to stay inside. Root-caused
# via live testing: once the polling loops (_wait_for_any, the post-password
# loop) stop finding any expected screen, they fall back to blindly tapping
# whatever matches a generic dismiss-prompt string — harmless while still on
# a Google/Settings screen, but if the flow has actually already fallen out
# to the home screen (a crash, an unhandled block screen, anything), that
# same blind tapping can wander into unrelated apps. Confirmed live twice:
# it ended up tapping into Samsung's Finder/search overlay. _foreground_package
# is the safety rail that stops this before it starts.
_IN_FLOW_PACKAGES = {
    "com.google.android.gms",   # intro splash + the WebView sign-in form
    "com.android.settings",     # Add account / Accounts list screens
    "com.google.android.gsf",   # GSF account manager, seen on some builds
}


class _OffFlowError(Exception):
    """Raised by the polling loops when the foreground app has left the
    expected Google/Settings flow — caught in sign_in()/sign_out() and
    turned into a clean diagnostic result instead of letting the loop keep
    blindly tapping whatever screen it's landed on."""
    def __init__(self, package: str | None):
        self.package = package
        super().__init__(f"left the sign-in flow — foreground app is now {package!r}")


async def _foreground_package(serial: str) -> str | None:
    """Best-effort current foreground package via the activity manager."""
    r = await adb.shell(serial, "dumpsys activity activities | grep mResumedActivity")
    m = re.search(r"\s([\w.]+)/", r.get("stdout", ""))
    return m.group(1) if m else None


async def _assert_in_flow(serial: str) -> None:
    pkg = await _foreground_package(serial)
    if pkg is not None and pkg not in _IN_FLOW_PACKAGES:
        raise _OffFlowError(pkg)


async def _find(serial: str, needles: list[str]) -> dict | None:
    import recipeui  # local import: keeps this module importable without the uiautomator dep at load time
    st = await recipeui.ui_state(serial, needles)
    for n in needles:
        m = st.get("matches", {}).get(n)
        if m and m.get("present") and m.get("x") is not None:
            return {"needle": n, **m}
    return None


async def _tap_match(serial: str, m: dict) -> None:
    await adb.tap(serial, m["x"], m["y"])
    await asyncio.sleep(0.4)


async def _dismiss_known_popups(serial: str) -> bool:
    hit = await _find(serial, _DISMISS_PROMPTS)
    if not hit:
        return False
    if hit.get("enabled") == "false":
        # Root-caused via ui_dump diagnostics on a real device: the "NEXT"
        # button on the "device works better with a Google Account" intro
        # screen is a genuine native android.widget.Button with sane
        # bounds — not a WebView bounds-accuracy issue as first suspected —
        # but Android leaves it disabled for a beat as an anti-misclick
        # guard, so a tap on it is a silent no-op. Scrolling the body is
        # what actually enables it; harmless elsewhere since this only runs
        # against a static popup and the caller re-polls right after.
        await adb.swipe(serial, 540, 1500, 540, 700, 300)
        await asyncio.sleep(0.6)
        return True
    await _tap_match(serial, hit)
    return True


async def _wait_for_any(serial: str, needles: list[str], timeout: float) -> dict | None:
    """Tier 1: pure deterministic accessibility-tree polling — cheap, no
    screenshots, no model. Returns None on timeout without trying anything
    else; the caller decides whether to escalate to tier 2/3 (see
    _recover())."""
    deadline = time.monotonic() + timeout
    first_iteration = True
    while time.monotonic() < deadline:
        hit = await _find(serial, needles)
        if hit:
            return hit
        # Skipped on the very first iteration: right after am start, the
        # foreground activity can still be mid-transition, and checking here
        # too eagerly risks a false trip on that transient flicker.
        if not first_iteration:
            await _assert_in_flow(serial)
        first_iteration = False
        await _dismiss_known_popups(serial)
        await asyncio.sleep(0.7)
    return None


async def _dismiss_keyboard(serial: str) -> None:
    """Closes the on-screen keyboard, if shown, before tapping "Next".
    Confirmed live on real hardware: typing into the email/password fields
    on the WebView-rendered sign-in form opens the software keyboard, which
    can visually cover the form's own floating "Next" button — a tap there
    then lands on a keyboard key instead (observed: it typed a stray "."
    into the field rather than advancing). A single BACK press closes just
    the IME on Android without triggering the underlying screen's back
    navigation, confirmed live to leave the typed text and screen state
    untouched."""
    r = await adb.shell(serial, "dumpsys input_method")
    if "mInputShown=true" in r.get("stdout", ""):
        await adb.keyevent(serial, "KEYCODE_BACK")
        await asyncio.sleep(0.4)


async def list_accounts(serial: str) -> dict:
    """Currently signed-in Google accounts via `dumpsys account` — no UI
    interaction needed, so cheap enough to use for idempotency checks."""
    r = await adb.shell(serial, "dumpsys account")
    emails = sorted(set(_ACCOUNT_RE.findall(r.get("stdout", ""))))
    return {"ok": True, "emails": emails}


async def _ensure_ready(serial: str) -> None:
    """Wakes the screen if it's asleep and swipes past a basic (no-PIN)
    lock screen. Every UI-driven flow in this module needs the screen
    actually visible — `uiautomator dump` (what `_find` runs on) returns an
    empty/stale tree against a sleeping display, which otherwise surfaces as
    a confusing "email field never appeared" failure with no clue why. A
    PIN/pattern/biometric lock still can't be passed (no credential for
    it) — this only clears the common no-lock or swipe-up case."""
    pw = (await adb.shell(serial, "dumpsys power")).get("stdout", "")
    awake = "mWakefulness=Awake" in pw or "Display Power: state=ON" in pw
    if not awake:
        await adb.keyevent(serial, "KEYCODE_WAKEUP")
        await asyncio.sleep(0.6)
    # A swipe-up is a harmless no-op on an already-unlocked home/app screen,
    # and dismisses a plain swipe-to-unlock lock screen when one is present.
    await adb.swipe(serial, 540, 1600, 540, 600, 250)
    await asyncio.sleep(0.5)


async def _visible_texts(serial: str) -> list[str]:
    """Best-effort snapshot of on-screen text — attached to "unknown screen"
    failures so a stuck automation run is diagnosable from the API response
    alone, without needing to reproduce it live against the real device."""
    import recipeui
    st = await recipeui.ui_state(serial)
    return st.get("texts", [])[:15]


async def _diagnostic(serial: str, needle: str = "next") -> dict:
    """Attached to every "unknown screen" failure alongside `visible_texts`.
    Plain text wasn't enough to explain the "Next" dismiss-prompt fix failing
    identically on a real device after being deployed and confirmed live —
    this adds a full node-level dump (bounds, class, clickable, resource-id)
    for anything matching `needle` plus every clickable element on screen, so
    the next failure shows whether the matched node is a real native widget
    or WebView-rendered content whose reported bounds don't actually
    correspond to a tappable spot, without needing another guess-and-push
    cycle."""
    import recipeui
    return {"visible_texts": await _visible_texts(serial),
            "ui_dump": await recipeui.ui_dump_diagnostic(serial, needle)}


# Tier 3's model — deliberately a bigger/more capable model than tier 2's
# classifier (detect.DEFAULT_VISION_MODEL), since this tier has to actually
# act on multi-step instructions, not just categorize a screen. Override via
# env to point at any other local Ollama tag or a cloud provider (paired
# with SIGNIN_RECOVERY_PROVIDER) — same convention droidrun.py itself uses
# for MOBILERUN_MODEL/MOBILERUN_PROVIDER.
_RECOVERY_MODEL = os.environ.get("SIGNIN_RECOVERY_MODEL", "qwen3")
_RECOVERY_PROVIDER = os.environ.get("SIGNIN_RECOVERY_PROVIDER", "Ollama")
_RECOVERY_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")

_RECOVERY_GOAL = (
    "You are in Android's 'Add a Google Account' flow. Dismiss whatever "
    "dialog, splash, or interstitial screen is currently showing (tap "
    "Close, Skip, Not now, or OK as appropriate) until you reach a Google "
    "sign-in form with an email or password field. Do not type anything "
    "into any field — only dismiss screens and navigate."
)


async def _recent_logcat(serial: str, lines: int = 12) -> list[str]:
    """Last ~N WARN/ERROR/FATAL logcat lines — filtered to "important things
    only" (not a raw dump) and attached to the tier-3 escalation, so the LLM
    (and whoever reads a failure diagnostic afterward) has whatever the OS
    itself logged around the failure, not just an isolated screenshot."""
    try:
        r = await adb.logcat_dump(serial, lines=300)
    except Exception:  # noqa: BLE001
        return []
    if not r.get("ok"):
        return []
    hits = [ln for ln in r.get("text", "").splitlines() if " W " in ln or " E " in ln or " F " in ln]
    return hits[-lines:]


async def _tier2_ocr_and_vision(serial: str, needles: list[str], vision_model: dict | None = None) -> dict:
    """Tier 2: RapidOCR (no LLM, bundled, CPU-only) + a vision-LLM
    classification, both against ONE shared screenshot (no duplicate
    screencap). OCR re-checks the needles against actual on-screen pixels,
    independent of the accessibility tree entirely — catches a
    custom-rendered element (e.g. inside a WebView) that exposes no useful
    a11y node at all. If OCR also finds nothing, the same screenshot goes to
    the vision model, used here purely as a classifier (real block vs.
    merely-unfamiliar layout) — cheap relative to tier 3, which has to act.
    `vision_model` — {"model_tag", "base_url", ...}, resolved by the backend
    from the AI Model Registry's "vision_classify" assignment and passed
    down per-call — overrides detect.py's own hardcoded default when given;
    an unconfigured farm (vision_model=None) keeps using that default, so
    this registry is opt-in, not a hard requirement. detect.classify_screen
    only speaks Ollama's chat API today, so only an Ollama-compatible
    endpoint (a different local Ollama, or another server matching that
    same API) can be assigned here — a cloud provider needing a different
    request/response shape (OpenAI, Anthropic, ...) is a natural follow-up,
    not yet wired in.
    Returns {"hit": {...}} | {"blocked": True, state, reason} |
    {"vision_state": ...} | {"error": ...} — always a dict, never None, so
    the caller can always log what this tier actually saw."""
    import vision
    jpeg = await adb.screencap_full_jpeg(serial, quality=85)
    if not jpeg:
        return {"error": "screencap failed"}

    for n in needles:
        hit = await vision.ocr_find(serial, n, jpeg=jpeg)
        if hit:
            return {"hit": {"needle": n, "present": True, "x": hit["x"], "y": hit["y"],
                             "text": hit["text"], "enabled": "true"}}

    try:
        import detect
        kwargs = {}
        if vision_model:
            kwargs["model"] = vision_model["model_tag"]
            if vision_model.get("base_url"):
                kwargs["base_url"] = vision_model["base_url"]
        cls = await detect.classify_screen(serial, jpg=jpeg, **kwargs)
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}
    if cls.get("ok") and cls.get("blocked"):
        return {"blocked": True, "state": cls.get("state"),
                "reason": cls.get("reason") or cls.get("state")}
    return {"vision_state": cls.get("state") if cls.get("ok") else None,
            "vision_error": cls.get("error")}


async def _tier3_droidrun(serial: str, expect: list[str], goal: str, prior_reason: str, tier2: dict,
                           recovery_model: dict | None = None) -> dict:
    """Tier 3, last resort: hands the on-device DroidRun/Portal agent a
    plain-language goal enriched with everything gathered so far — the
    deterministic tier's failure reason, tier 2's vision-classifier verdict,
    and recent device logcat warnings/errors — so the LLM is reasoning from
    real context instead of a bare screenshot alone. Deliberately never
    asked to type the email or password itself, only to dismiss/navigate —
    credentials never pass through an LLM; typing stays on the deterministic
    tier, which gets one more try afterward via _wait_for_any. A tight step/
    time budget on purpose: this is a best-effort last resort, not the
    primary path — the known real screens are all handled deterministically
    already. `recovery_model` — {"provider", "model_tag", "base_url", ...},
    resolved by the backend from the "recovery_agent" registry assignment —
    overrides the env-var defaults (_RECOVERY_MODEL etc.) when given; both
    Ollama tags and cloud providers work here since droidrun.run_task passes
    `provider` straight through to the mobilerun CLI, which is provider-
    agnostic (unlike tier 2's Ollama-only vision classifier above). Always
    returns a dict describing what happened, never None."""
    logcat = await _recent_logcat(serial)
    context = [f"Last automated attempt failed with: {prior_reason}"]
    if tier2.get("vision_state"):
        context.append(f"Vision classifier saw screen state: {tier2['vision_state']}")
    if logcat:
        context.append("Recent device warnings/errors:\n" + "\n".join(logcat))
    enriched_goal = goal + "\n\nContext:\n" + "\n".join(context)

    provider = (recovery_model or {}).get("provider") or _RECOVERY_PROVIDER
    model = (recovery_model or {}).get("model_tag") or _RECOVERY_MODEL
    base_url = (recovery_model or {}).get("base_url") or _RECOVERY_BASE_URL

    try:
        import droidrun
        result = await droidrun.run_task(
            serial, enriched_goal, provider=provider, model=model,
            base_url=base_url, vision=True, steps=5, timeout=40)
    except Exception as e:  # noqa: BLE001
        return {"error": str(e), "model": model, "logcat_tail": logcat}

    hit = await _wait_for_any(serial, expect, 12)
    return {"hit": hit, "model": model, "droidrun_success": result.get("success"),
            "droidrun_message": result.get("message"), "logcat_tail": logcat}


async def _recover(serial: str, expect: list[str], goal: str, prior_reason: str = "",
                    vision_model: dict | None = None, recovery_model: dict | None = None) -> dict:
    """Escalation orchestrator once tier 1 (_wait_for_any) gives up: tier 2
    (OCR + vision-classify) then tier 3 (DroidRun + a large LLM). Returns
    {"ok": True, "hit": ...} on recovery, {"ok": False, "blocked": True,
    state, reason} on a confirmed block, or {"ok": False, "blocked": False,
    "trail": [...]} — a per-tier diagnostic trail (what tier 2 saw, what
    tier 3 saw/did, model used, logcat tail) attached to every plain
    failure, so it's never a mystery which tier ran and what it found.
    `vision_model`/`recovery_model` are the AI Model Registry's current
    assignments (resolved by the backend, threaded through sign_in()) —
    None for either falls back to that tier's own built-in default."""
    trail = []

    t2 = await _tier2_ocr_and_vision(serial, expect, vision_model)
    trail.append({"tier": "ocr_vision", **t2})
    if t2.get("hit"):
        return {"ok": True, "hit": t2["hit"], "trail": trail}
    if t2.get("blocked"):
        return {"ok": False, "blocked": True, "state": t2["state"], "reason": t2["reason"], "trail": trail}

    t3 = await _tier3_droidrun(serial, expect, goal, prior_reason or "unrecognized screen", t2, recovery_model)
    trail.append({"tier": "droidrun", **{k: v for k, v in t3.items() if k != "hit"}})
    if t3.get("hit"):
        return {"ok": True, "hit": t3["hit"], "trail": trail}

    return {"ok": False, "blocked": False, "trail": trail}


async def sign_in(serial: str, email: str, password: str, timeout: float = 120.0,
                   vision_model: dict | None = None, recovery_model: dict | None = None) -> dict:
    """Adds a Google account through the standard "Add account" flow.

    `vision_model`/`recovery_model` — the AI Model Registry's current
    "vision_classify"/"recovery_agent" assignments (resolved backend-side
    and passed down per-call by main.py's device_google_signin), used by the
    tier 2/3 recovery escalation in _recover() when the deterministic tier 1
    can't find what it's looking for. Both default to None — an
    unconfigured farm keeps using each tier's own built-in default model.

    Returns {ok: True, email, detail} on success (including a no-op if the
    account is already signed in), or on a hard block {ok: False,
    quarantine: True, state: "blocked", reason}. An unrecognized/unexpected
    screen returns {ok: False, quarantine: False, ...} — a plain automation
    failure, not a ban signal."""
    already = await list_accounts(serial)
    if email.lower() in [e.lower() for e in already.get("emails", [])]:
        return {"ok": True, "email": email, "detail": "already signed in — no-op"}
    try:
        return await _sign_in_flow(serial, email, password, timeout, vision_model, recovery_model)
    except _OffFlowError as e:
        return {"ok": False, "quarantine": False, "state": "unknown",
                "reason": f"left the sign-in flow — foreground app is now {e.package!r}",
                **await _diagnostic(serial)}


async def _sign_in_flow(serial: str, email: str, password: str, timeout: float,
                         vision_model: dict | None = None, recovery_model: dict | None = None) -> dict:
    await _ensure_ready(serial)
    await adb.shell(serial, f"am start -a {ADD_ACCOUNT_INTENT}")

    # The account-type picker ("Google" / "Personal (Google)") only shows up
    # on some Android builds, and how long it takes to render varies — poll
    # for it alongside the email field itself (whichever appears first)
    # instead of a fixed sleep-then-check-once, which was fragile: on a slow
    # device the picker could still be mid-render at the 1.5s mark, get
    # missed, and then the email-field wait would time out for good since
    # that field doesn't exist on the picker screen.
    #
    # _EMAIL_FIELD is checked first deliberately: confirmed live on real
    # hardware, the actual sign-in form's own "Google" wordmark/logo also
    # matches the bare "Google" picker needle, so checking the picker first
    # made _find() latch onto the logo and tap it (a harmless no-op on that
    # non-clickable node) while the real, present email field was ignored —
    # the automation then waited out the whole email-field timeout on a
    # screen where the field was there all along. Since _find() returns
    # whichever needle it's given first that's present, putting the email
    # field first means it always wins when both are simultaneously present,
    # and the picker branch only fires on an actual picker screen where no
    # email field exists yet.
    first = await _wait_for_any(serial, _EMAIL_FIELD + _ACCOUNT_TYPE_PICKER, 15)
    if first and first["needle"] in _ACCOUNT_TYPE_PICKER:
        await _tap_match(serial, first)
        email_field = await _wait_for_any(serial, _EMAIL_FIELD, 20)
    else:
        email_field = first
    if not email_field:
        recovery = await _recover(serial, _EMAIL_FIELD, _RECOVERY_GOAL, "email field never appeared",
                                   vision_model, recovery_model)
        if recovery.get("blocked"):
            return {"ok": False, "quarantine": True, "state": recovery["state"], "reason": recovery["reason"]}
        email_field = recovery["hit"] if recovery.get("ok") else None
    if not email_field:
        return {"ok": False, "quarantine": False, "state": "unknown",
                "reason": "email field never appeared", "recovery_trail": recovery.get("trail", []),
                **await _diagnostic(serial)}
    await _tap_match(serial, email_field)
    await humanize.human_type(serial, email, typo_rate=0.0)
    await _dismiss_keyboard(serial)
    nxt = await _wait_for_any(serial, _NEXT, 8)
    if nxt:
        await _tap_match(serial, nxt)

    # Between email and password Google may show a block message directly
    # (unknown account, disabled) instead of ever reaching a password field.
    outcome = await _wait_for_any(serial, _PASSWORD_FIELD + _BLOCK_PHRASES, 30)
    if not outcome:
        recovery = await _recover(serial, _PASSWORD_FIELD + _BLOCK_PHRASES, _RECOVERY_GOAL,
                                   "no password field or block message appeared", vision_model, recovery_model)
        if recovery.get("blocked"):
            return {"ok": False, "quarantine": True, "state": recovery["state"], "reason": recovery["reason"]}
        outcome = recovery["hit"] if recovery.get("ok") else None
    if not outcome:
        return {"ok": False, "quarantine": False, "state": "unknown",
                "reason": "no password field or block message appeared", "recovery_trail": recovery.get("trail", []),
                **await _diagnostic(serial)}
    if outcome["needle"] in _BLOCK_PHRASES:
        return {"ok": False, "quarantine": True, "state": "blocked", "reason": outcome["needle"]}

    await _tap_match(serial, outcome)
    await humanize.human_type(serial, password, typo_rate=0.0)
    await _dismiss_keyboard(serial)
    nxt = await _wait_for_any(serial, _NEXT, 8)
    if nxt:
        await _tap_match(serial, nxt)

    # Post-password: either a block phrase, or a cascade of "I agree" /
    # backup-setup / sync prompts before landing back in Settings.
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        block = await _find(serial, _BLOCK_PHRASES)
        if block:
            return {"ok": False, "quarantine": True, "state": "blocked", "reason": block["needle"]}
        await _assert_in_flow(serial)
        if await _dismiss_known_popups(serial):
            await asyncio.sleep(1.0)
            continue
        confirmed = await list_accounts(serial)
        if email.lower() in [e.lower() for e in confirmed.get("emails", [])]:
            return {"ok": True, "email": email, "detail": "signed in"}
        await asyncio.sleep(1.0)

    recovery = await _recover(serial, _BLOCK_PHRASES, _RECOVERY_GOAL, "timed out waiting for sign-in to complete",
                               vision_model, recovery_model)
    if recovery.get("blocked"):
        return {"ok": False, "quarantine": True, "state": recovery["state"], "reason": recovery["reason"]}
    if recovery.get("ok"):
        confirmed = await list_accounts(serial)
        if email.lower() in [e.lower() for e in confirmed.get("emails", [])]:
            return {"ok": True, "email": email, "detail": "signed in"}

    return {"ok": False, "quarantine": False, "state": "unknown",
            "reason": "timed out waiting for sign-in to complete", "recovery_trail": recovery.get("trail", []),
            **await _diagnostic(serial)}


async def sign_out(serial: str, email: str | None = None, timeout: float = 60.0) -> dict:
    """Removes a Google account (a specific `email`, or the first Google
    account found if none given) via Settings > Accounts > Remove account."""
    current = await list_accounts(serial)
    emails = current.get("emails", [])
    if not emails:
        return {"ok": True, "detail": "no Google account signed in — no-op"}
    target = email or emails[0]
    if email and email.lower() not in [e.lower() for e in emails]:
        return {"ok": False, "error": f"{email} is not signed in on this device"}
    try:
        return await _sign_out_flow(serial, target, timeout)
    except _OffFlowError as e:
        return {"ok": False, "error": f"left the sign-out flow — foreground app is now {e.package!r}",
                **await _diagnostic(serial, needle="")}


async def _sign_out_flow(serial: str, target: str, timeout: float) -> dict:
    await _ensure_ready(serial)
    await adb.shell(serial, f"am start -a {ACCOUNTS_INTENT}")
    acct_row = await _wait_for_any(serial, [target], 15)
    if not acct_row:
        return {"ok": False, "error": f"couldn't find {target} in the Accounts list UI",
                **await _diagnostic(serial, needle="")}
    await _tap_match(serial, acct_row)
    await asyncio.sleep(1.0)

    remove = await _wait_for_any(serial, ["Remove account"], 10)
    if not remove:
        return {"ok": False, "error": "'Remove account' option not found",
                **await _diagnostic(serial, needle="remove")}
    await _tap_match(serial, remove)
    await asyncio.sleep(0.8)
    confirm = await _wait_for_any(serial, ["Remove account"], 5)  # confirmation dialog reuses the label
    if confirm:
        await _tap_match(serial, confirm)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        left = await list_accounts(serial)
        if target.lower() not in [e.lower() for e in left.get("emails", [])]:
            return {"ok": True, "email": target, "detail": "signed out"}
        await _assert_in_flow(serial)
        await asyncio.sleep(1.0)
    return {"ok": False, "error": "timed out waiting for account removal to complete",
            **await _diagnostic(serial, needle="")}
