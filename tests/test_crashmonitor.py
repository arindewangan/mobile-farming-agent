"""
Continuous crash/ANR monitoring: log parsing against realistic logcat
output, and the dedupe/baseline behavior of the polling loop.
"""
from __future__ import annotations

import pytest

import crashmonitor

CRASH_SAMPLE = """--------- beginning of crash
E/AndroidRuntime( 5321): FATAL EXCEPTION: main
E/AndroidRuntime( 5321): Process: com.example.badapp, PID: 5321
E/AndroidRuntime( 5321): java.lang.NullPointerException: something bad
E/AndroidRuntime( 5321):     at com.example.badapp.Main.onCreate(Main.java:42)
"""

ANR_SAMPLE = """I/ActivityManager( 1200): ANR in com.example.slowapp (com.example.slowapp/.MainActivity)
I/ActivityManager( 1200): Reason: Input dispatching timed out
"""


def test_parse_crashes_extracts_package_and_detail():
    events = crashmonitor._parse_crashes(CRASH_SAMPLE)
    assert len(events) == 1
    assert events[0]["kind"] == "crash"
    assert events[0]["package"] == "com.example.badapp"
    assert "FATAL EXCEPTION" in events[0]["detail"]


def test_parse_crashes_handles_multiple_crashes():
    doubled = CRASH_SAMPLE + "\n" + CRASH_SAMPLE.replace("badapp", "otherapp").replace("5321", "9999")
    events = crashmonitor._parse_crashes(doubled)
    assert len(events) == 2
    assert {e["package"] for e in events} == {"com.example.badapp", "com.example.otherapp"}


def test_parse_crashes_falls_back_to_unknown_package_when_process_line_missing():
    sample = "E/AndroidRuntime( 1): FATAL EXCEPTION: main\nno process line here at all"
    events = crashmonitor._parse_crashes(sample)
    assert len(events) == 1
    assert events[0]["package"] == "unknown"


def test_parse_crashes_empty_text_returns_nothing():
    assert crashmonitor._parse_crashes("") == []
    assert crashmonitor._parse_crashes("nothing interesting here") == []


def test_parse_anrs_extracts_package():
    events = crashmonitor._parse_anrs(ANR_SAMPLE)
    assert len(events) == 1
    assert events[0]["kind"] == "anr"
    assert events[0]["package"] == "com.example.slowapp"


def test_parse_anrs_empty_text_returns_nothing():
    assert crashmonitor._parse_anrs("") == []


@pytest.mark.asyncio
async def test_poll_once_first_call_establishes_baseline_without_reporting(monkeypatch):
    async def fake_shell(serial, cmd):
        if "crash" in cmd:
            return {"stdout": CRASH_SAMPLE}
        return {"stdout": ""}

    import adb
    monkeypatch.setattr(adb, "shell", fake_shell)
    crashmonitor._seen.pop("serial-1", None)

    events = await crashmonitor._poll_once("serial-1")
    # crashmonitor.start()'s _watch() treats the FIRST poll as baseline-only
    # and discards its return value — but _poll_once() itself always
    # reports what's new-to-it, so a fresh device's first poll does return
    # the crash. The "don't report it as new" behavior lives in _watch().
    assert len(events) == 1
    assert events[0]["package"] == "com.example.badapp"


@pytest.mark.asyncio
async def test_poll_once_does_not_repeat_already_seen_events(monkeypatch):
    async def fake_shell(serial, cmd):
        if "crash" in cmd:
            return {"stdout": CRASH_SAMPLE}
        return {"stdout": ""}

    import adb
    monkeypatch.setattr(adb, "shell", fake_shell)
    crashmonitor._seen.pop("serial-1", None)

    first = await crashmonitor._poll_once("serial-1")
    second = await crashmonitor._poll_once("serial-1")
    assert len(first) == 1
    assert len(second) == 0  # same crash still in the buffer, already reported


@pytest.mark.asyncio
async def test_poll_once_reports_a_genuinely_new_crash(monkeypatch):
    call = {"stdout": CRASH_SAMPLE}

    async def fake_shell(serial, cmd):
        if "crash" in cmd:
            return {"stdout": call["stdout"]}
        return {"stdout": ""}

    import adb
    monkeypatch.setattr(adb, "shell", fake_shell)
    crashmonitor._seen.pop("serial-1", None)

    first = await crashmonitor._poll_once("serial-1")
    assert len(first) == 1

    call["stdout"] = CRASH_SAMPLE + "\n" + CRASH_SAMPLE.replace("badapp", "newapp").replace("5321", "7777")
    second = await crashmonitor._poll_once("serial-1")
    assert len(second) == 1
    assert second[0]["package"] == "com.example.newapp"


@pytest.mark.asyncio
async def test_watch_loop_suppresses_pre_existing_crash_then_reports_a_new_one(monkeypatch):
    """End-to-end through start()/_watch() (not just _poll_once directly):
    a crash already in the buffer when monitoring starts must never reach
    on_event — only a crash that appears AFTER the baseline poll should."""
    import asyncio

    import adb

    state = {"stdout": CRASH_SAMPLE}

    async def fake_shell(serial, cmd):
        if "crash" in cmd:
            return {"stdout": state["stdout"]}
        return {"stdout": ""}

    monkeypatch.setattr(adb, "shell", fake_shell)
    monkeypatch.setattr(crashmonitor, "CHECK_INTERVAL_SEC", 0.02)
    crashmonitor._seen.pop("serial-1", None)
    crashmonitor._watchers.pop("serial-1", None)

    received = []

    async def on_event(serial, event):
        received.append((serial, event))

    crashmonitor.start("serial-1", on_event)
    await asyncio.sleep(0.05)  # let the baseline poll + one interval tick pass
    assert received == [], "the pre-existing crash must not have been reported"

    state["stdout"] = CRASH_SAMPLE + "\n" + CRASH_SAMPLE.replace("badapp", "newapp").replace("5321", "7777")
    await asyncio.sleep(0.05)  # let another poll tick pick up the new one
    crashmonitor.stop("serial-1")

    assert len(received) == 1
    assert received[0][0] == "serial-1"
    assert received[0][1]["package"] == "com.example.newapp"


def test_stop_on_never_started_serial_is_a_noop():
    crashmonitor.stop("never-started-serial")  # must not raise


@pytest.mark.asyncio
async def test_start_is_idempotent_for_an_already_running_watcher(monkeypatch):
    import adb

    async def fake_shell(serial, cmd):
        return {"stdout": ""}

    monkeypatch.setattr(adb, "shell", fake_shell)
    crashmonitor._watchers.pop("serial-2", None)

    async def on_event(serial, event):
        pass

    crashmonitor.start("serial-2", on_event)
    first_task = crashmonitor._watchers["serial-2"]
    crashmonitor.start("serial-2", on_event)  # should NOT replace the running task
    assert crashmonitor._watchers["serial-2"] is first_task
    crashmonitor.stop("serial-2")
