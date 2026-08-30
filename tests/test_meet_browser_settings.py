from __future__ import annotations

import sys

import pytest

from ws_collab.errors import ValidationError
from ws_collab.meet_bridge import bridge, navigator
from ws_collab.meet_browser_settings import MeetBrowserSettings

V1 = "/ws_collab/v1"


def test_meet_browser_settings_persist_across_instances(tmp_path) -> None:
    store = MeetBrowserSettings(tmp_path)
    store.set("browser_backend", "wsl")
    store.set("profile_path", str(tmp_path / "profile"))
    store.set_profile_state(
        tmp_path / "profile",
        known_meeting_urls=["https://meet.google.com/tbz-gxzr-wwv"],
    )

    reopened = MeetBrowserSettings(tmp_path)
    assert reopened.get("browser_backend") == "wsl"
    assert reopened.get("profile_path") == str(tmp_path / "profile")
    assert reopened.get_profile_state(tmp_path / "profile")["known_meeting_urls"] == [
        "https://meet.google.com/tbz-gxzr-wwv"
    ]
    assert (tmp_path / "meet_browser_settings.json").is_file()


def test_sso_consent_setting_defaults_false_and_persists(tmp_path) -> None:
    assert MeetBrowserSettings(tmp_path).require_sso_consent() is False

    first = MeetBrowserSettings(tmp_path)
    first.set(MeetBrowserSettings.REQUIRE_SSO_CONSENT_KEY, True)

    assert MeetBrowserSettings(tmp_path).require_sso_consent() is True


def test_meet_browser_settings_endpoint_round_trip(client, admin_headers, monkeypatch, tmp_path):
    from ws_collab import service as service_mod

    monkeypatch.setattr(service_mod, "DEFAULT_PROFILE", tmp_path / "default_profile")
    monkeypatch.setattr(service_mod.WsCollabService, "_meet_bridge_health", lambda self, timeout=0.5: None)
    body = client.post(
        f"{V1}/meet/browser-settings",
        headers=admin_headers,
        json={
            "browser_backend": "wsl",
            "profile_path": str(tmp_path / "custom profile"),
            "require_sso_consent": True,
        },
    ).json()
    assert body["browser_backend"] == "wsl"
    assert body["profile_path"] == str(tmp_path / "custom profile")
    assert body["require_sso_consent"] is True
    assert "--profile-mode" not in body["next_launch_command"]
    assert "--role-authuser" not in body["next_launch_command"]
    assert "role_account_map" not in body
    assert "role_assignments" not in body
    fetched = client.get(f"{V1}/meet/browser-settings", headers=admin_headers).json()
    assert fetched["browser_backend"] == "wsl"
    assert fetched["require_sso_consent"] is True
    assert "profile_mode" not in fetched
    assert "companion_profile_path" not in fetched

    disabled = client.post(
        f"{V1}/meet/browser-settings",
        headers=admin_headers,
        json={
            "browser_backend": "wsl",
            "profile_path": str(tmp_path / "custom profile"),
            "require_sso_consent": False,
        },
    ).json()
    assert disabled["require_sso_consent"] is False
    assert client.get(
        f"{V1}/meet/browser-settings", headers=admin_headers
    ).json()["require_sso_consent"] is False


@pytest.mark.parametrize("invalid", ("false", None, 0, 1))
def test_meet_browser_settings_rejects_non_boolean_consent(
    client, admin_headers, app_context, invalid
) -> None:
    before = app_context.service.get_meet_browser_settings()
    response = client.post(
        f"{V1}/meet/browser-settings",
        headers=admin_headers,
        json={
            "browser_backend": before["browser_backend"],
            "profile_path": before["profile_path"],
            "require_sso_consent": invalid,
        },
    )

    assert response.status_code == 400
    assert "must be a boolean" in response.text
    assert app_context.service.get_meet_browser_settings()["require_sso_consent"] is False


