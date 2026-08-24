"""Shared application context binding config, store, service, and security.

Both the REST and WebSocket routers use the same :class:`AppContext`, which is
what makes cross-transport parity structural rather than accidental. The context
also owns one-time async startup so it works whether the app is a standalone
server (via lifespan) or mounted as a workbench plugin (started lazily on the
first request).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional

from .config import Config
from .jsonl_store import JsonlStore
from .security import Security
from .service import WsCollabService


@dataclass
class AppContext:
    config: Config
    store: JsonlStore
    service: WsCollabService
    security: Security

    def __post_init__(self) -> None:
        self._started = False
        self._start_lock = asyncio.Lock()

    async def ensure_started(self) -> None:
        if self._started:
            return
        async with self._start_lock:
            if self._started:
                return
            await self.service.startup()
            self._started = True

    async def aclose(self) -> None:
        if self._started:
            await self.service.shutdown()
            self._started = False
        self.store.close()


def build_context(config: Optional[Config] = None) -> AppContext:
    config = config or Config.from_env()
    config.prepare_state_dir()
    store = JsonlStore(
        config.jsonl_dir,
        rotate_max_bytes=config.rotate_max_bytes,
        retention_max_files=config.retention_max_files,
    )
    service = WsCollabService(config, store)
    security = Security(config, audit_sink=service._audit_sink)
    return AppContext(config=config, store=store, service=service, security=security)
