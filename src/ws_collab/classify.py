"""Source classification and TTS echo handling (task section 9).

Captured speech is classified as operator, known agent, system TTS, external
speaker, or unknown, using playback overlap, expected TTS text, loopback state,
correlation IDs, timing, and source metadata. Confidence and the reasons behind
it are always recorded; certainty is never claimed without evidence. Speech that
is confidently TTS/agent output is tagged as echo, excluded from operator-command
execution, and preserved diagnostically -- which is what prevents a recursive
TTS -> STT -> TTS loop. Uncertain but consequential speech is never executed
automatically.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .audio.segment import AudioSegment
from .tts.accuracy import word_error_rate

SOURCE_OPERATOR = "operator"
SOURCE_AGENT = "agent"
SOURCE_SYSTEM_TTS = "system_tts"
SOURCE_EXTERNAL = "external"
SOURCE_COMPANION_HEARD = "companion_heard"
SOURCE_UNKNOWN = "unknown"

ECHO_POLICIES = {
    "mute_input_during_tts",
    "listen_and_filter_tts",
    "listen_and_measure_tts_accuracy",
    "full_duplex_with_echo_cancellation",
}


@dataclass
class Classification:
    source: str
    confidence: float
    reasons: list[str] = field(default_factory=list)
    is_echo: bool = False
    should_execute: bool = False
    matched_tts_event_id: str | None = None
    expected_text: str | None = None
    echo_wer: float | None = None

    def public(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "confidence": round(self.confidence, 4),
            "reasons": list(self.reasons),
            "is_echo": self.is_echo,
            "should_execute": self.should_execute,
            "matched_tts_event_id": self.matched_tts_event_id,
            "expected_text": self.expected_text,
            "echo_wer": None if self.echo_wer is None else round(self.echo_wer, 4),
        }


class SourceClassifier:
    def __init__(self, echo_policy: str = "listen_and_filter_tts", echo_match_wer: float = 0.4):
        self.echo_policy = echo_policy
        self.echo_match_wer = echo_match_wer

    def classify(
        self,
        segment: AudioSegment,
        resolved_text: str,
        active_tts: list[dict[str, Any]] | None = None,
    ) -> Classification:
        active_tts = active_tts or []
        reasons: list[str] = []

        # 1) Segment already tagged as a deliberate TTS echo capture.
        if segment.expected_tts_text is not None:
            wer = word_error_rate(segment.expected_tts_text, resolved_text)
            reasons.append(f"segment tagged as TTS echo (wer={wer:.2f})")
            return Classification(
                source=SOURCE_SYSTEM_TTS,
                confidence=0.95,
                reasons=reasons,
                is_echo=True,
                should_execute=False,
                matched_tts_event_id=segment.tts_event_id,
                expected_text=segment.expected_tts_text,
                echo_wer=wer,
            )

        # 2) Overlap with an active TTS playback whose text matches.
        best = None
        for playback in active_tts:
            expected = playback.get("expected_text", "")
            if not expected:
                continue
            wer = word_error_rate(expected, resolved_text)
            if best is None or wer < best[0]:
                best = (wer, playback)
        if best is not None and best[0] <= self.echo_match_wer:
            wer, playback = best
            reasons.append(f"overlaps active TTS playback and matches text (wer={wer:.2f})")
            return Classification(
                source=SOURCE_SYSTEM_TTS,
                confidence=max(0.6, 1.0 - wer),
                reasons=reasons,
                is_echo=True,
                should_execute=False,
                matched_tts_event_id=playback.get("tts_event_id"),
                expected_text=playback.get("expected_text"),
                echo_wer=wer,
            )

        # 3) Loopback / diagnostic routes are never operator commands.
        if segment.is_loopback or segment.is_diagnostic:
            reasons.append("captured on a loopback/diagnostic route")
            return Classification(
                source=SOURCE_SYSTEM_TTS if segment.is_loopback else SOURCE_UNKNOWN,
                confidence=0.7,
                reasons=reasons,
                is_echo=segment.is_loopback,
                should_execute=False,
            )

        # 4) Trust explicit, corroborated source metadata.
        if segment.source_kind == SOURCE_AGENT:
            reasons.append("segment source metadata = agent")
            return Classification(source=SOURCE_AGENT, confidence=0.7, reasons=reasons, should_execute=False)
        if segment.source_kind == SOURCE_OPERATOR:
            # Operator speech may execute commands only when not an echo and while
            # no TTS is playing under a filtering policy.
            executing = self.echo_policy != "mute_input_during_tts" or not active_tts
            reasons.append("segment source metadata = operator")
            if active_tts:
                reasons.append("TTS active; operator command execution gated by echo policy")
            return Classification(
                source=SOURCE_OPERATOR,
                confidence=0.75,
                reasons=reasons,
                should_execute=bool(executing),
            )

        if segment.source_kind == SOURCE_COMPANION_HEARD:
            reasons.append("segment source metadata = companion-heard meeting audio")
            return Classification(source=SOURCE_EXTERNAL, confidence=0.75, reasons=reasons, should_execute=False)

        # 5) Otherwise external/unknown: never auto-execute consequential speech.
        reasons.append("no corroborating evidence for a trusted source")
        return Classification(source=SOURCE_UNKNOWN, confidence=0.4, reasons=reasons, should_execute=False)
