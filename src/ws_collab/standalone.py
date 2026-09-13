"""Detached startup, foreground supervision, and CLI lifecycle controls.

``python -m ws_collab.standalone start|status|restart|shutdown`` controls the
standalone service. Legacy ``[host] [http_port] [https_port]`` arguments still
run it in the foreground. Restarts always use a fresh server interpreter.
"""

from __future__ import annotations

import argparse
import errno
import json
import math
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .lifecycle import RESTART_EXIT_CODE

# .../ws_collab (package dir) and its parent (import root for ``ws_collab``).
_PACKAGE_DIR = Path(__file__).resolve().parent
_IMPORT_ROOT = _PACKAGE_DIR.parent
_PROJECT_ROOT = _IMPORT_ROOT.parent if (_IMPORT_ROOT.parent / "plugin.json").is_file() else Path.cwd()

DEFAULT_HOST = os.environ.get("WS_COLLAB_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("WS_COLLAB_HTTP_PORT", "8802"))

# Windows process-creation flags (no dependency on the ``subprocess`` constants,
# which are only defined on Windows): detach from the parent console and start a
# new process group so the child survives the host exiting.
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200


def _probe_host(host: str) -> str:
    """A connectable address for a bind host (wildcards map to loopback)."""

    if host in ("0.0.0.0", ""):
        return "127.0.0.1"
    if host == "::":
        return "::1"
    return host


def is_listening(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, timeout: float = 0.5) -> bool:
    """Return True when a TCP connection to ``host:port`` succeeds."""

    try:
        with socket.create_connection((_probe_host(host), port), timeout=timeout):
            return True
    except OSError:
        return False


def _base_url(host: str, port: int) -> str:
    if not host or any(c.isspace() or c in "/\\@?#" for c in host):
        raise ValueError("host must be a hostname or unbracketed IP address")
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    address = _probe_host(host)
    return f"http://{'[' + address + ']' if ':' in address else address}:{port}/ws_collab"


def _deadline(timeout: float) -> float:
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be a positive, finite number of seconds")
    return time.monotonic() + timeout


def _state_dir(value: str | Path | None = None) -> Path:
    return Path(value or os.environ.get("WS_COLLAB_STATE_DIR") or (_PROJECT_ROOT / "collab_state")).resolve()


def _child_environment(state_dir: str | Path | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (str(_IMPORT_ROOT), env.get("PYTHONPATH", "")) if p
    )
    env["WS_COLLAB_STATE_DIR"] = str(_state_dir(state_dir))
    return env


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _read_json(url: str, *, timeout: float, token: str = "", post: bool = False) -> dict[str, Any]:
    headers = {"Accept": "application/json"}
    if token:
        if not token.isascii() or any(ord(char) < 33 or ord(char) > 126 for char in token):
            raise ValueError("Operator token must be printable ASCII without whitespace")
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, data=b"" if post else None, headers=headers)
    # Local controls must not send credentials through a proxy or a redirect.
    with build_opener(ProxyHandler({}), _NoRedirects()).open(request, timeout=timeout) as response:
        data = response.read(1024 * 1024 + 1)
    if len(data) > 1024 * 1024:
        raise RuntimeError(f"Oversized response from {url}")
    try:
        body = json.loads(data)
    except (ValueError, UnicodeError) as exc:
        raise RuntimeError(f"Expected JSON from {url}") from exc
    if not isinstance(body, dict):
        raise RuntimeError(f"Expected a JSON object from {url}")
    return body


def get_status(
    host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, *, timeout: float = 2.0
) -> dict[str, Any] | None:
    """Read WS_COLLAB status; only a refused connection means stopped."""
    url = _base_url(host, port)
    _deadline(timeout)
    try:
        body = _read_json(f"{url}/status", timeout=timeout)
    except URLError as exc:
        if getattr(exc.reason, "errno", None) in {errno.ECONNREFUSED, 10061}:
            return None
        raise
    subsystems = body.get("subsystems")
    if (
        not isinstance(body.get("boot_id"), str) or not body["boot_id"]
        or body.get("status") not in {"ok", "degraded", "down"}
        or not isinstance(subsystems, dict)
        or not {"event_store", "transports"} <= subsystems.keys()
    ):
        raise RuntimeError(f"{url} did not return WS_COLLAB status; refusing to control or replace it")
    return body


