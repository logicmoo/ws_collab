"""REST and WebSocket must be interchangeable.

These tests assert the parity contract itself: whatever is written through one
transport is immediately visible through the other, with the same identity,
cursor semantics, idempotency, filters, and structured errors -- and that no
essential capability is WebSocket-only.
"""

from __future__ import annotations

import json

import pytest
from starlette.websockets import WebSocketDisconnect

from ws_collab.events import streams_for_role

CONVERSATION = streams_for_role("conversation")[0]
API_BASE = "/ws_collab"


def _ws_login(ws, token: str) -> dict:
    ws.send_json({"type": "auth", "token": token})
    return ws.receive_json()


def _drain_until(ws, wanted: str, limit: int = 40) -> dict | None:
    for _ in range(limit):
        message = ws.receive_json()
        if message["type"] == wanted:
            return message
    return None


def _route_paths(router) -> list[str]:
    paths: list[str] = []
    for route in router.routes:
        if hasattr(route, "path"):
            paths.append(route.path)
        elif hasattr(route, "original_router"):
            paths.extend(_route_paths(route.original_router))
    return paths


# ------------------------------------------------------------------ discovery
def test_health_is_reachable_without_credentials(client) -> None:
    assert client.get(f"{API_BASE}/health").json()["status"] == "ok"


def test_capabilities_advertises_all_four_transports(client) -> None:
    caps = client.get(f"{API_BASE}/capabilities").json()
    assert {"http", "https", "ws", "wss"}.issubset(set(caps["transports"]))
    assert caps["streams"], "the stream registry must be discoverable"
    assert caps["stream_roles"], "semantic roles must be discoverable"


# --------------------------------------------------------- status / readiness
def test_status_reports_every_subsystem_with_an_overall_verdict(client) -> None:
    body = client.get(f"{API_BASE}/status").json()
    assert body["status"] in ("ok", "degraded", "down")
    assert body["subsystems"], "a rollup must name its parts"
    for name, entry in body["subsystems"].items():
        assert entry.get("state"), f"subsystem {name} must report a state"


def test_status_is_reachable_without_credentials(client) -> None:
    """Status pages and load balancers cannot authenticate."""

    assert client.get(f"{API_BASE}/status").status_code == 200


def test_status_leaks_no_secrets(client) -> None:
    from conftest import ADMIN_TOKEN

    body = client.get(f"{API_BASE}/status").text
    assert ADMIN_TOKEN not in body
    assert "session_secret" not in body


def test_health_and_status_answer_different_questions(client) -> None:
    health = client.get(f"{API_BASE}/health").json()
    status = client.get(f"{API_BASE}/status").json()
    assert "subsystems" not in health, "health is liveness only"
    assert "subsystems" in status, "status is a rollup"


def test_ready_signals_serving_state(client) -> None:
    response = client.get(f"{API_BASE}/ready")
    assert response.status_code in (200, 503)
    body = response.json()
    assert body["ready"] is (response.status_code == 200), "the code and body must agree"


# ------------------------------------------------------------ endpoint map
def test_endpoint_map_describes_both_transports(client, admin_headers) -> None:
    body = client.get(f"{API_BASE}/endpoints", headers=admin_headers).json()
    kinds = {e["kind"] for e in body["endpoints"]}
    assert {"http", "ws"} <= kinds, "clients must be able to find both transports"
    assert set(body["categories"]) == {
        "rest",
        "admin-control",
        "websocket",
        "admin_ui",
        "openapi",
        "internal_meet_bridge",
    }
    assert all(
        path.startswith("/ws_collab/meet-bridge/")
        for path in body["categories"]["internal_meet_bridge"]["paths"]
    )
    rest_routes = body["categories"]["rest"]["endpoints"]
    assert len(rest_routes) > 50
    assert all(route["path"].startswith(API_BASE) and route["methods"] for route in rest_routes)
    assert all(
        endpoint["description"]
        and endpoint["auth"]
        and endpoint["capability"]
        and endpoint["visibility"] in {"public", "admin-ui", "internal-worker"}
        for endpoint in body["endpoints"]
    )


