from __future__ import annotations

import asyncio
import json
import re
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import make_config, make_event_store
from ws_collab.captioner import (
    BrowserCaptioner,
    CaptionSourcePolicy,
    CaptionerInstanceRegistry,
    CaptionerFinalizationError,
    CaptionerSettings,
    CaptionerSupervisor,
    LocalMicFloor,
    RECENT_FINAL_LIMIT,
    RECENT_FINAL_WINDOW_SECONDS,
)
from ws_collab.drivers import discover_stt_drivers
from ws_collab.events import (
    CONVERSATION_MESSAGE,
    HEARD_SPEECH,
    STREAM_AUDIT,
    STREAM_TTS,
    STT_FINAL_RESULT,
    STT_PARTIAL_RESULT,
    TRANSCRIPT_RESOLVED,
)
from ws_collab.meet_bridge import navigator
from ws_collab.service import WsCollabService
from ws_collab.tts.engine import TtsItem


def envelope(*, seq=1, revision=1, final=False, text="hello"):
    return {
        "session_id": "59a92f2c-c45e-48a3-bfd2-6df656bf51cf",
        "instance_id": "captioner-instance-a",
        "utterance_id": "59a92f2c-c45e-48a3-bfd2-6df656bf51cf:1:0",
        "seq": seq,
        "revision": revision,
        "text": text,
        "is_final": final,
        "confidence": 0.0,
        "language": "en-US",
        "started_at": "2026-09-11T10:00:00.000Z",
        "result_at": "2026-09-11T10:00:01.000Z",
        "speech_started_at": "2026-09-11T09:59:59.500Z",
        "speech_ended_at": "2026-09-11T10:00:00.750Z",
        "silence_before_ms": 420,
    }


def register_service_captioner(service, *, instance_id="captioner-instance-a", state="standby"):
    return service.captioner_heartbeat(
        {
            "session_id": "59a92f2c-c45e-48a3-bfd2-6df656bf51cf",
            "instance_id": instance_id,
            "boot_id": service.boot_id,
            "owns_lease": state in {"listening", "speaking"},
            "state": state,
            "queue_depth": 0,
            "recognizer_supported": True,
        }
    )


def acoustic_pause(*, duration=80, after_char=2, alignment="interim_prefix"):
    return {
        "duration_ms": duration,
        "start_at": "2026-09-11T10:00:00.100Z",
        "end_at": "2026-09-11T10:00:00.180Z",
        "source": "browser_rms_vad",
        "alignment": alignment,
        "after_char": after_char,
    }


def test_captioner_settings_are_durable_and_pause_defaults_false(tmp_path: Path) -> None:
    settings = CaptionerSettings(tmp_path)
    assert settings.get() == {
        "enabled": True,
        "language": "en-US",
        "send_interims": True,
        "paused": False,
        "prefer_over_google_meet": True,
        "disable_google_meet": False,
        "disable_other_stts": False,
    }
    settings.update({"language": "de-DE", "send_interims": False, "paused": True})
    assert CaptionerSettings(tmp_path).get()["paused"] is True
    assert CaptionerSettings(tmp_path).get()["language"] == "de-DE"


def test_captioner_settings_preserve_unknown_existing_values(tmp_path: Path) -> None:
    path = tmp_path / "captioner_settings.json"
    path.write_text(
        json.dumps({"language": "fr-FR", "future_setting": {"keep": True}}),
        encoding="utf-8",
    )
    settings = CaptionerSettings(tmp_path)
    settings.update({"disable_google_meet": True})
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["future_setting"] == {"keep": True}
    assert saved["prefer_over_google_meet"] is True
    assert saved["disable_google_meet"] is True


@pytest.mark.parametrize(
    "field",
    [
        "prefer_over_google_meet",
        "disable_google_meet",
        "disable_other_stts",
    ],
)
def test_captioner_policy_settings_require_strict_booleans(
    tmp_path: Path, field: str
) -> None:
    with pytest.raises(Exception, match="must be a boolean"):
        CaptionerSettings(tmp_path).update({field: 1})


def test_caption_source_policy_migrates_legacy_and_repairs_primary(tmp_path: Path) -> None:
    (tmp_path / "captioner_settings.json").write_text(
        json.dumps(
            {
                "enabled": True,
                "disable_google_meet": False,
                "prefer_over_google_meet": False,
            }
        ),
        encoding="utf-8",
    )
    policy = CaptionSourcePolicy(tmp_path)
    assert policy.get()["primary_source_id"] == "google_meet"
    disabled = policy.action("google_meet", "disable")
    assert disabled["changed"] is True
    assert disabled["policy"]["primary_source_id"] == "browser_captioner"
    assert CaptionSourcePolicy(tmp_path).get() == disabled["policy"]
    with pytest.raises(Exception, match="unknown caption source"):
        policy.action("meet-ish", "enable")


def test_caption_source_policy_concurrent_writes_remain_atomic(tmp_path: Path) -> None:
    policy = CaptionSourcePolicy(tmp_path)
    failures = []

    def mutate(source_id: str) -> None:
        try:
            for _ in range(20):
                policy.action(source_id, "make-primary")
                policy.action(source_id, "enable")
        except Exception as error:
            failures.append(error)

    threads = [
        threading.Thread(target=mutate, args=(source_id,))
        for source_id in ("browser_captioner", "google_meet")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert failures == []
    saved = json.loads(policy.path.read_text(encoding="utf-8"))
    assert saved["primary_source_id"] in {
        source_id for source_id, enabled in saved["sources"].items() if enabled
    }


def test_captioner_instance_registry_selects_pins_disables_and_fails_over(
    tmp_path: Path,
) -> None:
    now = [100.0]
    registry = CaptionerInstanceRegistry(
        tmp_path,
        boot_id="boot",
        clock=lambda: now[0],
        stale_seconds=10,
        retention_seconds=60,
        max_instances=2,
    )

    def beat(instance_id: str, state: str = "standby"):
        return registry.heartbeat(
            {
                "session_id": f"session-{instance_id}",
                "instance_id": instance_id,
                "page_boot_id": "boot",
                "state": state,
                "queue_depth": 0,
                "recognizer_supported": True,
            }
        )

    assert beat("instance-a")["selected"] is True
    now[0] += 1
    assert beat("instance-b")["selected"] is False
    pinned = registry.action("instance-b", "make-primary")["registry"]
    assert pinned["selected_instance_id"] == "instance-b"
    assert pinned["selection_mode"] == "pinned"
    restored = CaptionerInstanceRegistry(
        tmp_path,
        boot_id="boot",
        clock=lambda: now[0],
        stale_seconds=10,
        retention_seconds=60,
        max_instances=2,
    )
    assert restored.list()["selected_instance_id"] == "instance-b"
    assert restored.list()["selection_mode"] == "pinned"
    disabled = registry.action("instance-b", "disable")["registry"]
    assert disabled["selected_instance_id"] == "instance-a"
    assert beat("instance-b")["reason"] == "instance_disabled"
    registry.action("instance-b", "enable")
    registry.action("instance-b", "make-primary")
    now[0] += 9
    beat("instance-a")
    assert registry.list()["selected_instance_id"] == "instance-b"
    now[0] += 2
    beat("instance-a")
    assert registry.list()["selection_mode"] == "automatic"
    assert registry.list()["selected_instance_id"] == "instance-a"


def test_captioner_instance_registry_rejects_self_promotion_and_is_bounded(
    tmp_path: Path,
) -> None:
    now = [10.0]
    registry = CaptionerInstanceRegistry(
        tmp_path,
        boot_id="boot",
        clock=lambda: now[0],
        stale_seconds=5,
        retention_seconds=10,
        max_instances=2,
    )
    for index in range(3):
        registry.heartbeat(
            {
                "session_id": f"session-{index}",
                "instance_id": f"instance-{index}",
                "page_boot_id": "boot",
                "state": "standby",
                "recognizer_supported": True,
                "queue_depth": index,
            }
        )
        now[0] += 1
    assert len(registry.list()["instances"]) == 2
    source = BrowserCaptioner(
        tmp_path / "strict",
        boot_id="boot",
        publish_item=lambda _item: None,
    )
    with pytest.raises(Exception, match="unsupported fields"):
        source.heartbeat(
            {
                "session_id": "session-a",
                "instance_id": "instance-a",
                "state": "standby",
                "selected": True,
            }
        )
    now[0] += 11
    assert registry.list()["instances"] == []
    assert registry.list()["selected_instance_id"] is None


def test_caption_source_and_instance_actions_are_admin_only_and_explicit(
    client, admin_headers, worker_headers
) -> None:
    policy = client.get("/ws_collab/caption-sources", headers=admin_headers)
    assert policy.status_code == 200
    assert policy.json()["primary_source_id"] == "browser_captioner"
    denied = client.post(
        "/ws_collab/caption-sources/google_meet/make-primary",
        headers=worker_headers,
        json={},
    )
    assert denied.status_code == 403
    changed = client.post(
        "/ws_collab/caption-sources/google_meet/make-primary",
        headers=admin_headers,
        json={},
    )
    assert changed.status_code == 200
    assert changed.json()["changed"] is True
    assert changed.json()["policy"]["primary_source_id"] == "google_meet"
    repeated = client.post(
        "/ws_collab/caption-sources/google_meet/make-primary",
        headers=admin_headers,
        json={},
    )
    assert repeated.json()["changed"] is False
    assert repeated.json()["action"] == "make-primary"


def test_root_admin_spa_exact_surface_and_assets(client) -> None:
    root = client.get(
        "/ws_collab/?cache-bust=policy",
        headers={"accept": "text/html"},
    )
    assert root.status_code == 200
    assert root.headers["cache-control"] == "no-store"
    assert 'data-page="chrome-captions"' in root.text
    assert "Make Chrome Captions primary" in root.text
    assert client.get("/ws_collab/app.js").status_code == 200
    assert client.get("/ws_collab/app.css").status_code == 200
    assert client.get("/ws_collab/admin/").status_code == 200


def test_captioner_revisions_and_final_are_idempotent_across_restart(tmp_path: Path) -> None:
    published = []
    source = BrowserCaptioner(
        tmp_path, boot_id="boot-a", publish_item=published.append, clock=lambda: 1_800_000_000
    )
    assert source.ingest(envelope())["results"][0]["status"] == "accepted"
    assert source.ingest(envelope(seq=2, revision=1))["results"][0]["status"] == "rejected_stale_revision"
    assert source.ingest(envelope(seq=3, revision=2, final=True, text="done"))["accepted"] == 1
    assert source.ingest(envelope(seq=3, revision=2, final=True, text="done"))["results"][0]["status"] == "duplicate"
    assert source.ingest(envelope(seq=4, revision=3, text="changed"))["results"][0]["status"] == "rejected_final_immutable"
    assert [item["text"] for item in published] == ["hello", "done"]

    replayed = []
    restarted = BrowserCaptioner(
        tmp_path, boot_id="boot-b", publish_item=replayed.append, clock=lambda: 1_800_000_001
    )
    assert restarted.ingest(envelope(seq=3, revision=2, final=True, text="done"))["results"][0]["status"] == "duplicate"
    assert replayed == []


def test_captioner_preserves_boundary_metadata_across_revisions(tmp_path: Path) -> None:
    published = []
    source = BrowserCaptioner(
        tmp_path, boot_id="boot", publish_item=published.append, clock=lambda: 1_800_000_000
    )
    interim = envelope()
    interim["speech_ended_at"] = None
    source.ingest(interim)
    final = envelope(seq=2, revision=2, final=True)
    final["speech_started_at"] = "2026-09-11T10:00:10.000Z"
    final["speech_ended_at"] = "2026-09-11T10:00:01.250Z"
    final["silence_before_ms"] = 999
    source.ingest(final)

    assert published[1]["speech_started_at"] == interim["speech_started_at"]
    assert published[1]["speech_ended_at"] == final["speech_ended_at"]
    assert published[1]["silence_before_ms"] == interim["silence_before_ms"]


def test_captioner_rejects_out_of_order_sequence(tmp_path: Path) -> None:
    published = []
    source = BrowserCaptioner(
        tmp_path, boot_id="boot", publish_item=published.append, clock=lambda: 1_800_000_000
    )
    assert source.ingest(envelope(seq=2))["accepted"] == 1
    result = source.ingest(envelope(seq=1, revision=2, text="late"))
    assert result["results"][0]["status"] == "rejected_stale_seq"
    assert [item["text"] for item in published] == ["hello"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("seq", 0),
        ("revision", -1),
        ("confidence", 1.1),
        ("language", "not a language tag!"),
        ("text", "x" * 4001),
    ],
)
def test_captioner_envelope_validation(field, value) -> None:
    item = envelope()
    item[field] = value
    with pytest.raises(Exception):
        BrowserCaptioner.validate_envelope(item)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("speech_started_at", "2026-09-11T10:00:00"),
        ("speech_ended_at", "not-a-time"),
        ("silence_before_ms", -1),
        ("silence_before_ms", 19),
        ("silence_before_ms", True),
        ("silence_before_ms", 86_400_001),
        ("silence_before_ms", 1.5),
    ],
)
def test_captioner_rejects_invalid_speech_boundary_metadata(field, value) -> None:
    item = envelope()
    item[field] = value
    with pytest.raises(Exception):
        BrowserCaptioner.validate_envelope(item)


