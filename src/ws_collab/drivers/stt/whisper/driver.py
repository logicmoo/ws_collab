"""Whisper STT driver (recommended engine, task section 12).

Uses ``faster-whisper`` when installed to transcribe real PCM. The model loads
lazily off the event loop on first use. If the library/model is unavailable the
driver reports itself unavailable with ``fallback=True`` so a deterministic
double is substituted and the system keeps working.

The same model size can be configured more than once with different settings
so it behaves like a distinct recognizer -- e.g. two ``whisper:tiny.en``
instances at different input gain levels, one attenuated to avoid clipping on
a loud source and one boosted to pick up a quiet/distant voice. Configure
extra settings as a query string after the model size, e.g.
``whisper:tiny.en?gain=2.0``.
"""

from __future__ import annotations

import asyncio
import importlib.util
import time

import numpy as np

from ws_collab.audio.segment import AudioSegment
from ws_collab.drivers import DriverUnavailable, SttDriverSpec, logprob_to_confidence, pcm_to_float32
from ws_collab.stt.base import Hypothesis, PartialCallback, SttAdapter, normalize_text


def _parse_settings(spec: str) -> tuple[str, float]:
    """Split ``model_size?gain=2.0`` into (model_size, gain)."""

    model_size, _, params_str = spec.partition("?")
    gain = 1.0
    for pair in params_str.split("&"):
        key, _, raw_value = pair.partition("=")
        if key == "gain" and raw_value:
            try:
                gain = float(raw_value)
            except ValueError:
                pass
    return model_size or "base", gain


class WhisperAdapter(SttAdapter):
    is_remote = False

    def __init__(
        self,
        name: str,
        model_size: str = "base",
        device: str = "cpu",
        compute_type: str = "int8",
        language: str | None = None,
        gain: float = 1.0,
    ):
        self.name = name
        self.model = f"whisper-{model_size}" + (f"@gain={gain:g}" if gain != 1.0 else "")
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self.language = language
        self.gain = gain
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
            if self.gain != 1.0:
                # Applied before inference so each instance of the same model
                # genuinely sees different audio -- not just a relabeled copy.
                audio = np.clip(audio * self.gain, -1.0, 1.0).astype("float32")
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
    spec = name.split(":", 1)[1] if ":" in name else "base"
    model_size, gain = _parse_settings(spec)
    return WhisperAdapter(name, model_size=model_size, gain=gain)


def get_driver() -> SttDriverSpec:
    return SttDriverSpec(
        id="whisper",
        aliases=["whisper", "faster-whisper"],
        build=_build,
        description=(
            "Whisper via faster-whisper (recommended). Names may include a size and "
            "settings, e.g. 'whisper:small' or 'whisper:tiny.en?gain=2.0' -- the same "
            "size can be configured more than once at different gains to act as "
            "distinct recognizers."
        ),
        is_remote=False,
    )
