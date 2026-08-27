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
    """Energy-gate VAD with adaptive "hunting" sensitivity.

    While nothing has crossed the gate for a while, the effective threshold is
    gradually lowered (more sensitive) so a quiet or distant voice still has a
    chance to trigger -- the system actively hunts for a signal instead of
    sitting at a fixed level that only works for one mic/room. If that makes
    it sensitive enough to trip on room noise, the resulting blip normally
    never reaches ``min_speech_ms`` / produces no usable audio and is simply
    never dispatched -- an occasional false trigger that goes nowhere is an
    accepted trade-off for not missing quiet speech. The moment real speech is
    confirmed (a full utterance completes), the threshold snaps back to the
    configured baseline since that level just proved it works.
    """

    def __init__(
        self,
        threshold: float = 0.02,
        silence_ms: int = 600,
        frame_ms: int = 20,
        min_speech_ms: int = 200,
        *,
        hunt_after_ms: float = 2500,
        hunt_ramp_ms: float = 6000,
        hunt_floor_ratio: float = 0.35,
    ):
        self.base_threshold = threshold
        self.threshold = threshold
        self.silence_ms = silence_ms
        self.frame_ms = frame_ms
        self.min_speech_ms = min_speech_ms
        self.hunt_after_ms = hunt_after_ms
        self.hunt_ramp_ms = max(1.0, hunt_ramp_ms)
        self.hunt_floor = threshold * hunt_floor_ratio
        self._quiet_ms = 0.0  # continuous time with no speech in progress at all
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
            if self.state.silence_ms >= self.silence_ms:
                if self.state.speech_ms >= self.min_speech_ms:
                    self.state.in_speech = False
                    event = "speech_end"
                else:
                    # A hunting-sensitive false start that never became real
                    # speech: cancel it quietly instead of latching in_speech
                    # forever waiting for speech_ms to reach min_speech_ms.
                    # This is the "won't send" outcome, and it must release
                    # the gate so real speech right after is still caught.
                    self.state.in_speech = False
                    self.state.speech_ms = 0.0

        if event == "speech_start" or (self.state.in_speech and level >= self.threshold):
            # Found a level that works -- stop hunting and reset the clock.
            self._quiet_ms = 0.0
            self.threshold = self.base_threshold
        elif not self.state.in_speech:
            # Nothing at all is getting through: the longer that persists, the
            # more sensitive the gate becomes, down to a noise-floor-aware
            # minimum so it never chases pure silence to zero.
            self._quiet_ms += self.frame_ms
            over = self._quiet_ms - self.hunt_after_ms
            if over > 0:
                progress = min(1.0, over / self.hunt_ramp_ms)
                self.threshold = self.base_threshold - (self.base_threshold - self.hunt_floor) * progress
        return event