def test_captioner_accepts_null_speech_boundary_metadata() -> None:
    item = envelope()
    item.update(
        speech_started_at=None,
        speech_ended_at=None,
        silence_before_ms=None,
    )
    validated = BrowserCaptioner.validate_envelope(item)
    assert validated["speech_started_at"] is None
    assert validated["speech_ended_at"] is None
    assert validated["silence_before_ms"] is None


def test_captioner_strictly_validates_acoustic_pauses() -> None:
    item = envelope(final=True)
    item["pauses"] = [
        acoustic_pause(duration=20, after_char=0, alignment="between_utterances"),
        acoustic_pause(duration=80, after_char=3),
    ]
    assert BrowserCaptioner.validate_envelope(item)["pauses"] == item["pauses"]

    invalid = [
        {**acoustic_pause(), "duration_ms": 19},
        {**acoustic_pause(), "duration_ms": 86_400_001},
        {**acoustic_pause(), "duration_ms": True},
        {**acoustic_pause(), "after_char": 6},
        {**acoustic_pause(), "after_char": True},
        {**acoustic_pause(), "source": "speech_recognition"},
        {**acoustic_pause(), "alignment": "exact_word"},
        {**acoustic_pause(), "start_at": "2026-09-11T10:00:00"},
        {**acoustic_pause(), "end_at": "2026-09-10T10:00:00.000Z"},
        {**acoustic_pause(), "pcm": [0.1]},
    ]
    for pause in invalid:
        malformed = envelope(final=True)
        malformed["pauses"] = [pause]
        with pytest.raises(Exception):
            BrowserCaptioner.validate_envelope(malformed)
    too_many = envelope(final=True)
    too_many["pauses"] = [acoustic_pause()] * 129
    with pytest.raises(Exception):
        BrowserCaptioner.validate_envelope(too_many)


def test_pause_list_is_published_and_replayed_exactly(tmp_path: Path) -> None:
    pauses = [
        acoustic_pause(duration=20, after_char=1),
        acoustic_pause(duration=240, after_char=4),
    ]
    def fail_publish(_item):
        raise RuntimeError("leave final pending")

    source = BrowserCaptioner(
        tmp_path, boot_id="boot", publish_item=fail_publish
    )
    item = envelope(final=True)
    item["pauses"] = pauses
    with pytest.raises(CaptionerFinalizationError):
        source.ingest(item)
    state = json.loads((tmp_path / "captioner_dedupe.json").read_text(encoding="utf-8"))
    assert next(iter(state["finalizations"].values()))["item"]["pauses"] == pauses
    replayed = []
    restarted = BrowserCaptioner(
        tmp_path, boot_id="next", publish_item=replayed.append
    )
    assert restarted.recover_pending() == {"recovered": 1, "failed": 0}
    assert replayed[0]["pauses"] == pauses


def test_acoustic_pauses_are_durable_stt_event_metadata(service) -> None:
    register_service_captioner(service)
    item = envelope(final=True, text="one two")
    item["pauses"] = [
        acoustic_pause(duration=20, after_char=3),
        acoustic_pause(duration=240, after_char=7),
    ]
    service.ingest_captioner(item)
    event = next(
        row
        for row in service.store.tail("stt_transcripts", 20)
        if row.type == STT_FINAL_RESULT
    )
    assert event.data["pauses"] == item["pauses"]


def test_captioner_routes_scope_secret_and_publish_once(
    client, app_context, admin_headers
) -> None:
    with TestClient(
        client.app,
        base_url="http://127.0.0.1:8802",
        client=("127.0.0.1", 50000),
    ) as local:
        page = local.get("/ws_collab/captioner/")
        assert page.status_code == 200
        token = re.search(
            r'name="ws-captioner-token" content="([^"]+)"', page.text
        ).group(1)
        assert token not in str(page.url)
        assert "not offline or local" in page.text
        headers = {
            "x-ws-collab-captioner-token": token,
            "content-type": "application/json",
            "origin": "http://127.0.0.1:8802",
        }
        registered = local.post(
            "/ws_collab/captioner/heartbeat",
            headers=headers,
            json={
                "session_id": envelope()["session_id"],
                "instance_id": envelope()["instance_id"],
                "boot_id": app_context.service.boot_id,
                "state": "standby",
                "queue_depth": 0,
                "recognizer_supported": True,
            },
        )
        assert registered.status_code == 200
        first = local.post(
            "/ws_collab/captioner/ingest",
            headers=headers,
            json=envelope(final=True),
        )
        assert first.status_code == 200
        assert first.json()["accepted"] == 1
        assert local.post(
            "/ws_collab/captioner/ingest", headers=headers, json=envelope(final=True)
        ).json()["accepted"] == 0
        assert local.post(
            "/ws_collab/captioner/ingest",
            headers={**headers, "x-ws-collab-captioner-token": "wrong"},
            json=envelope(),
        ).status_code == 401
    transcripts = app_context.store.tail("stt_transcripts", 20)
    translated = app_context.store.tail("translated_audio", 20)
    assert sum(event.type == STT_FINAL_RESULT for event in transcripts) == 1
    assert sum(event.type == HEARD_SPEECH for event in translated) == 1
    assert transcripts[-2].data["engine"] == "browser_captioner"
    assert transcripts[-2].data["model"] == "chrome-web-speech"
    assert transcripts[-2].data["cloud_processing"] is True
    assert transcripts[-2].data["speech_started_at"] == envelope()["speech_started_at"]
    assert transcripts[-2].data["speech_ended_at"] == envelope()["speech_ended_at"]
    assert transcripts[-2].data["silence_before_ms"] == 420
    assert client.get(
        "/ws_collab/captioner/status", headers=admin_headers
    ).json()["display_name"] == "Chrome Captions"


def test_unselected_and_non_primary_browser_finals_are_acked_and_audited(
    service,
) -> None:
    register_service_captioner(service, instance_id="captioner-instance-a")
    register_service_captioner(service, instance_id="captioner-instance-b")
    standby = envelope(final=True)
    standby["instance_id"] = "captioner-instance-b"
    result = service.ingest_captioner(standby)
    assert result["acked_seqs"] == [1]
    assert result["suppressed"] == 1
    assert result["results"][0]["reason"] == "unselected_instance"
    assert not [
        event
        for event in service.store.tail("stt_transcripts", 20)
        if event.type == STT_FINAL_RESULT
    ]

    service.caption_source_action("google_meet", "make-primary")
    non_primary = envelope(seq=2, revision=1, final=True, text="diagnostic only")
    non_primary["utterance_id"] += ":meet-primary"
    result = service.ingest_captioner(non_primary)
    assert result["acked_seqs"] == [2]
    assert result["results"][0]["reason"] == "primary_google_meet"
    audits = service.store.tail(STREAM_AUDIT, 20)
    reasons = {
        event.data.get("reason")
        for event in audits
        if event.type == "BROWSER_CAPTION_SUPPRESSED"
    }
    assert {"unselected_instance", "primary_google_meet"} <= reasons


def test_disabled_browser_source_acks_final_into_bounded_raw_history(service) -> None:
    register_service_captioner(service)
    service.caption_source_action("browser_captioner", "disable")
    result = service.ingest_captioner(envelope(final=True, text="disabled raw final"))
    assert result["acked_seqs"] == [1]
    assert result["results"][0]["reason"] == "source_disabled"
    saved = json.loads(
        (service.config.state_dir / "captioner_dedupe.json").read_text(encoding="utf-8")
    )
    assert saved["suppressed"][-1]["item"]["text"] == "disabled raw final"
    assert not [
        event for event in service.store.tail("stt_transcripts", 20)
        if event.type == STT_FINAL_RESULT
    ]


