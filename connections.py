"""
Named connections/tokens for agent.py's --listen (server) mode.

A "connection" is just a label + a bearer token the control plane presents
when it dials in. Multiple can exist and be simultaneously active (e.g. one
per environment, or a second dashboard just watching this box) — the first
to connect drives the device, every later one is accepted read-only (see
agent.py's Agent.observers); each token can still be individually disabled
or revoked from the local admin panel without restarting the agent.

Each token also carries a `scope` — "local", "remote", or "both" — checked
against where the connecting peer's IP actually classifies (see netinfo.py),
so a token minted for use on the LAN can't also be used to reach in from the
open internet/Tailscale, and vice versa.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
from typing import Optional

DEFAULT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "connections.json")
VALID_SCOPES = {"local", "remote", "both"}


def _gen_token() -> str:
    return secrets.token_urlsafe(32)


class ConnectionStore:
    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path or DEFAULT_PATH
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        if not os.path.exists(self.path):
            self._save([])

    def _load(self) -> list[dict]:
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:  # noqa: BLE001
            return []

    def _save(self, rows: list[dict]) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rows, f, indent=2)
        os.replace(tmp, self.path)

    def list_all(self) -> list[dict]:
        return self._load()

    def create(self, name: str, scope: str = "both") -> dict:
        if scope not in VALID_SCOPES:
            scope = "both"
        with self._lock:
            rows = self._load()
            next_id = (max((r["id"] for r in rows), default=0)) + 1
            row = {"id": next_id, "name": name.strip() or f"connection-{next_id}",
                   "token": _gen_token(), "scope": scope, "enabled": True,
                   "created_at": time.time(), "last_seen": None, "last_ip": None, "last_scope": None}
            rows.append(row)
            self._save(rows)
            return row

    def toggle(self, conn_id: int, enabled: bool) -> bool:
        with self._lock:
            rows = self._load()
            for r in rows:
                if r["id"] == conn_id:
                    r["enabled"] = enabled
                    self._save(rows)
                    return True
            return False

    def set_scope(self, conn_id: int, scope: str) -> bool:
        if scope not in VALID_SCOPES:
            return False
        with self._lock:
            rows = self._load()
            for r in rows:
                if r["id"] == conn_id:
                    r["scope"] = scope
                    self._save(rows)
                    return True
            return False

    def delete(self, conn_id: int) -> bool:
        with self._lock:
            rows = self._load()
            kept = [r for r in rows if r["id"] != conn_id]
            if len(kept) == len(rows):
                return False
            self._save(kept)
            return True

    def get_token(self, conn_id: int) -> Optional[str]:
        """The full bearer token, for the admin panel's on-demand "show" —
        deliberately not part of the list response (see adminpanel._public),
        so it's never sent to the browser until the operator asks for it."""
        for r in self._load():
            if r["id"] == conn_id:
                return r["token"]
        return None

    def check_token(
        self, token: str, peer_ip: str | None = None, peer_scope: str = "both",
    ) -> tuple[bool, Optional[str], Optional[dict]]:
        """Validates the bearer token AND that `peer_scope` (the classification
        of wherever this connection is physically coming from — see
        netinfo.classify_peer) is permitted by that token's own scope. On
        success, also returns the matched connection's public row (id/name)
        so the caller can record "connected using which token"."""
        if not token:
            return False, "missing token", None
        with self._lock:
            rows = self._load()
            for r in rows:
                if secrets.compare_digest(r["token"], token):
                    if not r["enabled"]:
                        return False, "this connection is disabled", None
                    scope = r.get("scope", "both")
                    if scope != "both" and scope != peer_scope:
                        return False, f"this token only permits {scope} connections", None
                    r["last_seen"] = time.time()
                    r["last_ip"] = peer_ip
                    r["last_scope"] = peer_scope
                    self._save(rows)
                    return True, None, {"id": r["id"], "name": r["name"]}
            return False, "invalid token", None
