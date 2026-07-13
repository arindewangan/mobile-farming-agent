"""
Custom script execution — runs user Python/Bash on the AGENT HOST (not the
device), with DEVICE_SERIAL in its environment so the script can shell out to
`adb -s $DEVICE_SERIAL ...` itself. This is the automation engine's escape
hatch for anything the step DSL doesn't cover directly.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys


async def run_script(serial: str, interpreter: str, code: str, timeout: float = 30.0) -> dict:
    env = os.environ.copy()
    env["DEVICE_SERIAL"] = serial

    if interpreter == "python":
        cmd = [sys.executable, "-c", code]
    elif interpreter == "bash":
        bash = shutil.which("bash")
        if not bash:
            return {"ok": False, "error": "bash not found on agent host PATH"}
        cmd = [bash, "-c", code]
    else:
        return {"ok": False, "error": f"unknown interpreter '{interpreter}' (use python|bash)"}

    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return {"ok": False, "error": f"script timed out after {timeout}s"}

    return {
        "ok": proc.returncode == 0,
        "rc": proc.returncode,
        "stdout": out.decode(errors="replace"),
        "stderr": err.decode(errors="replace"),
    }
