"""Whisper STT driver (recommended engine, task section 12).

Uses ``faster-whisper`` when installed to transcribe real PCM. The model loads
lazily off the event loop on first use. If the library/model is unavailable the
driver reports itself unavailable with ``fallback=True`` so a deterministic
double is substituted and the system keeps working.
"""

from __future__ import annotations

import asyncio
import importlib.util
import time

from ws_collab.audio.segment import AudioSegment
from ws_collab.drivers import DriverUnavailable, SttDriverSpec, logprob_to_confidence, pcm_to_float32
from ws_collab.stt.base import Hypothesis, PartialCallback, SttAdapter, normalize_text


class WhisperAdapter(SttAdapter):
    is_remote = False

    def __init__(self, name: str, model_size: str = "base", device: str = "cpu", compute_type: str = "int8", language: str | None = None):
        self.name = name
        self.model = f"whisper-{model_size}"
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self.language = language
        self._impl = None
        self._load_error: str | None = None

    def _ensure_model(self) -> None:
        if self._impl is not None or self._load_error is not None:
            return
        try:
            from faster_whisper import WhisperModel  # type: ignore

            self._impl = WhisperModel(self.model_size, device=self.device, compute_type=self.compute_type)
        except Exception as error:  # pragma: no cover - optional dependency
            self._load_error = str(error)

    async def transcribe(self, segment: AudioSegment, on_partial: PartialCallback | None = None) -> Hypothesis:
        start = time.perf_counter()
        if segment.samples is None:
            return Hypothesis.failed(self.name, self.model, "whisper requires audio samples (no PCM in segment)")
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._ensure_model)
        if self._impl is None:  # pragma: no cover - optional dependency
            return Hypothesis.failed(self.name, self.model, f"whisper unavailable: {self._load_error}")

        def _run():  # pragma: no cover - optional dependency
            audio = pcm_to_float32(segment.samples, segment.sample_rate)
            segments, info = self._impl.transcribe(audio, language=self.language, beam_size=1)
            texts, logprobs = [], []
            for piece in segments:
                texts.append(piece.text)
                if getattr(piece, "avg_logprob", None) is not None:
                    logprobs.append(piece.avg_logprob)
            text = " ".join(t.strip() for t in texts).strip()
            confidence = logprob_to_confidence(sum(logprobs) / len(logprobs)) if logprobs else 0.6
            return text, confidence, getattr(info, "language", self.language or "en")

        text, confidence, language = await loop.run_in_executor(None, _run)
        return Hypothesis(
            engine=self.name, model=self.model, raw_text=text, normalized_text=normalize_text(text),
            confidence=confidence, language=language, latency_ms=(time.perf_counter() - start) * 1000, is_final=True,
        )


def _build(name: str, config) -> SttAdapter:
    if importlib.util.find_spec("faster_whisper") is None:
        raise DriverUnavailable(
            "faster-whisper is not installed; install it for real Whisper transcription",
            fallback=True,
        )
    model_size = name.split(":", 1)[1] if ":" in name else "base"
    return WhisperAdapter(name, model_size=model_size)


def get_driver() -> SttDriverSpec:
    return SttDriverSpec(
        id="whisper",
        aliases=["whisper", "faster-whisper"],
        build=_build,
        description="Whisper via faster-whisper (recommended). Names may include a size, e.g. 'whisper:small'.",
        is_remote=False,
    )
