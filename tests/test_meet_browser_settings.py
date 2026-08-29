from __future__ import annotations

import pytest

from ws_collab.errors import ValidationError
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


def test_meet_role_settings_reject_duplicate_accounts(service, monkeypatch, tmp_path) -> None:
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

    with pytest.raises(ValidationError, match="distinct signed-in accounts"):
        service.set_meet_role_assignments({"host": "sso_1", "companion": "sso_1"})


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
