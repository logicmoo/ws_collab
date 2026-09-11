"""Always-on browser Web Speech caption source and supervisor.

The captioner is push driven: one dedicated same-origin page owns the microphone
and sends revisioned transcript envelopes here.  It is intentionally not an
``SttAdapter`` and is never invoked for captured audio segments.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import random
import re
import secrets
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from .errors import AuthenticationError, ValidationError, WsCollabError
from .events import utc_now_iso

CAPTIONER_ID = "browser_captioner"
CAPTIONER_DISPLAY_NAME = "Chrome Captions"
CAPTIONER_MODEL = "chrome-web-speech"
CAPTIONER_PROVENANCE = "microphone_browser_web_speech"
CAPTIONER_CLOUD_DISCLOSURE = (
    "Chrome Web Speech may send microphone audio to Google/cloud services; "
    "this is not offline or local speech recognition. Pause detection uses local "
    "browser microphone RMS; WS_COLLAB does not transmit or store those VAD samples. "
    "Input scope is microphone only; other tabs are not captured directly, and "
    "speaker leakage into the microphone cannot be reliably identified."
)
CAPTIONER_PROFILE_DISCLOSURE = (
    "Isolated browser profile: no Google login is used or inherited."
)

MAX_BATCH_ITEMS = 50
MAX_TEXT_CHARS = 4000
MAX_LANGUAGE_CHARS = 35
MAX_SESSION_CHARS = 128
MAX_UTTERANCE_CHARS = 160
MAX_CAPTIONER_BODY_BYTES = 128 * 1024
HEARTBEAT_STALE_SECONDS = 15.0
RESTART_HEALTH_GRACE_SECONDS = 10.0
CAPTIONER_STARTUP_GRACE_SECONDS = 12.0
MAX_PENDING_FINALIZATIONS = 128
RECENT_FINAL_LIMIT = 128
RECENT_FINAL_WINDOW_SECONDS = 12.0
MAX_SILENCE_MS = 24 * 60 * 60 * 1000
MIN_PAUSE_MS = 20
MAX_PAUSES_PER_ENVELOPE = 128
PAUSE_SOURCE = "browser_rms_vad"
MIC_INPUT_SCOPE = "microphone"
MIC_FLOOR_SOURCE_ID = "local_microphone"
MAX_TRACK_LABEL_CHARS = 160
MAX_CAPTIONER_INSTANCES = 64
INSTANCE_RETENTION_SECONDS = 5 * 60.0
VAD_TRANSITION_STALE_SECONDS = 10.0
VAD_TRANSITION_FUTURE_SECONDS = 2.0
PAUSE_ALIGNMENTS = {
    "interim_prefix",
    "between_utterances",
    "approximate_text_position",
}
_LANGUAGE_RE = re.compile(r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$")
_RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)
_INSTANCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

CAPTION_SOURCES = {
    "browser_captioner": "Chrome Captions",
    "google_meet": "Google Meet",
}
CAPTION_SOURCE_ORDER = tuple(CAPTION_SOURCES)


def _atomic_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temp, path)


class CaptionerSettings:
    """Small atomic durable store for captioner operator settings."""

    DEFAULTS = {
        "enabled": True,
        "language": "en-US",
        "send_interims": True,
        "paused": False,
        "prefer_over_google_meet": True,
        "disable_google_meet": False,
        "disable_other_stts": False,
    }

    def __init__(self, directory: Path | str):
        self.path = Path(directory) / "captioner_settings.json"
        self._lock = threading.RLock()
        self._data = dict(self.DEFAULTS)
        if self.path.is_file():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self._data.update(
                        {
                            key: value
                            for key, value in loaded.items()
                            if key not in self.DEFAULTS
                        }
                    )
                    for key, value in loaded.items():
                        if key not in self.DEFAULTS:
                            continue
                        try:
                            self._data.update(
                                self._validated({key: value}, partial=True)
                            )
                        except ValidationError:
                            continue
            except (OSError, ValueError, ValidationError):
                pass

    @staticmethod
    def _validated(values: dict[str, Any], *, partial: bool) -> dict[str, Any]:
        allowed = set(CaptionerSettings.DEFAULTS)
        unknown = set(values) - allowed
        if unknown:
            raise ValidationError(
                "unknown captioner setting",
                details={"fields": sorted(unknown), "allowed": sorted(allowed)},
            )
        result: dict[str, Any] = {}
        for key in (
            "enabled",
            "send_interims",
            "paused",
            "prefer_over_google_meet",
            "disable_google_meet",
            "disable_other_stts",
        ):
            if key in values:
                if not isinstance(values[key], bool):
                    raise ValidationError(f"captioner {key} must be a boolean")
                result[key] = values[key]
        if "language" in values:
            language = str(values["language"]).strip()
            if len(language) > MAX_LANGUAGE_CHARS or not _LANGUAGE_RE.fullmatch(language):
                raise ValidationError("captioner language must be a valid BCP-47 style tag")
            result["language"] = language
        if not partial:
            return {**CaptionerSettings.DEFAULTS, **result}
        return result

    def get(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._data)

    def update(self, values: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(values, dict):
            raise ValidationError("captioner settings must be an object")
        checked = self._validated(values, partial=True)
        with self._lock:
            self._data.update(checked)
            _atomic_json(self.path, self._data)
            return dict(self._data)


class CaptionSourcePolicy:
    """Atomic backend authority for typed/browser caption publication."""

    def __init__(self, directory: Path | str):
        self.path = Path(directory) / "caption_source_policy.json"
        self._legacy_path = Path(directory) / "captioner_settings.json"
        self._lock = threading.RLock()
        self._revision = 0
        self._enabled = {source_id: True for source_id in CAPTION_SOURCES}
        self._primary: str | None = "browser_captioner"
        self._load_or_migrate()

    def _load_or_migrate(self) -> None:
        loaded: Any = None
        if self.path.is_file():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                loaded = None
        if isinstance(loaded, dict):
            sources = loaded.get("sources")
            primary = loaded.get("primary_source_id")
            if (
                isinstance(sources, dict)
                and set(sources) == set(CAPTION_SOURCES)
                and all(isinstance(value, bool) for value in sources.values())
                and (primary is None or primary in CAPTION_SOURCES)
            ):
                self._enabled = dict(sources)
                self._primary = primary
                revision = loaded.get("revision", 0)
                self._revision = (
                    revision
                    if isinstance(revision, int)
                    and not isinstance(revision, bool)
                    and revision >= 0
                    else 0
                )
                before = self._primary
                self._repair_primary()
                if self._primary != before:
                    self._revision += 1
                    self._save()
                return

        legacy: dict[str, Any] = {}
        if self._legacy_path.is_file():
            try:
                candidate = json.loads(self._legacy_path.read_text(encoding="utf-8"))
                if isinstance(candidate, dict):
                    legacy = candidate
            except (OSError, ValueError):
                pass
        browser_enabled = legacy.get("enabled", True)
        meet_disabled = legacy.get("disable_google_meet", False)
        prefer_browser = legacy.get("prefer_over_google_meet", True)
        self._enabled = {
            "browser_captioner": browser_enabled if isinstance(browser_enabled, bool) else True,
            "google_meet": not meet_disabled if isinstance(meet_disabled, bool) else True,
        }
        self._primary = "browser_captioner" if prefer_browser is not False else "google_meet"
        self._repair_primary()
        self._save()

    def _repair_primary(self) -> None:
        if self._primary is not None and self._enabled.get(self._primary):
            return
        self._primary = next(
            (source_id for source_id in CAPTION_SOURCE_ORDER if self._enabled[source_id]),
            None,
        )

    def _save(self) -> None:
        _atomic_json(
            self.path,
            {
                "version": 1,
                "revision": self._revision,
                "primary_source_id": self._primary,
                "sources": dict(self._enabled),
            },
        )

    def get(self) -> dict[str, Any]:
        with self._lock:
            return {
                "revision": self._revision,
                "primary_source_id": self._primary,
                "sources": [
                    {
                        "id": source_id,
                        "display_name": display_name,
                        "enabled": self._enabled[source_id],
                        "primary": self._primary == source_id,
                    }
                    for source_id, display_name in CAPTION_SOURCES.items()
                ],
            }

    def enabled(self, source_id: str) -> bool:
        with self._lock:
            return bool(self._enabled.get(source_id))

    def primary(self) -> str | None:
        with self._lock:
            return self._primary

    @staticmethod
    def _source(source_id: Any) -> str:
        value = str(source_id or "").strip()
        if value not in CAPTION_SOURCES:
            raise ValidationError(
                "unknown caption source",
                details={"source_id": value, "allowed": list(CAPTION_SOURCE_ORDER)},
            )
        return value

    def action(self, source_id: Any, action: Any) -> dict[str, Any]:
        source = self._source(source_id)
        operation = str(action or "").strip()
        if operation not in {"enable", "disable", "make-primary"}:
            raise ValidationError(
                "unknown caption source action",
                details={"allowed": ["enable", "disable", "make-primary"]},
            )
        with self._lock:
            before = (dict(self._enabled), self._primary)
            if operation == "enable":
                self._enabled[source] = True
                if self._primary is None:
                    self._primary = source
            elif operation == "disable":
                self._enabled[source] = False
                if self._primary == source:
                    self._primary = None
                self._repair_primary()
            else:
                self._enabled[source] = True
                self._primary = source
            changed = before != (self._enabled, self._primary)
            if changed:
                self._revision += 1
                self._save()
            return {
                "action": operation,
                "source_id": source,
                "changed": changed,
                "policy": self.get(),
            }

    def apply_legacy(self, values: dict[str, Any]) -> dict[str, Any]:
        """Migrate old captioner booleans without allowing inconsistent state."""

        relevant = {
            key: values[key]
            for key in ("enabled", "disable_google_meet", "prefer_over_google_meet")
            if key in values
        }
        for key, value in relevant.items():
            if not isinstance(value, bool):
                raise ValidationError(f"captioner {key} must be a boolean")
        with self._lock:
            before = (dict(self._enabled), self._primary)
            if "enabled" in relevant:
                self._enabled["browser_captioner"] = relevant["enabled"]
            if "disable_google_meet" in relevant:
                self._enabled["google_meet"] = not relevant["disable_google_meet"]
            if relevant.get("prefer_over_google_meet") is True:
                if self._enabled["browser_captioner"]:
                    self._primary = "browser_captioner"
            elif relevant.get("prefer_over_google_meet") is False:
                if self._enabled["google_meet"]:
                    self._primary = "google_meet"
            self._repair_primary()
            if before != (self._enabled, self._primary):
                self._revision += 1
                self._save()
            return self.get()


class CaptionerInstanceRegistry:
    """Bounded, boot-scoped browser tab registry and selection authority."""

    def __init__(
        self,
        directory: Path | str,
        *,
        boot_id: str,
        clock: Callable[[], float] = time.time,
        stale_seconds: float = HEARTBEAT_STALE_SECONDS,
        retention_seconds: float = INSTANCE_RETENTION_SECONDS,
        max_instances: int = MAX_CAPTIONER_INSTANCES,
    ):
        self.path = Path(directory) / "captioner_instances.json"
        self.boot_id = boot_id
        self._clock = clock
        self.stale_seconds = stale_seconds
        self.retention_seconds = retention_seconds
        self.max_instances = max_instances
        self._lock = threading.RLock()
        self._instances: dict[str, dict[str, Any]] = {}
        self._selected: str | None = None
        self._selection_mode = "automatic"
        self._load()

    @staticmethod
    def validate_id(value: Any, field: str) -> str:
        text = str(value or "").strip()
        if not _INSTANCE_ID_RE.fullmatch(text):
            raise ValidationError(
                f"captioner {field} must use 1-128 ASCII letters, digits, dot, colon, underscore, or hyphen"
            )
        return text

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(loaded, dict) or loaded.get("boot_id") != self.boot_id:
            return
        rows = loaded.get("instances")
        if isinstance(rows, dict):
            self._instances = {
                key: value for key, value in rows.items()
                if isinstance(value, dict) and _INSTANCE_ID_RE.fullmatch(key)
            }
        selected = loaded.get("selected_instance_id")
        if selected in self._instances:
            self._selected = selected
            self._selection_mode = (
                "pinned" if loaded.get("selection_mode") == "pinned" else "automatic"
            )
        self._prune_and_select()

    def _save(self) -> None:
        _atomic_json(
            self.path,
            {
                "version": 1,
                "boot_id": self.boot_id,
                "selected_instance_id": self._selected,
                "selection_mode": self._selection_mode,
                "instances": self._instances,
            },
        )

    def _fresh(self, row: dict[str, Any]) -> bool:
        return self._clock() - float(row.get("last_seen", 0)) <= self.stale_seconds

    def _healthy(self, row: dict[str, Any]) -> bool:
        return bool(
            row.get("enabled", True)
            and row.get("recognizer_supported")
            and row.get("state") not in {"unsupported", "permission_denied", "error"}
            and self._fresh(row)
        )

    def _prune_and_select(self) -> None:
        now = self._clock()
        retained = sorted(
            (
                (key, row)
                for key, row in self._instances.items()
                if now - float(row.get("last_seen", 0)) <= self.retention_seconds
            ),
            key=lambda pair: (
                -float(pair[1].get("last_seen", 0)),
                pair[0],
            ),
        )[: self.max_instances]
        self._instances = dict(retained)
        selected = self._instances.get(self._selected or "")
        if self._selection_mode == "pinned" and selected and self._fresh(selected):
            return
        if self._selection_mode == "pinned":
            self._selection_mode = "automatic"
            self._selected = None
        if selected and self._healthy(selected):
            return
        candidates = [
            row for row in self._instances.values() if self._healthy(row)
        ]
        candidates.sort(
            key=lambda row: (
                0 if row.get("state") in {"listening", "speaking"} else 1,
                float(row.get("first_seen", 0)),
                str(row["instance_id"]),
            )
        )
        self._selected = candidates[0]["instance_id"] if candidates else None

    def heartbeat(self, heartbeat: dict[str, Any]) -> dict[str, Any]:
        import hashlib

        instance_id = self.validate_id(heartbeat.get("instance_id"), "instance_id")
        session_id = self.validate_id(heartbeat.get("session_id"), "session_id")
        supplied_boot = heartbeat.get("page_boot_id", self.boot_id)
        if supplied_boot != self.boot_id:
            raise AuthenticationError("captioner page boot identity is stale")
        with self._lock:
            now = self._clock()
            previous = self._instances.get(instance_id)
            if previous and previous.get("session_id") != session_id:
                raise AuthenticationError("captioner instance/session binding changed")
            row = {
                **heartbeat,
                "instance_id": instance_id,
                "session_id": session_id,
                "boot_id": self.boot_id,
                "session_fingerprint": hashlib.sha256(
                    session_id.encode("utf-8")
                ).hexdigest()[:12],
                "listening": heartbeat.get("state") in {"listening", "speaking"},
                "first_seen": (
                    float(previous["first_seen"]) if previous else now
                ),
                "last_seen": now,
                "enabled": bool(previous.get("enabled", True)) if previous else True,
            }
            row["state_since"] = (
                previous.get("state_since")
                if previous and previous.get("state") == row.get("state")
                else now
            )
            self._instances[instance_id] = row
            self._prune_and_select()
            self._save()
            return self.authority(instance_id, session_id)

    def authority(self, instance_id: Any, session_id: Any) -> dict[str, Any]:
        instance = self.validate_id(instance_id, "instance_id")
        session = self.validate_id(session_id, "session_id")
        with self._lock:
            self._prune_and_select()
            row = self._instances.get(instance)
            reason: str | None = None
            if row is None or row.get("session_id") != session:
                reason = "unregistered_instance"
            elif not row.get("enabled", True):
                reason = "instance_disabled"
            elif not self._fresh(row):
                reason = "stale_instance"
            elif self._selected != instance:
                reason = "unselected_instance"
            return {
                "selected": reason is None,
                "enabled": bool(row and row.get("enabled", True)),
                "reason": reason,
                "selected_instance_id": self._selected,
                "selection_mode": self._selection_mode,
            }

    def list(self) -> dict[str, Any]:
        with self._lock:
            self._prune_and_select()
            now = self._clock()
            rows = []
            for row in sorted(
                self._instances.values(),
                key=lambda item: (float(item.get("first_seen", 0)), item["instance_id"]),
            ):
                public = dict(row)
                public["selected"] = row["instance_id"] == self._selected
                public["healthy"] = self._healthy(row)
                public["stale"] = not self._fresh(row)
                public["last_seen_age_seconds"] = round(
                    max(0.0, now - float(row.get("last_seen", 0))), 3
                )
                rows.append(public)
            return {
                "boot_id": self.boot_id,
                "selected_instance_id": self._selected,
                "selection_mode": self._selection_mode,
                "stale_after_seconds": self.stale_seconds,
                "instances": rows,
            }

    def action(self, instance_id: Any, action: Any) -> dict[str, Any]:
        instance = self.validate_id(instance_id, "instance_id")
        operation = str(action or "").strip()
        if operation not in {"enable", "disable", "make-primary"}:
            raise ValidationError(
                "unknown captioner instance action",
                details={"allowed": ["enable", "disable", "make-primary"]},
            )
        with self._lock:
            self._prune_and_select()
            row = self._instances.get(instance)
            if row is None:
                raise ValidationError("captioner instance is unknown or expired")
            before = (row.get("enabled", True), self._selected, self._selection_mode)
            if operation == "enable":
                row["enabled"] = True
                self._prune_and_select()
            elif operation == "disable":
                row["enabled"] = False
                if self._selected == instance:
                    self._selected = None
                    self._selection_mode = "automatic"
                self._prune_and_select()
            else:
                row["enabled"] = True
                self._selected = instance
                self._selection_mode = "pinned"
            self._save()
            return {
                "action": operation,
                "instance_id": instance,
                "changed": before != (
                    row.get("enabled", True),
                    self._selected,
                    self._selection_mode,
                ),
                "registry": self.list(),
            }


class CaptionerFinalizationError(WsCollabError):
    """A durable final is pending and must be retried by the browser."""

    code = "captioner_finalization_pending"
    http_status = 503


class BrowserCaptioner:
    """Validation, durable replay protection, heartbeat, and publication."""

    def __init__(
        self,
        directory: Path | str,
        *,
        boot_id: str,
        publish_item: Callable[[dict[str, Any]], Any],
        clock: Callable[[], float] = time.time,
        require_registration: bool = False,
        suppress_item: Callable[[dict[str, Any], str], Any] | None = None,
    ):
        self.boot_id = boot_id
        self.settings = CaptionerSettings(directory)
        self._state_path = Path(directory) / "captioner_dedupe.json"
        self._publish_item = publish_item
        self._suppress_item = suppress_item or (lambda _item, _reason: None)
        self._require_registration = require_registration
        self._clock = clock
        self._lock = threading.RLock()
        self._sessions: dict[str, dict[str, Any]] = {}
        self._finalizations: dict[str, dict[str, Any]] = {}
        self._suppressed: list[dict[str, Any]] = []
        self.instances = CaptionerInstanceRegistry(
            directory, boot_id=boot_id, clock=clock
        )
        self._heartbeat: dict[str, Any] = {}
        self._heartbeats: dict[str, dict[str, Any]] = {}
        self._vad_owner_tokens: dict[str, str] = {}
        self._supervisor: dict[str, Any] = {
            "tab_present": False,
            "tab_id": None,
            "retry_at": None,
            "error": None,
        }
        self._last_ack_at: str | None = None
        self._last_final_at: str | None = None
        self._load()

    def _load(self) -> None:
        if not self._state_path.is_file():
            return
        try:
            loaded = json.loads(self._state_path.read_text(encoding="utf-8"))
            sessions = loaded.get("sessions") if isinstance(loaded, dict) else None
            if isinstance(sessions, dict):
                self._sessions = sessions
            finalizations = loaded.get("finalizations") if isinstance(loaded, dict) else None
            if isinstance(finalizations, dict):
                self._finalizations = finalizations
            suppressed = loaded.get("suppressed") if isinstance(loaded, dict) else None
            if isinstance(suppressed, list):
                self._suppressed = [
                    row for row in suppressed if isinstance(row, dict)
                ][-1024:]
            self._prune()
        except (OSError, ValueError):
            self._sessions = {}
            self._finalizations = {}

    def _prune(self) -> None:
        cutoff = self._clock() - 7 * 24 * 3600
        recent = sorted(
            (
                (session_id, value)
                for session_id, value in self._sessions.items()
                if isinstance(value, dict) and float(value.get("last_seen", 0)) >= cutoff
            ),
            key=lambda pair: float(pair[1].get("last_seen", 0)),
            reverse=True,
        )[:128]
        self._sessions = dict(recent)
        for session in self._sessions.values():
            processed = session.get("processed")
            if isinstance(processed, list):
                session["processed"] = processed[-2048:]
            utterances = session.get("utterances")
            if isinstance(utterances, dict) and len(utterances) > 1024:
                session["utterances"] = dict(list(utterances.items())[-1024:])
        pending = {
            key: value
            for key, value in self._finalizations.items()
            if isinstance(value, dict) and value.get("status") != "completed"
        }
        completed = sorted(
            (
                (key, value)
                for key, value in self._finalizations.items()
                if isinstance(value, dict) and value.get("status") == "completed"
            ),
            key=lambda pair: float(pair[1].get("updated_at", 0)),
            reverse=True,
        )[:2048]
        self._finalizations = {**dict(completed), **pending}
        self._suppressed = self._suppressed[-1024:]

    def _save(self) -> None:
        self._prune()
        _atomic_json(
            self._state_path,
            {
                "version": 2,
                "sessions": self._sessions,
                "finalizations": self._finalizations,
                "suppressed": self._suppressed,
            },
        )

    @staticmethod
    def _finalization_key(item: dict[str, Any]) -> str:
        return (
            f"{item['session_id']}:{item['seq']}:{item['utterance_id']}:"
            f"{item['revision']}"
        )

    @staticmethod
    def _result(
        item: dict[str, Any], status: str, *, reason: str | None = None
    ) -> dict[str, Any]:
        result = {
            "session_id": item["session_id"],
            "instance_id": item.get("instance_id"),
            "utterance_id": item["utterance_id"],
            "seq": item["seq"],
            "revision": item["revision"],
            "is_final": item["is_final"],
            "status": status,
        }
        if reason is not None:
            result.update({"suppressed": True, "reason": reason})
        return result

    def _complete_finalization(
        self,
        key: str,
        record: dict[str, Any],
        session: dict[str, Any],
    ) -> Any:
        item = record["item"]
        try:
            publication = self._publish_item(item)
        except Exception as error:
            record["attempts"] = int(record.get("attempts", 0)) + 1
            record["last_error"] = str(error)[:500]
            record["updated_at"] = self._clock()
            self._save()
            raise CaptionerFinalizationError(
                "caption finalization is pending; retry delivery",
                details={"session_id": item["session_id"], "seq": item["seq"]},
            ) from error

        processed = {int(seq) for seq in session.get("processed", [])}
        processed.add(int(item["seq"]))
        session["processed"] = sorted(processed)[-2048:]
        session["highest_seq"] = max(
            int(session.get("highest_seq", 0)), int(item["seq"])
        )
        session["last_seen"] = self._clock()
        record.update(
            {
                "status": "completed",
                "last_error": None,
                "publication": publication,
                "updated_at": self._clock(),
            }
        )
        self._last_final_at = utc_now_iso()
        self._save()
        return publication

    def recover_pending(self, *, limit: int = MAX_PENDING_FINALIZATIONS) -> dict[str, int]:
        """Boundedly resume durable finalizations left pending by a crash."""

        recovered = 0
        failed = 0
        with self._lock:
            pending = [
                (key, record)
                for key, record in self._finalizations.items()
                if isinstance(record, dict) and record.get("status") != "completed"
            ][: max(0, limit)]
            for key, record in pending:
                try:
                    item = self.validate_envelope(record.get("item"))
                    record["item"] = item
                    session = self._sessions.setdefault(
                        item["session_id"],
                        {"processed": [], "utterances": {}, "last_seen": self._clock()},
                    )
                    self._complete_finalization(key, record, session)
                    recovered += 1
                except CaptionerFinalizationError:
                    failed += 1
                except Exception as error:
                    record["last_error"] = str(error)[:500]
                    record["updated_at"] = self._clock()
                    failed += 1
                    self._save()
        return {"recovered": recovered, "failed": failed}

    @staticmethod
    def _timestamp(value: Any, field: str, *, required: bool = True) -> str | None:
        if value is None and not required:
            return None
        if not isinstance(value, str) or not value.strip():
            requirement = "is required" if required else "must be RFC3339 or null"
            raise ValidationError(f"captioner {field} {requirement}")
        raw = value.strip()
        if not _RFC3339_RE.fullmatch(raw):
            raise ValidationError(f"captioner {field} must be an RFC3339 timestamp")
        try:
            parsed = datetime.fromisoformat(
                raw[:-1] + "+00:00" if raw.endswith("Z") else raw
            )
        except ValueError as error:
            raise ValidationError(
                f"captioner {field} must be an RFC3339 timestamp"
            ) from error
        if parsed.utcoffset() is None:
            raise ValidationError(f"captioner {field} must include a timezone")
        return raw

    @staticmethod
    def _pauses(value: Any, text: str) -> list[dict[str, Any]]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValidationError("captioner pauses must be an array")
        if len(value) > MAX_PAUSES_PER_ENVELOPE:
            raise ValidationError(
                "captioner pauses exceeds item limit",
                details={"limit": MAX_PAUSES_PER_ENVELOPE},
            )
        result: list[dict[str, Any]] = []
        allowed = {
            "duration_ms", "start_at", "end_at", "source", "alignment", "after_char"
        }
        for index, pause in enumerate(value):
            if not isinstance(pause, dict) or set(pause) != allowed:
                raise ValidationError(
                    f"captioner pauses[{index}] must contain exactly "
                    "duration_ms, start_at, end_at, source, alignment, and after_char"
                )
            duration = pause["duration_ms"]
            after_char = pause["after_char"]
            if (
                isinstance(duration, bool)
                or not isinstance(duration, int)
                or not MIN_PAUSE_MS <= duration <= MAX_SILENCE_MS
            ):
                raise ValidationError(
                    f"captioner pauses[{index}].duration_ms must be an integer "
                    f"from {MIN_PAUSE_MS} to {MAX_SILENCE_MS}"
                )
            if (
                isinstance(after_char, bool)
                or not isinstance(after_char, int)
                or not 0 <= after_char <= len(text)
            ):
                raise ValidationError(
                    f"captioner pauses[{index}].after_char must be within the text"
                )
            if pause["source"] != PAUSE_SOURCE:
                raise ValidationError(
                    f"captioner pauses[{index}].source must be {PAUSE_SOURCE}"
                )
            if pause["alignment"] not in PAUSE_ALIGNMENTS:
                raise ValidationError(
                    f"captioner pauses[{index}].alignment is invalid"
                )
            start_at = BrowserCaptioner._timestamp(
                pause["start_at"], f"pauses[{index}].start_at"
            )
            end_at = BrowserCaptioner._timestamp(
                pause["end_at"], f"pauses[{index}].end_at"
            )
            assert start_at is not None and end_at is not None
            parse = lambda raw: datetime.fromisoformat(
                raw[:-1] + "+00:00" if raw.endswith("Z") else raw
            )
            if parse(end_at) < parse(start_at):
                raise ValidationError(
                    f"captioner pauses[{index}].end_at must not precede start_at"
                )
            result.append(
                {
                    "duration_ms": duration,
                    "start_at": start_at,
                    "end_at": end_at,
                    "source": PAUSE_SOURCE,
                    "alignment": pause["alignment"],
                    "after_char": after_char,
                }
            )
        return result

    @staticmethod
    def validate_envelope(raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise ValidationError("captioner envelope must be an object")
        session_id = str(raw.get("session_id") or "").strip()
        utterance_id = str(raw.get("utterance_id") or "").strip()
        text = raw.get("text")
        language = str(raw.get("language") or "").strip()
        if not session_id or len(session_id) > MAX_SESSION_CHARS:
            raise ValidationError("captioner session_id is required and must be bounded")
        if not utterance_id or len(utterance_id) > MAX_UTTERANCE_CHARS:
            raise ValidationError("captioner utterance_id is required and must be bounded")
        if not isinstance(text, str) or not text.strip():
            raise ValidationError("captioner text is required")
        if len(text) > MAX_TEXT_CHARS:
            raise ValidationError("captioner text exceeds the maximum length")
        if not language or len(language) > MAX_LANGUAGE_CHARS or not _LANGUAGE_RE.fullmatch(language):
            raise ValidationError("captioner language must be a valid BCP-47 style tag")
        if not isinstance(raw.get("is_final"), bool):
            raise ValidationError("captioner is_final must be a boolean")
        for field in ("seq", "revision"):
            value = raw.get(field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValidationError(f"captioner {field} must be a non-negative integer")
            if value > 9_007_199_254_740_991:
                raise ValidationError(f"captioner {field} exceeds the safe integer range")
        if raw["seq"] < 1:
            raise ValidationError("captioner seq must be at least 1")
        confidence = raw.get("confidence")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise ValidationError("captioner confidence must be numeric")
        confidence = float(confidence)
        if not 0.0 <= confidence <= 1.0:
            raise ValidationError("captioner confidence must be between 0 and 1")
        silence_before_ms = raw.get("silence_before_ms")
        if silence_before_ms is not None and (
            isinstance(silence_before_ms, bool)
            or not isinstance(silence_before_ms, int)
            or not MIN_PAUSE_MS <= silence_before_ms <= MAX_SILENCE_MS
        ):
            raise ValidationError(
                f"captioner silence_before_ms must be an integer from "
                f"{MIN_PAUSE_MS} to {MAX_SILENCE_MS}"
            )
        normalized_text = text.strip()
        instance_id = raw.get("instance_id")
        if instance_id is not None:
            instance_id = CaptionerInstanceRegistry.validate_id(
                instance_id, "instance_id"
            )
        return {
            "session_id": session_id,
            "instance_id": instance_id,
            "utterance_id": utterance_id,
            "seq": raw["seq"],
            "revision": raw["revision"],
            "text": normalized_text,
            "is_final": raw["is_final"],
            "confidence": confidence,
            "language": language,
            "started_at": BrowserCaptioner._timestamp(raw.get("started_at"), "started_at"),
            "result_at": BrowserCaptioner._timestamp(raw.get("result_at"), "result_at"),
            "speech_started_at": BrowserCaptioner._timestamp(
                raw.get("speech_started_at"), "speech_started_at", required=False
            ),
            "speech_ended_at": BrowserCaptioner._timestamp(
                raw.get("speech_ended_at"), "speech_ended_at", required=False
            ),
            "silence_before_ms": silence_before_ms,
            "pauses": BrowserCaptioner._pauses(raw.get("pauses"), normalized_text),
        }

    @staticmethod
    def _contiguous(processed: set[int]) -> int:
        highest = 0
        while highest + 1 in processed:
            highest += 1
        return highest

    @staticmethod
    def _preserve_speech_metadata(
        item: dict[str, Any], current: dict[str, Any] | None
    ) -> dict[str, Any]:
        previous = (
            current.get("speech_metadata")
            if isinstance(current, dict)
            and isinstance(current.get("speech_metadata"), dict)
            else {}
        )
        for field in ("speech_started_at", "silence_before_ms"):
            if previous.get(field) is not None:
                item[field] = previous[field]
        if previous.get("speech_ended_at") is not None:
            item["speech_ended_at"] = previous["speech_ended_at"]
        return {
            "speech_started_at": item.get("speech_started_at"),
            "speech_ended_at": item.get("speech_ended_at"),
            "silence_before_ms": item.get("silence_before_ms"),
            "pauses": item.get("pauses", []),
        }

    def ingest(self, body: Any) -> dict[str, Any]:
        raw_items = body.get("items") if isinstance(body, dict) and "items" in body else [body]
        if not isinstance(raw_items, list) or not raw_items:
            raise ValidationError("captioner items must be a non-empty array")
        if len(raw_items) > MAX_BATCH_ITEMS:
            raise ValidationError(
                "captioner batch exceeds item limit", details={"limit": MAX_BATCH_ITEMS}
            )
        items = [self.validate_envelope(item) for item in raw_items]
        results: list[dict[str, Any]] = []
        touched = False
        with self._lock:
            for item in items:
                session = self._sessions.setdefault(
                    item["session_id"],
                    {"processed": [], "utterances": {}, "last_seen": self._clock()},
                )
                processed = {int(seq) for seq in session.get("processed", [])}
                seq = item["seq"]
                if seq in processed:
                    results.append(self._result(item, "duplicate"))
                    continue
                suppression_reason: str | None = None
                if self._require_registration:
                    if item.get("instance_id") is None:
                        suppression_reason = "unregistered_instance"
                    else:
                        authority = self.instances.authority(
                            item["instance_id"], item["session_id"]
                        )
                        suppression_reason = authority["reason"]
                    settings = self.settings.get()
                    if not settings["enabled"] or settings["paused"]:
                        suppression_reason = (
                            "source_disabled"
                            if not settings["enabled"]
                            else "source_paused"
                        )
                if suppression_reason is not None:
                    processed.add(seq)
                    session["processed"] = sorted(processed)[-2048:]
                    session["highest_seq"] = max(
                        int(session.get("highest_seq", 0)), seq
                    )
                    session["last_seen"] = self._clock()
                    self._suppressed.append(
                        {
                            "item": item,
                            "reason": suppression_reason,
                            "received_at": self._clock(),
                        }
                    )
                    self._save()
                    try:
                        self._suppress_item(item, suppression_reason)
                    except Exception as error:
                        self._suppressed[-1]["audit_error"] = str(error)[:500]
                    touched = True
                    results.append(
                        self._result(
                            item,
                            f"suppressed_{suppression_reason}",
                            reason=suppression_reason,
                        )
                    )
                    continue
                finalization_key = (
                    self._finalization_key(item) if item["is_final"] else None
                )
                pending = (
                    self._finalizations.get(finalization_key)
                    if finalization_key is not None
                    else None
                )
                if isinstance(pending, dict) and pending.get("status") != "completed":
                    publication = self._complete_finalization(
                        finalization_key, pending, session
                    )
                    touched = True
                    if isinstance(publication, dict) and publication.get("suppressed"):
                        results.append(
                            self._result(
                                item,
                                f"suppressed_{publication['reason']}",
                                reason=str(publication["reason"]),
                            )
                        )
                    else:
                        results.append(self._result(item, "accepted"))
                    continue
                utterances = session.setdefault("utterances", {})
                current = utterances.get(item["utterance_id"])
                status = "accepted"
                highest_seen = int(session.get("highest_seq", 0))
                if seq < highest_seen:
                    status = "rejected_stale_seq"
                elif current and current.get("final"):
                    status = "rejected_final_immutable"
                elif current and item["revision"] <= int(current.get("revision", -1)):
                    status = "rejected_stale_revision"
                else:
                    speech_metadata = self._preserve_speech_metadata(item, current)
                    utterances[item["utterance_id"]] = {
                        "revision": item["revision"],
                        "final": item["is_final"],
                        "speech_metadata": speech_metadata,
                    }
                    if item["is_final"]:
                        key = finalization_key
                        assert key is not None
                        record = self._finalizations.get(key)
                        if record is None:
                            record = {
                                "status": "pending",
                                "item": item,
                                "attempts": 0,
                                "last_error": None,
                                "updated_at": self._clock(),
                            }
                            self._finalizations[key] = record
                            session["last_seen"] = self._clock()
                            self._save()
                        publication = self._complete_finalization(
                            key, record, session
                        )
                        if isinstance(publication, dict) and publication.get(
                            "suppressed"
                        ):
                            status = f"suppressed_{publication['reason']}"
                    else:
                        publication = self._publish_item(item)
                        if isinstance(publication, dict) and publication.get(
                            "suppressed"
                        ):
                            status = f"suppressed_{publication['reason']}"
                        processed.add(seq)
                        session["processed"] = sorted(processed)[-2048:]
                        session["highest_seq"] = max(highest_seen, seq)
                        session["last_seen"] = self._clock()
                        self._save()
                    touched = True
                reason = (
                    status.removeprefix("suppressed_")
                    if status.startswith("suppressed_")
                    else None
                )
                results.append(self._result(item, status, reason=reason))
            if touched:
                self._save()
            self._last_ack_at = utc_now_iso()
            by_session: dict[str, int] = {}
            for item in items:
                session = self._sessions[item["session_id"]]
                by_session[item["session_id"]] = self._contiguous(
                    {int(seq) for seq in session.get("processed", [])}
                )
        return {
            "accepted": sum(result["status"] == "accepted" for result in results),
            "suppressed": sum(result.get("suppressed") is True for result in results),
            "acked_seqs": [result["seq"] for result in results],
            "highest_contiguous": by_session,
            "results": results,
            "boot_id": self.boot_id,
        }

    def heartbeat(self, body: Any) -> dict[str, Any]:
        if not isinstance(body, dict):
            raise ValidationError("captioner heartbeat must be an object")
        allowed = {
            "session_id",
            "instance_id",
            "boot_id",
            "owns_lease",
            "state",
            "last_error",
            "queue_depth",
            "recognizer_supported",
            "mic_permission",
            "last_result_at",
            "last_ack_at",
            "restart_count",
            "input",
            "current_silence_ms",
            "vad",
        }
        unknown = set(body) - allowed
        if unknown:
            raise ValidationError(
                "captioner heartbeat contains unsupported fields",
                details={"fields": sorted(unknown)},
            )
        for field in ("owns_lease", "recognizer_supported"):
            if field in body and not isinstance(body[field], bool):
                raise ValidationError(f"captioner heartbeat {field} must be a boolean")
        session_id = str(body.get("session_id") or "").strip()
        if not session_id or len(session_id) > MAX_SESSION_CHARS:
            raise ValidationError("captioner heartbeat session_id is required")
        allowed_states = {
            "starting", "listening", "speaking", "restarting", "paused",
            "unsupported", "permission_denied", "error", "idle", "standby",
        }
        state = str(body.get("state") or "").strip()
        if state not in allowed_states:
            raise ValidationError("invalid captioner heartbeat state")
        queue_depth = body.get("queue_depth", 0)
        if isinstance(queue_depth, bool) or not isinstance(queue_depth, int) or not 0 <= queue_depth <= 1_000_000:
            raise ValidationError("captioner queue_depth must be an integer from 0 to 1000000")
        restart_count = body.get("restart_count", 0)
        if (
            isinstance(restart_count, bool)
            or not isinstance(restart_count, int)
            or not 0 <= restart_count <= 1_000_000
        ):
            raise ValidationError("captioner restart_count must be a non-negative integer")
        current_silence_ms = body.get("current_silence_ms")
        if current_silence_ms is not None and (
            isinstance(current_silence_ms, bool)
            or not isinstance(current_silence_ms, int)
            or not 0 <= current_silence_ms <= MAX_SILENCE_MS
        ):
            raise ValidationError(
                f"captioner current_silence_ms must be an integer from 0 to {MAX_SILENCE_MS}"
            )
        vad_raw = body.get("vad")
        vad: dict[str, Any] | None = None
        if vad_raw is not None:
            vad_allowed = {
                "source",
                "available",
                "state",
                "rms",
                "noise_floor",
                "threshold",
                "current_silence_ms",
                "frame_interval_ms",
                "permission",
                "error",
            }
            if not isinstance(vad_raw, dict) or set(vad_raw) != vad_allowed:
                raise ValidationError(
                    "captioner vad must contain exactly the supported scalar fields"
                )
            if vad_raw["source"] != PAUSE_SOURCE:
                raise ValidationError(f"captioner vad source must be {PAUSE_SOURCE}")
            if not isinstance(vad_raw["available"], bool):
                raise ValidationError("captioner vad available must be a boolean")
            if vad_raw["state"] not in {"unavailable", "idle", "speech", "silence"}:
                raise ValidationError("captioner vad state is invalid")
            if (
                vad_raw["available"] and vad_raw["state"] == "unavailable"
            ) or (
                not vad_raw["available"] and vad_raw["state"] != "unavailable"
            ):
                raise ValidationError("captioner vad state must match availability")
            for field in ("rms", "noise_floor", "threshold"):
                value = vad_raw[field]
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                    or not 0 <= value <= 1
                ):
                    raise ValidationError(
                        f"captioner vad {field} must be a finite number from 0 to 1"
                    )
            vad_silence = vad_raw["current_silence_ms"]
            if vad_silence is not None and (
                isinstance(vad_silence, bool)
                or not isinstance(vad_silence, int)
                or not 0 <= vad_silence <= MAX_SILENCE_MS
            ):
                raise ValidationError("captioner vad current_silence_ms is invalid")
            interval = vad_raw["frame_interval_ms"]
            if (
                isinstance(interval, bool)
                or not isinstance(interval, int)
                or not 5 <= interval <= 1000
            ):
                raise ValidationError(
                    "captioner vad frame_interval_ms must be an integer from 5 to 1000"
                )
            permission = vad_raw["permission"]
            if permission not in {"unknown", "prompt", "granted", "denied", "error"}:
                raise ValidationError("captioner vad permission is invalid")
            error = vad_raw["error"]
            if error is not None and (not isinstance(error, str) or len(error) > 500):
                raise ValidationError("captioner vad error must be bounded text or null")
            vad = {
                "source": PAUSE_SOURCE,
                "available": vad_raw["available"],
                "state": vad_raw["state"],
                "rms": float(vad_raw["rms"]),
                "noise_floor": float(vad_raw["noise_floor"]),
                "threshold": float(vad_raw["threshold"]),
                "current_silence_ms": vad_silence,
                "frame_interval_ms": interval,
                "permission": permission,
                "error": error or None,
            }
        input_raw = body.get("input")
        input_metadata: dict[str, Any] | None = None
        if input_raw is not None:
            input_metadata = self._validate_input_metadata(input_raw)
        instance_id = CaptionerInstanceRegistry.validate_id(
            body.get("instance_id") or session_id, "instance_id"
        )
        heartbeat = {
            "session_id": session_id,
            "instance_id": instance_id,
            "page_boot_id": body.get("boot_id", self.boot_id),
            "reported_lease": bool(body.get("owns_lease")),
            "state": state,
            "last_error": str(body.get("last_error") or "")[:500] or None,
            "queue_depth": queue_depth,
            "recognizer_supported": bool(
                body.get("recognizer_supported")
                or state in {"starting", "listening", "speaking", "restarting"}
            ),
            "mic_permission": str(body.get("mic_permission") or "unknown")[:32],
            "last_result_at": str(body.get("last_result_at") or "")[:64] or None,
            "last_ack_at": str(body.get("last_ack_at") or "")[:64] or None,
            "restart_count": restart_count,
            "current_silence_ms": current_silence_ms,
            "vad": vad,
            "input": input_metadata,
            "boot_id": self.boot_id,
            "received_at": self._clock(),
        }
        authority = self.instances.heartbeat(heartbeat)
        source_enabled = self.settings.get()["enabled"] and not self.settings.get()["paused"]
        may_capture = bool(authority["selected"] and source_enabled)
        heartbeat["owns_lease"] = bool(may_capture and heartbeat["reported_lease"])
        if state in {"listening", "speaking", "starting", "restarting"} and not heartbeat["owns_lease"]:
            heartbeat["state"] = "standby"
        with self._lock:
            if heartbeat["owns_lease"]:
                for key, value in self._heartbeats.items():
                    if key != heartbeat["instance_id"]:
                        value["owns_lease"] = False
                        self._vad_owner_tokens.pop(key, None)
                owner_token = self._vad_owner_tokens.setdefault(
                    heartbeat["instance_id"], secrets.token_urlsafe(24)
                )
            else:
                self._vad_owner_tokens.pop(heartbeat["instance_id"], None)
                owner_token = None
            previous = self._heartbeats.get(heartbeat["instance_id"])
            heartbeat["state_since"] = (
                previous.get("state_since")
                if isinstance(previous, dict)
                and previous.get("state") == heartbeat["state"]
                and previous.get("state_since") is not None
                else self._clock()
            )
            self._heartbeats[heartbeat["instance_id"]] = heartbeat
            cutoff = self._clock() - HEARTBEAT_STALE_SECONDS
            self._heartbeats = {
                key: value
                for key, value in self._heartbeats.items()
                if float(value.get("received_at", 0)) >= cutoff
            }
            active = [
                value for value in self._heartbeats.values() if value.get("owns_lease")
            ]
            self._heartbeat = max(
                active or list(self._heartbeats.values()),
                key=lambda value: float(value.get("received_at", 0)),
            )
        return {
            "ok": True,
            "boot_id": self.boot_id,
            "config": self.settings.get(),
            "server_time": utc_now_iso(),
            "vad_owner_token": owner_token,
            "instance": {
                **authority,
                "source_enabled": source_enabled,
                "may_capture": may_capture,
                "reason": (
                    authority["reason"]
                    if authority["reason"] is not None
                    else "source_disabled"
                    if not source_enabled
                    else None
                ),
            },
        }

    @staticmethod
    def _validate_input_metadata(raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise ValidationError("captioner input metadata must be an object")
        allowed = {
            "input_scope",
            "track_label",
            "device_fingerprint",
            "echo_cancellation",
            "noise_suppression",
            "auto_gain_control",
            "channel_count",
            "sample_rate",
        }
        if set(raw) != allowed:
            raise ValidationError(
                "captioner input metadata must contain exactly the supported fields"
            )
        if raw["input_scope"] != MIC_INPUT_SCOPE:
            raise ValidationError("captioner input_scope must be microphone")
        label = raw["track_label"]
        if not isinstance(label, str) or len(label) > MAX_TRACK_LABEL_CHARS:
            raise ValidationError("captioner track_label must be bounded text")
        fingerprint = raw["device_fingerprint"]
        if fingerprint is not None and (
            not isinstance(fingerprint, str)
            or not re.fullmatch(r"[0-9a-f]{8,32}", fingerprint)
        ):
            raise ValidationError(
                "captioner device_fingerprint must be a short lowercase hex digest or null"
            )
        result: dict[str, Any] = {
            "input_scope": MIC_INPUT_SCOPE,
            "track_label": label,
            "device_fingerprint": fingerprint,
        }
        for field in ("echo_cancellation", "noise_suppression", "auto_gain_control"):
            value = raw[field]
            if value is not None and not isinstance(value, bool):
                raise ValidationError(f"captioner input {field} must be a boolean or null")
            result[field] = value
        for field, lower, upper in (
            ("channel_count", 1, 32),
            ("sample_rate", 8_000, 384_000),
        ):
            value = raw[field]
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not lower <= value <= upper
            ):
                raise ValidationError(
                    f"captioner input {field} must be an integer from {lower} to {upper} or null"
                )
            result[field] = value
        return result

    def authenticate_vad_transition(self, body: Any) -> dict[str, Any]:
        """Validate and authenticate one metadata-only VAD transition."""

        if not isinstance(body, dict):
            raise ValidationError("captioner VAD transition must be an object")
        allowed = {
            "source",
            "source_id",
            "input_scope",
            "session_id",
            "instance_id",
            "owner_token",
            "epoch",
            "seq",
            "event",
            "state",
            "at",
        }
        if set(body) != allowed:
            raise ValidationError(
                "captioner VAD transition must contain exactly the supported metadata fields"
            )
        if body["source"] != PAUSE_SOURCE:
            raise ValidationError(f"captioner VAD transition source must be {PAUSE_SOURCE}")
        if body["source_id"] != MIC_FLOOR_SOURCE_ID:
            raise ValidationError(
                f"captioner VAD transition source_id must be {MIC_FLOOR_SOURCE_ID}"
            )
        if body["input_scope"] != MIC_INPUT_SCOPE:
            raise ValidationError("captioner VAD transition input_scope must be microphone")
        session_id = str(body["session_id"]).strip()
        instance_id = str(body["instance_id"]).strip()
        if not session_id or len(session_id) > MAX_SESSION_CHARS:
            raise ValidationError("captioner VAD transition session_id is invalid")
        if not instance_id or len(instance_id) > MAX_SESSION_CHARS:
            raise ValidationError("captioner VAD transition instance_id is invalid")
        for field in ("epoch", "seq"):
            value = body[field]
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 1 <= value <= 9_007_199_254_740_991
            ):
                raise ValidationError(
                    f"captioner VAD transition {field} must be a positive safe integer"
                )
        event = body["event"]
        state = body["state"]
        expected_states = {
            "speech_start": {"speech"},
            "speech_end": {"silence"},
            "state_sync": {"speech", "silence", "idle", "unavailable"},
        }
        if event not in expected_states or state not in expected_states[event]:
            raise ValidationError("captioner VAD transition event/state combination is invalid")
        at = self._timestamp(body["at"], "VAD transition at")
        assert at is not None
        parsed_at = datetime.fromisoformat(
            at[:-1] + "+00:00" if at.endswith("Z") else at
        ).timestamp()
        now = self._clock()
        if parsed_at < now - VAD_TRANSITION_STALE_SECONDS:
            raise ValidationError("captioner VAD transition timestamp is stale")
        if parsed_at > now + VAD_TRANSITION_FUTURE_SECONDS:
            raise ValidationError("captioner VAD transition timestamp is in the future")
        with self._lock:
            heartbeat = self._heartbeats.get(instance_id)
            token = self._vad_owner_tokens.get(instance_id)
            supplied = body["owner_token"]
            authority = self.instances.authority(instance_id, session_id)
            if (
                not isinstance(supplied, str)
                or token is None
                or not secrets.compare_digest(supplied, token)
                or not heartbeat
                or not heartbeat.get("owns_lease")
                or heartbeat.get("session_id") != session_id
                or now - float(heartbeat.get("received_at", 0)) > HEARTBEAT_STALE_SECONDS
                or not authority["selected"]
                or not self.settings.get()["enabled"]
                or self.settings.get()["paused"]
            ):
                raise AuthenticationError(
                    "captioner VAD transition owner authentication failed"
                )
        return {
            "source": PAUSE_SOURCE,
            "source_id": MIC_FLOOR_SOURCE_ID,
            "input_scope": MIC_INPUT_SCOPE,
            "session_id": session_id,
            "instance_id": instance_id,
            "epoch": body["epoch"],
            "seq": body["seq"],
            "event": event,
            "state": state,
            "at": at,
        }

    def begin_page_generation(self) -> None:
        """Discard heartbeats from the previous page generation."""

        with self._lock:
            self._heartbeat = {}
            self._heartbeats = {}
            self._vad_owner_tokens = {}

    def set_supervisor_state(self, **values: Any) -> None:
        with self._lock:
            self._supervisor.update(values)

    def status(self) -> dict[str, Any]:
        with self._lock:
            settings = self.settings.get()
            heartbeat = dict(self._heartbeat)
            supervisor = dict(self._supervisor)
            age = (
                max(0.0, self._clock() - float(heartbeat["received_at"]))
                if heartbeat.get("received_at") is not None
                else None
            )
            state_age = (
                max(0.0, self._clock() - float(heartbeat["state_since"]))
                if heartbeat.get("state_since") is not None
                else None
            )
            health_level = "ok"
            health_error: str | None = None
            if not settings["enabled"]:
                state = "disabled"
                healthy = True
            elif settings["paused"]:
                state = "paused"
                healthy = True
            elif age is None or age > HEARTBEAT_STALE_SECONDS:
                state = "stale"
                healthy = False
                health_level = "error"
            elif heartbeat.get("state") in {"unsupported", "permission_denied", "error"}:
                state = str(heartbeat["state"])
                healthy = False
                health_level = "error"
            elif heartbeat.get("state") in {"starting", "restarting"}:
                if state_age is not None and state_age > RESTART_HEALTH_GRACE_SECONDS:
                    state = "error"
                    healthy = False
                    health_level = "error"
                    health_error = (
                        f"recognizer remained {heartbeat.get('state')} for "
                        f"{state_age:.1f}s"
                    )
                else:
                    state = str(heartbeat["state"])
                    healthy = True
                    health_level = "degraded"
            elif heartbeat.get("state") in {"listening", "speaking"}:
                state = str(heartbeat["state"])
                healthy = True
                if heartbeat.get("vad") and not heartbeat["vad"].get("available"):
                    health_level = "degraded"
            else:
                state = str(heartbeat.get("state") or "idle")
                healthy = False
                health_level = "degraded"
            return {
                "id": CAPTIONER_ID,
                "display_name": CAPTIONER_DISPLAY_NAME,
                "model": CAPTIONER_MODEL,
                "external": True,
                "push_driven": True,
                "cloud_processing": True,
                "privacy_disclosure": CAPTIONER_CLOUD_DISCLOSURE,
                **settings,
                **supervisor,
                "state": state,
                "healthy": healthy,
                "health": health_level,
                "heartbeat_age_seconds": round(age, 3) if age is not None else None,
                "state_age_seconds": round(state_age, 3) if state_age is not None else None,
                "restart_count": heartbeat.get("restart_count", 0),
                "session_id": heartbeat.get("session_id"),
                "recognizer_supported": heartbeat.get("recognizer_supported"),
                "mic_permission": heartbeat.get("mic_permission"),
                "queue_depth": heartbeat.get("queue_depth", 0),
                "last_error": heartbeat.get("last_error") or health_error or supervisor.get("error"),
                "last_result_at": heartbeat.get("last_result_at"),
                "last_ack_at": heartbeat.get("last_ack_at") or self._last_ack_at,
                "last_final_at": self._last_final_at,
                "current_silence_ms": heartbeat.get("current_silence_ms"),
                "vad": heartbeat.get("vad"),
                "input": heartbeat.get("input"),
                "instances": self.instances.list(),
            }


class LocalMicFloor:
    """Ordered, fail-safe local microphone floor derived only from browser RMS VAD."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.time,
        hangover_seconds: float = 0.35,
        stale_seconds: float = 15.0,
    ):
        self._clock = clock
        self.hangover_seconds = max(0.0, float(hangover_seconds))
        self.stale_seconds = max(0.5, float(stale_seconds))
        self._lock = threading.RLock()
        self._owner: tuple[str, str] | None = None
        self._epoch = 0
        self._seq = 0
        self._speech = False
        self._release_at: float | None = None
        self._last_received_at: float | None = None
        self._last_event_at: str | None = None
        self._last_event: str | None = None

    def apply(self, transition: dict[str, Any]) -> dict[str, Any]:
        now = self._clock()
        owner = (transition["session_id"], transition["instance_id"])
        epoch = int(transition["epoch"])
        seq = int(transition["seq"])
        with self._lock:
            was_available = (
                self._last_received_at is not None
                and now - self._last_received_at <= self.stale_seconds
            )
            if self._owner == owner:
                if epoch < self._epoch or (epoch == self._epoch and seq < self._seq):
                    return {**self.status(), "accepted": False, "stale": True}
                if epoch == self._epoch and seq == self._seq:
                    return {**self.status(), "accepted": False, "duplicate": True}
            changed_owner = self._owner != owner
            if changed_owner:
                self._owner = owner
                self._epoch = 0
                self._seq = 0
            self._epoch = epoch
            self._seq = seq
            self._last_received_at = now
            self._last_event_at = transition["at"]
            self._last_event = transition["event"]
            acquired = transition["state"] == "speech" and (
                changed_owner or not self._speech or not was_available
            )
            if transition["state"] == "speech":
                self._speech = True
                self._release_at = None
            elif transition["state"] in {"silence", "idle"}:
                needs_hangover = self._speech or transition["event"] == "speech_end"
                self._speech = False
                if needs_hangover:
                    self._release_at = now + self.hangover_seconds
            else:
                self._speech = False
                self._release_at = None
            return {
                **self.status(),
                "accepted": True,
                "acquired": acquired,
                "owner_changed": changed_owner,
            }

    def status(self) -> dict[str, Any]:
        with self._lock:
            now = self._clock()
            age = (
                max(0.0, now - self._last_received_at)
                if self._last_received_at is not None
                else None
            )
            available = age is not None and age <= self.stale_seconds
            if not available:
                state = "unknown"
                blocked = False
            elif self._speech:
                state = "speech"
                blocked = True
            elif self._release_at is not None and now < self._release_at:
                state = "hangover"
                blocked = True
            else:
                state = "clear"
                blocked = False
            return {
                "source": PAUSE_SOURCE,
                "source_id": MIC_FLOOR_SOURCE_ID,
                "input_scope": MIC_INPUT_SCOPE,
                "available": available,
                "state": state,
                "blocked": blocked,
                "hangover_ms": round(self.hangover_seconds * 1000),
                "stale_after_ms": round(self.stale_seconds * 1000),
                "age_ms": round(age * 1000) if age is not None else None,
                "session_id": self._owner[0] if self._owner else None,
                "instance_id": self._owner[1] if self._owner else None,
                "epoch": self._epoch or None,
                "seq": self._seq or None,
                "last_event": self._last_event,
                "last_event_at": self._last_event_at,
            }


