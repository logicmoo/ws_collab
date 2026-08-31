from __future__ import annotations

import json
import shutil
import subprocess

import pytest

import ws_collab.meet_bridge.bridge as bridge_module
from ws_collab.companion_wiring import (
    build_wiring_config,
    normalize_device_label,
    resolve_exact_normalized_label,
)
from ws_collab.meet_bridge.bridge import (
    bridge_worker_request_error,
    companion_wiring_is_suppressed,
    companion_wiring_operation_key,
    companion_click_trigger_decision,
    disconnect_companion_audio_wiring,
    saved_companion_wiring_config,
    wire_companion_audio,
)
from ws_collab.meet_bridge.scripts_js import (
    COMPANION_CABLE_FAIL_CLOSED_JS,
    COMPANION_CABLE_FINALIZE_JS,
    COMPANION_CABLE_PREPARE_JS,
)


@pytest.fixture(autouse=True)
def _verify_fake_output(monkeypatch):
    monkeypatch.setattr(
        bridge_module,
        "verify_audio_device_identity",
        lambda index, **_kwargs: {"index": index, "name": "Cable B Input", "hostApi": "Windows WASAPI"},
    )


def _devices() -> list[dict]:
    rows = []
    for index, (name, supports_input, supports_output) in enumerate(
        (
            ("Cable A Input (Virtual)", False, True),
            ("Cable A Output (Virtual)", True, False),
            ("Cable B Input (Virtual)", False, True),
            ("Cable B Output (Virtual)", True, False),
        )
    ):
        rows.append(
            {
                "id": f"dev-{index}",
                "name": name,
                "host_api": "Windows WASAPI",
                "backend_index": index + 10,
                "supports_input": supports_input,
                "supports_output": supports_output,
                "classes": ["virtual"],
            }
        )
    return rows


def _config() -> dict:
    return build_wiring_config(
        {
            "receive_playback_sink": "dev-0",
            "receive_capture_input": "dev-1",
            "transmit_tts_output": "dev-2",
            "transmit_companion_mic": "dev-3",
        },
        _devices(),
    )


def test_exact_browser_label_resolution_never_guesses() -> None:
    devices = [
        {"kind": "audiooutput", "label": " CABLE Input   (VB-Audio) ", "deviceId": "one"},
        {"kind": "audioinput", "label": "CABLE Input (VB-Audio)", "deviceId": "mic"},
    ]
    assert normalize_device_label("CABLE INPUT (VB-Audio)") == "cable input (vb-audio)"
    assert (
        resolve_exact_normalized_label(
            devices, "cable input (vb-audio)", kind="audiooutput"
        )["deviceId"]
        == "one"
    )
    with pytest.raises(ValueError, match="no audiooutput"):
        resolve_exact_normalized_label(devices, "missing", kind="audiooutput")
    devices.append(
        {"kind": "audiooutput", "label": "cable input (vb-audio)", "deviceId": "two"}
    )
    with pytest.raises(ValueError, match="2 audiooutput"):
        resolve_exact_normalized_label(
            devices, "CABLE Input (VB-Audio)", kind="audiooutput"
        )


def test_receive_and_transmit_same_pair_is_rejected() -> None:
    with pytest.raises(Exception, match="same virtual cable pair"):
        build_wiring_config(
            {
                "receive_playback_sink": "dev-0",
                "receive_capture_input": "dev-1",
                "transmit_tts_output": "dev-0",
                "transmit_companion_mic": "dev-1",
            },
            _devices(),
        )
    assert _config()["transmit_tts_output"]["serverIndex"] == 12


def test_two_distinct_voicemeeter_buses_are_valid() -> None:
    devices = []
    for index, (name, is_input) in enumerate(
        (
            ("VoiceMeeter Input (VB-Audio VoiceMeeter VAIO)", False),
            ("VoiceMeeter Output (VB-Audio VoiceMeeter VAIO)", True),
            ("VoiceMeeter AUX Input (VB-Audio VoiceMeeter AUX VAIO)", False),
            ("VoiceMeeter AUX Output (VB-Audio VoiceMeeter AUX VAIO)", True),
        )
    ):
        devices.append(
            {
                "id": f"vm-{index}",
                "name": name,
                "host_api": "Windows WASAPI",
                "backend_index": index,
                "supports_input": is_input,
                "supports_output": not is_input,
                "classes": ["virtual"],
            }
        )
    config = build_wiring_config(
        {
            "receive_playback_sink": "vm-0",
            "receive_capture_input": "vm-1",
            "transmit_tts_output": "vm-2",
            "transmit_companion_mic": "vm-3",
        },
        devices,
    )
    assert config["receive_playback_sink"]["pairIdentity"] == "voicemeeter:vaio"
    assert config["transmit_companion_mic"]["pairIdentity"] == "voicemeeter:aux"


