"""TTS voice catalog, per-agent voice profiles, and assignment policies (task 15).

Voices are enumerated provider-neutrally. A stable catalog of local "fake"
voices always exists so assignment, uniqueness, previews, and policies work and
are testable without any TTS engine installed; real SAPI voices are added when
available. Every agent gets a persisted profile (engine, voice, device, rate,
volume, pitch/style, permission, priority, max utterance, fallback). Provider
credentials are never stored.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..config import Config

VOICE_POLICIES = {
    "manual_only",
    "unique_when_possible",
    "role_based",
    "language_based",
    "round_robin",
    "shared_default",
}

FALLBACK_POLICIES = {"fail", "agent_fallback", "role_default", "system_default", "operator_approval"}

# Perceptually distinct (rate, pitch) pairs. Once the unique-voice policy runs
# out of distinct base voices it reuses a base voice with one of these variations
# so each agent still sounds different -- a synthesized "new" voice. ``rate`` is a
# speed multiplier; ``pitch`` is an offset the backend clamps to its own range.
VOICE_VARIATIONS: list[tuple[float, float]] = [
    (1.0, 0.0),
    (1.0, 4.0),
    (1.0, -4.0),
    (1.15, 2.0),
    (0.9, -2.0),
    (1.1, -4.0),
    (0.92, 4.0),
    (1.2, 0.0),
    (0.85, 0.0),
]


@dataclass
class Voice:
    id: str
    name: str
    provider: str
    language: str
    gender: str = "neutral"
    style: str = "general"
    formats: list[str] = field(default_factory=lambda: ["pcm_s16le"])
    sample_rates: list[int] = field(default_factory=lambda: [16000, 22050])
    locality: str = "local"  # local | remote
    available: bool = True
    latency_ms: float = 30.0
    requires_credentials: bool = False
    requires_network: bool = False
    cost_note: str = "free (local)"

    def public(self) -> dict[str, Any]:
        return asdict(self)


_FAKE_VOICES = [
    Voice("fake:aria", "Aria", "fake", "en-US", "female", "narration"),
    Voice("fake:guy", "Guy", "fake", "en-US", "male", "conversational"),
    Voice("fake:nova", "Nova", "fake", "en-GB", "female", "calm"),
    Voice("fake:orion", "Orion", "fake", "en-AU", "male", "news"),
    Voice("fake:rio", "Rio", "fake", "es-ES", "female", "conversational"),
    Voice("fake:kenji", "Kenji", "fake", "ja-JP", "male", "assistant"),
]

# SAPI reports language as a hex LCID; map the common ones to BCP-47 tags.
_LCID_TO_LOCALE = {
    "409": "en-US", "809": "en-GB", "c09": "en-AU", "1009": "en-CA", "1409": "en-NZ",
    "40c": "fr-FR", "407": "de-DE", "410": "it-IT", "c0a": "es-ES", "80a": "es-MX",
    "416": "pt-BR", "411": "ja-JP", "412": "ko-KR", "804": "zh-CN", "404": "zh-TW",
    "419": "ru-RU", "413": "nl-NL", "41d": "sv-SE", "415": "pl-PL", "41f": "tr-TR",
}


def _sapi_locale(raw: str) -> str:
    for part in str(raw or "").split(";"):
        key = part.strip().lower().lstrip("0") or "409"
        if key in _LCID_TO_LOCALE:
            return _LCID_TO_LOCALE[key]
    return "en-US"


def enumerate_voices(config: Config) -> tuple[list[Voice], list[str]]:
    """Return ``(voices, notes)``.

    ``auto`` (the default) prefers the real platform voices and falls back to the
    hardware-free catalog, reporting why, so voice assignment, previews, and
    policies always work.
    """

    backend = (config.tts_backend or "auto").lower()
    notes: list[str] = []

    if backend == "fake":
        return [Voice(**asdict(v)) for v in _FAKE_VOICES], notes

    real, error = _enumerate_sapi_voices()
    if real:
        return real, notes

    if backend == "sapi":
        notes.append(f"SAPI voices requested but unavailable: {error}; using the fake catalog")
    else:
        notes.append(f"no platform voices ({error}); using the fake catalog")
    return [Voice(**asdict(v)) for v in _FAKE_VOICES], notes


def _enumerate_sapi_voices() -> tuple[list[Voice], str | None]:
    """Enumerate real Windows SAPI voices. Returns ``(voices, error)``."""

    try:
        import pythoncom  # type: ignore
        import win32com.client  # type: ignore
    except Exception as error:  # pragma: no cover - depends on the platform
        return [], f"pywin32 unavailable: {error}"

    try:  # pragma: no cover - depends on the platform
        pythoncom.CoInitialize()
    except Exception:
        pass

    try:  # pragma: no cover - depends on the platform
        engine = win32com.client.Dispatch("SAPI.SpVoice")
        voices: list[Voice] = []
        for token in engine.GetVoices():
            description = str(token.GetDescription())

            def attribute(name: str, default: str = "") -> str:
                try:
                    return str(token.GetAttribute(name))
                except Exception:
                    return default

            name = attribute("Name", description) or description
            voices.append(
                Voice(
                    id=f"sapi:{name}",
                    name=description,
                    provider="sapi",
                    language=_sapi_locale(attribute("Language", "409")),
                    gender=(attribute("Gender", "neutral") or "neutral").lower(),
                    style=attribute("Age", "adult") or "adult",
                    formats=["pcm_s16le"],
                    sample_rates=[16000, 22050],
                    locality="local",
                    available=True,
                    latency_ms=60.0,
                    requires_credentials=False,
                    requires_network=False,
                    cost_note="free (installed with Windows)",
                )
            )
        if not voices:
            return [], "no SAPI voices are installed"
        return voices, None
    except Exception as error:  # pragma: no cover - depends on the platform
        return [], f"SAPI enumeration failed: {error}"


@dataclass
class AgentVoiceProfile:
    agent_id: str
    engine: str = "fake"
    voice_id: str = "fake:aria"
    output_device: str = "default"
    output_channel: int = 0
    language: str = "en-US"
    rate: float = 1.0
    volume: float = 1.0
    pitch: float = 0.0
    style: str = "general"
    speaking_permission: bool = True
    queue_priority: int = 5
    max_utterance_chars: int = 800
    fallback: str = "system_default"
    role: str = ""
    requested_voice_id: str = ""  # original request, preserved across fallback

    def public(self) -> dict[str, Any]:
        return asdict(self)


class VoiceManager:
    def __init__(self, config: Config, directory: str | Path, audit_sink: Callable[[dict[str, Any]], None] | None = None):
        self.config = config
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "voices.json"
        self._lock = threading.RLock()
        self._audit = audit_sink
        self._voices: dict[str, Voice] = {}
        self._profiles: dict[str, AgentVoiceProfile] = {}
        self._round_robin_index = 0
        self.notes: list[str] = []
        self.refresh()
        self._load_profiles()

    # ------------------------------------------------------------------ catalog
    def refresh(self) -> list[Voice]:
        with self._lock:
            voices, notes = enumerate_voices(self.config)
            self._voices = {voice.id: voice for voice in voices}
            self.notes = notes
            return list(self._voices.values())

    @property
    def backend(self) -> str:
        with self._lock:
            for voice in self._voices.values():
                return voice.provider
            return "none"

    def list_voices(self) -> list[dict[str, Any]]:
        with self._lock:
            return [voice.public() for voice in self._voices.values()]

    def get_voice(self, voice_id: str) -> Voice | None:
        with self._lock:
            return self._voices.get(voice_id)

    def is_available(self, voice_id: str) -> bool:
        voice = self.get_voice(voice_id)
        return bool(voice and voice.available)

    # ----------------------------------------------------------------- profiles
    def _load_profiles(self) -> None:
        if not self.path.is_file():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        for entry in payload.get("profiles", []):
            profile = AgentVoiceProfile(**{k: entry[k] for k in entry if k in AgentVoiceProfile.__annotations__})
            self._profiles[profile.agent_id] = profile

    def _save_profiles(self) -> None:
        payload = {"profiles": [profile.public() for profile in self._profiles.values()]}
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def list_profiles(self) -> list[dict[str, Any]]:
        with self._lock:
            return [profile.public() for profile in self._profiles.values()]

    def get_profile(self, agent_id: str) -> AgentVoiceProfile | None:
        with self._lock:
            return self._profiles.get(agent_id)

    def set_profile(self, agent_id: str, updates: dict[str, Any], *, operator: str = "operator") -> AgentVoiceProfile:
        with self._lock:
            profile = self._profiles.get(agent_id) or AgentVoiceProfile(agent_id=agent_id)
            if "fallback" in updates and updates["fallback"] not in FALLBACK_POLICIES:
                from ..errors import ValidationError

                raise ValidationError(f"invalid fallback policy: {updates['fallback']!r}")
            for key, value in updates.items():
                if key in AgentVoiceProfile.__annotations__ and key != "agent_id":
                    setattr(profile, key, value)
            if "voice_id" in updates:
                profile.requested_voice_id = updates["voice_id"]
            self._profiles[agent_id] = profile
            self._save_profiles()
        self._audit_event("VOICE_PROFILE_SET", agent_id=agent_id, voice_id=profile.voice_id, operator=operator)
        return profile

    # --------------------------------------------------------------- assignment
    def auto_assign(self, agents: list[dict[str, Any]], policy: str | None = None) -> dict[str, Any]:
        """Assign voices to agents according to ``policy``; returns assignments."""

        policy = policy or self.config.tts_policy
        if policy not in VOICE_POLICIES:
            from ..errors import ValidationError

            raise ValidationError(f"invalid voice policy: {policy!r}", details={"allowed": sorted(VOICE_POLICIES)})
        with self._lock:
            voices = [v for v in self._voices.values() if v.available]
            warnings: list[str] = []
            assignments: dict[str, str] = {}
            if policy == "manual_only":
                return {"policy": policy, "assignments": {}, "warnings": ["manual_only: no automatic assignment"]}

            used: set[tuple[str, float, float]] = set()
            for index, agent in enumerate(agents):
                agent_id = agent["agent_id"] if isinstance(agent, dict) else str(agent)
                chosen = self._choose_voice(policy, agent, index, voices, used, warnings)
                if chosen is None:
                    warnings.append(f"no voice available for {agent_id}")
                    continue
                voice, rate, pitch = chosen
                used.add((voice.id, round(rate, 3), round(pitch, 3)))
                assignments[agent_id] = voice.id
                self.set_profile(agent_id, {
                    "voice_id": voice.id, "engine": voice.provider, "language": voice.language,
                    "rate": rate, "pitch": pitch,
                })
            return {"policy": policy, "assignments": assignments, "warnings": warnings}

    def _choose_voice(self, policy, agent, index, voices, used, warnings) -> tuple[Voice, float, float] | None:
        if not voices:
            return None
        agent_language = (agent.get("language") if isinstance(agent, dict) else "") or "en"
        role = (agent.get("role") if isinstance(agent, dict) else "") or ""
        if policy == "shared_default":
            return (voices[0], 1.0, 0.0)
        if policy == "round_robin":
            voice = voices[(self._round_robin_index) % len(voices)]
            self._round_robin_index += 1
            return (voice, 1.0, 0.0)
        if policy == "language_based":
            matches = [v for v in voices if v.language.lower().startswith(agent_language.lower()[:2])]
            return (matches[0] if matches else voices[index % len(voices)], 1.0, 0.0)
        if policy == "role_based":
            pick = voices[hash(role) % len(voices)] if role else voices[index % len(voices)]
            return (pick, 1.0, 0.0)
        # unique_when_possible (default): hand out every distinct base voice
        # first; once those run out keep agents distinct by reusing a base voice
        # with an unused (rate, pitch) variation -- a synthesized "new" voice.
        for rate, pitch in VOICE_VARIATIONS:
            for voice in voices:
                key = (voice.id, round(rate, 3), round(pitch, 3))
                if key not in used:
                    if (rate, pitch) != (1.0, 0.0):
                        warnings.append(
                            f"out of unique voices; using {voice.id} with a distinct "
                            f"variation (rate {rate:g}, pitch {pitch:+g}) so it sounds like a new voice"
                        )
                    return (voice, rate, pitch)
        warnings.append("ran out of unique voice variations; sharing a voice (intentional collision)")
        return (voices[index % len(voices)], 1.0, 0.0)

    # --------------------------------------------------------------- resolution
    def resolve_for_speak(self, agent_id: str) -> dict[str, Any]:
        """Resolve the voice to actually use, applying the fallback policy."""

        with self._lock:
            profile = self._profiles.get(agent_id) or AgentVoiceProfile(agent_id=agent_id)
            requested = profile.voice_id
            if self.is_available(requested):
                return {"voice_id": requested, "requested_voice_id": requested, "fallback_applied": None}
            note = f"requested voice {requested!r} unavailable"
            policy = profile.fallback
            if policy == "fail":
                from ..errors import ConflictError

                raise ConflictError(note, details={"requested_voice_id": requested})
            if policy == "operator_approval":
                from ..errors import ConflictError

                raise ConflictError(
                    note + "; operator approval required for fallback",
                    details={"requested_voice_id": requested, "fallback": policy},
                )
            candidate = None
            available = [v for v in self._voices.values() if v.available]
            if policy in ("agent_fallback", "role_default") and available:
                candidate = available[0]
            if candidate is None and available:  # system_default
                candidate = available[0]
            if candidate is None:
                from ..errors import ConflictError

                raise ConflictError("no voices available at all", details={"requested_voice_id": requested})
            self._audit_event("VOICE_FALLBACK", agent_id=agent_id, requested=requested, resolved=candidate.id, policy=policy)
            return {
                "voice_id": candidate.id,
                "requested_voice_id": requested,
                "fallback_applied": policy,
                "note": note,
            }

    def _audit_event(self, action: str, **fields: Any) -> None:
        if self._audit is not None:
            self._audit({"type": "VOICE_AUDIT", "action": action, **fields})
