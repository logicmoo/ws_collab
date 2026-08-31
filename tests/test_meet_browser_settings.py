from __future__ import annotations

import json
import multiprocessing
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from ws_collab.errors import ConflictError, ValidationError
from ws_collab.meet_bridge import bridge, navigator
from ws_collab.meet_browser_settings import (
    MeetBrowserSettings,
    companion_click_layers,
    companion_click_runtime_layers,
    prune_meeting_channels,
)

V1 = "/ws_collab/v1"


def _write_meet_setting(directory: str, key: str, value: str) -> None:
    MeetBrowserSettings(Path(directory)).set(key, value)


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


def test_meet_browser_settings_concurrent_writers_preserve_independent_keys(
    tmp_path,
) -> None:
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [
            pool.submit(
                _write_meet_setting, str(tmp_path), f"thread-{index}", str(index)
            )
            for index in range(12)
        ]
        for future in futures:
            future.result()

    context = multiprocessing.get_context("spawn")
    processes = [
        context.Process(
            target=_write_meet_setting,
            args=(str(tmp_path), f"process-{index}", str(index)),
        )
        for index in range(8)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(20)
        assert process.exitcode == 0

    raw = (tmp_path / "meet_browser_settings.json").read_text(encoding="utf-8")
    persisted = json.loads(raw)
    assert all(persisted[f"thread-{index}"] == str(index) for index in range(12))
    assert all(persisted[f"process-{index}"] == str(index) for index in range(8))
    assert list(tmp_path.glob(".meet_browser_settings.json.*.tmp")) == []


def test_runtime_companion_layers_expire_without_settings_leakage() -> None:
    state = {
        "companion_click": {"phrase": "uh"},
        "meeting_companion_click": {
            "https://meet.google.com/abc-defg-hij": {"phrase": "hmm"}
        },
        "test_companion_click": {"count20": {"gain": 0.4}},
        "active_test_companion_click": {
            "testProfile": "count20",
            "channelKey": "https://meet.google.com/abc-defg-hij",
            "expiresAt": 105,
        },
    }

    active = companion_click_layers(
        state, "https://meet.google.com/abc-defg-hij", now=100
    )
    expired = companion_click_layers(
        state, "https://meet.google.com/abc-defg-hij", now=106
    )

    assert [source for source, _patch in active] == [
        "default", "override", "test:count20"
    ]
    assert [source for source, _patch in expired] == ["default", "override"]
    assert state["meeting_companion_click"] == {
        "https://meet.google.com/abc-defg-hij": {"phrase": "hmm"}
    }


def test_runtime_companion_refresh_does_not_reveal_cli_values_after_patch_deletion() -> None:
    meeting = "https://meet.google.com/abc-defg-hij"
    cli_seed = {"phrase": "hmm", "gain": 0.9, "afterSeconds": 99.0}
    launched = {
        "companion_click": {"phrase": "uhuh"},
        "meeting_companion_click": {meeting: {"gain": 0.4}},
    }

    layers, persisted_seen = companion_click_runtime_layers(
        launched, meeting, cli_seed
    )
    effective = {}
    for _source, patch in layers:
        effective.update(patch)
    assert effective["phrase"] == "uhuh"
    assert effective["gain"] == 0.4

    refreshed = {
        "companion_click": {"phrase": "hmm"},
        "meeting_companion_click": {},
    }
    layers, persisted_seen = companion_click_runtime_layers(
        refreshed,
        meeting,
        cli_seed,
        persisted_source_seen=persisted_seen,
    )
    effective = {}
    for _source, patch in layers:
        effective.update(patch)

    assert effective["phrase"] == "hmm"
    assert effective["gain"] == 0.12
    assert effective["afterSeconds"] == 10.0
    assert persisted_seen is True


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
    assert default["action"] == "say:uh"
    assert "phrase" not in default and "sound" not in default
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
    assert overridden["action"] == "say:click"
    assert "phrase" not in overridden and "sound" not in overridden
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

    assert saved["action"] == fetched["action"] == f"say:{phrase}"
    assert "phrase" not in saved and "sound" not in saved
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
    assert response.json()["action"] == "say:click"
    assert "phrase" not in response.json() and "sound" not in response.json()


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
    assert saved.json()["action"] == "say:hmm"
    assert "phrase" not in saved.json() and "sound" not in saved.json()
    assert saved.json()["f0Hz"] == 130.0
    assert saved.json()["f1Hz"] == 620.0
    assert saved.json()["f2Hz"] == 1350.0
    assert fetched.json()["source"] == "override"
    assert fetched.json()["enabled"] is True
    assert cleared.json()["source"] == "default"
    assert cleared.json()["enabled"] is False


def test_scoped_companion_defaults_global_reset_and_channel_inheritance(
    service, monkeypatch, tmp_path
) -> None:
    profile = tmp_path / "profile"
    monkeypatch.setattr(service, "_meet_bridge_health", lambda timeout=0.5: None)
    service.meet_browser_settings.set("profile_path", str(profile))

    built_in = service.get_companion_click_config("global")
    assert built_in["hasOverride"] is False
    assert built_in["effective"]["action"] == "say:uh"
    assert built_in["sources"]["action"] == "built-in"

    saved = service.set_companion_click_config(
        "global", {"phrase": "hmm", "interval_seconds": 7}
    )
    assert saved["effective"]["action"] == "say:hmm"
    assert saved["effective"]["intervalSeconds"] == 7
    assert saved["sources"]["action"] == "global"

    channel = service.set_companion_click_config(
        "channel",
        {"enabled": True},
        channel_key="https://meet.google.com/ABC-DEFG-HIJ?authuser=1",
    )
    assert channel["scopeKey"] == "google-meet:abc-defg-hij"
    assert channel["knownChannels"] == [
        {
            "key": "google-meet:abc-defg-hij",
            "provider": "Google Meet",
            "code": "abc-defg-hij",
            "label": "abc-defg-hij",
            "url": "https://meet.google.com/abc-defg-hij",
        }
    ]
    assert service.get_companion_click_config(
        "channel", channel_key=channel["scopeKey"]
    )["effective"]["enabled"] is True
    assert channel["override"] == {"enabled": True}
    assert channel["effective"]["action"] == "say:hmm"
    assert channel["sources"]["enabled"] == "channel"
    assert channel["sources"]["action"] == "global"

    full_form = {
        key: value
        for key, value in channel["effective"].items()
        if key != "triggerMode"
    }
    compacted = service.set_companion_click_config(
        "channel",
        full_form,
        channel_key="abc-defg-hij",
        replace=True,
    )
    assert compacted["override"] == {"enabled": True}

    service.set_companion_click_config("global", {"phrase": "uhuh", "gain": 0.2})
    changed = service.get_companion_click_config(
        "channel", channel_key="abc-defg-hij"
    )
    assert changed["effective"]["enabled"] is True
    assert changed["effective"]["action"] == "say:uhuh"
    assert changed["effective"]["gain"] == 0.2

    deleted = service.clear_companion_click_config(
        "channel", channel_key="abc-defg-hij"
    )
    repeated = service.clear_companion_click_config(
        "channel", channel_key="abc-defg-hij"
    )
    assert deleted["hasOverride"] is repeated["hasOverride"] is False
    assert deleted["effective"]["action"] == "say:uhuh"

    reset = service.clear_companion_click_config("global")
    assert reset["hasOverride"] is True
    assert reset["effective"]["action"] == "say:uh"
    assert reset["sources"]["action"] == "global"


def test_scoped_channels_and_test_profiles_are_isolated_with_test_precedence(
    service, monkeypatch, tmp_path
) -> None:
    profile = tmp_path / "profile"
    monkeypatch.setattr(service, "_meet_bridge_health", lambda timeout=0.5: None)
    service.meet_browser_settings.set("profile_path", str(profile))
    service.set_companion_click_config("global", {"phrase": "uh", "gain": 0.1})
    service.set_companion_click_config(
        "channel", {"phrase": "hmm"}, channel_key="abc-defg-hij"
    )
    service.set_companion_click_config(
        "channel", {"gain": 0.3}, channel_key="xyz-abcd-efg"
    )
    service.set_companion_click_config(
        "test", {"gain": 0.4}, test_profile="count20"
    )
    service.set_companion_click_config(
        "test", {"phrase": "uhuh"}, test_profile="abcs"
    )

    count = service.get_companion_click_config(
        "test", channel_key="abc-defg-hij", test_profile="count20"
    )
    alphabet = service.get_companion_click_config(
        "test", channel_key="abc-defg-hij", test_profile="abcs"
    )
    other = service.get_companion_click_config(
        "channel", channel_key="xyz-abcd-efg"
    )
    assert count["effective"]["action"] == "say:hmm"
    assert count["effective"]["gain"] == 0.4
    assert count["sources"]["action"] == "channel"
    assert count["sources"]["gain"] == "test"
    assert alphabet["effective"]["action"] == "say:uhuh"
    assert alphabet["effective"]["gain"] == 0.1
    assert other["effective"]["action"] == "say:uh"
    assert other["effective"]["gain"] == 0.3

    removed = service.clear_companion_click_config("test", test_profile="count20")
    assert removed["hasOverride"] is False
    assert removed["effective"]["gain"] == 0.1
    assert service.get_companion_click_config(
        "test", test_profile="abcs"
    )["hasOverride"] is True


def test_promoting_effective_test_values_preserves_partial_scoped_patches(
    service, monkeypatch, tmp_path
) -> None:
    profile = tmp_path / "profile"
    monkeypatch.setattr(service, "_meet_bridge_health", lambda timeout=0.5: None)
    service.meet_browser_settings.set("profile_path", str(profile))
    service.set_companion_click_config(
        "global", {"phrase": "uh", "gain": 0.1}, replace=True
    )
    service.set_companion_click_config(
        "channel", {"phrase": "hmm"}, channel_key="abc-defg-hij", replace=True
    )
    service.set_companion_click_config(
        "test", {"gain": 0.4}, test_profile="count20", replace=True
    )

    displayed_test = service.get_companion_click_config(
        "test", test_profile="count20"
    )["effective"]
    displayed_test.pop("triggerMode")
    service.set_companion_click_config("global", displayed_test, replace=True)

    channel = service.get_companion_click_config(
        "channel", channel_key="abc-defg-hij"
    )
    test = service.get_companion_click_config("test", test_profile="count20")
    assert channel["override"]["action"] == "say:hmm"
    assert channel["effective"]["gain"] == 0.4
    assert channel["sources"]["gain"] == "global"
    assert test["override"]["gain"] == 0.4
    assert test["hasOverride"] is True


def test_scoped_companion_legacy_storage_and_strict_keys(
    service, monkeypatch, tmp_path
) -> None:
    profile = tmp_path / "profile"
    monkeypatch.setattr(service, "_meet_bridge_health", lambda timeout=0.5: None)
    service.meet_browser_settings.set("profile_path", str(profile))
    service.meet_browser_settings.set_profile_state(
        profile,
        companion_click={"enabled": True, "interval_seconds": 3},
        meeting_companion_click={
            "https://meet.google.com/abc-defg-hij": {
                "enabled": False,
                "intervalSeconds": 8,
                "phrase": "hmm",
            }
        },
    )

    legacy = service.get_meet_companion_click("ABC-DEFG-HIJ")
    assert legacy["enabled"] is False
    assert legacy["intervalSeconds"] == 8
    assert legacy["action"] == "say:hmm"
    assert "phrase" not in legacy and "sound" not in legacy
    assert legacy["sources"]["action"] == "channel"
    stored = service.meet_browser_settings.get_profile_state(profile)
    assert "action" not in stored["meeting_companion_click"]["https://meet.google.com/abc-defg-hij"]
    assert legacy["source"] == "override"
    with pytest.raises(ValidationError, match="meeting_url"):
        service.get_companion_click_config(
            "channel", channel_key="https://meet.google.com/../../settings"
        )
    with pytest.raises(ValidationError, match="test_profile"):
        service.get_companion_click_config("test", test_profile="../count20")
    with pytest.raises(ValidationError, match="unknown companion-click setting"):
        service.set_companion_click_config("global", {"unexpected": 1})


@pytest.mark.parametrize(
    ("action", "mode", "valid"),
    [
        ("continue", "reactive", True),
        ("continue", "on_silence", True),
        ("continue", "fixed", False),
        ("continue", "interval", False),
        ("nothing", "reactive", True),
        ("nothing", "fixed", True),
        ("say:uh", "reactive", True),
        ("say:uh", "fixed", True),
        ("say:uhuh", "fixed", True),
        ("say:hmm", "interval", True),
        ("speak:anything", "reactive", False),
        ("say:click", "reactive", False),
    ],
)
def test_silence_action_trigger_validation_matrix(
    service, monkeypatch, tmp_path, action, mode, valid
) -> None:
    monkeypatch.setattr(service, "_meet_bridge_health", lambda timeout=0.5: None)
    service.meet_browser_settings.set("profile_path", str(tmp_path / "profile"))

    if valid:
        saved = service.set_companion_click_config(
            "global", {"enabled": True, "action": action, "mode": mode}
        )
        assert saved["effective"]["action"] == action
    else:
        with pytest.raises(ValidationError):
            service.set_companion_click_config(
                "global", {"enabled": True, "action": action, "mode": mode}
            )


@pytest.mark.parametrize(
    "action", ["continue", "nothing", "say:uh", "say:uhuh", "say:hmm"]
)
def test_canonical_action_api_round_trip_has_no_phrase_family(
    client, admin_headers, app_context, monkeypatch, tmp_path, action
) -> None:
    service = app_context.service
    monkeypatch.setattr(service, "_meet_bridge_health", lambda timeout=0.5: None)
    service.meet_browser_settings.set("profile_path", str(tmp_path / "profile"))

    saved = client.post(
        f"{V1}/meet/companion-click",
        headers=admin_headers,
        json={
            "scope": "global",
            "override": {"action": action, "mode": "on_silence"},
        },
    )
    fetched = client.get(
        f"{V1}/meet/companion-click?scope=global", headers=admin_headers
    )

    assert saved.status_code == fetched.status_code == 200
    for payload in (saved.json(), fetched.json()):
        assert payload["action"] == action
        assert payload["effective"]["action"] == action
        assert "phrase" not in payload
        assert "sound" not in payload
        assert "phrase" not in payload["effective"]
        assert "sound" not in payload["effective"]


@pytest.mark.parametrize(
    ("legacy", "action"),
    [
        ({"phrase": "uhuh"}, "say:uhuh"),
        ({"sound": "hmm"}, "say:hmm"),
        ({"sound": "click"}, "say:click"),
    ],
)
def test_legacy_phrase_and_sound_inputs_return_canonical_action_only(
    client, admin_headers, app_context, monkeypatch, tmp_path, legacy, action
) -> None:
    service = app_context.service
    monkeypatch.setattr(service, "_meet_bridge_health", lambda timeout=0.5: None)
    service.meet_browser_settings.set("profile_path", str(tmp_path / "profile"))
    response = client.post(
        f"{V1}/meet/companion-click",
        headers=admin_headers,
        json={"scope": "global", "override": legacy},
    )

    assert response.status_code == 200
    assert response.json()["effective"]["action"] == action
    assert "phrase" not in response.json()["effective"]
    assert "sound" not in response.json()["effective"]


def test_continue_and_say_actions_have_identical_config_and_source_shape(
    service, monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(service, "_meet_bridge_health", lambda timeout=0.5: None)
    service.meet_browser_settings.set("profile_path", str(tmp_path / "profile"))
    shapes = []
    for action in ("continue", "say:hmm"):
        config = service.set_companion_click_config(
            "global", {"action": action, "mode": "reactive"}
        )
        shapes.append((set(config["effective"]), set(config["sources"])))

    assert shapes[0] == shapes[1]
    assert "action" in shapes[0][0]
    assert "phrase" not in shapes[0][0] and "sound" not in shapes[0][0]


def test_scoped_action_inherits_changes_and_reset_deletes_only_override(
    service, monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(service, "_meet_bridge_health", lambda timeout=0.5: None)
    service.meet_browser_settings.set("profile_path", str(tmp_path / "profile"))
    service.set_companion_click_config("global", {"action": "continue"})
    channel = service.set_companion_click_config(
        "channel", {"action": "say:hmm"}, channel_key="abc-defg-hij"
    )
    service.set_companion_click_config(
        "test", {"action": "say:uhuh"}, test_profile="count20"
    )

    assert channel["sources"]["action"] == "channel"
    assert service.get_companion_click_config(
        "test", channel_key="abc-defg-hij", test_profile="count20"
    )["effective"]["action"] == "say:uhuh"
    inherited = service.clear_companion_click_config(
        "channel", channel_key="abc-defg-hij"
    )
    assert inherited["effective"]["action"] == "continue"
    assert inherited["sources"]["action"] == "global"
    service.set_companion_click_config("global", {"action": "say:uh"})
    assert service.get_companion_click_config(
        "channel", channel_key="abc-defg-hij"
    )["effective"]["action"] == "say:uh"
    promoted_values = service.get_companion_click_config(
        "test", test_profile="count20"
    )["effective"]
    promoted_values.pop("triggerMode")
    promoted = service.set_companion_click_config(
        "global",
        promoted_values,
        replace=True,
    )
    assert promoted["effective"]["action"] == "say:uhuh"
    assert promoted["override"]["action"] == "say:uhuh"


def test_scoped_companion_rest_payloads_and_test_lease(
    client, admin_headers, app_context, monkeypatch, tmp_path
) -> None:
    service = app_context.service
    profile = tmp_path / "profile"
    monkeypatch.setattr(service, "_meet_bridge_health", lambda timeout=0.5: None)
    service.meet_browser_settings.set("profile_path", str(profile))

    saved = client.post(
        f"{V1}/meet/companion-click",
        headers=admin_headers,
        json={
            "scope": "channel",
            "channel_key": "abc-defg-hij",
            "override": {"phrase": "hmm"},
        },
    )
    fetched = client.get(
        f"{V1}/meet/companion-click?scope=channel&channel_key=abc-defg-hij",
        headers=admin_headers,
    )
    activated = client.post(
        f"{V1}/meet/companion-click/test-session",
        headers=admin_headers,
        json={"test_profile": "count20", "channel_key": "abc-defg-hij"},
    )
    active_state = service.meet_browser_settings.get_profile_state(profile)[
        "active_test_companion_click"
    ]
    stopped = client.delete(
        f"{V1}/meet/companion-click/test-session"
        "?test_profile=count20&channel_key=abc-defg-hij",
        headers=admin_headers,
    )

    assert saved.status_code == fetched.status_code == activated.status_code == 200
    assert fetched.json()["override"] == {"action": "say:hmm"}
    assert fetched.json()["effective"]["action"] == "say:hmm"
    assert active_state["testProfile"] == "count20"
    assert active_state["channelKey"] == "https://meet.google.com/abc-defg-hij"
    assert stopped.json() == {"active": False}
    assert service.meet_browser_settings.get_profile_state(profile)[
        "active_test_companion_click"
    ] == {}


def test_test_scope_room_context_retargets_effective_config_and_lease(
    client, admin_headers, app_context, monkeypatch, tmp_path
) -> None:
    service = app_context.service
    profile = tmp_path / "profile"
    monkeypatch.setattr(service, "_meet_bridge_health", lambda timeout=0.5: None)
    service.meet_browser_settings.set("profile_path", str(profile))
    service.set_companion_click_config("global", {"phrase": "uh", "gain": 0.1})
    service.set_companion_click_config(
        "channel", {"phrase": "hmm"}, channel_key="abc-defg-hij"
    )
    service.set_companion_click_config(
        "channel", {"phrase": "uhuh"}, channel_key="xyz-abcd-efg"
    )
    service.set_companion_click_config(
        "test", {"gain": 0.4}, test_profile="count20"
    )

    room_a = client.get(
        f"{V1}/meet/companion-click"
        "?scope=test&test_profile=count20&channel_key=abc-defg-hij",
        headers=admin_headers,
    ).json()
    room_b = client.get(
        f"{V1}/meet/companion-click"
        "?scope=test&test_profile=count20&channel_key=xyz-abcd-efg",
        headers=admin_headers,
    ).json()
    client.post(
        f"{V1}/meet/companion-click/test-session",
        headers=admin_headers,
        json={"test_profile": "count20", "channel_key": "xyz-abcd-efg"},
    )
    stale_stop = client.delete(
        f"{V1}/meet/companion-click/test-session"
        "?test_profile=count20&channel_key=abc-defg-hij",
        headers=admin_headers,
    ).json()
    stopped = client.delete(
        f"{V1}/meet/companion-click/test-session"
        "?test_profile=count20&channel_key=xyz-abcd-efg",
        headers=admin_headers,
    ).json()

    assert room_a["meetingUrl"].endswith("/abc-defg-hij")
    assert room_a["effective"]["action"] == "say:hmm"
    assert room_a["effective"]["gain"] == 0.4
    assert room_b["meetingUrl"].endswith("/xyz-abcd-efg")
    assert room_b["effective"]["action"] == "say:uhuh"
    assert room_b["effective"]["gain"] == 0.4
    assert stale_stop["active"] is True
    assert stale_stop["deactivated"] is False
    assert stopped == {"active": False}


def test_continue_floor_api_releases_queued_agent_once_and_records_durable_status(
    client, admin_headers, app_context, monkeypatch, tmp_path
) -> None:
    service = app_context.service
    profile = tmp_path / "profile"
    meeting = "https://meet.google.com/abc-defg-hij"
    service.meet_browser_settings.set("profile_path", str(profile))
    service.meet_browser_settings.set_profile_state(
        profile,
        accounts={
            "sso_1": {
                "id": "sso_1", "email": "agent@example.test",
                "authuser": 0, "signed_in": True,
            }
        },
        role_account_map={"companion": "sso_1"},
    )
    service.set_companion_click_config(
        "global", {"enabled": True, "action": "continue", "mode": "reactive"}
    )
    monkeypatch.setattr(
        service,
        "_meet_bridge_health",
        lambda timeout=0.5: {
            "meetingUrl": meeting,
            "companionAudio": {"companionReady": True, "queued": 0, "speaking": False},
        },
    )
    monkeypatch.setattr(service.tts, "_signal", lambda: None)

    queued = client.post(
        f"{V1}/meet/floor/queue",
        headers=admin_headers,
        json={
            "meeting_url": meeting,
            "agent_id": "agent-1",
            "role": "companion",
            "text": "next turn",
        },
    )
    opened = client.post(
        f"{V1}/meet/floor/continue",
        headers=admin_headers,
        json={"meeting_url": meeting, "event_key": "silence-edge-1", "role": "companion"},
    )
    duplicate = client.post(
        f"{V1}/meet/floor/continue",
        headers=admin_headers,
        json={"meeting_url": meeting, "event_key": "silence-edge-1", "role": "companion"},
    )

    assert queued.status_code == 200
    assert queued.json()["waiting_for_floor"] is True
    assert opened.status_code == 200
    assert opened.json()["granted"] is True
    assert opened.json()["floorGrantCount"] == 1
    assert opened.json()["floorGrantedTo"] == "agent-1"
    assert duplicate.json()["duplicate"] is True
    assert duplicate.json()["floorGrantCount"] == 1
    events = service.tail("conversation", count=20)["events"]
    assert any(
        event["type"] == "CONVERSATION_FLOOR_CONTINUE"
        and event["data"]["meetingUrl"] == meeting
        for event in events
    )


def test_continue_floor_without_queued_agent_opens_and_test_stop_invalidates(
    client, admin_headers, app_context, monkeypatch, tmp_path
) -> None:
    service = app_context.service
    profile = tmp_path / "profile"
    meeting = "https://meet.google.com/abc-defg-hij"
    monkeypatch.setattr(service, "_meet_bridge_health", lambda timeout=0.5: None)
    service.meet_browser_settings.set("profile_path", str(profile))
    service.set_companion_click_config(
        "global", {"enabled": True, "action": "continue", "mode": "reactive"}
    )
    service.activate_companion_click_test("count20", meeting)

    opened = service.continue_meeting_floor(
        meeting, "silence-edge-1", test_profile="count20", role="companion"
    )
    stopped = service.deactivate_companion_click_test()

    assert opened["granted"] is False
    assert opened["floorOpen"] is True
    assert stopped == {"active": False}
    assert service.meeting_floor_status(meeting)["floorOpen"] is False
    with pytest.raises(ConflictError, match="stale or stopped"):
        service.continue_meeting_floor(
            meeting, "silence-edge-2", test_profile="count20", role="companion"
        )


def test_say_nothing_records_durable_noop_without_opening_floor(
    service, monkeypatch, tmp_path
) -> None:
    meeting = "https://meet.google.com/abc-defg-hij"
    monkeypatch.setattr(service, "_meet_bridge_health", lambda timeout=0.5: None)
    service.meet_browser_settings.set("profile_path", str(tmp_path / "profile"))
    service.set_companion_click_config(
        "global", {"enabled": True, "action": "nothing", "mode": "fixed"}
    )
    open_floor_calls = []
    monkeypatch.setattr(
        service.tts,
        "open_floor",
        lambda *args, **kwargs: open_floor_calls.append((args, kwargs)),
    )

    first = service.evaluate_meeting_silence_action(
        meeting, "interval:1", "nothing", trigger="interval-elapsed"
    )
    duplicate = service.evaluate_meeting_silence_action(
        meeting, "interval:1", "nothing", trigger="interval-elapsed"
    )
    second = service.evaluate_meeting_silence_action(
        meeting, "interval:2", "nothing", trigger="interval-elapsed"
    )

    assert first["accepted"] is True and first["granted"] is False
    assert duplicate["duplicate"] is True
    assert second["noOpSelectionCount"] == 2
    assert second["actionEvaluationCount"] == 2
    assert second["floorGrantCount"] == 0
    assert second["floorOpen"] is False
    assert open_floor_calls == []
    events = service.tail("system_diagnostics", count=10)["events"]
    noops = [
        event
        for event in events
        if event["type"] == "CONVERSATION_SILENCE_ACTION_EVALUATED"
    ]
    assert len(noops) == 2
    assert all(event["data"]["reason"] == "configured-say-nothing" for event in noops)


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


@pytest.mark.parametrize(
    "source",
    ["known", "roles", "silence", "events", "admin", "history", "tabs"],
)
def test_forgotten_meeting_never_resurrects_from_passive_sources(
    service, monkeypatch, source
) -> None:
    profile = service.config.state_dir / "meet_bridge_profile"
    meeting = "https://meet.google.com/abc-defg-hij"
    monkeypatch.setattr(
        service,
        "_meet_bridge_health",
        lambda timeout=0.5: {"meetingUrl": meeting} if source == "live" else None,
    )
    state = {"forgotten_meeting_urls": [meeting]}
    if source == "known":
        state["known_meeting_urls"] = [meeting]
    elif source == "roles":
        state["meeting_role_account_maps"] = {meeting: {"host": "sso_1"}}
    elif source == "silence":
        state["meeting_companion_click"] = {meeting: {"enabled": True}}
    elif source == "events":
        service.add_conversation(
            f"old caption from {meeting}",
            source_id="google-meet-captions",
            source_kind="operator",
        )
    elif source == "admin":
        service.admin_ui_state.set_page("meet", {"remembered": [meeting]})
    else:
        profile.mkdir(parents=True, exist_ok=True)
        (profile / ("Tabs_1" if source == "tabs" else "History")).write_text(
            f"visited {meeting}?authuser=1", encoding="utf-8"
        )
    service.meet_browser_settings.update_profile_state(
        profile, lambda current: current.update(state)
    )

    assert service._known_meeting_urls(profile) == []
    persisted = service.meet_browser_settings.get_profile_state(profile)
    assert persisted["known_meeting_urls"] == []
    assert persisted["forgotten_meeting_urls"] == [meeting]


def test_passive_live_status_does_not_unforget_meeting(service, monkeypatch) -> None:
    profile = service.config.state_dir / "meet_bridge_profile"
    meeting = "https://meet.google.com/abc-defg-hij"
    service.meet_browser_settings.set_profile_state(
        profile, forgotten_meeting_urls=[meeting]
    )
    monkeypatch.setattr(
        service, "_meet_bridge_health", lambda timeout=0.5: {"meetingUrl": meeting}
    )

    assert service._known_meeting_urls(profile, include_history=False) == []
    assert service.meet_browser_settings.get_profile_state(profile)[
        "forgotten_meeting_urls"
    ] == [meeting]


def test_prune_keeps_exact_channels_cleans_scoped_config_and_preserves_events(
    service, monkeypatch
) -> None:
    profile = service.config.state_dir / "meet_bridge_profile"
    driver = "https://meet.google.com/bgb-xqts-xjt"
    client_meeting = "https://meet.google.com/qmj-bkbk-mik"
    old = "https://meet.google.com/abc-defg-hij"
    service.meet_browser_settings.set("profile_path", str(profile))
    service.meet_browser_settings.set_profile_state(
        profile,
        known_meeting_urls=[driver, client_meeting, old],
        meeting_role_account_maps={old: {"host": "sso_1"}, driver: {"host": "sso_2"}},
        meeting_companion_click={old: {"enabled": True}, driver: {"enabled": False}},
        active_test_companion_click={
            "channelKey": old,
            "testProfile": "count20",
            "expiresAt": 9999999999,
        },
    )
    service.add_conversation(
        f"durable transcript in {old}",
        source_id="google-meet-captions",
        source_kind="operator",
    )
    before = service.tail("conversation", count=100)["events"]
    monkeypatch.setattr(
        service, "_meet_bridge_health", lambda timeout=0.5: {"meetingUrl": driver}
    )

    result = service.prune_meeting_channels([driver.upper(), client_meeting])

    assert result == {
        "kept": [driver, client_meeting],
        "forgotten": [old],
        "alreadyForgotten": [],
        "active": driver,
        "historyPreserved": True,
    }
    state = service.meet_browser_settings.get_profile_state(profile)
    assert state["known_meeting_urls"] == [driver, client_meeting]
    assert state["forgotten_meeting_urls"] == [old]
    assert state["meeting_role_account_maps"] == {driver: {"host": "sso_2"}}
    assert state["meeting_companion_click"] == {driver: {"enabled": False}}
    assert state["active_test_companion_click"] == {}
    assert service.tail("conversation", count=100)["events"] == before


def test_prune_is_normalized_idempotent_and_direct_helper_is_writer_free(
    tmp_path
) -> None:
    state_dir = tmp_path / "collab_state"
    profile = state_dir / "profile"
    keep = "https://meet.google.com/bgb-xqts-xjt"
    old = "https://meet.google.com/abc-defg-hij"
    store = MeetBrowserSettings(state_dir)
    store.set("profile_path", str(profile))
    store.set_profile_state(profile, known_meeting_urls=[keep, old])

    first = prune_meeting_channels(
        state_dir, ["BGB-XQTS-XJT"], active_meeting_url=keep
    )
    second = prune_meeting_channels(
        state_dir, [f"{keep}?authuser=1"], active_meeting_url=keep
    )

    assert first["kept"] == [keep]
    assert first["forgotten"] == [old]
    assert second["forgotten"] == []
    assert second["alreadyForgotten"] == [old]
    assert first["historyPreserved"] is True
    assert not (state_dir / "events").exists()


def test_active_meeting_cannot_be_forgotten_or_pruned(service, monkeypatch) -> None:
    active = "https://meet.google.com/abc-defg-hij"
    service.meet_browser_settings.set(
        "profile_path", str(service.config.state_dir / "meet_bridge_profile")
    )
    monkeypatch.setattr(
        service, "_meet_bridge_health", lambda timeout=0.5: {"meetingUrl": active}
    )

    with pytest.raises(ConflictError, match="currently active"):
        service.forget_meeting_channel(active)
    with pytest.raises(ConflictError, match="currently active"):
        service.prune_meeting_channels(["https://meet.google.com/bgb-xqts-xjt"])


def test_explicit_join_unforgets_but_passive_discovery_does_not(
    service, monkeypatch
) -> None:
    profile = service.config.state_dir / "meet_bridge_profile"
    meeting = "https://meet.google.com/abc-defg-hij"
    service.meet_browser_settings.set("profile_path", str(profile))
    service.meet_browser_settings.set_profile_state(
        profile, forgotten_meeting_urls=[meeting]
    )
    monkeypatch.setattr(
        service, "_meet_bridge_command", lambda command, timeout=1.0: {"verdict": command}
    )

    result = service.meet_bridge_command(f"/join {meeting}?authuser=2")

    state = service.meet_browser_settings.get_profile_state(profile)
    assert result["verdict"].startswith("/join ")
    assert state["forgotten_meeting_urls"] == []
    assert state["known_meeting_urls"] == [meeting]


def test_channel_forget_api_requires_operator_and_is_idempotent(
    client, admin_headers, viewer_headers, app_context, monkeypatch
) -> None:
    meeting = "https://meet.google.com/abc-defg-hij"
    service = app_context.service
    profile = service._meet_profile_path()
    service.meet_browser_settings.set_profile_state(
        profile, known_meeting_urls=[meeting]
    )
    monkeypatch.setattr(service, "_meet_bridge_health", lambda timeout=0.5: None)

    denied = client.post(
        f"{V1}/meet/channels/forget",
        headers=viewer_headers,
        json={"meeting_url": meeting},
    )
    unauthenticated = client.post(
        f"{V1}/meet/channels/forget",
        json={"meeting_url": meeting},
    )
    first = client.post(
        f"{V1}/meet/channels/forget",
        headers=admin_headers,
        json={"meeting_url": meeting},
    )
    second = client.post(
        f"{V1}/meet/channels/forget",
        headers=admin_headers,
        json={"meeting_url": meeting},
    )

    assert denied.status_code == 403
    assert unauthenticated.status_code == 401
    assert first.status_code == 200
    assert first.json()["forgotten"] == [meeting]
    assert second.json()["alreadyForgotten"] == [meeting]
    route = client.get("/openapi.json").json()["paths"][
        "/ws_collab/meet/channels/forget"
    ]
    assert set(route) == {"post"}


def test_channel_prune_api_requires_operator_and_rejects_paths(
    client, admin_headers, viewer_headers, app_context, monkeypatch
) -> None:
    service = app_context.service
    service.meet_browser_settings.set(
        "profile_path", str(service.config.state_dir / "meet_bridge_profile")
    )
    monkeypatch.setattr(service, "_meet_bridge_health", lambda timeout=0.5: None)
    payload = {"keep": ["https://meet.google.com/bgb-xqts-xjt"]}

    assert client.post(
        f"{V1}/meet/channels/prune", headers=viewer_headers, json=payload
    ).status_code == 403
    assert client.post(
        f"{V1}/meet/channels/prune",
        headers=admin_headers,
        json={"keep": ["../meet_browser_settings.json"]},
    ).status_code == 400
    allowed = client.post(
        f"{V1}/meet/channels/prune", headers=admin_headers, json=payload
    )
    assert allowed.status_code == 200
    assert allowed.json()["kept"] == payload["keep"]


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


def test_persisted_legacy_click_command_is_accepted_by_bridge_startup(
    service, monkeypatch, tmp_path
) -> None:
    captured = {}

    class Process:
        pid = 4243

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        return Process()

    profile = tmp_path / "profile"
    service.meet_browser_settings.set("profile_path", str(profile))
    service.meet_browser_settings.set_profile_state(
        profile,
        companion_click={"enabled": True, "sound": "click"},
    )
    monkeypatch.setattr(service, "_meet_bridge_health", lambda timeout=0.75: None)
    monkeypatch.setattr(service, "_meet_bridge_port_open", lambda: False)
    monkeypatch.setattr(
        service,
        "get_meet_role_assignments",
        lambda meeting_url="": {
            "role_assignments": [
                {
                    "role": "host",
                    "account_id": "sso_1",
                    "authuser": 0,
                    "email": "one@example.test",
                },
                {
                    "role": "companion",
                    "account_id": "sso_2",
                    "authuser": 1,
                    "email": "two@example.test",
                },
            ]
        },
    )
    monkeypatch.setattr("ws_collab.service.subprocess.Popen", fake_popen)

    service.start_meet_bridge("https://meet.google.com/abc-defg-hij")
    argv = captured["argv"]
    action_at = argv.index("--companion-click-action")
    assert argv[action_at + 1] == "say:click"
    assert "--companion-click-phrase" not in argv
    assert "--companion-click-sound" not in argv

    monkeypatch.setattr(bridge, "ensure_default_profile_migrated", lambda: profile)
    monkeypatch.setattr(bridge, "browser_profile_root", lambda *_args, **_kwargs: profile)
    monkeypatch.setattr(bridge, "list_tabs", lambda _endpoint: [])
    monkeypatch.setattr(
        sys,
        "argv",
        ["ws-collab-meet-bridge", *argv[4:], "--attach-only", "--list-tabs"],
    )
    bridge.main()


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