def test_vad_transition_auth_ordering_floor_and_tts_cancellation(
    client, app_context, admin_headers, monkeypatch
) -> None:
    cancelled_backchannels: list[str] = []
    monkeypatch.setattr(
        app_context.service,
        "_refresh_companion_tts_status",
        lambda timeout=0.05: {
            "companionReady": True,
            "current": {"id": "backchannel-current", "kind": "interject"},
        },
    )
    monkeypatch.setattr(
        app_context.service,
        "_cancel_companion_tts",
        cancelled_backchannels.append,
    )
    app_context.service.tts._route_cancel = lambda _utterance_id: None
    with TestClient(
        client.app,
        base_url="http://127.0.0.1:8802",
        client=("127.0.0.1", 50000),
    ) as local:
        page = local.get("/ws_collab/captioner/")
        token = re.search(
            r'name="ws-captioner-token" content="([^"]+)"', page.text
        ).group(1)
        headers = {
            "x-ws-collab-captioner-token": token,
            "content-type": "application/json",
            "origin": "http://127.0.0.1:8802",
        }
        heartbeat = local.post(
            "/ws_collab/captioner/heartbeat",
            headers=headers,
            json={
                "session_id": "session",
                "instance_id": "owner",
                "owns_lease": True,
                "state": "listening",
                "queue_depth": 0,
            },
        ).json()
        owner_token = heartbeat["vad_owner_token"]
        current = TtsItem(
            agent_id="debater",
            text="existing conversational output",
            voice_id="fake:aria",
            destination="companion",
            artifact_source="virtual-agent-tts",
        )
        app_context.service.tts._current = current
        transition = {
            "source": "browser_rms_vad",
            "source_id": "local_microphone",
            "input_scope": "microphone",
            "session_id": "session",
            "instance_id": "owner",
            "owner_token": owner_token,
            "epoch": 1,
            "seq": 1,
            "event": "speech_start",
            "state": "speech",
            "at": datetime.now(timezone.utc).isoformat(),
        }
        wrong_owner = local.post(
            "/ws_collab/captioner/vad-transition",
            headers=headers,
            json={**transition, "owner_token": "wrong"},
        )
        assert wrong_owner.status_code == 401
        accepted = local.post(
            "/ws_collab/captioner/vad-transition", headers=headers, json=transition
        )
        assert accepted.status_code == 200
        assert accepted.json()["cancellation"]["cancelled"] is True
        assert accepted.json()["cancellation"]["backchannel_cancelled"] is True
        assert cancelled_backchannels == ["backchannel-current"]
        assert current.cancelled is True
        duplicate = local.post(
            "/ws_collab/captioner/vad-transition", headers=headers, json=transition
        ).json()
        assert duplicate["duplicate"] is True
        malformed = local.post(
            "/ws_collab/captioner/vad-transition",
            headers=headers,
            json={**transition, "seq": 2, "pcm": [0.1]},
        )
        assert malformed.status_code == 400
        oversized = local.post(
            "/ws_collab/captioner/vad-transition",
            headers=headers,
            json={**transition, "seq": 2, "padding": "x" * 9000},
        )
        assert oversized.status_code == 413
        stale_time = local.post(
            "/ws_collab/captioner/vad-transition",
            headers=headers,
            json={
                **transition,
                "seq": 2,
                "at": "2000-01-01T00:00:00Z",
            },
        )
        assert stale_time.status_code == 400
        assert (
            local.get("/ws_collab/captioner/status", headers=admin_headers)
            .json()["local_mic_floor"]["state"]
            == "speech"
        )
    audits = app_context.store.tail(STREAM_AUDIT, 20)
    floor_events = [event for event in audits if event.type == "LOCAL_MIC_FLOOR_ACQUIRED"]
    assert len(floor_events) == 1
    assert "text" not in floor_events[0].data


def test_captioner_token_surface_remains_loopback_with_remote_admin(
    client, admin_headers
) -> None:
    with TestClient(
        client.app,
        base_url="http://127.0.0.1:8802",
        client=("127.0.0.1", 50000),
    ) as local:
        page = local.get("/ws_collab/captioner/")
        assert page.status_code == 200
        token = re.search(
            r'name="ws-captioner-token" content="([^"]+)"', page.text
        ).group(1)
        internal_headers = {
            "x-ws-collab-captioner-token": token,
            "content-type": "application/json",
            "origin": "http://127.0.0.1:8802",
        }
        assert local.post(
            "/ws_collab/captioner/heartbeat",
            headers=internal_headers,
            json={
                "session_id": "local",
                "instance_id": "local-owner",
                "owns_lease": True,
                "state": "listening",
                "queue_depth": 0,
            },
        ).status_code == 200
        assert local.post(
            "/ws_collab/captioner/heartbeat",
            headers={**internal_headers, "origin": "http://localhost:8802"},
            json={"session_id": "local", "state": "idle"},
        ).status_code == 403

    with TestClient(
        client.app,
        base_url="http://127.0.0.1:8802",
        client=("10.1.2.3", 50000),
    ) as remote:
        assert remote.get(
            "/ws_collab/captioner/status", headers=admin_headers
        ).status_code == 200
        assert remote.get(
            "/ws_collab/captioner/config", headers=admin_headers
        ).status_code == 200
        assert remote.get("/ws_collab/captioner/").status_code == 403
        assert remote.post(
            "/ws_collab/captioner/ingest",
            headers=internal_headers,
            json=envelope(),
        ).status_code == 403
        assert remote.post(
            "/ws_collab/captioner/heartbeat",
            headers=internal_headers,
            json={"session_id": "remote", "state": "idle"},
        ).status_code == 403


def test_captioner_policy_config_requires_operator_and_validates_booleans(
    client, admin_headers, viewer_headers, worker_headers, tmp_path: Path
) -> None:
    payload = {
        "prefer_over_google_meet": False,
        "disable_google_meet": True,
        "disable_other_stts": True,
    }
    assert client.post(
        "/ws_collab/captioner/config", headers=viewer_headers, json=payload
    ).status_code == 403
    assert client.post(
        "/ws_collab/captioner/config", headers=worker_headers, json=payload
    ).status_code == 403
    saved = client.post(
        "/ws_collab/captioner/config", headers=admin_headers, json=payload
    )
    assert saved.status_code == 200
    assert saved.json()["disable_google_meet"] is True
    assert saved.json()["disable_other_stts"] is True
    assert saved.json()["prefer_over_google_meet"] is True
    assert saved.json()["caption_source_policy"]["primary_source_id"] == "browser_captioner"
    invalid = client.post(
        "/ws_collab/captioner/config",
        headers=admin_headers,
        json={"disable_google_meet": "true"},
    )
    assert invalid.status_code == 400
    persisted = json.loads(
        (
            tmp_path
            / "collab_state"
            / "captioner_settings.json"
        ).read_text(encoding="utf-8")
    )
    assert persisted["disable_google_meet"] is True


