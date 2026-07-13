"""
Fleet-push self-update: bundle extraction, backup, apply, rollback, and
the safety checks around them (zip-slip, missing agent.py, backup
pruning). This is code that overwrites its own install directory — it
gets real test coverage precisely because a bug here is a bricked box.
"""
from __future__ import annotations

import base64
import io
import os
import zipfile

import pytest

import selfupdate

pytestmark = pytest.mark.asyncio


def _make_bundle(files: dict[str, str]) -> str:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return base64.b64encode(buf.getvalue()).decode("ascii")


@pytest.fixture
def fake_install(tmp_path, monkeypatch):
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    (install_dir / "agent.py").write_text("VERSION_MARKER = 'old'\n")
    (install_dir / "VERSION").write_text("2026.01.01")
    data_dir = install_dir / "data"
    data_dir.mkdir()
    (data_dir / "connections.json").write_text("important-runtime-state")

    monkeypatch.setattr(selfupdate, "INSTALL_DIR", str(install_dir))
    monkeypatch.setattr(selfupdate, "BACKUP_ROOT", str(install_dir / "data" / ".update_backups"))
    monkeypatch.setattr(selfupdate, "STAGING_DIR", str(install_dir / "data" / ".update_staging"))
    return install_dir


async def test_apply_update_replaces_agent_py_and_version(fake_install):
    bundle = _make_bundle({"agent.py": "VERSION_MARKER = 'new'\n", "VERSION": "2026.07.13"})
    result = await selfupdate.apply_update(bundle, "2026.07.13")
    assert result["ok"] is True
    assert result["from_version"] == "2026.01.01"
    assert result["to_version"] == "2026.07.13"
    assert "new" in (fake_install / "agent.py").read_text()
    assert selfupdate.current_version() == "2026.07.13"


async def test_apply_update_preserves_data_dir(fake_install):
    bundle = _make_bundle({"agent.py": "x = 1\n", "VERSION": "2026.07.13"})
    await selfupdate.apply_update(bundle, "2026.07.13")
    assert (fake_install / "data" / "connections.json").read_text() == "important-runtime-state"


async def test_apply_update_adds_new_files_from_bundle(fake_install):
    bundle = _make_bundle({"agent.py": "x = 1\n", "VERSION": "2026.07.13", "newmodule.py": "# new"})
    await selfupdate.apply_update(bundle, "2026.07.13")
    assert (fake_install / "newmodule.py").exists()


async def test_apply_update_removes_files_no_longer_in_bundle(fake_install):
    (fake_install / "oldmodule.py").write_text("# stale, should be removed")
    bundle = _make_bundle({"agent.py": "x = 1\n", "VERSION": "2026.07.13"})
    await selfupdate.apply_update(bundle, "2026.07.13")
    assert not (fake_install / "oldmodule.py").exists()


async def test_apply_update_rejects_bundle_missing_agent_py(fake_install):
    bundle = _make_bundle({"VERSION": "2026.07.13", "README.md": "no agent.py here"})
    result = await selfupdate.apply_update(bundle, "2026.07.13")
    assert result["ok"] is False
    assert "agent.py" in result["error"]
    # the old install must be untouched
    assert "old" in (fake_install / "agent.py").read_text()


async def test_apply_update_rejects_zip_slip(fake_install):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../../evil.py", "pwned")
    evil_bundle = base64.b64encode(buf.getvalue()).decode("ascii")
    result = await selfupdate.apply_update(evil_bundle, "evil")
    assert result["ok"] is False
    assert "unsafe path" in result["error"]
    assert "old" in (fake_install / "agent.py").read_text()  # untouched


async def test_apply_update_creates_a_backup(fake_install):
    bundle = _make_bundle({"agent.py": "x = 1\n", "VERSION": "2026.07.13"})
    await selfupdate.apply_update(bundle, "2026.07.13")
    backups = selfupdate.list_backups()
    assert len(backups) == 1
    assert backups[0].startswith("2026.01.01_")


async def test_rollback_restores_previous_version_and_removes_added_files(fake_install):
    bundle = _make_bundle({"agent.py": "VERSION_MARKER = 'new'\n", "VERSION": "2026.07.13", "newmodule.py": "# new"})
    await selfupdate.apply_update(bundle, "2026.07.13")

    result = await selfupdate.rollback()
    assert result["ok"] is True
    assert result["restored_version"] == "2026.01.01"
    assert "old" in (fake_install / "agent.py").read_text()
    assert selfupdate.current_version() == "2026.01.01"
    assert not (fake_install / "newmodule.py").exists()


async def test_rollback_preserves_data_dir(fake_install):
    bundle = _make_bundle({"agent.py": "x = 1\n", "VERSION": "2026.07.13"})
    await selfupdate.apply_update(bundle, "2026.07.13")
    await selfupdate.rollback()
    assert (fake_install / "data" / "connections.json").read_text() == "important-runtime-state"


async def test_rollback_with_no_backups_fails_cleanly(fake_install):
    result = await selfupdate.rollback()
    assert result["ok"] is False
    assert "no backups" in result["error"]


async def test_backup_pruning_keeps_only_the_most_recent(fake_install, monkeypatch):
    monkeypatch.setattr(selfupdate, "BACKUPS_TO_KEEP", 2)
    for v in ("2026.02.01", "2026.03.01", "2026.04.01"):
        bundle = _make_bundle({"agent.py": f"VERSION_MARKER = '{v}'\n", "VERSION": v})
        result = await selfupdate.apply_update(bundle, v)
        assert result["ok"] is True
    assert len(selfupdate.list_backups()) == 2


async def test_apply_update_failure_mid_apply_restores_previous(fake_install, monkeypatch):
    """If something goes wrong applying the staged files, the install must
    end up back on the version it started from — not half-updated."""
    bundle = _make_bundle({"agent.py": "VERSION_MARKER = 'new'\n", "VERSION": "2026.07.13"})

    real_apply_from = selfupdate._apply_from
    calls = {"n": 0}

    def flaky_apply_from(source_dir):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("simulated disk error mid-apply")
        return real_apply_from(source_dir)

    monkeypatch.setattr(selfupdate, "_apply_from", flaky_apply_from)
    result = await selfupdate.apply_update(bundle, "2026.07.13")
    assert result["ok"] is False
    assert "old" in (fake_install / "agent.py").read_text()
