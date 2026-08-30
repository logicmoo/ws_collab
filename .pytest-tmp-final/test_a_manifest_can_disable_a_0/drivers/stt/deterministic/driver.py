"""Deterministic STT driver: hardware-free doubles for the default engine set.

Provides three materially independent error models so hypotheses genuinely
disagree, which makes disambiguation and WER/CER meaningful and testable without
any model, hardware, or network.
"""

from __future__ import annotations

import asyncio
import random
import time

from ws_collab.audio.segment import AudioSegment
from ws_collab.drivers import DriverUnavailable, SttDriverSpec
from ws_collab.stt.base import Hypothesis, PartialCallback, SttAdapter, normalize_text

_HOMOPHONES = {
    "two": "too", "too": "to", "to": "two", "their": "there", "there": "their",
    "hear": "here", "here": "hear", "for": "four", "four": "for", "write": "right",
    "right": "write", "know": "no", "no": "know", "one": "won", "won": "one",
    "buy": "by", "by": "buy", "sea": "see", "see": "sea", "flower": "flour",
}
_SHORT_WORDS = {"a", "an", "the", "to", "of", "is", "it", "in", "on", "and", "or"}

_PROFILES = {
    "fallback_alpha": dict(mode="homophone", error_rate=0.05, base_confidence=0.93),
    "fallback_beta": dict(mode="homophone", error_rate=0.18, base_confidence=0.80),
    "fallback_gamma": dict(mode="drop", error_rate=0.25, base_confidence=0.70),
}


class DeterministicEngine(SttAdapter):
    is_remote = False

    def __init__(self, name: str, *, mode: str, error_rate: float, base_confidence: float, latency_ms: float = 40.0):
        self.name = name
        self.model = f"deterministic-{mode}"
        self.mode = mode
        self.error_rate = error_rate
        self.base_confidence = base_confidence
        self.latency_ms = latency_ms

    async def transcribe(self, segment: AudioSegment, on_partial: PartialCallback | None = None) -> Hypothesis:
        start = time.perf_counter()
        reference = segment.reference_text
        if reference is None:
            # Real captured audio with no ground truth. This engine has no
            # acoustic model, so it reports honestly instead of inventing text.
            return Hypothesis.failed(
                self.name,
                self.model,
                error=(
                    "no acoustic model: this deterministic engine cannot decode real audio. "
                    "Configure a real engine (e.g. whisper or vosk) for live capture."
                ),
                latency_ms=(time.perf_counter() - start) * 1000,
            )
        await asyncio.sleep(self.latency_ms / 1000.0)
        words = normalize_text(reference).split()
        rng = random.Random(f"{self.name}:{reference}")

        if on_partial is not None and words:
            prefix = " ".join(words[: max(1, len(words) // 2)])
            partial = Hypothesis(
                engine=self.name, model=self.model, raw_text=prefix, normalized_text=prefix,
                confidence=self.base_confidence * 0.6, latency_ms=(time.perf_counter() - start) * 1000, is_final=False,
            )
            result = on_partial(segment.correlation_id, partial)
            if asyncio.iscoroutine(result):
                await result

        produced, substitutions = self._apply_errors(words, rng)
        raw_text = " ".join(produced)
        confidence = max(0.2, self.base_confidence - 0.08 * substitutions)
        return Hypothesis(
            engine=self.name, model=self.model, raw_text=raw_text, normalized_text=normalize_text(raw_text),
            confidence=confidence, language="en", latency_ms=(time.perf_counter() - start) * 1000, is_final=True,
        )

    def _apply_errors(self, words, rng):
        produced = []
        substitutions = 0
        for word in words:
            if rng.random() < self.error_rate:
                if self.mode in ("homophone", "mixed") and word in _HOMOPHONES:
                    produced.append(_HOMOPHONES[word])
                    substitutions += 1
                    continue
                if self.mode in ("drop", "mixed") and word in _SHORT_WORDS:
                    substitutions += 1
                    continue
            produced.append(word)
        return produced, substitutions


def _build(name: str, config) -> SttAdapter:
    lowered = name.lower()
    if lowered in _PROFILES:
        return DeterministicEngine(name, **_PROFILES[lowered])
    # Any unknown engine name falls here as a working default.
    return DeterministicEngine(name, mode="mixed", error_rate=0.12, base_confidence=0.82)


def get_driver() -> SttDriverSpec:
    return SttDriverSpec(
        id="deterministic",
        aliases=["deterministic", "fallback", "fallback_alpha", "fallback_beta", "fallback_gamma"],
        build=_build,
        description="Hardware-free deterministic STT doubles (default engine set).",
        is_remote=False,
    )
