"""
GitHub-Releases-based updater — pulls the latest tagged release of this
agent's own repo instead of waiting for a control plane to push a bundle
over an active websocket connection (see selfupdate.py's own docstring for
why that push path exists: it's what reaches an edge box that's only ever
reachable over Tailscale/LAN, never the open internet). This is the
complementary PULL path — works from anywhere with outbound HTTPS, needs no
control-plane connection at all, and backs the "check for updates"/"update
now" affordance in the local admin panel and the main dashboard alike.

Reuses selfupdate.py's backup/apply/rollback machinery via install_from() —
the only difference from the push path is where the new source tree comes
from (a downloaded+extracted GitHub release archive, vs. a base64 zip
pushed by the backend).

Release tags are expected as `vX.Y.Z` (GitHub convention); this agent's own
VERSION file holds the bare `X.Y.Z` (no `v` prefix) — see current_version().
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import urllib.error
import urllib.request
import zipfile

import selfupdate

REPO = os.environ.get("MF_AGENT_REPO", "arindewangan/mobile-farming-agent")
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
ARCHIVE_URL = f"https://github.com/{REPO}/archive/refs/tags/{{tag}}.zip"
_UA = "mobile-farming-agent-updater"


def _get_json(url: str, timeout: float = 15.0) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — fixed https:// github.com URL
        return json.loads(resp.read())


def check_latest() -> dict:
    """{"ok", "current", "latest", "tag", "update_available", "url", "notes"}
    on success, or {"ok": False, "error", "current"} if the check itself
    failed (e.g. offline, rate-limited, or a private repo with no token)."""
    current = selfupdate.current_version()
    try:
        data = _get_json(API_LATEST)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"ok": False, "error": f"no releases published yet for {REPO}", "current": current}
        return {"ok": False, "error": f"GitHub API error: HTTP {e.code}", "current": current}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e), "current": current}
    tag = data.get("tag_name") or ""
    latest = tag[1:] if tag.startswith("v") else tag
    return {
        "ok": True, "current": current, "latest": latest, "tag": tag,
        "update_available": bool(latest) and latest != current,
        "url": data.get("html_url"),
        "published_at": data.get("published_at"),
        "notes": (data.get("body") or "")[:2000],
    }


def _download_and_extract(tag: str, work_dir: str) -> str:
    """Downloads the tag's auto-generated source archive (every GitHub
    release gets one for free, no custom asset upload needed) and extracts
    it, stripping the single wrapping top-level folder GitHub always adds
    (`<repo>-<tag-without-v>/`) so the returned dir directly contains
    agent.py etc. — the same shape selfupdate.py's own bundle extraction
    produces, which is what install_from() expects."""
    zip_path = os.path.join(work_dir, "src.zip")
    req = urllib.request.Request(ARCHIVE_URL.format(tag=tag), headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=120) as resp, open(zip_path, "wb") as f:  # noqa: S310
        shutil.copyfileobj(resp, f)

    extract_root = os.path.join(work_dir, "extracted")
    with zipfile.ZipFile(zip_path) as zf:
        # zip-slip guard, matching selfupdate.py's own _extract_bundle().
        for member in zf.namelist():
            if member.startswith("/") or ".." in member.replace("\\", "/").split("/"):
                raise ValueError(f"unsafe path in release archive: {member}")
        zf.extractall(extract_root)
    os.remove(zip_path)

    entries = os.listdir(extract_root)
    if len(entries) != 1 or not os.path.isdir(os.path.join(extract_root, entries[0])):
        raise ValueError(f"unexpected release archive layout: {entries}")
    return os.path.join(extract_root, entries[0])


async def apply_latest() -> dict:
    """Check, download, and apply the latest GitHub release in one call —
    backs the "update now" affordance. Reuses selfupdate.py's backup/apply,
    so a bad release is exactly as recoverable (selfupdate.rollback()) as a
    pushed one would be. Does NOT restart — same contract as
    selfupdate.apply_update(); the caller sends the result back, then calls
    selfupdate.schedule_restart()."""
    info = check_latest()
    if not info["ok"]:
        return {"ok": False, "error": info["error"]}
    if not info["update_available"]:
        return {"ok": False, "error": f"already on the latest version ({info['current']})"}

    work = tempfile.mkdtemp(prefix="mf_ghupdate_")
    try:
        src_dir = _download_and_extract(info["tag"], work)
        return await selfupdate.install_from(src_dir, info["latest"])
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"download/extract failed: {e}"}
    finally:
        shutil.rmtree(work, ignore_errors=True)
