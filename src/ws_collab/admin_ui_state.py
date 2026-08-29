"""Atomic server-side persistence for admin UI page state."""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import ValidationError


_PAGE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_SENSITIVE_KEY_RE = re.compile(
    r"(authorization|cookie|credential|password|secret|sessionstorage|access.?token|"
    r"refresh.?token|api.?key|(?:^|[_-])token(?:$|[_-]))",
    re.IGNORECASE,
)


class AdminUIState:
    """Persist page-keyed JSON snapshots without retaining credentials."""

    _MAX_PAGE_BYTES = 20 * 1024 * 1024

    def __init__(self, directory: Path | str):
        self.directory = Path(directory)
        self.path = self.directory / "admin_ui_state.json"
        self._lock = threading.RLock()
        self._pages: dict[str, dict[str, Any]] = {}
        self._load()

    @staticmethod
    def _page_key(page: str) -> str:
        key = str(page or "").strip().lower()
        if not _PAGE_RE.fullmatch(key):
            raise ValidationError("invalid admin page id")
        return key

    @classmethod
    def _sanitize(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                str(key): cls._sanitize(child)
                for key, child in value.items()
                if not _SENSITIVE_KEY_RE.search(str(key))
            }
        if isinstance(value, list):
            return [cls._sanitize(child) for child in value]
        if value is None or isinstance(value, (bool, int, float, str)):
            return value
        raise ValidationError("admin UI state must contain only JSON values")

    def _load(self) -> None:
        with self._lock:
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raw = {}
            pages = raw.get("pages", {}) if isinstance(raw, dict) else {}
            self._pages = pages if isinstance(pages, dict) else {}

    def _save(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "pages": self._pages}
        temp = self.path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        for attempt in range(6):
            try:
                os.replace(temp, self.path)
                return
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.02 * (2**attempt))

    def get_page(self, page: str) -> dict[str, Any]:
        key = self._page_key(page)
        with self._lock:
            entry = self._pages.get(key)
            if not isinstance(entry, dict):
                return {"page": key, "exists": False, "state": {}}
            return {
                "page": key,
                "exists": True,
                "updated_at": entry.get("updated_at"),
                "state": json.loads(json.dumps(entry.get("state", {}))),
            }

    def set_page(self, page: str, state: Any) -> dict[str, Any]:
        key = self._page_key(page)
        if not isinstance(state, dict):
            raise ValidationError("admin UI page state must be an object")
        cleaned = self._sanitize(state)
        encoded = json.dumps(cleaned, separators=(",", ":")).encode("utf-8")
        if len(encoded) > self._MAX_PAGE_BYTES:
            raise ValidationError("admin UI page state exceeds the 20 MiB limit")
        with self._lock:
            self._pages[key] = {
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "state": cleaned,
            }
            self._save()
        return self.get_page(key)
