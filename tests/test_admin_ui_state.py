from __future__ import annotations

import json

import pytest

from ws_collab.admin_ui_state import AdminUIState
from ws_collab.errors import ValidationError

V1 = "/ws_collab/v1"


def test_admin_ui_state_persists_page_snapshots_and_redacts_credentials(tmp_path) -> None:
    store = AdminUIState(tmp_path)
    saved = store.set_page(
        "meet",
        {
            "controls": {"meet-join-url": "https://meet.google.com/abc-defg-hij"},
            "display": {"text": "HOST ready"},
            "preferences": {"ws_collab_toggle_connectors": "true"},
            "api_snapshots": {"/meet/role-assignments": {"accounts": ["sso_1"]}},
            "token": "must-not-persist",
            "nested": {"authorization": "Bearer secret", "safe": True},
        },
    )

    assert saved["exists"] is True
    assert "token" not in saved["state"]
    assert saved["state"]["nested"] == {"safe": True}

    reopened = AdminUIState(tmp_path)
    assert reopened.get_page("meet")["state"]["display"]["text"] == "HOST ready"
    on_disk = json.loads((tmp_path / "admin_ui_state.json").read_text(encoding="utf-8"))
    assert on_disk["pages"]["meet"]["state"]["controls"]["meet-join-url"].endswith("abc-defg-hij")


def test_admin_ui_state_rejects_invalid_page_and_non_json_state(tmp_path) -> None:
    store = AdminUIState(tmp_path)

    with pytest.raises(ValidationError, match="invalid admin page"):
        store.set_page("../meet", {})
    with pytest.raises(ValidationError, match="only JSON values"):
        store.set_page("meet", {"bad": object()})


def test_admin_ui_state_can_clear_one_auxiliary_page(tmp_path) -> None:
    store = AdminUIState(tmp_path)
    store.set_page("meet", {"display": {"text": "old meeting"}})
    store.set_page("silences", {"display": {"text": "keep"}})

    assert store.clear_page("meet") == {
        "page": "meet",
        "exists": False,
        "state": {},
    }
    assert store.get_page("silences")["exists"] is True


def test_admin_ui_state_endpoints_round_trip(client, admin_headers) -> None:
    response = client.post(
        f"{V1}/admin/ui-state/devices",
        headers=admin_headers,
        json={"state": {"controls": {"dv-search": "microphone"}, "display": {"text": "2 devices"}}},
    )
    assert response.status_code == 200

    fetched = client.get(f"{V1}/admin/ui-state/devices", headers=admin_headers).json()
    assert fetched["exists"] is True
    assert fetched["state"]["controls"]["dv-search"] == "microphone"
