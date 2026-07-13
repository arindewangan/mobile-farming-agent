"""Small persisted settings for agent.py's --listen mode admin panel — right
now just the global "accept connections from outside my LAN at all" switch,
which sits above per-connection scope as a blanket kill switch."""
from __future__ import annotations

import json
import os
from typing import Optional

DEFAULT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "agent_settings.json")

_DEFAULTS = {"allow_remote": True}


class SettingsStore:
    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path or DEFAULT_PATH
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        if not os.path.exists(self.path):
            self._save(dict(_DEFAULTS))

    def _load(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as f:
                return {**_DEFAULTS, **json.load(f)}
        except Exception:  # noqa: BLE001
            return dict(_DEFAULTS)

    def _save(self, data: dict) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, self.path)

    def get_all(self) -> dict:
        return self._load()

    def set_allow_remote(self, allow: bool) -> None:
        data = self._load()
        data["allow_remote"] = allow
        self._save(data)
