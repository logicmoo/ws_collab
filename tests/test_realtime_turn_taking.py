from __future__ import annotations

import asyncio
import json

import pytest

from ws_collab.events import (
    HEARD_SPEECH,
    TRANSCRIPT_FILTERED,
    TTS_FINISHED,
    streams_for_role,
)
from ws_collab.realtime_live import live_readiness_errors
from ws_collab.realtime_scenarios import (
    ERROR_DEADLINE_EXCEEDED,
    ERROR_DUPLICATE,
    ERROR_ECHO_LEAK,
    ERROR_MISSING,
    ERROR_OUT_OF_ORDER,
    ERROR_WRONG_SPEAKER,
    DeterministicProductionScenarioIO,
    TurnObservation,
    TurnTakingScenarioEngine,
    alphabet_scenario,
    counting_scenario,
)


@pytest.mark.parametrize(
    ("factory", "expected_length", "last_token"),
    [
        (counting_scenario, 20, "twenty"),
        (alphabet_scenario, 26, "Z"),
    ],
)
def test_realtime_scenarios_pass_through_production_audio_and_stt_paths(
    client, admin_headers, app_context, factory, expected_length, last_token
) -> None:
    service = app_context.service
    scenario = factory()
    engine = TurnTakingScenarioEngine(scenario)
    http_ingestions: list[dict] = []

    def ingest(payload):
        response = client.post(
            "/ws_collab/v1/audio/secondary-capture/browser",
            headers=admin_headers,
            json=payload,
        )
        assert response.status_code == 200
        http_ingestions.append(payload)
        return response.json()

    io = DeterministicProductionScenarioIO(service, browser_ingest=ingest)
    try:
        report = asyncio.run(engine.run(io))
    finally:
        io.close()

    assert report["passed"] is True
    assert len(report["turns"]) == expected_length
    assert report["turns"][-1]["expected"]["spoken_token"] == last_token
    assert all(row["observed"]["actor"] == row["expected"]["actor"] for row in report["turns"])
    assert all(row["latency_ms"] is not None for row in report["turns"])
    assert report["latency"]["count"] == expected_length
    assert report["latency"]["p50_ms"] <= report["latency"]["p95_ms"] <= report["latency"]["max_ms"]
    assert report["drops"] == report["duplicates"] == report["misattributions"] == 0
    assert report["echoes"] == sum(turn.actor == "agent" for turn in scenario.turns)
    assert len(io.played) == report["echoes"]
    assert all(item["source"] == "virtual-agent-tts" for item in io.played)
    assert all(item["meetingUrl"] == io.meeting_url for item in io.played)
    assert len(http_ingestions) == expected_length
    assert all(payload["stream_id"] == "deterministic-fake-cdp-remote-media" for payload in http_ingestions)
    assert sum(bool(payload["chunks"]) for payload in http_ingestions) == sum(
        turn.actor == "user" for turn in scenario.turns
    )
    capture = service.secondary_capture_state()
    assert capture["chunks_received"] == sum(turn.actor == "user" for turn in scenario.turns)
    assert capture["segments_forwarded"] == sum(turn.actor == "user" for turn in scenario.turns)
    assert capture["dropped_artifact_chunks"] == report["echoes"]

    turn_timeline = [
        event["kind"] for event in io.timeline
        if event["kind"] in {"agent_outbound", "user_inbound"}
    ]
    assert turn_timeline == [
        "agent_outbound" if turn.actor == "agent" else "user_inbound"
        for turn in scenario.turns
    ]
    for item in io.played:
        accepted = next(
            index for index, row in enumerate(io.timeline)
            if row["kind"] == "bridge_queue_accepted" and row["id"] == item["id"]
        )
        completed = next(
            index for index, row in enumerate(io.timeline)
            if row["kind"] == "bridge_playback_completed" and row["id"] == item["id"]
        )
        assert accepted < completed

    tts_events = service.read_events(streams_for_role("tts_queue")[0], limit=1000)["events"]
    speech_events = service.read_events(streams_for_role("resolved_speech")[0], limit=1000)["events"]
    assert sum(event["type"] == TTS_FINISHED for event in tts_events) == len(io.played)
    assert sum(event["type"] == HEARD_SPEECH for event in speech_events) == sum(
        turn.actor == "user" for turn in scenario.turns
    )
    assert sum(event["type"] == TRANSCRIPT_FILTERED for event in speech_events) == 0
    json.dumps(report)


