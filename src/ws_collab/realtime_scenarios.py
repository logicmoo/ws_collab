"""Reusable spoken turn-taking scenarios and deterministic production-path harness."""

from __future__ import annotations

import math
import base64
import json
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from .meet_bridge.bridge import forward_companion_heard_audio
from .meet_bridge.companion_audio import CompanionAudioArbiter
from .stt.base import Hypothesis, normalize_text

AGENT = "agent"
USER = "user"

ERROR_DUPLICATE = "duplicate"
ERROR_MISSING = "missing"
ERROR_OUT_OF_ORDER = "out_of_order"
ERROR_WRONG_SPEAKER = "wrong_speaker"
ERROR_ECHO_LEAK = "echo_leak"
ERROR_DEADLINE_EXCEEDED = "deadline_exceeded"


@dataclass(frozen=True)
class ExpectedTurn:
    index: int
    actor: str
    spoken_token: str
    accepted_asr_forms: tuple[str, ...]
    deadline_ms: float

    def accepts(self, text: str) -> bool:
        return normalize_text(text) in {normalize_text(value) for value in self.accepted_asr_forms}

    def public(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "actor": self.actor,
            "spoken_token": self.spoken_token,
            "accepted_asr_forms": list(self.accepted_asr_forms),
            "deadline_ms": self.deadline_ms,
        }


@dataclass(frozen=True)
class TurnTakingScenario:
    name: str
    turns: tuple[ExpectedTurn, ...]

    def __post_init__(self) -> None:
        if not self.turns:
            raise ValueError("scenario requires at least one turn")
        for position, turn in enumerate(self.turns, start=1):
            if turn.index != position:
                raise ValueError("turn indexes must be contiguous and one-based")
            expected_actor = AGENT if position % 2 else USER
            if turn.actor != expected_actor:
                raise ValueError(f"turn {position} must belong to {expected_actor}")
            if not turn.accepted_asr_forms:
                raise ValueError(f"turn {position} requires accepted ASR forms")
            if turn.deadline_ms <= 0:
                raise ValueError(f"turn {position} deadline must be positive")

    def public(self) -> dict[str, Any]:
        return {"name": self.name, "turns": [turn.public() for turn in self.turns]}


@dataclass(frozen=True)
class TurnObservation:
    actor: str
    token: str
    source: str
    observed_at_ms: float | None = None
    channel: str = "inbound"
    is_echo: bool = False
    classification: dict[str, Any] = field(default_factory=dict)

    def public(self) -> dict[str, Any]:
        return {
            "actor": self.actor,
            "token": self.token,
            "normalized_token": normalize_text(self.token),
            "source": self.source,
            "channel": self.channel,
            "is_echo": self.is_echo,
            "classification": dict(self.classification),
        }


class ScenarioIO(Protocol):
    async def perform_turn(self, turn: ExpectedTurn) -> list[TurnObservation]:
        """Produce observations by speaking or hearing the requested turn."""


