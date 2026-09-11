from __future__ import annotations

import json

from ws_collab.meet_bridge.mailbox_client import MailboxClient


def test_calls_use_bearer_token(monkeypatch) -> None:
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return json.dumps({"ok": True}).encode()

    def urlopen(request, *, timeout):
        captured.update(request=request, timeout=timeout)
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    client = MailboxClient(token="worker-token", timeout=3.0)

    assert client._call("/status") == {"ok": True}
    assert captured["request"].get_header("Authorization") == "Bearer worker-token"
    assert captured["timeout"] == 3.0


def test_final_caption_is_ingested_as_meeting_context(monkeypatch) -> None:
    client = MailboxClient(token="worker-token")
    captured = {}

    def call(path, *, method="GET", body=None):
        captured.update(path=path, method=method, body=body)
        return {"ok": True}

    monkeypatch.setattr(client, "_call", call)

    result = client.ingest_meeting_caption(
        "A complete Meet caption.",
        correlation_id="meet-caption:room:key",
        metadata={
            "speaker": "Douglas",
            "role": "host",
            "meetingUrl": "https://meet.google.com/abc-defg-hij",
            "key": "host:key",
            "final": True,
        },
    )

    assert result == {"ok": True}
    assert captured["path"] == "/meet/captions/ingest"
    assert captured["method"] == "POST"
    assert captured["body"]["text"] == "A complete Meet caption."
    assert captured["body"]["final"] is True
    assert captured["body"]["speaker"] == "Douglas"


def test_secondary_capture_start_uses_audio_endpoint(monkeypatch) -> None:
    client = MailboxClient(token="worker-token")
    captured = {}

    def call(path, *, method="GET", body=None):
        captured.update(path=path, method=method, body=body)
        return {"listening": True, "device_id": "dev-1"}

    monkeypatch.setattr(client, "_call", call)

    result = client.start_secondary_capture("dev-1")

    assert result["listening"] is True
    assert captured["path"] == "/audio/secondary-capture/start"
    assert captured["method"] == "POST"
    assert captured["body"] == {"device_id": "dev-1"}


def test_companion_wiring_runtime_request_is_meeting_scoped(monkeypatch) -> None:
    client = MailboxClient(token="worker-token")
    captured = {}

    def call(path, *, method="GET", body=None):
        captured.update(path=path, method=method, body=body)
        return {"validation": {"valid": True}}

    monkeypatch.setattr(client, "_call", call)
    meeting = "https://meet.google.com/abc-defg-hij"

    assert client.companion_cable_wiring(meeting)["validation"]["valid"] is True
    assert captured["path"] == (
        "/meet/companion-cable-wiring/runtime"
        "?meeting_url=https%3A%2F%2Fmeet.google.com%2Fabc-defg-hij"
    )


def test_companion_browser_audio_uses_shared_secondary_endpoint(monkeypatch) -> None:
    client = MailboxClient(token="worker-token")
    captured = {}

    def call(path, *, method="GET", body=None):
        captured.update(path=path, method=method, body=body)
        return {"browser_connected": True}

    monkeypatch.setattr(client, "_call", call)
    payload = {"sample_rate": 48000, "connected": True, "muted": True, "chunks": []}

    result = client.ingest_companion_browser_audio(payload)

    assert result["browser_connected"] is True
    assert captured == {
        "path": "/audio/secondary-capture/browser",
        "method": "POST",
        "body": payload,
    }
