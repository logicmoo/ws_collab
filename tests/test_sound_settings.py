"""Persistent sound settings and the shared STT capture stream."""

from __future__ import annotations

import asyncio

from ws_collab.audio.segment import AudioSegment
from ws_collab.sound_settings import SoundSettings
from ws_collab.stt.base import Hypothesis, SttAdapter
from ws_collab.stt.engines import run_stt


# ----------------------------------------------------- sound settings store
def test_sound_settings_persist_across_instances(tmp_path) -> None:
    store = SoundSettings(tmp_path)
    store.set("capture_device", "dev-mic-1")
    store.set("agent_output_device", "dev-out-2")
    store.set_engine_device("whisper:tiny.en", "dev-mic-1")

    reopened = SoundSettings(tmp_path)
    assert reopened.get("capture_device") == "dev-mic-1"
    assert reopened.get("agent_output_device") == "dev-out-2"
    assert reopened.get_engine_device("whisper:tiny.en") == "dev-mic-1"
    assert (tmp_path / "sound_settings.json").is_file()


def test_sound_settings_clear_with_none(tmp_path) -> None:
    store = SoundSettings(tmp_path)
    store.set("capture_device", "dev-1")
    store.set("capture_device", None)
    assert store.get("capture_device") is None

    store.set_engine_device("engine", "dev-1")
    store.set_engine_device("engine", None)
    assert store.get_engine_device("engine") is None


# ----------------------------------------------- service-level persistence
def _device_id(service, name: str) -> str:
    for device in service.list_devices()["devices"]:
        if device["name"] == name:
            return device["id"]
    raise AssertionError(f"fake device {name!r} not found")


def test_capture_device_persists_on_start(service) -> None:
    mic = _device_id(service, "Conference Array")
    service.start_capture(mic)
    # A fresh reader sees the same file the running service wrote.
    persisted = SoundSettings(service.config.state_dir)
    assert persisted.get("capture_device") == mic


def test_default_output_device_persists(service) -> None:
    speakers = _device_id(service, "Primary Speakers")
    service.set_default_output_device(speakers)
    persisted = SoundSettings(service.config.state_dir)
    assert persisted.get("agent_output_device") == speakers


# ----------------------------------------------- shared STT capture stream
class _RecordingAdapter(SttAdapter):
    def __init__(self, name: str) -> None:
        self.name = name
        self.model = "recording"
        self.seen: AudioSegment | None = None

    async def transcribe(self, segment: AudioSegment, on_partial=None) -> Hypothesis:
        self.seen = segment
        return Hypothesis(
            engine=self.name, model=self.model,
            raw_text="ok", normalized_text="ok", confidence=1.0,
        )


def test_preview_voice_queues_speech(service) -> None:
    voice_id = service.list_voices()["voices"][0]["id"]
    result = service.preview_voice(voice_id)
    assert result.get("id"), "preview must enqueue an utterance"


def test_test_output_device_returns_a_method(service) -> None:
    speakers = _device_id(service, "Primary Speakers")
    result = service.test_output_device(speakers)
    assert result["device_id"] == speakers and result["method"] in ("tone", "tts")


def test_all_stt_drivers_share_one_segment() -> None:
    """Every configured driver must receive the *same* captured segment."""

    engines = [_RecordingAdapter(f"engine-{i}") for i in range(3)]
    segment = AudioSegment(correlation_id="c", reference_text="hello world")

    results = asyncio.run(run_stt(engines, segment, timeout_ms=2000, concurrency=3))

    assert len(results) == len(engines)
    assert all(engine.seen is segment for engine in engines), "drivers must share one stream"
