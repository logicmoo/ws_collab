"""Vosk STT driver: an independent local recognizer (task section 12).

Enabled by configuring an engine name of the form ``vosk:/path/to/model``.
Unavailable (no library or model path) reports ``fallback=True`` so a
deterministic double keeps the three-engine pipeline working.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import time
from pathlib import Path

from ws_collab.audio.segment import AudioSegment
from ws_collab.drivers import DriverUnavailable, SttDriverSpec, pcm_to_float32
from ws_collab.stt.base import Hypothesis, PartialCallback, SttAdapter, normalize_text


class VoskAdapter(SttAdapter):
    is_remote = False

    def __init__(self, name: str, model_path: str):
        self.name = name
        self.model = "vosk"
        self.model_path = model_path
        self._impl = None
        self._load_error: str | None = None

    def _ensure_model(self) -> None:
        if self._impl is not None or self._load_error is not None:
            return
        try:
            from vosk import Model  # type: ignore

            self._impl = Model(self.model_path)
        except Exception as error:  # pragma: no cover - optional dependency
            self._load_error = str(error)

    async def transcribe(self, segment: AudioSegment, on_partial: PartialCallback | None = None) -> Hypothesis:
        start = time.perf_counter()
        if segment.samples is None:
            return Hypothesis.failed(self.name, self.model, "vosk requires audio samples (no PCM in segment)")
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._ensure_model)
        if self._impl is None:  # pragma: no cover - optional dependency
            return Hypothesis.failed(self.name, self.model, f"vosk unavailable: {self._load_error}")

        def _run() -> str:  # pragma: no cover - optional dependency
            import json as _json

            import numpy as np
            from vosk import KaldiRecognizer  # type: ignore

            recognizer = KaldiRecognizer(self._impl, segment.sample_rate)
            audio = pcm_to_float32(segment.samples, segment.sample_rate)
            pcm16 = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
            recognizer.AcceptWaveform(pcm16)
            return str(_json.loads(recognizer.FinalResult()).get("text", ""))

        text = await loop.run_in_executor(None, _run)
        return Hypothesis(
            engine=self.name, model=self.model, raw_text=text, normalized_text=normalize_text(text),
            confidence=0.7 if text else 0.0, latency_ms=(time.perf_counter() - start) * 1000, is_final=True,
        )


_MODEL_ENV = "WS_COLLAB_VOSK_MODEL"
_MODEL_CACHE = Path.home() / ".cache" / "ws_collab_models"


def _discover_model(explicit: str) -> str:
    """Resolve a Vosk model directory from the engine name, env, or cache."""

    if explicit:
        return explicit
    from_env = os.environ.get(_MODEL_ENV, "").strip()
    if from_env:
        return from_env
    if _MODEL_CACHE.is_dir():
        # A Vosk model directory always contains an acoustic model in "am".
        candidates = sorted(
            path for path in _MODEL_CACHE.iterdir()
            if path.is_dir() and path.name.startswith("vosk-") and (path / "am").is_dir()
        )
        if candidates:
            return str(candidates[-1])
    return ""


def _build(name: str, config) -> SttAdapter:
    explicit = name.split(":", 1)[1] if ":" in name else ""
    model_path = _discover_model(explicit)
    if importlib.util.find_spec("vosk") is None:
        raise DriverUnavailable("vosk is not installed; pip install vosk for a second local engine", fallback=True)
    if not model_path or not Path(model_path).is_dir():
        raise DriverUnavailable(
            "no vosk model found; set WS_COLLAB_VOSK_MODEL, use 'vosk:/path/to/model', "
            f"or unpack one into {_MODEL_CACHE}",
            fallback=True,
        )
    return VoskAdapter(name, model_path=model_path)


def get_driver() -> SttDriverSpec:
    return SttDriverSpec(
        id="vosk",
        aliases=["vosk"],
        build=_build,
        description=(
            "Independent local recognizer (Vosk). Configure as 'vosk:/path/to/model', "
            f"or set {_MODEL_ENV}, or unpack a model into {_MODEL_CACHE}."
        ),
        is_remote=False,
    )
