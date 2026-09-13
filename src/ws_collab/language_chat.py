"""ChatBot Test agent, fed by existing service STT with browser speech playback.

Call ``startup`` on the service loop before accepting Start. All other methods
except ``models``/``shutdown`` are safe to call from caption and HTTP threads.
The UI saves its automatically selected installed/default browser voice with
``configure({"voice_uri": selected_uri})`` before Start; no new capture is opened.
``client_factory(**httpx_options)`` may supply an AsyncClient for testing.
The optional clock returns Unix seconds; caption timestamps accept Unix seconds,
Unix milliseconds, or timezone-qualified ISO 8601.
"""

from __future__ import annotations

import asyncio
import codecs
import copy
import importlib
import json
import math
import os
import re
import threading
import time
import uuid
from collections import deque
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urlsplit

from .errors import ConfigurationError, ConflictError, ValidationError, WsCollabError


class LanguageChatError(WsCollabError):
    code = "language_chat_error"
    http_status = 502


DEFAULT_CONFIG = {
    "agent_id": "language-chat",
    "endpoint": "http://127.0.0.1:8801/v1",
    "model": "emullm/default",
    "system_prompt": (
        "You are a concise conversational assistant for testing and learning. "
        "Respond naturally and briefly. Do not take external actions unless requested."
    ),
    "turn_silence_ms": 1000,
    "speak_replies": True,
    "voice_uri": "",
    "speech_rate": 1.0,
    "language": "en-US",
    "max_tokens": 512,
    "history_limit": 24,
}

MAX_TEXT = 16_384
MAX_OUTPUT = 32_768
MAX_HISTORY_CHARS = 131_072
MAX_MESSAGES = 100
MAX_AGENTS = 32
MAX_QUEUE = 128
MAX_MODEL_TASKS = 2
MAX_SPEECH_CHARS = 320
MAX_WIRE_BYTES = 1_048_576
LEASE_SECONDS = 15.0
SETTLE_SECONDS = 0.35
ECHO_SECONDS = 4.0
REQUEST_SECONDS = 90.0
TICK_SECONDS = 0.25
_TERMINAL = {"complete", "interrupted", "error", "truncated"}
_INTERRUPTIONS = {"stop", "wait", "no", "pause", "hold on", "please stop", "be quiet"}


def _id(value, name):
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise ValidationError(f"{name} must be a nonblank string of at most 128 characters.")
    if any(ord(c) < 32 for c in value):
        raise ValidationError(f"{name} must not contain control characters.")
    return value.strip()


def _text(value, name="text", limit=MAX_TEXT, blank=False):
    if not isinstance(value, str) or len(value) > limit or (not blank and not value.strip()):
        raise ValidationError(f"{name} must be {'a' if blank else 'a nonblank'} string of at most {limit} characters.")
    return value


def _body(body, allowed):
    if not isinstance(body, dict) or any(key not in allowed for key in body):
        raise ValidationError("Expected an object with only the supported fields.")


def _timestamp(value):
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return None
            return parsed.timestamp()
        except (ValueError, OverflowError):
            return None
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return value / 1000 if value > 100_000_000_000 else float(value)
    return None


