"""The shared WS_COLLAB service layer.

Every essential capability lives here exactly once. REST and WebSocket are thin
transports over this object, which is what guarantees parity: an event written
through either transport is durably appended, fanned out to live subscribers, and
visible to the other transport immediately, with identical IDs, cursors,
idempotency, filters, validation, auditing, and worker/routing/prompt logic.
"""

from __future__ import annotations

import asyncio
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
    HEARD_SPEECH,
    STREAM_AUDIT,
    STREAM_CONVERSATION,
    STREAM_DIAGNOSTICS,
    STREAM_STT_TRANSCRIPTS,
    STREAM_TRANSLATED_AUDIO,
    STREAM_TTS,
    STREAM_ROLES,
    STREAM_STATUSES,
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

    def list_mailboxes(self) -> dict[str, Any]:
        """List every durable stream as a mailbox (one JSONL file each)."""
        stats = {row["stream"]: row for row in self.store.stats()}
        mailboxes = [
            {
                "id": name,
                "kind": "stream",
                "messages": int((stats.get(name) or {}).get("seq") or 0),
                "name": name,
                "filename": filename,
            }
            for name, filename in STREAMS.items()
        ]
        return {"mailboxes": mailboxes, "server_time": utc_now_iso()}

    def mailbox_agents(self) -> dict[str, Any]:
        """Agents for the YOU/TO pickers: the operator plus registered workers."""
        agents: list[dict[str, Any]] = [{"id": "operator", "kind": "operator"}]
        seen = {"operator"}
        for worker in self.workers.list_workers():
            worker_id = str(worker.get("worker_id") or worker.get("id") or "")
            if worker_id and worker_id not in seen:
                seen.add(worker_id)
                agents.append({**worker, "id": worker_id, "kind": "worker"})
        return {"agents": agents}

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
    ) -> dict[str, Any]:
        """Messages in a mailbox = events in that stream (newest last).

        Unknown mailboxes read as empty rather than erroring. When ``do_filter``
        is set, the require-match bar constraints apply: ``sender`` is the chat's
        YOU (the message ``from``), ``to`` the addressed recipient, ``send_to``
        the routed mailbox (null == this mailbox) and ``text`` a case-insensitive
        substring."""
        if mailbox not in STREAMS:
            return {"messages": [], "user": sender or "", "peer": mailbox}
        events = self.store.tail(mailbox, max(1, min(limit, 2000)), _build_predicate(filters))
        messages = [self._event_to_message(event.to_dict()) for event in events]
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
        topic = next((name for name in (send_to, to) if name and name in STREAMS), STREAM_CONVERSATION)
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
        if stream not in STREAMS:
            raise ValidationError(f"unknown mailbox: {stream!r}", details={"allowed": sorted(STREAMS)})
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
