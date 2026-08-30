from __future__ import annotations

import asyncio
import base64
import json
import queue
import subprocess

import numpy as np

from ws_collab.meet_bridge.bridge import forward_companion_heard_audio
from ws_collab.meet_bridge.scripts_js import COMPANION_AUDIO_TAP_JS
from ws_collab.stt.base import Hypothesis


def test_secondary_capture_state_defaults(service) -> None:
    state = service.secondary_capture_state()
    assert state["listening"] is False
    assert state["device_id"] == ""
    assert state["source_kind"] == "companion_heard"
    assert state["audio_source"] == "companion_heard_meeting_audio"


def test_secondary_capture_rest_start_stop(
    client, admin_headers, worker_headers, viewer_headers, app_context
) -> None:
    device_id = next(d["id"] for d in app_context.service.list_devices()["devices"] if d["direction"] in ("input", "loopback", "virtual"))
    started = client.post("/ws_collab/v1/audio/secondary-capture/start", headers=admin_headers, json={"device_id": device_id})
    assert started.status_code == 200
    assert started.json()["device_id"] == device_id
    browser = client.post(
        "/ws_collab/v1/audio/secondary-capture/browser",
        headers=admin_headers,
        json={
            "stream_id": "remote-track-rest",
            "sample_rate": 16000,
            "connected": True,
            "muted": True,
            "chunks": [],
        },
    )
    assert browser.status_code == 200
    assert browser.json()["input_mode"] == "browser"
    assert browser.json()["browser_connected"] is True
    worker = client.post(
        "/ws_collab/v1/audio/secondary-capture/browser",
        headers=worker_headers,
        json={
            "stream_id": "remote-track-worker",
            "sample_rate": 16000,
            "connected": True,
            "muted": True,
            "chunks": [],
        },
    )
    assert worker.status_code == 200
    assert client.post(
        "/ws_collab/v1/audio/secondary-capture/browser",
        headers=viewer_headers,
        json={"sample_rate": 16000, "chunks": []},
    ).status_code == 403
    assert client.post(
        "/ws_collab/v1/audio/secondary-capture/browser",
        headers={"Authorization": "Bearer wrong-token"},
        json={"sample_rate": 16000, "chunks": []},
    ).status_code == 401
    assert client.post(
        "/ws_collab/v1/audio/secondary-capture/start",
        headers=worker_headers,
        json={"device_id": device_id},
    ).status_code == 403
    too_many_chunks = client.post(
        "/ws_collab/v1/audio/secondary-capture/browser",
        headers=worker_headers,
        json={
            "sample_rate": 16000,
            "connected": True,
            "chunks": [{}] * 97,
        },
    )
    assert too_many_chunks.status_code == 400
    assert "at most 96" in str(too_many_chunks.json())
    stopped = client.post("/ws_collab/v1/audio/secondary-capture/stop", headers=admin_headers)
    assert stopped.status_code == 200
    assert stopped.json()["listening"] is False