def test_bridge_reads_consent_toggle_for_each_navigation_without_restart(tmp_path) -> None:
    records = []
    requests = []
    backend = navigator.InMemoryBrowserBackend()
    navigator.configure_browser_nav_logging(records.append)
    navigator.set_consent_required_provider(
        lambda: bridge.read_sso_consent_setting(tmp_path)
    )
    navigator.set_consent_provider(
        lambda request: (
            requests.append(request),
            navigator.ConsentDecision.ALLOW_ONCE,
        )[1]
    )
    try:
        MeetBrowserSettings(tmp_path).set(
            MeetBrowserSettings.REQUIRE_SSO_CONSENT_KEY,
            False,
        )
        navigator.open_url(
            "http://127.0.0.1:9223",
            "https://accounts.google.com/AccountChooser?authuser=0",
            reason="test",
            detail="disabled",
            role="host",
            sso_intent=navigator.SsoIntent.OPERATOR_REQUEST,
            backend=backend,
        )
        assert requests == []
        assert any(row["outcome"] == "consent-disabled" for row in records)

        MeetBrowserSettings(tmp_path).set(
            MeetBrowserSettings.REQUIRE_SSO_CONSENT_KEY,
            True,
        )
        navigator.open_url(
            "http://127.0.0.1:9223",
            "https://accounts.google.com/AccountChooser?authuser=1",
            reason="test",
            detail="enabled",
            role="host",
            sso_intent=navigator.SsoIntent.OPERATOR_REQUEST,
            backend=backend,
        )
        assert len(requests) == 1
        assert any(row["outcome"] == "approved" for row in records)
    finally:
        navigator.set_consent_provider(None)
        navigator.set_consent_required_provider(None)
        navigator.configure_browser_nav_logging(None)


def test_meet_roles_may_share_an_account(service, monkeypatch, tmp_path) -> None:
    profile = tmp_path / "profile"
    monkeypatch.setattr(service, "_meet_bridge_health", lambda timeout=0.5: None)
    service.meet_browser_settings.set("profile_path", str(profile))
    service._set_meet_profile_state(
        profile,
        accounts={
            "sso_1": {"id": "sso_1", "email": "one@example.test", "authuser": 0},
            "sso_2": {"id": "sso_2", "email": "two@example.test", "authuser": 1},
        },
    )
    monkeypatch.setattr(
        service,
        "list_meet_sso_accounts",
        lambda: {
            "accounts": [
                {"id": "sso_1", "email": "one@example.test", "authuser": 0, "signed_in": True},
                {"id": "sso_2", "email": "two@example.test", "authuser": 1, "signed_in": True},
            ]
        },
    )

    saved = service.set_meet_role_assignments({"host": "sso_1", "companion": "sso_1"})

    assert saved["role_account_map"] == {"host": "sso_1", "companion": "sso_1"}


def test_meet_roles_remain_unassigned_until_operator_selects_accounts(service, monkeypatch, tmp_path) -> None:
    profile = tmp_path / "profile"
    monkeypatch.setattr(service, "_meet_bridge_health", lambda timeout=0.5: None)
    service.meet_browser_settings.set("profile_path", str(profile))
    service._set_meet_profile_state(
        profile,
        accounts={
            "sso_1": {"id": "sso_1", "email": "one@example.test", "authuser": 0},
            "sso_2": {"id": "sso_2", "email": "two@example.test", "authuser": 1},
        },
    )

    settings = service.get_meet_role_assignments()

    assert settings["role_account_map"] == {}
    assert "--role-authuser" not in settings["role_arguments"]
    assert f"--profile {profile}" in settings["role_arguments"]
    assert "--browser-backend windows" in settings["role_arguments"]