def test_captioner_finalization_failure_is_retryable_http(
    client, app_context, monkeypatch
) -> None:
    original_finalize = app_context.service._finalize
    attempts = 0

    def fail_once(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("injected finalization failure")
        return original_finalize(*args, **kwargs)

    monkeypatch.setattr(app_context.service, "_finalize", fail_once)
    with TestClient(
        client.app,
        base_url="http://127.0.0.1:8802",
        client=("127.0.0.1", 50000),
    ) as local:
        page = local.get("/ws_collab/captioner/")
        token = re.search(
            r'name="ws-captioner-token" content="([^"]+)"', page.text
        ).group(1)
        headers = {
            "x-ws-collab-captioner-token": token,
            "content-type": "application/json",
            "origin": "http://127.0.0.1:8802",
        }
        register = local.post(
            "/ws_collab/captioner/heartbeat",
            headers=headers,
            json={
                "session_id": envelope()["session_id"],
                "instance_id": envelope()["instance_id"],
                "boot_id": app_context.service.boot_id,
                "state": "standby",
                "queue_depth": 0,
                "recognizer_supported": True,
            },
        )
        assert register.status_code == 200
        first = local.post(
            "/ws_collab/captioner/ingest",
            headers=headers,
            json=envelope(final=True),
        )
        assert first.status_code == 503
        assert first.json()["error"]["code"] == "captioner_finalization_pending"
        retry = local.post(
            "/ws_collab/captioner/ingest",
            headers=headers,
            json=envelope(final=True),
        )
        assert retry.status_code == 200
        assert retry.json()["accepted"] == 1


def test_captioner_heartbeat_changes_stale_health(tmp_path: Path) -> None:
    now = [100.0]
    source = BrowserCaptioner(
        tmp_path, boot_id="boot", publish_item=lambda _item: None, clock=lambda: now[0]
    )
    assert source.status()["state"] == "stale"
    source.heartbeat(
        {
            "session_id": "session",
            "instance_id": "owner",
            "owns_lease": True,
            "state": "listening",
            "queue_depth": 2,
            "recognizer_supported": True,
            "mic_permission": "granted",
        }
    )
    assert source.status()["healthy"] is True
    now[0] += 16
    assert source.status()["state"] == "stale"


def test_captioner_heartbeat_validates_and_exposes_vad(tmp_path: Path) -> None:
    source = BrowserCaptioner(
        tmp_path, boot_id="boot", publish_item=lambda _item: None
    )
    base = {
        "session_id": "session",
        "instance_id": "owner",
        "owns_lease": True,
        "state": "listening",
        "queue_depth": 0,
    }
    vad = {
        "source": "browser_rms_vad",
        "available": True,
        "state": "silence",
        "rms": 0.003,
        "noise_floor": 0.002,
        "threshold": 0.009,
        "current_silence_ms": 80,
        "frame_interval_ms": 20,
        "permission": "granted",
        "error": None,
    }
    source.heartbeat({**base, "vad": vad})
    assert source.status()["vad"] == vad
    assert source.status()["instance_id"] == "owner"
    for field, value in [
        ("rms", -1),
        ("threshold", float("nan")),
        ("frame_interval_ms", 2),
        ("current_silence_ms", 86_400_001),
    ]:
        with pytest.raises(Exception):
            source.heartbeat({**base, "vad": {**vad, field: value}})
    with pytest.raises(Exception):
        source.heartbeat({**base, "vad": {**vad, "pcm": [0.1]}})


def test_captioner_heartbeat_validates_private_actual_track_metadata(tmp_path: Path) -> None:
    source = BrowserCaptioner(
        tmp_path, boot_id="boot", publish_item=lambda _item: None
    )
    base = {
        "session_id": "session",
        "instance_id": "owner",
        "owns_lease": True,
        "state": "listening",
        "queue_depth": 0,
    }
    metadata = {
        "input_scope": "microphone",
        "track_label": "Default Microphone",
        "device_fingerprint": None,
        "echo_cancellation": True,
        "noise_suppression": True,
        "auto_gain_control": False,
        "channel_count": 1,
        "sample_rate": 48000,
    }
    source.heartbeat({**base, "input": metadata})
    assert source.status()["input"] == metadata
    assert "device_id" not in source.status()["input"]
    for malformed in (
        {**metadata, "device_id": "privacy-sensitive"},
        {**metadata, "input_scope": "browser_tab"},
        {**metadata, "noise_suppression": "yes"},
        {**metadata, "track_label": "x" * 161},
    ):
        with pytest.raises(Exception):
            source.heartbeat({**base, "input": malformed})


def test_local_mic_floor_orders_hangover_and_stale_fail_open() -> None:
    now = [100.0]
    floor = LocalMicFloor(
        clock=lambda: now[0], hangover_seconds=0.35, stale_seconds=3.0
    )

    def edge(seq: int, event: str, state: str) -> dict:
        return {
            "session_id": "session",
            "instance_id": "owner",
            "epoch": 1,
            "seq": seq,
            "event": event,
            "state": state,
            "at": "2026-09-12T00:00:00Z",
        }

    assert floor.apply(edge(1, "speech_start", "speech"))["acquired"] is True
    assert floor.status()["blocked"] is True
    assert floor.apply(edge(1, "speech_start", "speech"))["duplicate"] is True
    assert floor.apply(edge(0, "speech_start", "speech"))["stale"] is True
    floor.apply(edge(2, "speech_end", "silence"))
    assert floor.status()["state"] == "hangover"
    now[0] += 0.2
    assert floor.apply(edge(3, "speech_start", "speech"))["acquired"] is True
    floor.apply(edge(4, "speech_end", "silence"))
    now[0] += 0.351
    assert floor.status()["state"] == "clear"
    floor.apply(edge(5, "state_sync", "silence"))
    assert floor.status()["state"] == "clear"
    now[0] += 3.1
    assert floor.status()["state"] == "unknown"
    assert floor.status()["blocked"] is False


def test_floor_release_never_auto_speaks_and_debate_can_resume(service) -> None:
    now = [100.0]
    service.local_mic_floor = LocalMicFloor(
        clock=lambda: now[0], hangover_seconds=0.35, stale_seconds=3.0
    )

    def edge(seq: int, event: str, state: str) -> dict:
        return {
            "session_id": "debate",
            "instance_id": "mic-owner",
            "epoch": 1,
            "seq": seq,
            "event": event,
            "state": state,
            "at": "2026-09-12T00:00:00Z",
        }

    service.local_mic_floor.apply(edge(1, "speech_start", "speech"))
    blocked = service.speak("debater", "I will wait")
    assert blocked["blocked"] is True
    service.local_mic_floor.apply(edge(2, "speech_end", "silence"))
    before = len(service.store.tail(STREAM_TTS, 100))
    now[0] += 0.351
    assert service.local_mic_floor.status()["state"] == "clear"
    assert len(service.store.tail(STREAM_TTS, 100)) == before

    resumed = service.speak("debater", "Now I can respond")
    assert resumed["blocked"] is False
    assert asyncio.run(service.tts.process_next()) is True


def test_finalization_failure_retries_without_duplicate_outputs(
    tmp_path: Path, monkeypatch
) -> None:
    config = make_config(tmp_path)
    store = make_event_store(config)
    service = WsCollabService(config, store)
    original_publish = service.publish
    calls = 0

    def fail_once(*args, **kwargs):
        nonlocal calls
        if kwargs.get("type") == HEARD_SPEECH:
            calls += 1
            if calls == 1:
                raise RuntimeError("injected after resolved output")
        return original_publish(*args, **kwargs)

    monkeypatch.setattr(service, "publish", fail_once)
    try:
        register_service_captioner(service)
        with pytest.raises(CaptionerFinalizationError):
            service.ingest_captioner(envelope(final=True))
        assert sum(
            event.type == STT_FINAL_RESULT
            for event in store.tail("stt_transcripts", 20)
        ) == 1
        assert sum(
            event.type == TRANSCRIPT_RESOLVED
            for event in store.tail("stt_transcripts", 20)
        ) == 1

        result = service.ingest_captioner(envelope(final=True))
        assert result["accepted"] == 1
        assert service.ingest_captioner(envelope(final=True))["accepted"] == 0
        transcripts = store.tail("stt_transcripts", 20)
        heard = store.tail("translated_audio", 20)
        assert sum(event.type == STT_FINAL_RESULT for event in transcripts) == 1
        assert sum(event.type == TRANSCRIPT_RESOLVED for event in transcripts) == 1
        assert sum(event.type == HEARD_SPEECH for event in heard) == 1
        final = next(event for event in transcripts if event.type == STT_FINAL_RESULT)
        assert final.data["speech_started_at"] == envelope()["speech_started_at"]
        assert final.data["speech_ended_at"] == envelope()["speech_ended_at"]
        assert final.data["silence_before_ms"] == 420
    finally:
        store.close()


def test_pending_finalization_recovers_on_service_restart(
    tmp_path: Path, monkeypatch
) -> None:
    config = make_config(tmp_path)
    first_store = make_event_store(config)
    first = WsCollabService(config, first_store)
    register_service_captioner(first)
    monkeypatch.setattr(
        first,
        "_finalize",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("injected restart window")
        ),
    )
    with pytest.raises(CaptionerFinalizationError):
        first.ingest_captioner(envelope(final=True))
    first_store.close()

    second_store = make_event_store(config)
    try:
        WsCollabService(config, second_store)
        transcripts = second_store.tail("stt_transcripts", 20)
        heard = second_store.tail("translated_audio", 20)
        assert sum(event.type == STT_FINAL_RESULT for event in transcripts) == 1
        assert sum(event.type == TRANSCRIPT_RESOLVED for event in transcripts) == 1
        assert sum(event.type == HEARD_SPEECH for event in heard) == 1
        state = json.loads(
            (config.state_dir / "captioner_dedupe.json").read_text(encoding="utf-8")
        )
        assert {row["status"] for row in state["finalizations"].values()} == {
            "completed"
        }
    finally:
        second_store.close()


def test_captioner_supervisor_singleton_pause_and_backoff(tmp_path: Path) -> None:
    clock = [10.0]
    source = BrowserCaptioner(
        tmp_path, boot_id="boot", publish_item=lambda _item: None, clock=lambda: clock[0]
    )
    backend = navigator.InMemoryBrowserBackend()
    supervisor = CaptionerSupervisor(
        source,
        page_url="http://127.0.0.1:8802/ws_collab/captioner/",
        cdp_endpoint="http://127.0.0.1:9223",
        profile=tmp_path / "profile",
        browser_path=lambda: "chrome.exe",
        navigator_module=navigator,
        cdp_available=lambda: True,
        clock=lambda: clock[0],
        jitter=lambda: 0,
        suppress_launch=False,
    )
    navigator.set_browser_backend(backend)
    try:
        assert supervisor.run_cycle()["tab_present"] is True
        assert len(backend.targets) == 1
        assert ("open-background", "http://127.0.0.1:8802/ws_collab/captioner/") in backend.actions
        assert supervisor.run_cycle()["tab_present"] is True
        assert len(backend.targets) == 1
        assert not [action for action in backend.actions if action[0] == "foreground"]
        source.settings.update({"paused": True})
        assert supervisor.run_cycle()["paused"] is True
        backend.targets.clear()
        source.settings.update({"paused": False})
        backend.fail_next = RuntimeError("closed")
        failed = supervisor.run_cycle()
        assert failed["retry_at"] == 11.0
        assert "captioner tab" in supervisor.run_cycle()["error"]
        clock[0] = 11.0
        assert supervisor.run_cycle()["tab_present"] is True
    finally:
        navigator.set_browser_backend(None)


def test_captioner_supervisor_reuses_existing_tab_without_periodic_foreground(
    tmp_path: Path,
) -> None:
    source = BrowserCaptioner(
        tmp_path, boot_id="boot", publish_item=lambda _item: None
    )
    backend = navigator.InMemoryBrowserBackend()
    existing, _, _ = backend.open_tab(
        "http://127.0.0.1:9223",
        "http://127.0.0.1:8802/ws_collab/captioner/",
    )
    supervisor = CaptionerSupervisor(
        source,
        page_url="http://127.0.0.1:8802/ws_collab/captioner/",
        cdp_endpoint="http://127.0.0.1:9223",
        profile=tmp_path / "profile",
        browser_path=lambda: "chrome.exe",
        navigator_module=navigator,
        cdp_available=lambda: True,
        suppress_launch=False,
    )
    navigator.set_browser_backend(backend)
    try:
        supervisor.run_cycle()
        supervisor.run_cycle()
    finally:
        navigator.set_browser_backend(None)

    assert list(backend.targets) == [existing.id]
    assert len([action for action in backend.actions if action[0] == "navigate"]) == 1
    assert not [action for action in backend.actions if action[0] == "foreground"]


def test_captioner_launch_is_visible_and_never_headless(tmp_path: Path) -> None:
    source = BrowserCaptioner(
        tmp_path, boot_id="boot", publish_item=lambda _item: None
    )
    backend = navigator.InMemoryBrowserBackend()
    supervisor = CaptionerSupervisor(
        source,
        page_url="http://127.0.0.1:8802/ws_collab/captioner/",
        cdp_endpoint="http://127.0.0.1:9223",
        profile=tmp_path / "profile",
        browser_path=lambda: "chrome.exe",
        navigator_module=navigator,
        cdp_available=lambda: False,
        suppress_launch=False,
    )
    navigator.set_browser_backend(backend)
    try:
        supervisor.run_cycle()
    finally:
        navigator.set_browser_backend(None)

    launch = next(value for action, value in backend.actions if action == "launch")
    assert "--headless" not in launch
    assert "--remote-debugging-port=9223" in launch
    assert f"--user-data-dir={tmp_path / 'profile'}" in launch
    assert "http://127.0.0.1:8802/ws_collab/captioner/" in launch
    assert "google.com" not in launch.lower()
    assert "authuser" not in launch.lower()


