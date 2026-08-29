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