def test_meet_role_command_binds_each_authuser_to_expected_email(service, monkeypatch, tmp_path) -> None:
    profile = tmp_path / "profile"
    monkeypatch.setattr(service, "_meet_bridge_health", lambda timeout=0.5: None)
    monkeypatch.setattr(
        service,
        "list_meet_sso_accounts",
        lambda: {
            "accounts": [
                {"id": "sso_1", "email": "one@example.test", "authuser": 0, "signed_in": True},
                {"id": "sso_2", "email": "two@example.test", "authuser": 1, "signed_in": True},
            ]
        },
    )
    service.meet_browser_settings.set("profile_path", str(profile))

    settings = service.set_meet_role_assignments({"host": "sso_2", "companion": "sso_1"})

    assert "--role-authuser host=1 --role-email host=two@example.test" in settings["role_arguments"]
    assert "--role-authuser companion=0 --role-email companion=one@example.test" in settings["role_arguments"]


def test_companion_heard_stt_is_explicit_and_added_to_server_launch_arguments(service) -> None:
    assert service.config.companion_heard_stt is False
    assert "--companion-heard-stt" not in service.get_meet_browser_settings()["next_launch_command"]

    service.config.companion_heard_stt = True

    assert "--companion-heard-stt" in service.get_meet_browser_settings()["next_launch_command"]


def test_meeting_role_assignments_inherit_global_then_override_independently(
    service, monkeypatch, tmp_path
) -> None:
    profile = tmp_path / "profile"
    monkeypatch.setattr(service, "_meet_bridge_health", lambda timeout=0.5: None)
    monkeypatch.setattr(
        service,
        "list_meet_sso_accounts",
        lambda: {
            "accounts": [
                {"id": "sso_1", "email": "one@example.test", "authuser": 0, "signed_in": True},
                {"id": "sso_2", "email": "two@example.test", "authuser": 1, "signed_in": True},
                {"id": "sso_3", "email": "three@example.test", "authuser": 2, "signed_in": True},
            ]
        },
    )
    service.meet_browser_settings.set("profile_path", str(profile))
    service.set_meet_role_assignments({"host": "sso_1", "companion": "sso_2"})

    inherited = service.get_meet_role_assignments("https://meet.google.com/abc-defg-hij")
    assert inherited["role_account_map"] == {"host": "sso_1", "companion": "sso_2"}
    assert inherited["inherited_roles"] == ["host", "companion"]

    overridden = service.set_meet_role_assignments(
        {"host": "sso_2", "companion": "sso_1"},
        "https://meet.google.com/abc-defg-hij?authuser=0",
    )
    other = service.get_meet_role_assignments("https://meet.google.com/xyz-abcd-efg")

    assert overridden["meeting_url"] == "https://meet.google.com/abc-defg-hij"
    assert overridden["role_account_map"] == {"host": "sso_2", "companion": "sso_1"}
    assert overridden["inherited_roles"] == []
    assert other["role_account_map"] == {"host": "sso_1", "companion": "sso_2"}

    reset = service.clear_meet_role_assignments("https://meet.google.com/abc-defg-hij")
    assert reset["role_account_map"] == {"host": "sso_1", "companion": "sso_2"}
    assert reset["inherited_roles"] == ["host", "companion"]

    partial = service.set_meet_role_assignments(
        {"host": "sso_3", "companion": "sso_1"},
        "https://meet.google.com/abc-defg-hij",
    )
    inherited_companion = service.set_meet_role_assignments(
        {"companion": "__default__"},
        "https://meet.google.com/abc-defg-hij",
    )
    assert partial["role_overrides"] == {"host": "sso_3", "companion": "sso_1"}
    assert inherited_companion["role_overrides"] == {"host": "sso_3"}
    assert inherited_companion["inherited_roles"] == ["companion"]


