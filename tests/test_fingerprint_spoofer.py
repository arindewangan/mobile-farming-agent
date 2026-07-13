"""
fingerprint_spoofer.py: get_status()/spoof_all() must report top-level "ok" —
main.py's _cmd() gate (backend/app/main.py) treats a result dict without an
"ok": True key as an agent failure (502 "agent error") regardless of what
actually happened on-device. Found live during a full app test pass: every
fingerprint status read 502'd (device page always showed "-" placeholders)
and every real spoof succeeded on-device but was reported to the operator
as a failure.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

import fingerprint_spoofer

pytestmark = pytest.mark.asyncio


def _shell_ok(stdout: str = "") -> dict:
    return {"ok": True, "rc": 0, "stdout": stdout, "stderr": ""}


async def test_get_status_reports_ok():
    with patch.object(fingerprint_spoofer.adb, "shell", AsyncMock(return_value=_shell_ok("null"))), \
         patch.object(fingerprint_spoofer.adb, "getprop", AsyncMock(return_value="unknown")):
        result = await fingerprint_spoofer.get_status("serial1")
    assert result["ok"] is True


async def test_get_status_reads_real_device_values():
    async def shell_side_effect(serial, cmd):
        if "android_id" in cmd:
            return _shell_ok("abc123")
        return _shell_ok("null")
    with patch.object(fingerprint_spoofer.adb, "shell", AsyncMock(side_effect=shell_side_effect)), \
         patch.object(fingerprint_spoofer.adb, "getprop", AsyncMock(return_value="Pixel 8")):
        result = await fingerprint_spoofer.get_status("serial1")
    assert result["android_id"] == "abc123"
    assert result["advertising_id"] is None  # "null" string maps to None


async def test_spoof_all_reports_ok():
    with patch.object(fingerprint_spoofer.adb, "shell", AsyncMock(return_value=_shell_ok())):
        result = await fingerprint_spoofer.spoof_all("serial1")
    assert result["ok"] is True
    # per-field results are still nested underneath, untouched by the fix
    assert result["android_id"]["ok"] is True


async def test_rollback_still_reports_ok():
    """rollback() already had the right shape — pin it so a future edit
    can't silently regress it back to the get_status/spoof_all bug."""
    with patch.object(fingerprint_spoofer.adb, "shell", AsyncMock(return_value=_shell_ok())):
        result = await fingerprint_spoofer.rollback("serial1")
    assert result["ok"] is True
