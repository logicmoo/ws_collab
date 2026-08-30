"""Persistent Meet browser settings for the next bridge launch."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any


class MeetBrowserSettings:
    """A small atomic JSON store for Meet browser launch preferences."""

    _PROFILES_KEY = "profiles"
    _LEGACY_PROFILES_KEY = "shared_profiles"

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
        for attempt in range(6):
            try:
                os.replace(tmp, self.path)
                return
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.02 * (2**attempt))

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

    def _profiles_copy(self) -> dict[str, Any]:
        raw = self.get(self._PROFILES_KEY, self.get(self._LEGACY_PROFILES_KEY, {}))
        return json.loads(json.dumps(raw)) if isinstance(raw, dict) else {}

    def get_profile_state(self, profile_path: Path | str) -> dict[str, Any]:
        key = str(Path(profile_path).expanduser())
        profiles = self._profiles_copy()
        state = profiles.get(key, {})
        if not isinstance(state, dict):
            state = {}
        accounts = state.get("accounts", {})
        role_account_map = state.get("role_account_map", {})
        meeting_role_account_maps = state.get("meeting_role_account_maps", {})
        companion_click = state.get("companion_click", {})
        meeting_companion_click = state.get("meeting_companion_click", {})
        known_meeting_urls = state.get("known_meeting_urls", [])
        return json.loads(json.dumps({
            "accounts": accounts if isinstance(accounts, dict) else {},
            "role_account_map": role_account_map if isinstance(role_account_map, dict) else {},
            "meeting_role_account_maps": (
                meeting_role_account_maps if isinstance(meeting_role_account_maps, dict) else {}
            ),
            "companion_click": companion_click if isinstance(companion_click, dict) else {},
            "meeting_companion_click": (
                meeting_companion_click if isinstance(meeting_companion_click, dict) else {}
            ),
            "known_meeting_urls": known_meeting_urls if isinstance(known_meeting_urls, list) else [],
        }))

    def set_profile_state(
        self,
        profile_path: Path | str,
        *,
        accounts: dict[str, Any] | None = None,
        role_account_map: dict[str, Any] | None = None,
        meeting_role_account_maps: dict[str, Any] | None = None,
        companion_click: dict[str, Any] | None = None,
        meeting_companion_click: dict[str, Any] | None = None,
        known_meeting_urls: list[str] | None = None,
    ) -> dict[str, Any]:
        key = str(Path(profile_path).expanduser())
        with self._lock:
            profiles = self._profiles_copy()
            state = profiles.get(key, {})
            if not isinstance(state, dict):
                state = {}
            if accounts is not None:
                state["accounts"] = json.loads(json.dumps(accounts))
            if role_account_map is not None:
                state["role_account_map"] = json.loads(json.dumps(role_account_map))
            if meeting_role_account_maps is not None:
                state["meeting_role_account_maps"] = json.loads(json.dumps(meeting_role_account_maps))
            if companion_click is not None:
                state["companion_click"] = json.loads(json.dumps(companion_click))
            if meeting_companion_click is not None:
                state["meeting_companion_click"] = json.loads(json.dumps(meeting_companion_click))
            if known_meeting_urls is not None:
                state["known_meeting_urls"] = json.loads(json.dumps(known_meeting_urls))
            profiles[key] = state
            self._data[self._PROFILES_KEY] = profiles
            self._data.pop(self._LEGACY_PROFILES_KEY, None)
            self._save()
        return self.get_profile_state(key)

    def clear_profile_state(self, profile_path: Path | str) -> None:
        key = str(Path(profile_path).expanduser())
        with self._lock:
            profiles = self._profiles_copy()
            profiles.pop(key, None)
            if profiles:
                self._data[self._PROFILES_KEY] = profiles
            else:
                self._data.pop(self._PROFILES_KEY, None)
            self._data.pop(self._LEGACY_PROFILES_KEY, None)
            self._save()