def test_meeting_companion_click_inherits_default_then_override_and_clear(service, monkeypatch, tmp_path) -> None:
    profile = tmp_path / "profile"
    monkeypatch.setattr(service, "_meet_bridge_health", lambda timeout=0.5: None)
    service.meet_browser_settings.set("profile_path", str(profile))

    default = service.get_meet_companion_click("https://meet.google.com/abc-defg-hij?authuser=0")
    assert default["enabled"] is False
    assert default["intervalSeconds"] == 2.0
    assert default["mode"] == "reactive"
    assert default["trigger"] == "caption"
    assert default["afterSeconds"] == 10.0
    assert default["silenceMs"] == 500.0
    assert default["maxWaitSeconds"] == 0.0
    assert default["audioRmsThreshold"] == 0.015
    assert default["clickMs"] == 100.0
    assert default["gain"] == 0.12
    assert default["sound"] == "uh"
    assert default["f0Hz"] == 125.0
    assert default["f1Hz"] == 600.0
    assert default["f2Hz"] == 1300.0
    assert default["meetingUrl"] == "https://meet.google.com/abc-defg-hij"
    assert default["source"] == "default"

    service.set_meet_companion_click(True, 1.25)
    inherited = service.get_meet_companion_click("abc-defg-hij")
    assert inherited["enabled"] is True
    assert inherited["intervalSeconds"] == 1.25
    assert inherited["source"] == "default"

    overridden = service.set_meet_companion_click(
        False,
        3.0,
        "https://meet.google.com/abc-defg-hij?authuser=1",
        mode="fixed",
        trigger="both",
        after_seconds=9.0,
        silence_ms=450.0,
        max_wait_seconds=30.0,
        audio_rms_threshold=0.02,
        click_ms=120.0,
        gain=0.2,
        sound="click",
        f0_hz=140.0,
        f1_hz=650.0,
        f2_hz=1450.0,
    )
    other = service.get_meet_companion_click("https://meet.google.com/xyz-abcd-efg")
    assert overridden["enabled"] is False
    assert overridden["intervalSeconds"] == 3.0
    assert overridden["mode"] == "fixed"
    assert overridden["trigger"] == "both"
    assert overridden["afterSeconds"] == 9.0
    assert overridden["silenceMs"] == 450.0
    assert overridden["maxWaitSeconds"] == 30.0
    assert overridden["audioRmsThreshold"] == 0.02
    assert overridden["clickMs"] == 120.0
    assert overridden["gain"] == 0.2
    assert overridden["sound"] == "click"
    assert overridden["f0Hz"] == 140.0
    assert overridden["f1Hz"] == 650.0
    assert overridden["f2Hz"] == 1450.0
    assert overridden["source"] == "override"
    assert other["enabled"] is True
    assert other["source"] == "default"

    reset = service.clear_meet_companion_click("abc-defg-hij")
    assert reset["enabled"] is True
    assert reset["intervalSeconds"] == 1.25
    assert reset["source"] == "default"


@pytest.mark.parametrize("phrase", ["uh", "uhuh", "hmm"])
@pytest.mark.parametrize(
    ("requested_mode", "stored_mode", "trigger_mode"),
    [
        ("on_silence", "reactive", "on_silence"),
        ("interval", "fixed", "interval"),
    ],
)
def test_meeting_backchannel_round_trips_phrases_and_modes(
    service, monkeypatch, tmp_path, phrase, requested_mode, stored_mode, trigger_mode
) -> None:
    profile = tmp_path / "profile"
    monkeypatch.setattr(service, "_meet_bridge_health", lambda timeout=0.5: None)
    service.meet_browser_settings.set("profile_path", str(profile))

    saved = service.set_meet_companion_click(
        True,
        9.0,
        "abc-defg-hij",
        mode=requested_mode,
        phrase=phrase,
        sound="uh",
    )
    fetched = service.get_meet_companion_click("https://meet.google.com/abc-defg-hij")

    assert saved["phrase"] == fetched["phrase"] == phrase
    assert saved["sound"] == fetched["sound"] == "uh"
    assert saved["mode"] == fetched["mode"] == stored_mode
    assert saved["triggerMode"] == fetched["triggerMode"] == trigger_mode