def test_service_captioner_uses_dedicated_profile_and_port(service) -> None:
    status = service.captioner_status()
    captioner_profile = Path(status["profile_path"]).resolve()
    meet_profile = service._meet_profile_path().resolve()
    assert captioner_profile.name == "chrome_captioner"
    assert captioner_profile != meet_profile
    assert service.config.captioner_cdp_port not in {9222, 9223}
    assert status["cdp_endpoint"].endswith(f":{service.config.captioner_cdp_port}")
    assert status["profile_disclosure"] == (
        "Isolated browser profile: no Google login is used or inherited."
    )


def test_cdp_backend_opens_captioner_target_in_background() -> None:
    calls = []

    class BrowserConnection:
        def call(self, method, params):
            calls.append((method, params))
            return {"targetId": "captioner-tab"}

        def close(self):
            calls.append(("close", None))

    def http_json(url, **_kwargs):
        if url.endswith("/json/version"):
            return {"webSocketDebuggerUrl": "ws://browser"}
        if url.endswith("/json"):
            return [
                {
                    "id": "captioner-tab",
                    "url": "http://127.0.0.1:8802/ws_collab/captioner/",
                    "webSocketDebuggerUrl": "ws://captioner",
                    "type": "page",
                }
            ]
        raise AssertionError(f"unexpected CDP HTTP request: {url}")

    backend = navigator.CdpBrowserBackend(
        http_json=http_json,
        tab_factory=lambda _url: BrowserConnection(),
    )
    target, error, action = backend.open_tab(
        "http://127.0.0.1:9223",
        "http://127.0.0.1:8802/ws_collab/captioner/",
        background=True,
    )

    assert error is None
    assert action == "open-background-tab"
    assert target is not None and target.id == "captioner-tab"
    assert calls[0] == (
        "Target.createTarget",
        {
            "url": "http://127.0.0.1:8802/ws_collab/captioner/",
            "background": True,
        },
    )


def test_service_startup_starts_captioner_supervisor_but_never_meet(
    service, monkeypatch
) -> None:
    meeting = "https://meet.google.com/abc-defg-hij"
    profile = service._meet_profile_path()
    service.meet_browser_settings.set_profile_state(
        profile,
        meeting_routing={meeting: {"autostart": True}},
    )
    calls: list[str] = []

    async def captioner_run() -> None:
        calls.append("captioner")
        await asyncio.Event().wait()

    async def tts_start() -> None:
        calls.append("tts-start")

    async def tts_stop() -> None:
        calls.append("tts-stop")

    def unexpected_meet_start(*_args, **_kwargs):
        raise AssertionError("service startup must not start the Meet bridge")

    monkeypatch.setattr(service.captioner_supervisor, "run", captioner_run)
    monkeypatch.setattr(service.captioner_supervisor, "shutdown", lambda: {})
    monkeypatch.setattr(service.tts, "start", tts_start)
    monkeypatch.setattr(service.tts, "stop", tts_stop)
    monkeypatch.setattr(service, "start_meet_bridge", unexpected_meet_start)

    async def exercise() -> None:
        await service.startup()
        await asyncio.sleep(0)
        assert calls == ["tts-start", "captioner"]
        await service.shutdown()

    asyncio.run(exercise())


def test_captioner_shutdown_stops_and_closes_only_owned_pages(tmp_path: Path) -> None:
    source = BrowserCaptioner(
        tmp_path, boot_id="boot", publish_item=lambda _item: None
    )
    backend = navigator.InMemoryBrowserBackend()
    caption, _, _ = backend.open_tab(
        "http://127.0.0.1:9223",
        "http://127.0.0.1:8802/ws_collab/captioner/",
    )
    meet, _, _ = backend.open_tab(
        "http://127.0.0.1:9223", "https://meet.google.com/abc-defg-hij"
    )
    unrelated, _, _ = backend.open_tab(
        "http://127.0.0.1:9223", "http://127.0.0.1:8802/admin/"
    )
    supervisor = CaptionerSupervisor(
        source,
        page_url="http://127.0.0.1:8802/ws_collab/captioner/",
        cdp_endpoint="http://127.0.0.1:9223",
        profile=tmp_path / "profile",
        browser_path=lambda: "chrome.exe",
        navigator_module=navigator,
        cdp_available=lambda: True,
        suppress_launch=False,
    )
    navigator.set_browser_backend(backend)
    try:
        result = supervisor.shutdown()
    finally:
        navigator.set_browser_backend(None)

    assert result == {"stopped_tabs": 1, "errors": []}
    assert ("script", "window.__wsCollabCaptionerShutdown ? window.__wsCollabCaptionerShutdown() : null") in backend.actions
    assert ("close", caption.id) in backend.actions
    assert meet.id in backend.targets
    assert unrelated.id in backend.targets


def test_captioner_supervisor_repairs_stale_generation_with_backoff(tmp_path: Path) -> None:
    now = [10.0]
    source = BrowserCaptioner(
        tmp_path, boot_id="boot", publish_item=lambda _item: None, clock=lambda: now[0]
    )
    backend = navigator.InMemoryBrowserBackend()
    supervisor = CaptionerSupervisor(
        source,
        page_url="http://127.0.0.1:8802/ws_collab/captioner/",
        cdp_endpoint="http://127.0.0.1:9223",
        profile=tmp_path / "profile",
        browser_path=lambda: "chrome.exe",
        navigator_module=navigator,
        cdp_available=lambda: True,
        clock=lambda: now[0],
        jitter=lambda: 0,
        suppress_launch=False,
    )
    navigator.set_browser_backend(backend)
    try:
        supervisor.run_cycle()
        assert not [action for action in backend.actions if action[0] == "navigate"]
        now[0] = 21.0
        supervisor.run_cycle()
        assert not [action for action in backend.actions if action[0] == "navigate"]
        now[0] = 22.0
        supervisor.run_cycle()
        assert len([action for action in backend.actions if action[0] == "navigate"]) == 1
        first_generation = next(iter(backend.targets))
        now[0] = 34.0
        supervisor.run_cycle()
        assert ("close", first_generation) in backend.actions
        assert len(backend.targets) == 1
        repairs = len([action for action in backend.actions if action[0] in {"navigate", "close"}])
        supervisor.run_cycle()
        assert len([action for action in backend.actions if action[0] in {"navigate", "close"}]) == repairs
        source.settings.update({"paused": True})
        now[0] = 100.0
        supervisor.run_cycle()
        assert len([action for action in backend.actions if action[0] in {"navigate", "close"}]) == repairs
    finally:
        navigator.set_browser_backend(None)


def test_captioner_restart_health_expires_and_listening_resets(tmp_path: Path) -> None:
    now = [100.0]
    source = BrowserCaptioner(
        tmp_path, boot_id="boot", publish_item=lambda _item: None, clock=lambda: now[0]
    )
    base = {
        "session_id": "session",
        "instance_id": "owner",
        "owns_lease": True,
        "queue_depth": 0,
        "recognizer_supported": True,
        "mic_permission": "granted",
        "restart_count": 3,
    }
    source.heartbeat({**base, "state": "restarting"})
    assert source.status()["health"] == "degraded"
    assert source.status()["healthy"] is True
    now[0] = 111.0
    assert source.status()["state"] == "error"
    assert source.status()["healthy"] is False
    source.heartbeat({**base, "state": "listening", "restart_count": 0})
    assert source.status()["health"] == "ok"
    assert source.status()["healthy"] is True


def test_meet_is_not_stt_driver_and_caption_is_conversation(service) -> None:
    specs, _ = discover_stt_drivers()
    assert "google_meet" not in {spec.id for spec in specs}
    item = {
        "text": "Meeting context",
        "speaker": "Alice",
        "role": "host",
        "meeting_url": "https://meet.google.com/abc-defg-hij",
        "key": "host:1",
        "final": True,
    }
    service.ingest_meeting_caption(item)
    service.ingest_meeting_caption(item)
    conversation = service.store.tail("conversation", 5)
    meet_events = [
        event for event in conversation
        if event.data.get("source") == "google_meet_caption"
    ]
    assert len(meet_events) == 1
    assert meet_events[0].type == CONVERSATION_MESSAGE
    assert not service.store.tail("translated_audio", 5)
    assert not service.store.tail("stt_transcripts", 5)


def test_preferred_captioner_suppresses_only_safe_recent_meet_duplicates(
    service,
) -> None:
    register_service_captioner(service)
    service.ingest_captioner(
        envelope(final=True, text="The browser caption is safely duplicated")
    )
    duplicate = service.ingest_meeting_caption(
        {
            "text": "browser caption is safely duplicated",
            "speaker": "Alice",
            "role": "host",
            "meeting_url": "https://meet.google.com/abc-defg-hij",
            "key": "host:preferred",
            "final": True,
        }
    )
    unrelated = service.ingest_meeting_caption(
        {
            "text": "This is unrelated meeting context",
            "speaker": "Bob",
            "role": "host",
            "meeting_url": "https://meet.google.com/abc-defg-hij",
            "key": "host:unrelated",
            "final": True,
        }
    )
    assert duplicate["acknowledged"] is True
    assert duplicate["reason"] == "preferred_browser_captioner"
    assert unrelated.get("suppressed") is not True
    meet_rows = [
        event
        for event in service.store.tail("conversation", 20)
        if event.data.get("source") == "google_meet_caption"
    ]
    assert [event.data["text"] for event in meet_rows] == [
        "Bob: This is unrelated meeting context"
    ]
    audit = service.store.tail("system_audit", 20)
    suppression = next(event for event in audit if event.type == "MEET_CAPTION_SUPPRESSED")
    assert "text" not in suppression.data
    assert suppression.data["reason"] == "preferred_browser_captioner"


def test_preferred_captioner_index_is_bounded_and_expires(service) -> None:
    now = __import__("time").time()
    for index in range(RECENT_FINAL_LIMIT + 10):
        service._recent_captioner_finals.append(
            {
                "normalized_text": f"caption number {index} has enough safe words",
                "received_at": now,
                "correlation_id": str(index),
                "utterance_id": str(index),
            }
        )
    assert len(service._recent_captioner_finals) == RECENT_FINAL_LIMIT
    service._recent_captioner_finals[-1]["received_at"] = (
        now - RECENT_FINAL_WINDOW_SECONDS - 1
    )
    assert (
        service._recent_browser_caption_match(
            service._recent_captioner_finals[-1]["normalized_text"]
        )
        is None
    )


