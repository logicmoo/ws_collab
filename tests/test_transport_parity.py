"""REST and WebSocket must be interchangeable.

These tests assert the parity contract itself: whatever is written through one
transport is immediately visible through the other, with the same identity,
cursor semantics, idempotency, filters, and structured errors -- and that no
essential capability is WebSocket-only.
"""

from __future__ import annotations

import json

from ws_collab.events import streams_for_role

CONVERSATION = streams_for_role("conversation")[0]
V1 = "/ws_collab/v1"


def _ws_login(ws, token: str) -> dict:
    ws.send_json({"type": "auth", "token": token})
    return ws.receive_json()


def _drain_until(ws, wanted: str, limit: int = 40) -> dict | None:
    for _ in range(limit):
        message = ws.receive_json()
        if message["type"] == wanted:
            return message
    return None


# ------------------------------------------------------------------ discovery
def test_health_is_reachable_without_credentials(client) -> None:
    assert client.get("/ws_collab/health").json()["status"] == "ok"


def test_capabilities_advertises_all_four_transports(client) -> None:
    caps = client.get(f"{V1}/capabilities").json()
    assert {"http", "https", "ws", "wss"}.issubset(set(caps["transports"]))
    assert caps["streams"], "the stream registry must be discoverable"
    assert caps["stream_roles"], "semantic roles must be discoverable"


# --------------------------------------------------------- status / readiness
def test_status_reports_every_subsystem_with_an_overall_verdict(client) -> None:
    body = client.get("/ws_collab/status").json()
    assert body["status"] in ("ok", "degraded", "down")
    assert body["subsystems"], "a rollup must name its parts"
    for name, entry in body["subsystems"].items():
        assert entry.get("state"), f"subsystem {name} must report a state"


def test_status_is_reachable_without_credentials(client) -> None:
    """Status pages and load balancers cannot authenticate."""

    assert client.get("/ws_collab/status").status_code == 200


def test_status_leaks_no_secrets(client) -> None:
    from conftest import ADMIN_TOKEN

    body = client.get("/ws_collab/status").text
    assert ADMIN_TOKEN not in body
    assert "session_secret" not in body


def test_health_and_status_answer_different_questions(client) -> None:
    health = client.get("/ws_collab/health").json()
    status = client.get("/ws_collab/status").json()
    assert "subsystems" not in health, "health is liveness only"
    assert "subsystems" in status, "status is a rollup"


def test_ready_signals_serving_state(client) -> None:
    response = client.get("/ws_collab/ready")
    assert response.status_code in (200, 503)
    body = response.json()
    assert body["ready"] is (response.status_code == 200), "the code and body must agree"


# ------------------------------------------------------------ endpoint map
def test_endpoint_map_describes_both_transports(client, admin_headers) -> None:
    body = client.get(f"{V1}/endpoints", headers=admin_headers).json()
    kinds = {e["kind"] for e in body["endpoints"]}
    assert {"http", "ws"} <= kinds, "clients must be able to find both transports"


def test_endpoint_map_urls_are_absolute_and_correctly_schemed(client, admin_headers) -> None:
    body = client.get(f"{V1}/endpoints", headers=admin_headers).json()
    for endpoint in body["endpoints"]:
        if endpoint["kind"] == "ws":
            assert endpoint["url"].startswith(("ws://", "wss://")), endpoint
        else:
            assert endpoint["url"].startswith(("http://", "https://")), endpoint


def test_endpoint_map_paths_actually_resolve(client, admin_headers) -> None:
    """Every advertised HTTP path must exist -- no map entry may be a dead link."""

    body = client.get(f"{V1}/endpoints", headers=admin_headers).json()
    for endpoint in body["endpoints"]:
        if endpoint["kind"] != "http" or endpoint["id"] == "openapi":
            continue
        response = client.get(endpoint["path"], headers=admin_headers)
        assert response.status_code != 404, f"{endpoint['path']} is advertised but missing"


def test_endpoint_map_declares_auth_requirements(client, admin_headers) -> None:
    body = client.get(f"{V1}/endpoints", headers=admin_headers).json()
    assert all(e.get("auth") in ("public", "token") for e in body["endpoints"])
    public = {e["id"] for e in body["endpoints"] if e["auth"] == "public"}
    assert {"health", "status", "ready"} <= public


# ------------------------------------------------------------ mount aliases
MOUNTS = ["", "/v1", "/ws_collab", "/ws_collab/v1"]


def test_every_mount_root_answers_identically(client, admin_headers) -> None:
    """The API is reachable at /, /v1, /ws_collab, and /ws_collab/v1."""

    for path, headers in (("/health", {}), ("/status", {}), ("/capabilities", {}),
                          ("/endpoints", admin_headers), ("/workers", admin_headers)):
        codes = {m: client.get(f"{m}{path}", headers=headers).status_code for m in MOUNTS}
        assert set(codes.values()) == {200}, f"{path} differs by mount: {codes}"


