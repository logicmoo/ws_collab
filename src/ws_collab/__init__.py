"""WS_COLLAB coordination, audio, transcription, and administration infrastructure.

This package implements a provider-neutral coordination server for Codex/Copilot
workers built on durable JSONL event streams. Every essential capability is
reachable identically through REST (HTTP/HTTPS) and WebSocket (WS/WSS) because
both transports share a single service layer.

The package works in two deployment shapes:

* As a workbench plugin: ``plugin.py`` at the repository root imports
  :func:`ws_collab.plugin_router.create_router` and mounts every route under
  ``/ws_collab``.
* As a standalone server: ``python -m ws_collab.server`` binds HTTP/HTTPS/WS/WSS
  sockets directly and prints a startup report.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "1.0.0"
