from __future__ import annotations

import os
from threading import Event
import asyncio
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

from conftest import ADMIN_TOKEN
from ws_collab.lifecycle import LifecycleController
from ws_collab.server import RESTART_EXIT_CODE, build_app


class ObserveFlush:
    def __init__(self, app, flushed: Event):
        self.app = app
        self.flushed = flushed

    async def __call__(self, scope, receive, send):
        async def observe(message):
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                self.flushed.set()

        await self.app(scope, receive, observe)


@pytest.fixture
def lifecycle_client(app_context):
    flushed = Event()
    calls: list[str] = []

    async def shutdown():
        assert flushed.is_set()
        calls.append("shutdown")

    async def restart():
        assert flushed.is_set()
        calls.append("restart")

    app_context.lifecycle = LifecycleController(shutdown=shutdown, restart=restart)
    app = ObserveFlush(build_app(app_context, with_lifespan=False), flushed)
    with TestClient(app) as client:
        yield client, calls, flushed


def test_shutdown_acknowledges_after_flush_and_invokes_once(
    lifecycle_client, admin_headers
) -> None:
    client, calls, flushed = lifecycle_client
    response = client.post("/ws_collab/admin/shutdown", headers=admin_headers)
    assert response.status_code == 202
    assert response.json() == {
        "action": "shutdown",
        "pid": os.getpid(),
        "scheduled": True,
        "accepted": True,
        "status": "scheduled",
        "boot_id": response.json()["boot_id"],
    }
    assert flushed.is_set()
    assert calls == ["shutdown"]

    duplicate = client.post("/ws_collab/admin/shutdown", headers=admin_headers)
    assert duplicate.status_code == 202
    assert duplicate.json()["status"] == "already-scheduled"
    assert duplicate.json()["accepted"] is False
    assert calls == ["shutdown"]

    conflict = client.post("/ws_collab/admin/restart", headers=admin_headers)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["details"]["pending"] == "shutdown"


def test_restart_acknowledges_and_invokes_callback_once(
    lifecycle_client, admin_headers
) -> None:
    client, calls, _ = lifecycle_client
    response = client.post("/ws_collab/admin/restart", headers=admin_headers)
    assert response.status_code == 202
    assert response.json()["action"] == "restart"
    assert response.json()["scheduled"] is True
    assert calls == ["restart"]


def test_lifecycle_routes_are_post_only_and_old_aliases_are_absent(
    lifecycle_client, admin_headers
) -> None:
    client, _, _ = lifecycle_client
    assert client.get("/ws_collab/admin/shutdown", headers=admin_headers).status_code == 405
    assert client.get("/ws_collab/admin/restart", headers=admin_headers).status_code == 405
    old_paths = (
        "/shutdown",
        "/restart",
        "/ws_collab/shutdown",
        "/ws_collab/restart",
    )
    assert all(client.post(path, headers=admin_headers).status_code == 404 for path in old_paths)


def test_lifecycle_requires_operator(
    lifecycle_client, worker_headers, viewer_headers
) -> None:
    client, calls, _ = lifecycle_client
    for headers in ({}, worker_headers, viewer_headers):
        response = client.post("/ws_collab/admin/shutdown", headers=headers)
        assert response.status_code in ({401} if not headers else {403})
    assert calls == []


def test_lifecycle_cookie_session_requires_csrf_and_trusted_origin(
    lifecycle_client, app_context
) -> None:
    client, calls, _ = lifecycle_client
    app_context.config.trusted_origins = ["http://trusted.example"]
    login = client.post("/ws_collab/auth/login", json={"token": ADMIN_TOKEN})
    csrf = login.json()["csrf"]

    assert client.post("/ws_collab/admin/shutdown").status_code == 403
    assert client.post(
        "/ws_collab/admin/shutdown",
        headers={"X-WS-Collab-CSRF": csrf, "Origin": "http://untrusted.example"},
    ).status_code == 403
    response = client.post(
        "/ws_collab/admin/shutdown",
        headers={"X-WS-Collab-CSRF": csrf, "Origin": "http://trusted.example"},
    )
    assert response.status_code == 202
    assert calls == ["shutdown"]


def test_embedded_host_reports_lifecycle_unavailable(client, admin_headers) -> None:
    for action in ("shutdown", "restart"):
        response = client.post(f"/ws_collab/admin/{action}", headers=admin_headers)
        assert response.status_code == 409
        assert response.json()["error"]["message"] == f"{action} unavailable in embedded host"


