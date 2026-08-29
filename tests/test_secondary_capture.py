from __future__ import annotations


def test_secondary_capture_state_defaults(service) -> None:
    state = service.secondary_capture_state()
    assert state["listening"] is False
    assert state["device_id"] == ""


def test_secondary_capture_rest_start_stop(client, admin_headers, app_context) -> None:
    device_id = next(d["id"] for d in app_context.service.list_devices()["devices"] if d["direction"] in ("input", "loopback", "virtual"))
    started = client.post("/ws_collab/v1/audio/secondary-capture/start", headers=admin_headers, json={"device_id": device_id})
    assert started.status_code == 200
    assert started.json()["device_id"] == device_id
    stopped = client.post("/ws_collab/v1/audio/secondary-capture/stop", headers=admin_headers)
    assert stopped.status_code == 200
    assert stopped.json()["listening"] is False