def test_websocket_is_available_at_every_mount_root(client, admin_headers) -> None:
    token = admin_headers["Authorization"].split()[1]
    for mount in MOUNTS:
        with client.websocket_connect(f"{mount}/ws") as ws:
            assert _ws_login(ws, token)["type"] == "auth_ok", f"{mount}/ws failed"


def test_a_write_through_one_mount_is_visible_through_another(client, admin_headers) -> None:
    written = client.post("/conversation/events", headers=admin_headers,
                          json={"text": "via bare mount"}).json()
    page = client.get("/ws_collab/v1/events", headers=admin_headers,
                      params={"stream": CONVERSATION}).json()
    assert any(e["id"] == written["id"] for e in page["events"]), \
        "mounts are aliases of one service, not separate instances"


def test_endpoint_map_lists_every_mount(client, admin_headers) -> None:
    body = client.get("/endpoints", headers=admin_headers).json()
    assert set(body["mounts"]) == set(MOUNTS)
    for endpoint in body["endpoints"]:
        if endpoint.get("aliases"):
            assert len(endpoint["aliases"]) == len(MOUNTS)


def test_openapi_documents_each_operation_once(client) -> None:
    """Alias mounts are hidden from the schema so the docs are not quadrupled."""

    schema = client.get("/openapi.json").json()
    documented = [p for p in schema["paths"] if p.endswith("/health")]
    assert len(documented) == 1, f"health documented {len(documented)} times: {documented}"
    assert all(p.startswith("/ws_collab") for p in schema["paths"]), \
        "only the canonical mount should be documented"


def test_interactive_docs_moved_aside_for_markdown_docs(client, admin_headers) -> None:
    assert client.get("/openapi/docs").status_code == 200
    body = client.get("/docs", headers=admin_headers).json()
    assert body["documents"], "/docs serves the markdown documentation index"


# ------------------------------------------------------------- UI at roots
HTML = {"Accept": "text/html"}


def test_browsers_get_the_workbench_at_every_root(client) -> None:
    """Opening any mount root in a browser lands on the UI, not raw JSON."""

    for mount in MOUNTS:
        for path in (mount or "/", f"{mount}/"):
            response = client.get(path, headers=HTML)
            assert response.status_code == 200, f"{path} -> {response.status_code}"
            assert "html" in response.headers["content-type"], f"{path} did not serve the UI"


def test_api_clients_still_get_json_at_every_root(client) -> None:
    """Content negotiation must not break machine callers."""

    for mount in MOUNTS:
        body = client.get(mount or "/").json()
        assert body["status"] == "ok", f"{mount or '/'} did not return health"


def test_health_path_is_always_json_even_for_browsers(client) -> None:
    for mount in MOUNTS:
        response = client.get(f"{mount}/health", headers=HTML)
        assert response.json()["status"] == "ok", "an explicit /health must never return HTML"


def test_ui_assets_resolve_beside_every_root(client) -> None:
    """Relative asset URLs in the page must resolve wherever it is served."""

    for mount in MOUNTS:
        for asset in ("/app.css", "/app.js"):
            assert client.get(f"{mount}{asset}").status_code == 200, f"{mount}{asset} missing"


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
            assert guarded_client.get("/", headers=HTML).status_code == 403
    finally:
        store.close()


# ------------------------------------------------------------------- docs
def test_docs_are_listed_and_readable(client, admin_headers) -> None:
    listing = client.get("/ws_collab/docs", headers=admin_headers).json()
    assert listing["documents"], "shipped documentation must be discoverable"
    name = listing["documents"][0]["name"]
    body = client.get(f"/ws_collab/docs/{name}", headers=admin_headers)
    assert body.status_code == 200 and body.text.strip()


def test_docs_refuse_non_markdown_and_traversal(client, admin_headers) -> None:
    assert client.get("/ws_collab/docs/../config.py", headers=admin_headers).status_code in (400, 404)
    assert client.get("/ws_collab/docs/secrets.env", headers=admin_headers).status_code == 400


def test_docs_require_authentication(client) -> None:
    assert client.get("/ws_collab/docs").status_code == 401


# ---------------------------------------------------------------- ui links
def test_ui_links_cover_every_workbench_page(client, admin_headers) -> None:
    body = client.get(f"{V1}/ui/links", headers=admin_headers).json()
    assert body["links"], "the UI must be navigable programmatically"
    assert all(link["url"].startswith("http") and "#" in link["url"] for link in body["links"])
    assert all(link["title"] and link["description"] for link in body["links"])


# ------------------------------------------------------------------- files
def test_files_lists_the_state_directory(client, admin_headers) -> None:
    body = client.get(f"{V1}/files", headers=admin_headers).json()
    assert body["entries"], "the writable state directory must be inspectable"
    assert all({"name", "kind", "readable", "protected"} <= set(e) for e in body["entries"])


