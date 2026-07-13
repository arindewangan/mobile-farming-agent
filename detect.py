"""
Detection feedback loop — classify a device's current screen with a local vision LLM.

The other half of "act like a human" is *knowing when you've been caught*. After
(or during) an automation run we screenshot the device and ask a local multimodal
model to classify the screen: normal, captcha, blocked/banned, login-challenge,
rate-limited, etc. The platform can then quarantine the profile, back off, or alert
— instead of blindly hammering a flagged account.

Runs fully local via Ollama's vision chat API (no API key, no data leaving the box).
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import urllib.request

import adb

DEFAULT_VISION_MODEL = os.environ.get("VISION_MODEL", "mistral-small3.1")
OLLAMA = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")

# States we care about; anything not in OK_STATES is treated as a problem.
OK_STATES = {"normal", "permission_dialog", "consent_dialog"}

_PROMPT = (
    "You are a QA bot inspecting a single Android app screenshot. Classify the "
    "CURRENT screen into exactly one state and reply ONLY as compact JSON: "
    '{"state": "...", "reason": "<=12 words}. Allowed states: '
    "normal (ordinary app content), "
    "captcha (a CAPTCHA / 'verify you are human' / puzzle), "
    "blocked (account banned/suspended/disabled, or an 'unusual activity' warning), "
    "login_challenge (asked to log in again, verify identity, enter OTP/2FA), "
    "rate_limited (too many requests / try again later), "
    "permission_dialog (an OS runtime-permission prompt), "
    "consent_dialog (cookie/terms/age consent), "
    "error (crash, 'something went wrong', no connection)."
)


async def classify_screen(serial: str, model: str = DEFAULT_VISION_MODEL,
                          base_url: str = OLLAMA, timeout: float = 150.0,
                          jpg: bytes | None = None) -> dict:
    """Screenshot the device and classify the screen state via a local vision
    model. Pass an already-captured `jpg` to skip the screencap — lets a
    caller chaining this with another screenshot-based check (e.g.
    vision.ocr_find) share one capture instead of two."""
    if jpg is None:
        jpg = await adb.screencap_full_jpeg(serial, 70)
    if not jpg:
        return {"ok": False, "error": "screencap failed"}
    b64 = base64.b64encode(jpg).decode("ascii")
    body = {
        "model": model,
        "stream": False,
        "format": "json",
        "keep_alive": "30m",
        "messages": [{"role": "user", "content": _PROMPT, "images": [b64]}],
    }

    def _call():
        req = urllib.request.Request(
            f"{base_url.rstrip('/')}/api/chat",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        return urllib.request.urlopen(req, timeout=timeout).read()

    try:
        raw = await asyncio.to_thread(_call)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"vision call failed: {e}"}

    try:
        content = json.loads(raw).get("message", {}).get("content", "{}")
        verdict = json.loads(content)
    except Exception:  # noqa: BLE001
        return {"ok": False, "error": "could not parse model output", "raw": raw.decode(errors="replace")[:400]}

    state = str(verdict.get("state", "normal")).lower().strip()
    blocked = state not in OK_STATES
    return {
        "ok": True,
        "state": state,
        "reason": verdict.get("reason", ""),
        "blocked": blocked,          # True → needs attention (quarantine/back-off)
        "model": model,
    }
