"""NVIDIA NeMo STT driver (Parakeet / Canary / Nemotron-speech family).

This is the engine family the Copilot app uses for voice dictation (e.g.
``nemotron-speech-streaming-en-0.6b``). Configure it as
``nemo:<model-name-or-path>`` where the value is an NGC/HuggingFace model name or
a local ``.nemo`` path. Requires ``nemo_toolkit[asr]``; if unavailable the driver
reports ``fallback=True`` so a deterministic double keeps the pipeline working.

Note: the Copilot app manages its own downloaded model internally; that artifact
is not a supported public API. To feed the Copilot app's recognizer into
WS_COLLAB, prefer the external ingest bridge (POST /ws_collab/v1/stt/ingest)
instead of loading the app's model file directly.
"""

from __future__ import annotations

import asyncio
import importlib.util
import time
import wave
from pathlib import Path

from ws_collab.audio.segment import AudioSegment
from ws_collab.drivers import DriverUnavailable, SttDriverSpec, pcm_to_float32
from ws_collab.stt.base import Hypothesis, PartialCallback, SttAdapter, normalize_text


class NemoAdapter(SttAdapter):
    is_remote = False

    def __init__(self, name: str, model_ref: str):
        self.name = name
        self.model = f"nemo:{model_ref}"
        self.model_ref = model_ref
        self._impl = None
        self._load_error: str | None = None

    def _ensure_model(self) -> None:
        if self._impl is not None or self._load_error is not None:
            return
        try:  # pragma: no cover - optional heavy dependency
            from nemo.collections.asr.models import ASRModel  # type: ignore

            ref = self.model_ref
            if ref.endswith(".nemo") and Path(ref).is_file():
                self._impl = ASRModel.restore_from(ref, map_location="cpu")
            else:
                self._impl = ASRModel.from_pretrained(ref, map_location="cpu")
        except Exception as error:  # pragma: no cover - optional dependency
            self._load_error = str(error)

    async def transcribe(self, segment: AudioSegment, on_partial: PartialCallback | None = None) -> Hypothesis:
        start = time.perf_counter()
        if segment.samples is None:
            return Hypothesis.failed(self.name, self.model, "nemo requires audio samples (no PCM in segment)")
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._ensure_model)
        if self._impl is None:  # pragma: no cover - optional dependency
            return Hypothesis.failed(self.name, self.model, f"nemo unavailable: {self._load_error}")

        def _run() -> str:  # pragma: no cover - optional dependency
            import tempfile

            import numpy as np

            audio = pcm_to_float32(segment.samples, segment.sample_rate)
            pcm16 = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
                path = handle.name
            with wave.open(path, "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(segment.sample_rate)
                wav.writeframes(pcm16.tobytes())
            try:
                result = self._impl.transcribe([path], batch_size=1)
            finally:
                Path(path).unlink(missing_ok=True)
            first = result[0] if result else ""
            return first if isinstance(first, str) else getattr(first, "text", str(first))

        text = await loop.run_in_executor(None, _run)
        return Hypothesis(
            engine=self.name, model=self.model, raw_text=text, normalized_text=normalize_text(text),
            confidence=0.85 if text else 0.0, latency_ms=(time.perf_counter() - start) * 1000, is_final=True,
        )


def _build(name: str, config) -> SttAdapter:
    if importlib.util.find_spec("nemo") is None:
        raise DriverUnavailable("nemo_toolkit not installed; install nemo_toolkit[asr] for NeMo STT", fallback=True)
    model_ref = name.split(":", 1)[1] if ":" in name else "nvidia/parakeet-tdt-0.6b-v2"
    return NemoAdapter(name, model_ref=model_ref)


def get_driver() -> SttDriverSpec:
    return SttDriverSpec(
        id="nemo",
        aliases=["nemo", "nemotron", "parakeet", "canary"],
        build=_build,
        description="NVIDIA NeMo ASR (Parakeet/Canary/Nemotron). Configure as 'nemo:<model-or-path>'.",
        is_remote=False,
    )
