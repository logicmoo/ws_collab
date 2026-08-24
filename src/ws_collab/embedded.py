"""Embedded runner: run WS_COLLAB *inside* the host (workbench) process.

In embedded mode the service shares the host's event loop and is mounted directly
into the host application. ``create_router(manifest)`` returns the FastAPI router
that the workbench mounts under the manifest's ``routePrefix`` (default
``/ws_collab``). No separate OS process and no proxy are involved.

Contrast with :mod:`ws_collab.standalone`, which runs the service as its own
process reached through the workbench ``web_proxy``.
"""

from __future__ import annotations

from typing import Any


def create_router(manifest: dict[str, Any] | None = None):
    """Return the in-process router the host mounts directly.

    The workbench plugin loader passes the plugin manifest (used for
    ``routePrefix``). When called without one (e.g. from a test) we fall back to
    an empty manifest so the default prefix applies.
    """

    from ws_collab.plugin_router import create_router as _create_router

    return _create_router(manifest or {})


def main(argv: list[str] | None = None) -> None:
    """Convenience: serve the same wiring in *this* process (foreground).

    This is handy for local runs/tests; the workbench itself consumes
    :func:`create_router` rather than calling ``main``.
    """

    from ws_collab.server import main as server_main

    server_main(argv)


if __name__ == "__main__":
    main()
