"""Speech-to-text adapter contract and shared text normalization.

Adapters are provider-neutral: each exposes a name, a model label, whether it
sends audio off-device (``is_remote``), and an async :meth:`transcribe`. The
concurrent runner applies an individual timeout to each adapter so one slow or
failing engine never cancels the others (task section 12).
"""

from __future__ import annotations

import re
import unicodedata
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ..audio.segment import AudioSegment

_PUNCT = re.compile(r"[^\w\s']", flags=re.UNICODE)
_WS = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace, normalize unicode."""

    text = unicodedata.normalize("NFKC", text or "")
    text = text.lower()
    text = _PUNCT.sub(" ", text)
    text = _WS.sub(" ", text)
    return text.strip()


@dataclass
class Hypothesis:
    engine: str
    model: str
    raw_text: str
    normalized_text: str
    confidence: float
    language: str = "en"
    latency_ms: float = 0.0
    is_final: bool = True
    is_remote: bool = False
    error: str | None = None
    alternatives: list[str] = field(default_factory=list)

    def public(self) -> dict[str, Any]:
        return {
            "engine": self.engine,
            "model": self.model,
            "raw_text": self.raw_text,
            "normalized_text": self.normalized_text,
            "confidence": round(self.confidence, 4),
            "language": self.language,
            "latency_ms": round(self.latency_ms, 2),
            "is_final": self.is_final,
            "is_remote": self.is_remote,
            "error": self.error,
            "alternatives": list(self.alternatives),
        }

    @classmethod
    def failed(cls, engine: str, model: str, error: str, latency_ms: float = 0.0) -> "Hypothesis":
        return cls(
            engine=engine,
            model=model,
            raw_text="",
            normalized_text="",
            confidence=0.0,
            latency_ms=latency_ms,
            error=error,
        )


PartialCallback = Callable[[str, Hypothesis], Awaitable[None] | None]


class SttAdapter(ABC):
    name: str = "adapter"
    model: str = "unknown"
    is_remote: bool = False

    @abstractmethod
    async def transcribe(self, segment: AudioSegment, on_partial: PartialCallback | None = None) -> Hypothesis:
        """Return a final :class:`Hypothesis` for ``segment``.

        Implementations may invoke ``on_partial`` zero or more times with
        interim hypotheses before returning the final one.
        """
        raise NotImplementedError