def test_endpoint_map_urls_are_absolute_and_correctly_schemed(client, admin_headers) -> None:
    body = client.get(f"{API_BASE}/endpoints", headers=admin_headers).json()
    for endpoint in body["endpoints"]:
        if endpoint["kind"] == "ws":
            assert endpoint["url"].startswith(("ws://", "wss://")), endpoint
        else:
            assert endpoint["url"].startswith(("http://", "https://")), endpoint


def test_endpoint_map_matches_the_actual_rest_route_set(client, admin_headers) -> None:
    """Every mounted REST operation is inventoried, preventing metadata drift."""

    body = client.get(f"{API_BASE}/endpoints", headers=admin_headers).json()
    advertised = {
        (endpoint["path"], tuple(endpoint["methods"]))
        for category in ("rest", "admin-control")
        for endpoint in body["categories"][category]["endpoints"]
    }
    actual: set[tuple[str, tuple[str, ...]]] = set()

    def collect(router) -> None:
        for route in router.routes:
            if (
                getattr(route, "include_in_schema", False)
                and route.path.startswith(API_BASE)
                and getattr(route, "methods", None)
            ):
                actual.add((route.path, tuple(sorted(route.methods))))
            if included := getattr(route, "original_router", None):
                collect(included)

    collect(client.app)
    assert advertised == actual


def test_endpoint_map_declares_auth_requirements(client, admin_headers) -> None:
    body = client.get(f"{API_BASE}/endpoints", headers=admin_headers).json()
    assert all(e.get("auth") for e in body["endpoints"])
    public = {e["path"] for e in body["endpoints"] if e["auth"] == "none"}
    assert {f"{API_BASE}/health", f"{API_BASE}/status", f"{API_BASE}/ready"} <= public


# -------------------------------------------------------- canonical namespace
def test_legacy_root_and_versioned_aliases_are_not_mounted(client) -> None:
    old_paths = (
        "/",
        "/status",
        "/health",
        "/ready",
        "/v1/status",
        "/ws_collab/v1/status",
        "/ws",
        "/admin",
        "/openapi.json",
        "/docs",
        "/redoc",
    )
    assert {path: client.get(path).status_code for path in old_paths} == {
        path: 404 for path in old_paths
    }


def test_websocket_has_one_canonical_path(client, admin_headers) -> None:
    token = admin_headers["Authorization"].split()[1]
    with client.websocket_connect("/ws_collab/ws") as ws:
        assert _ws_login(ws, token)["type"] == "auth_ok"
    for old_path in ("/ws", "/v1/ws", "/ws_collab/v1/ws"):
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(old_path):
                pass


def test_endpoint_map_contains_no_aliases(client, admin_headers) -> None:
    body = client.get(f"{API_BASE}/endpoints", headers=admin_headers).json()
    assert "mounts" not in body
    assert all("aliases" not in endpoint for endpoint in body["endpoints"])
    assert all(endpoint["path"].startswith("/ws_collab") for endpoint in body["endpoints"])


def test_every_standalone_route_is_namespaced(client) -> None:
    paths = _route_paths(client.app)
    assert paths
    assert all(path.startswith("/ws_collab") for path in paths)
    for path in paths:
        if path.startswith(("/ws_collab/admin", "/ws_collab/openapi")):
            continue
        if path in {"/ws_collab/ws", "/ws_collab/{asset:path}"}:
            continue
        assert path.startswith(API_BASE), path
        assert "/v1/" not in path


def test_custom_plugin_prefix_composes_once(app_context, monkeypatch) -> None:
    from ws_collab import plugin_router

    monkeypatch.setattr(plugin_router, "_context", app_context)
    router = plugin_router.create_router({"routePrefix": "/custom/"})
    paths = _route_paths(router)
    assert "/custom/status" in paths
    assert "/custom/ws" in paths
    assert "/custom/admin/shutdown" in paths
    assert "/custom/admin/restart" in paths


def test_openapi_documents_each_operation_once(client) -> None:
    """Alias mounts are hidden from the schema so the docs are not quadrupled."""

    schema = client.get("/ws_collab/openapi.json").json()
    documented = [p for p in schema["paths"] if p.endswith("/health")]
    assert len(documented) == 1, f"health documented {len(documented)} times: {documented}"
    assert all(p.startswith(API_BASE) for p in schema["paths"])
    operations = [
        operation
        for methods in schema["paths"].values()
        for operation in methods.values()
    ]
    assert all(operation["summary"] and operation["description"] for operation in operations)


