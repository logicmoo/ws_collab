"""Audio devices, routing matrix, capture service, and prompt versioning."""

from __future__ import annotations

import asyncio

import pytest

from ws_collab.audio.devices import DeviceRegistry, Device, DIRECTION_INPUT
from ws_collab.audio.routing import RoutingManager
from ws_collab.audio.vad import SimpleVad
from ws_collab.errors import ConflictError, NotFoundError, ValidationError
from ws_collab.prompt import PromptManager


# ------------------------------------------------------------------- devices
def test_devices_are_enumerable_without_hardware(config) -> None:
    devices = DeviceRegistry(config).list()
    assert devices, "a usable device catalog must always exist"
    assert all(d["id"] and d["name"] and d["direction"] for d in devices)


def test_device_ids_are_stable_not_positional(config) -> None:
    registry = DeviceRegistry(config)
    before = {d["id"] for d in registry.list()}
    registry.refresh()
    assert {d["id"] for d in registry.list()} == before
    assert not any(d["id"].isdigit() for d in registry.list()), "ids must not be numeric indexes"


def test_device_capabilities_are_reported(config) -> None:
    device = DeviceRegistry(config).list()[0]
    assert {"channels", "sample_rates", "formats", "latency_ms", "available"} <= set(device)


def test_a_default_input_exists(config) -> None:
    assert DeviceRegistry(config).default_input() is not None


def test_refresh_reports_a_new_generation(config) -> None:
    registry = DeviceRegistry(config)
    before = registry.generation
    registry.refresh()
    assert registry.generation > before


def test_hotplug_removal_is_visible(config) -> None:
    registry = DeviceRegistry(config)
    device = next(d for d in registry.list() if d["direction"] == DIRECTION_INPUT)
    registry.simulate_hotplug(Device(**{k: v for k, v in device.items()}), removed=True)
    assert device["id"] not in {d["id"] for d in registry.list()}


# ------------------------------------------------------------------- routing
def test_route_round_trips_and_persists(config) -> None:
    manager = RoutingManager(config.state_dir)
    manager.set_route("microphone", "engine-a", "dev-1", gain=2.0, command_eligible=False)
    reloaded = RoutingManager(config.state_dir)
    route = reloaded.get("microphone", "engine-a")
    assert route.device_id == "dev-1" and route.gain == 2.0 and route.command_eligible is False


def test_one_device_can_feed_several_engines(config) -> None:
    manager = RoutingManager(config.state_dir)
    manager.set_route("microphone", "engine-a", "dev-1")
    manager.set_route("microphone", "engine-b", "dev-1")
    assert len(manager.matrix()) == 2


def test_different_engines_can_use_different_devices(config) -> None:
    manager = RoutingManager(config.state_dir)
    manager.set_route("microphone", "engine-a", "dev-1")
    manager.set_route("microphone", "engine-b", "dev-2")
    assert {r["device_id"] for r in manager.matrix()} == {"dev-1", "dev-2"}


def test_incomplete_routes_are_rejected(config) -> None:
    manager = RoutingManager(config.state_dir)
    with pytest.raises(ValidationError):
        manager.set_route("", "engine-a", "dev-1")


def test_unavailable_device_never_silently_picks_another(config) -> None:
    manager = RoutingManager(config.state_dir)
    manager.set_route("microphone", "engine-a", "gone")
    resolved = manager.resolve("microphone", "engine-a", {"other-device"})
    assert resolved["device_id"] is None, "never silently swap in an unrelated microphone"
    assert resolved["fallback_applied"] is False


def test_explicit_fallback_is_honoured_and_reported(config) -> None:
    manager = RoutingManager(config.state_dir)
    manager.set_route("microphone", "engine-a", "gone",
                      fallback_policy="explicit_device", fallback_device_id="backup")
    resolved = manager.resolve("microphone", "engine-a", {"backup"})
    assert resolved["device_id"] == "backup" and resolved["fallback_applied"] is True


def test_fail_policy_raises_rather_than_guessing(config) -> None:
    manager = RoutingManager(config.state_dir)
    manager.set_route("microphone", "engine-a", "gone", fallback_policy="fail")
    with pytest.raises(ConflictError):
        manager.resolve("microphone", "engine-a", {"other"})


def test_route_changes_are_audited(config) -> None:
    audited: list[dict] = []
    manager = RoutingManager(config.state_dir, audit_sink=audited.append)
    manager.set_route("microphone", "engine-a", "dev-1")
    manager.delete_route("microphone", "engine-a")
    actions = {entry["action"] for entry in audited}
    assert {"ROUTE_SET", "ROUTE_DELETED"} <= actions