def test_custom_plugin_prefix_composes_lifecycle_paths(app_context, monkeypatch) -> None:
    from ws_collab import plugin_router
    from fastapi import FastAPI

    monkeypatch.setattr(plugin_router, "_context", app_context)
    router = plugin_router.create_router({"routePrefix": "/custom/"})
    paths: set[str] = set()

    def collect(current) -> None:
        for route in current.routes:
            if path := getattr(route, "path", ""):
                paths.add(path)
            if included := getattr(route, "original_router", None):
                collect(included)

    collect(router)
    assert "/custom/admin/shutdown" in paths
    assert "/custom/admin/restart" in paths
    assert "/custom/custom/admin/shutdown" not in paths

    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as custom_client:
        assert custom_client.post(
            "/custom/admin/shutdown",
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        ).status_code == 409
        assert custom_client.post(
            "/custom/admin/restart",
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        ).status_code == 409
        assert custom_client.post(
            "/custom/shutdown",
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        ).status_code == 404


def test_standalone_supervisor_waits_for_release_before_rebind(monkeypatch) -> None:
    from ws_collab import standalone

    events: list[object] = []
    commands = []
    monkeypatch.setenv("WS_COLLAB_TEST_SENTINEL", "preserved")

    class Child:
        def __init__(self, command, *, env):
            if commands:
                assert events[-1] == "released-port-and-locks"
            commands.append(command)
            assert env["WS_COLLAB_TEST_SENTINEL"] == "preserved"
            assert env["WS_COLLAB_STATE_DIR"]

        def wait(self):
            events.append("waited")
            events.append("released-port-and-locks")
            return RESTART_EXIT_CODE if len(commands) == 1 else 0

    monkeypatch.setattr(standalone.subprocess, "Popen", Child)
    assert standalone.main(["127.0.0.1", "8802"]) == 0
    assert commands == [[standalone.sys.executable, "-u", "-m", "ws_collab.server", "127.0.0.1", "8802"]] * 2


def test_server_bounds_connection_drain_before_releasing_context(config, monkeypatch) -> None:
    from ws_collab import server
    import uvicorn

    context = Mock(ensure_started=AsyncMock(), aclose=AsyncMock())
    monkeypatch.setattr(server, "build_context", lambda _: context)
    monkeypatch.setattr(server, "build_app", lambda *a, **kw: object())
    monkeypatch.setattr(server, "build_startup_report", lambda *a: "test")
    configurations = []

    class FakeServer:
        def __init__(self, config):
            configurations.append(config)
            self.started = False

        async def serve(self):
            self.started = True

    monkeypatch.setattr(uvicorn, "Server", FakeServer)
    assert asyncio.run(server._serve(config)) is False
    assert configurations[0].timeout_graceful_shutdown == 10.0
    context.aclose.assert_awaited_once()


def test_inventory_describes_admin_controls(client, admin_headers) -> None:
    body = client.get("/ws_collab/endpoints", headers=admin_headers).json()
    entries = body["categories"]["admin-control"]["endpoints"]
    assert {entry["path"] for entry in entries} == {
        "/ws_collab/admin/shutdown",
        "/ws_collab/admin/restart",
    }
    assert all(entry["methods"] == ["POST"] for entry in entries)
    assert all(entry["capability"] == "operator" for entry in entries)
    assert all(entry["auth"] == "bearer-or-session" for entry in entries)
    assert all("acknowledgement is flushed" in entry["description"] for entry in entries)
    assert all(entry["origin"] for entry in entries)


def test_admin_ui_lifecycle_controls_are_confirmed_and_prefix_aware() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "src" / "ws_collab" / "admin"
    html = (root / "index.html").read_text(encoding="utf-8")
    source = (root / "app.js").read_text(encoding="utf-8")
    assert 'id="sy-shutdown"' in html and 'class="danger"' in html
    assert 'id="sy-restart"' in html and 'class="secondary"' in html
    assert 'id="server-restart"' in html and 'id="server-shutdown"' in html
    assert 'role="status"' in html and 'aria-live="polite"' in html
    assert "if (!confirm(warning)) return" in source
    assert "`${API_BASE}/admin/${action}`" in source
    assert "`${API_BASE}/status`" in source
    assert "location.reload()" in source
    assert "requestLifecycle(" not in source.split("function requestLifecycle", 1)[0]