def test_interactive_docs_moved_aside_for_markdown_docs(client, admin_headers) -> None:
    assert client.get("/ws_collab/openapi/docs").status_code == 200
    body = client.get(f"{API_BASE}/docs", headers=admin_headers).json()
    assert body["documents"]


# --------------------------------------------------------------- admin UI
HTML = {"Accept": "text/html"}


def test_health_path_is_always_json_even_for_browsers(client) -> None:
    response = client.get(f"{API_BASE}/health", headers=HTML)
    assert response.json()["status"] == "ok"


def test_ui_assets_resolve_only_under_admin(client) -> None:
    for asset in ("app.css", "app.js"):
        assert client.get(f"/ws_collab/admin/{asset}").status_code == 200
        assert client.get(f"/{asset}").status_code == 404


def test_static_fallback_cannot_mask_api_misses_or_traversal(client) -> None:
    assert client.get("/ws_collab/not-a-route", headers=HTML).status_code == 404
    assert client.get("/ws_collab/v1/status", headers=HTML).status_code == 404
    assert client.get("/ws_collab/admin/../../pyproject.toml").status_code == 404


def test_ui_at_roots_honours_the_admin_restriction(tmp_path) -> None:
    """Serving the UI at the root must not bypass the loopback-only rule."""

    from fastapi.testclient import TestClient

    from conftest import make_config
    from ws_collab.context import AppContext
    from ws_collab.security import Security
    from ws_collab.server import build_app
    from ws_collab.service import WsCollabService

    from conftest import make_event_store

    config = make_config(tmp_path)  # admin_remote defaults to off
    store = make_event_store(config)
    try:
        service = WsCollabService(config, store)
        context = AppContext(config=config, store=store, service=service,
                             security=Security(config, audit_sink=service._audit_sink))
        with TestClient(build_app(context, with_lifespan=False)) as guarded_client:
            assert guarded_client.get("/", headers=HTML).status_code == 404
            assert guarded_client.get("/ws_collab/status", headers=HTML).status_code == 200
            assert guarded_client.get("/ws_collab/admin/", headers=HTML).status_code == 403
    finally:
        store.close()


# ------------------------------------------------------------------- docs
def test_docs_are_listed_and_readable(client, admin_headers) -> None:
    listing = client.get(f"{API_BASE}/docs", headers=admin_headers).json()
    assert listing["documents"], "shipped documentation must be discoverable"
    name = listing["documents"][0]["name"]
    body = client.get(f"{API_BASE}/docs/{name}", headers=admin_headers)
    assert body.status_code == 200 and body.text.strip()


def test_docs_refuse_non_markdown_and_traversal(client, admin_headers) -> None:
    assert client.get(f"{API_BASE}/docs/../config.py", headers=admin_headers).status_code in (400, 404)
    assert client.get(f"{API_BASE}/docs/secrets.env", headers=admin_headers).status_code == 400


def test_docs_require_authentication(client) -> None:
    assert client.get(f"{API_BASE}/docs").status_code == 401


def test_repo_owned_clients_do_not_use_legacy_endpoint_literals() -> None:
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    clients = [
        *root.glob("src/ws_collab/**/*.py"),
        *root.glob("src/ws_collab/**/*.js"),
        *root.glob("examples/clients/*.py"),
        root / "plugin.json",
    ]
    internal_operation = (
        "status|health|ready|endpoints|capabilities|events|conversation|mailbox|"
        "workers|audio|stt|transcripts|tts|voices|meet|cursors|prompt|config|"
        "diagnostics|alerts|audit|docs|ui|files|convert|browser|streams"
    )
    forbidden = re.compile(
        rf"""ws_collab/v1|(?P<quote>["'`])/v1/(?:{internal_operation})(?:[/?]|(?P=quote))"""
    )
    violations = {
        str(path.relative_to(root)): forbidden.findall(path.read_text(encoding="utf-8"))
        for path in clients
        if forbidden.search(path.read_text(encoding="utf-8"))
    }
    assert violations == {}