# ----------------------------------------------------------------------- VAD
def test_vad_detects_speech_start_and_end() -> None:
    vad = SimpleVad(threshold=0.05, silence_ms=40, frame_ms=20, min_speech_ms=20)
    assert vad.process([0.5] * 10) == "speech_start"
    vad.process([0.5] * 10)
    vad.process([0.0] * 10)
    assert vad.process([0.0] * 10) == "speech_end"


def test_vad_ignores_silence() -> None:
    vad = SimpleVad(threshold=0.05)
    assert vad.process([0.0] * 10) == ""


# ------------------------------------------------------------------- capture
def test_capture_must_be_enabled_before_listening(tmp_path) -> None:
    from conftest import make_config
    from ws_collab.jsonl_store import JsonlStore
    from ws_collab.service import WsCollabService

    config = make_config(tmp_path, WS_COLLAB_AUDIO_ENABLED="0")
    store = JsonlStore(config.jsonl_dir)
    try:
        service = WsCollabService(config, store)
        with pytest.raises(ConflictError):
            service.start_capture()
    finally:
        store.close()


def test_listening_state_and_privacy_indicator_are_visible(service) -> None:
    service.start_capture()
    state = service.capture_state()
    assert state["listening"] is True and state["privacy_indicator"] == "LISTENING"
    service.stop_capture()
    assert service.capture_state()["listening"] is False


def test_injecting_before_listening_is_refused(service) -> None:
    with pytest.raises(ConflictError):
        asyncio.run(service.capture.inject_utterance("hello"))


def test_selecting_an_unknown_device_is_refused(service) -> None:
    with pytest.raises(NotFoundError):
        service.capture.select_device("nope")


def test_mute_policy_drops_input_while_speaking(tmp_path) -> None:
    from conftest import make_config
    from ws_collab.jsonl_store import JsonlStore
    from ws_collab.service import WsCollabService

    config = make_config(tmp_path, WS_COLLAB_ECHO_POLICY="mute_input_during_tts")
    store = JsonlStore(config.jsonl_dir)
    try:
        service = WsCollabService(config, store)
        service.start_capture()
        service.capture._is_tts_speaking = lambda: True
        result = asyncio.run(service.capture.inject_utterance("should be dropped"))
        assert result is None, "input must be muted while the system speaks"
        assert service.capture_state()["dropped_echo"] == 1
    finally:
        store.close()


# -------------------------------------------------------------------- prompt
@pytest.fixture
def prompt(config):
    history: list[dict] = []

    def publish(**kwargs):
        history.append({"id": f"e{len(history)}", "ts": "now", **kwargs})
        return {"id": f"e{len(history)}"}

    return PromptManager(config, publish, read_history=lambda: list(history))


def test_prompt_save_creates_a_version(prompt) -> None:
    prompt.save("first version", operator="alice", note="initial")
    current = prompt.current()
    assert current["text"] == "first version" and current["version"] == 1


def test_saving_preserves_the_previous_version(prompt) -> None:
    prompt.save("v1 text", operator="alice")
    prompt.save("v2 text", operator="bob")
    assert prompt.version_text(1) == "v1 text"
    assert prompt.current()["text"] == "v2 text"


def test_history_records_operator_and_note(prompt) -> None:
    prompt.save("text", operator="carol", note="tightened wording")
    entry = prompt.history()[-1]
    assert entry["operator"] == "carol" and entry["note"] == "tightened wording"


def test_diff_between_versions_is_available(prompt) -> None:
    prompt.save("alpha\n", operator="a")
    prompt.save("beta\n", operator="a")
    diff = prompt.diff(1, 2)
    assert "-alpha" in diff and "+beta" in diff


def test_preview_diff_does_not_save(prompt) -> None:
    prompt.save("original\n", operator="a")
    preview = prompt.preview_diff("proposed\n")
    assert "+proposed" in preview
    assert prompt.current()["text"] == "original\n", "previewing must not mutate the prompt"


def test_rollback_creates_a_new_version_and_keeps_history(prompt) -> None:
    prompt.save("v1\n", operator="a")
    prompt.save("v2\n", operator="a")
    prompt.rollback(1, operator="b")
    assert prompt.current()["text"] == "v1\n"
    assert prompt.current()["version"] == 3, "rollback is a new version, history is append-only"


def test_rollback_to_unknown_version_is_refused(prompt) -> None:
    with pytest.raises(NotFoundError):
        prompt.rollback(99)


def test_oversized_prompt_is_rejected(prompt, config) -> None:
    with pytest.raises(ValidationError):
        prompt.save("x" * (config.max_body_bytes + 1))