def _supervise(argv: list[str], *, state_dir: str | Path | None = None) -> int:
    """Wait for all child teardown before starting another interpreter."""
    env = _child_environment(state_dir)
    while True:
        command = [sys.executable, "-u", "-m", "ws_collab.server", *argv]
        child = subprocess.Popen(command, env=env)
        try:
            exit_code = child.wait()
        except KeyboardInterrupt:
            # The console signal reaches the child too. Further interrupts escalate.
            _stop_child(child)
            return 130
        if exit_code != RESTART_EXIT_CODE:
            return exit_code


def _stop_child(child: subprocess.Popen) -> None:
    for action in (None, child.terminate, child.kill):
        if action:
            action()
        try:
            child.wait(timeout=5 if action else 10)
            return
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            continue
    raise RuntimeError(f"Owned server pid {child.pid} did not exit after termination; check its process state")


def launch(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    wait: bool = True,
    timeout: float = 20.0,
    state_dir: str | Path | None = None,
    python_executable: str | Path | None = None,
) -> subprocess.Popen | None:
    """Spawn the standalone server as a detached background process.

    Idempotent: returns ``None`` if this port already serves ready WS_COLLAB.
    Otherwise starts ``python -m ws_collab.standalone host port`` detached, with
    stdout/stderr redirected to ``<state_dir>/standalone.log``. When ``wait`` is
    true, waits for HTTP readiness, not merely an open socket. A startup timeout
    reports the owned supervisor PID/log without killing a possibly starting server.
    """

    deadline = _deadline(timeout)
    _base_url(host, port)
    if is_listening(host, port, timeout=min(0.5, timeout)):
        status = get_status(host, port, timeout=min(2.0, timeout))
        if status and status["status"] != "down":
            return None
        raise RuntimeError(f"{host}:{port} is occupied but not ready; no process was started")

    env = _child_environment(state_dir)
    directory = Path(env["WS_COLLAB_STATE_DIR"])
    directory.mkdir(parents=True, exist_ok=True)
    log_path = directory / "standalone.log"

    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        kwargs["creationflags"] = _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True

    with log_path.open("ab", buffering=0) as log:
        proc = subprocess.Popen(
            [str(python_executable or sys.executable), "-u", "-m", "ws_collab.standalone", host, str(port)],
            cwd=str(_PROJECT_ROOT),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            close_fds=True,
            **kwargs,
        )

    if not wait:
        return proc

    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"ws_collab standalone exited (code {proc.returncode}) during "
                f"startup; see {log_path}"
            )
        status = _poll_status(host, port, deadline)
        if status and status["status"] != "down":
            return proc
        time.sleep(min(0.25, max(0, deadline - time.monotonic())))
    raise TimeoutError(
        f"WS_COLLAB did not become ready on {host}:{port} within {timeout:g}s; "
        f"supervisor pid {proc.pid} may still be starting; see {log_path}"
    )


def _poll_status(host: str, port: int, deadline: float) -> dict[str, Any] | None:
    try:
        return get_status(host, port, timeout=max(0.001, min(1.0, deadline - time.monotonic())))
    except HTTPError as exc:
        if exc.code != 503:
            raise
    except (URLError, TimeoutError, ConnectionError):
        # Connection loss is expected during a bounded startup/restart wait.
        pass
    return None


def start_server(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    state_dir: str | Path | None = None,
    timeout: float = 20.0,
    python_executable: str | Path | None = None,
) -> dict[str, Any]:
    """Host-side start API, callable even when the HTTP service is shut down."""
    proc = launch(host, port, state_dir=state_dir, timeout=timeout, python_executable=python_executable)
    status = get_status(host, port, timeout=min(2.0, timeout))
    if not status or status["status"] == "down":
        raise RuntimeError("WS_COLLAB lost readiness immediately after startup")
    return {
        "status": "started" if proc else "already-running",
        "url": _base_url(host, port) + "/",
        "boot_id": status["boot_id"],
        "supervisor_pid": proc.pid if proc else None,
    }