# ---------------------------------------------------------------- ui links
def test_ui_links_cover_every_workbench_page(client, admin_headers) -> None:
    body = client.get(f"{API_BASE}/ui/links", headers=admin_headers).json()
    assert body["links"], "the UI must be navigable programmatically"
    assert all(link["url"].startswith("http") and "#" in link["url"] for link in body["links"])
    assert all(link["title"] and link["description"] for link in body["links"])


# ------------------------------------------------------------------- files
def test_files_lists_the_state_directory(client, admin_headers) -> None:
    body = client.get(f"{API_BASE}/files", headers=admin_headers).json()
    assert body["entries"], "the writable state directory must be inspectable"
    assert all({"name", "kind", "readable", "protected"} <= set(e) for e in body["entries"])


def test_files_never_serve_the_generated_token(client, admin_headers, app_context) -> None:
    """The state directory holds credentials; listing them is fine, reading is not."""

    token_path = app_context.config.generated_token_path
    token_path.write_text("super-secret-value\n", encoding="utf-8")

    listing = client.get(f"{API_BASE}/files", headers=admin_headers).json()
    entry = next((e for e in listing["entries"] if e["name"] == token_path.name), None)
    assert entry is not None, "operators should know the file exists"
    assert entry["protected"] is True and entry["readable"] is False

    response = client.get(f"{API_BASE}/files/content", headers=admin_headers,
                          params={"path": token_path.name})
    assert response.status_code == 403
    assert "super-secret-value" not in response.text


def test_files_reject_path_traversal(client, admin_headers) -> None:
    for attempt in ("../config.py", "../../etc/passwd", "sessions/../../secret"):
        response = client.get(f"{API_BASE}/files/content", headers=admin_headers, params={"path": attempt})
        assert response.status_code in (400, 403, 404), f"{attempt} must not escape the state root"


def test_files_read_is_bounded(client, admin_headers, app_context) -> None:
    big = app_context.config.state_dir / "big.log"
    big.write_text("x" * (400 * 1024), encoding="utf-8")
    body = client.get(f"{API_BASE}/files/content", headers=admin_headers, params={"path": "big.log"}).json()
    assert body["truncated"] is True
    assert len(body["content"]) < 400 * 1024, "a huge file must not be returned whole"


def test_files_require_operator_role(client, worker_headers, viewer_headers) -> None:
    assert client.get(f"{API_BASE}/files", headers=viewer_headers).status_code == 403
    assert client.get(f"{API_BASE}/files", headers=worker_headers).status_code == 403


def test_unauthenticated_reads_are_refused(client) -> None:
    response = client.get(f"{API_BASE}/events", params={"stream": CONVERSATION})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_required"


# ------------------------------------------------------------- cross-transport
def test_event_written_over_rest_is_visible_over_websocket(client, admin_headers) -> None:
    written = client.post(
        f"{API_BASE}/conversation/events", headers=admin_headers, json={"text": "from rest"}
    ).json()
    with client.websocket_connect("/ws_collab/ws") as ws:
        _ws_login(ws, admin_headers["Authorization"].split()[1])
        ws.send_json({"type": "subscribe", "streams": [CONVERSATION]})
        seen = []
        for _ in range(40):
            message = ws.receive_json()
            if message["type"] == "event":
                seen.append(message["event"])
            if message["type"] == "caught_up":
                break
        assert any(e["id"] == written["id"] for e in seen), "WS catch-up must include REST writes"


def test_event_written_over_websocket_is_visible_over_rest(client, admin_headers) -> None:
    with client.websocket_connect("/ws_collab/ws") as ws:
        _ws_login(ws, admin_headers["Authorization"].split()[1])
        ws.send_json({
            "type": "publish", "ack_id": "a1", "stream": CONVERSATION,
            "event_type": "CONVERSATION_MESSAGE", "data": {"text": "from ws"},
        })
        ack = _drain_until(ws, "ack")
    assert ack and ack["id"]
    page = client.get(f"{API_BASE}/events", headers=admin_headers, params={"stream": CONVERSATION}).json()
    assert any(e["id"] == ack["id"] for e in page["events"]), "REST must see WS writes"


