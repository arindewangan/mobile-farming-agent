"""
ollamaadmin: local Ollama model management (list/pull/delete) — backs the AI
Model Registry settings page. Ollama always runs on the same host as this
agent process, so these are agent-level actions, not something the backend
can reach directly.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import pytest

import ollamaadmin

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _reset_pulls():
    """_pulls is module-level state shared across calls (that's the point —
    pull_status() reads whatever pull_model() kicked off) — clear it between
    tests so they don't see each other's leftover state."""
    ollamaadmin._pulls.clear()
    yield
    ollamaadmin._pulls.clear()


async def test_list_models_reports_installed_tags(monkeypatch):
    fake_response = {
        "models": [
            {"name": "qwen3:latest", "size": 4_000_000_000, "modified_at": "2026-01-01T00:00:00Z",
             "details": {"family": "qwen3", "parameter_size": "8B", "quantization_level": "Q4_K_M"}},
        ]
    }
    monkeypatch.setattr(ollamaadmin, "_http_json", lambda path, **kw: fake_response)

    result = await ollamaadmin.list_models()
    assert result["ok"] is True
    assert result["models"] == [{
        "name": "qwen3:latest", "size": 4_000_000_000, "family": "qwen3",
        "parameter_size": "8B", "quantization": "Q4_K_M", "modified_at": "2026-01-01T00:00:00Z",
    }]


async def test_list_models_reports_a_connection_failure_cleanly(monkeypatch):
    def raising(*a, **kw):
        raise ConnectionRefusedError("no ollama running")
    monkeypatch.setattr(ollamaadmin, "_http_json", raising)

    result = await ollamaadmin.list_models()
    assert result["ok"] is False
    assert "no ollama running" in result["error"]
    assert result["models"] == []


async def test_pull_status_reports_nothing_started_for_an_unknown_tag():
    result = await ollamaadmin.pull_status("never-pulled")
    assert result == {"ok": False, "error": "no pull started for this tag"}


async def test_pull_model_refuses_a_second_concurrent_pull_of_the_same_tag():
    ollamaadmin._pulls["qwen3"] = {"status": "downloading", "completed": 1, "total": 10, "error": None, "done": False}
    result = await ollamaadmin.pull_model("qwen3")
    assert result["ok"] is False
    assert "already in progress" in result["error"]


async def test_pull_model_allows_retrying_a_previously_finished_pull(monkeypatch):
    ollamaadmin._pulls["qwen3"] = {"status": "success", "completed": 10, "total": 10, "error": None, "done": True}
    monkeypatch.setattr(ollamaadmin, "_pull_worker", lambda tag: None)  # don't actually hit the network

    result = await ollamaadmin.pull_model("qwen3")
    assert result == {"ok": True, "started": True}
    assert ollamaadmin._pulls["qwen3"]["done"] is False  # reset for the new attempt


async def test_pull_worker_updates_progress_as_ndjson_lines_arrive(monkeypatch):
    """Confirms pull_status() reflects live progress while a pull runs, not
    just its final state — the whole reason this is a background-task-plus-
    poll design instead of one blocking call."""
    lines = [
        json.dumps({"status": "pulling manifest"}),
        json.dumps({"status": "downloading", "completed": 50, "total": 100}),
        json.dumps({"status": "success"}),
    ]

    class FakeResponse:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def __iter__(self):
            return iter((ln + "\n").encode() for ln in lines)

    with patch("urllib.request.urlopen", return_value=FakeResponse()):
        ollamaadmin._pulls["qwen3"] = {"status": "starting", "completed": 0, "total": 0, "error": None, "done": False}
        await asyncio.to_thread(ollamaadmin._pull_worker, "qwen3")

    state = ollamaadmin._pulls["qwen3"]
    assert state["status"] == "success"
    assert state["completed"] == 50
    assert state["total"] == 100
    assert state["done"] is True
    assert state["error"] is None


async def test_pull_worker_records_a_network_error(monkeypatch):
    def raising(*a, **kw):
        raise TimeoutError("connection timed out")
    with patch("urllib.request.urlopen", side_effect=raising):
        ollamaadmin._pulls["qwen3"] = {"status": "starting", "completed": 0, "total": 0, "error": None, "done": False}
        await asyncio.to_thread(ollamaadmin._pull_worker, "qwen3")

    state = ollamaadmin._pulls["qwen3"]
    assert state["done"] is True
    assert "connection timed out" in state["error"]


async def test_delete_model_calls_the_ollama_delete_endpoint(monkeypatch):
    calls = []
    monkeypatch.setattr(ollamaadmin, "_http_json", lambda path, **kw: calls.append((path, kw)))

    result = await ollamaadmin.delete_model("qwen3")
    assert result == {"ok": True}
    assert calls == [("/api/delete", {"method": "DELETE", "body": {"name": "qwen3"}})]


async def test_delete_model_reports_a_failure_cleanly(monkeypatch):
    def raising(*a, **kw):
        raise ValueError("model not found")
    monkeypatch.setattr(ollamaadmin, "_http_json", raising)

    result = await ollamaadmin.delete_model("nonexistent")
    assert result == {"ok": False, "error": "model not found"}
