"""
AI-driven UI automation via mobilerun (formerly DroidRun) + a local Ollama LLM.

mobilerun installs a Portal accessibility service on the device that exposes the
UI tree and executes gestures; an LLM agent loop reads the tree and drives the app
from a natural-language goal ("open Settings and report the Android version",
"log into the app", "scroll the feed and like 3 posts"). We shell out to the
`mobilerun` CLI (proven, stable surface) rather than its internal Python API.

LLM backend is local Ollama by default — no API key, no per-task cost, private,
which is what a 10k-device farm needs. Big local models cold-load slowly (a 15GB
model took ~80s), which blows past mobilerun's ~30s per-request timeout, so we
pre-warm the model (Ollama keep_alive) before running the task.
"""
from __future__ import annotations

import asyncio
import glob
import json
import os
import shutil
import sys
import tempfile
import urllib.request

# Defaults (overridable per call or via env).
DEFAULT_PROVIDER = os.environ.get("MOBILERUN_PROVIDER", "Ollama")
DEFAULT_MODEL = os.environ.get("MOBILERUN_MODEL", "mistral-small3.1")
DEFAULT_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
DEFAULT_STEPS = int(os.environ.get("MOBILERUN_STEPS", "15"))

PORTAL_PKG = "com.mobilerun.portal"
PORTAL_A11Y = f"{PORTAL_PKG}/{PORTAL_PKG}.service.MobilerunAccessibilityService"


def _mobilerun_exe() -> str:
    """Resolve the mobilerun CLI next to the running interpreter, else rely on PATH."""
    d = os.path.dirname(sys.executable)
    for name in ("mobilerun.exe", "mobilerun"):
        p = os.path.join(d, name)
        if os.path.exists(p):
            return p
    return "mobilerun"


