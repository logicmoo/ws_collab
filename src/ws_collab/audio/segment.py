"""Audio segment value object shared by capture, STT, and TTS-accuracy code.

A segment is provider-neutral. When real capture is enabled it carries PCM
samples; for fake/loopback/synthetic sources it may instead carry a
``reference_text`` (for example the text a fake microphone "heard" or the known
text a TTS engine played back). Deterministic STT doubles use ``reference_text``
so the entire pipeline -- and WER/CER accuracy -- works and is testable without
any hardware.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..events import utc_now_iso
from ..ids import new_event_id


@dataclass
class AudioSegment:
    correlation_id: str
    id: str = field(default_factory=new_event_id)
    sample_rate: int = 16000
    channels: int = 1
    device_id: str = "fake-input"
    started_at: str = field(default_factory=utc_now_iso)
    duration_ms: int = 0
    samples: Any = None  # np.ndarray | bytes | None
    reference_text: str | None = None  # synthetic/loopback ground truth, if known
    source_kind: str = "external"  # operator | agent | system | external | unknown
    is_loopback: bool = False
    is_replay: bool = False
    is_diagnostic: bool = False
    expected_tts_text: str | None = None  # set when this segment is a TTS echo
    tts_event_id: str | None = None
    route: dict[str, Any] = field(default_factory=dict)

    def public(self) -> dict[str, Any]:
        return {
            "segment_id": self.id,
            "correlation_id": self.correlation_id,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "device_id": self.device_id,
            "started_at": self.started_at,
            "duration_ms": self.duration_ms,
            "source_kind": self.source_kind,
            "is_loopback": self.is_loopback,
            "is_replay": self.is_replay,
            "is_diagnostic": self.is_diagnostic,
            "expected_tts_text": self.expected_tts_text,
            "tts_event_id": self.tts_event_id,
            "route": self.route,
        }
