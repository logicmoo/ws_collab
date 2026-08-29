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


def test_driver_startup_requires_explicit_sso_role_assignments(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(bridge, "ensure_default_profile_migrated", lambda: tmp_path / "profile")
    monkeypatch.setattr(sys, "argv", ["ws-collab-meet-bridge", "--companion"])

    with pytest.raises(SystemExit, match="missing --role-authuser for: host, companion"):
        bridge.main()