def _validated_config(body, base):
    _body(body, DEFAULT_CONFIG)
    result = dict(base)
    result.update(body)
    result["agent_id"] = _id(result["agent_id"], "agent_id")
    for name, limit, blank in (
        ("model", 256, True), ("system_prompt", MAX_TEXT, True),
        ("voice_uri", 1024, True), ("language", 64, False), ("endpoint", 2048, False),
    ):
        _text(result[name], name, limit, blank)
    endpoint = result["endpoint"].rstrip("/")
    try:
        url = urlsplit(endpoint)
        valid = (
            url.scheme in {"http", "https"} and url.hostname and url.port != 0
            and url.username is None and url.password is None and not url.query and not url.fragment
            and not any(c.isspace() or ord(c) < 32 for c in endpoint)
            and "\\" not in endpoint
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValidationError("endpoint must be an HTTP(S) base URL without credentials, query, or fragment.")
    result["endpoint"] = endpoint
    for name, low, high in (
        ("turn_silence_ms", 300, 10_000), ("max_tokens", 64, 4096), ("history_limit", 2, 100),
    ):
        value = result[name]
        if type(value) is not int or not low <= value <= high:
            raise ValidationError(f"{name} must be an integer from {low} to {high}.")
    rate = result["speech_rate"]
    if type(rate) not in (float, int) or not math.isfinite(rate) or not 0.5 <= rate <= 2:
        raise ValidationError("speech_rate must be a number from 0.5 to 2.")
    if type(result["speak_replies"]) is not bool:
        raise ValidationError("speak_replies must be a boolean.")
    return result


class LanguageChat:
    def __init__(
        self, directory, *, microphone_state, publish_message, audit,
        client_factory=None, clock=None, monotonic_clock=None,
    ):
        self.directory = Path(directory)
        self.path = self.directory / "language_chat.json"
        self._microphone_state = microphone_state
        self._publish_message = publish_message
        self._audit = audit
        self._client_factory = client_factory
        self._clock = clock or time.time
        self._monotonic = monotonic_clock or clock or time.monotonic
        self._lock = threading.RLock()
        self._selected = DEFAULT_CONFIG["agent_id"]
        self._agents = {self._selected: {"config": dict(DEFAULT_CONFIG), "messages": []}}
        self._loop = None
        self._tick_task = None
        self._model_task = None
        self._tasks = set()
        self._active = False
        self._client_id = None
        self._session_id = None
        self._session_owner = None
        self._generation = 0
        self._phase = "stopped"
        self._started_at = 0.0
        self._heartbeat_at = 0.0
        self._last_input_at = 0.0
        self._pending = ""
        self._pending_timing = None
        self._error = None
        self._current = None
        self._request = None
        self._speech_buffer = ""
        self._queue = []
        self._recent_speech = deque(maxlen=32)
        self._seen = deque(maxlen=512)
        self._seen_ids = set()
        self._echo_suppressed = 0
        self._output_suppressed = 0
        self._listen_after = 0.0
        self._last_speech_end = None
        self._pending_timing = None
        self._turn_clocks = {}
        self._effects = deque()
        self._flushing = False
        self._load()

    def _record(self):
        return self._agents[self._selected]

    def _cfg(self):
        return self._record()["config"]

    def _load(self):
        try:
            if not self.path.exists():
                return
            if self.path.stat().st_size > 32 * MAX_WIRE_BYTES:
                raise ValueError
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data["version"] != 1 or not isinstance(data["agents"], dict):
                raise ValueError
            if not 1 <= len(data["agents"]) <= MAX_AGENTS:
                raise ValueError
            agents = {}
            for agent_id, record in data["agents"].items():
                config = _validated_config(record["config"], DEFAULT_CONFIG)
                if agent_id != config["agent_id"]:
                    raise ValueError
                messages = record["messages"]
                if not isinstance(messages, list) or len(messages) > MAX_MESSAGES:
                    raise ValueError
                seen = set()
                for msg in messages:
                    if (
                        not isinstance(msg, dict) or msg.get("role") not in {"user", "assistant"}
                        or msg.get("status") not in _TERMINAL | {"streaming"}
                        or _timestamp(msg.get("created_at")) is None
                    ):
                        raise ValueError
                    msg_id = _id(msg["id"], "message id")
                    if msg_id in seen:
                        raise ValueError
                    seen.add(msg_id)
                    _text(msg["content"], limit=MAX_OUTPUT, blank=True)
                    if msg["status"] == "streaming":
                        msg["status"] = "interrupted"
                if sum(len(m["content"]) for m in messages) > MAX_HISTORY_CHARS:
                    raise ValueError
                agents[agent_id] = {"config": config, "messages": messages}
            if data["selected_agent"] not in agents:
                raise ValueError
            self._selected = data["selected_agent"]
            self._agents = agents
        except (OSError, ValueError, TypeError, KeyError, WsCollabError) as exc:
            raise ConfigurationError("Saved language-chat settings/history are invalid or unreadable.") from exc

    def _save_locked(self):
        data = {"version": 1, "selected_agent": self._selected, "agents": self._agents}
        staged = self.directory / f".language-chat-{uuid.uuid4().hex}.pending"
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            with staged.open("x", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(staged, self.path)
        except (OSError, UnicodeError) as exc:
            raise ConfigurationError("Could not persist language-chat settings/history.") from exc
        finally:
            staged.unlink(missing_ok=True)

    def _event_locked(self, event, **data):
        self._effects.append(("audit", (event, {"agent_id": self._selected, **data})))

    def _publish_locked(self, message):
        self._effects.append(("publish", (
            message.get("source_agent_id", self._selected), self._session_id, message["role"], message["content"],
            message["id"], message["status"],
        )))

    def _flush(self):
        # Serialize effects without holding a lock across application callbacks.
        # Reentrant callbacks can enqueue more work for this same drain.
        with self._lock:
            if self._flushing:
                return
            self._flushing = True
        try:
            while True:
                with self._lock:
                    if not self._effects:
                        self._flushing = False
                        return
                    kind, args = self._effects.popleft()
                callback = self._audit if kind == "audit" else self._publish_message
                try:
                    callback(*args)
                except Exception as exc:
                    with self._lock:
                        self._error = "The conversation persistence/audit callback failed."
                        self._cancel_locked("callback_failed", persist=False)
                        self._active = False
                        self._client_id = None
                        self._phase = "error"
                        self._effects.clear()
                    raise ConfigurationError(self._error) from exc
        except BaseException:
            with self._lock:
                self._flushing = False
            raise

    def config(self):
        with self._lock:
            return copy.deepcopy(self._cfg())

    def configure(self, body):
        _body(body, DEFAULT_CONFIG)
        with self._lock:
            live_mute = (
                "speak_replies" in body
                and set(body) <= {"agent_id", "speak_replies"}
                and body.get("agent_id", self._selected) == self._selected
            )
            if self._active and not live_mute:
                raise ConflictError("Stop language chat before changing its settings.")
            agent_id = _id(body.get("agent_id", self._selected), "agent_id")
            if agent_id not in self._agents and len(self._agents) >= MAX_AGENTS:
                raise ValidationError(f"At most {MAX_AGENTS} language-chat agents may be stored.")
            base = self._agents.get(agent_id, {}).get("config", DEFAULT_CONFIG)
            config = _validated_config({**body, "agent_id": agent_id}, base)
            previous, selected = copy.deepcopy(self._agents), self._selected
            self._agents.setdefault(agent_id, {"messages": []})["config"] = config
            self._selected = agent_id
            try:
                self._save_locked()
            except WsCollabError:
                self._agents, self._selected = previous, selected
                raise
            if selected != agent_id:
                self._session_id = self._session_owner = None
                self._queue.clear()
            if self._active and not config["speak_replies"]:
                self._speech_buffer = ""
                for item in self._queue:
                    if item["status"] in {"pending", "playing"}:
                        if item["status"] == "playing":
                            self._remember_locked(item)
                        item["status"] = "cancelled"
                self._update_phase_locked()
            self._error = None
            self._event_locked("language_chat.configured")
        self._flush()
        return self.config()

    def _microphone(self):
        try:
            raw = self._microphone_state()
        except Exception:
            return {"available": False, "state": "unavailable", "current_silence_ms": None,
                    "error": "Microphone state is unavailable."}
        if not isinstance(raw, dict):
            raw = {}
        state = raw.get("state", "unavailable")
        if state not in {"speech", "silence", "idle", "unavailable"}:
            state = "unavailable"
        ms = raw.get("current_silence_ms")
        valid_ms = type(ms) in (int, float) and math.isfinite(ms) and ms >= 0
        return {
            "available": raw.get("available") is True and not raw.get("stale", False),
            "state": state, "current_silence_ms": int(ms) if valid_ms else None,
            "error": "Microphone state reports an error." if raw.get("error") else None,
        }

    def state(self):
        microphone = self._microphone()
        with self._lock:
            self._update_phase_locked()
            return copy.deepcopy({
                "agents": [{"agent_id": key, "model": rec["config"]["model"]}
                           for key, rec in self._agents.items()],
                "agent_id": self._selected, "config": self._cfg(), "active": self._active,
                "phase": self._phase, "session_id": self._session_id, "client_id": self._client_id,
                "generation": self._generation, "messages": self._record()["messages"],
                "pending_text": self._pending, "error": self._error, "microphone": microphone,
                "speech_queue": self._queue, "echo_suppressed_count": self._echo_suppressed,
                "suppressed_input_count": self._output_suppressed,
                "input_accepting": self._input_ready_locked(),
                "input_gate_reason": (
                    "Stopped" if not self._active else
                    "Waiting for the full model reply and browser playback" if self._busy_locked() else
                    "Waiting for the playback echo tail" if self._clock() < self._listen_after else
                    "Ready for your speech"
                ),
                "turn_timings": [
                    self._timing_locked(message)
                    for message in self._record()["messages"]
                    if message.get("role") == "assistant" and message.get("timing")
                ][-50:],
                "next_request": self._request or self._preview_locked(),
            })

    def _preview_locked(self):
        config = self._cfg()
        history = [
            {"role": m["role"], "content": m["content"]}
            for m in self._record()["messages"]
            if m["status"] in {"complete", "interrupted", "truncated"} and m["content"].strip()
            and m.get("channel") != "agent_voice"
        ][-config["history_limit"]:]
        messages = [{"role": "system", "content": config["system_prompt"]}, *history]
        if self._pending.strip():
            messages.append({"role": "user", "content": self._pending.strip()})
        return {"model": config["model"], "messages": messages, "stream": True,
                "max_tokens": config["max_tokens"]}

    def request_preview(self):
        with self._lock:
            return copy.deepcopy(self._request or self._preview_locked())

    def set_history(self, body):
        _body(body, {"agent_id", "messages"})
        agent_id = _id(body.get("agent_id"), "agent_id")
        raw = body.get("messages")
        if not isinstance(raw, list) or len(raw) > MAX_MESSAGES:
            raise ValidationError(f"messages must be a list of at most {MAX_MESSAGES} messages.")
        messages = []
        for msg in raw:
            _body(msg, {"role", "content"})
            if msg.get("role") not in {"user", "assistant"}:
                raise ValidationError("History roles must be user or assistant; edit the system prompt in settings.")
            content = _text(msg.get("content"), "content", MAX_OUTPUT)
            messages.append(self._message(msg["role"], content, "complete"))
        if sum(len(m["content"]) for m in messages) > MAX_HISTORY_CHARS:
            raise ValidationError(f"History must contain at most {MAX_HISTORY_CHARS} characters.")
        with self._lock:
            if self._active:
                raise ConflictError("Stop language chat before editing history.")
            if agent_id not in self._agents:
                raise ValidationError("Select/configure this agent before editing its history.")
            previous = self._agents[agent_id]["messages"]
            self._agents[agent_id]["messages"] = messages
            try:
                self._save_locked()
            except WsCollabError:
                self._agents[agent_id]["messages"] = previous
                raise
            self._event_locked("language_chat.history_edited", agent_id=agent_id, message_count=len(messages))
        self._flush()
        return self.state()

    async def startup(self):
        loop = asyncio.get_running_loop()
        with self._lock:
            if self._loop is not None:
                if self._loop is not loop:
                    raise ConfigurationError("Language chat is already bound to another event loop.")
                return
            self._loop = loop
            self._tick_task = loop.create_task(self._tick(), name="language-chat-tick")

    async def shutdown(self):
        failure = None
        with self._lock:
            if self._loop is None:
                return
            if self._loop is not asyncio.get_running_loop():
                raise ConfigurationError("Shut down language chat on its startup event loop.")
            try:
                self._stop_locked("shutdown")
            except WsCollabError as exc:
                failure = exc
            tasks = list(self._tasks)
            if self._tick_task is not None:
                tasks.append(self._tick_task)
            self._loop = None
            self._tick_task = None
        try:
            self._flush()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            with self._lock:
                self._tasks.clear()
                self._model_task = None
        if failure is not None:
            raise failure

    def _owner_locked(self, client_id):
        if not self._active:
            raise ConflictError("Language chat is stopped. Press Start first.")
        if client_id != self._client_id:
            raise ConflictError("Another browser owns this language-chat session.")
        if self._clock() - self._heartbeat_at >= LEASE_SECONDS:
            raise ConflictError("The browser lease expired. Press Start again.")

    def start(self, client_id):
        client_id = _id(client_id, "client_id")
        with self._lock:
            if not self._loop or not self._loop.is_running():
                raise ConfigurationError("Language chat must be started on the service event loop first.")
            if not self._cfg()["model"].strip():
                raise ValidationError("Select an EMULLM model before starting language chat.")
            now = self._clock()
            if self._active and now - self._heartbeat_at >= LEASE_SECONDS:
                self._stop_locked("client_timeout")
            if self._active:
                self._owner_locked(client_id)
            else:
                if self._session_owner != client_id or not self._session_id:
                    self._session_id = uuid.uuid4().hex
                    self._session_owner = client_id
                self._active = True
                self._client_id = client_id
                self._started_at = now
                self._pending = ""
                self._pending_timing = None
                self._last_speech_end = None
                self._error = None
                self._generation += 1
                self._phase = "listening"
                self._event_locked("language_chat.started", session_id=self._session_id)
            self._heartbeat_at = now
        self._flush()
        return self.state()

    def _cancel_locked(self, reason, *, persist=True):
        self._generation += 1
        if self._model_task and not self._model_task.done() and self._loop:
            self._loop.call_soon_threadsafe(self._model_task.cancel)
        changed = self._current is not None
        if self._current is not None:
            self._current["status"] = "interrupted"
            self._finish_timing_locked(self._current["id"], "interrupted")
            self._publish_locked(self._current)
            self._current = None
        for item in self._queue:
            if item["status"] in {"pending", "playing"}:
                self._finish_timing_locked(item["message_id"], "interrupted")
                if item["status"] == "playing":
                    self._remember_locked(item)
                item["status"] = "cancelled"
        self._speech_buffer = ""
        self._listen_after = self._clock() + 0.5
        self._request = None
        self._phase = "listening" if self._active else "stopped"
        self._event_locked("language_chat.interrupted", reason=reason, generation=self._generation)
        if persist and changed:
            self._save_locked()

    def _stop_locked(self, reason):
        try:
            self._cancel_locked(reason)
        finally:
            self._active = False
            self._client_id = None
            self._pending = ""
            self._phase = "stopped"
            if reason == "client_timeout":
                self._error = "The browser heartbeat expired. Press Start to resume."
            self._event_locked("language_chat.stopped", reason=reason)

    def stop(self, client_id):
        client_id = _id(client_id, "client_id")
        with self._lock:
            if self._active:
                if client_id != self._client_id:
                    raise ConflictError("Only the owning browser may stop this session.")
                self._stop_locked("operator")
        self._flush()
        return self.state()

    def interrupt(self, client_id, reason="operator"):
        client_id = _id(client_id, "client_id")
        _text(reason, "reason", 128)
        with self._lock:
            self._owner_locked(client_id)
            # Operator-supplied text is not copied into operational audit metadata.
            self._cancel_locked("operator")
            self._pending = ""
            self._pending_timing = None
        self._flush()
        return self.state()

    def _message(self, role, content, status):
        return {"id": uuid.uuid4().hex, "role": role, "content": content,
                "created_at": self._clock(), "status": status}

    def _trim_locked(self):
        messages = self._record()["messages"]
        chars = sum(len(m["content"]) for m in messages)
        while len(messages) > MAX_MESSAGES or chars > MAX_HISTORY_CHARS:
            chars -= len(messages.pop(0)["content"])

    def _busy_locked(self):
        return self._current is not None or any(q["status"] in {"pending", "playing"} for q in self._queue)

    def _input_ready_locked(self):
        return self._active and not self._busy_locked() and self._clock() >= self._listen_after

    def _remember_input_locked(self, utterance_id):
        if utterance_id in self._seen_ids:
            return
        if len(self._seen) == self._seen.maxlen:
            self._seen_ids.discard(self._seen.popleft())
        self._seen.append(utterance_id)
        self._seen_ids.add(utterance_id)

    def _timing_message_locked(self, message_id):
        return next((message for message in self._record()["messages"] if message["id"] == message_id), None)

    def _mark_timing_locked(self, message_id, stage):
        message = self._timing_message_locked(message_id)
        clocks = self._turn_clocks.get(message_id)
        if message is None or clocks is None:
            return
        if stage not in clocks or stage == "tts_finished":
            clocks[stage] = self._monotonic()
            message["timing"][f"{stage}_at"] = self._clock()

    def _finish_timing_locked(self, message_id, status):
        message = self._timing_message_locked(message_id)
        if message is None or "timing" not in message:
            return
        self._mark_timing_locked(message_id, "finished")
        message["timing"]["status"] = status
        message["timing"].update(self._timing_locked(message))

    def _maybe_finish_timing_locked(self, message_id):
        clocks = self._turn_clocks.get(message_id, {})
        message = self._timing_message_locked(message_id)
        if not message or "response_finished" not in clocks or message["status"] not in {"complete", "truncated"}:
            return False
        speech = [item for item in self._queue if item["message_id"] == message_id]
        if any(item["status"] in {"pending", "playing"} for item in speech):
            return False
        self._finish_timing_locked(message_id, "error" if any(item["status"] == "error" for item in speech) else "complete")
        return True

    def _timing_locked(self, message):
        timing = dict(message.get("timing") or {})
        clocks = self._turn_clocks.get(message["id"])
        if not clocks:
            return timing
        now = self._monotonic()
        pairs = {
            "turn_wait_ms": ("scheduled", "input_finished"),
            "model_queue_ms": ("request_started", "scheduled"),
            "emullm_first_token_ms": ("first_token", "request_started"),
            "emullm_total_ms": ("response_finished", "request_started"),
            "tts_queue_wait_ms": ("tts_started", "tts_queued"),
            "total_ms": ("finished", "input_received"),
        }
        for name, (end, start) in pairs.items():
            timing[name] = (
                round(max(0.0, clocks[end] - clocks[start]) * 1000)
                if end in clocks and start in clocks else None
            )
        browser_duration = clocks.get("reported_speech_ms")
        timing["tts_playback_ms"] = (
            browser_duration
            if browser_duration is not None and not clocks.get("unmeasured_speech_chunks")
            else round(max(0.0, clocks["tts_finished"] - clocks["tts_started"]) * 1000)
            if "tts_finished" in clocks and "tts_started" in clocks else None
        )
        timing["tts_playback_source"] = "browser" if browser_duration is not None and not clocks.get("unmeasured_speech_chunks") else "server acknowledgements"
        timing["elapsed_ms"] = round(max(0.0, clocks.get("finished", now) - clocks["input_received"]) * 1000)
        if "finished" not in clocks:
            timing["status"] = (
                "speaking" if any(item["message_id"] == message["id"] and item["status"] in {"pending", "playing"} for item in self._queue)
                else "responding" if "first_token" in clocks else "thinking"
            )
        return timing

    def _begin_locked(self):
        if not self._pending.strip():
            raise ValidationError("There is no pending text to send.")
        self._generation += 1
        generation = self._generation
        request = self._preview_locked()
        user = self._message("user", self._pending.strip(), "complete")
        previous = list(self._record()["messages"])
        pending = self._pending
        self._record()["messages"].append(user)
        self._pending = ""
        self._trim_locked()
        try:
            self._save_locked()
        except WsCollabError:
            self._record()["messages"] = previous
            self._pending = pending
            raise
        self._publish_locked(user)
        assistant = self._message("assistant", "", "streaming")
        now = self._clock()
        pending_timing = self._pending_timing or {
            "input_source": "manual", "input_received_at": now,
            "input_received_mono": self._monotonic(), "stt_result_at": None, "speech_end_at": None,
        }
        assistant["timing"] = {
            "message_id": assistant["id"], "agent_id": self._selected,
            "session_id": self._session_id, "status": "thinking",
            **{key: value for key, value in pending_timing.items() if not key.endswith("_mono")},
            "speech_to_stt_ms": (
                round((pending_timing["stt_result_at"] - pending_timing["speech_end_at"]) * 1000)
                if pending_timing["speech_end_at"] is not None and pending_timing["stt_result_at"] is not None
                and pending_timing["stt_result_at"] >= pending_timing["speech_end_at"] else None
            ),
            "stt_delivery_ms": (
                round(max(0.0, pending_timing.get("input_finished_at", pending_timing["input_received_at"]) - pending_timing["stt_result_at"]) * 1000)
                if pending_timing["stt_result_at"] is not None else None
            ),
        }
        self._turn_clocks[assistant["id"]] = {
            "input_received": pending_timing["input_received_mono"], "scheduled": self._monotonic(),
            "input_finished": pending_timing.get("input_finished_mono", pending_timing["input_received_mono"]),
        }
        while len(self._turn_clocks) > MAX_MESSAGES:
            self._turn_clocks.pop(next(iter(self._turn_clocks)))
        self._pending_timing = None
        self._record()["messages"].append(assistant)
        self._trim_locked()
        self._current = assistant
        self._request = request
        self._error = None
        self._phase = "thinking"
        self._speech_buffer = ""
        self._event_locked("language_chat.requested", generation=generation, message_id=assistant["id"])
        self._loop.call_soon_threadsafe(self._launch, generation, request, self._cfg()["endpoint"])

    def submit_text(self, client_id, text):
        client_id = _id(client_id, "client_id")
        text = _text(text).strip()
        with self._lock:
            self._owner_locked(client_id)
            combined = " ".join(filter(None, (self._pending, text)))
            _text(combined)
            if self._busy_locked():
                self._cancel_locked("manual_input")
            self._pending = combined
            if self._pending_timing is None:
                self._pending_timing = {
                    "input_source": "manual", "input_received_at": self._clock(),
                    "input_received_mono": self._monotonic(), "stt_result_at": None, "speech_end_at": None,
                }
            self._begin_locked()
        self._flush()
        return self.state()

    def send_now(self, client_id):
        client_id = _id(client_id, "client_id")
        with self._lock:
            self._owner_locked(client_id)
            if not self._pending.strip():
                raise ValidationError("There is no pending text to send.")
            if self._busy_locked():
                self._cancel_locked("manual_send")
            self._begin_locked()
        self._flush()
        return self.state()

    def speak_agent(self, agent_id, text):
        agent_id = _id(agent_id, "agent_id")
        text = _text(text).strip()
        with self._lock:
            if not self._input_live_locked():
                raise ConflictError("Open and start ChatBot Test before sending agent speech.")
            if self._busy_locked():
                raise ConflictError("Wait for the current reply to finish before sending agent speech.")
            if not self._cfg()["speak_replies"]:
                raise ConflictError("Spoken replies are muted.")
            message = self._message("assistant", text, "complete")
            message.update(channel="agent_voice", source_agent_id=agent_id)
            received = self._monotonic()
            message["timing"] = {
                "message_id": message["id"], "agent_id": agent_id, "session_id": self._session_id,
                "input_source": "agent_voice", "input_received_at": self._clock(),
                "response_finished_at": self._clock(), "status": "speaking",
                "speech_to_stt_ms": None, "stt_delivery_ms": None,
            }
            self._turn_clocks[message["id"]] = {
                "input_received": received, "input_finished": received,
                "scheduled": received, "response_finished": received,
            }
            while len(self._turn_clocks) > MAX_MESSAGES:
                self._turn_clocks.pop(next(iter(self._turn_clocks)))
            previous = list(self._record()["messages"])
            self._record()["messages"].append(message)
            self._trim_locked()
            try:
                self._save_locked()
            except WsCollabError:
                self._record()["messages"] = previous
                self._turn_clocks.pop(message["id"], None)
                raise
            self._generation += 1
            self._current = message
            self._speech_buffer = text
            try:
                self._sentences_locked(final=True)
            finally:
                self._current = None
            voice = self._agents.get(agent_id, {}).get("config", {}).get("voice_uri") or self._cfg()["voice_uri"]
            for item in self._queue:
                if item["message_id"] == message["id"]:
                    item.update(source_agent_id=agent_id, voice_uri=voice)
            self._error = None
            self._publish_locked(message)
            self._event_locked("language_chat.agent_speech", source_agent_id=agent_id, message_id=message["id"])
            self._update_phase_locked()
        self._flush()
        return {"queued": True, "message_id": message["id"], **self.state()}

    def _remember_locked(self, item):
        self._recent_speech.append((self._clock(), item["id"], item["text"], item.get("source_agent_id", self._selected)))

    def heartbeat(self, body):
        _body(body, {"client_id", "speech_state", "speech_id", "error", "duration_ms"})
        client_id = _id(body.get("client_id"), "client_id")
        speech_state = body.get("speech_state")
        if speech_state not in {"idle", "speaking", "done", "error"}:
            raise ValidationError("speech_state must be idle, speaking, done, or error.")
        speech_id = body.get("speech_id")
        if speech_state != "idle" or speech_id is not None:
            speech_id = _id(speech_id, "speech_id")
        if "error" in body:
            _text(body["error"], "error", 1024, blank=True)
        if "duration_ms" in body and (
            type(body["duration_ms"]) is not int or not 0 <= body["duration_ms"] <= 1_800_000
        ):
            raise ValidationError("duration_ms must be an integer from 0 to 1800000.")
        with self._lock:
            self._owner_locked(client_id)
            acknowledged = False
            if speech_id is not None:
                item = next((q for q in self._queue if q["id"] == speech_id), None)
                if item is not None and item["generation"] == self._generation:
                    if speech_state == "speaking" and item["status"] == "pending":
                        first = next((q for q in self._queue if q["status"] in {"pending", "playing"}), None)
                        if first is not item:
                            raise ConflictError("Speak queued sentences in order, one at a time.")
                        item["status"] = "playing"
                        self._mark_timing_locked(item["message_id"], "tts_started")
                        self._remember_locked(item)
                        acknowledged = True
                    elif speech_state == "speaking" and item["status"] == "playing":
                        acknowledged = True
                    elif speech_state in {"done", "error"} and item["status"] in {"playing", "pending"}:
                        # A fast browser can finish before its 'speaking' heartbeat.
                        first = next((q for q in self._queue if q["status"] in {"pending", "playing"}), None)
                        if first is not item:
                            raise ConflictError("Acknowledge queued sentences in order.")
                        item["status"] = speech_state
                        if "duration_ms" in body:
                            item["duration_ms"] = body["duration_ms"]
                        clocks = self._turn_clocks.get(item["message_id"])
                        if clocks is not None and speech_state == "done":
                            if "duration_ms" in body:
                                clocks["reported_speech_ms"] = clocks.get("reported_speech_ms", 0) + body["duration_ms"]
                            else:
                                clocks["unmeasured_speech_chunks"] = clocks.get("unmeasured_speech_chunks", 0) + 1
                        self._mark_timing_locked(item["message_id"], "tts_finished")
                        self._listen_after = self._clock() + 0.5
                        self._remember_locked(item)
                        acknowledged = True
                        if speech_state == "error":
                            self._error = "Browser speech synthesis failed; the reply remains available as text."
                            self._event_locked("language_chat.speech_error", speech_id=speech_id)
                            self._stop_locked("speech_error")
                            self._phase = "error"
                        if self._maybe_finish_timing_locked(item["message_id"]):
                            self._save_locked()
                    elif speech_state == item["status"]:
                        acknowledged = True
            self._heartbeat_at = self._clock()
            self._update_phase_locked()
        self._flush()
        result = self.state()
        result["acknowledged"] = acknowledged
        return result

    def _update_phase_locked(self):
        if not self._active:
            self._phase = "error" if self._error and self._phase == "error" else "stopped"
        elif any(q["status"] in {"pending", "playing"} for q in self._queue):
            self._phase = "speaking"
        elif self._current is not None:
            self._phase = "responding" if self._current["content"] else "thinking"
        elif self._clock() < self._listen_after:
            self._phase = "finishing_speech"
        else:
            self._phase = "error" if self._error else "listening"

    def _contexts_locked(self):
        now = self._clock()
        contexts = {}
        for at, speech_id, text, agent_id in self._recent_speech:
            if now - at <= ECHO_SECONDS:
                contexts[speech_id] = (text, agent_id)
        for item in self._queue:
            if item["status"] == "playing":
                contexts[item["id"]] = (item["text"], item.get("source_agent_id", self._selected))
        audible_messages = {q["message_id"] for q in self._queue if q["id"] in contexts}
        for message_id in audible_messages:
            spoken = [q["text"] for q in self._queue if q["message_id"] == message_id
                      and (q["status"] in {"done", "playing"} or q["id"] in contexts)]
            if len(spoken) > 1:
                # A single ASR final may span multiple browser sentence chunks.
                contexts[f"{message_id}:spoken"] = (" ".join(spoken), self._selected)
        return [{"tts_event_id": key, "expected_text": text, "agent_id": agent_id}
                for key, (text, agent_id) in contexts.items()]

    def active_tts_context(self):
        with self._lock:
            return self._contexts_locked()

    def _echo_locked(self, text, *, partial=False):
        def words_for(value):
            return re.findall(r"\w+", value.casefold().replace("'", "").replace("\u2019", ""))

        words = words_for(text)
        normalized = " ".join(words)
        if not words or normalized in _INTERRUPTIONS:
            return False
        contexts = self._contexts_locked()
        references = [c["expected_text"] for c in contexts]
        if len(references) > 1:
            references.append(" ".join(references))
        for reference in references:
            expected = words_for(reference)
            if words == expected:
                return True
            if partial and expected[:len(words)] == words:
                return True
            if len(words) >= 3 and f" {normalized} " in f" {' '.join(expected)} ":
                return True
            if len(words) >= 4 and SequenceMatcher(None, words, expected).ratio() >= 0.9:
                return True
            if 4 <= len(words) <= len(expected) + 1:
                tolerance = min(2, max(1, len(words) // 6))
                for size in (len(words) - 1, len(words), len(words) + 1):
                    if size <= 0:
                        continue
                    for start in range(max(0, len(expected) - size + 1)):
                        window = expected[start:start + size]
                        if SequenceMatcher(None, words, window).ratio() >= 1 - tolerance / max(len(words), size):
                            return True
        return False

    def _input_live_locked(self):
        return self._active and self._clock() - self._heartbeat_at < LEASE_SECONDS

    def on_caption(self, text, utterance_id, result_at, is_echo=False):
        at = _timestamp(result_at)
        if not isinstance(text, str) or not text.strip() or at is None:
            return False
        if not isinstance(utterance_id, str) or not utterance_id.strip() or len(utterance_id) > 256:
            return False
        accepted = False
        with self._lock:
            now = self._clock()
            if (
                not self._input_live_locked() or at < self._started_at or at > now + 5
                or utterance_id in self._seen_ids
            ):
                return False
            self._remember_input_locked(utterance_id)
            if not self._input_ready_locked():
                self._output_suppressed += 1
                return False
            if is_echo or (len(text) <= MAX_TEXT and self._echo_locked(text)):
                self._echo_suppressed += 1
                return False
            combined = " ".join(filter(None, (self._pending, text.strip())))
            if len(combined) > MAX_TEXT:
                self._error = "Pending microphone text is too long. Send or clear it before continuing."
                self._event_locked("language_chat.input_limit")
            else:
                self._pending = combined
                self._last_input_at = now
                if self._pending_timing is None:
                    self._pending_timing = {
                        "input_source": "stt", "input_received_at": now,
                        "input_received_mono": self._monotonic(),
                    }
                self._pending_timing.update({
                    "input_finished_at": now, "input_finished_mono": self._monotonic(),
                    "stt_result_at": at, "speech_end_at": self._last_speech_end,
                })
                accepted = True
        self._flush()
        return accepted

    def on_partial(self, text, utterance_id=None):
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
            return False
        with self._lock:
            if not self._input_live_locked():
                return False
            if not self._input_ready_locked():
                if self._busy_locked() and isinstance(utterance_id, str) and 0 < len(utterance_id) <= 256:
                    self._remember_input_locked(utterance_id)
                self._output_suppressed += 1
                return False
            if self._echo_locked(text, partial=True):
                self._echo_suppressed += 1
                return False
            self._last_input_at = self._clock()
        self._flush()
        return True

    def on_speech_start(self):
        with self._lock:
            if not self._input_live_locked():
                return False
            if not self._input_ready_locked():
                return False
            # Raw VAD is not evidence of barge-in while our own audio is audible.
            if self._contexts_locked():
                return False
            self._last_input_at = self._clock()
            self._last_speech_end = None
        self._flush()
        return True

    def on_speech_end(self, at):
        ended = _timestamp(at)
        if ended is None:
            return False
        with self._lock:
            if not self._input_live_locked() or not self._input_ready_locked():
                return False
            self._last_speech_end = ended
        return True

    def _tick_once(self):
        microphone = self._microphone()
        with self._lock:
            if not self._active:
                return
            now = self._clock()
            if now - self._heartbeat_at >= LEASE_SECONDS:
                self._stop_locked("client_timeout")
            elif (
                self._pending and self._input_ready_locked()
                and now - self._last_input_at >= SETTLE_SECONDS
                and microphone["available"] and microphone["state"] == "silence"
                and microphone["error"] is None and microphone["current_silence_ms"] is not None
                and microphone["current_silence_ms"] >= self._cfg()["turn_silence_ms"]
            ):
                self._begin_locked()
        self._flush()

    async def _tick(self):
        while True:
            await asyncio.sleep(TICK_SECONDS)
            try:
                self._tick_once()
            except WsCollabError:
                with self._lock:
                    self._cancel_locked("storage_failed", persist=False)
                    self._active = False
                    self._client_id = None
                    self._phase = "error"
                    self._error = "Language chat stopped because conversation state could not be saved."

    def _client(self):
        try:
            httpx = importlib.import_module("httpx")
        except ImportError as exc:
            raise ConfigurationError("Language chat requires the optional 'remote' dependency (httpx).") from exc
        factory = self._client_factory or httpx.AsyncClient
        return factory(timeout=httpx.Timeout(30.0, connect=5.0), follow_redirects=False, trust_env=False)

    @staticmethod
    def _headers():
        token = os.environ.get("WS_COLLAB_EMULLM_TOKEN", "").strip()
        return {"Authorization": f"Bearer {token}"} if token else {}

    @staticmethod
    def _status(response):
        if response.status_code < 200 or response.status_code >= 300:
            raise LanguageChatError(
                f"EMULLM returned HTTP {response.status_code}; check the endpoint, model, and optional authentication."
            )

    @staticmethod
    async def _json_response(response):
        data = bytearray()
        async for chunk in response.aiter_bytes():
            if len(data) + len(chunk) > MAX_WIRE_BYTES:
                raise LanguageChatError("EMULLM response exceeded the size limit.")
            data.extend(chunk)
        try:
            result = json.loads(data)
        except (ValueError, UnicodeError) as exc:
            raise LanguageChatError("EMULLM returned invalid JSON.") from exc
        if not isinstance(result, dict) or "error" in result:
            raise LanguageChatError("EMULLM returned an invalid response or provider error.")
        return result

    async def models(self):
        endpoint = self.config()["endpoint"]
        try:
            async def fetch():
                async with self._client() as client:
                    async with client.stream("GET", endpoint + "/models", headers=self._headers()) as response:
                        self._status(response)
                        return await self._json_response(response)
            data = await asyncio.wait_for(fetch(), timeout=REQUEST_SECONDS)
            rows = data.get("data")
            if not isinstance(rows, list) or not rows:
                raise LanguageChatError("EMULLM did not return any available models.")
            models = []
            for row in rows[:256]:
                if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"].strip():
                    raise LanguageChatError("EMULLM returned an invalid model list.")
                if len(row["id"]) > 256:
                    raise LanguageChatError("EMULLM returned an invalid model identifier.")
                models.append({"id": row["id"]})
            return {"models": models, "endpoint": endpoint}
        except asyncio.CancelledError:
            raise
        except WsCollabError:
            raise
        except Exception as exc:
            # Provider exception strings and response bodies can contain headers.
            raise LanguageChatError("Could not query EMULLM models; check the endpoint and provider availability.") from exc

    def _launch(self, generation, request, endpoint):
        with self._lock:
            if not self._valid_locked(generation) or self._loop is None:
                return
            if sum(not task.done() for task in self._tasks) < MAX_MODEL_TASKS:
                task = self._loop.create_task(self._run(generation, request, endpoint), name="language-chat-model")
                self._model_task = task
                self._tasks.add(task)
                task.add_done_callback(self._task_done)
                return
        self._fail(generation, LanguageChatError("The previous model request is still closing. Try again shortly."))

    def _task_done(self, task):
        with self._lock:
            self._tasks.discard(task)
            if self._model_task is task:
                self._model_task = None
        if not task.cancelled():
            task.exception()  # _run records operational failures before returning.

    def _valid_locked(self, generation):
        return self._active and generation == self._generation and self._current is not None

    def _enqueue_locked(self, text):
        text = text.strip()
        if not text:
            return
        while len(self._queue) >= MAX_QUEUE:
            terminal = next((q for q in self._queue if q["status"] not in {"pending", "playing"}), None)
            if terminal is None:
                raise LanguageChatError("The browser speech queue is full; resume playback or interrupt this reply.")
            self._queue.remove(terminal)
        self._queue.append({
            "id": uuid.uuid4().hex, "message_id": self._current["id"], "text": text,
            "generation": self._generation, "status": "pending",
        })
        self._mark_timing_locked(self._current["id"], "tts_queued")

    def _sentences_locked(self, final=False):
        while self._speech_buffer:
            match = re.search(r'[.!?](?:["\')\]]*)(?=\s)', self._speech_buffer)
            cut = match.end() if match else 0
            if not cut or cut > MAX_SPEECH_CHARS:
                if len(self._speech_buffer) > MAX_SPEECH_CHARS:
                    cut = self._speech_buffer.rfind(" ", 0, MAX_SPEECH_CHARS + 1)
                    if cut <= 0:
                        cut = MAX_SPEECH_CHARS
                elif final:
                    cut = len(self._speech_buffer)
                else:
                    break
            self._enqueue_locked(self._speech_buffer[:cut])
            self._speech_buffer = self._speech_buffer[cut:].lstrip()

    def _delta(self, generation, text):
        with self._lock:
            if not self._valid_locked(generation):
                raise asyncio.CancelledError
            if len(self._current["content"]) + len(text) > MAX_OUTPUT:
                raise LanguageChatError("EMULLM reply exceeded the output limit.")
            self._current["content"] += text
            if text:
                self._mark_timing_locked(self._current["id"], "first_token")
            self._trim_locked()
            if self._cfg()["speak_replies"]:
                self._speech_buffer += text
                self._sentences_locked()
            self._update_phase_locked()

    def _chunk(self, generation, data):
        if not isinstance(data, dict) or "error" in data:
            raise LanguageChatError("EMULLM stream reported a provider error or malformed event.")
        choices = data.get("choices")
        if choices == [] and isinstance(data.get("usage"), dict):
            return None
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise LanguageChatError("EMULLM stream has invalid choices.")
        choice = choices[0]
        delta = choice.get("delta")
        if choice.get("index", 0) != 0 or not isinstance(delta, dict):
            raise LanguageChatError("EMULLM stream has an invalid delta.")
        if delta.get("role") not in {None, "assistant"} or delta.get("tool_calls") or delta.get("function_call"):
            raise LanguageChatError("This conversation supports text replies, not provider tool calls.")
        content = delta.get("content")
        if content is not None:
            if not isinstance(content, str):
                raise LanguageChatError("EMULLM stream content must be text.")
            self._delta(generation, content)
        finish = choice.get("finish_reason")
        if finish not in {None, "stop", "length"}:
            raise LanguageChatError("EMULLM did not finish with a usable text reply.")
        return finish

    async def _sse(self, response, generation):
        decoder = codecs.getincrementaldecoder("utf-8")("strict")
        buffer, lines, total = "", [], 0

        def dispatch():
            payload = "\n".join(lines)
            lines.clear()
            if not payload:
                return None
            if payload.strip() == "[DONE]":
                return "stop"
            try:
                data = json.loads(payload)
            except ValueError as exc:
                raise LanguageChatError("EMULLM returned a malformed streaming event.") from exc
            return self._chunk(generation, data)

        def line_received(line):
            line = line.rstrip("\r")
            if not line:
                return dispatch()
            if line.startswith("data:"):
                lines.append(line[5:].removeprefix(" "))
                if sum(map(len, lines)) > 65_536:
                    raise LanguageChatError("EMULLM streaming event exceeded the size limit.")
            return None

        async for raw in response.aiter_bytes():
            total += len(raw)
            if total > MAX_WIRE_BYTES:
                raise LanguageChatError("EMULLM stream exceeded the size limit.")
            buffer += decoder.decode(raw)
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                finish = line_received(line)
                if finish:
                    return finish
            if len(buffer) > 65_536:
                raise LanguageChatError("EMULLM streaming line exceeded the size limit.")
        buffer += decoder.decode(b"", final=True)
        if buffer:
            finish = line_received(buffer)
            if finish:
                return finish
        finish = dispatch()
        if not finish:
            raise LanguageChatError("EMULLM stream ended before a completion marker.")
        return finish

    async def _consume(self, generation, request, endpoint):
        async with self._client() as client:
            with self._lock:
                if not self._valid_locked(generation):
                    raise asyncio.CancelledError
                self._mark_timing_locked(self._current["id"], "request_started")
            async with client.stream(
                "POST", endpoint + "/chat/completions", json=request, headers=self._headers(),
            ) as response:
                self._status(response)
                content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if content_type == "text/event-stream":
                    return await self._sse(response, generation)
                if content_type == "application/json" or content_type.endswith("+json"):
                    data = await self._json_response(response)
                    choices = data.get("choices")
                    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
                        raise LanguageChatError("EMULLM JSON reply has invalid choices.")
                    choice = choices[0]
                    message = choice.get("message")
                    if not isinstance(message, dict) or not isinstance(message.get("content"), str):
                        raise LanguageChatError("EMULLM JSON reply has no text message.")
                    return self._chunk(generation, {"choices": [{
                        "index": choice.get("index", 0), "delta": message,
                        "finish_reason": choice.get("finish_reason", "stop"),
                    }]}) or "stop"
                raise LanguageChatError("EMULLM returned neither an SSE stream nor a JSON reply.")

    async def _run(self, generation, request, endpoint):
        try:
            finish = await asyncio.wait_for(self._consume(generation, request, endpoint), REQUEST_SECONDS)
            with self._lock:
                if not self._valid_locked(generation):
                    return
                if not self._current["content"].strip():
                    raise LanguageChatError("EMULLM returned an empty reply.")
                self._sentences_locked(final=True)
                self._mark_timing_locked(self._current["id"], "response_finished")
                self._current["status"] = "truncated" if finish == "length" else "complete"
                self._current["finish_reason"] = finish
                self._maybe_finish_timing_locked(self._current["id"])
                self._save_locked()
                self._publish_locked(self._current)
                self._event_locked("language_chat.completed", generation=generation, finish_reason=finish)
                self._current = None
                self._request = None
                self._update_phase_locked()
            self._flush()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._fail(generation, exc)

    def _fail(self, generation, exc):
        error = exc.message if isinstance(exc, WsCollabError) else (
            "EMULLM request failed or timed out; check the endpoint, model, and provider availability."
        )
        with self._lock:
            if not self._valid_locked(generation):
                return
            self._error = error
            self._current["status"] = "error"
            self._mark_timing_locked(self._current["id"], "response_finished")
            self._finish_timing_locked(self._current["id"], "error")
            self._publish_locked(self._current)
            self._current = None
            self._request = None
            self._speech_buffer = ""
            self._generation += 1
            for item in self._queue:
                if item["status"] in {"pending", "playing"}:
                    if item["status"] == "playing":
                        self._remember_locked(item)
                    item["status"] = "cancelled"
            self._phase = "error"
            self._event_locked("language_chat.failed", generation=generation)
            try:
                self._save_locked()
            except WsCollabError:
                self._error = "EMULLM request failed and conversation state could not be saved."
                self._active = False
                self._client_id = None
        self._flush()