class TurnTakingScenarioEngine:
    """Validate a sequential exchange while retaining actionable failure evidence."""

    def __init__(
        self,
        scenario: TurnTakingScenario,
        *,
        clock_ms: Callable[[], float] | None = None,
    ) -> None:
        self.scenario = scenario
        self._clock_ms = clock_ms or (lambda: time.perf_counter() * 1000.0)
        self._cursor = 0
        self._waiting_since_ms = self._clock_ms()
        self._rows = [
            {
                "expected": turn.public(),
                "observed": None,
                "latency_ms": None,
                "passed": False,
                "error_category": None,
                "coordinated_after_index": turn.index - 1 if turn.actor == AGENT and turn.index > 1 else None,
            }
            for turn in scenario.turns
        ]
        self._incidents: list[dict[str, Any]] = []
        self._echo_observations: list[dict[str, Any]] = []
        self._counts = {
            "drops": 0,
            "duplicates": 0,
            "misattributions": 0,
            "echoes": 0,
        }
        self._finished = False

    @property
    def next_turn(self) -> ExpectedTurn | None:
        if self._cursor >= len(self.scenario.turns):
            return None
        return self.scenario.turns[self._cursor]

    def observe(self, observation: TurnObservation) -> str:
        if self._finished:
            raise RuntimeError("scenario is already finished")
        now = observation.observed_at_ms
        if now is None:
            now = self._clock_ms()

        if observation.channel == "inbound" and observation.is_echo:
            self._counts["echoes"] += 1
            self._echo_observations.append(
                {"action": "rejected", "observation": observation.public(), "at_ms": round(now, 3)}
            )
            return "echo_rejected"

        current = self.next_turn
        if current is None:
            self._record_incident(ERROR_DUPLICATE, observation, None)
            return ERROR_DUPLICATE

        normalized = normalize_text(observation.token)
        prior_matches = [
            turn
            for turn in self.scenario.turns[: self._cursor]
            if normalized in {normalize_text(value) for value in turn.accepted_asr_forms}
        ]
        future_matches = [
            turn
            for turn in self.scenario.turns[self._cursor + 1 :]
            if normalized in {normalize_text(value) for value in turn.accepted_asr_forms}
        ]

        if observation.channel == "inbound" and any(turn.actor == AGENT for turn in prior_matches):
            self._counts["echoes"] += 1
            self._record_incident(ERROR_ECHO_LEAK, observation, current)
            return ERROR_ECHO_LEAK
        if prior_matches:
            self._counts["duplicates"] += 1
            self._record_incident(ERROR_DUPLICATE, observation, current)
            return ERROR_DUPLICATE
        if future_matches and not current.accepts(observation.token):
            self._record_incident(ERROR_OUT_OF_ORDER, observation, current)
            return ERROR_OUT_OF_ORDER
        if not current.accepts(observation.token):
            self._record_incident(ERROR_OUT_OF_ORDER, observation, current)
            return ERROR_OUT_OF_ORDER

        latency_ms = max(0.0, now - self._waiting_since_ms)
        category = None
        if observation.actor != current.actor:
            category = ERROR_WRONG_SPEAKER
            self._counts["misattributions"] += 1
        elif latency_ms > current.deadline_ms:
            category = ERROR_DEADLINE_EXCEEDED

        row = self._rows[self._cursor]
        row.update(
            observed=observation.public(),
            latency_ms=round(latency_ms, 3),
            passed=category is None,
            error_category=category,
        )
        if category is not None:
            self._incidents.append(
                {
                    "category": category,
                    "expected_index": current.index,
                    "expected_actor": current.actor,
                    "expected_token": current.spoken_token,
                    "observed": observation.public(),
                }
            )
        self._cursor += 1
        self._waiting_since_ms = now
        return category or "accepted"

    async def run(self, io: ScenarioIO) -> dict[str, Any]:
        while self.next_turn is not None:
            cursor_before = self._cursor
            observations = await io.perform_turn(self.next_turn)
            for observation in observations:
                self.observe(observation)
            if self._cursor == cursor_before:
                break
        return self.finish()

    def finish(self) -> dict[str, Any]:
        if not self._finished:
            while self._cursor < len(self.scenario.turns):
                turn = self.scenario.turns[self._cursor]
                row = self._rows[self._cursor]
                row["error_category"] = ERROR_MISSING
                self._counts["drops"] += 1
                self._incidents.append(
                    {
                        "category": ERROR_MISSING,
                        "expected_index": turn.index,
                        "expected_actor": turn.actor,
                        "expected_token": turn.spoken_token,
                        "observed": None,
                    }
                )
                self._cursor += 1
            self._finished = True

        latencies = [
            float(row["latency_ms"])
            for row in self._rows
            if row["latency_ms"] is not None
        ]
        aggregates = {
            "count": len(latencies),
            "p50_ms": _percentile(latencies, 50),
            "p95_ms": _percentile(latencies, 95),
            "max_ms": round(max(latencies), 3) if latencies else None,
        }
        passed = not self._incidents and all(row["passed"] for row in self._rows)
        return {
            "schema_version": 1,
            "scenario": self.scenario.name,
            "passed": passed,
            "turns": self._rows,
            "errors": self._incidents,
            "latency": aggregates,
            **self._counts,
            "echo_observations": self._echo_observations,
        }

    def _record_incident(
        self,
        category: str,
        observation: TurnObservation,
        current: ExpectedTurn | None,
    ) -> None:
        self._incidents.append(
            {
                "category": category,
                "expected_index": current.index if current else None,
                "expected_actor": current.actor if current else None,
                "expected_token": current.spoken_token if current else None,
                "observed": observation.public(),
            }
        )


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = (len(ordered) - 1) * percentile / 100.0
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return round(ordered[lower], 3)
    weight = rank - lower
    return round(ordered[lower] * (1.0 - weight) + ordered[upper] * weight, 3)


