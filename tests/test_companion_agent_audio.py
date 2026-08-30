from __future__ import annotations

import asyncio
import io
import sys
import threading
import time
import wave

import pytest

from ws_collab.errors import ConflictError
from ws_collab.meet_bridge.audio_out import AudioPlaybackCancelled, play_wav_bytes_to_device
from ws_collab.meet_bridge.companion_audio import CompanionAudioArbiter


def _ready_state() -> dict:
    return {
        "ready": True,
        "meetingUrl": "https://meet.google.com/abc-defg-hij",
        "tabId": "companion-tab",
        "syntheticMicReady": True,
    }


def test_companion_audio_rejects_without_ready_companion() -> None:
    arbiter = CompanionAudioArbiter(
        lambda _target: {"ready": False, "error": "companion tab is not attached"},
        lambda _item, _cancel: None,
        lambda _reason: None,
    )

    result = arbiter.submit(kind="speech", text="hello")

    assert result["ok"] is False
    assert result["accepted"] is False
    assert result["reason"] == "companion-not-ready"
    assert "not attached" in result["error"]
    assert arbiter.status()["rejected"] == 1


def test_companion_audio_serializes_speech_and_interject_and_carries_markers() -> None:
    active = 0
    peak = 0
    played: list[dict] = []

    def playback(item, _cancel):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        played.append(item)
        active -= 1

    arbiter = CompanionAudioArbiter(
        lambda _target: _ready_state(),
        playback,
        lambda _reason: None,
    )
    speech = arbiter.submit(
        kind="speech",
        text="agent report",
        meeting_url="https://meet.google.com/abc-defg-hij",
        source="virtual-agent-tts",
        metadata={
            "utterance_id": "tts-1",
            "agent_id": "agent-7",
            "correlation_id": "corr-1",
            "expected_text": "agent report",
        },
    )
    interject = arbiter.submit(
        kind="interject",
        meeting_url="https://meet.google.com/abc-defg-hij",
        source="companion-interjector",
        metadata={"phrase": "hmm", "decision": {"mode": "on_silence"}},
    )

    assert speech["destination"]["type"] == "companion"
    assert speech["artifact"] == {"source": "virtual-agent-tts", "kind": "speech"}
    assert interject["accepted"] is True
    assert arbiter.process_next() is True
    assert arbiter.process_next() is True
    assert peak == 1
    assert [item["kind"] for item in played] == ["speech", "interject"]
    assert played[0]["metadata"]["correlation_id"] == "corr-1"
    assert played[1]["metadata"]["phrase"] == "hmm"
    status = arbiter.status()
    assert status["queued"] == 0
    assert status["speaking"] is False
    assert status["sent"] == status["completed"] == 2
    assert status["lastDestination"]["tabId"] == "companion-tab"


def test_companion_audio_queue_is_bounded_and_overflow_counted() -> None:
    arbiter = CompanionAudioArbiter(
        lambda _target: _ready_state(),
        lambda _item, _cancel: None,
        lambda _reason: None,
        max_pending=2,
    )
    assert arbiter.submit(kind="speech", text="one")["accepted"]
    assert arbiter.submit(kind="speech", text="two")["accepted"]

    overflow = arbiter.submit(kind="speech", text="three")

    assert overflow["reason"] == "queue-full"
    assert overflow["status"]["capacity"] == 2
    assert overflow["status"]["queued"] == 2
    assert overflow["status"]["dropped"] == 1
    assert overflow["status"]["rejected"] == 1


def test_companion_audio_meeting_switch_cancels_current_and_pending() -> None:
    state = _ready_state()
    started = threading.Event()
    release = threading.Event()
    cancel_calls: list[str] = []

    def readiness(target):
        result = dict(state)
        if target and target != result["meetingUrl"]:
            result.update(ready=False, error="meeting mismatch")
        return result

    def playback(_item, cancel):
        started.set()
        while not cancel.is_set() and not release.wait(0.01):
            pass

    arbiter = CompanionAudioArbiter(readiness, playback, cancel_calls.append)
    arbiter.start()
    arbiter.submit(kind="speech", text="current")
    pending = arbiter.submit(kind="speech", text="pending")
    assert started.wait(1)

    state["meetingUrl"] = "https://meet.google.com/new-room-xyz"
    dropped = arbiter.invalidate("meeting switch")
    release.set()
    deadline = time.time() + 1
    while arbiter.status()["speaking"] and time.time() < deadline:
        time.sleep(0.01)

    assert dropped == 1
    assert cancel_calls == ["meeting switch"]
    assert arbiter.status()["completed"] == 0
    assert arbiter.status()["cancelled"] == 2
    assert arbiter.status()["queued"] == 0
    assert arbiter.utterance_status(pending["id"])["state"] == "cancelled"
    arbiter.stop()


