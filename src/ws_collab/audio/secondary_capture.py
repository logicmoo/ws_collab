"""Independent secondary audio capture for Meet companion incoming audio."""

from __future__ import annotations

import asyncio
import base64
import binascii
import queue
import threading
import time
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
_MAX_BROWSER_CHUNKS_PER_REQUEST = 96
_MAX_BROWSER_CHUNK_BYTES = 64 * 1024
_BROWSER_DEVICE_ID = "browser:meet-companion-incoming"


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
        self._dropped_chunks = 0
        self._dropped_bytes = 0
        self._dropped_artifact_chunks = 0
        self._dropped_artifact_frames = 0
        self._dropped_artifact_bytes = 0
        self._last_suppression_artifact: dict[str, Any] | None = None
        self._chunks_received = 0
        self._frames_received = 0
        self._bytes_received = 0
        self._chunks_forwarded = 0
        self._frames_forwarded = 0
        self._bytes_forwarded = 0
        self._last_chunk_at: float | None = None
        self._last_audio_at: float | None = None
        self._segments_forwarded = 0
        self._dispatch_errors = 0
        self._stream_error: str | None = None
        self._input_mode = "device"
        self._browser_connected = False
        self._browser_ever_connected = False
        self._browser_stream_id: str | None = None
        self._browser_sample_rate: int | None = None
        self._browser_muted = True
        self._browser_last_chunk_at: float | None = None
        self._browser_disconnects = 0
        self._browser_reconnects = 0
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
            self._input_mode = "device"
            self._browser_connected = False
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
            self._browser_connected = False
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
                "input_mode": self._input_mode,
                "device_id": self._device_id,
                "device_name": device.name if device else None,
                "backend": "browser_media_stream" if self._input_mode == "browser" else (device.backend if device else self.devices.backend),
                "live_capture": self._browser_connected if self._input_mode == "browser" else self._stream is not None,
                "meter_level": round(self._meter_level, 4),
                "peak_level": round(self._peak_level, 4),
                "clipping": self._clipping,
                "captured": self._captured,
                "dropped_frames": self._dropped_frames,
                "dropped_chunks": self._dropped_chunks,
                "dropped_bytes": self._dropped_bytes,
                "dropped_artifact_chunks": self._dropped_artifact_chunks,
                "dropped_artifact_frames": self._dropped_artifact_frames,
                "dropped_artifact_bytes": self._dropped_artifact_bytes,
                "last_suppression_artifact": (
                    dict(self._last_suppression_artifact)
                    if self._last_suppression_artifact
                    else None
                ),
                "chunks_received": self._chunks_received,
                "frames_received": self._frames_received,
                "bytes_received": self._bytes_received,
                "chunks_forwarded": self._chunks_forwarded,
                "frames_forwarded": self._frames_forwarded,
                "bytes_forwarded": self._bytes_forwarded,
                "last_chunk_at": self._last_chunk_at,
                "last_audio_at": self._last_audio_at,
                "vad_in_speech": bool(vad and vad.state.in_speech),
                "silence_ready": bool(
                    self._listening
                    and (
                        self._browser_connected
                        if self._input_mode == "browser"
                        else self._stream is not None
                    )
                ),
                "decision_source": (
                    "browser-media-stream"
                    if self._input_mode == "browser"
                    else "receive-cable"
                ),
                "segments_forwarded": self._segments_forwarded,
                "dispatch_errors": self._dispatch_errors,
                "browser_connected": self._browser_connected,
                "browser_stream_id": self._browser_stream_id,
                "browser_sample_rate": self._browser_sample_rate,
                "browser_muted": self._browser_muted,
                "browser_last_chunk_at": self._browser_last_chunk_at,
                "browser_disconnects": self._browser_disconnects,
                "browser_reconnects": self._browser_reconnects,
                "queued_chunks": self._frames.qsize(),
                "queue_capacity": self._frames.maxsize,
                "error": self._stream_error,
                "mic_sensitivity": {
                    "base_threshold": round(vad.base_threshold, 5),
                    "current_threshold": round(vad.threshold, 5),
                    "hunting": vad.threshold < vad.base_threshold,
                } if vad is not None else None,
            }

    def ingest_browser_audio(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Feed companion-tab PCM into the same VAD/STT path as device capture."""

        if not self.config.audio_enabled:
            from ..errors import ConflictError

            raise ConflictError("secondary capture is disabled; set WS_COLLAB_AUDIO_ENABLED=1 to enable it")
        if not isinstance(payload, dict):
            from ..errors import ValidationError

            raise ValidationError("browser audio payload must be an object")
        try:
            rate = int(payload.get("sample_rate") or 0)
        except (TypeError, ValueError):
            rate = 0
        if rate < 8000 or rate > 192000:
            from ..errors import ValidationError

            raise ValidationError("sample_rate must be between 8000 and 192000")
        chunks = payload.get("chunks") or []
        if not isinstance(chunks, list) or len(chunks) > _MAX_BROWSER_CHUNKS_PER_REQUEST:
            from ..errors import ValidationError

            raise ValidationError(f"chunks must be a list of at most {_MAX_BROWSER_CHUNKS_PER_REQUEST} items")

        stream_id = str(payload.get("stream_id") or "companion-remote-media")
        connected = bool(payload.get("connected"))
        muted = bool(payload.get("muted", True))
        with self._lock:
            needs_worker = (
                not self._listening
                or self._input_mode != "browser"
                or self._browser_sample_rate != rate
            )
        if needs_worker:
            self._start_browser_worker(rate, stream_id)
        self._update_browser_connection(connected, stream_id, muted)

        suppressed_chunks = max(0, int(payload.get("suppressed_artifact_chunks") or 0))
        suppressed_frames = max(0, int(payload.get("suppressed_artifact_frames") or 0))
        suppressed_bytes = max(0, int(payload.get("suppressed_artifact_bytes") or 0))
        with self._lock:
            self._dropped_artifact_chunks += suppressed_chunks
            self._dropped_artifact_frames += suppressed_frames
            self._dropped_artifact_bytes += suppressed_bytes
            artifact = payload.get("suppression_artifact")
            if suppressed_chunks and isinstance(artifact, dict):
                self._last_suppression_artifact = dict(artifact)

        import numpy as np

        for chunk in chunks:
            if not isinstance(chunk, dict):
                continue
            encoded = chunk.get("pcm_s16le_base64")
            if not isinstance(encoded, str):
                continue
            try:
                raw = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error):
                with self._lock:
                    self._dropped_chunks += 1
                continue
            if not raw or len(raw) > _MAX_BROWSER_CHUNK_BYTES or len(raw) % 2:
                with self._lock:
                    self._dropped_chunks += 1
                    self._dropped_bytes += len(raw)
                continue
            frame = np.frombuffer(raw, dtype="<i2").astype("float32") / 32768.0
            frames = int(frame.size)
            with self._lock:
                self._chunks_received += 1
                self._frames_received += frames
                self._bytes_received += len(raw)
            if not muted:
                with self._lock:
                    self._dropped_artifact_chunks += 1
                    self._dropped_artifact_frames += frames
                    self._dropped_artifact_bytes += len(raw)
                continue
            blocksize = max(1, int(rate * FRAME_MS / 1000))
            for offset in range(0, frames, blocksize):
                block = frame[offset:offset + blocksize]
                self._enqueue_frame(
                    block,
                    chunks=1 if offset == 0 else 0,
                    frames=int(block.size),
                    byte_count=int(block.size * 2),
                )
            with self._lock:
                self._browser_last_chunk_at = time.time()
        return self.state()

    def _start_browser_worker(self, rate: int, stream_id: str) -> None:
        self._stop_stream()
        with self._lock:
            self._device_id = _BROWSER_DEVICE_ID
            self._input_mode = "browser"
            self._listening = True
            self._stream_error = None
            self._browser_sample_rate = rate
            self._browser_stream_id = stream_id
        self._stop_worker.clear()
        self._worker = threading.Thread(
            target=self._worker_loop,
            args=(rate,),
            name="ws_collab_companion_audio",
            daemon=True,
        )
        self._worker.start()
        self._publish(
            stream=STREAM_TRANSLATED_AUDIO,
            type=LISTENING_STARTED,
            data={
                "device_id": _BROWSER_DEVICE_ID,
                "backend": "browser_media_stream",
                "live_capture": True,
                "secondary": True,
                "audio_source": "companion_heard_meeting_audio",
            },
            source_id="meet-companion-incoming",
            source_kind="system",
        )

    def _update_browser_connection(self, connected: bool, stream_id: str, muted: bool) -> None:
        ended = False
        with self._lock:
            was_connected = self._browser_connected
            if was_connected and not connected:
                self._browser_disconnects += 1
                ended = True
            elif connected and not was_connected and self._browser_ever_connected:
                self._browser_reconnects += 1
            self._browser_connected = connected
            self._browser_ever_connected = self._browser_ever_connected or connected
            self._browser_stream_id = stream_id
            self._browser_muted = muted
            if not muted:
                self._stream_error = "companion media playback is not muted"
            elif connected:
                self._stream_error = None
        if ended:
            self._enqueue_frame(None, chunks=0, frames=0, byte_count=0)

    def _enqueue_frame(self, frame: Any, *, chunks: int, frames: int, byte_count: int) -> None:
        item = (frame, chunks, frames, byte_count)
        try:
            self._frames.put_nowait(item)
        except queue.Full:
            try:
                dropped = self._frames.get_nowait()
                self._frames.put_nowait(item)
            except queue.Empty:
                dropped = None
            with self._lock:
                self._dropped_frames += 1
                if isinstance(dropped, tuple) and len(dropped) == 4:
                    self._dropped_chunks += int(dropped[1])
                    self._dropped_bytes += int(dropped[3])
        with self._lock:
            self._chunks_forwarded += chunks
            self._frames_forwarded += frames
            self._bytes_forwarded += byte_count
            if frame is not None:
                self._last_chunk_at = time.time()

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
            copied = indata.copy()
            self._enqueue_frame(
                copied,
                chunks=1,
                frames=int(getattr(copied, "shape", [len(copied)])[0]),
                byte_count=int(getattr(copied, "nbytes", 0)),
            )

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
                queued = self._frames.get(timeout=0.2)
            except queue.Empty:
                continue
            frame = queued[0] if isinstance(queued, tuple) else queued
            if frame is None:
                if in_speech and collecting:
                    audio = np.concatenate(collecting)
                    if audio.size:
                        self._dispatch_live_segment(audio, rate)
                pre_roll = []
                collecting = []
                in_speech = False
                vad.state.in_speech = False
                continue
            mono = frame.mean(axis=1) if getattr(frame, "ndim", 1) > 1 else frame
            level = frame_rms(mono)
            with self._lock:
                self._meter_level = level
                self._peak_level = max(self._peak_level * 0.96, level)
                self._clipping = bool(getattr(mono, "size", 0)) and bool((np.abs(mono) >= 0.999).any())
                if level > vad.threshold:
                    self._last_audio_at = time.time()
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
                "self_audio_exclusion": (
                    "remote-media-stream-only"
                    if self._input_mode == "browser"
                    else "distinct-transmit-cable"
                ),
                "artifact_suppression": (
                    "bridge-capture-time-filter"
                    if self._input_mode == "browser"
                    else "two-cable-physical-isolation"
                ),
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
            with self._lock:
                self._dispatch_errors += 1
            return
        future = asyncio.run_coroutine_threadsafe(self._on_segment(segment), loop)
        with self._lock:
            self._segments_forwarded += 1

        def record_failure(done) -> None:
            try:
                done.result()
            except Exception:
                with self._lock:
                    self._dispatch_errors += 1

        future.add_done_callback(record_failure)
