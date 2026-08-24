"""Per-agent voices, the speech queue, and speech-output policies."""

from __future__ import annotations

import asyncio

import pytest

from ws_collab.errors import ConflictError, ValidationError
from ws_collab.events import streams_for_role
from ws_collab.tts.engine import TtsEngine
from ws_collab.tts.voices import VoiceManager

TTS_STREAM = streams_for_role("tts_queue")[0]


@pytest.fixture
def voices(config):
    return VoiceManager(config, config.state_dir)


@pytest.fixture
def engine(config):
    published: list[dict] = []

    def publish(**kwargs):
        published.append(kwargs)
        return {"id": f"e{len(published)}"}

    instance = TtsEngine(config, publish)
    instance.published = published  # type: ignore[attr-defined]
    return instance


# ------------------------------------------------------------------- catalog
def test_voices_are_available_without_hardware(voices) -> None:
    catalog = voices.list_voices()
    assert catalog, "a usable voice catalog must always exist"
    assert all(v["id"] and v["name"] and v["language"] for v in catalog)


def test_voice_ids_are_stable_across_refresh(voices) -> None:
    before = {v["id"] for v in voices.list_voices()}
    voices.refresh()
    assert {v["id"] for v in voices.list_voices()} == before


def test_credentials_are_never_stored_in_a_profile(voices) -> None:
    profile = voices.set_profile("agent-1", {"voice_id": voices.list_voices()[0]["id"]})
    assert not any("token" in k or "secret" in k or "credential" in k for k in profile.public())


# ------------------------------------------------------------------ profiles
def test_profile_round_trips_and_persists(voices, config) -> None:
    chosen = voices.list_voices()[1]["id"]
    voices.set_profile("agent-1", {"voice_id": chosen, "rate": 1.2, "queue_priority": 2})
    reloaded = VoiceManager(config, config.state_dir)
    profile = reloaded.get_profile("agent-1")
    assert profile.voice_id == chosen and profile.rate == 1.2 and profile.queue_priority == 2


def test_invalid_fallback_policy_is_rejected(voices) -> None:
    with pytest.raises(ValidationError):
        voices.set_profile("agent-1", {"fallback": "explode"})


def test_the_originally_requested_voice_is_remembered(voices) -> None:
    voices.set_profile("agent-1", {"voice_id": "does-not-exist", "fallback": "system_default"})
    resolution = voices.resolve_for_speak("agent-1")
    assert resolution["requested_voice_id"] == "does-not-exist"
    assert resolution["voice_id"] != "does-not-exist"
    assert resolution["fallback_applied"] == "system_default"


def test_fail_policy_refuses_rather_than_substituting(voices) -> None:
    voices.set_profile("agent-1", {"voice_id": "missing", "fallback": "fail"})
    with pytest.raises(ConflictError):
        voices.resolve_for_speak("agent-1")


def test_operator_approval_policy_blocks_silent_substitution(voices) -> None:
    voices.set_profile("agent-1", {"voice_id": "missing", "fallback": "operator_approval"})
    with pytest.raises(ConflictError):
        voices.resolve_for_speak("agent-1")


# ------------------------------------------------------------------ policies
def test_unique_policy_prefers_distinct_voices(voices) -> None:
    """Distinct voices while supply lasts -- not a fixed count of agents."""

    available = len([v for v in voices.list_voices() if v["available"]])
    agents = [{"agent_id": f"a{i}"} for i in range(min(3, available))]
    assignments = voices.auto_assign(agents, "unique_when_possible")["assignments"]
    assert len(set(assignments.values())) == len(assignments)


def test_shared_policy_deliberately_reuses_one_voice(voices) -> None:
    agents = [{"agent_id": f"a{i}"} for i in range(3)]
    assignments = voices.auto_assign(agents, "shared_default")["assignments"]
    assert len(set(assignments.values())) == 1


def test_manual_policy_changes_nothing_automatically(voices) -> None:
    result = voices.auto_assign([{"agent_id": "a1"}], "manual_only")
    assert result["assignments"] == {}