def test_crossed_cable_halves_are_rejected() -> None:
    with pytest.raises(Exception, match="RECEIVE playback and recording endpoints are crossed"):
        build_wiring_config(
            {
                "receive_playback_sink": "dev-0",
                "receive_capture_input": "dev-3",
                "transmit_tts_output": "dev-2",
                "transmit_companion_mic": "dev-1",
            },
            _devices(),
        )


def test_ambiguous_virtual_labels_are_rejected_with_selection_guidance() -> None:
    devices = _devices()
    for index, device in enumerate(devices):
        device["name"] = (
            f"Unrelated virtual {'speaker' if device['supports_output'] else 'microphone'} {index}"
        )
    with pytest.raises(Exception, match="select endpoints whose labels include"):
        build_wiring_config(
            {
                "receive_playback_sink": "dev-0",
                "receive_capture_input": "dev-1",
                "transmit_tts_output": "dev-2",
                "transmit_companion_mic": "dev-3",
            },
            devices,
        )


class _Tab:
    def __init__(self, events: list[str], *, fail_prepare: bool = False, fail_finalize: bool = False):
        self.events = events
        self.fail_prepare = fail_prepare
        self.fail_finalize = fail_finalize

    def evaluate(self, script, **_kwargs):
        if "capture-pending" in script:
            self.events.append("muted-sink-mic")
            return json.dumps(
                {
                    "ok": not self.fail_prepare,
                    "error": "prepare failed" if self.fail_prepare else None,
                    "sink": {"verified": True},
                    "mic": {"verified": True},
                    "browserDevices": [],
                }
            )
        if "state.phase = \"wired\"" in script:
            self.events.append("unmute")
            return json.dumps(
                {
                    "ok": not self.fail_finalize,
                    "error": "finalize failed" if self.fail_finalize else None,
                    "sink": {"actualLabel": "Cable A Input", "verified": True},
                    "mic": {"actualLabel": "Cable B Output", "verified": True},
                }
            )
        self.events.append("mute-fail-closed")
        return json.dumps({"ok": True})


class _Mailbox:
    def __init__(self, events: list[str], *, healthy: bool = True):
        self.events = events
        self.healthy = healthy

    def start_companion_wiring_capture(self, device_id):
        self.events.append("capture")
        return {
            "listening": True,
            "live_capture": self.healthy,
            "input_mode": "device",
            "device_id": device_id,
            "device_name": "Cable A Output",
            "error": None if self.healthy else "capture unavailable",
        }

    def stop_companion_wiring_capture(self):
        self.events.append("capture-stop")
        return {"listening": False}


def test_receive_atomic_order_and_idempotence() -> None:
    events: list[str] = []
    holder: dict = {}
    status: dict = {}
    result = wire_companion_audio(
        _Tab(events),
        _Mailbox(events),
        holder,
        status,
        _config(),
        meeting_url="https://meet.google.com/abc-defg-hij",
        tab_id="tab-1",
        reason="manual",
    )
    assert result["ok"] is True
    assert events == ["muted-sink-mic", "capture", "unmute"]
    assert result["phase"] == "wired"
    assert result["detectorSource"] == "receive-cable"
    again = wire_companion_audio(
        _Tab(events),
        _Mailbox(events),
        holder,
        status,
        _config(),
        meeting_url="https://meet.google.com/abc-defg-hij",
        tab_id="tab-1",
        reason="manual",
    )
    assert again["idempotent"] is True
    assert events == ["muted-sink-mic", "capture", "unmute"]


@pytest.mark.parametrize("fail_at", ["prepare", "capture", "finalize"])
def test_atomic_failure_stays_muted_and_stops_capture(fail_at: str) -> None:
    events: list[str] = []
    result = wire_companion_audio(
        _Tab(
            events,
            fail_prepare=fail_at == "prepare",
            fail_finalize=fail_at == "finalize",
        ),
        _Mailbox(events, healthy=fail_at != "capture"),
        {},
        {},
        _config(),
        meeting_url="https://meet.google.com/abc-defg-hij",
        tab_id="tab-1",
        reason="manual",
    )
    assert result["ok"] is False
    assert result["phase"] == "failed"
    assert result["feedbackSafeMuted"] is True
    assert "mute-fail-closed" in events
    assert "capture-stop" in events
    if fail_at != "finalize":
        assert "unmute" not in events


