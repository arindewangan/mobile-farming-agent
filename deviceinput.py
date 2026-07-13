"""
Persistent adb-shell input channel — the reliable, rootless fast path.

`adb shell input tap ...` as a fresh process each time is slow (~500ms on
Windows: adb.exe spawn + server handshake + on-device JVM). We instead keep ONE
`adb -s <serial> shell` alive per device and feed it `input ...` lines on stdin,
fire-and-forget. That removes the per-call adb.exe spawn and handshake, leaving
only Android's on-device `input` cost (~150ms) — a big win, no root, works on
devices where minitouch can't attach to the touch node.

For truly native (<50ms) input you need the scrcpy control server (persistent
InputManager injection); this is the best rootless path without that.
"""
from __future__ import annotations

import asyncio
import shlex


class InputShell:
    def __init__(self, serial: str) -> None:
        self.serial = serial
        self.proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        """Pre-warm the shell so the first input has no spawn delay."""
        async with self._lock:
            await self._ensure()

    async def _ensure(self) -> None:
        if self.proc is None or self.proc.returncode is not None:
            self.proc = await asyncio.create_subprocess_exec(
                "adb", "-s", self.serial, "shell",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )

    async def _write(self, line: str) -> None:
        async with self._lock:
            await self._ensure()
            assert self.proc and self.proc.stdin
            self.proc.stdin.write((line + "\n").encode())
            await self.proc.stdin.drain()

    async def tap(self, x: int, y: int) -> None:
        await self._write(f"input tap {x} {y}")

    async def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 140) -> None:
        await self._write(f"input swipe {x1} {y1} {x2} {y2} {duration_ms}")

    async def key(self, keycode: str) -> None:
        await self._write(f"input keyevent {keycode}")

    async def text(self, s: str) -> None:
        # `s` is raw, potentially untrusted text fed straight into a persistent
        # `adb shell` process's stdin, which the remote /system/bin/sh interprets
        # line by line — an unescaped shell metacharacter (`` ` ``, `$(...)`, `;`,
        # `&&`, quotes, ...) would execute on the device instead of being typed.
        # shlex.quote() wraps it in a single-quoted, POSIX-shell-safe token.
        body = s.replace(" ", "%s")
        await self._write(f"input text {shlex.quote(body)}")

    async def stop(self) -> None:
        if self.proc and self.proc.returncode is None:
            try:
                if self.proc.stdin:
                    self.proc.stdin.close()
            except Exception:
                pass
            self.proc.kill()
            await self.proc.wait()
