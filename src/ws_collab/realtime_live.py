"""Explicit, interactive live runner for the reusable turn-taking scenarios."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from typing import Any

from .realtime_scenarios import (
    AGENT,
    ExpectedTurn,
    TurnObservation,
    TurnTakingScenarioEngine,
    alphabet_scenario,
    counting_scenario,
)
from .stt.base import normalize_text


def _normal_meeting_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value.strip())
    return urllib.parse.urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"), "", ""))


def live_readiness_errors(status: dict[str, Any], meeting_url: str) -> list[str]:
    """Fail-closed readiness checks; role identities come only from bridge status."""

    errors = []
    if not status:
        return ["Meet bridge status is unavailable"]
    if _normal_meeting_url(str(status.get("meetingUrl") or "")) != _normal_meeting_url(meeting_url):
        errors.append("active Meet URL does not match --meeting-url")
    if status.get("ssoSatisfied") is not True:
        errors.append("bridge SSO identity preflight is not satisfied")
    companion_audio = status.get("companionAudio") or {}
    if companion_audio.get("companionReady") is not True:
        errors.append("companion audio destination is not ready")
    host_account = ((status.get("hostProfile") or {}).get("account") or {})
    if host_account.get("signedIn") is not True or not host_account.get("email"):
        errors.append("HOST live identity is not verified")
    companions = [
        client for client in status.get("clients") or []
        if str(client.get("role") or "").lower() == "companion"
    ]
    if len(companions) != 1:
        errors.append("exactly one configured COMPANION client is required")
    else:
        companion = companions[0]
        account = companion.get("account") or {}
        if companion.get("state") != "in-call":
            errors.append("COMPANION is not in-call")
        if account.get("signedIn") is not True or not account.get("email"):
            errors.append("COMPANION live identity is not verified")
    return errors


class _Api:
    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}", "content-type": "application/json"}

    def get(self, path: str) -> dict[str, Any]:
        request = urllib.request.Request(f"{self.base_url}{path}", headers=self.headers)
        with urllib.request.urlopen(request, timeout=3.0) as response:  # noqa: S310
            return json.loads(response.read().decode("utf-8"))

    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(body).encode("utf-8"),
            headers=self.headers,
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5.0) as response:  # noqa: S310
            return json.loads(response.read().decode("utf-8"))


class LiveScenarioIO:
    def __init__(
        self,
        api: _Api,
        *,
        meeting_url: str,
        agent_id: str,
        user_caption_name: str = "",
        user_source: str = "companion-heard",
    ) -> None:
        self.api = api
        self.meeting_url = meeting_url
        self.agent_id = agent_id
        self.user_caption_name = user_caption_name
        self.user_source = user_source

    async def perform_turn(self, turn: ExpectedTurn) -> list[TurnObservation]:
        if turn.actor == AGENT:
            return [await asyncio.to_thread(self._agent_turn, turn)]
        return [await asyncio.to_thread(self._user_turn, turn)]

    def _agent_turn(self, turn: ExpectedTurn) -> TurnObservation:
        print(f"Turn {turn.index}: agent {self.agent_id!r} should speak {turn.spoken_token!r}.")
        result = self.api.post(
            "/ws_collab/v1/tts/speak",
            {
                "agent_id": self.agent_id,
                "text": turn.spoken_token,
                "destination": "companion",
                "meeting_url": self.meeting_url,
                "correlation_id": f"live-turn-{turn.index}-{time.time_ns()}",
            },
        )
        utterance_id = result.get("id")
        if not utterance_id or result.get("duplicate"):
            raise RuntimeError(f"agent turn was not queued: {result}")
        deadline = time.monotonic() + turn.deadline_ms / 1000.0
        while time.monotonic() <= deadline:
            status = self.api.get("/ws_collab/v1/meet/bridge/status")
            errors = live_readiness_errors(status, self.meeting_url)
            if errors:
                raise RuntimeError("; ".join(errors))
            last = (status.get("companionAudio") or {}).get("lastUtterance") or {}
            if last.get("id") == utterance_id:
                if last.get("error"):
                    raise RuntimeError(f"companion playback failed: {last['error']}")
                return TurnObservation(
                    actor=AGENT,
                    token=turn.spoken_token,
                    source="virtual-agent-tts",
                    channel="outbound",
                    classification={"destination": "companion", "utterance_id": utterance_id},
                )
            time.sleep(0.1)
        raise TimeoutError(f"agent turn {turn.index} did not complete before its deadline")

    def _user_turn(self, turn: ExpectedTurn) -> TurnObservation:
        since = time.time()
        input(
            f"Turn {turn.index}: person observed via {self.user_source!r} "
            f"must speak {turn.spoken_token!r}; press Enter immediately after speaking. "
        )
        deadline = time.monotonic() + turn.deadline_ms / 1000.0
        accepted = {normalize_text(value) for value in turn.accepted_asr_forms}
        while time.monotonic() <= deadline:
            status = self.api.get("/ws_collab/v1/meet/bridge/status")
            errors = live_readiness_errors(status, self.meeting_url)
            if errors:
                raise RuntimeError("; ".join(errors))
            if self.user_source == "companion-heard":
                payload = self.api.get(
                    "/ws_collab/v1/events?stream=translated_audio"
                    "&source_kind=companion_heard&type=HEARD_SPEECH&since="
                    + urllib.parse.quote(str(since), safe="")
                    + "&limit=100"
                )
                rows = payload.get("events") or []
            else:
                payload = self.api.get(
                    "/ws_collab/v1/meet/bridge/captions?since="
                    + urllib.parse.quote(str(since), safe="")
                )
                rows = [
                    row for row in payload.get("captions") or []
                    if row.get("speaker") == self.user_caption_name and row.get("final") is True
                ]
            for row in rows:
                text = str(
                    (
                        (row.get("data") or {}).get("resolved_text")
                        if self.user_source == "companion-heard"
                        else row.get("text")
                    )
                    or ""
                )
                if normalize_text(text) in accepted:
                    return TurnObservation(
                        actor="user",
                        token=text,
                        source=(
                            "companion_heard"
                            if self.user_source == "companion-heard"
                            else "google_meet_caption"
                        ),
                        channel="inbound",
                        classification=(
                            row.get("data") or {}
                            if self.user_source == "companion-heard"
                            else {"speaker": row.get("speaker"), "caption_key": row.get("key")}
                        ),
                    )
            time.sleep(0.1)
        raise TimeoutError(f"user turn {turn.index} was not captioned before its deadline")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", choices=("count", "alphabet"))
    parser.add_argument("--meeting-url", required=True)
    parser.add_argument("--user-caption-name", default="")
    parser.add_argument(
        "--user-source",
        choices=("companion-heard", "meet-caption"),
        default="companion-heard",
        help="inbound user observation source (default: companion-heard STT)",
    )
    parser.add_argument("--agent-id", default="realtime-live-agent")
    parser.add_argument("--base-url", default="http://127.0.0.1:8802")
    parser.add_argument("--token-env", default="WS_COLLAB_TOKEN")
    parser.add_argument("--deadline-ms", type=float, default=10000.0)
    parser.add_argument(
        "--confirm-live",
        action="store_true",
        help="required acknowledgement that this will speak into an already-running real Meet",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.confirm_live:
        print("Refusing live trial without --confirm-live.", file=sys.stderr)
        return 2
    token = os.environ.get(args.token_env, "")
    if not token:
        print(f"Refusing live trial: {args.token_env} is unset.", file=sys.stderr)
        return 2
    api = _Api(args.base_url, token)
    try:
        status = api.get("/ws_collab/v1/meet/bridge/status")
        errors = live_readiness_errors(status, args.meeting_url)
        if args.user_source == "companion-heard":
            heard = status.get("companionHeardStt") or {}
            if heard.get("enabled") is not True or heard.get("captureLive") is not True:
                errors.append("companion-heard STT capture is not enabled and live")
        elif not args.user_caption_name:
            errors.append("--user-caption-name is required with --user-source meet-caption")
        if errors:
            print("Refusing live trial: " + "; ".join(errors), file=sys.stderr)
            return 2
        scenario = (
            counting_scenario(deadline_ms=args.deadline_ms)
            if args.scenario == "count"
            else alphabet_scenario(deadline_ms=args.deadline_ms)
        )
        print(
            "Live trial ready. Identity-to-role assignment is taken only from the "
            "bridge's verified configuration; the human caption name was supplied explicitly."
        )
        report = asyncio.run(
            TurnTakingScenarioEngine(scenario).run(
                LiveScenarioIO(
                    api,
                    meeting_url=args.meeting_url,
                    agent_id=args.agent_id,
                    user_caption_name=args.user_caption_name,
                    user_source=args.user_source,
                )
            )
        )
        print(json.dumps(report, indent=2))
        return 0 if report["passed"] else 1
    except (OSError, RuntimeError, TimeoutError, ValueError) as error:
        print(f"Live trial failed closed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