def test_meeting_backchannel_accepts_legacy_sound_mode_and_snake_case_payload(
    client, admin_headers, app_context, monkeypatch, tmp_path
) -> None:
    service = app_context.service
    monkeypatch.setattr(service, "_meet_bridge_health", lambda timeout=0.5: None)
    service.meet_browser_settings.set("profile_path", str(tmp_path / "profile"))

    response = client.post(
        f"{V1}/meet/companion-click",
        headers=admin_headers,
        json={
            "meeting_url": "abc-defg-hij",
            "enabled": True,
            "interval_seconds": 4,
            "mode": "fixed",
            "trigger": "caption",
            "sound": "click",
        },
    )

    assert response.status_code == 200
    assert response.json()["mode"] == "fixed"
    assert response.json()["triggerMode"] == "interval"
    assert response.json()["sound"] == "click"
    assert response.json()["phrase"] == "click"


def test_meeting_backchannel_rejects_invalid_phrase_and_ranges(service, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(service, "_meet_bridge_health", lambda timeout=0.5: None)
    service.meet_browser_settings.set("profile_path", str(tmp_path / "profile"))

    with pytest.raises(ValidationError, match="phrase must be"):
        service.set_meet_companion_click(True, 2, "abc-defg-hij", phrase="beep")
    with pytest.raises(ValidationError, match="intervalSeconds must be between"):
        service.set_meet_companion_click(True, 3601, "abc-defg-hij")


def test_meet_companion_click_rest_routes(client, admin_headers, app_context, monkeypatch, tmp_path) -> None:
    profile = tmp_path / "profile"
    service = app_context.service
    monkeypatch.setattr(service, "_meet_bridge_health", lambda timeout=0.5: None)
    service.meet_browser_settings.set("profile_path", str(profile))

    saved = client.post(
        f"{V1}/meet/companion-click",
        headers=admin_headers,
        json={
            "meeting_url": "https://meet.google.com/abc-defg-hij?authuser=0",
            "enabled": True,
            "interval_seconds": 1.75,
            "mode": "reactive",
            "trigger": "audio",
            "after_seconds": 8.0,
            "silence_ms": 550.0,
            "min_gap_seconds": 7.0,
            "max_wait_seconds": 0.0,
            "audio_rms_threshold": 0.025,
            "click_ms": 110.0,
            "gain": 0.18,
            "sound": "uh",
            "phrase": "hmm",
            "f0_hz": 130.0,
            "f1_hz": 620.0,
            "f2_hz": 1350.0,
        },
    )
    fetched = client.get(
        f"{V1}/meet/companion-click?meeting_url=https://meet.google.com/abc-defg-hij?authuser=1",
        headers=admin_headers,
    )
    cleared = client.delete(
        f"{V1}/meet/companion-click?meeting_url=abc-defg-hij",
        headers=admin_headers,
    )

    assert saved.status_code == 200
    assert saved.json()["source"] == "override"
    assert saved.json()["enabled"] is True
    assert saved.json()["intervalSeconds"] == 1.75
    assert saved.json()["trigger"] == "audio"
    assert saved.json()["afterSeconds"] == 8.0
    assert saved.json()["silenceMs"] == 550.0
    assert saved.json()["minGapSeconds"] == 7.0
    assert saved.json()["maxWaitSeconds"] == 0.0
    assert saved.json()["audioRmsThreshold"] == 0.025
    assert saved.json()["clickMs"] == 110.0
    assert saved.json()["gain"] == 0.18
    assert saved.json()["sound"] == "uh"
    assert saved.json()["phrase"] == "hmm"
    assert saved.json()["f0Hz"] == 130.0
    assert saved.json()["f1Hz"] == 620.0
    assert saved.json()["f2Hz"] == 1350.0
    assert fetched.json()["source"] == "override"
    assert fetched.json()["enabled"] is True
    assert cleared.json()["source"] == "default"
    assert cleared.json()["enabled"] is False


def test_known_meetings_are_recovered_from_history_and_persisted(service, monkeypatch, tmp_path) -> None:
    profile = tmp_path / "profile"
    monkeypatch.setattr(service, "_meet_bridge_health", lambda timeout=0.5: None)
    service.meet_browser_settings.set("profile_path", str(profile))
    service.add_conversation(
        "Past room https://meet.google.com/tbz-gxzr-wwv?authuser=0; ignore https://meet.google.com/xxx-yyyy-zzz",
        source_id="google-meet-captions",
        source_kind="operator",
    )

    settings = service.get_meet_role_assignments()

    assert "https://meet.google.com/tbz-gxzr-wwv" in settings["known_meeting_urls"]
    assert "https://meet.google.com/xxx-yyyy-zzz" not in settings["known_meeting_urls"]
    state = service.meet_browser_settings.get_profile_state(profile)
    assert "https://meet.google.com/tbz-gxzr-wwv" in state["known_meeting_urls"]


def test_role_combo_can_save_any_known_sso_id_for_runtime_resolution(
    service, monkeypatch, tmp_path
) -> None:
    profile = tmp_path / "profile"
    monkeypatch.setattr(service, "_meet_bridge_health", lambda timeout=0.5: None)
    monkeypatch.setattr(
        service,
        "list_meet_sso_accounts",
        lambda: {
            "accounts": [
                {"id": "sso_1", "email": "known@example.test", "authuser": None, "signed_in": False},
                {"id": "sso_2", "email": "live@example.test", "authuser": 1, "signed_in": True},
            ]
        },
    )
    service.meet_browser_settings.set("profile_path", str(profile))

    saved = service.set_meet_role_assignments({"host": "sso_1", "companion": "sso_2"})

    assert saved["role_account_map"] == {"host": "sso_1", "companion": "sso_2"}
    assert "--role-authuser host=" not in saved["role_arguments"]
    assert "--role-authuser companion=1" in saved["role_arguments"]


def test_start_meet_bridge_uses_persisted_runtime_role_bindings(
    service, monkeypatch, tmp_path
) -> None:
    captured = {}

    class Process:
        pid = 4242

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return Process()

    monkeypatch.setattr(service, "_meet_bridge_health", lambda timeout=0.75: None)
    monkeypatch.setattr(service, "_meet_bridge_port_open", lambda: False)
    monkeypatch.setattr(
        service,
        "get_meet_role_assignments",
        lambda meeting_url="": {
            "role_assignments": [
                {"role": "host", "account_id": "sso_1", "authuser": 0, "email": "one@example.test"},
                {"role": "companion", "account_id": "sso_1", "authuser": 0, "email": "one@example.test"},
            ]
        },
    )
    monkeypatch.setattr("ws_collab.service.subprocess.Popen", fake_popen)

    started = service.start_meet_bridge("https://meet.google.com/abc-defg-hij")

    assert started["started"] is True
    assert started["pid"] == 4242
    assert captured["argv"][0] == sys.executable
    assert "-u" in captured["argv"]
    assert captured["argv"].count("host=0") == 1
    assert captured["argv"].count("companion=0") == 1
    assert "https://meet.google.com/abc-defg-hij" in captured["argv"]
    assert "WS_COLLAB_TOKEN" not in " ".join(captured["argv"])
    assert captured["kwargs"]["env"]["PYTHONUNBUFFERED"] == "1"
    assert captured["kwargs"]["env"]["WS_COLLAB_STATE_DIR"] == str(
        service.config.state_dir.resolve()
    )
    assert captured["kwargs"]["env"]["WS_COLLAB_TOKEN"] in service.config.tokens


def test_start_meet_bridge_refuses_duplicate_tracked_process(service, monkeypatch) -> None:
    class RunningProcess:
        pid = 7777

        def poll(self):
            return None

    service._meet_bridge_process = RunningProcess()
    monkeypatch.setattr(service, "_meet_bridge_health", lambda timeout=0.75: None)
    monkeypatch.setattr(service, "_meet_bridge_port_open", lambda: False)
    monkeypatch.setattr(
        "ws_collab.service.subprocess.Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not spawn duplicate bridge")),
    )

    started = service.start_meet_bridge("https://meet.google.com/abc-defg-hij")

    assert started["started"] is False
    assert started["already_running"] is True


