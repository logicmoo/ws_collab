"""Canonical event model and stream registry for WS_COLLAB.

Every durable record is an :class:`Event`. Events are transport-independent: the
exact same object is produced by a REST ``POST`` and by a WebSocket ``publish``
frame, is written once to a JSONL stream, and is replayed identically to both
transports. Unknown top-level fields encountered while reading older records are
preserved (never silently dropped) to keep forward/backward compatibility.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .errors import ValidationError
from .ids import new_event_id

SCHEMA_VERSION = 1

# Canonical durable streams. Each maps to a ``<stream>.jsonl`` file. The speech
# pipeline stream is ``translated_audio`` (listening/VAD/heard-speech and the
# final resolved/translated transcript live here).
#
# Stream names use category prefixes so their domain is obvious on disk:
#   conversation | worker_* | stt_* / translated_audio | tts_* (queue) | audio_* | system_* | prompt_*
STREAM_CONVERSATION = "conversation"
STREAM_STATUSES = "worker_statuses"
STREAM_TRANSLATED_AUDIO = "translated_audio"
STREAM_STT_TRANSCRIPTS = "stt_transcripts"
STREAM_TTS = "tts_queue"
STREAM_ROUTING = "audio_routing"
STREAM_DEVICES = "audio_devices"
STREAM_ALERTS = "system_alerts"
STREAM_AUDIT = "system_audit"
STREAM_DIAGNOSTICS = "system_diagnostics"
STREAM_WS_EVENTS = "ws_event_log"
STREAM_PROMPT = "prompt"

STREAMS: dict[str, str] = {
    STREAM_CONVERSATION: "conversation.jsonl",
    STREAM_STATUSES: "worker_statuses.jsonl",
    STREAM_TRANSLATED_AUDIO: "translated_audio.jsonl",
    STREAM_STT_TRANSCRIPTS: "stt_transcripts.jsonl",
    STREAM_TTS: "tts_queue.jsonl",
    STREAM_ROUTING: "audio_routing.jsonl",
    STREAM_DEVICES: "audio_devices.jsonl",
    STREAM_ALERTS: "system_alerts.jsonl",
    STREAM_AUDIT: "system_audit.jsonl",
    STREAM_DIAGNOSTICS: "system_diagnostics.jsonl",
    STREAM_WS_EVENTS: "ws_event_log.jsonl",
    STREAM_PROMPT: "prompt_history.jsonl",
}

# Human-readable purpose of each mailbox/stream, surfaced in the mailbox
# directory so a consumer knows what a mailbox is for at a glance.
STREAM_PURPOSES: dict[str, str] = {
    STREAM_CONVERSATION: "Shared chat and coordination between humans and agents.",
    STREAM_STATUSES: "Worker registration and heartbeat/status updates.",
    STREAM_TRANSLATED_AUDIO: "Resolved / translated audio utterances.",
    STREAM_STT_TRANSCRIPTS: "Speech-to-text hypotheses and final transcripts (heard speech).",
    STREAM_TTS: "Text queued for and spoken by the shared text-to-speech.",
    STREAM_ROUTING: "Audio device routing decisions.",
    STREAM_DEVICES: "Enumerated audio input / output devices.",
    STREAM_ALERTS: "System alerts and warnings.",
    STREAM_AUDIT: "Administrative audit log.",
    STREAM_DIAGNOSTICS: "Diagnostics and internal telemetry.",
    STREAM_WS_EVENTS: "WebSocket protocol events and errors (e.g. unknown message types).",
    STREAM_PROMPT: "Prompt and version history.",
}

# Names of client-created ("dynamic") mailboxes the server has begun hosting.
# Populated at runtime from the durable mailbox registry; kept beside STREAMS so
# stream validation, publishing, and reads accept them just like built-ins.
DYNAMIC_STREAMS: set[str] = set()

# Semantic roles -> stream name(s). Clients, the admin UI, documentation, and
# tests resolve streams through these roles instead of literal names, so a stream
# can be renamed or split without breaking anything downstream. This registry is
# the single source of truth; it is published via /ws_collab/v1/capabilities.
STREAM_ROLES: dict[str, list[str] | str] = {
    "conversation": STREAM_CONVERSATION,
    "worker_status": STREAM_STATUSES,
    "speech_pipeline": [STREAM_TRANSLATED_AUDIO, STREAM_STT_TRANSCRIPTS, STREAM_TTS],
    "resolved_speech": STREAM_TRANSLATED_AUDIO,
    "stt_hypotheses": STREAM_STT_TRANSCRIPTS,
    "tts_queue": STREAM_TTS,
    "audio_config": [STREAM_DEVICES, STREAM_ROUTING],
    "alerts": STREAM_ALERTS,
    "audit": STREAM_AUDIT,
    "diagnostics": STREAM_DIAGNOSTICS,
    "prompt_history": STREAM_PROMPT,
}


def streams_for_role(role: str) -> list[str]:
    """Resolve a semantic role to the stream names that currently implement it."""

    value = STREAM_ROLES.get(role)
    if value is None:
        raise ValidationError(f"unknown stream role: {role!r}", details={"allowed": sorted(STREAM_ROLES)})
    return list(value) if isinstance(value, list) else [value]

# Event types used across the speech pipeline (section 8/17 of the task spec).
# They are correlated by ``correlation_id`` and never overwrite one another.
LISTENING_STARTED = "LISTENING_STARTED"
LISTENING_STOPPED = "LISTENING_STOPPED"
SPEECH_DETECTED = "SPEECH_DETECTED"
HEARD_SPEECH = "HEARD_SPEECH"
STT_PARTIAL_RESULT = "STT_PARTIAL_RESULT"
STT_FINAL_RESULT = "STT_FINAL_RESULT"
STT_ENGINE_ERROR = "STT_ENGINE_ERROR"
TRANSCRIPT_RESOLVED = "TRANSCRIPT_RESOLVED"
TRANSCRIPT_FILTERED = "TRANSCRIPT_FILTERED"
AGENT_SPEECH_STARTED = "AGENT_SPEECH_STARTED"
TTS_STARTED = "TTS_STARTED"
TTS_FINISHED = "TTS_FINISHED"
TTS_CANCELLED = "TTS_CANCELLED"
TTS_AUDIO_DETECTED_BY_MICROPHONE = "TTS_AUDIO_DETECTED_BY_MICROPHONE"
TTS_TRANSCRIPTION_EVALUATED = "TTS_TRANSCRIPTION_EVALUATED"

# Conversation / worker types.
CONVERSATION_MESSAGE = "CONVERSATION_MESSAGE"
WORKER_REGISTERED = "WORKER_REGISTERED"
WORKER_STATUS = "WORKER_STATUS"
WORKER_STATE_CHANGED = "WORKER_STATE_CHANGED"
ALERT_RAISED = "ALERT_RAISED"
ALERT_RECOVERED = "ALERT_RECOVERED"

VALID_SOURCE_KINDS = {"operator", "agent", "system", "client", "worker", "unknown"}


def utc_now_iso() -> str:
    """Return an RFC3339/ISO-8601 timestamp in UTC with a trailing ``Z``."""

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


_RESERVED_KEYS = {
    "id",
    "stream",
    "seq",
    "type",
    "ts",
    "schema_version",
    "source_id",
    "source_kind",
    "correlation_id",
    "idempotency_key",
    "data",
}


@dataclass
class Event:
    """A single durable record on a stream."""

    stream: str
    type: str
    data: dict[str, Any] = field(default_factory=dict)
    source_id: str = "system"
    source_kind: str = "system"
    correlation_id: str | None = None
    idempotency_key: str | None = None
    id: str = ""
    seq: int | None = None
    ts: str = ""
    schema_version: int = SCHEMA_VERSION
    extra: dict[str, Any] = field(default_factory=dict)

    def ensure_identity(self) -> "Event":
        """Populate id/timestamp if this event has not been persisted yet."""

        if not self.id:
            self.id = new_event_id()
        if not self.ts:
            self.ts = utc_now_iso()
        return self

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "id": self.id,
            "stream": self.stream,
            "seq": self.seq,
            "type": self.type,
            "ts": self.ts,
            "schema_version": self.schema_version,
            "source_id": self.source_id,
            "source_kind": self.source_kind,
            "data": self.data,
        }
        if self.correlation_id is not None:
            body["correlation_id"] = self.correlation_id
        if self.idempotency_key is not None:
            body["idempotency_key"] = self.idempotency_key
        # Re-emit any unknown fields we preserved on read.
        for key, value in self.extra.items():
            body.setdefault(key, value)
        return body

    def to_line(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"), ensure_ascii=False)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Event":
        extra = {key: value for key, value in raw.items() if key not in _RESERVED_KEYS}
        return cls(
            stream=str(raw.get("stream", "")),
            type=str(raw.get("type", "")),
            data=raw.get("data") if isinstance(raw.get("data"), dict) else {},
            source_id=str(raw.get("source_id", "system")),
            source_kind=str(raw.get("source_kind", "system")),
            correlation_id=raw.get("correlation_id"),
            idempotency_key=raw.get("idempotency_key"),
            id=str(raw.get("id", "")),
            seq=raw.get("seq"),
            ts=str(raw.get("ts", "")),
            schema_version=int(raw.get("schema_version", SCHEMA_VERSION)),
            extra=extra,
        )


def validate_stream(stream: str) -> str:
    if stream not in STREAMS and stream not in DYNAMIC_STREAMS:
        raise ValidationError(
            f"unknown stream: {stream!r}",
            details={"allowed": sorted(STREAMS) + sorted(DYNAMIC_STREAMS)},
        )
    return stream


def validate_new_event(
    stream: str,
    type_: str,
    data: Any,
    *,
    source_id: str,
    source_kind: str,
    correlation_id: str | None,
    idempotency_key: str | None,
) -> Event:
    """Validate a client-supplied event before it is admitted to the store."""

    validate_stream(stream)
    if not isinstance(type_, str) or not type_.strip():
        raise ValidationError("event 'type' is required and must be a non-empty string")
    if len(type_) > 128:
        raise ValidationError("event 'type' is too long (max 128 chars)")
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValidationError("event 'data' must be an object")
    if source_kind not in VALID_SOURCE_KINDS:
        raise ValidationError(
            f"invalid source_kind: {source_kind!r}",
            details={"allowed": sorted(VALID_SOURCE_KINDS)},
        )
    if correlation_id is not None and not isinstance(correlation_id, str):
        raise ValidationError("correlation_id must be a string")
    if idempotency_key is not None and not isinstance(idempotency_key, str):
        raise ValidationError("idempotency_key must be a string")
    event = Event(
        stream=stream,
        type=type_,
        data=data,
        source_id=str(source_id or "system"),
        source_kind=source_kind,
        correlation_id=correlation_id,
        idempotency_key=idempotency_key,
    )
    return event.ensure_identity()
