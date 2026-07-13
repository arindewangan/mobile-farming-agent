"""
Local Ollama model management (list installed tags, pull/delete a model) —
backs the AI Model Registry settings page so an operator can manage models
directly on this agent's host without SSHing in. Ollama always runs on
THIS host (wherever this agent process lives, not the control plane's
machine), which is why this is an agent-level action rather than something
the backend can reach directly — the same reason detect.py/droidrun.py
already talk to a local Ollama instance from here.

No existing "background task + poll" pattern exists elsewhere in this
codebase (self_update just blocks until done) — a model pull is a new case
for it: it can legitimately take minutes for a multi-GB model, so it runs in
a background thread (urllib doesn't do async streaming cleanly) while
`pull_status()` reports live progress from a shared in-memory dict.
"""
from __future__ import annotations

import asyncio
import json
import os
import urllib.request

OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")

# tag -> {"status", "completed", "total", "error", "done"} — one entry per
# pull ever kicked off since this process started; small and short-lived
# enough (a handful of tags per operator session) that it never needs
# eviction.
_pulls: dict[str, dict] = {}


def _http_json(path: str, method: str = "GET", body: dict | None = None, timeout: float = 15.0) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{OLLAMA_BASE_URL.rstrip('/')}{path}", data=data,
        headers={"Content-Type": "application/json"} if data else {}, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else {}


async def list_models() -> dict:
    """Installed local Ollama models — name, size, family/parameter-size/
    quantization (from Ollama's own model metadata), last-modified."""
    try:
        data = await asyncio.to_thread(_http_json, "/api/tags")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e), "models": []}
    models = []
    for m in data.get("models", []):
        details = m.get("details", {})
        models.append({
            "name": m.get("name"), "size": m.get("size"),
            "family": details.get("family"), "parameter_size": details.get("parameter_size"),
            "quantization": details.get("quantization_level"), "modified_at": m.get("modified_at"),
        })
    return {"ok": True, "models": models}


def _pull_worker(tag: str) -> None:
    """Runs in a thread executor: reads Ollama's newline-delimited-JSON pull
    progress stream, updating the shared _pulls[tag] dict as each line
    arrives so pull_status() always reflects the latest state."""
    try:
        req = urllib.request.Request(
            f"{OLLAMA_BASE_URL.rstrip('/')}/api/pull",
            data=json.dumps({"name": tag}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=1800) as resp:
            for raw in resp:
                line = raw.decode(errors="replace").strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                state = _pulls[tag]
                state["status"] = evt.get("status", state["status"])
                if "completed" in evt:
                    state["completed"] = evt["completed"]
                if "total" in evt:
                    state["total"] = evt["total"]
                if evt.get("error"):
                    state["error"] = evt["error"]
        _pulls[tag]["done"] = True
    except Exception as e:  # noqa: BLE001
        _pulls[tag]["error"] = str(e)
        _pulls[tag]["done"] = True


async def pull_model(tag: str) -> dict:
    """Kicks off a background pull and returns immediately — poll
    pull_status(tag) for progress. Refuses to start a second pull for the
    same tag while one's already running."""
    existing = _pulls.get(tag)
    if existing and not existing.get("done"):
        return {"ok": False, "error": f"a pull for {tag!r} is already in progress"}
    _pulls[tag] = {"status": "starting", "completed": 0, "total": 0, "error": None, "done": False}
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, _pull_worker, tag)
    return {"ok": True, "started": True}


async def pull_status(tag: str) -> dict:
    state = _pulls.get(tag)
    if state is None:
        return {"ok": False, "error": "no pull started for this tag"}
    return {"ok": True, **state}


async def delete_model(tag: str) -> dict:
    """Remove a locally-pulled model to free disk space."""
    try:
        await asyncio.to_thread(_http_json, "/api/delete", method="DELETE", body={"name": tag})
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
    return {"ok": True}