def test_disable_meet_acks_without_publication_and_preserves_meet_config(
    service,
) -> None:
    before = service.meet_browser_settings.all()
    service.set_captioner_config({"disable_google_meet": True})
    body = {
        "text": "Retained only as diagnostic metadata",
        "speaker": "Alice",
        "role": "host",
        "meeting_url": "https://meet.google.com/abc-defg-hij",
        "key": "host:disabled",
        "revision": "1",
        "final": True,
    }
    first = service.ingest_meeting_caption(body)
    second = service.ingest_meeting_caption(body)
    assert first["accepted"] is True and first["suppressed"] is True
    assert first["reason"] == "google_meet_disabled"
    assert second["accepted"] is True and second["duplicate"] is True
    assert not service.store.tail("conversation", 5)
    assert service.meet_browser_settings.all() == before
    assert service.captioner_status()["meet_caption_suppressions"][
        "google_meet_disabled"
    ] == 1


def test_disable_other_stts_skips_audio_engines_but_not_manual_ingest(
    service, monkeypatch
) -> None:
    calls = []

    async def fake_run_stt(*args, **kwargs):
        calls.append((args, kwargs))
        return [
            __import__("ws_collab.stt.base", fromlist=["Hypothesis"]).Hypothesis(
                engine="fallback_alpha",
                model="test",
                raw_text="resumed",
                normalized_text="resumed",
                confidence=0.9,
                language="en",
            )
        ]

    monkeypatch.setattr("ws_collab.service.run_stt", fake_run_stt)
    service.set_captioner_config({"disable_other_stts": True})
    segment = __import__(
        "ws_collab.audio.segment", fromlist=["AudioSegment"]
    ).AudioSegment(
        correlation_id="segment-policy-test",
        source_kind="client",
        device_id="microphone",
    )
    skipped = asyncio.run(service.process_segment(segment))
    assert skipped["reason"] == "disabled_by_chrome_captions_policy"
    assert calls == []
    manual = service.ingest_transcript(engine="manual", text="manual remains live")
    assert manual["resolved"]["resolved_text"] == "manual remains live"
    configured = service.ingest_transcript(
        engine=service.stt_engines[0].name, text="must be skipped"
    )
    assert configured["skipped"] is True
    status = service.status()["subsystems"]["stt"]
    assert status["disabled_by_chrome_captions_policy"] is True
    assert all(
        value["state"] == "disabled_by_chrome_captions_policy"
        for value in status["engine_states"].values()
    )
    service.set_captioner_config({"disable_other_stts": False})
    resumed = asyncio.run(service.process_segment(segment))
    assert resumed["resolved"]["resolved_text"] == "resumed"
    assert len(calls) == 1


def test_captioner_page_contains_restart_queue_and_privacy_contract() -> None:
    script_path = (
        Path(__file__).parents[1]
        / "src"
        / "ws_collab"
        / "captioner"
        / "captioner.js"
    )
    source = script_path.read_text(encoding="utf-8")
    html = script_path.with_name("index.html").read_text(encoding="utf-8")
    assert "indexedDB.open" in source
    assert "classifyRecognitionError" in source
    assert "scheduleRestart" in source
    assert "payload.results" in source
    assert "navigator.locks.request" in source
    assert 'document.visibilityState !== "visible"' in source
    assert "First run requires visible microphone permission" in source
    assert "!hasOwnership || recognizer !== instance" in source
    assert 'window.addEventListener("pagehide"' in source
    assert "ownership.release()" in source
    assert 'queueVadTransition("speech_start", "speech"' in source
    assert 'queueVadTransition("speech_end", "silence"' in source
    assert 'queueVadTransition("state_sync", "unavailable"' in source
    assert "getUserMedia" not in source  # lifecycle is isolated in the pure runtime helper
    runtime = script_path.with_name("captioner_runtime.js").read_text(encoding="utf-8")
    assert "getUserMedia" in runtime
    assert "context.audioWorklet.addModule" in runtime
    assert "this.frames.push(samples, currentFrame)" in runtime
    assert "getFloatTimeDomainData" not in runtime
    assert "browser_rms_vad" in runtime
    assert "recording-indicator" in html
    assert "not offline or local" in html
    assert "Input scope: microphone only." in html
    assert "Other tabs are not captured directly." in html
    assert "cannot be reliably identified" in html
    assert "Chrome/OS default input" in html
    assert "Recognition and silence detection share the same microphone track" in html
    assert "instance.start(audioTrack)" in source
    assert "noiseSuppression: { ideal: true }" in runtime
    assert "settings.deviceId" not in runtime