def test_files_never_serve_the_generated_token(client, admin_headers, app_context) -> None:
    """The state directory holds credentials; listing them is fine, reading is not."""

    token_path = app_context.config.generated_token_path
    token_path.write_text("super-secret-value\n", encoding="utf-8")

    listing = client.get(f"{V1}/files", headers=admin_headers).json()
    entry = next((e for e in listing["entries"] if e["name"] == token_path.name), None)
    assert entry is not None, "operators should know the file exists"
    assert entry["protected"] is True and entry["readable"] is False

    response = client.get(f"{V1}/files/content", headers=admin_headers,
                          params={"path": token_path.name})
    assert response.status_code == 403
    assert "super-secret-value" not in response.text


def test_files_reject_path_traversal(client, admin_headers) -> None:
    for attempt in ("../config.py", "../../etc/passwd", "sessions/../../secret"):
        response = client.get(f"{V1}/files/content", headers=admin_headers, params={"path": attempt})
        assert response.status_code in (400, 403, 404), f"{attempt} must not escape the state root"


def test_files_read_is_bounded(client, admin_headers, app_context) -> None:
    big = app_context.config.state_dir / "big.log"
    big.write_text("x" * (400 * 1024), encoding="utf-8")
    body = client.get(f"{V1}/files/content", headers=admin_headers, params={"path": "big.log"}).json()
    assert body["truncated"] is True
    assert len(body["content"]) < 400 * 1024, "a huge file must not be returned whole"


def test_files_require_operator_role(client, worker_headers, viewer_headers) -> None:
    assert client.get(f"{V1}/files", headers=viewer_headers).status_code == 403
    assert client.get(f"{V1}/files", headers=worker_headers).status_code == 403


def test_unauthenticated_reads_are_refused(client) -> None:
    response = client.get(f"{V1}/events", params={"stream": CONVERSATION})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_required"


# ------------------------------------------------------------- cross-transport
def test_event_written_over_rest_is_visible_over_websocket(client, admin_headers) -> None:
    written = client.post(
        f"{V1}/conversation/events", headers=admin_headers, json={"text": "from rest"}
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
    page = client.get(f"{V1}/events", headers=admin_headers, params={"stream": CONVERSATION}).json()
    assert any(e["id"] == ack["id"] for e in page["events"]), "REST must see WS writes"


def test_identity_and_cursor_semantics_match_across_transports(client, admin_headers) -> None:
    rest = client.post(f"{V1}/conversation/events", headers=admin_headers, json={"text": "a"}).json()
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
    first = client.post(f"{V1}/conversation/events", headers=headers, json={"text": "once"}).json()
    second = client.post(f"{V1}/conversation/events", headers=headers, json={"text": "once"}).json()
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
        client.post(f"{V1}/conversation/events", headers=admin_headers, json={"text": f"m{i}"})
    page = client.get(f"{V1}/events", headers=admin_headers,
                      params={"stream": CONVERSATION, "limit": 2}).json()
    assert len(page["events"]) == 2 and page["has_more"] is True
    assert page["next_cursor"] and page["server_time"]


def test_conditional_request_avoids_resending_an_empty_page(client, admin_headers) -> None:
    client.post(f"{V1}/conversation/events", headers=admin_headers, json={"text": "x"})
    page = client.get(f"{V1}/events", headers=admin_headers, params={"stream": CONVERSATION}).json()
    cursor = page["next_cursor"]
    response = client.get(f"{V1}/events", headers={**admin_headers, "If-None-Match": cursor},
                          params={"stream": CONVERSATION, "after": cursor})
    assert response.status_code == 304


def test_long_polling_returns_promptly_when_idle(client, admin_headers) -> None:
    page = client.get(f"{V1}/events", headers=admin_headers,
                      params={"stream": CONVERSATION, "wait_ms": 150}).json()
    assert page["events"] == [] and page["next_cursor"]


def test_filters_apply_identically_through_rest(client, admin_headers) -> None:
    client.post(f"{V1}/conversation/events", headers=admin_headers, json={"text": "keep me"})
    client.post(f"{V1}/events", headers=admin_headers,
                json={"stream": CONVERSATION, "type": "NOISE", "data": {"text": "ignore"}})
    page = client.get(f"{V1}/events", headers=admin_headers,
                      params={"stream": CONVERSATION, "type": "NOISE"}).json()
    assert all(e["type"] == "NOISE" for e in page["events"]) and page["events"]


# --------------------------------------------------------------------- errors
def test_unknown_stream_is_a_structured_error_on_both_transports(client, admin_headers) -> None:
    rest = client.get(f"{V1}/events", headers=admin_headers, params={"stream": "nope"})
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

    result = client.post(f"{V1}/voices/assign", headers=admin_headers, json={"policy": "unique_when_possible"}).json()
    assert "assignments" in result, "the action route must win over /voices/{agent_id}"
    profiles = client.get(f"{V1}/voices", headers=admin_headers).json()["profiles"]
    assert "assign" not in {p["agent_id"] for p in profiles}


def test_websocket_resume_skips_already_delivered_events(client, admin_headers) -> None:
    for i in range(3):
        client.post(f"{V1}/conversation/events", headers=admin_headers, json={"text": f"m{i}"})
    page = client.get(f"{V1}/events", headers=admin_headers,
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
