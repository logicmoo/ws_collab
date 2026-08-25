"""Shared pytest fixtures for the WS_COLLAB suite.

Every fixture uses hardware-free backends and explicitly configured tokens, so
the tests exercise the real security, storage, and pipeline code paths without
paid APIs, production credentials, or physical devices.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ws_collab.config import Config  # noqa: E402
from ws_collab.jsonl_store import JsonlStore  # noqa: E402
from ws_collab.service import WsCollabService  # noqa: E402

ADMIN_TOKEN = "test-admin-token"
WORKER_TOKEN = "test-worker-token"
VIEWER_TOKEN = "test-viewer-token"


def make_event_store(config: Config, **overrides):
    """Construct the durable event store for tests.

    This is the ONLY place the concrete storage technology is named. Tests depend
    on the ``event_store`` role, so replacing the backend (JSONL today, something
    else tomorrow) means editing this factory rather than every test.
    """

    return JsonlStore(
        config.jsonl_dir,
        rotate_max_bytes=overrides.get("rotate_max_bytes", config.rotate_max_bytes),
        retention_max_files=overrides.get("retention_max_files", config.retention_max_files),
    )


def make_config(tmp_path: Path, **overrides: str) -> Config:
    """Build a hermetic test configuration.

    Production now defaults to *real* hardware (auto-detected devices, platform
    voices, real STT models). Tests must never depend on what happens to be
    installed on the machine, so the doubles are pinned explicitly here. Any test
    that wants real backends must opt in by overriding these.
    """

    env = {
        "WS_COLLAB_STATE_DIR": str(tmp_path / "collab_state"),
        "WS_COLLAB_TOKENS": f"{ADMIN_TOKEN}=admin,{WORKER_TOKEN}=worker,{VIEWER_TOKEN}=viewer",
        "WS_COLLAB_SESSION_SECRET": "unit-test-session-secret",
        # Tests exercise the real auth mechanism; the product default is auth-off.
        "WS_COLLAB_AUTH_DISABLED": "0",
        "WS_COLLAB_AUDIO_ENABLED": "1",
        "WS_COLLAB_HOST": "127.0.0.1",
        # Hermetic backends: no hardware, no models, no network.
        "WS_COLLAB_AUDIO_BACKEND": "fake",
        "WS_COLLAB_TTS_BACKEND": "fake",
        "WS_COLLAB_STT_ENGINES": "fallback_alpha,fallback_beta,fallback_gamma",
        "WS_COLLAB_STT_TIMEOUT_MS": "5000",
    }
    env.update(overrides)
    config = Config.from_env(env)
    config.prepare_state_dir()
    return config


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return make_config(tmp_path)


@pytest.fixture
def event_store(config: Config):
    """The durable event store under test (technology-agnostic role)."""

    store = make_event_store(config)
    yield store
    store.close()


@pytest.fixture
def store(event_store):
    """Backwards-compatible alias for :func:`event_store`."""

    return event_store


@pytest.fixture
def service(config: Config, event_store) -> WsCollabService:
    return WsCollabService(config, event_store)


@pytest.fixture
def app_context(tmp_path: Path):
    """A fully wired context whose admin page is reachable from the test client."""

    from ws_collab.context import AppContext
    from ws_collab.security import Security

    config = make_config(tmp_path, WS_COLLAB_ADMIN_REMOTE="1", WS_COLLAB_DEV_INSECURE="1")
    store = make_event_store(config)
    service = WsCollabService(config, store)
    security = Security(config, audit_sink=service._audit_sink)
    context = AppContext(config=config, store=store, service=service, security=security)
    yield context
    store.close()


@pytest.fixture
def client(app_context):
    from fastapi.testclient import TestClient

    from ws_collab.server import build_app

    app = build_app(app_context, with_lifespan=False)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def admin_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {ADMIN_TOKEN}"}


@pytest.fixture
def worker_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {WORKER_TOKEN}"}


@pytest.fixture
def viewer_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {VIEWER_TOKEN}"}