def test_running_out_of_unique_voices_warns_instead_of_failing(voices) -> None:
    count = len(voices.list_voices())
    agents = [{"agent_id": f"a{i}"} for i in range(count + 2)]
    result = voices.auto_assign(agents, "unique_when_possible")
    assert result["warnings"], "sharing a voice must be reported, not silent"


def test_unknown_policy_is_rejected(voices) -> None:
    with pytest.raises(ValidationError):
        voices.auto_assign([{"agent_id": "a1"}], "telepathy")


# --------------------------------------------------------------------- queue
def test_speech_is_queued_and_played(engine) -> None:
    async def scenario():
        engine.speak("agent-1", "hello there", voice_id="fake:aria")
        await engine.process_next()

    asyncio.run(scenario())
    kinds = [p["type"] for p in engine.published]
    assert "TTS_STARTED" in kinds and "TTS_FINISHED" in kinds


def test_agent_identity_is_preserved_through_the_queue(engine) -> None:
    async def scenario():
        engine.speak("agent-7", "mine", voice_id="fake:nova")
        await engine.process_next()

    asyncio.run(scenario())
    started = [p for p in engine.published if p["type"] == "TTS_STARTED"][0]
    assert started["data"]["agent_id"] == "agent-7"
    assert started["data"]["voice_id"] == "fake:nova", "never speak with the wrong voice"


def test_duplicate_utterances_are_suppressed(engine) -> None:
    first = engine.speak("agent-1", "same text", voice_id="fake:aria")
    second = engine.speak("agent-1", "same text", voice_id="fake:aria")
    assert first["duplicate"] is False and second["duplicate"] is True


def test_higher_priority_speaks_first(engine) -> None:
    async def scenario():
        engine.speak("low", "later", voice_id="fake:aria", priority=9)
        engine.speak("high", "sooner", voice_id="fake:guy", priority=1)
        await engine.process_next()

    asyncio.run(scenario())
    assert [p for p in engine.published if p["type"] == "TTS_STARTED"][0]["data"]["agent_id"] == "high"


def test_cancelled_speech_is_not_played(engine) -> None:
    async def scenario():
        queued = engine.speak("agent-1", "cancel me", voice_id="fake:aria")
        assert engine.cancel(queued["id"]) is True
        await engine.process_next()

    asyncio.run(scenario())
    assert not [p for p in engine.published if p["type"] == "TTS_STARTED"]


def test_muted_agent_is_not_spoken(engine) -> None:
    async def scenario():
        engine.mute("agent-1")
        engine.speak("agent-1", "silence", voice_id="fake:aria")
        return await engine.process_next()

    assert asyncio.run(scenario()) is False


def test_global_pause_holds_the_queue_then_resumes(engine) -> None:
    async def scenario():
        engine.pause()
        engine.speak("agent-1", "queued", voice_id="fake:aria")
        held = await engine.process_next()
        engine.resume()
        released = await engine.process_next()
        return held, released

    held, released = asyncio.run(scenario())
    assert held is False and released is True


def test_empty_utterances_are_rejected(engine) -> None:
    with pytest.raises(ValidationError):
        engine.speak("agent-1", "   ", voice_id="fake:aria")


def test_playback_state_is_visible_for_echo_protection(engine) -> None:
    state = engine.state()
    assert "is_speaking" in state and "queue" in state
    assert engine.active_expected_texts() == [], "nothing is playing initially"


# ------------------------------------------------------------- via service
def test_service_speak_resolves_the_agent_voice(service) -> None:
    voice = service.voices.list_voices()[0]["id"]
    service.set_voice_profile("agent-1", {"voice_id": voice})
    result = service.speak("agent-1", "status report ready")
    assert result["voice_resolution"]["voice_id"] == voice


def test_service_refuses_to_speak_without_permission(service) -> None:
    service.set_voice_profile("agent-1", {"speaking_permission": False})
    with pytest.raises(ConflictError):
        service.speak("agent-1", "should not speak")


def test_service_enforces_max_utterance_length(service) -> None:
    service.set_voice_profile("agent-1", {"max_utterance_chars": 5})
    with pytest.raises(ValidationError):
        service.speak("agent-1", "far too long for this agent")
