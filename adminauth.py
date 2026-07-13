"""
Guards the admin panel itself — creating a connection mints a token that
grants full device control, so the panel can't be left wide open just
because it happens to be reachable over Tailscale. A random password is
generated on first run (printed to the console) and unlocks a short-lived
session, mirroring how the panel used to gate a single fixed token.
"""
from __future__ import annotations

import os
import secrets

DEFAULT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "admin_password.txt")


def load_or_create_password(path: str | None = None) -> str:
    path = path or DEFAULT_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        return open(path, encoding="utf-8").read().strip()
    pw = secrets.token_urlsafe(12)
    with open(path, "w", encoding="utf-8") as f:
        f.write(pw)
    print(f"[agent admin] generated an admin panel password: {pw}")
    print(f"[agent admin] (also saved to {path} — re-run to see it again)")
    return pw


class SessionAuth:
    def __init__(self, password: str, path: str | None = None) -> None:
        self.password = password
        self.path = path or DEFAULT_PATH
        self._sessions: set[str] = set()

    def login(self, password: str) -> str | None:
        if not secrets.compare_digest(password, self.password):
            return None
        sid = secrets.token_urlsafe(24)
        self._sessions.add(sid)
        return sid

    def logout(self, sid: str | None) -> None:
        if sid:
            self._sessions.discard(sid)

    def is_valid(self, sid: str | None) -> bool:
        return bool(sid) and sid in self._sessions

    def change_password(self, old: str, new: str) -> str | None:
        """Verifies `old`, persists `new` to disk, and drops every existing
        session (including the caller's) so a stolen/shared old session
        can't outlive a password rotation. Returns a fresh session id for
        the caller to keep them logged in, or None if `old` was wrong."""
        if not secrets.compare_digest(old, self.password):
            return None
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(new)
        self.password = new
        self._sessions.clear()
        sid = secrets.token_urlsafe(24)
        self._sessions.add(sid)
        return sid
