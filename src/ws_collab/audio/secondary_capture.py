"""Independent secondary audio capture for Meet companion incoming audio."""

from __future__ import annotations

import asyncio
import queue
import threading
from typing import Any, Awaitable, Callable

from ..config import Config
from ..events import LISTENING_STARTED, LISTENING_STOPPED, SPEECH_DETECTED, STREAM_TRANSLATED_AUDIO
from ..ids import new_event_id
from .devices import DeviceRegistry
from .segment import AudioSegment
from .vad import SimpleVad, frame_rms

SegmentHandler = Callable[[AudioSegment], Awaitable[None]]
PublishFn = Callable[..., dict[str, Any]]

STT_SAMPLE_RATE = 16000
FRAME_MS = 20
PRE_ROLL_MS = 320
MAX_UTTERANCE_MS = 30000
_MAX_QUEUED_FRAMES = 512


class SecondaryCaptureService:
    def __init__(self, config: Config, devices: DeviceRegistry, publish: PublishFn, on_segment: SegmentHandler):
        self.config = config
        self.devices = devices
        self._publish = publish
        self._on_segment = on_segment
        self._listening = False
        self._lock = threading.RLock()
        self._device_id = ""
        self._meter_level = 0.0
        self._peak_level = 0.0
        self._clipping = False
        self._captured = 0
        self._dropped_frames = 0
        self._stream_error: str | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stream = None
        self._frames: queue.Queue = queue.Queue(maxsize=_MAX_QUEUED_FRAMES)
        self._worker: threading.Thread | None = None
        self._stop_worker = threading.Event()
        self._vad: SimpleVad | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def start(self, *, device_id: str) -> dict[str, Any]:
        if not self.config.audio_enabled:
            from ..errors import ConflictError
            raise ConflictError("secondary capture is disabled; set WS_COLLAB_AUDIO_ENABLED=1 to enable it")
        with self._lock:
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
            data={"device_id": self._device_id, "device_name": device.name if device else None, "backend": device.backend if device else self.devices.backend, "live_capture": started_real, "secondary": True},
            source_id="meet-companion-incoming", source_kind="system",
        )
        return self.state()

    def stop(self) -> dict[str, Any]:
        with self._lock:
            self._listening = False
        self._stop_stream()
        self._publish(
            stream=STREAM_TRANSLATED_AUDIO, type=LISTENING_STOPPED,
            data={"device_id": self._device_id, "secondary": True}, source_id="meet-companion-incoming", source_kind="system",
        )
        return self.state()

    def state(self) -> dict[str, Any]:
        with self._lock:
            device = self.devices.get(self._device_id)
            vad = self._vad
            return {
                "listening": self._listening,
                "source_id": "meet-companion-incoming",
                "source_kind": "companion_heard",
                "audio_source": "companion_heard_meeting_audio",
                "device_id": self._device_id,
                "device_name": device.name if device else None,
                "backend": device.backend if device else self.devices.backend,
                "live_capture": self._stream is not None,
                "meter_level": round(self._meter_level, 4),
                "peak_level": round(self._peak_level, 4),
                "clipping": self._clipping,
                "captured": self._captured,
                "dropped_frames": self._dropped_frames,
                "error": self._stream_error,
                "mic_sensitivity": {
                    "base_threshold": round(vad.base_threshold, 5),
                    "current_threshold": round(vad.threshold, 5),
                    "hunting": vad.threshold < vad.base_threshold,
                } if vad is not None else None,
            }

    def _start_stream(self, device) -> bool:
        try:
            import numpy  # noqa: F401
            import sounddevice as sd
        except Exception as error:
            self._stream_error = f"sounddevice unavailable: {error}"
            return False
        rate = STT_SAMPLE_RATE if STT_SAMPLE_RATE in device.sample_rates else device.sample_rates[-1]
        channels = min(device.channels, 2) or 1
        blocksize = max(1, int(rate * FRAME_MS / 1000))

        def callback(indata, _frames, _time_info, status):
            if status:
                self._stream_error = str(status)
            try:
                self._frames.put_nowait(indata.copy())
            except queue.Full:
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
        except Exception as error:
            self._stream_error = f"could not open {device.name}: {error}"
            return False
        self._stream = stream
        self._stop_worker.clear()
        self._worker = threading.Thread(target=self._worker_loop, args=(rate,), daemon=True)
        self._worker.start()
        return True

    def _stop_stream(self) -> None:
        self._stop_worker.set()
        if self._worker and self._worker.is_alive():
            self._worker.join(timeout=1.0)
        self._worker = None
        stream = self._stream
        self._stream = None
        if stream is not None:
            try:
                stream.stop()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass
        while not self._frames.empty():
            try:
                self._frames.get_nowait()
            except queue.Empty:
                break

    def _worker_loop(self, rate: int) -> None:
        import numpy as np

        pre_roll_frames = max(1, PRE_ROLL_MS // FRAME_MS)
        max_frames = max(1, MAX_UTTERANCE_MS // FRAME_MS)
        pre_roll: list[Any] = []
        collecting: list[Any] = []
        vad = SimpleVad(self.config.vad_threshold, self.config.vad_silence_ms, frame_ms=FRAME_MS)
        self._vad = vad
        in_speech = False
        while not self._stop_worker.is_set():
            try:
                frame = self._frames.get(timeout=0.2)
            except queue.Empty:
                continue
            mono = frame.mean(axis=1) if getattr(frame, "ndim", 1) > 1 else frame
            level = frame_rms(mono)
            with self._lock:
                self._meter_level = level
                self._peak_level = max(self._peak_level * 0.96, level)
                self._clipping = bool(getattr(mono, "size", 0)) and bool((np.abs(mono) >= 0.999).any())
            event = vad.process(mono)
            if event == "speech_start":
                in_speech = True
                collecting = pre_roll[:] + [mono]
            elif in_speech:
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

    def _dispatch_live_segment(self, audio, rate: int) -> None:
        import numpy as np

        if rate != STT_SAMPLE_RATE and audio.size:
            target = int(round(audio.size * STT_SAMPLE_RATE / rate))
            if target > 0:
                positions = np.linspace(0, audio.size - 1, target)
                audio = np.interp(positions, np.arange(audio.size), audio).astype("float32")
        correlation_id = new_event_id()
        duration_ms = int(audio.size * 1000 / STT_SAMPLE_RATE)
        segment = AudioSegment(
            correlation_id=correlation_id,
            device_id=self._device_id,
            sample_rate=STT_SAMPLE_RATE,
            channels=1,
            samples=audio,
            reference_text=None,
            source_kind="companion_heard",
            duration_ms=duration_ms,
            route={
                "source": "meet-companion-incoming",
                "audio_source": "companion_heard_meeting_audio",
                "capture": "secondary",
                "exclude_engines": ["google_meet"],
                "self_audio_exclusion": "browser-output-sink-only",
            },
        )
        with self._lock:
            self._captured += 1
            level = self._meter_level
        self._publish(
            stream=STREAM_TRANSLATED_AUDIO, type=SPEECH_DETECTED,
            data={"segment_id": segment.id, "device_id": segment.device_id, "live": True, "duration_ms": duration_ms, "level": round(level, 4), "secondary": True},
            source_id="meet-companion-incoming", source_kind="system", correlation_id=correlation_id,
        )
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        asyncio.run_coroutine_threadsafe(self._on_segment(segment), loop)