def test_scenario_definitions_preserve_actor_order_and_bounded_asr_variants() -> None:
    count = counting_scenario()
    alphabet = alphabet_scenario()

    assert [turn.spoken_token for turn in count.turns] == [
        "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
        "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen",
        "eighteen", "nineteen", "twenty",
    ]
    assert [turn.spoken_token for turn in alphabet.turns] == list("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    for scenario in (count, alphabet):
        assert all(
            turn.actor == ("agent" if turn.index % 2 else "user")
            for turn in scenario.turns
        )
        assert all(turn.spoken_token.lower() in turn.accepted_asr_forms for turn in scenario.turns)
    assert "won" in count.turns[0].accepted_asr_forms
    assert {"c", "see", "sea"} <= set(alphabet.turns[2].accepted_asr_forms)
    assert max(len(turn.accepted_asr_forms) for turn in alphabet.turns) <= 4


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


def _observation(
    actor: str,
    token: str,
    *,
    at: float = 1.0,
    channel: str = "outbound",
    is_echo: bool = False,
) -> TurnObservation:
    return TurnObservation(
        actor=actor,
        token=token,
        source="failure-injector",
        observed_at_ms=at,
        channel=channel,
        is_echo=is_echo,
    )


@pytest.mark.parametrize(
    ("category", "exercise"),
    [
        (
            ERROR_DUPLICATE,
            lambda engine: (
                engine.observe(_observation("agent", "one")),
                engine.observe(_observation("agent", "one", at=2.0)),
            ),
        ),
        (
            ERROR_MISSING,
            lambda engine: None,
        ),
        (
            ERROR_OUT_OF_ORDER,
            lambda engine: engine.observe(_observation("agent", "three")),
        ),
        (
            ERROR_WRONG_SPEAKER,
            lambda engine: engine.observe(_observation("user", "one")),
        ),
        (
            ERROR_ECHO_LEAK,
            lambda engine: (
                engine.observe(_observation("agent", "one")),
                engine.observe(_observation("user", "won", at=2.0, channel="inbound")),
            ),
        ),
    ],
)
def test_realtime_failure_categories_are_reported(category, exercise) -> None:
    clock = _Clock()
    engine = TurnTakingScenarioEngine(counting_scenario(), clock_ms=clock)
    exercise(engine)
    report = engine.finish()

    assert report["passed"] is False
    assert category in {error["category"] for error in report["errors"]}
    assert report["turns"]
    json.dumps(report)


def test_realtime_deadline_exceeded_is_reported_with_latency() -> None:
    clock = _Clock()
    engine = TurnTakingScenarioEngine(counting_scenario(deadline_ms=10), clock_ms=clock)
    result = engine.observe(_observation("agent", "one", at=11.0))
    report = engine.finish()

    assert result == ERROR_DEADLINE_EXCEEDED
    assert report["turns"][0]["latency_ms"] == 11.0
    assert report["turns"][0]["error_category"] == ERROR_DEADLINE_EXCEEDED


def test_marked_echo_is_rejected_without_consuming_the_user_turn() -> None:
    engine = TurnTakingScenarioEngine(counting_scenario())
    engine.observe(_observation("agent", "one"))
    rejected = engine.observe(
        _observation("agent", "one", at=2.0, channel="inbound", is_echo=True)
    )
    accepted = engine.observe(_observation("user", "two", at=3.0, channel="inbound"))
    report = engine.finish()

    assert rejected == "echo_rejected"
    assert accepted == "accepted"
    assert report["echoes"] == 1
    assert report["turns"][1]["observed"]["token"] == "two"


def test_live_readiness_fails_closed_without_bridge_companion_and_identities() -> None:
    meeting = "https://meet.google.com/abc-defg-hij"
    assert live_readiness_errors({}, meeting)
    status = {
        "meetingUrl": meeting,
        "ssoSatisfied": True,
        "companionAudio": {"companionReady": True},
        "hostProfile": {
            "account": {"signedIn": True, "email": "configured-host@example.test"}
        },
        "clients": [
            {
                "role": "companion",
                "state": "in-call",
                "account": {
                    "signedIn": True,
                    "email": "configured-companion@example.test",
                },
            }
        ],
    }
    assert live_readiness_errors(status, meeting) == []

    status["clients"][0]["account"]["signedIn"] = False
    assert "COMPANION live identity is not verified" in live_readiness_errors(status, meeting)
