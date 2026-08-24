"""Workbench-plugin adapter.

Exposes :func:`create_router` returning an ``APIRouter`` that mounts the full
WS_COLLAB REST + WebSocket surface under the manifest's ``routePrefix`` (default
``/ws_collab``). The shared service is started lazily on the first request, so it
works when included into the running workbench app without a dedicated lifespan.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from .config import Config
from .context import build_context
from .rest import create_rest_router
from .ws import create_ws_router

_context = None


def create_router(manifest: dict[str, Any]) -> APIRouter:
    global _context
    prefix = str(manifest.get("routePrefix") or "/ws_collab").rstrip("/")
    if _context is None:
        _context = build_context(Config.from_env())
    router = APIRouter()
    router.include_router(create_rest_router(_context, prefix))
    router.include_router(create_ws_router(_context, prefix))
    return router
