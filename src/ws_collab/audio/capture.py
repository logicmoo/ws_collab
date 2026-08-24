"""Always-listening audio capture service (task section 10).

This is a real, event-driven server audio service -- never a self-relaunch or a
shell polling loop. It must be explicitly enabled.

Two input paths feed the *same* pipeline:

* **Real capture** (``sounddevice``): a PortAudio callback pushes frames onto a
  bounded queue; a worker thread runs energy VAD over them, keeps a rolling
  pre-roll buffer so the start of a word is never clipped, and emits a segment
  on end-of-utterance silence. Audio is downmixed to mono and resampled to the
  STT rate. Memory is bounded at every stage.
* **Injected utterances**: whole utterances supplied by the admin page or tests.

The service honours the echo policy so it does not treat the system's own TTS as
an operator command, exposes an input meter and a privacy (listening) indicator,
and recovers from device hot-plug.
"""

from __future__ import annotations

import asyncio
import queue
import threading
from typing import Any, Awaitable, Callable

from ..config import Config
from ..events import (
    LISTENING_STARTED,
    LISTENING_STOPPED,
    SPEECH_DETECTED,
    STREAM_TRANSLATED_AUDIO,
)
from .devices import DeviceRegistry
from .segment import AudioSegment
from .vad import SimpleVad, frame_rms

SegmentHandler = Callable[[AudioSegment], Awaitable[None]]
PublishFn = Callable[..., dict[str, Any]]

STT_SAMPLE_RATE = 16000
FRAME_MS = 20
PRE_ROLL_MS = 320
MAX_UTTERANCE_MS = 30000
_MAX_QUEUED_FRAMES = 512  # bounded: ~10s at 20ms, then the oldest frame is dropped


