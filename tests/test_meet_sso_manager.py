from __future__ import annotations

V1 = "/ws_collab/v1"


def test_list_meet_sso_accounts_is_account_centric_when_bridge_offline(client, admin_headers, monkeypatch, tmp_path):
    from ws_collab import service as service_mod

    host = tmp_path / "meet_profile"
    host.mkdir()
    monkeypatch.setattr(service_mod, "DEFAULT_PROFILE", host)
    monkeypatch.setattr(service_mod.WsCollabService, "_meet_bridge_health", lambda self, timeout=0.5: None)
    body = client.get(f"{V1}/meet/sso/accounts", headers=admin_headers).json()
    assert body == {
        "profile_path": str(host),
        "accounts": [],
        "signed_in_count": 0,
        "ready_for_meet": False,
    }


def test_open_meet_sso_account_launches_sign_in_page(client, admin_headers, monkeypatch, tmp_path):
    from ws_collab import service as service_mod

    host = tmp_path / "meet_profile"
    monkeypatch.setattr(service_mod, "DEFAULT_PROFILE", host)
    monkeypatch.setattr(service_mod.WsCollabService, "_meet_bridge_health", lambda self, timeout=0.5: None)
    monkeypatch.setattr(service_mod, "find_browser", lambda explicit: r"C:\Chrome\chrome.exe")
    monkeypatch.setattr(service_mod, "cdp_alive", lambda _endpoint: False)

    launched = {}

    class FakePopen:
        def __init__(self, argv, **kwargs):
            launched["argv"] = argv
            self.pid = 4321

    monkeypatch.setattr(service_mod.subprocess, "Popen", FakePopen)
    body = client.post(f"{V1}/meet/sso/open", headers=admin_headers, json={"add_account": True}).json()
    assert body["ok"] is True
    assert body["pid"] == 4321
    assert any(str(host) in arg for arg in launched["argv"])
    assert "--remote-debugging-port=9223" in launched["argv"]
    assert launched["argv"][-1] == "https://accounts.google.com/AccountChooser?continue=https://accounts.google.com/"
    assert body["reused_bridge_window"] is False


def test_list_meet_sso_accounts_reports_only_live_sign_ins_as_ready(
    client, app_context, admin_headers, monkeypatch, tmp_path
):
    from ws_collab import service as service_mod

    profile = tmp_path / "meet_profile"
    monkeypatch.setattr(service_mod, "DEFAULT_PROFILE", profile)
    monkeypatch.setattr(service_mod.WsCollabService, "_meet_bridge_health", lambda self, timeout=0.5: None)
    monkeypatch.setattr(
        service_mod.WsCollabService,
        "_meet_browser_live_accounts",
        lambda self: [
            {"email": "one@example.test", "authuser": 0, "signedIn": True},
            {"email": "two@example.test", "authuser": 1, "signedIn": True},
        ],
    )

    body = client.get(f"{V1}/meet/sso/accounts", headers=admin_headers).json()

    assert body["ready_for_meet"] is True
    assert body["signed_in_count"] == 2
    assert [(row["email"], row["signed_in"]) for row in body["accounts"]] == [
        ("one@example.test", True),
        ("two@example.test", True),
    ]


def test_open_meet_sso_account_reuses_and_foregrounds_bridge_window(
    client, app_context, admin_headers, monkeypatch, tmp_path
):
    from ws_collab import service as service_mod

    host = tmp_path / "meet_profile"
    host.mkdir()
    monkeypatch.setattr(service_mod, "DEFAULT_PROFILE", host)
    monkeypatch.setattr(service_mod.WsCollabService, "_meet_bridge_health", lambda self, timeout=0.5: {
        "processes": [{"role": "host", "profile": str(host), "alive": True, "pid": 2468}],
    })
    commands = []

    def bridge_command(self, command, timeout=1.0):
        commands.append(command)
        return {"ok": True, "verdict": "sso:0"}

    monkeypatch.setattr(service_mod.WsCollabService, "_meet_bridge_command", bridge_command)

    def fail_popen(*args, **kwargs):
        raise AssertionError("should not launch a new browser when bridge window can be reused")

    monkeypatch.setattr(service_mod.subprocess, "Popen", fail_popen)
    app_context.service._set_meet_profile_state(
        host,
        accounts={"sso_1": {"id": "sso_1", "email": "one@example.test", "authuser": 0}},
    )
    body = client.post(f"{V1}/meet/sso/open", headers=admin_headers, json={"account_id": "sso_1"}).json()
    assert body["ok"] is True
    assert body["reused_bridge_window"] is True
    assert commands == ["/sso 0"]


def test_forget_meet_sso_profile_refuses_when_bridge_reports_profile_in_use(client, admin_headers, monkeypatch, tmp_path):
    from ws_collab import service as service_mod

    host = tmp_path / "meet_profile"
    host.mkdir()
    monkeypatch.setattr(service_mod, "DEFAULT_PROFILE", host)
    monkeypatch.setattr(service_mod.WsCollabService, "_meet_bridge_health", lambda self, timeout=0.5: {
        "processes": [{"role": "host", "profile": str(host), "alive": True, "pid": 999}],
    })
    response = client.post(f"{V1}/meet/sso/forget", headers=admin_headers, json={})
    assert response.status_code == 409
    assert "Close the bridge browser window first" in response.text