def _browser_chunk(samples: np.ndarray, *, captured_at: float = 1.0) -> dict:
    raw = (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()
    return {
        "pcm_s16le_base64": base64.b64encode(raw).decode("ascii"),
        "frames": int(samples.size),
        "bytes": len(raw),
        "capturedAt": captured_at,
    }


def _disable_secondary_worker(secondary) -> None:
    def start(rate: int, stream_id: str) -> None:
        secondary._device_id = "browser:meet-companion-incoming"
        secondary._input_mode = "browser"
        secondary._listening = True
        secondary._browser_sample_rate = rate
        secondary._browser_stream_id = stream_id

    secondary._start_browser_worker = start


def test_browser_pcm_reaches_shared_secondary_pipeline_and_non_meet_engines(service) -> None:
    seen: dict[str, object] = {}

    class RecordingEngine:
        model = "test"
        is_remote = False

        def __init__(self, name: str):
            self.name = name

        async def transcribe(self, segment, on_partial=None):
            seen[self.name] = segment
            return Hypothesis(
                engine=self.name,
                model=self.model,
                raw_text="remote meeting speech",
                normalized_text="remote meeting speech",
                confidence=0.9,
            )

    service.stt_engines = [
        RecordingEngine("google_meet"),
        RecordingEngine("whisper:tiny.en"),
        RecordingEngine("vosk"),
    ]
    secondary = service.secondary_capture
    _disable_secondary_worker(secondary)
    samples = np.linspace(-0.25, 0.25, 800, dtype="float32")

    async def run() -> None:
        secondary.bind_loop(asyncio.get_running_loop())
        state = secondary.ingest_browser_audio({
            "stream_id": "remote-track-1",
            "sample_rate": 16000,
            "connected": True,
            "muted": True,
            "chunks": [_browser_chunk(samples)],
        })
        assert state["chunks_forwarded"] == 1
        queued = secondary._frames.get_nowait()[0]
        secondary._dispatch_live_segment(queued, 16000)
        await asyncio.sleep(0.05)

    asyncio.run(run())

    assert set(seen) == {"whisper:tiny.en", "vosk"}
    segment = seen["whisper:tiny.en"]
    assert segment.source_kind == "companion_heard"
    assert segment.route["audio_source"] == "companion_heard_meeting_audio"
    assert segment.route["capture"] == "secondary"
    assert segment.route["self_audio_exclusion"] == "remote-media-stream-only"
    assert service.secondary_capture_state()["segments_forwarded"] == 1


def test_secondary_browser_buffer_is_bounded_and_tracks_disconnect_reconnect(service) -> None:
    secondary = service.secondary_capture
    _disable_secondary_worker(secondary)
    secondary._frames = queue.Queue(maxsize=1)
    chunk = _browser_chunk(np.ones(160, dtype="float32") * 0.1)
    base = {
        "stream_id": "remote-track-1",
        "sample_rate": 16000,
        "connected": True,
        "muted": True,
    }

    secondary.ingest_browser_audio({**base, "chunks": [chunk]})
    bounded = secondary.ingest_browser_audio({**base, "chunks": [chunk]})
    secondary._frames = queue.Queue(maxsize=1)
    disconnected = secondary.ingest_browser_audio({**base, "connected": False, "chunks": []})
    reconnected = secondary.ingest_browser_audio({**base, "connected": True, "chunks": []})

    assert reconnected["queued_chunks"] == 1
    assert bounded["dropped_frames"] == 1
    assert bounded["dropped_chunks"] == 1
    assert bounded["dropped_bytes"] == 320
    assert disconnected["browser_disconnects"] == 1
    assert reconnected["browser_reconnects"] == 1
    assert reconnected["browser_connected"] is True


def test_bridge_suppresses_synthetic_artifacts_and_preserves_audio_source_status() -> None:
    artifact = _browser_chunk(np.ones(8, dtype="float32") * 0.1, captured_at=1500)
    meeting = _browser_chunk(np.ones(8, dtype="float32") * 0.2, captured_at=4000)

    class Tab:
        def evaluate(self, *_args, **_kwargs):
            return json.dumps({
                "ok": True,
                "status": "capturing-muted-remote-stream",
                "connected": True,
                "muted": True,
                "streamId": "remote-track-1",
                "sampleRate": 16000,
                "chunks": [artifact, meeting],
                "capturedChunks": 2,
                "capturedFrames": 16,
                "capturedBytes": 32,
                "droppedChunks": 0,
                "disconnects": 0,
                "reconnects": 0,
            })

    class Mailbox:
        request = None

        def ingest_companion_browser_audio(self, payload):
            self.request = payload
            return {
                "listening": True,
                "live_capture": True,
                "chunks_forwarded": 1,
                "frames_forwarded": 8,
                "bytes_forwarded": 16,
                "error": None,
            }

    mailbox = Mailbox()
    holder = {
        "companion_heard_stt_enabled": True,
        "companion_click_artifact_until": 0.0,
        "companion_say_artifact_started_at": 1.0,
        "companion_say_artifact_until": 2.0,
        "companion_say_artifact": {
            "id": "tts-1",
            "source": "virtual-agent-tts",
            "agentId": "agent-1",
            "expectedText": "hello",
        },
        "speaking_until": 0.0,
    }
    status: dict = {}
    result = forward_companion_heard_audio(Tab(), mailbox, holder, status)

    assert mailbox.request["chunks"] == [meeting]
    assert mailbox.request["suppressed_artifact_chunks"] == 1
    assert mailbox.request["suppression_artifact"]["source"] == "virtual-agent-tts"
    assert result["audioSource"] == "companion_heard_meeting_audio"
    assert result["artifactChunksSuppressed"] == 1
    assert result["lastSuppressionArtifact"]["id"] == "tts-1"
    assert result["mediaElementsMuted"] is True
    assert result["serverCapture"]["chunks_forwarded"] == 1


def test_bridge_transport_failure_counts_drained_chunks_as_dropped() -> None:
    chunk = _browser_chunk(np.ones(8, dtype="float32") * 0.2, captured_at=4000)

    class Tab:
        def evaluate(self, *_args, **_kwargs):
            return json.dumps({
                "connected": True,
                "muted": True,
                "streamId": "remote-track-1",
                "sampleRate": 16000,
                "chunks": [chunk],
            })

    class OfflineMailbox:
        def ingest_companion_browser_audio(self, _payload):
            raise ConnectionError("server offline")

    result = forward_companion_heard_audio(
        Tab(),
        OfflineMailbox(),
        {"companion_heard_stt_enabled": True},
        {},
    )

    assert result["captureLive"] is False
    assert result["transportChunksDropped"] == 1
    assert result["chunksDropped"] == 1
    assert result["lastError"] == "server offline"


def test_muted_media_element_still_produces_companion_audio_chunks() -> None:
    script = f"""
const tap = {json.dumps(COMPANION_AUDIO_TAP_JS)};
let tracks = [{{ id: "remote-1", readyState: "live" }}];
const media = {{ muted: false, volume: 1, srcObject: {{ getAudioTracks: () => tracks }} }};
global.window = global;
global.document = {{ querySelectorAll: () => [media] }};
global.MediaStream = class {{ constructor(tracks) {{ this.tracks = tracks; }} }};
global.btoa = global.btoa || ((value) => Buffer.from(value, "binary").toString("base64"));
class AudioContext {{
  constructor() {{ this.sampleRate = 48000; this.state = "running"; this.destination = {{}}; }}
  createMediaStreamSource() {{ return {{ connect() {{}}, disconnect() {{}} }}; }}
  createScriptProcessor() {{ return {{ connect() {{}}, disconnect() {{}}, onaudioprocess: null }}; }}
  createGain() {{ return {{ gain: {{ value: 1 }}, connect() {{}}, disconnect() {{}} }}; }}
  async resume() {{ this.state = "running"; }}
}}
global.AudioContext = AudioContext;
(async () => {{
  const first = JSON.parse(await eval(tap));
  for (let i = 0; i < 65; i += 1) {{
    window.__wsCollabCompanionAudioTap.processor.onaudioprocess({{
      inputBuffer: {{
        length: 4,
        numberOfChannels: 1,
        getChannelData: () => new Float32Array([0.1, -0.1, 0.2, -0.2]),
      }},
    }});
  }}
  const second = JSON.parse(await eval(tap));
  tracks = [];
  const disconnected = JSON.parse(await eval(tap));
  tracks = [{{ id: "remote-2", readyState: "live" }}];
  const reconnected = JSON.parse(await eval(tap));
  console.log(JSON.stringify({{ first, second, disconnected, reconnected, muted: media.muted, volume: media.volume }}));
}})().catch((error) => {{ console.error(error); process.exit(1); }});
"""
    result = subprocess.run(["node", "-e", script], check=True, text=True, capture_output=True)
    payload = json.loads(result.stdout)

    assert payload["first"]["connected"] is True
    assert payload["muted"] is True
    assert payload["volume"] == 0
    assert payload["second"]["chunks"][0]["frames"] == 4
    assert len(payload["second"]["chunks"]) == 64
    assert payload["second"]["droppedChunks"] == 1
    assert payload["disconnected"]["connected"] is False
    assert payload["disconnected"]["disconnects"] == 1
    assert payload["reconnected"]["connected"] is True
    assert payload["reconnected"]["reconnects"] == 1
