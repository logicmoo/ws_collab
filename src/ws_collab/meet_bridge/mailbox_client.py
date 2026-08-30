"""Native ws_collab mailbox client for the Meet bridge.

Replaces the original design's cross-plugin import (a fallback chain that
reached into a *sibling* plugin's source tree, ``workbench/plugins/
mailbox_chat/src``, and a workbench-wide ``AGENT_MAILBOX_DIR``) with a plain
HTTP client against ws_collab's own ``/v1/mailbox`` REST API. The bridge runs
as its own OS process (it drives a real Chrome over CDP, which cannot share
an event loop with the main asyncio server), so talking to ws_collab over
loopback HTTP -- the same way the admin SPA and every other external
consumer already does -- is the natural fit, not a workaround.

Two honest simplifications versus the original in-process mailbox client:

* ``mailbox_send`` (``POST /v1/mailbox/send``) accepts ``to``/``text``/
  ``sender``/``source_kind`` but no free-form ``metadata`` dict. The original
  attached rich metadata (``key``/``final``/``replaces``/``meetingUrl``) to
  every mailbox message; ws_collab's native send does not have that field, so
  it is folded into the visible text instead of silently dropped -- see
  :func:`send`.
* Polling ``receive`` has no durable cross-restart cursor here (the original
  used a named durable cursor via the mailbox_chat client). This client
  tracks "highest message id seen" purely in memory for the lifetime of one
  bridge process, which is sufficient for a live control channel (/join,
  /new, /say) -- those are one-off commands, not a transcript of record.
  Finalized captions are pushed into ws_collab's STT ingest route as
  ``google_meet`` and resolved through the normal durable transcript pipeline.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

DEFAULT_BASE_URL = "http://127.0.0.1:8802/ws_collab"


class MailboxClient:
    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        timeout: float = 5.0,
        token: str | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.token = token if token is not None else os.environ.get("WS_COLLAB_TOKEN", "")
        self._last_seen_id: dict[str, str | None] = {}

    def _call(self, path: str, *, method: str = "GET", body: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            request.add_header("content-type", "application/json")
        if self.token:
            request.add_header("authorization", f"Bearer {self.token}")
        with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310 - local trusted ws_collab server
            return json.loads(response.read().decode("utf-8"))

    def send(self, to: str, text: str, *, sender: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        """Post a chat-visible line into a ws_collab mailbox/stream.

        `metadata` (if given) is rendered as a trailing bracketed suffix
        rather than silently dropped, since ws_collab's native send has no
        separate metadata field for it.
        """
        line = text
        if metadata:
            interesting = {k: v for k, v in metadata.items() if v is not None and k != "meetingUrl"}
            if interesting:
                line = f"{text} [{', '.join(f'{k}={v}' for k, v in interesting.items())}]"
        try:
            return self._call(
                "/v1/mailbox/send", method="POST",
                body={"to": to, "text": line, "sender": sender, "source_kind": "system"},
            )
        except urllib.error.URLError as error:
            raise ConnectionError(f"ws_collab mailbox send failed ({self.base_url}): {error}") from error

    def receive_new(self, mailbox: str, *, limit: int = 50) -> list[dict[str, Any]]:
        """Return messages posted to `mailbox` since the last call for that
        mailbox on this client instance (in-memory cursor; see module
        docstring). The very first call for a mailbox baselines silently
        (returns nothing) so a bridge restart never replays old commands."""
        try:
            payload = self._call(f"/v1/mailbox/messages?mailbox={mailbox}&limit={limit}")
        except urllib.error.URLError as error:
            raise ConnectionError(f"ws_collab mailbox read failed ({self.base_url}): {error}") from error
        messages = payload.get("messages") or []
        last_seen = self._last_seen_id.get(mailbox)
        if mailbox not in self._last_seen_id:
            self._last_seen_id[mailbox] = messages[-1].get("id") if messages else None
            return []
        if last_seen is None:
            fresh = messages
        else:
            ids = [m.get("id") for m in messages]
            cut = ids.index(last_seen) + 1 if last_seen in ids else 0
            fresh = messages[cut:]
        if messages:
            self._last_seen_id[mailbox] = messages[-1].get("id")
        return fresh

    def ingest_transcript(
        self,
        text: str,
        *,
        correlation_id: str,
        source_kind: str,
        audio_meta: dict[str, Any],
    ) -> dict[str, Any]:
        """Push one finalized Meet caption into ws_collab's STT pipeline."""
        return self._call(
            "/v1/stt/ingest",
            method="POST",
            body={
                "engine": "google_meet",
                "text": text,
                "correlation_id": correlation_id,
                "confidence": 0.75,
                "is_final": True,
                "language": "en",
                "source_kind": source_kind,
                "resolve": True,
                "audio_meta": audio_meta,
            },
        )

    def list_audio_devices(self) -> dict[str, Any]:
        """Return ws_collab's audio device catalog."""
        return self._call("/v1/audio/devices")

    def secondary_capture_state(self) -> dict[str, Any]:
        """Return the current companion-heard secondary capture state."""
        return self._call("/v1/audio/secondary-capture")

    def start_secondary_capture(self, device_id: str) -> dict[str, Any]:
        """Start server-side secondary capture on the virtual cable input side."""
        return self._call(
            "/v1/audio/secondary-capture/start",
            method="POST",
            body={"device_id": device_id},
        )