def test_physical_playback_cancellation_aborts_promptly_and_frees_arbiter_queue(
    monkeypatch
) -> None:
    writes_started = threading.Event()
    aborted = threading.Event()
    exited = threading.Event()

    class FakeStream:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            exited.set()

        def write(self, _chunk):
            writes_started.set()
            time.sleep(0.01)

        def abort(self):
            aborted.set()

    class FakeSoundDevice:
        OutputStream = FakeStream

        @staticmethod
        def query_devices(_index):
            return {"default_samplerate": 16000}

    monkeypatch.setitem(sys.modules, "sounddevice", FakeSoundDevice)
    wav = io.BytesIO()
    with wave.open(wav, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(16000)
        writer.writeframes(b"\0\0" * 16000 * 5)

    def playback(_item, cancel):
        play_wav_bytes_to_device(wav.getvalue(), 1, cancellation=cancel)

    arbiter = CompanionAudioArbiter(
        lambda _target: _ready_state(), playback, lambda _reason: None
    )
    arbiter.start()
    current = arbiter.submit(kind="speech", text="long physical playback")
    pending = arbiter.submit(kind="speech", text="must be released")
    assert writes_started.wait(1)

    started = time.monotonic()
    dropped = arbiter.invalidate("meeting switch")
    deadline = time.monotonic() + 1
    while arbiter.status()["speaking"] and time.monotonic() < deadline:
        time.sleep(0.005)

    assert time.monotonic() - started < 0.5
    assert dropped == 1
    assert aborted.is_set() and exited.is_set()
    assert arbiter.status()["queued"] == 0
    assert arbiter.utterance_status(current["id"])["state"] == "cancelled"
    assert arbiter.utterance_status(pending["id"])["state"] == "cancelled"
    arbiter.stop()


def test_physical_playback_accepts_callback_cancellation(monkeypatch) -> None:
    class FakeStream:
        def __init__(self, **_kwargs):
            self.aborted = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def write(self, _chunk):
            return None

        def abort(self):
            self.aborted = True

    class FakeSoundDevice:
        OutputStream = FakeStream

        @staticmethod
        def query_devices(_index):
            return {"default_samplerate": 8000}

    monkeypatch.setitem(sys.modules, "sounddevice", FakeSoundDevice)
    wav = io.BytesIO()
    with wave.open(wav, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(8000)
        writer.writeframes(b"\0\0" * 5000)
    checks = 0

    def cancelled():
        nonlocal checks
        checks += 1
        return checks > 2

    with pytest.raises(AudioPlaybackCancelled):
        play_wav_bytes_to_device(wav.getvalue(), 1, cancellation=cancelled)


def test_service_routes_requested_agent_speech_only_to_companion(service, monkeypatch) -> None:
    bridge_calls: list[dict] = []
    local_calls: list[str] = []

    async def local_play(item):
        local_calls.append(item.text)
        return 0.0

    service.tts._backend.play = local_play
    monkeypatch.setattr(
        service,
        "_meet_bridge_health",
        lambda timeout=0.5: {
            "meetingUrl": "https://meet.google.com/abc-defg-hij",
            "companionAudio": {"companionReady": True, "lastError": None},
        },
    )
    def bridge(payload, timeout=2.0, *, path="/speech"):
        bridge_calls.append({"path": path, **payload})
        if path == "/speech/status":
            return {
                "ok": True, "id": payload["utterance_id"], "state": "completed",
                "terminal": True, "startedAt": 1.0, "completedAt": 1.2,
            }
        return {"ok": True, "accepted": True, "destination": {"type": "companion"}}

    monkeypatch.setattr(service, "_meet_bridge_speech", bridge)

    queued = service.speak(
        "agent-1",
        "status report",
        destination="companion",
        meeting_url="https://meet.google.com/abc-defg-hij",
    )
    assert queued["destination"]["type"] == "companion"
    asyncio.run(service.tts.process_next())

    assert local_calls == []
    assert bridge_calls[0]["agent_id"] == "agent-1"
    assert bridge_calls[0]["artifact_source"] == "virtual-agent-tts"
    assert bridge_calls[0]["meeting_url"] == "https://meet.google.com/abc-defg-hij"
    assert bridge_calls[1]["path"] == "/speech/status"


def test_service_companion_route_refuses_when_companion_not_ready(service, monkeypatch) -> None:
    monkeypatch.setattr(
        service,
        "_meet_bridge_health",
        lambda timeout=0.5: {
            "meetingUrl": "https://meet.google.com/abc-defg-hij",
            "companionAudio": {
                "companionReady": False,
                "lastError": "companion synthetic microphone is not ready",
            },
        },
    )
    with pytest.raises(ConflictError, match="synthetic microphone"):
        service.speak("agent-1", "hello", destination="companion")


def test_tts_api_returns_conflict_when_companion_is_not_ready(
    client, worker_headers, app_context, monkeypatch
) -> None:
    monkeypatch.setattr(
        app_context.service,
        "_meet_bridge_health",
        lambda timeout=0.5: {
            "meetingUrl": "https://meet.google.com/abc-defg-hij",
            "companionAudio": {
                "companionReady": False,
                "lastError": "companion tab is not attached",
            },
        },
    )
    response = client.post(
        "/ws_collab/v1/tts/speak",
        headers=worker_headers,
        json={"agent_id": "agent-1", "text": "hello", "destination": "companion"},
    )
    assert response.status_code == 409
    assert "not attached" in str(response.json())


def test_legacy_service_speak_keeps_local_destination(service) -> None:
    queued = service.speak("agent-1", "legacy local output")
    assert queued["destination"] == {
        "type": "local",
        "meeting_url": None,
        "companion_ready": False,
    }
    assert service.tts.state()["queue"][0]["destination"] == "local"


def test_config_can_make_companion_the_explicit_default(tmp_path) -> None:
    from conftest import make_config

    config = make_config(
        tmp_path,
        WS_COLLAB_TTS_OUTPUT_DESTINATION="companion",
        WS_COLLAB_COMPANION_AUDIO_QUEUE_MAX="3",
    )
    assert config.tts_output_destination == "companion"
    assert config.companion_audio_queue_max == 3


def test_tts_api_exposes_destination_and_companion_status(
    client, worker_headers, viewer_headers, app_context, monkeypatch
) -> None:
    monkeypatch.setattr(
        app_context.service,
        "_meet_bridge_health",
        lambda timeout=0.5: {
            "meetingUrl": "https://meet.google.com/abc-defg-hij",
            "companionAudio": {
                "destination": "companion",
                "companionReady": True,
                "queued": 0,
                "speaking": False,
                "completed": 3,
                "dropped": 1,
                "rejected": 2,
            },
        },
    )
    response = client.post(
        "/ws_collab/v1/tts/speak",
        headers=worker_headers,
        json={
            "agent_id": "agent-api",
            "text": "hello meeting",
            "destination": "companion",
            "meeting_url": "https://meet.google.com/abc-defg-hij",
        },
    )
    assert response.status_code == 200
    assert response.json()["destination"]["type"] == "companion"

    state = client.get("/ws_collab/v1/tts", headers=viewer_headers)
    assert state.status_code == 200
    companion = state.json()["destinations"]["companion"]
    assert companion["companionReady"] is True
    assert companion["completed"] == 3
    assert companion["dropped"] == 1


def _ready_service_companion(service, monkeypatch) -> None:
    monkeypatch.setattr(
        service,
        "_meet_bridge_health",
        lambda timeout=0.5: {
            "meetingUrl": "https://meet.google.com/abc-defg-hij",
            "companionAudio": {"companionReady": True},
        },
    )


def test_companion_tts_waits_for_remote_completion_before_finished(service, monkeypatch) -> None:
    accepted = threading.Event()
    release = threading.Event()
    _ready_service_companion(service, monkeypatch)

    def bridge(payload, timeout=2.0, *, path="/speech"):
        if path == "/speech":
            accepted.set()
            return {"ok": True, "accepted": True, "id": payload["utterance_id"]}
        assert path == "/speech/status"
        release.wait(1)
        return {
            "ok": True, "id": payload["utterance_id"], "state": "completed",
            "terminal": True, "startedAt": 10.0, "completedAt": 10.25,
        }

    monkeypatch.setattr(service, "_meet_bridge_speech", bridge)
    queued = service.speak(
        "agent-1", "wait for playback", destination="companion",
        meeting_url="https://meet.google.com/abc-defg-hij",
    )

    async def run():
        task = asyncio.create_task(service.tts.process_next())
        assert await asyncio.to_thread(accepted.wait, 1)
        await asyncio.sleep(0.02)
        events = service.read_events("tts_queue", limit=100)["events"]
        assert not any(
            event["type"] == "TTS_FINISHED" and event["data"]["id"] == queued["id"]
            for event in events
        )
        assert service.tts.state()["current"]["id"] == queued["id"]
        release.set()
        assert await task

    asyncio.run(run())
    events = service.read_events("tts_queue", limit=100)["events"]
    assert any(
        event["type"] == "TTS_FINISHED" and event["data"]["id"] == queued["id"]
        and event["data"]["error"] is None
        for event in events
    )


@pytest.mark.parametrize(
    ("remote", "error"),
    [
        ({"ok": True, "state": "failed", "terminal": True, "error": "remote device failed"}, "remote device failed"),
    ],
)
def test_companion_tts_remote_failure_and_timeout_are_explicit(
    service, monkeypatch, remote, error
) -> None:
    calls: list[str] = []
    _ready_service_companion(service, monkeypatch)

    def bridge(payload, timeout=2.0, *, path="/speech"):
        calls.append(path)
        if path == "/speech":
            return {"ok": True, "accepted": True, "id": payload["utterance_id"]}
        if path == "/speech/status":
            return {"id": payload["utterance_id"], **remote}
        return {"ok": True, "cancelled": True}

    monkeypatch.setattr(service, "_meet_bridge_speech", bridge)
    queued = service.speak(
        "agent-1", error, destination="companion",
        meeting_url="https://meet.google.com/abc-defg-hij",
    )
    asyncio.run(service.tts.process_next())
    events = service.read_events("tts_queue", limit=100)["events"]
    finished = next(
        event for event in events
        if event["type"] == "TTS_FINISHED" and event["data"]["id"] == queued["id"]
    )
    assert error in finished["data"]["error"]
    if not remote["terminal"]:
        assert calls[-1] == "/speech/cancel"


def test_companion_tts_duration_hint_allows_legitimate_over_45_second_playback(
    service, monkeypatch
) -> None:
    calls: list[tuple[str, dict, float]] = []
    _ready_service_companion(service, monkeypatch)

    def bridge(payload, timeout=2.0, *, path="/speech"):
        calls.append((path, dict(payload), timeout))
        if path == "/speech":
            return {
                "ok": True,
                "accepted": True,
                "id": payload["utterance_id"],
                "estimatedDurationSeconds": 60.0,
                "estimatedQueueDelaySeconds": 8.0,
            }
        return {
            "ok": True,
            "id": payload["utterance_id"],
            "state": "completed",
            "terminal": True,
            "startedAt": 10.0,
            "completedAt": 70.0,
            "audioDurationSeconds": 60.0,
        }

    monkeypatch.setattr(service, "_meet_bridge_speech", bridge)
    service.set_voice_profile("agent-1", {"rate": 0.25})
    queued = service.speak(
        "agent-1",
        "legitimate long slow utterance",
        destination="companion",
        meeting_url="https://meet.google.com/abc-defg-hij",
    )
    asyncio.run(service.tts.process_next())

    status_calls = [entry for entry in calls if entry[0] == "/speech/status"]
    assert status_calls
    assert all(0 < entry[1]["wait_seconds"] <= 10 for entry in status_calls)
    events = service.read_events("tts_queue", limit=100)["events"]
    finished = next(
        event for event in events
        if event["type"] == "TTS_FINISHED" and event["data"]["id"] == queued["id"]
    )
    assert finished["data"]["duration_s"] == 60.0
    assert finished["data"]["error"] is None


def test_companion_tts_stuck_playing_times_out_at_firm_cap(
    service, monkeypatch
) -> None:
    from ws_collab import service as service_mod

    calls: list[str] = []
    _ready_service_companion(service, monkeypatch)
    monkeypatch.setattr(service_mod, "_COMPANION_TTS_BASE_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(service_mod, "_COMPANION_TTS_SAFETY_CAP_SECONDS", 0.2)
    monkeypatch.setattr(service_mod, "_COMPANION_TTS_STATUS_POLL_SECONDS", 0.05)
    monkeypatch.setattr(service_mod, "_COMPANION_TTS_DURATION_GRACE_SECONDS", 0.01)

    def bridge(payload, timeout=2.0, *, path="/speech"):
        calls.append(path)
        if path == "/speech":
            return {"ok": True, "accepted": True, "id": payload["utterance_id"]}
        if path == "/speech/status":
            return {
                "ok": True,
                "id": payload["utterance_id"],
                "state": "playing",
                "terminal": False,
                "startedAt": 1.0,
            }
        return {"ok": True, "cancelled": True}

    monkeypatch.setattr(service, "_meet_bridge_speech", bridge)
    queued = service.speak(
        "agent-1",
        "stuck playback",
        destination="companion",
        meeting_url="https://meet.google.com/abc-defg-hij",
    )
    started = time.monotonic()
    asyncio.run(service.tts.process_next())
    elapsed = time.monotonic() - started

    assert 0.15 <= elapsed < 1.0
    assert calls[-1] == "/speech/cancel"
    events = service.read_events("tts_queue", limit=100)["events"]
    finished = next(
        event for event in events
        if event["type"] == "TTS_FINISHED" and event["data"]["id"] == queued["id"]
    )
    assert "acknowledgement timed out" in finished["data"]["error"]


def test_interrupt_cancels_handed_off_companion_utterance(service, monkeypatch) -> None:
    playing = threading.Event()
    cancelled = threading.Event()
    paths: list[tuple[str, str]] = []
    _ready_service_companion(service, monkeypatch)

    def bridge(payload, timeout=2.0, *, path="/speech"):
        utterance_id = payload["utterance_id"]
        paths.append((path, utterance_id))
        if path == "/speech":
            return {"ok": True, "accepted": True, "id": utterance_id}
        if path == "/speech/cancel":
            cancelled.set()
            return {"ok": True, "cancelled": True}
        playing.set()
        cancelled.wait(1)
        return {"ok": True, "id": utterance_id, "state": "cancelled", "terminal": True}

    monkeypatch.setattr(service, "_meet_bridge_speech", bridge)
    first = service.speak(
        "agent-1", "long speech", destination="companion",
        meeting_url="https://meet.google.com/abc-defg-hij",
    )

    async def run():
        task = asyncio.create_task(service.tts.process_next())
        assert await asyncio.to_thread(playing.wait, 1)
        second = service.speak(
            "agent-2", "interrupt", destination="companion",
            meeting_url="https://meet.google.com/abc-defg-hij",
            interrupt=True,
        )
        assert await asyncio.to_thread(cancelled.wait, 1)
        await task
        return second

    second = asyncio.run(run())
    events = service.read_events("tts_queue", limit=100)["events"]
    assert any(
        event["type"] == "TTS_CANCELLED" and event["data"]["id"] == first["id"]
        for event in events
    )
    assert ("/speech/cancel", first["id"]) in paths
    assert second["id"]


def test_meeting_switch_cancellation_reaches_tts_lifecycle(service, monkeypatch) -> None:
    started = threading.Event()
    ready = _ready_state()

    def playback(_item, cancel):
        started.set()
        cancel.wait(1)

    arbiter = CompanionAudioArbiter(
        lambda _target: dict(ready), playback, lambda _reason: None
    )
    arbiter.start()
    _ready_service_companion(service, monkeypatch)

    def bridge(payload, timeout=2.0, *, path="/speech"):
        if path == "/speech":
            return arbiter.submit(
                kind="speech",
                text=payload["text"],
                meeting_url=payload["meeting_url"],
                metadata={"utterance_id": payload["utterance_id"]},
            )
        if path == "/speech/status":
            return arbiter.utterance_status(
                payload["utterance_id"], wait_seconds=payload["wait_seconds"]
            )
        return {"ok": True, "cancelled": arbiter.cancel(payload["utterance_id"])}

    monkeypatch.setattr(service, "_meet_bridge_speech", bridge)
    queued = service.speak(
        "agent-1", "switch me", destination="companion",
        meeting_url=ready["meetingUrl"],
    )

    async def run():
        task = asyncio.create_task(service.tts.process_next())
        assert await asyncio.to_thread(started.wait, 1)
        arbiter.invalidate("meeting switch")
        await task

    asyncio.run(run())
    remote = arbiter.utterance_status(queued["id"])
    events = service.read_events("tts_queue", limit=100)["events"]
    assert remote["state"] == "cancelled"
    assert arbiter.status()["cancelled"] == 1
    assert arbiter.status()["completed"] == 0
    assert any(
        event["type"] == "TTS_CANCELLED" and event["data"]["id"] == queued["id"]
        for event in events
    )
    arbiter.stop()