def _utf8_env() -> dict:
    env = os.environ.copy()
    # mobilerun's rich console prints emoji; force UTF-8 so it doesn't crash on cp1252.
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def _prewarm(base_url: str, model: str) -> None:
    """Load the model into memory (keep_alive) so the first agent step doesn't
    time out on cold-load. Best-effort, blocking — call via asyncio.to_thread."""
    try:
        req = urllib.request.Request(
            f"{base_url.rstrip('/')}/api/generate",
            data=json.dumps({"model": model, "keep_alive": "60m"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=300).read()
    except Exception:  # noqa: BLE001 — warming is an optimization, never fatal
        pass


def _read_trajectory(base_dir: str) -> list[dict]:
    """Parse mobilerun's saved trajectory into a compact per-action step list.

    mobilerun writes <cwd>/trajectories/<ts>_<id>/trajectory.json — a list of typed
    events. We pair each ToolExecutionEvent (the action) with the preceding
    FastAgentResponseEvent (the reasoning) into one readable step.
    """
    files = glob.glob(os.path.join(base_dir, "trajectories", "*", "trajectory.json"))
    if not files:
        return []
    files.sort(key=os.path.getmtime)
    try:
        events = json.load(open(files[-1], encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    steps: list[dict] = []
    thought = ""
    for e in events if isinstance(events, list) else []:
        t = e.get("type")
        if t == "FastAgentResponseEvent":
            thought = (e.get("thought") or "").strip()
        elif t == "ToolExecutionEvent":
            steps.append({
                "n": len(steps) + 1,
                "thought": thought[:400],
                "tool": e.get("tool_name"),
                "args": e.get("tool_args"),
                "ok": bool(e.get("success")),
                "summary": e.get("summary"),
            })
            thought = ""
    return steps


def _parse_result(output: str) -> dict:
    """Pull the final success/failure + message out of mobilerun's console output."""
    # mobilerun prints exactly "🎉 Goal achieved: <reason>" or "❌ Goal failed: <reason>".
    success = None
    message = ""
    for line in output.splitlines():
        s = line.strip()
        if "Goal failed" in s:
            success = False
            message = s.split(":", 1)[1].strip() if ":" in s else s
        elif "Goal achieved" in s or "Goal completed" in s:
            success = True
            message = s.split(":", 1)[1].strip() if ":" in s else s
    return {"success": success, "message": message}


async def run_task(
    serial: str,
    task: str,
    provider: str = DEFAULT_PROVIDER,
    model: str = DEFAULT_MODEL,
    base_url: str = DEFAULT_BASE_URL,
    steps: int = DEFAULT_STEPS,
    vision: bool = False,
    reasoning: bool = False,
    timeout: float = 600.0,
) -> dict:
    """Run a natural-language task against a device and return the outcome."""
    if provider == "Ollama":
        await asyncio.to_thread(_prewarm, base_url, model)

    args = [
        _mobilerun_exe(), "run",
        "-d", serial,
        "-p", provider,
        "-m", model,
        "--steps", str(steps),
        "--vision" if vision else "--no-vision",
        "--reasoning" if reasoning else "--no-reasoning",
        "--save-trajectory", "step",
    ]
    if provider == "Ollama":
        args += ["-u", base_url]
    args.append(task)

    # Run in a throwaway cwd so mobilerun writes its trajectory/<ts>/ under it.
    work = tempfile.mkdtemp(prefix="mr_traj_")
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=_utf8_env(),
            cwd=work,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return {"ok": False, "error": f"task timed out after {timeout}s", "task": task,
                    "trajectory": _read_trajectory(work)}

        output = out.decode(errors="replace")
        parsed = _parse_result(output)
        trajectory = _read_trajectory(work)
        return {
            "ok": proc.returncode == 0 and parsed["success"] is not False,
            "success": parsed["success"],
            "message": parsed["message"],
            "task": task,
            "model": model,
            "trajectory": trajectory,
            "output": output[-4000:],  # tail; full trajectory is large
        }
    finally:
        shutil.rmtree(work, ignore_errors=True)


MACRO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "macros")


def list_macros() -> dict:
    """Named macros that have been recorded (a trajectory folder each)."""
    if not os.path.isdir(MACRO_DIR):
        return {"ok": True, "macros": []}
    names = [d for d in os.listdir(MACRO_DIR)
             if os.path.isdir(os.path.join(MACRO_DIR, d))
             and glob.glob(os.path.join(MACRO_DIR, d, "trajectories", "*", "trajectory.json"))]
    return {"ok": True, "macros": sorted(names)}


async def record_macro(serial: str, task: str, name: str, provider: str = DEFAULT_PROVIDER,
                       model: str = DEFAULT_MODEL, base_url: str = DEFAULT_BASE_URL,
                       steps: int = DEFAULT_STEPS, vision: bool = False, timeout: float = 600.0) -> dict:
    """Run a task once with the LLM and KEEP its trajectory as a replayable macro."""
    if not name.replace("_", "").replace("-", "").isalnum():
        return {"ok": False, "error": "macro name must be alphanumeric/_/-"}
    if provider == "Ollama":
        await asyncio.to_thread(_prewarm, base_url, model)
    dest = os.path.join(MACRO_DIR, name)
    shutil.rmtree(dest, ignore_errors=True)
    os.makedirs(dest, exist_ok=True)
    args = [_mobilerun_exe(), "run", "-d", serial, "-p", provider, "-m", model,
            "--steps", str(steps), "--vision" if vision else "--no-vision",
            "--save-trajectory", "step"]
    if provider == "Ollama":
        args += ["-u", base_url]
    args.append(task)
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        env=_utf8_env(), cwd=dest)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill(); await proc.wait()
        return {"ok": False, "error": f"record timed out after {timeout}s", "macro": name}
    traj = _read_trajectory(dest)
    return {"ok": bool(traj), "macro": name, "steps": len(traj),
            "trajectory": traj, "task": task}


async def replay_macro(serial: str, name: str, delay: float = 0.6,
                       on_mismatch: str = "agent", provider: str = DEFAULT_PROVIDER,
                       model: str = DEFAULT_MODEL, base_url: str = DEFAULT_BASE_URL,
                       timeout: float = 400.0) -> dict:
    """Replay a recorded macro. on_mismatch='agent' hands off to the LLM when the UI
    diverges from the recording; 'stop' aborts. Far cheaper than an LLM run each time."""
    folders = sorted(glob.glob(os.path.join(MACRO_DIR, name, "trajectories", "*")))
    if not folders:
        return {"ok": False, "error": f"macro '{name}' not found"}
    if on_mismatch == "agent" and provider == "Ollama":
        await asyncio.to_thread(_prewarm, base_url, model)
    args = [_mobilerun_exe(), "macro", "replay", folders[-1], "-d", serial,
            "-t", str(delay), "--on-mismatch", on_mismatch]
    if on_mismatch == "agent":
        args += ["--provider", provider, "--model", model]
        if provider == "Ollama":
            args += ["-u", base_url]
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=_utf8_env())
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill(); await proc.wait()
        return {"ok": False, "error": f"replay timed out after {timeout}s"}
    output = out.decode(errors="replace")
    return {"ok": proc.returncode == 0, "macro": name, "output": output[-3000:]}


async def setup(serial: str) -> dict:
    """Install the Portal on a device and enable its accessibility service."""
    proc = await asyncio.create_subprocess_exec(
        _mobilerun_exe(), "setup", "-d", serial,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=_utf8_env(),
    )
    out, _ = await asyncio.wait_for(proc.communicate(), timeout=300)
    # a11y services can't be enabled by the setup flow without a manual toggle, so
    # grant it over adb (works because we control the device via adb).
    await _adb(serial, ["shell", "settings", "put", "secure",
                        "enabled_accessibility_services", PORTAL_A11Y])
    await _adb(serial, ["shell", "settings", "put", "secure", "accessibility_enabled", "1"])
    return {"ok": True, "output": out.decode(errors="replace")[-2000:]}


async def ping(serial: str) -> dict:
    proc = await asyncio.create_subprocess_exec(
        _mobilerun_exe(), "ping", "-d", serial,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=_utf8_env(),
    )
    out, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
    text = out.decode(errors="replace")
    return {"ok": "good to go" in text.lower() or "accessible" in text.lower(), "output": text[-1000:]}


async def status() -> dict:
    """Device-independent "is mobilerun actually installed on this host"
    check for the settings page — setup/ping both need a serial and thus a
    connected phone, which isn't required just to confirm the CLI resolves.
    Also probes the configured Ollama base_url, since that's the other
    on-agent-host piece a local-provider setup depends on."""
    exe = _mobilerun_exe()
    try:
        proc = await asyncio.create_subprocess_exec(
            exe, "--version",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=_utf8_env(),
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
        version = out.decode(errors="replace").strip().splitlines()[-1] if out else ""
        installed = proc.returncode == 0
    except FileNotFoundError:
        return {"ok": False, "installed": False,
                "error": f"'{exe}' not found — run `pip install mobilerun` in this agent's venv"}
    except asyncio.TimeoutError:
        return {"ok": False, "installed": False, "error": f"'{exe} --version' timed out"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "installed": False, "error": str(e)}

    ollama_ok = None
    if DEFAULT_PROVIDER == "Ollama":
        try:
            await asyncio.to_thread(
                lambda: urllib.request.urlopen(f"{DEFAULT_BASE_URL.rstrip('/')}/api/tags", timeout=5).read())
            ollama_ok = True
        except Exception:  # noqa: BLE001
            ollama_ok = False

    return {"ok": installed, "installed": installed, "version": version,
            "provider": DEFAULT_PROVIDER, "model": DEFAULT_MODEL, "base_url": DEFAULT_BASE_URL,
            "ollama_reachable": ollama_ok}


async def _adb(serial: str, args: list[str]) -> None:
    proc = await asyncio.create_subprocess_exec(
        "adb", "-s", serial, *args,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
