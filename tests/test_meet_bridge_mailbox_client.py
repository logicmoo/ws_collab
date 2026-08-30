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

    assert client._call("/v1/status") == {"ok": True}
    assert captured["request"].get_header("Authorization") == "Bearer worker-token"
    assert captured["timeout"] == 3.0


def test_final_caption_is_ingested_as_google_meet(monkeypatch) -> None:
    client = MailboxClient(token="worker-token")
    captured = {}

    def call(path, *, method="GET", body=None):
        captured.update(path=path, method=method, body=body)
        return {"ok": True}

    monkeypatch.setattr(client, "_call", call)

    result = client.ingest_transcript(
        "A complete Meet caption.",
        correlation_id="meet-caption:room:key",
        source_kind="operator",
        audio_meta={"speaker": "Douglas", "final": True},
    )

    assert result == {"ok": True}
    assert captured["path"] == "/v1/stt/ingest"
    assert captured["method"] == "POST"
    assert captured["body"]["engine"] == "google_meet"
    assert captured["body"]["text"] == "A complete Meet caption."
    assert captured["body"]["is_final"] is True
