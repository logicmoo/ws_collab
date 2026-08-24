"""Copilot -> WS_COLLAB speech bridge example.

Pushes an external recognizer's speech events (for example the Copilot app's
voice-dictation / Nemotron output) into WS_COLLAB via the ingest endpoint, so the
external recognizer participates in the same disambiguation, classification, and
unified transcript pipeline as local engines.

Because the Copilot app does not expose its recognizer over a public API, the
integration pattern is: whatever component observes the app's recognised text
calls ``post_transcript`` below (or the equivalent WS ``stt_ingest`` frame).

Usage:
    python copilot_speech_bridge.py --base http://127.0.0.1:8802 --token <TOKEN> \
        --engine copilot-nemotron --text "deploy the staging build"
"""

from __future__ import annotations

import argparse
import json
import urllib.request


def post_transcript(base_url: str, token: str, engine: str, text: str, *, confidence: float = 0.92,
                    source_kind: str = "operator", correlation_id: str | None = None) -> dict:
    payload = {
        "engine": engine,
        "text": text,
        "confidence": confidence,
        "source_kind": source_kind,
        "is_final": True,
        "resolve": True,
    }
    if correlation_id:
        payload["correlation_id"] = correlation_id
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/ws_collab/v1/stt/ingest",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Bridge external speech into WS_COLLAB")
    parser.add_argument("--base", default="http://127.0.0.1:8802")
    parser.add_argument("--token", required=True)
    parser.add_argument("--engine", default="copilot-nemotron")
    parser.add_argument("--text", required=True)
    parser.add_argument("--source-kind", default="operator")
    args = parser.parse_args()
    result = post_transcript(args.base, args.token, args.engine, args.text, source_kind=args.source_kind)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
