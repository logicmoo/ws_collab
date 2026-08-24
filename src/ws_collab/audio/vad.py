"""Voice-activity detection helpers.

A simple energy-gate VAD supports the real-capture path (segmenting a PCM stream
with pre-roll and end-of-utterance silence). The event-driven fake capture path
does not need frame-level VAD because whole utterances are injected directly.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any


def frame_rms(frame: Any) -> float:
    """Root-mean-square level of a frame (numpy array, bytes, or float list)."""

    try:
        import numpy as np  # local import keeps numpy optional

        if isinstance(frame, np.ndarray):
            if frame.size == 0:
                return 0.0
            data = frame.astype("float64")
            if data.dtype.kind in "iu" or (data.max() > 1.5 if data.size else False):
                data = data / 32768.0
            return float(np.sqrt(np.mean(np.square(data))))
    except Exception:
        pass
    if isinstance(frame, (bytes, bytearray)):
        if not frame:
            return 0.0
        import array

        samples = array.array("h")
        samples.frombytes(bytes(frame[: len(frame) // 2 * 2]))
        if not samples:
            return 0.0
        return math.sqrt(sum(s * s for s in samples) / len(samples)) / 32768.0
    if isinstance(frame, (list, tuple)) and frame:
        return math.sqrt(sum(float(s) * float(s) for s in frame) / len(frame))
    return 0.0


@dataclass
class VadState:
    in_speech: bool = False
    silence_ms: float = 0.0
    speech_ms: float = 0.0
    level: float = 0.0


class SimpleVad:
    def __init__(self, threshold: float = 0.02, silence_ms: int = 600, frame_ms: int = 20, min_speech_ms: int = 200):
        self.threshold = threshold
        self.silence_ms = silence_ms
        self.frame_ms = frame_ms
        self.min_speech_ms = min_speech_ms
        self.state = VadState()

    def process(self, frame: Any) -> str:
        """Feed one frame; return an event: '', 'speech_start', or 'speech_end'."""

        level = frame_rms(frame)
        self.state.level = level
        event = ""
        if level >= self.threshold:
            if not self.state.in_speech:
                self.state.in_speech = True
                self.state.speech_ms = 0.0
                event = "speech_start"
            self.state.speech_ms += self.frame_ms
            self.state.silence_ms = 0.0
        elif self.state.in_speech:
            self.state.silence_ms += self.frame_ms
            if self.state.silence_ms >= self.silence_ms and self.state.speech_ms >= self.min_speech_ms:
                self.state.in_speech = False
                event = "speech_end"
        return event
