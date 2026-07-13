"""Per-device visibility for agent.py's --listen mode — lets an operator hide
a specific ADB device from the main dashboard entirely (it still shows up
locally in this device's admin panel, and adb itself still sees it) without
unplugging it or disabling the whole connection."""
from __future__ import annotations

import json
import os
from typing import Optional

DEFAULT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "hidden_devices.json")


class HiddenDevicesStore:
    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path or DEFAULT_PATH
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        if not os.path.exists(self.path):
            self._save([])

    def _load(self) -> list[str]:
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:  # noqa: BLE001
            return []

    def _save(self, serials: list[str]) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(serials, f, indent=2)
        os.replace(tmp, self.path)

    def list_hidden(self) -> set[str]:
        return set(self._load())

    def is_hidden(self, serial: str) -> bool:
        return serial in self._load()

    def hide(self, serial: str) -> None:
        rows = self._load()
        if serial not in rows:
            rows.append(serial)
            self._save(rows)

    def show(self, serial: str) -> None:
        rows = self._load()
        if serial in rows:
            rows.remove(serial)
            self._save(rows)
