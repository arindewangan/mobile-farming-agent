"""
Fleet-wide push updates for remote/edge agents — before this, upgrading an
edge device meant manually re-running setup.sh (which itself needs fresh
code already sitting on that box first) on each box individually, one at a
time, over SSH/physical access.

Deliberately NOT git-based: this project isn't hosted anywhere a Pi could
`git pull` from, so the control plane pushing a zip of its own current
`agent/` source over the same WebSocket connection every command already
flows through is the only channel that reliably reaches a remote box
that's already connected (Tailscale/LAN). The control plane bundles +
pushes (see backend/app/agentupdate.py); this module receives it, backs
the current install up (for rollback), swaps the new files in, best-effort
reinstalls dependencies if requirements.txt changed, and exits with a
non-zero code so systemd's `Restart=always` relaunches the process running
the new code (see mobilefarm-agent.service — this REQUIRES that setting;
a clean exit(0) would not trigger a restart under Restart=on-failure).

Known limitation: this doesn't handle changes to the systemd unit file
itself or to setup.sh's OS-package prerequisites — those still need a
manual setup.sh re-run. It covers the common case (an agent/*.py code
change), which is the vast majority of updates in practice.
"""
from __future__ import annotations

import asyncio
import base64
import io
import os
import shutil
import time
import zipfile

INSTALL_DIR = os.path.dirname(os.path.abspath(__file__))
BACKUP_ROOT = os.path.join(INSTALL_DIR, "data", ".update_backups")
STAGING_DIR = os.path.join(INSTALL_DIR, "data", ".update_staging")
UPDATE_EXIT_CODE = 42  # any non-zero code works; distinct value aids reading journalctl
BACKUPS_TO_KEEP = 3

# Runtime state, never part of a push bundle and never touched by apply/rollback.
PRESERVE = {"data", ".venv", "__pycache__"}


def current_version() -> str:
    try:
        with open(os.path.join(INSTALL_DIR, "VERSION"), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return "unknown"


def _extract_bundle(bundle_b64: str, dest: str) -> None:
    if os.path.isdir(dest):
        shutil.rmtree(dest)
    os.makedirs(dest, exist_ok=True)
    blob = base64.b64decode(bundle_b64)
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        dest_abs = os.path.abspath(dest)
        for member in zf.namelist():
            target = os.path.abspath(os.path.join(dest, member))
            if target != dest_abs and not target.startswith(dest_abs + os.sep):
                raise ValueError(f"unsafe path in update bundle: {member}")
        zf.extractall(dest)


def _copy_tree_excluding_preserve(src_root: str, dst_root: str) -> None:
    for name in os.listdir(src_root):
        if name in PRESERVE:
            continue
        src, dst = os.path.join(src_root, name), os.path.join(dst_root, name)
        if os.path.isdir(dst):
            shutil.rmtree(dst)
        elif os.path.isfile(dst):
            os.remove(dst)
        if os.path.isdir(src):
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)


def _backup_current(label: str) -> str:
    os.makedirs(BACKUP_ROOT, exist_ok=True)
    backup_dir = os.path.join(BACKUP_ROOT, label)
    if os.path.isdir(backup_dir):
        shutil.rmtree(backup_dir)
    os.makedirs(backup_dir)
    for name in os.listdir(INSTALL_DIR):
        if name in PRESERVE:
            continue
        src = os.path.join(INSTALL_DIR, name)
        dst = os.path.join(backup_dir, name)
        if os.path.isdir(src):
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)
    return backup_dir


def _prune_old_backups(keep: int | None = None) -> None:
    # `keep: int = BACKUPS_TO_KEEP` as a default argument would capture the
    # module constant's value once at function-definition time, not when
    # this actually runs — reading it in the body instead means a future
    # runtime-configurable retention setting (or a test monkeypatching the
    # module constant) takes effect correctly.
    if keep is None:
        keep = BACKUPS_TO_KEEP
    if not os.path.isdir(BACKUP_ROOT):
        return
    backups = sorted(
        (d for d in os.listdir(BACKUP_ROOT) if os.path.isdir(os.path.join(BACKUP_ROOT, d))), reverse=True)
    for stale in backups[keep:]:
        shutil.rmtree(os.path.join(BACKUP_ROOT, stale), ignore_errors=True)


