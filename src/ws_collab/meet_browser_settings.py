"""Persistent Meet browser settings for the next bridge launch."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any


class MeetBrowserSettings:
    """A small atomic JSON store for Meet browser launch preferences."""

    def __init__(self, directory: Path | str):
        self.directory = Path(directory)
        self.path = self.directory / "meet_browser_settings.json"
        self._lock = threading.RLock()
        self._data: dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        with self._lock:
            if self.path.is_file():
                try:
                    loaded = json.loads(self.path.read_text(encoding="utf-8"))
                    self._data = loaded if isinstance(loaded, dict) else {}
                except (OSError, ValueError):
                    self._data = {}
            else:
                self._data = {}

    def _save(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._data, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)

    def all(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._data))

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            if value in (None, ""):
                self._data.pop(key, None)
            else:
                self._data[key] = value
            self._save()