def test_start_meet_bridge_refuses_duplicate_pid_file(service, monkeypatch) -> None:
    service._meet_bridge_pid_path().write_text("8888", encoding="utf-8")
    monkeypatch.setattr(service, "_meet_bridge_health", lambda timeout=0.75: None)
    monkeypatch.setattr(service, "_meet_bridge_port_open", lambda: False)
    monkeypatch.setattr(service, "_is_pid_alive", lambda pid: pid == 8888)
    monkeypatch.setattr(
        "ws_collab.service.subprocess.Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not spawn duplicate bridge")),
    )

    started = service.start_meet_bridge("https://meet.google.com/abc-defg-hij")

    assert started["started"] is False
    assert started["already_running"] is True
    assert started["pid"] == 8888


def test_server_managed_join_starts_offline_bridge(service, monkeypatch) -> None:
    monkeypatch.setattr(service, "_meet_bridge_command", lambda command, timeout=2.0: None)
    monkeypatch.setattr(
        service,
        "start_meet_bridge",
        lambda meeting_url="", new=False: {
            "ok": True,
            "started": True,
            "already_running": False,
            "pid": 4242,
            "meeting_url": meeting_url,
        },
    )

    result = service.meet_bridge_command("/join https://meet.google.com/abc-defg-hij")

    assert result["started"] is True
    assert result["verdict"] == "bridge starting (pid 4242)"