_COUNT_WORDS = (
    "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
    "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen",
    "eighteen", "nineteen", "twenty",
)
_COUNT_VARIANTS = {
    "one": ("won",),
    "two": ("to", "too"),
    "four": ("for",),
    "eight": ("ate",),
}

_LETTER_FORMS = {
    "A": ("a", "ay"),
    "B": ("b", "bee", "be"),
    "C": ("c", "cee", "see", "sea"),
    "D": ("d", "dee"),
    "E": ("e", "ee"),
    "F": ("f", "eff"),
    "G": ("g", "gee"),
    "H": ("h", "aitch"),
    "I": ("i", "eye"),
    "J": ("j", "jay"),
    "K": ("k", "kay"),
    "L": ("l", "el", "ell"),
    "M": ("m", "em"),
    "N": ("n", "en"),
    "O": ("o", "oh"),
    "P": ("p", "pee"),
    "Q": ("q", "cue", "queue"),
    "R": ("r", "are"),
    "S": ("s", "ess"),
    "T": ("t", "tea", "tee"),
    "U": ("u", "you", "yew"),
    "V": ("v", "vee"),
    "W": ("w", "double u", "double you"),
    "X": ("x", "ex"),
    "Y": ("y", "why"),
    "Z": ("z", "zee", "zed"),
}


def counting_scenario(*, deadline_ms: float = 2000.0) -> TurnTakingScenario:
    turns = []
    for index, word in enumerate(_COUNT_WORDS, start=1):
        forms = (word, str(index), *_COUNT_VARIANTS.get(word, ()))
        turns.append(
            ExpectedTurn(
                index=index,
                actor=AGENT if index % 2 else USER,
                spoken_token=word,
                accepted_asr_forms=forms,
                deadline_ms=deadline_ms,
            )
        )
    return TurnTakingScenario(name="count-1-to-20", turns=tuple(turns))


def alphabet_scenario(*, deadline_ms: float = 2000.0) -> TurnTakingScenario:
    turns = [
        ExpectedTurn(
            index=index,
            actor=AGENT if index % 2 else USER,
            spoken_token=letter,
            accepted_asr_forms=_LETTER_FORMS[letter],
            deadline_ms=deadline_ms,
        )
        for index, letter in enumerate(_LETTER_FORMS, start=1)
    ]
    return TurnTakingScenario(name="alphabet-a-to-z", turns=tuple(turns))


