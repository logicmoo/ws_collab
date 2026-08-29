from __future__ import annotations

from pathlib import Path

from ws_collab.meet_bridge.cdp import companion_profile_path

V1 = "/ws_collab/v1"


def test_list_meet_sso_profiles_uses_defaults_when_bridge_offline(client, admin_headers, monkeypatch, tmp_path):
    from ws_collab import service as service_mod

    host = tmp_path / "meet_profile"
    companion = companion_profile_path(host)
    host.mkdir()
    monkeypatch.setattr(service_mod, "DEFAULT_PROFILE", host)
    monkeypatch.setattr(service_mod.WsCollabService, "_meet_bridge_health", lambda self, timeout=0.5: None)
    body = client.get(f"{V1}/meet/sso/profiles", headers=admin_headers).json()
    roles = {row["role"]: row for row in body["profiles"]}
    assert roles["host"]["path"] == str(host)
    assert roles["host"]["exists"] is True
    assert roles["companion"]["path"] == str(companion)
    assert roles["companion"]["exists"] is False


def test_open_meet_sso_profile_launches_browser(client, admin_headers, monkeypatch, tmp_path):
    from ws_collab import service as service_mod

    host = tmp_path / "meet_profile"
    monkeypatch.setattr(service_mod, "DEFAULT_PROFILE", host)
    monkeypatch.setattr(service_mod.WsCollabService, "_meet_bridge_health", lambda self, timeout=0.5: None)
    monkeypatch.setattr(service_mod, "find_browser", lambda explicit: r"C:\Chrome\chrome.exe")

    launched = {}

    class FakePopen:
        def __init__(self, argv, **kwargs):
            launched["argv"] = argv
            self.pid = 4321

    monkeypatch.setattr(service_mod.subprocess, "Popen", FakePopen)
    body = client.post(f"{V1}/meet/sso/open", headers=admin_headers, json={"role": "host"}).json()
    assert body["ok"] is True
    assert body["pid"] == 4321
    assert any(str(host) in arg for arg in launched["argv"])
    assert launched["argv"][-1] == "https://accounts.google.com/"
    assert body["reused_bridge_window"] is False


def test_open_meet_sso_profile_reuses_bridge_window_when_process_is_alive(client, admin_headers, monkeypatch, tmp_path):
    from ws_collab import service as service_mod

    host = tmp_path / "meet_profile"
    host.mkdir()
    monkeypatch.setattr(service_mod, "DEFAULT_PROFILE", host)
    monkeypatch.setattr(service_mod.WsCollabService, "_meet_bridge_health", lambda self, timeout=0.5: {
        "processes": [{"role": "host", "profile": str(host), "alive": True, "pid": 2468}],
    })
    monkeypatch.setattr(service_mod.WsCollabService, "_meet_bridge_command", lambda self, command, timeout=1.0: {
        "ok": True,
        "verdict": "sso:host",
    })

    def fail_popen(*args, **kwargs):
        raise AssertionError("should not launch a new browser when bridge window can be reused")

    monkeypatch.setattr(service_mod.subprocess, "Popen", fail_popen)
    body = client.post(f"{V1}/meet/sso/open", headers=admin_headers, json={"role": "host"}).json()
    assert body["ok"] is True
    assert body["reused_bridge_window"] is True


def test_forget_meet_sso_profile_refuses_when_bridge_reports_profile_in_use(client, admin_headers, monkeypatch, tmp_path):
    from ws_collab import service as service_mod

    host = tmp_path / "meet_profile"
    host.mkdir()
    monkeypatch.setattr(service_mod, "DEFAULT_PROFILE", host)
    monkeypatch.setattr(service_mod.WsCollabService, "_meet_bridge_health", lambda self, timeout=0.5: {
        "processes": [{"role": "host", "profile": str(host), "alive": True, "pid": 999}],
    })
    response = client.post(f"{V1}/meet/sso/forget", headers=admin_headers, json={"role": "host"})
    assert response.status_code == 409
    assert "Close the bridge browser window first" in response.text