def test_identity_and_cursor_semantics_match_across_transports(client, admin_headers) -> None:
    rest = client.post(f"{API_BASE}/conversation/events", headers=admin_headers, json={"text": "a"}).json()
    with client.websocket_connect("/ws_collab/ws") as ws:
        _ws_login(ws, admin_headers["Authorization"].split()[1])
        ws.send_json({
            "type": "publish", "ack_id": "x", "stream": CONVERSATION,
            "event_type": "CONVERSATION_MESSAGE", "data": {"text": "b"},
        })
        ws_ack = _drain_until(ws, "ack")
    assert set(rest) >= {"id", "seq", "cursor", "duplicate"}
    assert set(ws_ack) >= {"id", "seq", "cursor", "duplicate"}
    assert ws_ack["seq"] > rest["seq"], "both transports share one ordered sequence"


# ------------------------------------------------------------------ contracts
def test_idempotency_is_enforced_on_rest_writes(client, admin_headers) -> None:
    headers = {**admin_headers, "Idempotency-Key": "same-key"}
    first = client.post(f"{API_BASE}/conversation/events", headers=headers, json={"text": "once"}).json()
    second = client.post(f"{API_BASE}/conversation/events", headers=headers, json={"text": "once"}).json()
    assert first["duplicate"] is False and second["duplicate"] is True
    assert first["id"] == second["id"]


def test_idempotency_is_enforced_on_websocket_writes(client, admin_headers) -> None:
    with client.websocket_connect("/ws_collab/ws") as ws:
        _ws_login(ws, admin_headers["Authorization"].split()[1])
        frame = {
            "type": "publish", "stream": CONVERSATION, "event_type": "CONVERSATION_MESSAGE",
            "data": {"text": "once"}, "idempotency_key": "ws-key",
        }
        ws.send_json({**frame, "ack_id": "1"})
        first = _drain_until(ws, "ack")
        ws.send_json({**frame, "ack_id": "2"})
        second = _drain_until(ws, "ack")
    assert first["duplicate"] is False and second["duplicate"] is True


def test_pagination_is_bounded_and_reports_more(client, admin_headers) -> None:
    for i in range(6):
        client.post(f"{API_BASE}/conversation/events", headers=admin_headers, json={"text": f"m{i}"})
    page = client.get(f"{API_BASE}/events", headers=admin_headers,
                      params={"stream": CONVERSATION, "limit": 2}).json()
    assert len(page["events"]) == 2 and page["has_more"] is True
    assert page["next_cursor"] and page["server_time"]


def test_conditional_request_avoids_resending_an_empty_page(client, admin_headers) -> None:
    client.post(f"{API_BASE}/conversation/events", headers=admin_headers, json={"text": "x"})
    page = client.get(f"{API_BASE}/events", headers=admin_headers, params={"stream": CONVERSATION}).json()
    cursor = page["next_cursor"]
    response = client.get(f"{API_BASE}/events", headers={**admin_headers, "If-None-Match": cursor},
                          params={"stream": CONVERSATION, "after": cursor})
    assert response.status_code == 304


def test_long_polling_returns_promptly_when_idle(client, admin_headers) -> None:
    page = client.get(f"{API_BASE}/events", headers=admin_headers,
                      params={"stream": CONVERSATION, "wait_ms": 150}).json()
    assert page["events"] == [] and page["next_cursor"]


def test_filters_apply_identically_through_rest(client, admin_headers) -> None:
    client.post(f"{API_BASE}/conversation/events", headers=admin_headers, json={"text": "keep me"})
    client.post(f"{API_BASE}/events", headers=admin_headers,
                json={"stream": CONVERSATION, "type": "NOISE", "data": {"text": "ignore"}})
    page = client.get(f"{API_BASE}/events", headers=admin_headers,
                      params={"stream": CONVERSATION, "type": "NOISE"}).json()
    assert all(e["type"] == "NOISE" for e in page["events"]) and page["events"]


