"""
minicap integration — fast (~30-40fps) JPEG screen capture.

Uses DeviceFarmer's minicap (github.com/DeviceFarmer/minicap): a small, no-root
native binary pushed over adb that streams JPEG frames out of an abstract unix
socket. We forward that socket to a local TCP port and read the documented
banner + length-prefixed frame protocol.

Binaries live under agent/vendor/<abi>/{minicap,minicap.so} and are matched to
the device ABI. Falls back gracefully (caller uses screencap) when a device's
ABI/SDK isn't vendored.
"""
from __future__ import annotations

import asyncio
import os
import struct

VENDOR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor")
DEVICE_DIR = "/data/local/tmp"


def binaries(abi: str) -> tuple[str | None, str | None]:
    d = os.path.join(VENDOR, abi)
    mc, so = os.path.join(d, "minicap"), os.path.join(d, "minicap.so")
    if os.path.exists(mc) and os.path.exists(so):
        return mc, so
    return None, None


async def _adb(args: list[str], timeout: float = 30.0):
    p = await asyncio.create_subprocess_exec(
        "adb", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await asyncio.wait_for(p.communicate(), timeout)
    return p.returncode or 0, out, err


async def deploy(serial: str, abi: str) -> None:
    """Push minicap + its .so to the device (idempotent enough to just re-push)."""
    mc, so = binaries(abi)
    if not mc or not so:
        raise RuntimeError(f"no vendored minicap for abi '{abi}'")
    await _adb(["-s", serial, "push", mc, f"{DEVICE_DIR}/minicap"])
    await _adb(["-s", serial, "push", so, f"{DEVICE_DIR}/minicap.so"])
    await _adb(["-s", serial, "shell", "chmod", "755", f"{DEVICE_DIR}/minicap"])


class Minicap:
    """One capture session for one device."""

    def __init__(self, serial: str, width: int, height: int, scale: float = 0.5) -> None:
        self.serial = serial
        self.width = width
        self.height = height
        self.scale = scale
        self.proc: asyncio.subprocess.Process | None = None
        self.port: int | None = None
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None

    async def start(self) -> None:
        vw = max(2, int(self.width * self.scale)) // 2 * 2
        vh = max(2, int(self.height * self.scale)) // 2 * 2
        param = f"{self.width}x{self.height}@{vw}x{vh}/0"
        self.proc = await asyncio.create_subprocess_exec(
            "adb", "-s", self.serial, "shell",
            f"LD_LIBRARY_PATH={DEVICE_DIR} {DEVICE_DIR}/minicap -P {param}",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        # Give minicap a moment to create its abstract socket, then forward it.
        await asyncio.sleep(0.5)
        rc, out, err = await _adb(["-s", self.serial, "forward", "tcp:0", "localabstract:minicap"])
        if rc != 0:
            raise RuntimeError(f"adb forward failed: {err.decode(errors='replace')}")
        self.port = int(out.decode().strip())

        # Connect (minicap may take a beat to accept).
        last: Exception | None = None
        for _ in range(30):
            try:
                self.reader, self.writer = await asyncio.open_connection("127.0.0.1", self.port)
                break
            except OSError as e:
                last = e
                await asyncio.sleep(0.1)
        else:
            raise RuntimeError(f"minicap connect failed: {last}")

        try:
            await asyncio.wait_for(self.reader.readexactly(24), timeout=5)  # global banner; sizes already known
        except asyncio.TimeoutError:
            raise RuntimeError("minicap header read timed out — process started but never wrote its banner")

    async def frames(self):
        assert self.reader is not None
        while True:
            try:
                (flen,) = struct.unpack("<I", await asyncio.wait_for(self.reader.readexactly(4), timeout=10))
                yield await asyncio.wait_for(self.reader.readexactly(flen), timeout=10)
            except asyncio.TimeoutError:
                raise RuntimeError("minicap frame stream stalled — no data within timeout")

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
