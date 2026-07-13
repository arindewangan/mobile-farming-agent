"""
scrcpy control server — instant, rootless input injection.

Android's `input` command cold-starts a JVM per call (~450ms on the S8). scrcpy
solves this with a persistent server process (com.genymobile.scrcpy.Server) that
holds InputManager and injects events over a socket in ~single-digit ms — no
root, no per-event process spawn.

We run it in CONTROL-ONLY mode (video=false), since minicap already handles the
mirror. Verified handshake on SM-G9500: one dummy byte (0x00) + 64-byte device
name, then it's the control channel.

Control message (INJECT_TOUCH_EVENT), scrcpy v2.4 wire format:
    >BBqiiHHHii = type(2) action pointerId x y width height pressure actionButton buttons
"""
from __future__ import annotations

import asyncio
import os
import struct

VENDOR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor")
JAR_LOCAL = os.path.join(VENDOR, "scrcpy-server.jar")
JAR_DEVICE = "/data/local/tmp/scrcpy-server.jar"
SERVER_VERSION = "2.4"

TYPE_INJECT_TOUCH = 2
ACTION_DOWN, ACTION_UP, ACTION_MOVE = 0, 1, 2
POINTER_ID = 0x1234567887654321  # a stable virtual finger id
_TOUCH = struct.Struct(">BBqiiHHHii")


def available() -> bool:
    return os.path.exists(JAR_LOCAL)


async def _adb(args: list[str], timeout: float = 30.0):
    p = await asyncio.create_subprocess_exec(
        "adb", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await asyncio.wait_for(p.communicate(), timeout)
    return p.returncode or 0, out, err


class ScrcpyControl:
    def __init__(self, serial: str, width: int, height: int, deployed: bool = False) -> None:
        self.serial = serial
        self.width = width
        self.height = height
        self._deployed = deployed
        self.proc: asyncio.subprocess.Process | None = None
        self.port: int | None = None
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if not self._deployed:
            await _adb(["-s", self.serial, "push", JAR_LOCAL, JAR_DEVICE])
        self.proc = await asyncio.create_subprocess_exec(
            "adb", "-s", self.serial, "shell",
            f"CLASSPATH={JAR_DEVICE} app_process / com.genymobile.scrcpy.Server "
            f"{SERVER_VERSION} log_level=error video=false audio=false control=true "
            f"tunnel_forward=true cleanup=false",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.sleep(0.7)  # let the server create the abstract socket
        rc, out, err = await _adb(["-s", self.serial, "forward", "tcp:0", "localabstract:scrcpy"])
        if rc != 0:
            raise RuntimeError(f"adb forward failed: {err.decode(errors='replace')}")
        self.port = int(out.decode().strip())

        last: Exception | None = None
        for _ in range(40):
            try:
                self.reader, self.writer = await asyncio.open_connection("127.0.0.1", self.port)
                break
            except OSError as e:
                last = e
                await asyncio.sleep(0.1)
        else:
            raise RuntimeError(f"scrcpy connect failed: {last}")

        # handshake: 1 dummy byte + 64-byte device name
        await asyncio.wait_for(self.reader.readexactly(65), timeout=5)

    async def _send_touch(self, action: int, x: int, y: int, pressure: int) -> None:
        msg = _TOUCH.pack(
            TYPE_INJECT_TOUCH, action, POINTER_ID,
            int(x), int(y), self.width, self.height, pressure, 0, 0,
        )
        async with self._lock:
            assert self.writer is not None
            self.writer.write(msg)
            await self.writer.drain()

    async def down(self, x: int, y: int) -> None:
        await self._send_touch(ACTION_DOWN, x, y, 0xFFFF)

    async def move(self, x: int, y: int) -> None:
        await self._send_touch(ACTION_MOVE, x, y, 0xFFFF)

    async def up(self, x: int, y: int) -> None:
        await self._send_touch(ACTION_UP, x, y, 0)

    async def tap(self, x: int, y: int) -> None:
        await self.down(x, y)
        await self.up(x, y)

    async def swipe(self, x1: int, y1: int, x2: int, y2: int, steps: int = 8) -> None:
        await self.down(x1, y1)
        for i in range(1, steps + 1):
            await self.move(x1 + (x2 - x1) * i // steps, y1 + (y2 - y1) * i // steps)
            await asyncio.sleep(0.008)
        await self.up(x2, y2)

    async def stop(self) -> None:
        try:
            if self.writer:
                self.writer.close()
        except Exception:
            pass
        if self.port is not None:
            await _adb(["-s", self.serial, "forward", "--remove", f"tcp:{self.port}"])
        if self.proc and self.proc.returncode is None:
            self.proc.kill()
            await self.proc.wait()
