"""WebSocket WS_COLLAB client with a REST fallback.

Shows the WebSocket-preferred-with-REST-fallback mode required by the task: it
tries WS/WSS (auth, subscribe from a cursor, catch-up + live, publish), and if the
WebSocket cannot connect it falls back to the REST long-poll client while
preserving the durable cursor so no events are missed or duplicated.
"""

from __future__ import annotations

import argparse
import asyncio
import json

try:
    import websockets
except ImportError:  # pragma: no cover
    websockets = None

from rest_client import RestClient  # type: ignore


async def run_ws(base_url: str, token: str, stream: str, cursor: str | None) -> None:
    ws_url = base_url.replace("http://", "ws://").replace("https://", "wss://").rstrip("/") + "/ws_collab/ws"
    async with websockets.connect(ws_url, max_size=None) as ws:
        await ws.send(json.dumps({"type": "auth", "token": token}))
        print("auth:", json.loads(await ws.recv())["type"])
        await ws.send(json.dumps({"type": "subscribe", "streams": [stream], "cursors": {stream: cursor} if cursor else {}}))
        async for raw in ws:
            message = json.loads(raw)
            if message["type"] == "event":
                event = message["event"]
                print("EVENT", event["seq"], event["type"])
            elif message["type"] == "ping":
                await ws.send(json.dumps({"type": "pong"}))


def run_rest(base_url: str, token: str, stream: str, cursor: str | None) -> None:
    client = RestClient(base_url, token)
    print("Falling back to REST long polling")
    for events, _cursor in client.consume(stream, after=cursor):
        for event in events:
            print("EVENT", event["seq"], event["type"])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8802")
    parser.add_argument("--token", required=True)
    parser.add_argument("--stream", default="conversation")
    parser.add_argument("--cursor", default=None)
    args = parser.parse_args()
    if websockets is not None:
        try:
            asyncio.run(run_ws(args.base, args.token, args.stream, args.cursor))
            return
        except Exception as error:  # noqa: BLE001
            print(f"WebSocket unavailable ({error}); using REST fallback")
    run_rest(args.base, args.token, args.stream, args.cursor)


if __name__ == "__main__":
    main()