# --------------------------------------------------------------------- errors
def test_unknown_stream_is_a_structured_error_on_both_transports(client, admin_headers) -> None:
    rest = client.get(f"{API_BASE}/events", headers=admin_headers, params={"stream": "nope"})
    assert rest.status_code == 400
    rest_code = rest.json()["error"]["code"]

    with client.websocket_connect("/ws_collab/ws") as ws:
        _ws_login(ws, admin_headers["Authorization"].split()[1])
        ws.send_json({"type": "subscribe", "streams": ["nope"]})
        error = _drain_until(ws, "error")
    assert error and error["error"]["code"] == rest_code, "error codes must match across transports"


def test_websocket_requires_authentication_before_use(client) -> None:
    with client.websocket_connect("/ws_collab/ws") as ws:
        ws.send_json({"type": "subscribe", "streams": [CONVERSATION]})
        error = _drain_until(ws, "error")
    assert error and error["error"]["code"] == "authentication_required"


def test_loopback_websocket_is_authenticated_automatically_when_auth_is_disabled(app_context) -> None:
    from fastapi.testclient import TestClient

    from ws_collab.server import build_app

    app_context.config.auth_disabled = True
    app = build_app(app_context, with_lifespan=False)
    with TestClient(app, client=("127.0.0.1", 50000)) as local_client:
        with local_client.websocket_connect("/ws_collab/ws") as ws:
            auth = ws.receive_json()
            assert auth["type"] == "auth_ok"
            assert auth["principal"]["label"] == "local"
            ws.send_json({"type": "subscribe", "streams": [CONVERSATION]})
            assert _drain_until(ws, "subscribed")


def test_websocket_rejects_invalid_credentials(client) -> None:
    with client.websocket_connect("/ws_collab/ws") as ws:
        ws.send_json({"type": "auth", "token": "wrong"})
        error = _drain_until(ws, "error")
    assert error and error["error"]["code"] == "authentication_required"


def test_role_is_enforced_on_websocket_publishes(client, viewer_headers) -> None:
    with client.websocket_connect("/ws_collab/ws") as ws:
        _ws_login(ws, viewer_headers["Authorization"].split()[1])
        ws.send_json({"type": "publish", "stream": CONVERSATION,
                      "event_type": "CONVERSATION_MESSAGE", "data": {"text": "nope"}})
        error = _drain_until(ws, "error")
    assert error and error["error"]["code"] == "forbidden"


# ------------------------------------------------------------------ liveness
def test_websocket_answers_liveness_pings(client, admin_headers) -> None:
    with client.websocket_connect("/ws_collab/ws") as ws:
        _ws_login(ws, admin_headers["Authorization"].split()[1])
        ws.send_json({"type": "ping"})
        assert _drain_until(ws, "pong") is not None


# ------------------------------------------------------------------- routing
def test_action_routes_are_not_captured_by_parameterised_paths(client, admin_headers) -> None:
    """`/voices/assign` must run the assign action, not create an agent named 'assign'."""

    result = client.post(f"{API_BASE}/voices/assign", headers=admin_headers, json={"policy": "unique_when_possible"}).json()
    assert "assignments" in result, "the action route must win over /voices/{agent_id}"
    profiles = client.get(f"{API_BASE}/voices", headers=admin_headers).json()["profiles"]
    assert "assign" not in {p["agent_id"] for p in profiles}


def test_websocket_resume_skips_already_delivered_events(client, admin_headers) -> None:
    for i in range(3):
        client.post(f"{API_BASE}/conversation/events", headers=admin_headers, json={"text": f"m{i}"})
    page = client.get(f"{API_BASE}/events", headers=admin_headers,
                      params={"stream": CONVERSATION, "limit": 2}).json()
    resume_cursor = page["next_cursor"]
    already = {e["id"] for e in page["events"]}

    with client.websocket_connect("/ws_collab/ws") as ws:
        _ws_login(ws, admin_headers["Authorization"].split()[1])
        ws.send_json({"type": "resume", "streams": [CONVERSATION],
                      "cursors": {CONVERSATION: resume_cursor}})
        delivered = []
        for _ in range(40):
            message = ws.receive_json()
            if message["type"] == "event":
                delivered.append(message["event"]["id"])
            if message["type"] == "caught_up":
                break
    assert not (set(delivered) & already), "resume must not redeliver acknowledged events"