def control_server(
    action: str,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    timeout: float = 60.0,
    state_dir: str | Path | None = None,
    token: str | None = None,
) -> dict[str, Any]:
    """Use the operator-only HTTP API, then wait for a new boot or a closed port."""
    if action not in {"restart", "shutdown"}:
        raise ValueError("action must be restart or shutdown")
    deadline = _deadline(timeout)
    url = _base_url(host, port)
    status = get_status(host, port, timeout=min(2.0, timeout))
    if status is None:
        if action == "shutdown":
            return {"status": "already-stopped", "url": url + "/"}
        raise RuntimeError("WS_COLLAB is stopped; use the start command")
    if token is None:
        token = os.environ.get("WS_COLLAB_TOKEN") or os.environ.get("WS_COLLAB_ADMIN_TOKEN") or ""
        token_path = _state_dir(state_dir) / "generated_admin_token.txt"
        if not token and token_path.is_file():
            token = token_path.read_text(encoding="utf-8").strip()
    acknowledgement = _read_json(
        f"{url}/admin/{action}", post=True, token=token,
        timeout=max(0.001, min(5.0, deadline - time.monotonic())),
    )
    if acknowledgement.get("action") != action or acknowledgement.get("scheduled") is not True:
        raise RuntimeError(f"WS_COLLAB did not acknowledge {action}")
    previous_boot = acknowledgement.get("boot_id") or status["boot_id"]
    while time.monotonic() < deadline:
        if action == "shutdown":
            if not is_listening(host, port, timeout=min(0.5, max(0.001, deadline - time.monotonic()))):
                return {"status": "stopped", "url": url + "/", "pid": acknowledgement.get("pid")}
        else:
            current = _poll_status(host, port, deadline)
            if current and current["status"] != "down" and current["boot_id"] != previous_boot:
                return {"status": "restarted", "url": url + "/", "boot_id": current["boot_id"]}
        time.sleep(min(0.25, max(0, deadline - time.monotonic())))
    raise TimeoutError(f"{action} was accepted but not completed within {timeout:g}s; check the launcher/log")


def main(argv: list[str] | None = None) -> int:
    """CLI control commands, retaining the original foreground positional form."""
    args = list(sys.argv[1:] if argv is None else argv)
    commands = {"start", "status", "restart", "shutdown", "run"}
    if not args or (args[0] not in commands and not args[0].startswith("-")):
        if len(args) > 3:
            print("Expected [host] [http_port] [https_port]; use --help for commands", file=sys.stderr)
            return 2
        return _supervise(args)

    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("start", "status", "restart", "shutdown", "run"):
        sub = subparsers.add_parser(command)
        sub.add_argument("--host", default=DEFAULT_HOST)
        sub.add_argument("--port", type=int, default=DEFAULT_PORT)
        sub.add_argument("--state-dir", type=Path, help="State/log/token directory; defaults to repo collab_state")
        if command != "run":
            sub.add_argument("--timeout", type=float, default=60.0 if command in {"restart", "shutdown"} else 20.0)
        else:
            sub.add_argument("--https-port", type=int)
    options = parser.parse_args(args)
    try:
        _base_url(options.host, options.port)
        if options.command == "run":
            positional = [options.host, str(options.port)]
            if options.https_port is not None:
                positional.append(str(options.https_port))
            return _supervise(positional, state_dir=options.state_dir)
        if options.command == "start":
            result = start_server(options.host, options.port, state_dir=options.state_dir, timeout=options.timeout)
        elif options.command == "status":
            result = get_status(options.host, options.port, timeout=options.timeout)
            print(json.dumps(result or {"status": "stopped", "url": _base_url(options.host, options.port) + "/"}))
            return 0 if result and result["status"] != "down" else 1
        else:
            result = control_server(
                options.command, options.host, options.port,
                state_dir=options.state_dir, timeout=options.timeout,
            )
        print(json.dumps(result))
        return 0
    except HTTPError as exc:
        print(f"WS_COLLAB HTTP {exc.code}; for protected controls set WS_COLLAB_TOKEN to an operator token.", file=sys.stderr)
    except (OSError, URLError, RuntimeError, ValueError) as exc:
        print(f"WS_COLLAB: {exc}", file=sys.stderr)
    return 1


def create_router(manifest: dict[str, Any] | None = None):
    """Plugin hook for standalone mode.

    Ensures the standalone server is running, then returns an empty router. The
    workbench serves ``/ws_collab`` by proxying to the standalone process (see
    the ``web_proxy`` entry in ``plugin.json``), so this router intentionally
    contributes no in-process routes.
    """

    start_server(DEFAULT_HOST, DEFAULT_PORT)
    from fastapi import APIRouter

    return APIRouter()


if __name__ == "__main__":
    raise SystemExit(main())
