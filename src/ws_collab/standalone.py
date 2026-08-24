"""Standalone runner: run WS_COLLAB as its own OS process.

Two entry points:

* ``python -m ws_collab.standalone [host] [http_port] [https_port]`` runs the
  server in the foreground (delegates to :func:`ws_collab.server.main`).
* :func:`launch` spawns the server as a *detached* background process. It is
  idempotent: if something is already serving the target port it does nothing.
  The workbench plugin uses this in "standalone" mode and reaches the server
  through its ``web_proxy`` (see ``plugin.json``).

Contrast with :mod:`ws_collab.embedded`, which mounts the service in-process.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# .../ws_collab (package dir) and its parent (import root for ``ws_collab``).
_PACKAGE_DIR = Path(__file__).resolve().parent
_IMPORT_ROOT = _PACKAGE_DIR.parent

DEFAULT_HOST = os.environ.get("WS_COLLAB_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("WS_COLLAB_HTTP_PORT", "8802"))

# Windows process-creation flags (no dependency on the ``subprocess`` constants,
# which are only defined on Windows): detach from the parent console and start a
# new process group so the child survives the host exiting.
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200


def _probe_host(host: str) -> str:
    """A connectable address for a bind host (wildcards map to loopback)."""

    if host in ("0.0.0.0", "::", ""):
        return "127.0.0.1"
    return host


def is_listening(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, timeout: float = 0.5) -> bool:
    """Return True when a TCP connection to ``host:port`` succeeds."""

    try:
        with socket.create_connection((_probe_host(host), port), timeout=timeout):
            return True
    except OSError:
        return False


def main(argv: list[str] | None = None) -> None:
    """Run the standalone server in the foreground."""

    from ws_collab.server import main as server_main

    server_main(argv)


def launch(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    wait: bool = True,
    timeout: float = 20.0,
) -> subprocess.Popen | None:
    """Spawn the standalone server as a detached background process.

    Idempotent: returns ``None`` immediately if ``host:port`` is already serving.
    Otherwise starts ``python -m ws_collab.standalone host port`` detached, with
    stdout/stderr redirected to ``<state_dir>/standalone.log``. When ``wait`` is
    true, blocks until the port accepts connections (or raises on failure).
    """

    if is_listening(host, port):
        return None

    env = os.environ.copy()
    # The child must import ``ws_collab`` regardless of its cwd.
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(_IMPORT_ROOT), existing) if p)

    state_dir = Path(env.get("WS_COLLAB_STATE_DIR") or (_IMPORT_ROOT / "collab_state"))
    state_dir.mkdir(parents=True, exist_ok=True)
    env["WS_COLLAB_STATE_DIR"] = str(state_dir)
    log_path = state_dir / "standalone.log"
    log = open(log_path, "ab", buffering=0)  # noqa: SIM115 - handed to the child

    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        kwargs["creationflags"] = _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True

    proc = subprocess.Popen(
        [sys.executable, "-m", "ws_collab.standalone", host, str(port)],
        cwd=str(_IMPORT_ROOT),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=log,
        close_fds=True,
        **kwargs,
    )

    if not wait:
        return proc

    deadline = time.time() + timeout
    while time.time() < deadline:
        if is_listening(host, port):
            return proc
        if proc.poll() is not None:
            raise RuntimeError(
                f"ws_collab standalone exited (code {proc.returncode}) during "
                f"startup; see {log_path}"
            )
        time.sleep(0.25)
    raise TimeoutError(
        f"ws_collab standalone did not start listening on {host}:{port} within "
        f"{timeout:.0f}s; see {log_path}"
    )


def create_router(manifest: dict[str, Any] | None = None):
    """Plugin hook for standalone mode.

    Ensures the standalone server is running, then returns an empty router. The
    workbench serves ``/ws_collab`` by proxying to the standalone process (see
    the ``web_proxy`` entry in ``plugin.json``), so this router intentionally
    contributes no in-process routes.
    """

    launch(DEFAULT_HOST, DEFAULT_PORT)
    try:
        from fastapi import APIRouter
    except Exception:  # pragma: no cover - FastAPI always present in practice
        return None
    return APIRouter()


if __name__ == "__main__":
    main()
