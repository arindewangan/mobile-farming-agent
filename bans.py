"""IP ban list for agent.py's --listen mode — reject a connection outright
before it even gets to present a token, e.g. a leaked token being used from
an unexpected address."""
from __future__ import annotations

import json
import os
import time
from typing import Optional

DEFAULT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "banned_ips.json")


class BanStore:
    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path or DEFAULT_PATH
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

    def is_banned(self, ip: str) -> bool:
        return any(r["ip"] == ip for r in self._load())

    def ban(self, ip: str, reason: str = "") -> dict:
        rows = self._load()
        existing = next((r for r in rows if r["ip"] == ip), None)
        if existing:
            return existing
        row = {"ip": ip, "reason": reason.strip(), "banned_at": time.time()}
        rows.append(row)
        self._save(rows)
        return row

    def unban(self, ip: str) -> bool:
        rows = self._load()
        kept = [r for r in rows if r["ip"] != ip]
        if len(kept) == len(rows):
            return False
        self._save(kept)
        return True