class CaptureService:
    def __init__(
        self,
        config: Config,
        devices: DeviceRegistry,
        publish: PublishFn,
        on_segment: SegmentHandler,
        is_tts_speaking: Callable[[], bool] | None = None,
    ):
        self.config = config
        self.devices = devices
        self._publish = publish
        self._on_segment = on_segment
        self._is_tts_speaking = is_tts_speaking or (lambda: False)
        self._listening = False
        self._lock = threading.RLock()
        default_input = devices.default_input()
        self._device_id = config.audio_input_device or (default_input.id if default_input else "")
        self._meter_level = 0.0
        self._peak_level = 0.0
        self._clipping = False
        self._captured = 0
        self._dropped_echo = 0
        self._dropped_frames = 0
        self._stream_error: str | None = None

        # Real-capture machinery.
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stream = None
        self._frames: queue.Queue = queue.Queue(maxsize=_MAX_QUEUED_FRAMES)
        self._worker: threading.Thread | None = None
        self._stop_worker = threading.Event()

    # ---------------------------------------------------------------- lifecycle
    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Remember the server loop so capture threads can dispatch onto it."""

        self._loop = loop

    def start(self, *, device_id: str | None = None) -> dict[str, Any]:
        if not self.config.audio_enabled:
            from ..errors import ConflictError

            raise ConflictError(
                "always-listening capture is disabled; set WS_COLLAB_AUDIO_ENABLED=1 to enable it"
            )
        with self._lock:
            if device_id:
                self._device_id = device_id
            already = self._listening
            self._listening = True
            self._stream_error = None
        if already:
            self._stop_stream()

        device = self.devices.get(self._device_id)
        started_real = False
        if device is not None and device.backend == "sounddevice":
            started_real = self._start_stream(device)

        self._publish(
            stream=STREAM_TRANSLATED_AUDIO, type=LISTENING_STARTED,
            data={
                "device_id": self._device_id,
                "device_name": device.name if device else None,
                "backend": device.backend if device else self.devices.backend,
                "live_capture": started_real,
                "echo_policy": self.config.echo_policy,
                "error": self._stream_error,
            },
            source_id="capture", source_kind="system",
        )
        return self.state()

    def stop(self) -> dict[str, Any]:
        with self._lock:
            self._listening = False
        self._stop_stream()
        self._publish(
            stream=STREAM_TRANSLATED_AUDIO, type=LISTENING_STOPPED,
            data={"device_id": self._device_id}, source_id="capture", source_kind="system",
        )
        return self.state()

    # ------------------------------------------------------------------- state
    @property
    def listening(self) -> bool:
        return self._listening

    @property
    def live_capture(self) -> bool:
        return self._stream is not None

    def state(self) -> dict[str, Any]:
        with self._lock:
            device = self.devices.get(self._device_id)
            return {
                "listening": self._listening,
                "privacy_indicator": "LISTENING" if self._listening else "muted",
                "device_id": self._device_id,
                "device_name": device.name if device else None,
                "backend": device.backend if device else self.devices.backend,
                "live_capture": self._stream is not None,
                "echo_policy": self.config.echo_policy,
                "meter_level": round(self._meter_level, 4),
                "peak_level": round(self._peak_level, 4),
                "clipping": self._clipping,
                "captured": self._captured,
                "dropped_echo": self._dropped_echo,
                "dropped_frames": self._dropped_frames,
                "error": self._stream_error,
            }

    def select_device(self, device_id: str) -> dict[str, Any]:
        device = self.devices.get(device_id)
        if device is None:
            from ..errors import NotFoundError

            raise NotFoundError(f"unknown device: {device_id}")
        was_listening = self._listening
        if was_listening:
            self.stop()
        with self._lock:
            self._device_id = device_id
        if was_listening:
            self.start()
        return self.state()

    def recover_hotplug(self) -> dict[str, Any]:
        """If the active input vanished, fall back to the default input."""

        with self._lock:
            missing = self.devices.get(self._device_id) is None
        if missing:
            fallback = self.devices.default_input()
            if fallback is not None:
                was_listening = self._listening
                self._stop_stream()
                with self._lock:
                    self._device_id = fallback.id
                self._publish(
                    stream=STREAM_TRANSLATED_AUDIO, type="INPUT_DEVICE_RECOVERED",
                    data={"device_id": self._device_id, "device_name": fallback.name},
                    source_id="capture", source_kind="system",
                )
                if was_listening:
                    self.start()
        return self.state()

    # --------------------------------------------------------------- real input
    def _start_stream(self, device) -> bool:
        try:
            import numpy  # noqa: F401
            import sounddevice as sd
        except Exception as error:  # pragma: no cover - depends on the platform
            self._stream_error = f"sounddevice unavailable: {error}"
            return False

        rate = STT_SAMPLE_RATE if STT_SAMPLE_RATE in device.sample_rates else device.sample_rates[-1]
        channels = min(device.channels, 2) or 1
        blocksize = max(1, int(rate * FRAME_MS / 1000))

        def callback(indata, _frames, _time_info, status):  # pragma: no cover - realtime
            if status:
                self._stream_error = str(status)
            try:
                self._frames.put_nowait(indata.copy())
            except queue.Full:
                # Bounded memory: drop the oldest frame rather than grow forever.
                try:
                    self._frames.get_nowait()
                    self._frames.put_nowait(indata.copy())
                except queue.Empty:
                    pass
                self._dropped_frames += 1

        try:
            stream = sd.InputStream(
                device=device.backend_index,
                channels=channels,
                samplerate=rate,
                blocksize=blocksize,
                dtype="float32",
                callback=callback,
            )
            stream.start()
        except Exception as error:  # pragma: no cover - depends on hardware
            self._stream_error = f"could not open {device.name}: {error}"
            return False

        self._stream = stream
        self._stop_worker.clear()
        self._worker = threading.Thread(
            target=self._segment_worker, args=(rate,), name="ws_collab_capture", daemon=True
        )
        self._worker.start()
        return True

    def _stop_stream(self) -> None:
        self._stop_worker.set()
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
        worker, self._worker = self._worker, None
        if worker is not None and worker.is_alive():
            worker.join(timeout=2.0)
        while not self._frames.empty():
            try:
                self._frames.get_nowait()
            except queue.Empty:
                break

    def _segment_worker(self, rate: int) -> None:  # pragma: no cover - realtime
        """Run VAD over live frames and emit one segment per utterance."""

        import numpy as np

        vad = SimpleVad(self.config.vad_threshold, self.config.vad_silence_ms, frame_ms=FRAME_MS)
        pre_roll_frames = max(1, PRE_ROLL_MS // FRAME_MS)
        max_frames = max(1, MAX_UTTERANCE_MS // FRAME_MS)
        pre_roll: list[Any] = []
        collecting: list[Any] = []
        in_speech = False

        while not self._stop_worker.is_set():
            try:
                frame = self._frames.get(timeout=0.2)
            except queue.Empty:
                continue

            mono = frame.mean(axis=1) if frame.ndim > 1 else frame
            level = frame_rms(mono)
            with self._lock:
                self._meter_level = level
                self._peak_level = max(self._peak_level * 0.995, level)
                self._clipping = bool(np.max(np.abs(mono)) >= 0.99) if mono.size else False

            event = vad.process(mono)
            if event == "speech_start":
                in_speech = True
                collecting = list(pre_roll)  # keep the audio just before the trigger
            if in_speech:
                collecting.append(mono)
                if len(collecting) >= max_frames:
                    event = "speech_end"
            else:
                pre_roll.append(mono)
                if len(pre_roll) > pre_roll_frames:
                    pre_roll.pop(0)

            if event == "speech_end" and in_speech:
                in_speech = False
                audio = np.concatenate(collecting) if collecting else np.zeros(0, dtype="float32")
                collecting = []
                pre_roll = []
                vad.state.in_speech = False
                if audio.size:
                    self._dispatch_live_segment(audio, rate)

    def _dispatch_live_segment(self, audio, rate: int) -> None:  # pragma: no cover - realtime
        """Hand a finished utterance to the async pipeline from the worker thread."""

        import numpy as np

        # Echo policy: ignore live input while the system is speaking.
        if self.config.echo_policy == "mute_input_during_tts" and self._is_tts_speaking():
            with self._lock:
                self._dropped_echo += 1
            return

        if rate != STT_SAMPLE_RATE and audio.size:
            target = int(round(audio.size * STT_SAMPLE_RATE / rate))
            if target > 0:
                positions = np.linspace(0, audio.size - 1, target)
                audio = np.interp(positions, np.arange(audio.size), audio).astype("float32")

        from ..ids import new_event_id

        correlation_id = new_event_id()
        duration_ms = int(audio.size * 1000 / STT_SAMPLE_RATE)
        speaking = self._is_tts_speaking()
        segment = AudioSegment(
            correlation_id=correlation_id,
            device_id=self._device_id,
            sample_rate=STT_SAMPLE_RATE,
            channels=1,
            samples=audio,
            reference_text=None,  # real audio: a real recognizer must decode it
            source_kind="unknown" if speaking else "operator",
            duration_ms=duration_ms,
        )
        with self._lock:
            self._captured += 1
            level = self._meter_level
        self._publish(
            stream=STREAM_TRANSLATED_AUDIO, type=SPEECH_DETECTED,
            data={
                "segment_id": segment.id, "device_id": segment.device_id, "live": True,
                "duration_ms": duration_ms, "level": round(level, 4),
            },
            source_id="capture", source_kind="system", correlation_id=correlation_id,
        )
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        asyncio.run_coroutine_threadsafe(self._on_segment(segment), loop)

    # --------------------------------------------------------- injected input
    async def inject_utterance(
        self,
        text: str,
        *,
        source_kind: str = "operator",
        correlation_id: str | None = None,
        device_id: str | None = None,
        is_loopback: bool = False,
        is_diagnostic: bool = False,
        expected_tts_text: str | None = None,
        tts_event_id: str | None = None,
        level: float = 0.35,
    ) -> AudioSegment | None:
        """Inject a whole synthetic utterance and run it through the pipeline."""

        if not self._listening:
            from ..errors import ConflictError

            raise ConflictError("capture is not listening; call start() first")

        if (
            self.config.echo_policy == "mute_input_during_tts"
            and self._is_tts_speaking()
            and expected_tts_text is None
            and not is_loopback
        ):
            with self._lock:
                self._dropped_echo += 1
            self._publish(
                stream=STREAM_TRANSLATED_AUDIO, type="INPUT_MUTED_DURING_TTS",
                data={"reason": "mute_input_during_tts policy active"}, source_id="capture", source_kind="system",
            )
            return None

        from ..ids import new_event_id

        correlation_id = correlation_id or new_event_id()
        segment = AudioSegment(
            correlation_id=correlation_id,
            device_id=device_id or self._device_id,
            reference_text=text,
            source_kind=source_kind,
            is_loopback=is_loopback,
            is_diagnostic=is_diagnostic,
            expected_tts_text=expected_tts_text,
            tts_event_id=tts_event_id,
            duration_ms=int(max(300, len(text) * 60)),
        )
        with self._lock:
            self._meter_level = level
            self._captured += 1
        self._publish(
            stream=STREAM_TRANSLATED_AUDIO, type=SPEECH_DETECTED,
            data={"segment_id": segment.id, "device_id": segment.device_id,
                  "source_kind": source_kind, "level": level, "live": False},
            source_id="capture", source_kind="system", correlation_id=correlation_id,
        )
        await self._on_segment(segment)
        return segment
