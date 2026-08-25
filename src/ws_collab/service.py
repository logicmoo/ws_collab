"""The shared WS_COLLAB service layer.

Every essential capability lives here exactly once. REST and WebSocket are thin
transports over this object, which is what guarantees parity: an event written
through either transport is durably appended, fanned out to live subscribers, and
visible to the other transport immediately, with identical IDs, cursors,
idempotency, filters, validation, auditing, and worker/routing/prompt logic.
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .audio.capture import CaptureService
from .audio.devices import DeviceRegistry
from .audio.routing import RoutingManager
from .audio.segment import AudioSegment
from .classify import SourceClassifier
from .config import Config
from .cursors import CursorManager
from .disambiguator import build_disambiguator
from .errors import AuthorizationError, ConflictError, NotFoundError, ValidationError
from .events import (
    AGENT_SPEECH_STARTED,
    CONVERSATION_MESSAGE,
    DYNAMIC_STREAMS,
    HEARD_SPEECH,
    STREAM_AUDIT,
    STREAM_CONVERSATION,
    STREAM_DIAGNOSTICS,
    STREAM_STT_TRANSCRIPTS,
    STREAM_TRANSLATED_AUDIO,
    STREAM_TTS,
    STREAM_ROLES,
    STREAM_STATUSES,
    STREAM_PURPOSES,
    STREAMS,
    STT_ENGINE_ERROR,
    STT_FINAL_RESULT,
    STT_PARTIAL_RESULT,
    TRANSCRIPT_FILTERED,
    TRANSCRIPT_RESOLVED,
    TTS_AUDIO_DETECTED_BY_MICROPHONE,
    TTS_TRANSCRIPTION_EVALUATED,
    Event,
    utc_now_iso,
    validate_new_event,
)
from .notify import Broker
from .prompt import PromptManager
from .sound_settings import SoundSettings
from .stt import build_engines, run_stt
from .stt.base import Hypothesis
from .tts import accuracy as accuracy_metrics
from .tts.engine import TtsEngine
from .tts.voices import VoiceManager
from .workers import WorkerMonitor
from . import __version__


# The canonical capture source that STT engine routes hang off.
DEFAULT_ROUTE_SOURCE = "microphone"

# Safety ceiling for a single read scan. `limit` is meant to bound the PRODUCED
# stream, not the pre-filter read window, so virtual/merge streams scan sources up
# to this ceiling (effectively "all") and only truncate the final result.
_MAX_SCAN = 100_000


def _markdown_title(path: Path) -> str:
    """First heading of a markdown file, for a readable document list."""

    try:
        with open(path, "r", encoding="utf-8") as handle:
            for _ in range(40):
                line = handle.readline()
                if not line:
                    break
                if line.startswith("#"):
                    return line.lstrip("#").strip()
    except OSError:
        pass
    return path.stem.replace("_", " ").title()


def _build_predicate(filters: dict[str, Any] | None) -> Callable[[Event], bool] | None:
    if not filters:
        return None
    types = set(filters.get("types") or ([filters["type"]] if filters.get("type") else []))
    source_id = filters.get("source_id")
    source_kind = filters.get("source_kind")
    correlation_id = filters.get("correlation_id")
    since = filters.get("since")
    until = filters.get("until")
    text = (filters.get("text") or "").lower()

    def predicate(event: Event) -> bool:
        if types and event.type not in types:
            return False
        if source_id and event.source_id != source_id:
            return False
        if source_kind and event.source_kind != source_kind:
            return False
        if correlation_id and event.correlation_id != correlation_id:
            return False
        if since and event.ts < since:
            return False
        if until and event.ts > until:
            return False
        if text:
            haystack = f"{event.type} {event.source_id} {event.data}".lower()
            if text not in haystack:
                return False
        return True

    return predicate


class WsCollabService:
    def __init__(self, config: Config, store):
        self.config = config
        self.store = store
        self.started_at = time.time()
        # Unique per process start. Clients compare this across (re)connects and
        # reload themselves when it changes, so a restart swaps in freshly hosted
        # assets instead of leaving stale HTML/JS running against the new server.
        self.boot_id = uuid.uuid4().hex
        self.broker = Broker()

        # Audit sink used by every subsystem so security-relevant changes are durable.
        self._audit_sink = lambda payload: self.publish(
            stream=STREAM_AUDIT,
            type=str(payload.get("type", "AUDIT")),
            data=payload,
            source_kind="system",
            source_id="system",
        )

        self.cursors = CursorManager(config.cursors_dir, audit_sink=self._audit_sink)
        self.devices = DeviceRegistry(config)
        self.routing = RoutingManager(config.state_dir, audit_sink=self._audit_sink)
        self.sound_settings = SoundSettings(config.state_dir)
        self.voices = VoiceManager(config, config.state_dir, audit_sink=self._audit_sink)
        self.workers = WorkerMonitor(config, self.publish, announce=self._announce)
        self.classifier = SourceClassifier(config.echo_policy)
        self.disambiguator = build_disambiguator(config)
        self.stt_engines, self.stt_warnings = build_engines(config)
        self.tts = TtsEngine(config, self.publish)
        self.capture = CaptureService(
            config, self.devices, self.publish, self.process_segment, is_tts_speaking=lambda: self.tts.is_speaking
        )
        # Restore the operator's persisted capture-device choice so a restart
        # resumes on the same input instead of the config/system default.
        saved_capture_device = self.sound_settings.get("capture_device")
        if saved_capture_device:
            self.capture.set_preferred_device(saved_capture_device)
        self.prompt = PromptManager(config, self.publish, read_history=self._prompt_history_events)
        self.accuracy = accuracy_metrics.AccuracyAccumulator()

        self._monitor_task: asyncio.Task | None = None
        self._warnings = list(config.warnings) + list(self.stt_warnings)

        # Client-created ("dynamic") mailboxes the server hosts, restored from a
        # durable registry so they survive restarts.
        self._dynamic_mailboxes: dict[str, dict[str, Any]] = {}
        self._load_mailbox_registry()

        # Agent (user) registry: arbitrary per-identity properties, durable.
        self._agents: dict[str, dict[str, Any]] = {}
        self._load_agent_registry()

        # Virtual (emulated) read-only mailboxes: name -> spec. Each projects a
        # source (a disk JSON file, a self:/http endpoint, or a merge: of other
        # mailboxes) as a read-only mailbox. server-agents is the default entry.
        # Runtime-created ones (e.g. a saved merge combo) are durable and marked
        # runtime="1"; config entries are re-applied on top and win on clashes.
        self._virtual: dict[str, dict[str, str]] = {}
        self._load_virtual_registry()
        for entry in (getattr(config, "virtual_mailboxes", None) or []):
            if entry.get("mailbox") and entry.get("source"):
                self._virtual[str(entry.get("mailbox"))] = {
                    "source": str(entry.get("source", "")),
                    "purpose": str(entry.get("purpose", "")),
                }
        # Global namespace prefix for this server's mailboxes (federation).
        self._global_name = str(getattr(config, "global_name", "") or "").strip()

        # Per-stream, per-field cache of the last 16 distinct values seen (plus each
        # field's inferred value type), so the render/filter pickers can offer
        # candidates and later use them intelligently. Durable on disk.
        self._field_cache: dict[str, dict[str, list[str]]] = {}
        self._field_types: dict[str, dict[str, str]] = {}
        # Per-field cache-limit overrides, layered: a per-(stream,field) override wins
        # over a global by-field override ("cache-overrides"), which wins over the
        # default cached_limit.
        self._field_overrides_global: dict[str, int] = {}
        self._field_overrides_stream: dict[str, dict[str, int]] = {}
        self._field_cache_dirty = False
        self._field_cache_saved_at = 0.0
        self._load_field_cache()

    # ------------------------------------------- per-field value cache (candidates)
    _FIELD_CACHE_SKIP = {"id", "raw", "text", "timestamp", "ts", "seq", "mailboxId", "forwarded_by"}
    _FIELD_CACHE_MAX = 16

    def _cache_config_path(self) -> Path:
        return Path(self.store.directory) / "field_cache_config.json"

    def _cache_data_path(self) -> Path:
        return Path(self.store.directory) / "fields_seen_in_streams.json"

    def _load_field_cache(self) -> None:
        import json

        # --- config: layered per-field cache-limit overrides ------------------
        try:
            cfg = json.loads(self._cache_config_path().read_text("utf-8"))
        except Exception:
            cfg = {}
        if isinstance(cfg, dict):
            glob = cfg.get("cache-overrides")
            if isinstance(glob, dict):
                for field, lim in glob.items():
                    if isinstance(field, str) and isinstance(lim, (int, float)) and int(lim) > 0:
                        self._field_overrides_global[field] = int(lim)
            streams_cfg = cfg.get("streams")
            if isinstance(streams_cfg, dict):
                for stream, over in streams_cfg.items():
                    if isinstance(stream, str) and isinstance(over, dict):
                        self._field_overrides_stream[stream] = {
                            str(f): int(v) for f, v in over.items() if isinstance(v, (int, float)) and int(v) > 0
                        }

        # --- data: cached values + inferred types ----------------------------
        try:
            data = json.loads(self._cache_data_path().read_text("utf-8"))
        except Exception:
            data = {}
        if isinstance(data, dict):
            for stream, entry in data.items():
                if not isinstance(stream, str) or not isinstance(entry, dict):
                    continue
                out: dict[str, list[str]] = {}
                out_types: dict[str, str] = {}
                fields = entry.get("fields") if isinstance(entry.get("fields"), dict) else {}
                for field, decl in fields.items():
                    if not isinstance(field, str) or not isinstance(decl, dict):
                        continue
                    values = decl.get("values")
                    if not isinstance(values, list):
                        continue
                    out_types[field] = str(decl.get("type") or "string")
                    out[field] = [str(v) for v in values][-self._field_limit(stream, field):]
                self._field_cache[stream] = out
                self._field_types[stream] = out_types

    def _field_limit(self, stream: str, field: str) -> int:
        """Effective cache limit: per-(stream,field) > global by-field > default."""
        s = self._field_overrides_stream.get(stream, {})
        if field in s:
            return s[field]
        if field in self._field_overrides_global:
            return self._field_overrides_global[field]
        return self._FIELD_CACHE_MAX

    def _cache_config_doc(self) -> dict[str, Any]:
        # cache_config.json: default + layered per-field limit overrides (editable).
        cfg: dict[str, Any] = {"default_limit": self._FIELD_CACHE_MAX}
        if self._field_overrides_global:
            cfg["cache-overrides"] = dict(self._field_overrides_global)
        streams = {s: dict(o) for s, o in self._field_overrides_stream.items() if o}
        if streams:
            cfg["streams"] = streams
        return cfg

    def _cache_data_doc(self) -> dict[str, Any]:
        # cache_data.json: the cached values per stream (auto-generated).
        return {
            stream: {
                "cached_limit": self._FIELD_CACHE_MAX,
                "fields": {
                    field: {
                        "type": self._field_types.get(stream, {}).get(field, "string"),
                        "cached_limit": self._field_limit(stream, field),
                        "values": values,
                    }
                    for field, values in fields.items()
                },
            }
            for stream, fields in self._field_cache.items()
        }

    def _save_cache_config(self) -> None:
        import json
        import os

        path = self._cache_config_path()
        tmp = path.with_name(path.name + ".tmp")
        try:
            tmp.write_text(json.dumps(self._cache_config_doc(), indent=2), "utf-8")
            os.replace(tmp, path)
        except OSError:
            pass

    def _save_field_cache(self, *, force: bool = False) -> None:
        import json
        import os

        if not self._field_cache_dirty:
            return
        now = time.time()
        if not force and (now - self._field_cache_saved_at) < 2.0:
            return
        path = self._cache_data_path()
        tmp = path.with_name(path.name + ".tmp")
        try:
            tmp.write_text(json.dumps(self._cache_data_doc(), indent=2), "utf-8")
            os.replace(tmp, path)
            self._field_cache_dirty = False
            self._field_cache_saved_at = now
        except OSError:
            pass

    def _remember_field_values(self, stream: str, messages: list[dict[str, Any]]) -> None:
        """Record the last N distinct values of each primitive field per stream,
        plus each field's inferred value type (string/number/boolean, or mixed)."""
        if not stream or not messages:
            return
        bucket = self._field_cache.setdefault(stream, {})
        types = self._field_types.setdefault(stream, {})

        def vtype(v: Any) -> str:
            if isinstance(v, bool):
                return "boolean"
            if isinstance(v, (int, float)):
                return "number"
            return "string"

        def offer(field: str, value: Any) -> None:
            if field in self._FIELD_CACHE_SKIP or value is None or isinstance(value, (dict, list)):
                return
            sval = str(value)
            if sval == "":
                return
            t = vtype(value)
            prior = types.get(field)
            if prior is None:
                types[field] = t
            elif prior != t and prior != "mixed":
                types[field] = "mixed"
            seq = bucket.setdefault(field, [])
            if seq and seq[-1] == sval:
                return
            if sval in seq:
                seq.remove(sval)
            seq.append(sval)
            lim = self._field_limit(stream, field)
            if len(seq) > lim:
                del seq[: len(seq) - lim]
            self._field_cache_dirty = True

        for m in messages:
            rec = m if isinstance(m, dict) else {}
            for k, v in rec.items():
                offer(k, v)
            raw = rec.get("raw")
            if isinstance(raw, dict):
                for k, v in raw.items():
                    offer(k, v)

    def field_values(self, mailbox: str) -> dict[str, Any]:
        """Candidate field values (and inferred types) for a mailbox, aggregated
        across a merge's members."""
        streams: list[str] = []
        spec = self._virtual.get(mailbox)
        if spec:
            source = str(spec.get("source", ""))
            if source.startswith("merge:"):
                target = source[len("merge:"):].strip()
                if target in ("*", "all"):
                    streams = [s for s in STREAMS] + list(self._dynamic_mailboxes)
                else:
                    streams = [n.strip() for n in target.split(",") if n.strip()]
            else:
                streams = [mailbox]
        else:
            streams = [mailbox]
        merged: dict[str, list[str]] = {}
        types: dict[str, str] = {}
        for s in streams:
            for field, values in self._field_cache.get(s, {}).items():
                dest = merged.setdefault(field, [])
                for v in values:
                    if v in dest:
                        dest.remove(v)
                    dest.append(v)
                if len(dest) > self._FIELD_CACHE_MAX:
                    del dest[: len(dest) - self._FIELD_CACHE_MAX]
            for field, t in self._field_types.get(s, {}).items():
                prior = types.get(field)
                types[field] = t if prior is None else (prior if prior == t else "mixed")
        limits: dict[str, int] = {}
        for s in streams:
            for field in self._field_cache.get(s, {}):
                limits[field] = max(limits.get(field, 0), self._field_limit(s, field))
        fields = {
            field: {"type": types.get(field, "string"), "cached_limit": limits.get(field, self._FIELD_CACHE_MAX), "values": values}
            for field, values in merged.items()
        }
        return {"mailbox": mailbox, "cached_limit": self._FIELD_CACHE_MAX, "fields": fields}

    def set_field_cache_limit(self, field: str, limit: int, *, stream: str = "") -> dict[str, Any]:
        """Set a per-field cache limit. With ``stream`` it is a per-(stream,field)
        override; without, it is a global by-field override ("cache-overrides")."""
        field = str(field or "").strip()
        if not field:
            raise ValidationError("field is required")
        lim = int(limit)
        if lim <= 0:
            raise ValidationError("limit must be a positive integer")
        if stream:
            self._field_overrides_stream.setdefault(stream, {})[field] = lim
        else:
            self._field_overrides_global[field] = lim
        # Re-trim any affected cached lists to the new effective limit.
        for s, fields in self._field_cache.items():
            if stream and s != stream:
                continue
            seq = fields.get(field)
            if seq is not None:
                eff = self._field_limit(s, field)
                if len(seq) > eff:
                    del seq[: len(seq) - eff]
        self._field_cache_dirty = True
        self._save_cache_config()
        self._save_field_cache(force=True)
        return {"stream": stream or "*", "field": field, "cached_limit": lim}

    def get_cache_config_file(self) -> dict[str, Any]:
        """Raw contents of the editable field-cache CONFIG file (limit overrides)."""
        path = self._cache_config_path()
        try:
            content = path.read_text("utf-8")
        except Exception:
            import json

            content = json.dumps(self._cache_config_doc(), indent=2)
        return {"path": str(path), "content": content}

    def get_cache_data_file(self) -> dict[str, Any]:
        """Raw contents of the auto-generated field-cache DATA file (seen values)."""
        path = self._cache_data_path()
        try:
            content = path.read_text("utf-8")
        except Exception:
            content = "{}"
        return {"path": str(path), "content": content}

    def set_cache_config_file(self, content: str) -> dict[str, Any]:
        """Overwrite the field-cache CONFIG file with edited JSON, then reload it and
        re-trim the cached data to the new limits."""
        import json
        import os

        try:
            parsed = json.loads(content)
        except Exception as error:
            raise ValidationError(f"invalid JSON: {error}")
        if not isinstance(parsed, dict):
            raise ValidationError("the cache config must be a JSON object")
        path = self._cache_config_path()
        tmp = path.with_name(path.name + ".tmp")
        try:
            tmp.write_text(json.dumps(parsed, indent=2), "utf-8")
            os.replace(tmp, path)
        except OSError as error:
            raise ValidationError(f"could not write file: {error}")
        # Reload overrides and re-trim existing values to the new effective limits.
        self._field_overrides_global.clear()
        self._field_overrides_stream.clear()
        glob = parsed.get("cache-overrides")
        if isinstance(glob, dict):
            for f, lim in glob.items():
                if isinstance(f, str) and isinstance(lim, (int, float)) and int(lim) > 0:
                    self._field_overrides_global[f] = int(lim)
        streams_cfg = parsed.get("streams")
        if isinstance(streams_cfg, dict):
            for s, over in streams_cfg.items():
                if isinstance(s, str) and isinstance(over, dict):
                    self._field_overrides_stream[s] = {
                        str(f): int(v) for f, v in over.items() if isinstance(v, (int, float)) and int(v) > 0
                    }
        for s, fields in self._field_cache.items():
            for f, seq in fields.items():
                eff = self._field_limit(s, f)
                if len(seq) > eff:
                    del seq[: len(seq) - eff]
        self._field_cache_dirty = True
        self._save_field_cache(force=True)
        return {"ok": True, "path": str(path)}

    # ------------------------------------------------------------------ core io
    def publish(
        self,
        *,
        stream: str,
        type: str,
        data: dict[str, Any] | None = None,
        source_id: str = "system",
        source_kind: str = "system",
        correlation_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        event = validate_new_event(
            stream,
            type,
            data or {},
            source_id=source_id,
            source_kind=source_kind,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
        )
        result = self.store.append(event, on_write=self.broker.publish)
        return {
            "id": result.event.id,
            "seq": result.event.seq,
            "cursor": result.cursor,
            "duplicate": result.duplicate,
            "stream": stream,
            "event_type": type,
            "server_time": utc_now_iso(),
        }

    def read_events(
        self,
        stream: str,
        *,
        after: str | None = None,
        limit: int = 100,
        filters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if stream not in STREAMS:
            raise ValidationError(f"unknown stream: {stream!r}", details={"allowed": sorted(STREAMS)})
        predicate = _build_predicate(filters)
        page = self.store.read(stream, after, max(1, min(limit, 1000)), predicate)
        return {
            "stream": stream,
            "events": [event.to_dict() for event in page.events],
            "next_cursor": page.next_cursor,
            "has_more": page.has_more,
            "server_time": page.server_time,
            "malformed": page.malformed,
        }

    async def read_events_wait(
        self,
        stream: str,
        *,
        after: str | None = None,
        limit: int = 100,
        filters: dict[str, Any] | None = None,
        wait_ms: int = 0,
    ) -> dict[str, Any]:
        """Cursor read with bounded long-polling (no tight client loop needed)."""

        page = self.read_events(stream, after=after, limit=limit, filters=filters)
        if page["events"] or wait_ms <= 0:
            return page
        loop = asyncio.get_running_loop()
        predicate = _build_predicate(filters)
        sub = self.broker.subscribe({stream}, loop, predicate)
        try:
            await asyncio.wait_for(sub.queue.get(), timeout=wait_ms / 1000.0)
        except asyncio.TimeoutError:
            return page
        finally:
            self.broker.unsubscribe(sub.id)
        return self.read_events(stream, after=after, limit=limit, filters=filters)

    def tail(self, stream: str, count: int = 50, filters: dict[str, Any] | None = None) -> dict[str, Any]:
        if stream not in STREAMS:
            raise ValidationError(f"unknown stream: {stream!r}", details={"allowed": sorted(STREAMS)})
        predicate = _build_predicate(filters)
        events = self.store.tail(stream, max(1, min(count, 2000)), predicate)
        return {"stream": stream, "events": [event.to_dict() for event in events], "server_time": utc_now_iso()}

    # ------------------------------------------------------------- capabilities
    def health(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "version": __version__,
            "server_time": utc_now_iso(),
            "uptime_seconds": round(time.time() - self.started_at, 1),
        }

    def capabilities(self) -> dict[str, Any]:
        return {
            "version": __version__,
            "boot_id": self.boot_id,
            "product": "WS_COLLAB",
            "transports": ["http", "https", "ws", "wss"],
            "rest_base": "/ws_collab",
            "versioned_base": "/ws_collab/v1",
            "streams": STREAMS,
            "mailboxes": STREAMS,
            "stream_roles": STREAM_ROLES,
            "auth_methods": ["bearer_token", "session_cookie"],
            "features": {
                "cursor_pagination": True,
                "idempotent_writes": True,
                "long_polling": True,
                "conditional_requests": True,
                "websocket_resume": True,
                "three_stt_engines": len(self.stt_engines),
                "disambiguator": getattr(self.disambiguator, "method_name", "deterministic"),
                "echo_policy": self.config.echo_policy,
                "voice_policy": self.config.tts_policy,
            },
            "warnings": self._warnings,
        }

    # ------------------------------------------------------------- conversation
    def add_conversation(
        self,
        text: str,
        *,
        source_id: str,
        source_kind: str,
        correlation_id: str | None = None,
        idempotency_key: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(text, str) or not text.strip():
            raise ValidationError("conversation 'text' is required")
        payload = {"text": text, **(data or {})}
        return self.publish(
            stream=STREAM_CONVERSATION,
            type=CONVERSATION_MESSAGE,
            data=payload,
            source_id=source_id,
            source_kind=source_kind,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
        )

    # ---------------------------------------------------- mailboxes (streams)
    # The mailbox-admin chat UI treats every durable JSONL stream as a
    # "mailbox": the stream file is the mailbox and its events are the messages.
    # Exposing streams under a mailbox-shaped API lets the shared workbench
    # ChatConversation browse ws_collab streams with no separate backend.
    @staticmethod
    def _event_to_message(event: dict[str, Any]) -> dict[str, Any]:
        """Map a stream event to the ChatMessage shape ChatConversation reads."""
        data = event.get("data") or {}
        text = ""
        for key in ("text", "message", "content", "summary", "utterance"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                text = value
                break
        return {
            "id": event.get("id"),
            "timestamp": event.get("ts"),
            "from": event.get("source_id"),
            "to": data.get("to") or data.get("target") or event.get("stream"),
            "send_to": data.get("send_to"),
            "text": text,
            "type": event.get("type"),
            "mailboxId": event.get("stream"),
            "author": event.get("source_id"),
            "authorName": data.get("author_name") or event.get("source_id"),
            "mailboxName": event.get("stream"),
            "raw": event,
        }

    # -------------------------------------------- dynamic mailbox registry
    def _mailbox_registry_path(self) -> Path:
        return Path(self.store.directory) / "mailboxes.json"

    def _load_mailbox_registry(self) -> None:
        import json

        try:
            raw = json.loads(self._mailbox_registry_path().read_text("utf-8"))
        except Exception:
            return
        if not isinstance(raw, dict):
            return
        for name, meta in raw.items():
            if not isinstance(name, str) or name in STREAMS:
                continue
            self._dynamic_mailboxes[name] = meta if isinstance(meta, dict) else {}
            self.store.register_mailbox(name)
            DYNAMIC_STREAMS.add(name)

    def _save_mailbox_registry(self) -> None:
        import json
        import os

        path = self._mailbox_registry_path()
        tmp = path.with_name(path.name + ".tmp")
        try:
            tmp.write_text(json.dumps(self._dynamic_mailboxes, indent=2), "utf-8")
            os.replace(tmp, path)
        except OSError:
            pass

    # ------------------------------------------ virtual mailbox registry (durable)
    def _virtual_registry_path(self) -> Path:
        return Path(self.store.directory) / "virtual_mailboxes.json"

    def _load_virtual_registry(self) -> None:
        """Rehydrate runtime-created virtual mailboxes (e.g. saved merge combos)."""
        import json

        try:
            raw = json.loads(self._virtual_registry_path().read_text("utf-8"))
        except Exception:
            return
        if not isinstance(raw, dict):
            return
        for name, spec in raw.items():
            if not isinstance(name, str) or name in STREAMS or not isinstance(spec, dict):
                continue
            if not spec.get("source"):
                continue
            entry: dict[str, Any] = {}
            for k, v in spec.items():
                key = str(k)
                if key == "rules" and isinstance(v, list):
                    entry[key] = [r for r in v if isinstance(r, dict)]
                else:
                    entry[key] = v if isinstance(v, (str, int, float, bool)) else str(v)
            entry["runtime"] = "1"
            self._virtual[name] = entry

    def _save_virtual_registry(self) -> None:
        """Persist only runtime-created virtual mailboxes; config ones stay in config."""
        import json
        import os

        runtime = {n: s for n, s in self._virtual.items() if s.get("runtime") == "1"}
        path = self._virtual_registry_path()
        tmp = path.with_name(path.name + ".tmp")
        try:
            tmp.write_text(json.dumps(runtime, indent=2), "utf-8")
            os.replace(tmp, path)
        except OSError:
            pass

    def _known_mailbox(self, name: str) -> bool:
        return name in STREAMS or name in self._dynamic_mailboxes or name in self._virtual

    def _stream_origin(self, name: str) -> str:
        """Which server a stream logically belongs to, for UI disambiguation.
        Streams named ``workbench_*`` are the workbench server's own streams;
        everything else is a native ws_collab stream."""
        return "workbench" if str(name).startswith("workbench_") else "ws_collab"

    def _writable_mailbox(self, name: str) -> bool:
        if name in self._virtual:
            return False
        if name in self._dynamic_mailboxes:
            return self._dynamic_mailboxes[name].get("writable", True) is not False
        return name in STREAMS

    def _global_id(self, name: str) -> str:
        """The globally-unique name for a local mailbox: an explicit per-mailbox
        override if set, else the server's global prefix + local name."""
        override = None
        if name in self._dynamic_mailboxes:
            override = self._dynamic_mailboxes[name].get("global_name")
        elif name in self._virtual:
            override = self._virtual[name].get("global_name")
        if override:
            return str(override)
        return f"{self._global_name}/{name}" if self._global_name else name

    # -------------------------------------------------- agent (user) registry
    def _agent_registry_path(self) -> Path:
        return Path(self.store.directory) / "agents.json"

    def _load_agent_registry(self) -> None:
        import json

        try:
            raw = json.loads(self._agent_registry_path().read_text("utf-8"))
        except Exception:
            return
        if isinstance(raw, dict):
            for name, props in raw.items():
                if isinstance(name, str):
                    self._agents[name] = props if isinstance(props, dict) else {}

    def _save_agent_registry(self) -> None:
        import json
        import os

        path = self._agent_registry_path()
        tmp = path.with_name(path.name + ".tmp")
        try:
            tmp.write_text(json.dumps(self._agents, indent=2), "utf-8")
            os.replace(tmp, path)
        except OSError:
            pass

    def set_agent(self, agent_id: str, properties: dict[str, Any] | None = None) -> dict[str, Any]:
        """Create or update an agent (user) with arbitrary properties."""
        agent_id = str(agent_id or "").strip()
        if not agent_id:
            raise ValidationError("agent id is required")
        record = dict(self._agents.get(agent_id) or {})
        if isinstance(properties, dict):
            record.update(properties)
        record.setdefault("created_at", utc_now_iso())
        record["updated_at"] = utc_now_iso()
        self._agents[agent_id] = record
        self._save_agent_registry()
        return {"id": agent_id, "properties": record}

    def _virtual_message(self, mailbox: str, record: dict[str, Any]) -> dict[str, Any]:
        """Project one source record as a message for an emulated mailbox."""
        record_id = str(record.get("id") or record.get("name") or "")
        return {
            "id": f"{mailbox}:{record_id}" if record_id else f"{mailbox}:{id(record)}",
            "timestamp": record.get("updated_at") or record.get("created_at"),
            "from": "server",
            "to": record_id or mailbox,
            "send_to": mailbox,
            "text": record_id or str(record.get("text") or ""),
            "type": "RECORD",
            "mailboxId": mailbox,
            "author": "server",
            "authorName": "server",
            "mailboxName": mailbox,
            "raw": record,
        }

    @staticmethod
    def _records_from_json(data: Any) -> list[dict[str, Any]]:
        if isinstance(data, list):
            return [item if isinstance(item, dict) else {"value": item} for item in data]
        if isinstance(data, dict):
            records: list[dict[str, Any]] = []
            for key, value in data.items():
                records.append({"id": key, **value} if isinstance(value, dict) else {"id": key, "value": value})
            return records
        return []

    def _resolve_virtual_records(self, mailbox: str) -> list[dict[str, Any]]:
        """Resolve a virtual mailbox's source into a list of records."""
        spec = self._virtual.get(mailbox)
        if not spec:
            return []
        source = str(spec.get("source", "")).strip()
        # self endpoints, resolved internally (no HTTP self-call)
        for prefix in ("self:", "http://self/", "https://self/"):
            if source.startswith(prefix):
                endpoint = source[len(prefix):].strip("/")
                if endpoint in ("mailbox/agents", "agents"):
                    return self.mailbox_agents().get("agents", [])
                if endpoint in ("mailbox/mailboxes", "mailboxes"):
                    return self.list_mailboxes().get("mailboxes", [])
                return []
        # disk JSON file (relative paths resolve under the state directory)
        if source.startswith(("file:", "./", "../", "/")) or source.endswith(".json"):
            path_str = source[5:] if source.startswith("file:") else source
            path = Path(path_str)
            if not path.is_absolute():
                path = Path(self.store.directory) / path
            try:
                import json

                return self._records_from_json(json.loads(path.read_text("utf-8-sig")))
            except Exception:
                return []
        # remote http(s) endpoint — a participant may cap/paginate its /v1, often
        # *silently* (ask per_page=500, get its hard-capped 200, with no has_more).
        # Strategy: (1) discover the real cap from the first page's length; (2) page
        # by that EFFECTIVE size — for page+per_page APIs the next page is computed
        # from how many we've collected, so a 500→200 mismatch can't skip rows; for
        # offset APIs advance by (page-overlap); follow next_cursor when offered.
        # (3) keep going while new records appear, stopping on an empty/no-new page.
        # Dedup by id covers overlaps; _MAX_SCAN and a page ceiling bound it.
        if source.startswith(("http://", "https://")):
            import json
            import re as _re
            import urllib.request
            from urllib.parse import parse_qs, quote, urlparse

            def _fetch(url: str) -> Any:
                try:
                    with urllib.request.urlopen(url, timeout=3) as response:  # noqa: S310 - operator-configured
                        return json.loads(response.read().decode("utf-8"))
                except Exception:
                    return None

            def _page_records(payload: Any) -> list[dict[str, Any]]:
                if isinstance(payload, dict):
                    for key in ("messages", "events", "agents", "mailboxes", "items"):
                        if isinstance(payload.get(key), list):
                            return [it if isinstance(it, dict) else {"value": it} for it in payload[key]]
                return self._records_from_json(payload) if payload is not None else []

            def _with_param(url: str, name: str, value: Any) -> str:
                pat = rf"([?&]{_re.escape(name)}=)[^&]*"
                if _re.search(pat, url):
                    return _re.sub(pat, lambda m: m.group(1) + quote(str(value)), url)
                return f"{url}{'&' if '?' in url else '?'}{name}={quote(str(value))}"

            paging = spec.get("paging") if isinstance(spec.get("paging"), dict) else {}
            overlap = int(paging.get("overlap") or 2)
            page_step = int(paging.get("step") or 0)
            qs = parse_qs(urlparse(source).query)
            page_param = next((p for p in ("page", "pageNumber", "p") if p in qs), None)
            per_page_param = next((p for p in ("per_page", "perPage", "pageSize", "page_size") if p in qs), "per_page")
            offset_param = next((p for p in ("offset", "start", "skip", "after") if p in qs), "offset")
            mode = str(paging.get("mode") or ("page" if page_param else "offset")).lower()
            page_base = int(paging.get("base", 1))
            # Requested page size — a probe for the server's real cap.
            req_pp = int(paging.get("limit") or 0)
            if req_pp <= 0:
                for p in (per_page_param, "limit"):
                    if p in qs:
                        try:
                            req_pp = int(qs[p][0])
                        except Exception:
                            req_pp = 0
                        if req_pp > 0:
                            break
            if req_pp <= 0:
                req_pp = 500
            size_param = per_page_param if mode == "page" else "limit"
            source = _with_param(source, size_param, req_pp)

            records: list[dict[str, Any]] = []
            seen: set[str] = set()
            seen_cursors: set[str] = set()
            effective_pp: int | None = None
            page_num = page_base
            offset = 0
            url = _with_param(source, page_param or "page", page_num) if mode == "page" else source
            for _ in range(2000):  # page-count safety ceiling
                payload = _fetch(url)
                if payload is None:
                    break
                page = _page_records(payload)
                if not page:
                    break  # an empty page is the reliable "done" signal
                if effective_pp is None:
                    effective_pp = len(page)  # discover the server's real cap
                new = 0
                for rec in page:
                    rid = rec.get("id") if isinstance(rec, dict) else None
                    key = f"id:{rid}" if rid is not None else json.dumps(rec, sort_keys=True, default=str)
                    if key in seen:
                        continue
                    seen.add(key)
                    records.append(rec)
                    new += 1
                if len(records) >= _MAX_SCAN:
                    break
                # We NEVER trust has_more as a stop signal (peers lie or send it
                # prematurely). The only reliable ends are an empty page (handled
                # above) or a page that yields no NEW records after dedup.
                if new == 0:
                    break
                nxt = payload.get("next_cursor") if isinstance(payload, dict) else None
                if nxt and str(nxt) not in seen_cursors:
                    seen_cursors.add(str(nxt))  # next_cursor is only an advance hint
                    url = _with_param(source, "after", nxt)
                    continue
                if mode == "page":
                    pp = effective_pp or len(page) or 1
                    # Continue from the page the last collected unit lands on (by the
                    # EFFECTIVE size), realigning per_page so a silent cap can't skip.
                    page_num = (len(records) // pp) + page_base
                    url = _with_param(_with_param(source, per_page_param, pp), page_param or "page", page_num)
                else:
                    offset += page_step if page_step > 0 else max(1, len(page) - overlap)
                    url = _with_param(source, offset_param, offset)
            return records[:_MAX_SCAN]
        return []

    _MAILBOX_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

    def _mailbox_descriptor(self, name: str, stats: dict[str, Any] | None = None) -> dict[str, Any]:
        if stats is None:
            stats = {row["stream"]: row for row in self.store.stats()}
        v1 = "/ws_collab/v1"
        mount = "/ws_collab"
        dyn = self._dynamic_mailboxes.get(name) or {}
        return {
            "id": name,
            "name": name,
            "global_name": self._global_id(name),
            "purpose": dyn.get("purpose") or STREAM_PURPOSES.get(name, ""),
            "kind": "stream" if name in STREAMS else "mailbox",
            "source": dyn.get("source") or "jsonl",
            "origin": self._stream_origin(name),
            "transports": ["jsonl", "ws"],
            "hidden": bool(dyn.get("hidden", False)),
            "writable": self._writable_mailbox(name),
            "messages": int((stats.get(name) or {}).get("seq") or 0),
            "filename": STREAMS.get(name) or f"{name}.jsonl",
            "endpoints": {
                "read": f"{v1}/mailbox/messages?mailbox={name}",
                "send": f"{v1}/mailbox/send",
                "tail": f"{v1}/streams/{name}/tail",
                "ws": f"{mount}/ws",
            },
        }

    def list_mailboxes(self) -> dict[str, Any]:
        """This place's directory of mailboxes: built-in streams plus any
        client-created mailboxes, each self-describing (name/purpose/source/
        transports + read/send/tail/ws endpoints). Hidden mailboxes are omitted;
        their unguessable name is the capability required to reach them."""
        stats = {row["stream"]: row for row in self.store.stats()}
        names = list(STREAMS) + sorted(self._dynamic_mailboxes)
        mailboxes = [
            self._mailbox_descriptor(name, stats)
            for name in names
            if not self._dynamic_mailboxes.get(name, {}).get("hidden")
        ]
        for vname in self._virtual:
            mailboxes.append(self._virtual_descriptor(vname))
        return {"place": "ws_collab", "global_name": self._global_name, "mailboxes": mailboxes, "server_time": utc_now_iso()}

    def mailbox_config(self, mailbox: str) -> dict[str, Any]:
        """The server's descriptor/config for a single mailbox (its real
        properties), or an empty object if the mailbox is unknown."""
        name = str(mailbox or "").strip()
        if not name:
            return {}
        if name in self._virtual:
            return self._virtual_descriptor(name)
        if name in STREAMS or name in self._dynamic_mailboxes:
            return self._mailbox_descriptor(name)
        return {}

    def _virtual_descriptor(self, name: str) -> dict[str, Any]:
        """Descriptor for an emulated read-only mailbox (projected server state)."""
        spec = self._virtual.get(name) or {}
        source = str(spec.get("source", ""))
        count = 0
        if source.rstrip("/").endswith("agents"):
            try:
                count = len(self.mailbox_agents().get("agents", []))
            except Exception:
                count = 0
        elif source.startswith("merge:"):
            try:
                count = len(self.mailbox_messages(name, limit=_MAX_SCAN).get("messages", []))
            except Exception:
                count = 0
        members = []
        if source.startswith("merge:"):
            target = source[len("merge:"):].strip()
            members = ["*"] if target in ("*", "all") else [n.strip() for n in target.split(",") if n.strip()]
        return {
            "id": name,
            "name": name,
            "global_name": self._global_id(name),
            "purpose": spec.get("purpose", ""),
            "kind": "merge" if source.startswith("merge:") else "registry",
            "source": "virtual",
            "origin": self._stream_origin(name),
            "definition": source,
            "members": members,
            "rules": spec.get("rules") or [],
            "policy": spec.get("policy") or "relay",
            "transports": ["jsonl"],
            "hidden": False,
            "writable": False,
            "messages": count,
            "filename": None,
            "endpoints": {"read": f"/ws_collab/v1/mailbox/messages?mailbox={name}"},
        }

    def create_mailbox(
        self,
        name: str,
        *,
        purpose: str = "",
        hidden: bool = False,
        writable: bool = True,
        global_name: str = "",
        source: str = "jsonl",
        rules: Any = None,
        policy: str = "",
        paging: Any = None,
        created_by: str = "operator",
    ) -> dict[str, Any]:
        """Begin hosting a new client-created mailbox (a durable JSONL stream).
        Idempotent: recreating an existing dynamic mailbox returns it unchanged.

        A virtual ``source`` (merge:/self:/disk:/http) creates a read-only projected
        stream instead; ``merge:*`` merges every real stream. ``rules``/``policy``
        add firewall-style RELAY/DROP filtering over the projected messages."""
        name = str(name or "").strip()
        if not self._MAILBOX_NAME_RE.match(name):
            raise ValidationError(
                "mailbox name must match ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$",
                details={"name": name},
            )
        if name in STREAMS:
            raise ConflictError(f"{name!r} is a built-in mailbox")
        source = str(source or "jsonl").strip()
        # A virtual source (merge:/self:/disk:/http) makes a read-only projected
        # mailbox rather than a hosted JSONL stream — this is what "save this
        # merge combo as a stream" produces. It is durable across restarts.
        if source.startswith(("merge:", "self:", "disk:", "http://", "https://")):
            if name in self._dynamic_mailboxes:
                raise ConflictError(f"{name!r} already exists as a hosted mailbox")
            if name in self._virtual:
                return {"created": False, "mailbox": self._virtual_descriptor(name)}
            spec: dict[str, Any] = {
                "source": source,
                "purpose": str(purpose or ""),
                "runtime": "1",
                "created_by": str(created_by or "operator"),
                "created_at": utc_now_iso(),
            }
            if global_name:
                spec["global_name"] = str(global_name)
            if isinstance(rules, list) and rules:
                spec["rules"] = [r for r in rules if isinstance(r, dict)]
            if policy:
                spec["policy"] = str(policy)
            if isinstance(paging, dict):
                pg = {k: paging[k] for k in ("limit", "step", "overlap") if isinstance(paging.get(k), (int, float))}
                if pg:
                    spec["paging"] = pg
            self._virtual[name] = spec
            self._save_virtual_registry()
            return {"created": True, "mailbox": self._virtual_descriptor(name)}
        if name in self._dynamic_mailboxes:
            return {"created": False, "mailbox": self._mailbox_descriptor(name)}
        self._dynamic_mailboxes[name] = {
            "purpose": str(purpose or ""),
            "hidden": bool(hidden),
            "writable": bool(writable),
            "global_name": str(global_name or ""),
            "source": source,
            "created_at": utc_now_iso(),
            "created_by": str(created_by or "operator"),
        }
        self.store.register_mailbox(name)
        DYNAMIC_STREAMS.add(name)
        self._save_mailbox_registry()
        return {"created": True, "mailbox": self._mailbox_descriptor(name)}

    def delete_mailbox(self, name: str) -> dict[str, Any]:
        """Stop hosting a client-created mailbox. Built-in streams cannot be
        deleted; the backing JSONL file is left on disk."""
        name = str(name or "").strip()
        if name in STREAMS:
            raise ConflictError(f"cannot delete built-in mailbox {name!r}")
        if name in self._virtual:
            if self._virtual[name].get("runtime") == "1":
                self._virtual.pop(name, None)
                self._save_virtual_registry()
                return {"id": name, "deleted": True}
            raise ConflictError(f"cannot delete config-declared virtual mailbox {name!r}")
        existed = name in self._dynamic_mailboxes
        if existed:
            self._dynamic_mailboxes.pop(name, None)
            DYNAMIC_STREAMS.discard(name)
            self._save_mailbox_registry()
        return {"id": name, "deleted": existed}

    def mailbox_agents(self) -> dict[str, Any]:
        """The users/identity directory: the operator, registered workers, agents
        in the durable registry, and any distinct source_ids seen in the
        conversation. Registry properties are merged in; ``id`` and ``kind`` win."""
        directory: dict[str, dict[str, Any]] = {}

        def ensure(agent_id: str) -> dict[str, Any]:
            agent_id = str(agent_id or "").strip()
            entry = directory.get(agent_id)
            if entry is None and agent_id:
                entry = {**(self._agents.get(agent_id) or {}), "id": agent_id}
                directory[agent_id] = entry
            return entry or {}

        ensure("operator")["kind"] = "operator"
        for worker in self.workers.list_workers():
            worker_id = str(worker.get("worker_id") or worker.get("id") or "")
            if worker_id:
                entry = ensure(worker_id)
                entry.update({key: value for key, value in worker.items() if key != "id"})
                entry["kind"] = "worker"
        for agent_id in list(self._agents):
            ensure(agent_id).setdefault("kind", "agent")
        try:
            for event in self.store.tail("conversation", 500):
                ensure(str(event.to_dict().get("source_id") or "")).setdefault("kind", "agent")
        except Exception:
            pass
        return {"agents": [entry for entry in directory.values() if entry.get("id")]}

    def _apply_filter_rules(
        self, messages: list[dict[str, Any]], rules: Any, policy: Any = None
    ) -> list[dict[str, Any]]:
        """Firewall-style, first-match RELAY/DROP filtering for a virtual stream.

        ``rules`` is an ordered list of ``{field, op, value, action}``. The first
        rule that matches a message decides its fate; if none match, the default
        ``policy`` applies (``relay`` unless set to ``drop``). Fields:
        ``any|type|text|from|to|send_to|source_id|source_kind|mailbox``. Ops:
        ``contains|equals|prefix|regex|present`` (case-insensitive). Actions and
        the policy accept synonyms: relay/accept/allow/pass vs drop/reject/deny.
        """
        import json as _json
        import re as _re

        rule_list = [r for r in rules if isinstance(r, dict)] if isinstance(rules, list) else []
        if not rule_list:
            return messages
        drop_words = {"drop", "reject", "deny", "block"}
        default_relay = str(policy or "relay").strip().lower() not in drop_words

        def field_value(m: dict[str, Any], field: str) -> str:
            field = (field or "any").strip().lower()
            if field in ("any", "*", ""):
                raw = m.get("raw")
                base = " ".join(
                    str(m.get(k, "") or "")
                    for k in ("type", "text", "from", "to", "send_to", "source_id", "source_kind", "mailboxName", "mailboxId")
                )
                return base + (" " + _json.dumps(raw, default=str) if raw is not None else "")
            alias = {"mailbox": "mailboxName", "stream": "mailboxName", "sender": "from"}.get(field, field)
            return str(m.get(alias, "") or "")

        def matches(m: dict[str, Any], rule: dict[str, Any]) -> bool:
            op = str(rule.get("op", "contains")).strip().lower()
            val = str(rule.get("value", ""))
            hay = field_value(m, str(rule.get("field", "any")))
            if op == "present":
                return hay.strip() != ""
            if op == "equals":
                return hay.lower() == val.lower()
            if op == "prefix":
                return hay.lower().startswith(val.lower())
            if op == "regex":
                try:
                    return _re.search(val, hay, _re.IGNORECASE) is not None
                except _re.error:
                    return False
            return val.lower() in hay.lower()

        out: list[dict[str, Any]] = []
        for m in messages:
            relay = default_relay
            for rule in rule_list:
                if matches(m, rule):
                    relay = str(rule.get("action", "relay")).strip().lower() not in drop_words
                    break
            if relay:
                out.append(m)
        return out

    def mailbox_messages(
        self,
        mailbox: str,
        *,
        to: str | None = None,
        sender: str | None = None,
        send_to: str | None = None,
        text: str | None = None,
        do_filter: bool = False,
        limit: int = 300,
        filters: dict[str, Any] | None = None,
        _chain: frozenset[str] = frozenset(),
    ) -> dict[str, Any]:
        """Messages in a mailbox = events in that stream (newest last).

        Unknown mailboxes read as empty rather than erroring. When ``do_filter``
        is set, the require-match bar constraints apply: ``sender`` is the chat's
        YOU (the message ``from``), ``to`` the addressed recipient, ``send_to``
        the routed mailbox (null == this mailbox) and ``text`` a case-insensitive
        substring."""
        if mailbox in self._virtual:
            # Loop prevention: a virtual stream already being resolved higher in the
            # chain reads as empty (breaks virtual→virtual cycles at the call level).
            if mailbox in _chain:
                return {"messages": [], "user": sender or "", "peer": mailbox}
            child_chain = _chain | {mailbox}
            spec = self._virtual[mailbox]
            source = str(spec.get("source", ""))
            if source.startswith("merge:"):
                target = source[len("merge:"):].strip()
                if target in ("*", "all"):
                    # "all streams" = the real streams only (built-in + dynamic),
                    # never other virtuals, so * stays a flat fan-in.
                    subs = [s for s in STREAMS]
                    subs += [m for m, meta in self._dynamic_mailboxes.items() if not (isinstance(meta, dict) and meta.get("hidden"))]
                else:
                    subs = [n.strip() for n in target.split(",") if n.strip()]
                merged: list[dict[str, Any]] = []
                for sub in subs:
                    if sub == mailbox or sub in child_chain:
                        continue  # skip self and any stream already in the relay chain
                    # Read sources unbounded — limit applies to the produced stream only.
                    merged.extend(self.mailbox_messages(sub, limit=_MAX_SCAN, _chain=child_chain).get("messages", []))
                merged.sort(key=lambda m: str(m.get("timestamp") or ""))
                messages = merged
            else:
                records = self._resolve_virtual_records(mailbox)
                messages = [self._virtual_message(mailbox, record) for record in records]
            messages = self._apply_filter_rules(messages, spec.get("rules"), spec.get("policy"))
            # Stamp provenance and drop anything this stream already forwarded
            # (message-level loop prevention across diamond/merge topologies).
            relayed: list[dict[str, Any]] = []
            for m in messages:
                fwd = list(m.get("forwarded_by") or [])
                if mailbox in fwd:
                    continue
                relayed.append({**m, "forwarded_by": [*fwd, mailbox]})
            messages = relayed
            if do_filter and text:
                needle = text.lower()
                messages = [m for m in messages if needle in (m.get("text") or "").lower()]
            # Single-source virtuals (self/disk/http) cache their own candidates;
            # merge members are cached by their leaf reads.
            if not source.startswith("merge:"):
                self._remember_field_values(mailbox, messages)
                self._save_field_cache()
            # Limit the PRODUCED stream only, after filtering.
            if limit and limit > 0:
                messages = messages[-min(limit, _MAX_SCAN):]
            return {"messages": messages, "user": sender or "", "peer": mailbox}
        if not self._known_mailbox(mailbox):
            return {"messages": [], "user": sender or "", "peer": mailbox}
        events = self.store.tail(mailbox, max(1, min(limit, _MAX_SCAN)), _build_predicate(filters))
        messages = [self._event_to_message(event.to_dict()) for event in events]
        # Remember field values (from the full read, before the require-match filter)
        # so the pickers have candidates for this stream.
        self._remember_field_values(mailbox, messages)
        self._save_field_cache()
        if do_filter:
            needle = (text or "").lower()

            def keep(message: dict[str, Any]) -> bool:
                if sender and message.get("from") != sender:
                    return False
                if to and message.get("to") != to:
                    return False
                if send_to and (message.get("send_to") or message.get("mailboxId")) != send_to:
                    return False
                if needle and needle not in (message.get("text") or "").lower():
                    return False
                return True

            messages = [message for message in messages if keep(message)]
        return {"messages": messages, "user": sender or "", "peer": mailbox}

    def mailbox_send(
        self,
        *,
        to: str,
        text: str,
        sender: str,
        source_kind: str = "agent",
        send_to: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Post into a mailbox. The message is published to the topic named by
        the mailbox: an explicit SEND-TO mailbox wins, else the addressed name
        when it is itself a mailbox/topic, else the shared conversation. The
        sender is recorded as the event source; a distinct recipient (an agent
        rather than a topic) is kept in ``data['to']``."""
        if not isinstance(text, str) or not text.strip():
            raise ValidationError("message 'text' is required")
        # send_to / to are mailbox names (topics). Route to the first that names a
        # real mailbox; a non-mailbox value (e.g. an agent or the default peer)
        # simply falls through to the shared conversation.
        topic = next((name for name in (send_to, to) if name and self._writable_mailbox(name)), STREAM_CONVERSATION)
        data: dict[str, Any] = {"text": text}
        if to and to != topic:
            data["to"] = to
        published = self.publish(
            stream=topic,
            type=CONVERSATION_MESSAGE,
            data=data,
            source_id=sender,
            source_kind=source_kind,
            idempotency_key=idempotency_key,
        )
        message = self._event_to_message({
            "id": published.get("id"),
            "ts": published.get("server_time"),
            "source_id": sender,
            "source_kind": source_kind,
            "type": CONVERSATION_MESSAGE,
            "stream": topic,
            "data": data,
        })
        return {"message": {**message, "seq": published.get("seq"), "cursor": published.get("cursor")}}

    def mailbox_cursor(self, mailbox: str, agent: str) -> dict[str, Any]:
        """Cursor position of an agent on a mailbox. Streams are read
        non-consumingly here, so this reports size and a zeroed position."""
        stats = {row["stream"]: row for row in self.store.stats()}
        total = int((stats.get(mailbox) or {}).get("seq") or 0)
        return {
            "mailbox": mailbox,
            "agent": agent,
            "initialized": False,
            "offset": 0,
            "size": total,
            "behind": total,
            "entries_consumed": 0,
            "entry_next": None,
            "entries_total": total,
        }

    def mailbox_record(self, record_id: str, record: dict[str, Any], mode: str = "at-end") -> dict[str, Any]:
        """Persist an edited record. Streams are append-only, so both "in-place"
        and "at-end" append a corrected event (tagged with the id it edits)
        rather than mutating the durable log."""
        if not record_id or not isinstance(record, dict):
            raise ValidationError("id and record object are required")
        raw = record.get("raw") if isinstance(record.get("raw"), dict) else {}
        inner = record.get("data") if isinstance(record.get("data"), dict) else None
        if inner is None and isinstance(raw.get("data"), dict):
            inner = raw["data"]
        if inner is None:
            inner = {"text": record.get("text") or ""}
        stream = record.get("stream") or record.get("mailboxId") or raw.get("stream") or "conversation"
        if not self._writable_mailbox(stream):
            raise ValidationError(f"mailbox {stream!r} is not writable", details={"mailbox": stream})
        event_type = record.get("type") or raw.get("type") or CONVERSATION_MESSAGE
        source_id = record.get("source_id") or record.get("author") or record.get("from") or raw.get("source_id") or "operator"
        source_kind = record.get("source_kind") or raw.get("source_kind") or "operator"
        payload = {**inner, "edited_from": record_id, "edit_mode": mode}
        published = self.publish(
            stream=stream,
            type=event_type,
            data=payload,
            source_id=source_id,
            source_kind=source_kind,
        )
        message = self._event_to_message({
            "id": published.get("id"),
            "ts": published.get("server_time"),
            "source_id": source_id,
            "source_kind": source_kind,
            "type": event_type,
            "stream": stream,
            "data": payload,
        })
        return {"entryKey": published.get("id"), "mailbox": stream, "record": message}

    # ------------------------------------------------------------------ workers
    def register_worker(self, worker_id: str, task: str = "", meta: dict[str, Any] | None = None) -> dict[str, Any]:
        if not worker_id:
            raise ValidationError("worker_id is required")
        return self.workers.register(worker_id, task, meta)

    def worker_status(self, worker_id: str, status: str, data: dict[str, Any] | None = None, errors: list[str] | None = None) -> dict[str, Any]:
        if not worker_id:
            raise ValidationError("worker_id is required")
        return self.workers.record_status(worker_id, status, data, errors=errors)

    def list_workers(self) -> dict[str, Any]:
        return {"workers": self.workers.list_workers(), "server_time": utc_now_iso()}

    def run_monitor_cycle(self) -> dict[str, Any]:
        alerts = self.workers.evaluate()
        return {"alerts": alerts, "workers": self.workers.list_workers()}

    def _announce(self, worker_id: str, message: str) -> None:
        # Route unresponsive-worker announcements through the TTS queue at high priority.
        try:
            profile = self.voices.get_profile("system") or self.voices.get_profile(worker_id)
            voice = profile.voice_id if profile else "fake:aria"
            self.tts.speak("system", message, voice_id=voice, priority=1, dedupe=True)
        except Exception:
            pass

    # -------------------------------------------------------------- speech in
    async def process_segment(self, segment: AudioSegment) -> dict[str, Any]:
        """Run one segment through STT -> disambiguation -> classification."""

        async def on_partial(correlation_id: str, hyp: Hypothesis) -> None:
            self.publish(
                stream=STREAM_STT_TRANSCRIPTS,
                type=STT_PARTIAL_RESULT,
                data={"segment_id": segment.id, **hyp.public()},
                source_id=hyp.engine,
                source_kind="system",
                correlation_id=correlation_id,
            )

        hypotheses = await run_stt(
            self.stt_engines,
            segment,
            timeout_ms=self.config.stt_timeout_ms,
            concurrency=self.config.stt_concurrency,
            on_partial=on_partial,
        )
        for hyp in hypotheses:
            self.publish(
                stream=STREAM_STT_TRANSCRIPTS,
                type=STT_ENGINE_ERROR if hyp.error else STT_FINAL_RESULT,
                data={"segment_id": segment.id, **hyp.public()},
                source_id=hyp.engine,
                source_kind="system",
                correlation_id=segment.correlation_id,
            )

        return self._finalize(segment, hypotheses)

    def _finalize(self, segment: AudioSegment, hypotheses: list[Hypothesis]) -> dict[str, Any]:
        """Resolve hypotheses, classify the source, handle echo, emit HEARD_SPEECH.

        Shared by the live capture pipeline and the external-recognizer ingest
        bridge so both produce identical resolved transcripts and timeline events.
        """

        resolved = self.disambiguator.resolve(hypotheses, context={"language": "en"})
        self.publish(
            stream=STREAM_STT_TRANSCRIPTS,
            type=TRANSCRIPT_RESOLVED,
            data={"segment_id": segment.id, **resolved.public()},
            source_id="disambiguator",
            source_kind="system",
            correlation_id=segment.correlation_id,
        )

        classification = self.classifier.classify(
            segment, resolved.resolved_text, active_tts=self.tts.active_expected_texts()
        )

        # TTS echo handling + accuracy measurement.
        expected = segment.expected_tts_text or classification.expected_text
        if classification.is_echo:
            self.publish(
                stream=STREAM_TRANSLATED_AUDIO,
                type=TTS_AUDIO_DETECTED_BY_MICROPHONE,
                data={"segment_id": segment.id, "classification": classification.public()},
                source_id="capture",
                source_kind="system",
                correlation_id=segment.correlation_id,
            )
            if expected:
                report = accuracy_metrics.evaluate_pipeline(
                    expected,
                    {h.engine: h.raw_text for h in hypotheses if not h.error},
                    resolved.resolved_text,
                )
                self.accuracy.add("final", report["final"], example={"expected": expected, "got": resolved.resolved_text})
                for engine, metrics in report["per_engine"].items():
                    self.accuracy.add(engine, metrics)
                self.publish(
                    stream=STREAM_TTS,
                    type=TTS_TRANSCRIPTION_EVALUATED,
                    data={"segment_id": segment.id, "tts_event_id": classification.matched_tts_event_id, **report},
                    source_id="accuracy",
                    source_kind="system",
                    correlation_id=segment.correlation_id,
                )
            self.publish(
                stream=STREAM_TRANSLATED_AUDIO,
                type=TRANSCRIPT_FILTERED,
                data={"segment_id": segment.id, "reason": "classified as TTS/agent echo", "classification": classification.public()},
                source_id="classifier",
                source_kind="system",
                correlation_id=segment.correlation_id,
            )

        heard = self.publish(
            stream=STREAM_TRANSLATED_AUDIO,
            type=HEARD_SPEECH,
            data={
                "segment": segment.public(),
                "resolved": resolved.public(),
                "classification": classification.public(),
            },
            source_id=segment.source_kind,
            source_kind=segment.source_kind if segment.source_kind in {"operator", "agent", "system"} else "unknown",
            correlation_id=segment.correlation_id,
        )
        return {
            "correlation_id": segment.correlation_id,
            "segment_id": segment.id,
            "heard_event_id": heard["id"],
            "resolved": resolved.public(),
            "classification": classification.public(),
            "hypotheses": [h.public() for h in hypotheses],
        }

    def ingest_transcript(
        self,
        *,
        engine: str,
        text: str,
        correlation_id: str | None = None,
        confidence: float = 0.9,
        is_final: bool = True,
        language: str = "en",
        source_kind: str = "operator",
        expected_tts_text: str | None = None,
        resolve: bool = True,
        device_id: str = "external",
        audio_meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Ingest an already-recognised transcript from an external recognizer.

        This is the bridge for pushing speech events from another application
        (for example the Copilot app's voice dictation / Nemotron recognizer) into
        WS_COLLAB. The external engine is recorded as an STT hypothesis and, when
        final, flows through the same disambiguation, classification, and timeline
        pipeline a local engine would.
        """

        from .ids import new_event_id
        from .stt.base import normalize_text

        if not engine:
            raise ValidationError("engine is required for an external transcript")
        if not isinstance(text, str) or not text.strip():
            raise ValidationError("text is required for an external transcript")
        correlation_id = correlation_id or new_event_id()
        hypothesis = Hypothesis(
            engine=engine,
            model=f"external:{engine}",
            raw_text=text,
            normalized_text=normalize_text(text),
            confidence=max(0.0, min(1.0, float(confidence))),
            language=language,
            is_final=is_final,
        )
        self.publish(
            stream=STREAM_STT_TRANSCRIPTS,
            type=STT_FINAL_RESULT if is_final else STT_PARTIAL_RESULT,
            data={"external": True, **hypothesis.public(), **(audio_meta or {})},
            source_id=engine,
            source_kind="system",
            correlation_id=correlation_id,
        )
        if not (is_final and resolve):
            return {"correlation_id": correlation_id, "recorded": True, "resolved": None}
        segment = AudioSegment(
            correlation_id=correlation_id,
            reference_text=text,
            source_kind=source_kind,
            device_id=device_id,
            expected_tts_text=expected_tts_text,
        )
        return self._finalize(segment, [hypothesis])

    # -------------------------------------------------------------- speech out
    def speak(
        self,
        agent_id: str,
        text: str,
        *,
        priority: int | None = None,
        interrupt: bool = False,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        profile = self.voices.get_profile(agent_id)
        if profile and not profile.speaking_permission:
            raise ConflictError(f"agent {agent_id!r} does not have speaking permission")
        if profile and len(text) > profile.max_utterance_chars:
            raise ValidationError("utterance exceeds the agent's max_utterance_chars")
        resolution = self.voices.resolve_for_speak(agent_id)
        engine_voice, params = self.voices.effective_voice(resolution["voice_id"])
        effective_priority = priority if priority is not None else (profile.queue_priority if profile else 5)
        base_rate = profile.rate if profile else 1.0
        base_pitch = profile.pitch if profile else 0.0
        base_volume = profile.volume if profile else 1.0
        result = self.tts.speak(
            agent_id,
            text,
            voice_id=engine_voice,
            requested_voice_id=resolution["requested_voice_id"],
            rate=base_rate * params.get("rate", 1.0),
            pitch=base_pitch + params.get("pitch", 0.0),
            volume=base_volume * params.get("volume", 1.0),
            device=profile.output_device if profile else "default",
            priority=effective_priority,
            correlation_id=correlation_id,
            interrupt=interrupt,
        )
        result["voice_resolution"] = resolution
        return result

    async def measure_tts_accuracy(self, agent_id: str, text: str) -> dict[str, Any]:
        """Speak text, then feed it back as a loopback echo to measure accuracy."""

        spoken = self.speak(agent_id, text)
        correlation_id = spoken.get("id")
        if self.capture.listening:
            await self.capture.inject_utterance(
                text,
                source_kind="system",
                correlation_id=correlation_id,
                is_loopback=True,
                expected_tts_text=text,
                tts_event_id=spoken.get("id"),
            )
        return {"spoken": spoken, "accuracy_summary": self.accuracy.summary()}

    def accuracy_summary(self) -> dict[str, Any]:
        return {"groups": self.accuracy.summary(), "server_time": utc_now_iso()}

    # --------------------------------------------------------------- devices
    def list_devices(self) -> dict[str, Any]:
        return {"devices": self.devices.list(), "generation": self.devices.generation}

    def refresh_devices(self) -> dict[str, Any]:
        self.devices.refresh()
        return self.list_devices()

    def list_voices(self) -> dict[str, Any]:
        return {
            "voices": self.voices.list_voices(),
            "profiles": self.voices.list_profiles(),
            "clones": self.voices.list_clones(),
        }

    def clone_voice(self, base_voice_id: str, name: str, *, rate: float = 1.0, pitch: float = 0.0,
                    volume: float = 1.0, style: str = "", operator: str = "operator") -> dict[str, Any]:
        return self.voices.clone_voice(
            base_voice_id, name, rate=rate, pitch=pitch, volume=volume, style=style, operator=operator
        )

    def delete_clone(self, clone_id: str, operator: str = "operator") -> dict[str, Any]:
        return {"deleted": self.voices.delete_clone(clone_id, operator=operator), "clone_id": clone_id}

    def convert_representation(self, value: Any = None, to: str = "metta",
                              items: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Render a value (or a batch of values) as MeTTa or pretty JSON.

        Server-side port of the workbench's markdown/MeTTa codec so clients can
        display JSON/MeTTa views without duplicating the codec.
        """

        from . import metta_codec

        if items is not None:
            return {
                "to": to,
                "results": [
                    {"id": item.get("id"), "text": metta_codec.convert_value(item.get("value"), to)}
                    for item in items
                ],
            }
        return {"to": to, "text": metta_codec.convert_value(value, to)}

    def set_voice_profile(self, agent_id: str, updates: dict[str, Any], operator: str = "operator") -> dict[str, Any]:
        return self.voices.set_profile(agent_id, updates, operator=operator).public()

    def assign_voices(self, policy: str | None = None) -> dict[str, Any]:
        agents = [{"agent_id": a} for a in self.config.agents] or [{"agent_id": p["agent_id"]} for p in self.voices.list_profiles()]
        return self.voices.auto_assign(agents, policy)

    # --------------------------------------------------------------- routing
    def routing_matrix(self) -> dict[str, Any]:
        return {"routes": self.routing.matrix()}

    def stt_engine_routes(self) -> dict[str, Any]:
        """One row per configured STT engine with the device it listens on.

        Engines without an explicit route fall back to the active capture device,
        which is reported so the UI can show what is actually in use rather than
        an empty cell.
        """

        capture = self.capture.state()
        rows = []
        for engine in self.stt_engines:
            route = self.routing.get(DEFAULT_ROUTE_SOURCE, engine.name)
            device_id = route.device_id if route else ""
            device = self.devices.get(device_id) if device_id else None
            rows.append({
                "engine": engine.name,
                "model": getattr(engine, "model", ""),
                "is_remote": getattr(engine, "is_remote", False),
                "device_id": device_id,
                "device_name": device.name if device else None,
                "explicit": route is not None,
                "effective_device_id": device_id or capture.get("device_id"),
                "effective_device_name": (device.name if device else capture.get("device_name")),
            })
        return {"engines": rows, "source": DEFAULT_ROUTE_SOURCE}

    def set_engine_device(self, engine: str, device_id: str, *, operator: str = "operator") -> dict[str, Any]:
        """Point one STT engine at a specific input device."""

        known = {e.name for e in self.stt_engines}
        if engine not in known:
            raise ValidationError(f"unknown STT engine: {engine!r}", details={"engines": sorted(known)})
        if not device_id:
            self.routing.delete_route(DEFAULT_ROUTE_SOURCE, engine, operator=operator)
            self.sound_settings.set_engine_device(engine, None)
            return {"engine": engine, "device_id": None, "cleared": True}
        device = self.devices.get(device_id)
        if device is None:
            raise NotFoundError(f"unknown device: {device_id}")
        if device.direction not in ("input", "loopback", "virtual"):
            raise ValidationError(
                "an STT engine must listen on a capture-capable device",
                details={"device": device.name, "direction": device.direction},
            )
        route = self.routing.set_route(
            DEFAULT_ROUTE_SOURCE, engine, device_id, operator=operator,
            # Loopback captures the machine's own output, so it is diagnostic and
            # TTS-accuracy material -- never a source of operator commands.
            command_eligible=device.direction != "loopback",
            tts_accuracy_eligible=device.direction == "loopback",
        )
        self.sound_settings.set_engine_device(engine, device_id)
        return route.public()

    # ------------------------------------------------------- audio defaults
    @property
    def _audio_defaults_path(self) -> Path:
        return Path(self.config.state_dir) / "audio_defaults.json"

    def _legacy_agent_output_device(self) -> str:
        """Read the agent output device from the pre-consolidation legacy file."""

        import json

        path = self._audio_defaults_path
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                return data.get("agent_output_device") or ""
            except (OSError, ValueError):
                return ""
        return ""

    def get_audio_defaults(self) -> dict[str, Any]:
        device_id = self.sound_settings.get("agent_output_device") or ""
        if not device_id:
            # One-time migration from the legacy audio_defaults.json file.
            migrated = self._legacy_agent_output_device()
            if migrated:
                device_id = migrated
                self.sound_settings.set("agent_output_device", migrated)
        device = self.devices.get(device_id) if device_id else None
        if device_id and device is None:
            # Persisted device is gone (unplugged, host API changed).
            fallback = self.devices.default_output()
            return {
                "agent_output_device": device_id,
                "agent_output_device_name": None,
                "available": False,
                "note": "the saved output device is not currently present",
                "effective_device_id": fallback.id if fallback else None,
                "effective_device_name": fallback.name if fallback else None,
            }
        fallback = self.devices.default_output()
        return {
            "agent_output_device": device_id or None,
            "agent_output_device_name": device.name if device else None,
            "available": True,
            "effective_device_id": device_id or (fallback.id if fallback else None),
            "effective_device_name": (device.name if device else (fallback.name if fallback else None)),
        }

    def set_default_output_device(self, device_id: str, *, operator: str = "operator") -> dict[str, Any]:
        """Set the output device agents speak through by default."""

        if device_id:
            device = self.devices.get(device_id)
            if device is None:
                raise NotFoundError(f"unknown device: {device_id}")
            if device.direction not in ("output", "virtual"):
                raise ValidationError(
                    "agents must speak through a playback-capable device",
                    details={"device": device.name, "direction": device.direction},
                )
        self.sound_settings.set("agent_output_device", device_id or None)

        # Agents that never chose their own output device resolve the default at
        # speak time, so this change takes effect immediately with no per-profile
        # writes to make here.
        self._audit_sink({
            "type": "AUDIO_DEFAULT_CHANGED", "action": "set_agent_output_device",
            "device_id": device_id or None, "operator": operator,
        })
        return self.get_audio_defaults()

    def preview_voice(self, voice_id: str, *, text: str | None = None, rate: float = 1.0,
                      pitch: float = 0.0, volume: float = 1.0) -> dict[str, Any]:
        """Speak a short sample with a specific voice (and optional rate/pitch).

        Independent of any agent profile, so the admin UI can audition any voice
        -- including cloned presets -- before assigning it.
        """

        voice = self.voices.get_voice(voice_id)
        if voice is None:
            raise NotFoundError(f"unknown voice: {voice_id}")
        engine_voice, params = self.voices.effective_voice(voice_id)
        sample = text or f"This is {voice.name}, a {voice.language} voice."
        return self.tts.speak(
            "preview",
            sample,
            voice_id=engine_voice,
            requested_voice_id=voice_id,
            rate=float(rate) * params.get("rate", 1.0),
            pitch=float(pitch) + params.get("pitch", 0.0),
            volume=float(volume) * params.get("volume", 1.0),
            priority=1,
            dedupe=False,
        )

    def _play_test_tone(self, device: Any) -> bool:
        """Best-effort: play a short sine tone on a real output device."""

        if getattr(device, "backend", "") != "sounddevice" or device.backend_index is None:
            return False
        try:  # pragma: no cover - depends on real audio hardware
            import numpy as np
            import sounddevice as sd

            sample_rate = 44100
            duration = 0.6
            t = np.linspace(0, duration, int(sample_rate * duration), endpoint=False)
            tone = (0.2 * np.sin(2 * np.pi * 440.0 * t)).astype("float32")
            sd.play(tone, samplerate=sample_rate, device=device.backend_index)
            return True
        except Exception:
            return False

    def test_output_device(self, device_id: str, *, text: str | None = None) -> dict[str, Any]:
        """Play a test sound on a specific output device.

        Prefers a real tone on the exact device; falls back to a spoken test
        phrase through the TTS engine when a direct tone is not possible.
        """

        device = self.devices.get(device_id)
        if device is None:
            raise NotFoundError(f"unknown device: {device_id}")
        if device.direction not in ("output", "virtual"):
            raise ValidationError(
                "a test sound needs a playback-capable device",
                details={"device": device.name, "direction": device.direction},
            )
        if self._play_test_tone(device):
            self._audit_sink({"type": "AUDIO_TEST", "action": "test_output_device",
                              "device_id": device_id, "method": "tone"})
            return {"device_id": device_id, "device_name": device.name, "method": "tone"}
        voices = self.voices.list_voices()
        voice_id = voices[0]["id"] if voices else "fake:aria"
        result = self.tts.speak(
            "device-test", text or f"Test sound for {device.name}.",
            voice_id=voice_id, device=device_id, priority=1, dedupe=False,
        )
        return {"device_id": device_id, "device_name": device.name, "method": "tts", "tts": result}

    def set_route(self, source: str, engine: str, device_id: str, operator: str = "operator", **params: Any) -> dict[str, Any]:
        return self.routing.set_route(source, engine, device_id, operator=operator, **params).public()

    # ---------------------------------------------------------------- capture
    def start_capture(self, device_id: str | None = None) -> dict[str, Any]:
        state = self.capture.start(device_id=device_id)
        # Persist the active input device so it survives a restart.
        self.sound_settings.set("capture_device", state.get("device_id") or None)
        return state

    def stop_capture(self) -> dict[str, Any]:
        return self.capture.stop()

    def capture_state(self) -> dict[str, Any]:
        return self.capture.state()

    # ----------------------------------------------------------------- cursors
    def cursor_get(self, stream: str, consumer: str) -> dict[str, Any]:
        if stream not in STREAMS:
            raise ValidationError(f"unknown stream: {stream!r}")
        position = self.cursors.get(stream, consumer)
        if position is None:
            return {"stream": stream, "consumer": consumer, "token": self.store.stream(stream).cursor_at_start(), "seq": 0}
        return position.public()

    def cursor_list(self) -> dict[str, Any]:
        return {"cursors": self.cursors.list()}

    def cursor_commit(self, stream: str, consumer: str, token: str, reason: str = "processed") -> dict[str, Any]:
        return self.cursors.commit(stream, consumer, token, reason=reason).public()

    def cursor_reposition(
        self,
        stream: str,
        consumer: str,
        *,
        token: str | None = None,
        seq: int | None = None,
        reason: str,
        operator: str,
        allow_replay: bool = False,
        allow_skip: bool = False,
    ) -> dict[str, Any]:
        if token is None:
            if seq is None:
                raise ValidationError("either token or seq is required")
            token = self.store.stream(stream).cursor_at_seq(int(seq))
        return self.cursors.reposition(
            stream, consumer, token, reason=reason, operator=operator, allow_replay=allow_replay, allow_skip=allow_skip
        ).public()

    def cursor_reset(self, stream: str, consumer: str, *, to: str = "start", reason: str = "", operator: str = "operator") -> dict[str, Any]:
        state = self.store.stream(stream)
        token = state.cursor_at_end() if to == "end" else state.cursor_at_start()
        return self.cursors.reset(stream, consumer, token, reason=reason or f"reset to {to}", operator=operator).public()

    def cursor_history(self, stream: str, consumer: str) -> dict[str, Any]:
        return {"history": self.cursors.history(stream, consumer)}

    # ------------------------------------------------------------------ prompt
    def _prompt_history_events(self) -> list[dict[str, Any]]:
        events = self.store.tail(STREAM_PROMPT_STREAM, 500)
        return [event.to_dict() for event in events]

    def prompt_get(self) -> dict[str, Any]:
        return self.prompt.current()

    def prompt_save(self, text: str, operator: str = "operator", note: str = "") -> dict[str, Any]:
        return self.prompt.save(text, operator=operator, note=note)

    def prompt_history(self) -> dict[str, Any]:
        return {"history": self.prompt.history()}

    def prompt_preview_diff(self, text: str) -> dict[str, Any]:
        return {"diff": self.prompt.preview_diff(text)}

    def prompt_rollback(self, version: int, operator: str = "operator") -> dict[str, Any]:
        return self.prompt.rollback(version, operator=operator)

    # -------------------------------------------------------------------- misc
    def get_config_public(self) -> dict[str, Any]:
        cfg = self.config
        return {
            "host": cfg.host,
            "http_port": cfg.http_port,
            "https_port": cfg.https_port,
            "https_enabled": cfg.https_enabled,
            "state_dir": str(cfg.state_dir),
            "jsonl_dir": str(cfg.jsonl_dir),
            "admin_remote": cfg.admin_remote,
            "require_tls": cfg.require_tls,
            "rate_limit_rps": cfg.rate_limit_rps,
            "worker_thresholds": {
                "warn": cfg.worker_warn_seconds,
                "overdue": cfg.worker_overdue_seconds,
                "unresponsive": cfg.worker_unresponsive_seconds,
            },
            "audio_enabled": cfg.audio_enabled,
            "echo_policy": cfg.echo_policy,
            "stt_engines": cfg.stt_engines,
            "tts_policy": cfg.tts_policy,
            "agents": cfg.agents,
            "warnings": self._warnings,
        }

    # ------------------------------------------------------- docs / ui / files
    # Files whose contents are never served, regardless of role. The writable
    # state directory holds the generated administrator token and session data;
    # listing their existence is fine, exposing their bytes is not.
    SECRET_FILENAMES = {"generated_admin_token.txt", ".ws_collab.lock"}
    SECRET_DIRS = {"sessions"}

    @property
    def docs_dir(self) -> Path:
        return Path(__file__).resolve().parents[1] / "docs"

    def list_docs(self) -> dict[str, Any]:
        """Markdown documentation shipped with the server."""

        directory = self.docs_dir
        documents = []
        if directory.is_dir():
            for path in sorted(directory.glob("*.md")):
                documents.append({
                    "id": path.stem.lower(),
                    "name": path.name,
                    "title": _markdown_title(path),
                    "bytes": path.stat().st_size,
                    "path": f"/ws_collab/docs/{path.name}",
                })
        readme = directory.parent / "README.md"
        if readme.is_file():
            documents.insert(0, {
                "id": "readme", "name": "README.md", "title": _markdown_title(readme),
                "bytes": readme.stat().st_size, "path": "/ws_collab/docs/README.md",
            })
        return {"documents": documents, "count": len(documents)}

    def read_doc(self, name: str) -> str:
        from .security import safe_join

        if not name.endswith(".md"):
            raise ValidationError("only markdown documents are served")
        root = self.docs_dir
        candidate = root / name
        if name == "README.md":
            candidate = root.parent / "README.md"
            if not candidate.is_file():
                raise NotFoundError(name)
            return candidate.read_text(encoding="utf-8")
        path = safe_join(root, name)
        if not path.is_file():
            raise NotFoundError(name)
        return path.read_text(encoding="utf-8")

    def ui_links(self, *, origin: str = "") -> dict[str, Any]:
        """Deep links to every page of the operations workbench."""

        origin = origin.rstrip("/")
        base = f"{origin}/ws_collab/admin/" if origin else "/ws_collab/admin/"
        pages = [
            ("transcript", "Unified Transcript", "Full speech pipeline in chronological order"),
            ("conversation", "Conversation", "Worker, agent, and human messages"),
            ("streams", "JSONL Streams", "Any durable stream, rendered or raw"),
            ("workers", "Workers", "Registry, health, and check-ins"),
            ("alerts", "Alerts", "Raised and recovered alerts"),
            ("devices", "Devices & Routing", "Audio devices, capture, routing matrix"),
            ("voices", "Agent Voices", "Voice catalog, profiles, and policies"),
            ("accuracy", "TTS Accuracy", "Rolling WER/CER by engine"),
            ("cursors", "Cursors", "Inspect and reposition durable cursors"),
            ("prompt", "Prompt", "Edit, diff, and roll back the worker prompt"),
            ("system", "System & Audit", "Health, configuration, and audit history"),
        ]
        return {
            "base": base,
            "links": [
                {"id": page, "title": title, "description": description, "url": f"{base}#{page}"}
                for page, title, description in pages
            ],
        }

    def list_files(self, subpath: str = "") -> dict[str, Any]:
        """List the writable state directory (read-only, traversal-safe)."""

        from .security import safe_join

        root = Path(self.config.state_dir).resolve()
        directory = safe_join(root, subpath) if subpath else root
        if not directory.is_dir():
            raise NotFoundError(f"not a directory: {subpath or '.'}")

        entries = []
        for path in sorted(directory.iterdir(), key=lambda p: (p.is_file(), p.name)):
            relative = path.relative_to(root).as_posix()
            secret = path.name in self.SECRET_FILENAMES or path.name in self.SECRET_DIRS
            stat = path.stat()
            entries.append({
                "name": path.name,
                "path": relative,
                "kind": "dir" if path.is_dir() else "file",
                "bytes": None if path.is_dir() else stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
                    .isoformat(timespec="seconds").replace("+00:00", "Z"),
                # Secrets are listed so operators know they exist, but never read.
                "readable": not secret and path.is_file(),
                "protected": secret,
            })
        return {"root": str(root), "path": subpath, "entries": entries}

    def read_file(self, subpath: str, *, max_bytes: int = 256 * 1024) -> dict[str, Any]:
        """Read a bounded tail of a file inside the writable state directory."""

        from .security import safe_join

        root = Path(self.config.state_dir).resolve()
        path = safe_join(root, subpath)
        if path.name in self.SECRET_FILENAMES or any(
            part in self.SECRET_DIRS for part in path.relative_to(root).parts[:-1]
        ):
            raise AuthorizationError(
                "this file holds credentials and is never served",
                details={"path": subpath},
            )
        if not path.is_file():
            raise NotFoundError(subpath)
        size = path.stat().st_size
        with open(path, "rb") as handle:
            if size > max_bytes:
                handle.seek(size - max_bytes)
            raw = handle.read(max_bytes)
        return {
            "path": subpath,
            "bytes": size,
            "truncated": size > max_bytes,
            "content": raw.decode("utf-8", errors="replace"),
        }

    def diagnostics(self) -> dict[str, Any]:
        return {
            "server_time": utc_now_iso(),
            "boot_id": self.boot_id,
            "uptime_seconds": round(time.time() - self.started_at, 1),
            "streams": self.store.stats(),
            "broker": self.broker.stats(),
            "workers": len(self.workers.list_workers()),
            "capture": self.capture.state(),
            "tts": self.tts.state(),
            "devices": self.devices.generation,
            "warnings": self._warnings,
        }

    # ------------------------------------------------------- status/readiness
    def status(self) -> dict[str, Any]:
        """Subsystem rollup with an overall verdict.

        ``/health`` answers "is the process alive"; this answers "is each part
        actually working". Deliberately coarse and free of secrets so it is safe
        to expose to a load balancer or status page.
        """

        subsystems: dict[str, dict[str, Any]] = {}

        # Durable store: the one hard dependency. If it cannot be read, we are down.
        try:
            stats = self.store.stats()
            subsystems["event_store"] = {
                "state": "ok",
                "streams": len(stats),
                "events": sum(entry.get("seq", 0) for entry in stats),
            }
        except Exception as error:  # noqa: BLE001
            subsystems["event_store"] = {"state": "down", "detail": str(error)}

        # Speech input: degraded when enabled but not actually capturing.
        capture = self.capture.state()
        if not self.config.audio_enabled:
            capture_state = "disabled"
        elif not capture["listening"]:
            capture_state = "idle"
        elif capture.get("error"):
            capture_state = "degraded"
        else:
            capture_state = "ok"
        subsystems["capture"] = {
            "state": capture_state,
            "backend": capture["backend"],
            "live": capture.get("live_capture", False),
            "device": capture.get("device_name"),
            "detail": capture.get("error"),
        }

        # Recognition: degraded when any engine fell back to a double.
        degraded_engines = [w for w in self._warnings if "STT engine" in w]
        subsystems["stt"] = {
            "state": "degraded" if degraded_engines else "ok",
            "engines": [engine.name for engine in self.stt_engines],
            "detail": degraded_engines or None,
        }

        subsystems["tts"] = {
            "state": "ok",
            "backend": self.tts.state()["backend"],
            "voices": len(self.voices.list_voices()),
            "queued": len(self.tts.state()["queue"]),
        }

        workers = self.workers.list_workers()
        unhealthy = [w for w in workers if w["state"] in ("overdue", "unresponsive")]
        subsystems["workers"] = {
            "state": "degraded" if unhealthy and len(unhealthy) == len(workers) else "ok",
            "total": len(workers),
            "unhealthy": len(unhealthy),
        }

        subsystems["transports"] = {
            "state": "ok",
            "websocket_subscribers": self.broker.stats()["subscriptions"],
            "tls": self.config.https_enabled,
        }

        # Overall verdict: anything down wins, then any degradation.
        states = [entry["state"] for entry in subsystems.values()]
        if "down" in states:
            overall = "down"
        elif "degraded" in states:
            overall = "degraded"
        else:
            overall = "ok"

        return {
            "status": overall,
            "version": __version__,
            "boot_id": self.boot_id,
            "server_time": utc_now_iso(),
            "uptime_seconds": round(time.time() - self.started_at, 1),
            "subsystems": subsystems,
            "warnings": self._warnings,
        }

    def readiness(self) -> tuple[bool, dict[str, Any]]:
        """Return ``(ready, body)``. Not ready => the caller should answer 503."""

        report = self.status()
        ready = report["status"] != "down"
        return ready, {
            "ready": ready,
            "status": report["status"],
            "server_time": report["server_time"],
        }

    def endpoints(self, *, origin: str = "") -> dict[str, Any]:
        """Machine-readable map of every mounted endpoint.

        Clients (including the admin page's connection inspector) resolve URLs
        from here instead of hard-coding them, so a changed prefix or scheme
        cannot leave a client pointing at the wrong place.
        """

        base = "/ws_collab"
        origin = origin.rstrip("/")
        ws_origin = ""
        if origin:
            ws_origin = ("wss://" if origin.startswith("https://") else "ws://") + origin.split("://", 1)[1]

        def http_url(path: str) -> str:
            return f"{origin}{path}" if origin else path

        def ws_url(path: str) -> str:
            return f"{ws_origin}{path}" if ws_origin else path

        # Every route answers under each of these roots.
        mounts = ["", "/v1", "/ws_collab", "/ws_collab/v1"]

        def entry(id_: str, path: str, auth: str, description: str, kind: str = "http") -> dict[str, Any]:
            full = f"{base}{path}"
            return {
                "id": id_,
                "kind": kind,
                "path": full,
                "url": ws_url(full) if kind == "ws" else http_url(full),
                "auth": auth,
                "description": description,
                "aliases": [
                    (ws_url(f"{m}{path}") if kind == "ws" else http_url(f"{m}{path}"))
                    for m in mounts
                ],
            }

        return {
            "origin": origin or None,
            "base": base,
            "mounts": mounts,
            "tls": self.config.https_enabled,
            "endpoints": [
                entry("health", "/health", "public", "Liveness: is the process running"),
                entry("status", "/status", "public", "Subsystem rollup with an overall verdict"),
                entry("ready", "/ready", "public", "Readiness probe; 503 when not serving"),
                entry("capabilities", "/capabilities", "public", "Streams, roles, and feature flags"),
                entry("endpoints", "/endpoints", "token", "This endpoint map"),
                entry("events", "/events", "token", "Cursor-paginated durable events"),
                entry("websocket", "/ws", "token", "WebSocket transport (full REST parity)", kind="ws"),
                entry("docs", "/docs", "token", "Markdown documentation shipped with the server"),
                entry("ui_links", "/ui/links", "token", "Deep links to every workbench page"),
                entry("files", "/files", "token", "Writable state directory (read-only; secrets withheld)"),
                entry("workers", "/workers", "token", "Worker registry and health"),
                entry("cursors", "/cursors", "token", "Durable consumer cursors"),
                entry("voices", "/voices", "token", "Voice catalog and agent profiles"),
                entry("devices", "/audio/devices", "token", "Enumerated audio devices"),
                entry("diagnostics", "/diagnostics", "token", "Detailed runtime diagnostics"),
                entry("audit", "/audit", "token", "Security audit history"),
                entry("admin", "/admin/", "token", "Operations workbench"),
                {"id": "openapi", "kind": "http", "path": "/openapi/docs",
                 "url": http_url("/openapi/docs"), "auth": "public",
                 "description": "Interactive OpenAPI documentation", "aliases": []},
            ],
        }

    def read_audit(self, after: str | None = None, limit: int = 100) -> dict[str, Any]:
        return self.read_events(STREAM_AUDIT, after=after, limit=limit)

    # --------------------------------------------------------------- lifecycle
    async def startup(self) -> None:
        # Let capture threads dispatch finished utterances onto the server loop.
        self.capture.bind_loop(asyncio.get_running_loop())
        await self.tts.start()
        self._seed_prompt()
        self._seed_voices()
        self._seed_workers()
        self._monitor_task = asyncio.create_task(self._monitor_loop())

    async def shutdown(self) -> None:
        if self._monitor_task is not None:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        await self.tts.stop()

    async def _monitor_loop(self) -> None:
        # An internal bounded server loop (allowed) -- NOT a worker keep-alive.
        interval = max(5, self.config.worker_warn_seconds // 2)
        while True:
            await asyncio.sleep(interval)
            try:
                self.workers.evaluate()
            except Exception:
                pass

    def _seed_prompt(self) -> None:
        if not self.prompt.current()["exists"]:
            self.prompt.save(DEFAULT_WORKER_PROMPT, operator="system", note="initial default prompt")

    def _seed_voices(self) -> None:
        if self.config.agents and not self.voices.list_profiles():
            try:
                self.assign_voices()
            except Exception:
                pass

    def _seed_workers(self) -> None:
        """Rehydrate the worker registry from durable status events after a restart,
        so previously-registered workers still show up (and stay assignable) instead
        of vanishing until they happen to check in again."""

        try:
            events = self.store.tail(STREAM_STATUSES, 1000)
            self.workers.rebuild_from_events([event.to_dict() for event in events])
        except Exception:
            pass


# The prompt history stream constant (imported lazily to avoid a cycle above).
from .events import STREAM_PROMPT as STREAM_PROMPT_STREAM  # noqa: E402


DEFAULT_WORKER_PROMPT = """\
WS_COLLAB worker monitoring addendum
====================================

Your existing assigned tasks remain your PRIMARY responsibility. WS_COLLAB
monitoring is an ADDITIONAL, lightweight duty layered on top of that work.

Recurring activation:
- Native Codex/Copilot automation is the ONLY approved recurring worker launcher.
- Do NOT use OS schedulers, external launchers, watchdogs, self-revival scripts,
  or scripted keep-alive/polling loops to keep yourself running.
- Each activation performs exactly ONE bounded monitoring cycle, then returns to
  your primary work.

Each monitoring cycle:
1. Read new audio, conversation, and worker-status events from your durable
   cursors (bounded page sizes only; never re-read whole files).
2. Advance each cursor only after you have successfully processed its events.
   Bounded same-activation retries and rereads are allowed; rewind only for
   deliberate replay/verification.
3. Check overall team health. If workers appear overdue/unresponsive, publish a
   status/alert; remember that you may be the last responsive observer.
4. Return to your primary task.

Honesty:
- If a requested timing/behaviour is not supported by native automation, report
  that limitation plainly instead of simulating it with a polling loop.
"""