def test_captioner_js_identity_and_fallback_lease_lifecycle() -> None:
    runtime_path = (
        Path(__file__).parents[1]
        / "src"
        / "ws_collab"
        / "captioner"
        / "captioner_runtime.js"
    )
    harness = r"""
const assert = require("node:assert/strict");
const runtime = require(process.argv[1]);
class Storage {
  constructor() { this.values = new Map(); }
  getItem(key) { return this.values.has(key) ? this.values.get(key) : null; }
  setItem(key, value) { this.values.set(key, String(value)); }
  removeItem(key) { this.values.delete(key); }
}
const storage = new Storage();
const uuids = [
  "00000000-0000-4000-8000-000000000001",
  "00000000-0000-4000-8000-000000000002",
  "00000000-0000-4000-8000-000000000003",
];
const cryptoApi = { randomUUID: () => uuids.shift() };
const first = runtime.createIdentity(storage, cryptoApi);
const second = runtime.createIdentity(storage, cryptoApi);
assert.equal(first.sessionId, second.sessionId);
assert.notEqual(first.instanceId, second.instanceId);
assert.notEqual(first.utteranceId(1, 0), second.utteranceId(1, 0));
assert.match(first.utteranceId(1, 0), new RegExp(first.instanceId));
assert.equal(runtime.nextFallbackSequence(storage), 1);
assert.equal(runtime.nextFallbackSequence(storage), 2);

let now = 1000;
const a = new runtime.StorageLease(storage, { ownerId: "owner-a", now: () => now, ttlMs: 100 });
const b = new runtime.StorageLease(storage, { ownerId: "owner-b", now: () => now, ttlMs: 100 });
assert.equal(a.tryAcquire(), true);
assert.equal(b.tryAcquire(), false);
a.release();
assert.equal(b.tryAcquire(), true);
now = 1200;
assert.equal(a.tryAcquire(), true);
assert.equal(b.renew(), false);
assert.equal(runtime.classifyRecognitionError("network"), "transient");
for (const error of ["not-allowed", "service-not-allowed", "audio-capture", "language-not-supported", "bad-grammar"]) {
  assert.equal(runtime.classifyRecognitionError(error), "terminal");
}
const interims = Array.from({length: 5}, (_, index) => ({
  id: `i${index}`, seq: index + 1, utterance_id: `u${index}`, revision: 1, is_final: false,
}));
const prunedInterims = runtime.pruneQueueRows(interims, 3);
assert.deepEqual(prunedInterims.kept.map((row) => row.id), ["i2", "i3", "i4"]);
const finals = Array.from({length: 4}, (_, index) => ({
  id: `f${index}`, seq: index + 1, utterance_id: `f${index}`, revision: 1, is_final: true,
}));
const prunedFinals = runtime.pruneQueueRows(finals, 2);
assert.equal(prunedFinals.kept.length, 4);
assert.equal(prunedFinals.removedIds.length, 0);
assert.equal(prunedFinals.fullOfFinals, true);
const queuedPause = {
  ...finals[0],
  pauses: [{duration_ms: 80, source: "browser_rms_vad", after_char: 2}],
};
assert.deepEqual(runtime.pruneQueueRows([queuedPause], 2).kept[0].pauses, queuedPause.pauses);
let heartbeatNow = 0;
const guard = new runtime.HeartbeatFailureGuard({now: () => heartbeatNow, graceMs: 100});
assert.equal(guard.failure(), false);
heartbeatNow = 99;
assert.equal(guard.failure(), false);
heartbeatNow = 100;
assert.equal(guard.failure(), true);
guard.success();
assert.equal(guard.expired(), false);

// Deterministic 20ms frames detect short and repeated pauses without idle noise.
let wall = Date.parse("2026-09-11T10:00:00.000Z");
let mono = 0;
const vad = new runtime.BrowserRmsVad();
vad.start("epoch");
for (let i = 0; i < 20; i += 1) {
  vad.processFrame(0.004, mono, wall, "epoch");
  mono += 20; wall += 20;
}
assert.ok(vad.noiseFloor > 0.003);
assert.equal(vad.status().state, "idle");
assert.equal(vad.currentSilenceMs(), null);
vad.processFrame(0.1, mono, wall, "epoch"); mono += 20; wall += 20;
let events = vad.processFrame(0.1, mono, wall, "epoch"); mono += 20; wall += 20;
assert.equal(events[0].type, "speech_start");
for (const duration of [20, 40, 80, 240]) {
  const start = mono;
  while (mono < start + duration) {
    vad.processFrame(0.001, mono, wall, "epoch"); mono += 20; wall += 20;
  }
  events = vad.processFrame(0.1, mono, wall, "epoch"); mono += 20; wall += 20;
  if (duration >= 40) {
    events = events.concat(vad.processFrame(0.1, mono, wall, "epoch"));
    mono += 20; wall += 20;
  }
  const pause = events.find((event) => event.type === "pause");
  assert.equal(pause.duration_ms, duration);
  assert.equal(pause.source, "browser_rms_vad");
}
vad.processFrame(0.001, mono, wall, "epoch");
vad.reset();
mono += 5000; wall += 5000;
vad.start("restart");
vad.processFrame(0.1, mono, wall, "restart"); mono += 20; wall += 20;
events = vad.processFrame(0.1, mono, wall, "restart");
assert.equal(events.some((event) => event.type === "pause"), false);

const association = new runtime.PauseAssociation();
association.updateTranscript("hello");
const p1 = {
  type: "pause", duration_ms: 80,
  start_at: "2026-09-11T10:00:01.000Z", end_at: "2026-09-11T10:00:01.080Z",
  source: "browser_rms_vad",
};
assert.equal(association.record(p1), true);
assert.equal(association.record(p1), false);
association.updateTranscript("hello wide");
association.record({...p1, duration_ms: 240, start_at: "2026-09-11T10:00:02.000Z"});
let aligned = association.finalize("hello world", {consume: false});
assert.equal(aligned.pauses.length, 2);
assert.equal(aligned.pauses[0].after_char, 5);
assert.equal(aligned.pauses[0].alignment, "interim_prefix");
assert.equal(aligned.pauses[1].alignment, "approximate_text_position");
association.commit();
association.record({...p1, duration_ms: 40, start_at: "2026-09-11T10:00:03.000Z"});
aligned = association.finalize("next");
assert.equal(aligned.pauses[0].alignment, "between_utterances");
assert.equal(aligned.silence_before_ms, 40);
console.log("captioner runtime lifecycle ok");
"""
    completed = subprocess.run(
        ["node", "-e", harness, str(runtime_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert "lifecycle ok" in completed.stdout


def test_captioner_script_initializes_and_wires_microphone_permission() -> None:
    root = Path(__file__).parents[1] / "src" / "ws_collab"
    script_path = root / "captioner" / "captioner.js"
    runtime_path = root / "captioner" / "captioner_runtime.js"
    transcript_path = root / "admin" / "transcript_runtime.js"
    harness = r"""
const assert = require("node:assert/strict");
const scriptPath = process.argv[1];
const runtimePath = process.argv[2];
const transcriptPath = process.argv[3];

class Element {
  constructor() {
    this.textContent = "";
    this.className = "";
    this.value = "";
    this.checked = false;
    this.disabled = false;
    this.scrollTop = 0;
    this.scrollHeight = 0;
    this.listeners = {};
    this.style = {setProperty() {}};
  }
  addEventListener(type, listener) { this.listeners[type] = listener; }
  setAttribute() {}
  append() {}
  remove() {}
  replaceChildren() {}
  appendChild() {}
}
class Storage {
  constructor() { this.values = new Map(); }
  getItem(key) { return this.values.has(key) ? this.values.get(key) : null; }
  setItem(key, value) { this.values.set(key, String(value)); }
  removeItem(key) { this.values.delete(key); }
}

const elements = new Map();
const element = (id) => {
  if (!elements.has(id)) elements.set(id, new Element());
  return elements.get(id);
};
const metadata = {
  "ws-captioner-token": "token",
  "ws-captioner-boot": "boot",
  "ws-captioner-enabled": "true",
  "ws-captioner-paused": "false",
  "ws-captioner-language": "en-US",
  "ws-captioner-send-interims": "true",
};
const windowListeners = {};
global.window = global;
global.document = {
  visibilityState: "visible",
  querySelector: (selector) => ({
    content: metadata[selector.match(/name="([^"]+)"/)[1]],
  }),
  getElementById: element,
  addEventListener() {},
  createDocumentFragment: () => new Element(),
  createElement: () => new Element(),
  createElementNS: () => new Element(),
};
global.window.location = {href: "http://127.0.0.1/captioner/"};
global.window.addEventListener = (type, listener) => { windowListeners[type] = listener; };
global.localStorage = new Storage();
global.localStorage.setItem("ws-captioner-paused", "true");
Object.defineProperty(global, "crypto", {
  value: {randomUUID: () => "00000000-0000-4000-8000-000000000001"},
  configurable: true,
});
global.BroadcastChannel = undefined;
global.indexedDB = undefined;
const timeouts = new Map();
const intervals = new Map();
let timerId = 0;
global.setTimeout = (callback, ms) => {
  const id = ++timerId; timeouts.set(id, {callback, ms}); return id;
};
global.clearTimeout = (id) => timeouts.delete(id);
global.setInterval = (callback, ms) => {
  const id = ++timerId; intervals.set(id, {callback, ms}); return id;
};
global.clearInterval = (id) => intervals.delete(id);

let permissionQueries = 0;
const permissionStatus = {state: "granted", onchange: null};
const lifecycle = [];
let trackStops = 0;
let contextCloses = 0;
const track = {
  kind: "audio",
  readyState: "live",
  label: "Unit Test Mic",
  getSettings: () => ({
    deviceId: "must-not-leak",
    echoCancellation: true,
    noiseSuppression: true,
    autoGainControl: false,
    channelCount: 1,
    sampleRate: 48000,
  }),
  stop: () => { trackStops += 1; lifecycle.push("track-stop"); },
};
const stream = {getTracks: () => [track], getAudioTracks: () => [track]};
Object.defineProperty(global, "navigator", {
  value: {
    permissions: {
      query: async ({name}) => {
        assert.equal(name, "microphone");
        permissionQueries += 1;
        return permissionStatus;
      },
    },
    mediaDevices: {
      getUserMedia: async (constraints) => {
        assert.deepEqual(constraints.audio.channelCount, {ideal: 1});
        assert.deepEqual(constraints.audio.echoCancellation, {ideal: true});
        assert.deepEqual(constraints.audio.noiseSuppression, {ideal: true});
        assert.deepEqual(constraints.audio.autoGainControl, {ideal: false});
        lifecycle.push("getUserMedia");
        return stream;
      },
    },
  },
  configurable: true,
});

let starts = 0;
let stops = 0;
let recognizer;
class Recognition {
  constructor() { recognizer = this; }
  start(inputTrack) {
    assert.equal(inputTrack, track, "recognition must consume the very same microphone track as the VAD");
    lifecycle.push("recognition-start");
    starts += 1;
    this.onstart();
  }
  stop() {
    stops += 1;
    this.onend();
  }
  abort() { this.stop(); }
}
global.window.SpeechRecognition = Recognition;
global.window.AudioContext = class {
  constructor() {
    this.state = "running"; this.currentTime = 0;
    this.audioWorklet = {addModule: async () => lifecycle.push("worklet-module")};
    lifecycle.push("audio-context");
  }
  createMediaStreamSource(value) {
    assert.equal(value, stream);
    return {connect() {}, disconnect() { lifecycle.push("disconnect"); }};
  }
  createGain() { return {gain: {value: 1}, connect() {}, disconnect() {}}; }
  async close() { contextCloses += 1; lifecycle.push("context-close"); }
};
let processor;
global.window.AudioWorkletNode = class {
  constructor() { processor = this; this.port = {close() {}}; lifecycle.push("audio-worklet"); }
  connect() {}
  disconnect() {}
};
const runtime = require(runtimePath);
const Association = runtime.PauseAssociation;
let association;
runtime.PauseAssociation = class extends Association {
  constructor(...args) { super(...args); association = this; }
};
runtime.StorageLease = class {
  constructor() { this.key = "lease"; }
  tryAcquire() { return true; }
  renew() { return true; }
  release() {}
  isOwner() { return false; }
};
global.window.WsCaptionerRuntime = runtime;
global.window.WsCollabTranscript = require(transcriptPath);
let backendPaused = false;
global.fetch = async (url, options) => {
  const body = JSON.parse(options.body);
  if (url.endsWith("/control")) {
    if (body.action === "pause") backendPaused = true;
    if (body.action === "resume") backendPaused = false;
  }
  return ({
  ok: true,
  status: 200,
  json: async () => ({
    boot_id: "boot",
    config: {enabled: true, paused: backendPaused},
    instance: {may_capture: !backendPaused, selected: true, enabled: true, reason: null},
  }),
  });
};

require(scriptPath);
setImmediate(async () => {
  assert.equal(permissionQueries, 1);
  assert.equal(typeof permissionStatus.onchange, "function");
  assert.equal(starts, 1);
  assert.ok(lifecycle.indexOf("getUserMedia") < lifecycle.indexOf("recognition-start"));
  assert.equal(elements.get("status").textContent, "Listening");
  assert.equal(elements.get("input-device").textContent, "Unit Test Mic");
  assert.match(elements.get("input-processing").textContent, /noise suppression on/);
  assert.doesNotMatch(elements.get("input-processing").textContent, /must-not-leak/);

  let finishSequence;
  runtime.nextFallbackSequence = () => new Promise(resolve => { finishSequence = resolve; });
  const firstFinal = [{transcript:"first three words",confidence:.9}];
  firstFinal.isFinal = true;
  const saving = recognizer.onresult({resultIndex:0,results:[firstFinal]});
  association.record({
    type:"pause",duration_ms:4000,start_at:"2026-09-11T00:00:00Z",
    end_at:"2026-09-11T00:00:04Z",source:"browser_rms_vad",
  });
  await recognizer.onresult({resultIndex:0,results:[firstFinal]});
  finishSequence(1);
  await saving;
  assert.equal(association.pending.length, 1, "silence detected during a real final handler must survive its save");
  runtime.nextFallbackSequence = () => 2;
  const secondFinal = [{transcript:"last three words",confidence:.9}];
  secondFinal.isFinal = true;
  await recognizer.onresult({resultIndex:1,results:[firstFinal,secondFinal]});
  const queued = JSON.parse(localStorage.getItem("ws-captioner-queue"));
  assert.equal(queued.length, 2, "concurrent duplicate final callback must not enqueue a second copy");
  assert.equal(queued[1].pauses[0].duration_ms, 4000);
  assert.equal(queued[1].pauses[0].after_char, 0);

  recognizer.onerror({error: "no-speech"});
  recognizer.onend();
  assert.equal(trackStops, 0, "recognizer silence timeout must not stop the acoustic detector");
  assert.equal(contextCloses, 0);
  assert.match(elements.get("status").textContent, /^Reconnecting/);
  assert.ok(![...intervals.values()].some((timer) => timer.ms === 20));
  assert.equal(typeof processor.port.onmessage, "function", "audio thread continues while recognition restarts");
  const restart = [...timeouts.values()].find((timer) => timer.ms >= 500 && timer.ms < 1000);
  assert.ok(restart);
  await restart.callback();
  await new Promise(setImmediate);
  assert.equal(starts, 2);
  assert.equal(lifecycle.filter((step) => step === "getUserMedia").length, 1);

  await elements.get("pause").listeners.click();
  assert.equal(trackStops, 1);
  assert.match(elements.get("status").textContent, /^Paused: Listening paused by the user/);
  assert.equal(backendPaused, true);
  await elements.get("resume").listeners.click();
  await new Promise(setImmediate);
  assert.equal(backendPaused, false);
  assert.equal(starts, 3);
  assert.equal(elements.get("status").textContent, "Listening");
  const priorStops = stops;
  const priorTrackStops = trackStops;
  const priorContextCloses = contextCloses;
  permissionStatus.state = "denied";
  permissionStatus.onchange();
  assert.equal(stops, priorStops + 1);
  assert.equal(trackStops, priorTrackStops + 1);
  assert.equal(contextCloses, priorContextCloses + 1);
  assert.match(elements.get("status").textContent, /^Permission denied/);

  windowListeners.storage({key: "lease"});
  assert.equal(stops, priorStops + 1);
  assert.match(elements.get("status").textContent, /^Standby/);
  assert.deepEqual(global.window.__wsCollabCaptionerShutdown(), {
    stopped: true,
    instance_id: "00000000-0000-4000-8000-000000000001",
  });
  console.log("captioner initialization ok");
});
"""
    completed = subprocess.run(
        [
            "node",
            "-e",
            harness,
            str(script_path),
            str(runtime_path),
            str(transcript_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert "captioner initialization ok" in completed.stdout


def test_transcript_runtime_boundaries_markers_dedupe_pagination_and_safe_rendering() -> None:
    runtime_path = (
        Path(__file__).parents[1]
        / "src"
        / "ws_collab"
        / "admin"
        / "transcript_runtime.js"
    )
    harness = r"""
const assert = require("node:assert/strict");
const transcript = require(process.argv[1]);

function event(id, seq, ts, text, extra = {}) {
  return {
    id, seq, ts, type: "STT_FINAL_RESULT", source_id: "browser_captioner",
    data: {
      engine: "browser_captioner", is_final: true, raw_text: text,
      session_id: "session", utterance_id: `utterance-${seq}`, caption_seq: seq,
      ...extra,
    },
  };
}

assert.equal(transcript.SILENCE_MARKER_THRESHOLD_MS, 300);
assert.equal(transcript.formatDuration(420), "420ms");
assert.equal(transcript.formatDuration(2400), "2.4s");
assert.equal(transcript.formatDuration(68000), "1m 08s");

const first = event("a", 1, "2026-09-11T10:00:00.000Z", "first");
const measured = event("b", 2, "2026-09-11T10:00:02.400Z", "second", {silence_before_ms: 420});
const below = event("c", 3, "2026-09-11T10:00:02.600Z", "third", {silence_before_ms: 20});
let marker = transcript.markerBetween(transcript.finalCaption(first), transcript.finalCaption(measured));
assert.equal(marker.kind, "measured");
assert.equal(marker.text, "420ms");
assert.match(marker.title, /Measured locally from microphone RMS/);
assert.equal(marker.ariaLabel, "Measured acoustic silence 420 milliseconds");
assert.equal(transcript.markerBetween(transcript.finalCaption(measured), transcript.finalCaption(below)).text, "20ms");
const knownUnknown = event("c2", 31, "2026-09-11T10:00:04.000Z", "known unknown", {silence_before_ms: null});
assert.equal(transcript.markerBetween(transcript.finalCaption(below), transcript.finalCaption(knownUnknown)), null);
const legacy = event("d", 4, "2026-09-11T10:00:05.000Z", "legacy");
marker = transcript.markerBetween(transcript.finalCaption(below), transcript.finalCaption(legacy));
assert.equal(marker.kind, "approximate");
assert.match(marker.text, /^~/);
assert.match(marker.ariaLabel, /^Approximate gap /);

const duplicateLogical = event("different-id", 2, "2026-09-11T10:00:03.000Z", "duplicate");
const rows = transcript.mergeFinalEvents([], [legacy, measured, first, duplicateLogical]);
assert.deepEqual(rows.map((row) => row.text), ["first", "second", "legacy"]);
const liveRows = transcript.mergeFinalEvents(rows, [event("e", 5, "2026-09-11T10:00:06.000Z", "live")]);
assert.equal(liveRows.at(-1).text, "live");
const cutoff = transcript.clearViewCutoff(rows);
assert.deepEqual(transcript.rowsAfterCutoff(liveRows, cutoff).map((row) => row.text), ["live"]);
assert.equal(rows.length, 3);

class FakeNode {
  constructor(tag) { this.tag = tag; this.children = []; this.attributes = {}; this.style = {setProperty() {}}; this._text = ""; this.title = ""; }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map((child) => child.textContent).join(""); }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  append(...children) { this.children.push(...children); }
  appendChild(child) { this.children.push(child); return child; }
  replaceChildren(...children) { this._text = ""; this.children = [...children]; }
}
const documentRef = {
  createElement: (tag) => new FakeNode(tag),
  createElementNS: (_ns, tag) => new FakeNode(tag),
  createDocumentFragment: () => new FakeNode("#fragment"),
};
const container = new FakeNode("div");
const unsafe = event("unsafe", 6, "2026-09-11T10:00:07.000Z", "<img src=x onerror=alert(1)>");
transcript.renderTranscript(documentRef, container, transcript.mergeFinalEvents([], [unsafe]));
assert.equal(container.textContent, "<img src=x onerror=alert(1)>");
assert.equal(container.children[0].children[0].tag, "span");
const internal = event("internal", 7, "2026-09-11T10:00:08.000Z", "one two three", {
  pauses: [
    {duration_ms: 20, start_at: "2026-09-11T10:00:07.100Z", end_at: "2026-09-11T10:00:07.120Z", source: "browser_rms_vad", alignment: "interim_prefix", after_char: 3},
    {duration_ms: 80, start_at: "2026-09-11T10:00:07.200Z", end_at: "2026-09-11T10:00:07.280Z", source: "browser_rms_vad", alignment: "approximate_text_position", after_char: 7},
  ],
});
transcript.renderTranscript(documentRef, container, transcript.mergeFinalEvents([], [internal]));
const utterance = container.children[0].children[0];
const markers = utterance.children.filter((child) => /silence-marker/.test(child.className));
assert.deepEqual(markers.map((node) => node.textContent), ["20ms", "80ms"]);
assert.equal(markers[1].attributes["aria-label"], "Measured acoustic silence 80 milliseconds");
assert.match(markers[1].title, /Chrome supplies no word timestamps/);

let wall = 1000;
let mono = 10;
const boundaries = new transcript.SpeechBoundaryTracker({
  wallNow: () => wall,
  monotonicNow: () => mono,
});
boundaries.beginEpoch("normal");
boundaries.speechStart("normal");
assert.equal(boundaries.forUtterance("u1").silence_before_ms, null);
wall = 1800; mono = 810; boundaries.speechEnd("normal");
assert.ok(boundaries.forUtterance("u1").speech_ended_at);
wall = 2300; mono = 1310; boundaries.speechStart("normal");
assert.equal(boundaries.forUtterance("u2").silence_before_ms, 500);
assert.equal(boundaries.forUtterance("u2").silence_before_ms, 500);
assert.equal(boundaries.forUtterance("u3").silence_before_ms, null);
wall = 2500; mono = 1510; boundaries.speechEnd("normal");
wall = 2900; mono = 1910;
assert.equal(boundaries.currentSilenceMs(), 400);

// A recognizer ending without speechend cannot leak a boundary into its restart.
boundaries.abortEpoch();
wall = 4000; mono = 3000; boundaries.beginEpoch("ended");
boundaries.speechStart("ended");
wall = 4500; mono = 3500; boundaries.abortEpoch();
wall = 9500; mono = 8500; boundaries.beginEpoch("restart");
boundaries.speechStart("restart");
assert.equal(boundaries.forUtterance("after-restart").silence_before_ms, null);

// Pause and ownership loss invalidate both completed and pending boundaries.
wall = 10000; mono = 9000; boundaries.speechEnd("restart");
boundaries.abortEpoch();
wall = 11000; mono = 10000; boundaries.beginEpoch("after-pause");
boundaries.speechStart("after-pause");
assert.equal(boundaries.forUtterance("after-pause").silence_before_ms, null);
wall = 11500; mono = 10500; boundaries.speechEnd("after-pause");
boundaries.abortEpoch();
assert.equal(boundaries.currentSilenceMs(), null);
assert.deepEqual(boundaries.speechStart("after-pause"), {
  speech_started_at: null, speech_ended_at: null, silence_before_ms: null,
});
wall = 12500; mono = 11500; boundaries.beginEpoch("after-ownership-loss");
boundaries.speechStart("after-ownership-loss");
assert.equal(boundaries.forUtterance("after-ownership-loss").silence_before_ms, null);

// Shutdown rejects stale callbacks and cannot manufacture restart downtime.
wall = 13000; mono = 12000; boundaries.speechEnd("after-ownership-loss");
boundaries.abortEpoch();
wall = 20000; mono = 19000;
boundaries.speechEnd("after-ownership-loss");
boundaries.speechStart("after-ownership-loss");
assert.equal(boundaries.forUtterance("after-shutdown").silence_before_ms, null);
assert.equal(boundaries.currentSilenceMs(), null);

(async () => {
  const calls = [];
  const pages = {
    start: {events: [first, measured], has_more: true, next_cursor: "next"},
    next: {events: [legacy], has_more: true, next_cursor: "more"},
  };
  const result = await transcript.collectFinalPages(async (cursor, limit) => {
    calls.push([cursor, limit]);
    return pages[cursor || "start"];
  }, {maxItems: 3, pageSize: 2});
  assert.equal(result.events.length, 3);
  assert.equal(result.truncated, true);
  assert.deepEqual(calls, [[null, 2], ["next", 1]]);
  console.log("transcript runtime ok");
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
    completed = subprocess.run(
        ["node", "-e", harness, str(runtime_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert "transcript runtime ok" in completed.stdout


def test_captioner_final_is_marked_only_after_durable_enqueue() -> None:
    source = (
        Path(__file__).parents[1]
        / "src"
        / "ws_collab"
        / "captioner"
        / "captioner.js"
    ).read_text(encoding="utf-8")
    enqueue_at = source.index("const queued = await enqueue")
    finalized_at = source.index("finalized.add(utteranceId)", enqueue_at)
    assert enqueue_at < finalized_at
    assert "queue full; delivery required" in source
    assert "__wsCollabCaptionerShutdown" in source
    assert "await vadCapture.start(epoch)" in source
    assert "pauseAssociator.record(event)" in source
    assert "instance.onspeechstart" in source
    assert "recognizerSpeakingDiagnostic = true" in source
    assert "speechBoundaries" not in source
    assert "...speechMetadata" in source
