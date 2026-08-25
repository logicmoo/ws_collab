"""Text-to-speech output engine (task sections 10 and 15).

A single fair, priority-ordered queue serialises speech so agent identity is
preserved and the wrong voice is never used. It supports per-agent and global
pause/mute, cancel, interruption by priority, and duplicate suppression. The
engine exposes its live playback state (``is_speaking`` and the expected text of
active utterances) so the capture/echo layer can avoid treating the system's own
speech as an operator command. The default backend is a hardware-free simulator;
a Windows SAPI backend is used when available.
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..config import Config
from ..events import (
    AGENT_SPEECH_STARTED,
    STREAM_TTS,
    TTS_CANCELLED,
    TTS_FINISHED,
    TTS_STARTED,
)
from ..ids import new_event_id
from ..stt.base import normalize_text

PublishFn = Callable[..., dict[str, Any]]


@dataclass(order=True)
class TtsItem:
    sort_index: tuple[int, float] = field(init=False)
    priority: int = 5
    enqueued_at: float = field(default_factory=time.time)
    id: str = field(default_factory=new_event_id, compare=False)
    agent_id: str = field(default="agent", compare=False)
    text: str = field(default="", compare=False)
    voice_id: str = field(default="fake:aria", compare=False)
    requested_voice_id: str = field(default="", compare=False)
    rate: float = field(default=1.0, compare=False)
    pitch: float = field(default=0.0, compare=False)
    volume: float = field(default=1.0, compare=False)
    device: str = field(default="default", compare=False)
    correlation_id: str | None = field(default=None, compare=False)
    interrupt: bool = field(default=False, compare=False)
    cancelled: bool = field(default=False, compare=False)

    def __post_init__(self) -> None:
        # Lower priority number = spoken sooner; ties broken by FIFO.
        self.sort_index = (self.priority, self.enqueued_at)

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "agent_id": self.agent_id,
            "text": self.text,
            "voice_id": self.voice_id,
            "requested_voice_id": self.requested_voice_id,
            "priority": self.priority,
            "rate": self.rate,
            "pitch": self.pitch,
            "device": self.device,
            "correlation_id": self.correlation_id,
            "cancelled": self.cancelled,
        }


class _InlineFakeBackend:
    """Ultimate fallback if no TTS driver is discovered."""

    name = "fake"

    async def play(self, item: "TtsItem") -> float:
        duration = min(6.0, max(0.3, len(item.text) * 0.045 / max(0.25, item.rate)))
        await asyncio.sleep(duration)
        return duration


def build_backend(config: Config):
    """Select a TTS backend from discovered drivers (skips ``*_disabled`` dirs).

    ``auto`` (the default) prefers a real platform backend and falls back to the
    simulator, so speech output always works.
    """

    from ..drivers import DriverUnavailable, discover_tts_drivers

    specs, _notes = discover_tts_drivers()
    requested = (config.tts_backend or "auto").lower()
    order: list[str] = []
    if requested == "auto":
        order = ["sapi", "fake"]
    else:
        order = [requested, "fake"]

    for wanted in order:
        spec = next((s for s in specs if s.id == wanted), None)
        if spec is None:
            continue
        try:
            return spec.build(config)
        except DriverUnavailable:
            continue
    return _InlineFakeBackend()


class TtsEngine:
    def __init__(self, config: Config, publish: PublishFn, backend=None, dedupe_window_s: float = 2.0):
        self.config = config
        self._publish = publish
        self._backend = backend or build_backend(config)
        self._pending: list[TtsItem] = []
        self._lock = threading.RLock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wakeup: asyncio.Event | None = None
        self._running = False
        self._task: asyncio.Task | None = None
        self._current: TtsItem | None = None
        self._paused_global = False
        self._muted_agents: set[str] = set()
        self._paused_agents: set[str] = set()
        self._recent: dict[str, float] = {}
        self._dedupe_window_s = dedupe_window_s

    # ---------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._wakeup = asyncio.Event()
        self._running = True
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._running = False
        if self._wakeup is not None:
            self._wakeup.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    # ------------------------------------------------------------------ enqueue
    def speak(
        self,
        agent_id: str,
        text: str,
        *,
        voice_id: str,
        requested_voice_id: str = "",
        rate: float = 1.0,
        pitch: float = 0.0,
        volume: float = 1.0,
        device: str = "default",
        priority: int = 5,
        correlation_id: str | None = None,
        interrupt: bool = False,
        dedupe: bool = True,
    ) -> dict[str, Any]:
        text = (text or "").strip()
        if not text:
            from ..errors import ValidationError

            raise ValidationError("tts text is required")
        max_chars = self.config.max_body_bytes
        if len(text) > max_chars:
            from ..errors import PayloadTooLargeError

            raise PayloadTooLargeError("tts text too long")
        with self._lock:
            if dedupe and self._is_duplicate(agent_id, text):
                return {"duplicate": True, "id": None}
            item = TtsItem(
                priority=priority,
                agent_id=agent_id,
                text=text,
                voice_id=voice_id,
                requested_voice_id=requested_voice_id or voice_id,
                rate=rate,
                pitch=pitch,
                volume=volume,
                device=device,
                correlation_id=correlation_id,
                interrupt=interrupt,
            )
            self._pending.append(item)
            self._pending.sort()
            self._recent[self._dedupe_key(agent_id, text)] = time.time()
            queue_position = [p.id for p in self._pending].index(item.id)
        if interrupt and self._current is not None:
            self.cancel(self._current.id)
        self._signal()
        return {"duplicate": False, "id": item.id, "queue_position": queue_position}

    def _dedupe_key(self, agent_id: str, text: str) -> str:
        return f"{agent_id}\x1f{normalize_text(text)}"

    def _is_duplicate(self, agent_id: str, text: str) -> bool:
        key = self._dedupe_key(agent_id, text)
        now = time.time()
        self._recent = {k: t for k, t in self._recent.items() if now - t < self._dedupe_window_s}
        if key in self._recent:
            return True
        for item in self._pending:
            if not item.cancelled and self._dedupe_key(item.agent_id, item.text) == key:
                return True
        return False

    def _signal(self) -> None:
        if self._loop is not None and self._wakeup is not None:
            try:
                self._loop.call_soon_threadsafe(self._wakeup.set)
            except RuntimeError:
                pass

    # ------------------------------------------------------------------ control
    def cancel(self, item_id: str) -> bool:
        with self._lock:
            for item in self._pending:
                if item.id == item_id:
                    item.cancelled = True
                    self._publish(
                        stream=STREAM_TTS, type=TTS_CANCELLED,
                        data={"id": item_id, "agent_id": item.agent_id}, source_id=item.agent_id, source_kind="agent",
                    )
                    return True
            if self._current is not None and self._current.id == item_id:
                self._current.cancelled = True
                return True
        return False

    def cancel_agent(self, agent_id: str) -> int:
        count = 0
        with self._lock:
            for item in self._pending:
                if item.agent_id == agent_id and not item.cancelled:
                    item.cancelled = True
                    count += 1
        return count

    def pause(self, agent_id: str | None = None) -> None:
        with self._lock:
            if agent_id is None:
                self._paused_global = True
            else:
                self._paused_agents.add(agent_id)

    def resume(self, agent_id: str | None = None) -> None:
        with self._lock:
            if agent_id is None:
                self._paused_global = False
            else:
                self._paused_agents.discard(agent_id)
        self._signal()

    def mute(self, agent_id: str) -> None:
        with self._lock:
            self._muted_agents.add(agent_id)

    def unmute(self, agent_id: str) -> None:
        with self._lock:
            self._muted_agents.discard(agent_id)
        self._signal()

    # -------------------------------------------------------------------- state
    def state(self) -> dict[str, Any]:
        with self._lock:
            return {
                "is_speaking": self._current is not None,
                "current": self._current.public() if self._current else None,
                "queue": [item.public() for item in self._pending if not item.cancelled],
                "paused_global": self._paused_global,
                "paused_agents": sorted(self._paused_agents),
                "muted_agents": sorted(self._muted_agents),
                "backend": self._backend.name,
            }

    def active_expected_texts(self) -> list[dict[str, Any]]:
        """Live playbacks so the echo layer can recognise the system's own speech."""

        with self._lock:
            if self._current is None:
                return []
            return [{
                "tts_event_id": self._current.id,
                "agent_id": self._current.agent_id,
                "expected_text": self._current.text,
                "voice_id": self._current.voice_id,
            }]

    @property
    def is_speaking(self) -> bool:
        return self._current is not None

    # ------------------------------------------------------------------- worker
    def _next_item(self) -> TtsItem | None:
        with self._lock:
            if self._paused_global:
                return None
            for item in list(self._pending):
                if item.cancelled:
                    self._pending.remove(item)
                    continue
                if item.agent_id in self._muted_agents:
                    self._pending.remove(item)  # muted: drop silently
                    continue
                if item.agent_id in self._paused_agents:
                    continue
                self._pending.remove(item)
                return item
            return None

    async def process_next(self) -> bool:
        """Process exactly one eligible item. Returns True if something played."""

        item = self._next_item()
        if item is None:
            return False
        with self._lock:
            self._current = item
        self._publish(
            stream=STREAM_TTS, type=TTS_STARTED,
            data={**item.public(), "backend": self._backend.name},
            source_id=item.agent_id, source_kind="agent", correlation_id=item.correlation_id,
        )
        self._publish(
            stream=STREAM_TTS, type=AGENT_SPEECH_STARTED,
            data={"agent_id": item.agent_id, "text": item.text, "voice_id": item.voice_id, "tts_event_id": item.id},
            source_id=item.agent_id, source_kind="agent", correlation_id=item.correlation_id,
        )
        error = None
        duration = 0.0
        try:
            if not item.cancelled:
                duration = await self._backend.play(item)
        except Exception as exc:  # noqa: BLE001
            error = str(exc)
        finally:
            with self._lock:
                cancelled = item.cancelled
                self._current = None
        self._publish(
            stream=STREAM_TTS,
            type=TTS_CANCELLED if cancelled else TTS_FINISHED,
            data={"id": item.id, "agent_id": item.agent_id, "duration_s": round(duration, 3), "error": error},
            source_id=item.agent_id, source_kind="agent", correlation_id=item.correlation_id,
        )
        return True

    async def _run(self) -> None:
        assert self._wakeup is not None
        while self._running:
            played = await self.process_next()
            if played:
                continue
            self._wakeup.clear()
            try:
                await asyncio.wait_for(self._wakeup.wait(), timeout=0.5)
            except asyncio.TimeoutError:
                pass
