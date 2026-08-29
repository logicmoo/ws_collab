from __future__ import annotations

from ws_collab.meet_browser_settings import MeetBrowserSettings

V1 = "/ws_collab/v1"


def test_meet_browser_settings_persist_across_instances(tmp_path) -> None:
    store = MeetBrowserSettings(tmp_path)
    store.set("browser_backend", "wsl")
    store.set("shared_window", True)
    store.set("profile_path", str(tmp_path / "profile"))

    reopened = MeetBrowserSettings(tmp_path)
    assert reopened.get("browser_backend") == "wsl"
    assert reopened.get("shared_window") is True
    assert reopened.get("profile_path") == str(tmp_path / "profile")
    assert (tmp_path / "meet_browser_settings.json").is_file()


def test_meet_browser_settings_endpoint_round_trip(client, admin_headers, monkeypatch, tmp_path):
    from ws_collab import service as service_mod

    monkeypatch.setattr(service_mod, "DEFAULT_PROFILE", tmp_path / "default_profile")
    monkeypatch.setattr(service_mod.WsCollabService, "_meet_bridge_health", lambda self, timeout=0.5: None)
    body = client.post(
        f"{V1}/meet/browser-settings",
        headers=admin_headers,
        json={"browser_backend": "wsl", "shared_window": True, "profile_path": str(tmp_path / "custom profile")},
    ).json()
    assert body["browser_backend"] == "wsl"
    assert body["shared_window"] is True
    assert body["profile_path"] == str(tmp_path / "custom profile")
    fetched = client.get(f"{V1}/meet/browser-settings", headers=admin_headers).json()
    assert fetched["browser_backend"] == "wsl"
    assert fetched["companion_profile_path"].endswith("_companion")
