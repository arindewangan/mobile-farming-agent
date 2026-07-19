"""
Reading which Google accounts are signed in on a device.

This file used to hold 42 tests for on-device sign-in/sign-out UI automation.
That implementation was deleted (see googleaccounts.py's module docstring):
the backend never dispatched it, and Google sign-in now runs PC-side in
backend/app/signin_recovery.py under the compute-split rule. The tests
outlived the code by one commit and errored at fixture setup, patching
attributes that no longer existed — 42 errors that drowned out any real
regression the rest of the suite might have caught.

What's left is the one function the backend does call. It parses `dumpsys
account`, so the tests worth having are parser tests: the shapes of real
dumpsys output, especially the ones that would quietly return a wrong answer
rather than raise.
"""
from __future__ import annotations

import pytest

import adb
import googleaccounts

pytestmark = pytest.mark.asyncio


def _stub_dumpsys(monkeypatch, stdout):
    """adb.shell returning a canned `dumpsys account` body."""
    async def fake_shell(serial, cmd):
        assert cmd == "dumpsys account", f"unexpected command: {cmd}"
        return {"ok": True, "stdout": stdout, "stderr": ""}
    monkeypatch.setattr(adb, "shell", fake_shell)


async def test_reads_the_signed_in_google_accounts(monkeypatch):
    _stub_dumpsys(monkeypatch, """
    Accounts: 2
      Account {name=first@gmail.com, type=com.google}
      Account {name=second@gmail.com, type=com.google}
    """)
    assert await googleaccounts.list_accounts("abc123") == {
        "ok": True, "emails": ["first@gmail.com", "second@gmail.com"]}


async def test_returns_no_emails_when_nothing_is_signed_in(monkeypatch):
    """The common case before a sign-in run — must be an empty list, not a
    failure, so callers can use it as a cheap idempotency check."""
    _stub_dumpsys(monkeypatch, "Accounts: 0\n")
    assert await googleaccounts.list_accounts("abc123") == {"ok": True, "emails": []}


async def test_ignores_non_google_accounts(monkeypatch):
    """Devices carry Samsung/WhatsApp/etc. accounts in the same dumpsys
    output. Counting those as Google accounts would make an idempotency check
    believe a device was already signed in and skip the real work."""
    _stub_dumpsys(monkeypatch, """
      Account {name=user@example.com, type=com.samsung.android.mobileservice}
      Account {name=real@gmail.com, type=com.google}
      Account {name=+15551234567, type=com.whatsapp}
    """)
    assert (await googleaccounts.list_accounts("abc123"))["emails"] == ["real@gmail.com"]


async def test_deduplicates_and_sorts(monkeypatch):
    """dumpsys repeats an account once per authenticator that references it,
    so duplicates are normal rather than a sign of corruption. Sorting keeps
    the result stable for callers comparing against a previous reading."""
    _stub_dumpsys(monkeypatch, """
      Account {name=zed@gmail.com, type=com.google}
      Account {name=amy@gmail.com, type=com.google}
      Account {name=zed@gmail.com, type=com.google}
    """)
    assert (await googleaccounts.list_accounts("abc123"))["emails"] == [
        "amy@gmail.com", "zed@gmail.com"]


async def test_survives_a_shell_call_that_returned_nothing(monkeypatch):
    """An offline or wedged device yields a result with no stdout key at all.
    Reading it must not raise KeyError — the caller's next move is to report
    the device unreachable, not to crash mid-sweep."""
    async def fake_shell(serial, cmd):
        return {"ok": False, "error": "device offline"}
    monkeypatch.setattr(adb, "shell", fake_shell)
    assert await googleaccounts.list_accounts("abc123") == {"ok": True, "emails": []}