def test_audio_silence_is_not_due_when_cable_is_unwired() -> None:
    holder = {
        "host_active_caption_key": "row",
        "host_active_caption_started_at": 1.0,
        "host_active_caption_last_growth_at": 2.0,
        "companion_click_after_seconds": 1.0,
        "companion_click_silence_ms": 100.0,
        "companion_click_min_gap_seconds": 0.1,
        "companion_click_max_wait_seconds": 1.0,
        "companion_click_trigger": "audio",
    }
    decision = companion_click_trigger_decision(
        holder,
        now=20.0,
        audio_probe={"ok": False, "source": "receive-cable", "status": "not-ready"},
    )
    assert decision["due"] is False
    assert decision["trigger"] == "audio-not-ready"


def test_browser_wiring_scripts_are_exact_and_fail_closed() -> None:
    assert 'device.kind === kind && normalize(device.label) === wanted' in COMPANION_CABLE_PREPARE_JS
    assert 'deviceId: { exact: mic.deviceId }' in COMPANION_CABLE_PREPARE_JS
    assert "await senders[0].replaceTrack(micTrack)" in COMPANION_CABLE_PREPARE_JS
    assert "requireCurrent();" in COMPANION_CABLE_PREPARE_JS
    assert "__wsCollabCableGeneration" in COMPANION_CABLE_PREPARE_JS
    assert "__wsCollabCableGeneration" in COMPANION_CABLE_FINALIZE_JS
    assert 'element.sinkId !== state.sinkId' in COMPANION_CABLE_FINALIZE_JS
    assert "element.muted = false" in COMPANION_CABLE_FINALIZE_JS


