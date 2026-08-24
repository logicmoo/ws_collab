"""Text-to-speech: voice catalog, per-agent profiles, engine, and accuracy."""

from __future__ import annotations

from . import accuracy
from .engine import TtsEngine, TtsItem, build_backend
from .voices import AgentVoiceProfile, Voice, VoiceManager, enumerate_voices

__all__ = [
    "accuracy",
    "TtsEngine",
    "TtsItem",
    "build_backend",
    "AgentVoiceProfile",
    "Voice",
    "VoiceManager",
    "enumerate_voices",
]
