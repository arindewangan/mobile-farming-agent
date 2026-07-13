"""
minitouch integration — low-latency multitouch input.

Uses DeviceFarmer's minitouch (github.com/DeviceFarmer/minitouch): a no-root
native binary that accepts a tiny text command protocol over an abstract unix
socket, injecting touch events far faster than `adb shell input` (which spawns
a JVM per call, ~100-300ms). We keep one persistent connection per device.

Coordinates: callers pass device-pixel coords; minitouch has its own max_x/max_y
space (read from its header), so we scale into it.
"""
from __future__ import annotations

import asyncio
import os

VENDOR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor")
DEVICE_DIR = "/data/local/tmp"


def binary(abi: str) -> str | None:
    p = os.path.join(VENDOR, abi, "minitouch")
    return p if os.path.exists(p) else None


async def _adb(args: list[str], timeout: float = 30.0):
    p = await asyncio.create_subprocess_exec(
        "adb", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await asyncio.wait_for(p.communicate(), timeout)
    return p.returncode or 0, out, err


async def deploy(serial: str, abi: str) -> None:
    b = binary(abi)
    if not b:
        raise RuntimeError(f"no vendored minitouch for abi '{abi}'")
    await _adb(["-s", serial, "push", b, f"{DEVICE_DIR}/minitouch"])
    await _adb(["-s", serial, "shell", "chmod", "755", f"{DEVICE_DIR}/minitouch"])


class Minitouch:
    """Persistent minitouch session for one device (device-pixel coord API)."""

    def __init__(self, serial: str, dev_width: int, dev_height: int) -> None:
        self.serial = serial
        self.dev_width = dev_width
        self.dev_height = dev_height
        self.proc: asyncio.subprocess.Process | None = None
        self.port: int | None = None
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.max_x = dev_width
        self.max_y = dev_height
        self.max_pressure = 50
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        self.proc = await asyncio.create_subprocess_exec(
            "adb", "-s", self.serial, "shell",
            f"{DEVICE_DIR}/minitouch",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.sleep(0.4)
        rc, out, err = await _adb(["-s", self.serial, "forward", "tcp:0", "localabstract:minitouch"])
        if rc != 0:
            raise RuntimeError(f"adb forward failed: {err.decode(errors='replace')}")
        self.port = int(out.decode().strip())

        last: Exception | None = None
        for _ in range(30):
            try:
                self.reader, self.writer = await asyncio.open_connection("127.0.0.1", self.port)
                break
            except OSError as e:
                last = e
                await asyncio.sleep(0.1)
        else:
            raise RuntimeError(f"minitouch connect failed: {last}")

        await self._read_header()

    async def _read_header(self) -> None:
        assert self.reader is not None
        # Header lines: "v <n>", "^ <contacts> <max_x> <max_y> <max_pressure>", "$ <pid>"
        for _ in range(8):
            try:
                line = (await asyncio.wait_for(self.reader.readline(), timeout=5)).decode(
                    errors="replace").strip()
            except asyncio.TimeoutError:
                raise RuntimeError("minitouch header read timed out — binary started but never wrote its banner")
            if not line:
                continue
            if line.startswith("^"):
                parts = line.split()
                if len(parts) >= 5:
                    self.max_x = int(parts[2])
                    self.max_y = int(parts[3])
                    self.max_pressure = int(parts[4])
            elif line.startswith("$"):
                break

    def _scale(self, x: int, y: int) -> tuple[int, int]:
        sx = int(x / max(1, self.dev_width) * self.max_x)
        sy = int(y / max(1, self.dev_height) * self.max_y)
        return sx, sy

    async def _write(self, cmd: str) -> None:
        assert self.writer is not None
        self.writer.write(cmd.encode())
        await self.writer.drain()

    async def down(self, x: int, y: int) -> None:
        sx, sy = self._scale(x, y)
        async with self._lock:
            await self._write(f"d 0 {sx} {sy} {self.max_pressure}\nc\n")

    async def move(self, x: int, y: int) -> None:
        sx, sy = self._scale(x, y)
        async with self._lock:
            await self._write(f"m 0 {sx} {sy} {self.max_pressure}\nc\n")

    async def up(self) -> None:
        async with self._lock:
            await self._write("u 0\nc\n")

    async def tap(self, x: int, y: int) -> None:
        sx, sy = self._scale(x, y)
        p = self.max_pressure
        async with self._lock:
            await self._write(f"d 0 {sx} {sy} {p}\nc\nu 0\nc\n")

    async def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 200) -> None:
        sx1, sy1 = self._scale(x1, y1)
        sx2, sy2 = self._scale(x2, y2)
        p = self.max_pressure
        steps = max(2, min(30, duration_ms // 15))
        async with self._lock:
            await self._write(f"d 0 {sx1} {sy1} {p}\nc\n")
            for i in range(1, steps + 1):
                ix = sx1 + (sx2 - sx1) * i // steps
                iy = sy1 + (sy2 - sy1) * i // steps
                await self._write(f"m 0 {ix} {iy} {p}\nc\nw {duration_ms // steps}\n")
            await self._write("u 0\nc\n")

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