def _apply_from(source_dir: str) -> None:
    """Mirrors `source_dir` onto INSTALL_DIR (minus PRESERVE): copies
    everything present in the source, and removes anything under
    INSTALL_DIR that's no longer present there — a file genuinely deleted
    upstream should disappear here too, not linger as dead code."""
    incoming = {n for n in os.listdir(source_dir) if n not in PRESERVE}
    for name in list(os.listdir(INSTALL_DIR)):
        if name in PRESERVE or name in incoming:
            continue
        target = os.path.join(INSTALL_DIR, name)
        if os.path.isdir(target):
            shutil.rmtree(target)
        else:
            os.remove(target)
    _copy_tree_excluding_preserve(source_dir, INSTALL_DIR)


async def _reinstall_deps() -> None:
    """Best-effort — a failure here doesn't undo the code update; it just
    means a genuinely new dependency won't be available until fixed
    manually (`sudo -u <user> INSTALL_DIR/.venv/bin/pip install -r
    requirements.txt`). Most updates don't touch requirements.txt at all,
    so this is typically a fast no-op."""
    pip = os.path.join(INSTALL_DIR, ".venv", "bin", "pip")
    req = os.path.join(INSTALL_DIR, "requirements.txt")
    if not (os.path.isfile(pip) and os.path.isfile(req)):
        return
    try:
        proc = await asyncio.create_subprocess_exec(
            pip, "install", "--quiet", "-r", req,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=120)
    except Exception:  # noqa: BLE001
        pass


async def install_from(source_dir: str, new_version: str) -> dict:
    """Shared apply core: validates `source_dir` looks like a real agent
    source tree, backs the current install up, swaps the new files in,
    best-effort reinstalls deps, prunes old backups. Used by both
    apply_update() (a bundle pushed over the websocket) and
    githubupdate.apply_latest() (a tag downloaded from a GitHub release) —
    from here on, it doesn't matter where the new source came from. Does NOT
    restart — the caller sends this result back first, then calls
    schedule_restart()."""
    if not os.path.isfile(os.path.join(source_dir, "agent.py")):
        return {"ok": False, "error": "update source is missing agent.py — refusing to apply"}

    old_version = current_version()
    try:
        backup_dir = _backup_current(f"{old_version}_{int(time.time())}")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"backup failed, update NOT applied: {e}"}

    try:
        _apply_from(source_dir)
    except Exception as e:  # noqa: BLE001
        restored = False
        try:
            _apply_from(backup_dir)  # best-effort restore of what was just backed up
            restored = True
        except Exception:  # noqa: BLE001
            pass
        if restored:
            return {"ok": False, "error": f"apply failed, restored previous version: {e}", "restored": True}
        return {"ok": False, "error": f"apply failed AND restore also failed, "
                                       f"INSTALL_DIR is left broken and needs manual recovery: {e}",
                "restored": False}

    await _reinstall_deps()
    _prune_old_backups()
    return {"ok": True, "from_version": old_version, "to_version": new_version, "restarting": True}


async def apply_update(bundle_b64: str, new_version: str) -> dict:
    """Validates + stages a bundle pushed over the websocket, then delegates
    to install_from(). See that function's docstring for the actual apply
    logic shared with the GitHub-pull path."""
    try:
        _extract_bundle(bundle_b64, STAGING_DIR)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"bad update bundle: {e}"}
    try:
        return await install_from(STAGING_DIR, new_version)
    finally:
        shutil.rmtree(STAGING_DIR, ignore_errors=True)


def list_backups() -> list[str]:
    if not os.path.isdir(BACKUP_ROOT):
        return []
    return sorted(
        (d for d in os.listdir(BACKUP_ROOT) if os.path.isdir(os.path.join(BACKUP_ROOT, d))), reverse=True)


async def rollback() -> dict:
    backups = list_backups()
    if not backups:
        return {"ok": False, "error": "no backups available to roll back to"}
    latest = backups[0]
    backup_dir = os.path.join(BACKUP_ROOT, latest)
    try:
        _apply_from(backup_dir)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"rollback failed: {e}"}
    await _reinstall_deps()
    shutil.rmtree(backup_dir, ignore_errors=True)
    return {"ok": True, "restored_version": latest.rsplit("_", 1)[0], "restarting": True}


def schedule_restart(delay: float = 1.5) -> None:
    """Fire-and-forget delayed process exit — gives the caller time to send
    the update result back over the websocket before the connection drops."""
    async def _go() -> None:
        await asyncio.sleep(delay)
        os._exit(UPDATE_EXIT_CODE)

    asyncio.create_task(_go())
