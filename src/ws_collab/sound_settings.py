"""Persistent sound settings.

Every audio setting an operator changes from the admin UI -- the active capture
(input) device, the default agent output device, and each STT driver's input
device -- is stored in a single JSON config file under the writable state
directory (``collab_state/sound_settings.json``) so the choices survive a
restart. Writes are atomic (temp file + ``os.replace``) and guarded by a lock,
matching the durability pattern used by cursors/routing/voices.

The engine-device map here is a durable, human-readable snapshot of the STT
driver -> device choices; the routing store remains the operational source of
truth for eligibility, and both are updated together whenever a route changes.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any


class SoundSettings:
    """A small atomic JSON store for audio settings that must persist."""

    def __init__(self, directory: Path | str):
        self.directory = Path(directory)
        self.path = self.directory / "sound_settings.json"
        self._lock = threading.RLock()
        self._data: dict[str, Any] = {}
        self._load()

    # ------------------------------------------------------------------ io
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

    # --------------------------------------------------------------- access
    def all(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._data))

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        """Set (or, when ``value`` is falsy/None, clear) a top-level key."""

        with self._lock:
            if value in (None, ""):
                self._data.pop(key, None)
            else:
                self._data[key] = value
            self._save()

    # ------------------------------------------------------- engine devices
    def get_engine_device(self, engine: str) -> str | None:
        with self._lock:
            return (self._data.get("engine_devices") or {}).get(engine)

    def set_engine_device(self, engine: str, device_id: str | None) -> None:
        with self._lock:
            mapping = dict(self._data.get("engine_devices") or {})
            if device_id:
                mapping[engine] = device_id
            else:
                mapping.pop(engine, None)
            if mapping:
                self._data["engine_devices"] = mapping
            else:
                self._data.pop("engine_devices", None)
            self._save()