def test_delayed_browser_wiring_cannot_reverse_disconnect(tmp_path) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the browser promise race test")
    config = _config()
    prepare = COMPANION_CABLE_PREPARE_JS.replace(
        "__WS_CONFIG__", json.dumps(config, separators=(",", ":"))
    )
    script = f"""
global.window = global;
let resolveDevices;
let replaceCalls = 0;
const remoteTrack = {{ kind: "audio", readyState: "live" }};
const media = {{
  muted: false, volume: 1, isConnected: true, sinkId: "",
  srcObject: {{ getAudioTracks: () => [remoteTrack] }},
  setSinkId: async function(id) {{ this.sinkId = id; }},
}};
global.document = {{ querySelectorAll: () => [media] }};
Object.defineProperty(global, "navigator", {{ configurable: true, value: {{ mediaDevices: {{
  enumerateDevices: () => new Promise((resolve) => {{ resolveDevices = resolve; }}),
}} }} }});
global.__wsCollabRealGetUserMedia = async () => {{ throw new Error("must not run"); }};
global.__wsCollabOutboundSenders = new Set([{{ track: remoteTrack, replaceTrack: async () => {{ replaceCalls += 1; }} }}]);
(async () => {{
  const pending = eval({json.dumps(prepare)});
  eval({json.dumps(COMPANION_CABLE_FAIL_CLOSED_JS)});
  resolveDevices([
    {{kind: "audiooutput", deviceId: "sink", label: {json.dumps(config["receive_playback_sink"]["label"])}}},
    {{kind: "audioinput", deviceId: "mic", label: {json.dumps(config["transmit_companion_mic"]["label"])}}},
  ]);
  const result = JSON.parse(await pending);
  const state = global.__wsCollabCableWiring;
  if (!result.stale || replaceCalls !== 0 || !media.muted || media.volume !== 0 ||
      state.phase !== "disconnected" || state.feedbackSafeMuted !== true) {{
    throw new Error(JSON.stringify({{result, replaceCalls, muted: media.muted, volume: media.volume, state}}));
  }}
}})().catch((error) => {{ console.error(error); process.exitCode = 1; }});
"""
    path = tmp_path / "wiring-race.js"
    path.write_text(script, encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(path)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    result = subprocess.run(
        [node, str(path)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_bridge_wiring_worker_auth_origin_and_saved_config_boundary() -> None:
    secret = "worker-only-secret"
    assert bridge_worker_request_error({}, secret) == (
        401,
        "missing or invalid bridge worker credential",
    )
    assert bridge_worker_request_error(
        {"authorization": f"Bearer {secret}", "origin": "https://evil.example"},
        secret,
    )[0] == 403
    assert bridge_worker_request_error(
        {"authorization": f"Bearer {secret}"}, secret
    ) is None

    class SavedMailbox:
        def companion_cable_wiring(self):
            return {"config": _config(), "validation": {"valid": True, "errors": []}}

    assert saved_companion_wiring_config(
        SavedMailbox(), {"meeting_url": "https://meet.google.com/abc-defg-hij"}
    ) == _config()
    with pytest.raises(ValueError, match="save device selections through the main server"):
        saved_companion_wiring_config(SavedMailbox(), {"config": _config()})
    assert secret not in str(
        bridge_worker_request_error({"authorization": "Bearer wrong"}, secret)
    )


def test_manual_disconnect_suppression_is_exact_and_manual_wire_clears() -> None:
    config = _config()
    holder = {
        "companion_wiring_desired": config,
        "companion_wiring_tab_id": "tab-1",
        "companion_wiring_meeting_url": "https://meet.google.com/abc-defg-hij",
    }
    disconnect_companion_audio_wiring(
        _Tab([]), _Mailbox([]), holder, {}, reason="manual-disconnect"
    )
    key = companion_wiring_operation_key(
        "tab-1", "https://meet.google.com/abc-defg-hij", config
    )
    assert holder["companion_wiring_manual_suppression"] == key
    assert companion_wiring_is_suppressed(
        holder, "tab-1", "https://meet.google.com/abc-defg-hij", config
    )
    assert not companion_wiring_is_suppressed(
        holder, "tab-2", "https://meet.google.com/abc-defg-hij", config
    )
    changed = json.loads(json.dumps(config))
    changed["version"] = 2
    assert not companion_wiring_is_suppressed(
        holder, "tab-1", "https://meet.google.com/abc-defg-hij", changed
    )
    wire_companion_audio(
        _Tab([]),
        _Mailbox([]),
        holder,
        {},
        config,
        meeting_url="https://meet.google.com/abc-defg-hij",
        tab_id="tab-1",
        reason="manual",
    )
    assert "companion_wiring_manual_suppression" not in holder


def test_wiring_rest_is_narrow_authenticated_and_save_does_not_apply(
    client, admin_headers, worker_headers, viewer_headers, app_context, monkeypatch
) -> None:
    devices = app_context.service.list_devices()["devices"]
    outputs = [row for row in devices if "virtual" in row["classes"] and row["supports_output"]]
    inputs = [row for row in devices if "virtual" in row["classes"] and row["supports_input"]]
    body = {
        "receive_playback_sink": outputs[0]["id"],
        "receive_capture_input": inputs[0]["id"],
        "transmit_tts_output": outputs[1]["id"],
        "transmit_companion_mic": inputs[1]["id"],
    }
    called = []
    monkeypatch.setattr(
        app_context.service,
        "_meet_bridge_wiring",
        lambda payload, **kwargs: called.append((payload, kwargs)) or {"ok": True, "phase": "wired"},
    )
    assert client.get("/ws_collab/v1/meet/companion-cable-wiring", headers=viewer_headers).status_code == 200
    assert client.post("/ws_collab/v1/meet/companion-cable-wiring", headers=viewer_headers, json=body).status_code == 403
    saved = client.post("/ws_collab/v1/meet/companion-cable-wiring", headers=admin_headers, json=body)
    assert saved.status_code == 200
    assert called == []
    assert client.get("/ws_collab/v1/meet/companion-cable-wiring/runtime", headers=worker_headers).status_code == 200
    assert client.post(
        "/ws_collab/v1/meet/companion-cable-wiring/capture/start",
        headers=worker_headers,
        json={"device_id": inputs[1]["id"]},
    ).status_code == 400
    capture = client.post(
        "/ws_collab/v1/meet/companion-cable-wiring/capture/start",
        headers=worker_headers,
        json={"device_id": inputs[0]["id"]},
    )
    assert capture.status_code == 200
    assert capture.json()["device_id"] == inputs[0]["id"]
    assert client.post(
        "/ws_collab/v1/meet/companion-cable-wiring/capture/stop",
        headers=worker_headers,
    ).status_code == 200
    wired = client.post(
        "/ws_collab/v1/meet/companion-cable-wiring/wire",
        headers=admin_headers,
        json={"meeting_url": "https://meet.google.com/abc-defg-hij"},
    )
    assert wired.status_code == 200
    assert called[0][1]["path"] == "/wire-companion-audio"


def test_silences_ui_exposes_directional_four_endpoint_controls() -> None:
    from pathlib import Path

    root = Path(__file__).parents[1] / "src" / "ws_collab" / "admin"
    html = (root / "index.html").read_text(encoding="utf-8")
    app = (root / "app.js").read_text(encoding="utf-8")
    for control in (
        "companion-cable-receive-playback",
        "companion-cable-receive-capture",
        "companion-cable-transmit-output",
        "companion-cable-transmit-mic",
    ):
        assert f'id="{control}"' in html
    assert "Save wiring config" in html
    assert "Re-wire now" in app
    assert "/meet/companion-cable-wiring/wire" in app
