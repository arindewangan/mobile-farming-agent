"""
Google account inspection on a device.

This module used to also drive sign-in and sign-out through the OS "Add
account" flow. Both were removed: the backend never dispatched them. Google
sign-in moved entirely PC-side to backend/app/signin_recovery.py, where the
control plane makes every decision (UI matching, OCR, tier-2/3 escalation) and
the device only executes primitives — the compute-split rule that keeps heavy
work off the edge. Keeping a second, diverging copy of that flow here meant
~550 lines of on-device automation nothing could reach, which would silently
rot out of sync with the one actually in use.

What remains is the one thing the backend does ask for (google_list_accounts):
reading which accounts are currently signed in. It needs no UI interaction, so
it's cheap enough to use for idempotency checks before a sign-in.
"""
from __future__ import annotations

import re

import adb

# `dumpsys account` lists entries as: Account {name=someone@gmail.com, type=com.google}
_ACCOUNT_RE = re.compile(r"Account \{name=([^,]+@[^,]+), type=com\.google\}")


async def list_accounts(serial: str) -> dict:
    """Currently signed-in Google accounts via `dumpsys account` — no UI
    interaction needed, so cheap enough to use for idempotency checks."""
    r = await adb.shell(serial, "dumpsys account")
    emails = sorted(set(_ACCOUNT_RE.findall(r.get("stdout", ""))))
    return {"ok": True, "emails": emails}
