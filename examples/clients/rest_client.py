"""REST-only WS_COLLAB client (no WebSocket required).

Demonstrates the transport-independent contract: authenticate, publish with an
idempotency key, and consume a stream with cursor-based bounded long polling.
REST clients never need a tight polling loop -- ``wait_ms`` blocks server-side
until an event arrives or the timeout elapses.
"""

from __future__ import annotations

import argparse
import json
import urllib.parse
import urllib.request


class RestClient:
    def __init__(self, base_url: str, token: str):
        self.base = base_url.rstrip("/")
        self.token = token

    def _request(self, method: str, path: str, body: dict | None = None, headers: dict | None = None) -> dict:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(f"{self.base}{path}", data=data, method=method)
        request.add_header("Authorization", f"Bearer {self.token}")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        with urllib.request.urlopen(request) as response:
            return json.loads(response.read().decode("utf-8"))

    def publish_conversation(self, text: str, idempotency_key: str) -> dict:
        return self._request(
            "POST", "/ws_collab/v1/conversation/events",
            {"text": text}, {"Idempotency-Key": idempotency_key},
        )

    def consume(self, stream: str, after: str | None = None, wait_ms: int = 25000):
        """Yield pages forever using cursor + long polling."""

        cursor = after
        while True:
            query = {"stream": stream, "limit": 100, "wait_ms": wait_ms}
            if cursor:
                query["after"] = cursor
            page = self._request("GET", f"/ws_collab/v1/events?{urllib.parse.urlencode(query)}")
            cursor = page["next_cursor"]
            if page["events"]:
                yield page["events"], cursor


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8802")
    parser.add_argument("--token", required=True)
    parser.add_argument("--stream", default="conversation")
    args = parser.parse_args()
    client = RestClient(args.base, args.token)
    print("capabilities:", client._request("GET", "/ws_collab/v1/capabilities")["features"])
    print(client.publish_conversation("hello from rest client", "demo-key-1"))
    for events, cursor in client.consume(args.stream):
        for event in events:
            print(event["seq"], event["type"], event.get("data"))


if __name__ == "__main__":
    main()