def test_server_managed_bridge_routes_use_authenticated_api(
    client, admin_headers, monkeypatch
) -> None:
    from ws_collab.service import WsCollabService

    monkeypatch.setattr(
        WsCollabService,
        "meet_bridge_health",
        lambda self: {"ok": True, "meetingUrl": "https://meet.google.com/abc-defg-hij"},
    )
    monkeypatch.setattr(
        WsCollabService,
        "meet_bridge_command",
        lambda self, command: {"ok": True, "verdict": f"accepted {command}"},
    )

    health = client.get(f"{V1}/meet/bridge/status", headers=admin_headers)
    command = client.post(
        f"{V1}/meet/bridge/command",
        headers=admin_headers,
        json={"command": "/new"},
    )

    assert health.status_code == 200
    assert health.json()["meetingUrl"].endswith("abc-defg-hij")
    assert command.status_code == 200
    assert command.json()["verdict"] == "accepted /new"


def test_live_account_reconciliation_keeps_stable_sso_ids(service, tmp_path) -> None:
    profile = tmp_path / "profile"
    service._set_meet_profile_state(
        profile,
        accounts={
            "sso_1": {"id": "sso_1", "email": "one@example.test", "authuser": 0},
            "sso_2": {"id": "sso_2", "email": "two@example.test", "authuser": 1},
        },
        role_account_map={"host": "sso_1", "companion": "sso_2"},
    )

    accounts = service._persist_live_sso_accounts({
        "hostProfile": {"path": str(profile)},
        "ssoAccounts": [
            {"email": "two@example.test", "authuser": 0},
            {"email": "one@example.test", "authuser": 1},
        ],
    })

    assert accounts["sso_1"]["email"] == "one@example.test"
    assert accounts["sso_1"]["authuser"] == 1
    assert accounts["sso_2"]["email"] == "two@example.test"
    assert accounts["sso_2"]["authuser"] == 0
    assert service._meet_role_account_map(accounts, profile) == {"host": "sso_1", "companion": "sso_2"}


def test_new_email_in_reused_authuser_slot_gets_new_sso_id(service, tmp_path) -> None:
    profile = tmp_path / "profile"
    service._set_meet_profile_state(
        profile,
        accounts={"sso_1": {"id": "sso_1", "email": "old@example.test", "authuser": 0}},
    )

    accounts = service._persist_live_sso_accounts({
        "hostProfile": {"path": str(profile)},
        "ssoAccounts": [{"email": "new@example.test", "authuser": 0}],
    })

    assert accounts["sso_1"]["email"] == "old@example.test"
    assert accounts["sso_1"]["authuser"] is None
    assert accounts["sso_2"]["email"] == "new@example.test"
    assert accounts["sso_2"]["authuser"] == 0