class CaptionerSupervisor:
    """Ensure one tab exists on the isolated captioner profile and CDP port."""

    def __init__(
        self,
        captioner: BrowserCaptioner,
        *,
        page_url: str,
        cdp_endpoint: str,
        profile: Path | str,
        browser_path: Callable[[], str],
        navigator_module: Any,
        cdp_available: Callable[[], bool],
        clock: Callable[[], float] = time.monotonic,
        jitter: Callable[[], float] = random.random,
        suppress_launch: bool | None = None,
    ):
        self.captioner = captioner
        self.page_url = page_url.rstrip("/") + "/"
        self.cdp_endpoint = cdp_endpoint
        self.profile = Path(profile)
        self.browser_path = browser_path
        self.nav = navigator_module
        self.cdp_available = cdp_available
        self.clock = clock
        self.jitter = jitter
        self.suppress_launch = (
            "PYTEST_CURRENT_TEST" in os.environ if suppress_launch is None else suppress_launch
        )
        self.attempts = 0
        self.retry_at = 0.0
        self._reload_existing = True
        self._generation_started_at: float | None = None
        self._generation = 0
        self._reloaded_tabs: set[str] = set()
        self._repair_attempts = 0
        self._repair_at = 0.0
        self.captioner.set_supervisor_state(
            profile_path=str(self.profile),
            cdp_endpoint=self.cdp_endpoint,
            isolated_profile=True,
            identity_policy=CAPTIONER_PROFILE_DISCLOSURE,
        )

    def _begin_generation(self) -> None:
        self._generation += 1
        self._generation_started_at = self.clock()
        self.captioner.begin_page_generation()

    def _schedule_repair_backoff(self) -> None:
        self._repair_attempts += 1
        base = min(60.0, float(2 ** min(self._repair_attempts - 1, 6)))
        self._repair_at = self.clock() + base + min(
            5.0, base * 0.25 * max(0.0, self.jitter())
        )

    def _matching(self, tabs: list[Any]) -> list[Any]:
        target = urlsplit(self.page_url)
        result = []
        for tab in tabs:
            url = str(getattr(tab, "url", "") or "")
            parsed = urlsplit(url)
            if (
                parsed.scheme == target.scheme
                and parsed.netloc == target.netloc
                and parsed.path.rstrip("/") == target.path.rstrip("/")
            ):
                result.append(tab)
        return result

    def _close_tab(self, backend: Any, tab_id: str) -> None:
        close = getattr(backend, "close_tab", None)
        if callable(close):
            close(tab_id)
            return
        from .meet_bridge.cdp import close_tab

        close_tab(self.cdp_endpoint, tab_id)

    def _navigate_existing(
        self, backend: Any, target: Any, legacy: dict[str, Any], *, reason: str, detail: str
    ) -> None:
        page = backend.attach(target)
        try:
            self.nav.navigate(
                page,
                self.page_url,
                cdp_endpoint=self.cdp_endpoint,
                reason=reason,
                detail=detail,
                role="captioner",
                component="captioner-supervisor",
                chrome_profile=self.profile,
                tab_info=legacy,
                backend=backend,
            )
        finally:
            backend.close_connection(page)

    def _failure(self, error: Exception | str) -> dict[str, Any]:
        self.attempts += 1
        base = min(60.0, float(2 ** min(self.attempts - 1, 6)))
        delay = base + min(5.0, base * 0.25 * max(0.0, self.jitter()))
        self.retry_at = self.clock() + delay
        self.captioner.set_supervisor_state(
            tab_present=False,
            tab_id=None,
            retry_at=self.retry_at,
            error=str(error)[:500],
        )
        return self.captioner.status()

    def run_cycle(self, *, foreground: bool = False, force: bool = False) -> dict[str, Any]:
        settings = self.captioner.settings.get()
        if not settings["enabled"] or settings["paused"]:
            self.attempts = 0
            self.retry_at = 0.0
            self._repair_attempts = 0
            self._repair_at = 0.0
            self.captioner.set_supervisor_state(retry_at=None, error=None)
            return self.captioner.status()
        if self.suppress_launch:
            self.captioner.set_supervisor_state(
                error="browser launch suppressed while running tests", retry_at=None
            )
            return self.captioner.status()
        if not force and self.clock() < self.retry_at:
            return self.captioner.status()
        try:
            backend = self.nav.get_browser_backend()
            tabs = backend.list_tabs(self.cdp_endpoint) if self.cdp_available() else []
            matches = self._matching(tabs)
            if matches:
                selected = matches[0]
                legacy = selected.to_legacy() if hasattr(selected, "to_legacy") else dict(selected)
                if self._reload_existing:
                    self._navigate_existing(
                        backend,
                        selected,
                        legacy,
                        reason="captioner-reload",
                        detail="reload the singleton captioner after server boot",
                    )
                    self._reload_existing = False
                    self._reloaded_tabs.add(str(legacy.get("id") or ""))
                    self._begin_generation()
                if foreground:
                    page = backend.attach(selected)
                    try:
                        self.nav.foreground(
                            page,
                            self.page_url,
                            cdp_endpoint=self.cdp_endpoint,
                            reason="captioner-foreground",
                            detail="operator requested the captioner tab",
                            role="captioner",
                            component="captioner-supervisor",
                            chrome_profile=self.profile,
                            tab_id=str(legacy.get("id") or ""),
                            backend=backend,
                        )
                    finally:
                        backend.close_connection(page)
                page_status = self.captioner.status()
                stale = page_status.get("state") == "stale"
                if not stale:
                    self._repair_attempts = 0
                    self._repair_at = 0.0
                    if self._generation_started_at is None:
                        self._generation_started_at = self.clock()
                elif self._generation_started_at is None:
                    self._generation_started_at = self.clock()
                elif (
                    self.clock() - self._generation_started_at
                    >= CAPTIONER_STARTUP_GRACE_SECONDS
                    and (force or self.clock() >= self._repair_at)
                ):
                    tab_id = str(legacy.get("id") or "")
                    generation = self._generation
                    if tab_id not in self._reloaded_tabs:
                        self._navigate_existing(
                            backend,
                            selected,
                            legacy,
                            reason="captioner-stale-reload",
                            detail="reload stale captioner after heartbeat grace",
                        )
                        self._reloaded_tabs.add(tab_id)
                    else:
                        self._close_tab(backend, tab_id)
                        info = self.nav.open_url(
                            self.cdp_endpoint,
                            self.page_url,
                            reason="captioner-stale-recreate",
                            detail="recreate captioner after a stale reload generation",
                            role="captioner",
                            component="captioner-supervisor",
                            chrome_profile=self.profile,
                            backend=backend,
                            background=True,
                        )
                        if not info:
                            raise RuntimeError("navigator did not recreate stale captioner tab")
                        legacy = info
                    if generation == self._generation:
                        self._begin_generation()
                        self._schedule_repair_backoff()
                for duplicate in matches[1:]:
                    self._close_tab(backend, str(getattr(duplicate, "id", "")))
                self.attempts = 0
                self.retry_at = 0.0
                self.captioner.set_supervisor_state(
                    tab_present=True,
                    tab_id=str(legacy.get("id") or "") or None,
                    retry_at=None,
                    error=None,
                )
                return self.captioner.status()

            if self.cdp_available():
                info = self.nav.open_url(
                    self.cdp_endpoint,
                    self.page_url,
                    reason="captioner-open",
                    detail="ensure the always-on captioner singleton exists",
                    role="captioner",
                    component="captioner-supervisor",
                    chrome_profile=self.profile,
                    backend=backend,
                    background=True,
                )
                if not info:
                    raise RuntimeError("navigator did not create a captioner tab")
                tab_id = str(info.get("id") or "") or None
            else:
                argv = [
                    self.browser_path(),
                    f"--remote-debugging-port={urlsplit(self.cdp_endpoint).port}",
                    f"--user-data-dir={self.profile}",
                    "--no-first-run",
                    "--no-default-browser-check",
                    self.page_url,
                ]
                self.nav.launch(
                    argv,
                    cdp_endpoint=self.cdp_endpoint,
                    url=self.page_url,
                    profile=self.profile,
                    reason="captioner-launch",
                    detail="launch isolated no-login Chrome for the always-on captioner",
                    role="captioner",
                    component="captioner-supervisor",
                    backend=backend,
                )
                tab_id = None
            self.attempts = 0
            self.retry_at = 0.0
            self._reload_existing = False
            self._begin_generation()
            self.captioner.set_supervisor_state(
                tab_present=True, tab_id=tab_id, retry_at=None, error=None
            )
            return self.captioner.status()
        except Exception as error:  # caller exposes bounded retry state
            return self._failure(error)

    def shutdown(self) -> dict[str, Any]:
        """Stop and close only same-origin captioner pages; never the browser."""

        stopped = 0
        errors: list[str] = []
        try:
            backend = self.nav.get_browser_backend()
            tabs = backend.list_tabs(self.cdp_endpoint) if self.cdp_available() else []
            for target in self._matching(tabs):
                tab_id = str(getattr(target, "id", "") or "")
                page = None
                try:
                    page = backend.attach(target)
                    evaluate = getattr(page, "evaluate", None)
                    script = (
                        "window.__wsCollabCaptionerShutdown"
                        " ? window.__wsCollabCaptionerShutdown()"
                        " : null"
                    )
                    if callable(evaluate):
                        evaluate(script)
                    else:
                        backend.evaluate_navigation(page, script)
                except Exception as error:
                    errors.append(str(error)[:200])
                finally:
                    if page is not None:
                        try:
                            backend.close_connection(page)
                        except Exception as error:
                            errors.append(str(error)[:200])
                if tab_id:
                    try:
                        self._close_tab(backend, tab_id)
                        stopped += 1
                    except Exception as error:
                        errors.append(str(error)[:200])
        except Exception as error:
            errors.append(str(error)[:200])
        self.captioner.begin_page_generation()
        self.captioner.set_supervisor_state(
            tab_present=False,
            tab_id=None,
            retry_at=None,
            error="; ".join(errors) or None,
        )
        return {"stopped_tabs": stopped, "errors": errors}

    async def run(self, interval: float = 5.0) -> None:
        await asyncio.sleep(min(2.0, interval))
        while True:
            await asyncio.to_thread(self.run_cycle)
            await asyncio.sleep(interval)
