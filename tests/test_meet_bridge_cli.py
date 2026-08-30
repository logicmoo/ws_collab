from __future__ import annotations

import sys

import pytest

from ws_collab.meet_bridge import bridge


def test_cli_exposes_single_profile_account_options(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr(bridge, "ensure_default_profile_migrated", lambda: tmp_path / "profile")
    monkeypatch.setattr(sys, "argv", ["ws-collab-meet-bridge", "--help"])

    with pytest.raises(SystemExit) as stopped:
        bridge.main()

    help_text = capsys.readouterr().out
    assert stopped.value.code == 0
    assert "--role-authuser" in help_text
    assert "--role-email" in help_text
    assert "--companion-click" in help_text
    assert "--companion-heard-stt" in help_text
    assert "--companion-heard-stt-input-device" in help_text
    assert "--companion-click-interval" in help_text
    assert "--companion-click-mode" in help_text
    assert "--companion-click-trigger" in help_text
    assert "--companion-click-after" in help_text
    assert "--companion-click-silence-ms" in help_text
    assert "--companion-click-max-wait" in help_text
    assert "--companion-click-audio-rms-threshold" in help_text
    assert "--companion-click-ms" in help_text
    assert "--companion-click-sound" in help_text
    assert "--companion-click-f0" in help_text
    assert "--profile-mode" not in help_text
    assert "--companion-port" not in help_text


def test_sso_preflight_requires_two_distinct_live_sign_ins() -> None:
    detected_only = [
        {"email": "one@example.test", "authuser": 0},
        {"email": "two@example.test", "authuser": 1},
    ]
    duplicate_session = [
        {"email": "one@example.test", "authuser": 0, "signedIn": True},
        {"email": "one@example.test", "authuser": 1, "signedIn": True},
    ]
    wrong_slots = [
        {"email": "one@example.test", "authuser": 0, "signedIn": True},
        {"email": "two@example.test", "authuser": 2, "signedIn": True},
    ]
    ready = [
        {"email": "one@example.test", "authuser": 0, "signedIn": True},
        {"email": "two@example.test", "authuser": 1, "signedIn": True},
    ]

    assert bridge.sso_preflight_ready(detected_only, required_authusers={0, 1}) is False
    assert bridge.sso_preflight_ready(duplicate_session, required_authusers={0, 1}) is False
    assert bridge.sso_preflight_ready(wrong_slots, required_authusers={0, 1}) is False
    assert bridge.sso_preflight_ready(ready, required_authusers={0, 1}) is True
    assert bridge.sso_preflight_ready(
        ready,
        required_authusers={0, 1},
        required_accounts={0: "two@example.test", 1: "one@example.test"},
    ) is False


def test_sso_preflight_waits_until_both_accounts_are_live(monkeypatch) -> None:
    scans = iter([
        [{"email": "one@example.test", "authuser": 0, "signedIn": True}],
        [
            {"email": "one@example.test", "authuser": 0, "signedIn": True},
            {"email": "two@example.test", "authuser": 1, "signedIn": True},
        ],
    ])
    monkeypatch.setattr(bridge, "scan_signed_in_sso_accounts", lambda *_args, **_kwargs: next(scans))
    monkeypatch.setattr(bridge.time, "sleep", lambda _seconds: None)

    accounts = bridge.wait_for_sso_preflight(
        "http://127.0.0.1:9223",
        required_authusers={0, 1},
        browser_process=None,
    )

    assert [account["email"] for account in accounts] == ["one@example.test", "two@example.test"]


def test_role_account_uses_preflight_identity_when_meet_hides_dom_label() -> None:
    accounts = [
        {"email": "one@example.test", "authuser": 0, "signedIn": True},
        {"email": "two@example.test", "authuser": 1, "signedIn": True},
    ]

    matched = bridge.match_role_account(
        {"signedIn": False},
        tab_url="https://meet.google.com/abc-defg-hij?authuser=0",
        expected_authuser=0,
        expected_email="one@example.test",
        scanned_accounts=accounts,
    )

    assert matched == accounts[0]
    assert bridge.match_role_account(
        None,
        tab_url="https://meet.google.com/abc-defg-hij?authuser=1",
        expected_authuser=0,
        expected_email="one@example.test",
        scanned_accounts=accounts,
    ) is None


def test_controlled_meet_tabs_are_scoped_by_connector_and_dynamic_sso_slot(monkeypatch) -> None:
    tabs = [
        {
            "id": "first-role-sso",
            "type": "page",
            "url": "https://accounts.google.com/?authuser=4",
        },
        {
            "id": "first-role",
            "type": "page",
            "url": "https://meet.google.com/abc-defg-hij?authuser=4",
        },
        {
            "id": "second-role",
            "type": "page",
            "url": "https://meet.google.com/abc-defg-hij?authuser=9",
        },
    ]
    monkeypatch.setattr(bridge, "list_tabs", lambda _endpoint: tabs)

    for _ in range(16):
        assert bridge.find_controlled_meet_tab("http://127.0.0.1:9223", 4)["id"] == "first-role"
        assert bridge.find_controlled_meet_tab("http://127.0.0.1:9223", 9)["id"] == "second-role"
    assert bridge.find_controlled_meet_tab(
        "http://127.0.0.1:9223",
        4,
        wanted_room="https://meet.google.com/xyz-abcd-efg",
    ) is None


def test_driver_startup_requires_explicit_sso_role_assignments(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(bridge, "ensure_default_profile_migrated", lambda: tmp_path / "profile")
    monkeypatch.setattr(sys, "argv", ["ws-collab-meet-bridge", "--companion"])

    with pytest.raises(SystemExit, match="missing --role-authuser for: host, companion"):
        bridge.main()


def test_driver_startup_requires_expected_role_emails(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(bridge, "ensure_default_profile_migrated", lambda: tmp_path / "profile")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ws-collab-meet-bridge",
            "--companion",
            "--role-authuser",
            "host=0",
            "--role-authuser",
            "companion=1",
        ],
    )

    with pytest.raises(SystemExit, match="missing --role-email for: host, companion"):
        bridge.main()


def test_companion_click_flag_requires_companion(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(bridge, "ensure_default_profile_migrated", lambda: tmp_path / "profile")
    monkeypatch.setattr(sys, "argv", ["ws-collab-meet-bridge", "--companion-click"])

    with pytest.raises(SystemExit, match="--companion-click requires --companion"):
        bridge.main()


def test_companion_heard_stt_requires_companion(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(bridge, "ensure_default_profile_migrated", lambda: tmp_path / "profile")
    monkeypatch.setattr(sys, "argv", ["ws-collab-meet-bridge", "--companion-heard-stt"])

    with pytest.raises(SystemExit, match="--companion-heard-stt requires --companion"):
        bridge.main()


def test_companion_heard_stt_requires_output_device(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(bridge, "ensure_default_profile_migrated", lambda: tmp_path / "profile")
    monkeypatch.setattr(sys, "argv", ["ws-collab-meet-bridge", "--companion", "--companion-heard-stt"])

    with pytest.raises(SystemExit, match="--companion-heard-stt requires --companion-listen-device"):
        bridge.main()


def test_companion_click_command_parsing() -> None:
    assert bridge.parse_companion_click_command("/click on") == {
        "action": "on",
        "intervalSeconds": None,
        "meetingUrl": None,
    }
    assert bridge.parse_companion_click_command("/click on 1.5") == {
        "action": "on",
        "intervalSeconds": 1.5,
        "meetingUrl": None,
    }
    assert bridge.parse_companion_click_command("/click on abc-defg-hij 1.5") == {
        "action": "on",
        "intervalSeconds": 1.5,
        "meetingUrl": "https://meet.google.com/abc-defg-hij",
    }
    assert bridge.parse_companion_click_command("/click off") == {"action": "off", "meetingUrl": None}
    assert bridge.parse_companion_click_command("/click off https://meet.google.com/abc-defg-hij?authuser=1") == {
        "action": "off",
        "meetingUrl": "https://meet.google.com/abc-defg-hij",
    }
    assert bridge.parse_companion_click_command("/click status") == {"action": "status", "meetingUrl": None}
    assert bridge.parse_companion_click_command("/say click on") is None
    invalid = bridge.parse_companion_click_command("/click on 0")
    assert invalid is not None
    assert invalid["action"] == "invalid"


def test_companion_click_status_payload() -> None:
    holder = {
        "companion_click_enabled": True,
        "companion_click_interval_seconds": 2.0,
        "companion_click_trigger": "caption",
        "companion_click_silence_ms": 500.0,
        "companion_click_meeting_url": "https://meet.google.com/abc-defg-hij",
        "companion_click_source": "override",
        "companion_click_installed": True,
        "companion_click_last_click_at": 123.0,
        "companion_click_last_install_at": 100.0,
        "companion_click_last_error": None,
        "companion_click_last_trigger": "caption-stasis",
        "companion_click_last_silence_ms": 650.0,
        "companion_click_last_monologue_seconds": 12.5,
    }
    status: dict = {}

    payload = bridge.update_companion_click_status(status, holder)

    assert payload == status["companionClick"]
    assert payload["enabled"] is True
    assert payload["intervalSeconds"] == 2.0
    assert payload["meetingUrl"] == "https://meet.google.com/abc-defg-hij"
    assert payload["source"] == "override"
    assert payload["trigger"] == "caption"
    assert payload["silenceMs"] == 500.0
    assert payload["lastClickAt"] == 123.0
    assert payload["lastTrigger"] == "caption-stasis"
    assert payload["lastSilenceMs"] == 650.0
    assert payload["lastMonologueSeconds"] == 12.5
    assert payload["installed"] is True


def test_companion_heard_stt_status_payload() -> None:
    holder = {
        "companion_heard_stt_enabled": True,
        "companion_heard_stt_output_device": "CABLE Input",
        "companion_heard_stt_input_device_selector": "CABLE Output",
        "companion_heard_stt_input_device_id": "dev-1",
        "companion_heard_stt_input_device_name": "CABLE Output (VB-Audio Virtual Cable)",
        "companion_heard_stt_capture_listening": True,
        "companion_heard_stt_capture_live": True,
        "companion_heard_stt_sink_status": "routed",
        "companion_heard_stt_sink_device_label": "CABLE Input (VB-Audio Virtual Cable)",
        "companion_heard_stt_last_error": None,
    }
    status: dict = {}

    payload = bridge.update_companion_heard_stt_status(status, holder)

    assert payload == status["companionHeardStt"]
    assert payload["enabled"] is True
    assert payload["sourceKind"] == "companion_heard"
    assert payload["captureListening"] is True
    assert payload["engineScope"] == "server secondary capture excludes google_meet and feeds non-Meet STT engines"


def test_companion_heard_capture_device_auto_matches_virtual_cable_pair() -> None:
    devices = [
        {"id": "speaker", "name": "Primary Speakers", "direction": "output", "supports_input": False, "classes": ["physical"]},
        {"id": "cable-play", "name": "CABLE Input (VB-Audio Virtual Cable)", "direction": "virtual", "supports_output": True, "supports_input": False, "classes": ["virtual"]},
        {"id": "cable-record", "name": "CABLE Output (VB-Audio Virtual Cable)", "direction": "virtual", "supports_input": True, "supports_output": False, "classes": ["virtual"]},
    ]

    selected = bridge.select_companion_heard_capture_device(
        devices,
        output_selector="CABLE Input",
    )

    assert selected is not None
    assert selected["id"] == "cable-record"


def test_companion_click_trigger_waits_for_caption_stasis() -> None:
    holder = {
        "host_active_caption_key": "row-1",
        "host_active_caption_started_at": 100.0,
        "host_active_caption_last_growth_at": 109.7,
        "companion_click_after_seconds": 8.0,
        "companion_click_silence_ms": 500.0,
        "companion_click_min_gap_seconds": 6.0,
        "companion_click_trigger": "caption",
    }

    growing = bridge.companion_click_trigger_decision(holder, now=110.0)
    paused = bridge.companion_click_trigger_decision(holder, now=110.3)

    assert growing["due"] is False
    assert growing["trigger"] == "waiting-for-silence"
    assert paused["due"] is True
    assert paused["trigger"] == "caption-stasis"
    assert paused["silenceMs"] == pytest.approx(600.0)


def test_companion_click_audio_trigger_uses_audio_quiet_ms() -> None:
    holder = {
        "host_active_caption_key": "row-1",
        "host_active_caption_started_at": 100.0,
        "host_active_caption_last_growth_at": 110.0,
        "companion_click_after_seconds": 8.0,
        "companion_click_silence_ms": 500.0,
        "companion_click_min_gap_seconds": 6.0,
        "companion_click_trigger": "audio",
    }

    decision = bridge.companion_click_trigger_decision(holder, now=110.3, audio_probe={"ok": True, "quietMs": 650.0})

    assert decision["due"] is True
    assert decision["trigger"] == "audio-rms"
    assert decision["silenceMs"] == 650.0