class DeterministicProductionScenarioIO:
    """Hardware-free bridge/HTTP/PCM/VAD/STT integration with only CDP and STT simulated."""

    def __init__(
        self,
        service: Any,
        *,
        meeting_url: str = "https://meet.google.com/sim-ulat-edt",
        agent_id: str = "realtime-test-agent",
        inject_agent_echo: bool = True,
        clock_ms: Callable[[], float] | None = None,
        browser_ingest: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        self.service = service
        self.meeting_url = meeting_url
        self.agent_id = agent_id
        self.inject_agent_echo = inject_agent_echo
        self._clock_ms = clock_ms or (lambda: time.perf_counter() * 1000.0)
        self.played: list[dict[str, Any]] = []
        self.timeline: list[dict[str, Any]] = []
        self._browser_ingest = browser_ingest or service.ingest_companion_browser_audio
        self._segment_results: queue.Queue[dict[str, Any]] = queue.Queue()
        self._accepted_condition = threading.Condition()
        self._accepted_ids: set[str] = set()
        self._original_health = service._meet_bridge_health
        self._original_speech = service._meet_bridge_speech
        self._original_engines = service.stt_engines
        self._original_segment_handler = service.secondary_capture._on_segment
        service.stt_engines = [_DeterministicToneStt()]

        async def capture_result(segment):
            result = await self._original_segment_handler(segment)
            self._segment_results.put(result)
            return result

        service.secondary_capture._on_segment = capture_result
        ready = {
            "ready": True,
            "meetingUrl": meeting_url,
            "tabId": "fake-companion-tab",
            "state": "in-call",
            "syntheticMicReady": True,
        }
        self.arbiter = CompanionAudioArbiter(
            lambda target: {
                **ready,
                "ready": target in (None, meeting_url),
                "error": None if target in (None, meeting_url) else "meeting mismatch",
            },
            self._playback,
            lambda _reason: None,
        )
        self.arbiter.start()
        service._meet_bridge_health = lambda timeout=0.5: {
            "meetingUrl": meeting_url,
            "companionAudio": {
                "companionReady": True,
                "lastError": None,
                "syntheticMicReady": True,
            },
        }
        service._meet_bridge_speech = self._submit_companion

    def close(self) -> None:
        self.arbiter.stop()
        self.service.secondary_capture.stop()
        self.service._meet_bridge_health = self._original_health
        self.service._meet_bridge_speech = self._original_speech
        self.service.stt_engines = self._original_engines
        self.service.secondary_capture._on_segment = self._original_segment_handler

    async def perform_turn(self, turn: ExpectedTurn) -> list[TurnObservation]:
        if turn.actor == AGENT:
            return await self._perform_agent_turn(turn)
        return [await self._perform_user_turn(turn)]

    async def _perform_agent_turn(self, turn: ExpectedTurn) -> list[TurnObservation]:
        self.service.secondary_capture.bind_loop(__import__("asyncio").get_running_loop())
        queued = self.service.speak(
            self.agent_id,
            turn.spoken_token,
            destination="companion",
            meeting_url=self.meeting_url,
            correlation_id=f"{self.meeting_url}:{turn.index}",
        )
        if queued.get("duplicate"):
            raise RuntimeError(f"production TTS rejected turn {turn.index} as duplicate")
        if self.service.tts._running:
            accepted = await __import__("asyncio").to_thread(
                self._wait_for_acceptance, queued["id"], turn.deadline_ms / 1000.0
            )
            if not accepted:
                raise RuntimeError(f"production TTS did not hand off turn {turn.index}")
            remote = await __import__("asyncio").to_thread(
                self.arbiter.utterance_status,
                queued["id"],
                wait_seconds=turn.deadline_ms / 1000.0,
            )
            if remote.get("state") != "completed":
                raise RuntimeError(
                    str(remote.get("error") or f"companion playback {remote.get('state')}")
                )
        elif not await self.service.tts.process_next():
            raise RuntimeError(f"production TTS did not process turn {turn.index}")
        item = next(played for played in self.played if played["id"] == queued["id"])
        observations = [
            TurnObservation(
                actor=AGENT,
                token=item["text"],
                source=item["source"],
                observed_at_ms=self._clock_ms(),
                channel="outbound",
                classification={"destination": "companion", "utterance_id": item["id"]},
            )
        ]
        self.timeline.append({"kind": "agent_outbound", "index": turn.index, "token": item["text"]})
        if self.inject_agent_echo:
            suppression = await self._ingest_tone(
                item["text"],
                artifact={
                    "id": item["id"],
                    "source": item["source"],
                    "agentId": self.agent_id,
                    "expectedText": item["text"],
                },
            )
            if suppression.get("artifactChunksSuppressed") != 1:
                raise RuntimeError("bridge capture-time echo suppression did not reject agent playback")
            observations.append(
                TurnObservation(
                    actor=AGENT,
                    token=item["text"],
                    source="companion_heard_capture_suppressed",
                    observed_at_ms=self._clock_ms(),
                    channel="inbound",
                    is_echo=True,
                    classification={
                        "is_echo": True,
                        "source": "virtual_agent",
                        "suppression": suppression,
                    },
                )
            )
            self.timeline.append({"kind": "echo_rejected", "index": turn.index, "token": item["text"]})
        return observations

    async def _perform_user_turn(self, turn: ExpectedTurn) -> TurnObservation:
        self.service.secondary_capture.bind_loop(__import__("asyncio").get_running_loop())
        await self._ingest_tone(turn.spoken_token)
        result = await __import__("asyncio").to_thread(
            self._segment_results.get, True, turn.deadline_ms / 1000.0
        )
        classification = result["classification"]
        actor = USER if classification["source"] == "external" else classification["source"]
        self.timeline.append({"kind": "user_inbound", "index": turn.index, "token": turn.spoken_token})
        return TurnObservation(
            actor=actor,
            token=result["resolved"]["resolved_text"],
            source="companion_heard",
            observed_at_ms=self._clock_ms(),
            channel="inbound",
            is_echo=bool(classification["is_echo"]),
            classification=classification,
        )

    async def _ingest_tone(
        self, text: str, *, artifact: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        import numpy as np

        rate = 16000
        frequency = _tone_frequency(text)
        tone_frames = int(rate * 0.35)
        silence_frames = int(rate * 0.7)
        samples = np.concatenate(
            [
                np.sin(2 * np.pi * frequency * np.arange(tone_frames) / rate) * 0.35,
                np.zeros(silence_frames),
            ]
        ).astype("float32")
        raw = (samples * 32767).astype("<i2").tobytes()
        now = time.time()
        chunk = {
            "pcm_s16le_base64": base64.b64encode(raw).decode("ascii"),
            "frames": int(samples.size),
            "bytes": len(raw),
            "capturedAt": now * 1000.0,
        }

        class FakeCdpAudioTab:
            def evaluate(self, *_args, **_kwargs):
                return json.dumps({
                    "ok": True,
                    "connected": True,
                    "muted": True,
                    "streamId": "deterministic-fake-cdp-remote-media",
                    "sampleRate": rate,
                    "chunks": [chunk],
                })

        class HttpMailbox:
            def __init__(self, ingest):
                self.ingest = ingest

            def ingest_companion_browser_audio(self, payload):
                return self.ingest(payload)

        holder = {"companion_heard_stt_enabled": True}
        if artifact is not None:
            holder.update({
                "companion_say_artifact_started_at": now - 0.1,
                "companion_say_artifact_until": now + 0.5,
                "companion_say_artifact": artifact,
            })
        return forward_companion_heard_audio(
            FakeCdpAudioTab(), HttpMailbox(self._browser_ingest), holder, {}
        )

    def _playback(self, item: dict[str, Any], _cancel: Any) -> None:
        self.timeline.append({"kind": "bridge_playback_started", "id": item["id"]})
        if _cancel.wait(0.01):
            return
        self.played.append(dict(item))
        self.timeline.append({"kind": "bridge_playback_completed", "id": item["id"]})

    def _submit_companion(
        self,
        payload: dict[str, Any],
        timeout: float = 2.0,
        *,
        path: str = "/speech",
    ) -> dict[str, Any]:
        del timeout
        if path == "/speech/status":
            return self.arbiter.utterance_status(
                str(payload.get("utterance_id") or ""),
                wait_seconds=float(payload.get("wait_seconds") or 0.0),
            )
        if path == "/speech/cancel":
            utterance_id = str(payload.get("utterance_id") or "")
            return {"ok": True, "cancelled": self.arbiter.cancel(utterance_id)}
        if path != "/speech":
            return {"ok": False, "accepted": False, "error": f"unsupported fake path {path}"}
        result = self.arbiter.submit(
            kind="speech",
            text=payload.get("text", ""),
            meeting_url=payload.get("meeting_url"),
            source=payload.get("artifact_source", "virtual-agent-tts"),
            metadata={
                "utterance_id": payload.get("utterance_id"),
                "agent_id": payload.get("agent_id"),
                "correlation_id": payload.get("correlation_id"),
                "expected_text": payload.get("text"),
            },
        )
        if result.get("accepted"):
            with self._accepted_condition:
                self._accepted_ids.add(result["id"])
                self._accepted_condition.notify_all()
            self.timeline.append({"kind": "bridge_queue_accepted", "id": result["id"]})
        return result

    def _wait_for_acceptance(self, utterance_id: str, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        with self._accepted_condition:
            while utterance_id not in self._accepted_ids:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._accepted_condition.wait(remaining)
            return True


_TONE_TOKENS = (*_COUNT_WORDS, *_LETTER_FORMS.keys())
_TONE_FREQUENCIES = {
    normalize_text(token): 300.0 + index * 25.0
    for index, token in enumerate(_TONE_TOKENS)
}


def _tone_frequency(text: str) -> float:
    return _TONE_FREQUENCIES[normalize_text(text)]


class _DeterministicToneStt:
    """Simulated STT boundary: decode deterministic frequencies after real PCM/VAD."""

    name = "deterministic-tone-stt"
    model = "known-tone-fixtures"
    is_remote = False

    async def transcribe(self, segment, on_partial=None):
        import numpy as np

        samples = np.asarray(segment.samples, dtype="float32")
        spectrum = np.abs(np.fft.rfft(samples))
        frequencies = np.fft.rfftfreq(samples.size, 1.0 / segment.sample_rate)
        peak = float(frequencies[int(np.argmax(spectrum))])
        token = min(
            _TONE_FREQUENCIES,
            key=lambda value: abs(_TONE_FREQUENCIES[value] - peak),
        )
        return Hypothesis(
            engine=self.name,
            model=self.model,
            raw_text=token,
            normalized_text=token,
            confidence=1.0,
        )
