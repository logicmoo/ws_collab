"""Worker prompt editing with versioning, diff, and rollback (tasks 7 & 16).

``long_running_prompt.txt`` is treated as a versioned artifact. Saves are atomic
(temp file + ``os.replace``), the previous text is always preserved, every
version is appended to the durable ``prompt`` history stream, and any version can
be diffed or rolled back to. Rollback is itself a new version, so history is
append-only and never rewritten.
"""

from __future__ import annotations

import difflib
import hashlib
import os
import threading
from pathlib import Path
from typing import Any, Callable

from .config import Config
from .errors import NotFoundError, ValidationError
from .events import STREAM_PROMPT, utc_now_iso

PublishFn = Callable[..., dict[str, Any]]


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class PromptManager:
    def __init__(self, config: Config, publish: PublishFn, read_history: Callable[[], list[dict[str, Any]]] | None = None):
        self.config = config
        self._publish = publish
        self._read_history = read_history or (lambda: [])
        self.path = Path(config.prompt_path)
        self._lock = threading.RLock()
        self._version = 0

    def _current_version(self) -> int:
        history = self._read_history()
        versions = [int(item.get("data", {}).get("version", 0)) for item in history]
        return max(versions, default=0)

    def current(self) -> dict[str, Any]:
        with self._lock:
            text = self.path.read_text(encoding="utf-8") if self.path.is_file() else ""
            return {
                "text": text,
                "version": self._current_version(),
                "hash": _hash(text),
                "exists": self.path.is_file(),
                "path": str(self.path),
            }

    def validate(self, text: str) -> None:
        if not isinstance(text, str):
            raise ValidationError("prompt text must be a string")
        if len(text.encode("utf-8")) > self.config.max_body_bytes:
            raise ValidationError("prompt text exceeds the maximum body size")

    def save(self, text: str, *, operator: str = "operator", note: str = "") -> dict[str, Any]:
        self.validate(text)
        with self._lock:
            previous = self.path.read_text(encoding="utf-8") if self.path.is_file() else ""
            version = self._current_version() + 1
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, self.path)
            diff = self._unified(previous, text)
        self._publish(
            stream=STREAM_PROMPT,
            type="PROMPT_SAVED",
            data={
                "version": version,
                "text": text,
                "hash": _hash(text),
                "previous_hash": _hash(previous),
                "note": note,
                "operator": operator,
                "saved_at": utc_now_iso(),
            },
            source_id=operator,
            source_kind="operator",
        )
        return {"version": version, "hash": _hash(text), "diff": diff}

    def history(self) -> list[dict[str, Any]]:
        items = []
        for event in self._read_history():
            data = event.get("data", {})
            items.append(
                {
                    "version": data.get("version"),
                    "hash": data.get("hash"),
                    "note": data.get("note", ""),
                    "operator": data.get("operator", ""),
                    "saved_at": data.get("saved_at", event.get("ts")),
                    "event_id": event.get("id"),
                }
            )
        return items

    def version_text(self, version: int) -> str:
        for event in self._read_history():
            data = event.get("data", {})
            if int(data.get("version", -1)) == version:
                return data.get("text", "")
        raise NotFoundError(f"prompt version {version} not found")

    def diff(self, version_a: int, version_b: int) -> str:
        return self._unified(self.version_text(version_a), self.version_text(version_b), label_a=f"v{version_a}", label_b=f"v{version_b}")

    def preview_diff(self, text: str) -> str:
        return self._unified(self.current()["text"], text, label_a="current", label_b="proposed")

    def rollback(self, version: int, *, operator: str = "operator") -> dict[str, Any]:
        text = self.version_text(version)
        return self.save(text, operator=operator, note=f"rollback to v{version}")

    @staticmethod
    def _unified(a: str, b: str, *, label_a: str = "previous", label_b: str = "current") -> str:
        return "".join(
            difflib.unified_diff(
                a.splitlines(keepends=True),
                b.splitlines(keepends=True),
                fromfile=label_a,
                tofile=label_b,
            )
        )
