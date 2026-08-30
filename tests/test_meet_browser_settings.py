from __future__ import annotations

import sys

from ws_collab.meet_browser_settings import MeetBrowserSettings

V1 = "/ws_collab/v1"


def test_meet_browser_settings_persist_across_instances(tmp_path) -> None:
    store = MeetBrowserSettings(tmp_path)
    store.set("browser_backend", "wsl")
    store.set("profile_path", str(tmp_path / "profile"))

    reopened = MeetBrowserSettings(tmp_path)
    assert reopened.get("browser_backend") == "wsl"
    assert reopened.get("profile_path") == str(tmp_path / "profile")
    assert (tmp_path / "meet_browser_settings.json").is_file()


def test_meet_browser_settings_endpoint_round_trip(client, admin_headers, monkeypatch, tmp_path):
    from ws_collab import service as service_mod

    monkeypatch.setattr(service_mod, "DEFAULT_PROFILE", tmp_path / "default_profile")
    monkeypatch.setattr(service_mod.WsCollabService, "_meet_bridge_health", lambda self, timeout=0.5: None)
    body = client.post(
        f"{V1}/meet/browser-settings",
        headers=admin_headers,
        json={"browser_backend": "wsl", "profile_path": str(tmp_path / "custom profile")},
    ).json()
    assert body["browser_backend"] == "wsl"
    assert body["profile_path"] == str(tmp_path / "custom profile")
    assert "--profile-mode" not in body["next_launch_command"]
    assert "--role-authuser" not in body["next_launch_command"]
    assert "role_account_map" not in body
    assert "role_assignments" not in body
    fetched = client.get(f"{V1}/meet/browser-settings", headers=admin_headers).json()
    assert fetched["browser_backend"] == "wsl"
    assert "profile_mode" not in fetched
    assert "companion_profile_path" not in fetched


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
    assert captured["argv"].count("host=0") == 1
    assert captured["argv"].count("companion=0") == 1
    assert "https://meet.google.com/abc-defg-hij" in captured["argv"]
    assert "WS_COLLAB_TOKEN" not in " ".join(captured["argv"])
    assert captured["kwargs"]["env"]["WS_COLLAB_TOKEN"] in service.config.tokens


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
