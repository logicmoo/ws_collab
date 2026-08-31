"""FastAPI application factory and standalone multi-transport server.

``build_app`` assembles the REST + WebSocket routers over a shared
:class:`~ws_collab.context.AppContext` and installs a structured error handler so
every failure returns ``{"error": {code, message, details}}``.

``run`` binds real sockets for HTTP and (when configured) HTTPS across the
requested addresses, and only after the sockets are actually listening prints a
startup report covering bound addresses, transport URLs, admin URLs, TLS/auth
status, discoverable LAN URLs, failed bindings, and prominent insecurity
warnings. Secrets are never printed.
"""

from __future__ import annotations

import asyncio
import socket
import sys
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .config import Config
from .context import AppContext, build_context
from .errors import WsCollabError
from .lifecycle import LifecycleController
from .rest import create_rest_router, create_static_router
from .urls import (
    DEFAULT_ROUTE_PREFIX,
    admin_base,
    openapi_base,
    rest_base,
    websocket_path,
)
from .ws import create_ws_router

_OPENAPI_BASE = openapi_base(DEFAULT_ROUTE_PREFIX)
_DOC_URLS = {
    "docs_url": f"{_OPENAPI_BASE}/docs",
    "redoc_url": f"{_OPENAPI_BASE}/redoc",
    "openapi_url": f"{_OPENAPI_BASE}.json",
    "swagger_ui_oauth2_redirect_url": f"{_OPENAPI_BASE}/oauth2-redirect",
}

RESTART_EXIT_CODE = 75


def build_app(ctx: AppContext, *, with_lifespan: bool = True) -> FastAPI:
    if with_lifespan:
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def lifespan(app: FastAPI):
            await ctx.ensure_started()
            try:
                yield
            finally:
                await ctx.aclose()

        app = FastAPI(title="WS_COLLAB", version="1.0.0", lifespan=lifespan, **_DOC_URLS)
    else:
        app = FastAPI(title="WS_COLLAB", version="1.0.0", **_DOC_URLS)

    @app.exception_handler(WsCollabError)
    async def _ws_collab_error_handler(request, exc: WsCollabError):
        return JSONResponse(exc.to_dict(), status_code=exc.http_status)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error_handler(request, exc: StarletteHTTPException):
        detail = exc.detail
        if isinstance(detail, dict) and "code" in detail:
            return JSONResponse({"error": detail}, status_code=exc.status_code)
        return JSONResponse(
            {"error": {"code": "http_error", "message": str(detail)}}, status_code=exc.status_code
        )

    app.include_router(create_rest_router(ctx, DEFAULT_ROUTE_PREFIX))
    app.include_router(create_ws_router(ctx, DEFAULT_ROUTE_PREFIX))
    # Static assets go last so no API route is shadowed.
    app.include_router(create_static_router(ctx, DEFAULT_ROUTE_PREFIX))
    return app


def create_app(config: Config | None = None) -> tuple[FastAPI, AppContext]:
    ctx = build_context(config)
    return build_app(ctx), ctx


# --------------------------------------------------------------------- binding
def _local_ipv4_addresses() -> list[str]:
    addresses: set[str] = set()
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            addresses.add(info[4][0])
    except OSError:
        pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("8.8.8.8", 80))
            addresses.add(probe.getsockname()[0])
    except OSError:
        pass
    return sorted(a for a in addresses if not a.startswith("127."))


def _binding_plans(config: Config) -> list[dict[str, Any]]:
    hosts = config.bind_addresses or [config.host]
    plans: list[dict[str, Any]] = []
    for host in hosts:
        plans.append({"host": host, "port": config.http_port, "scheme": "http"})
        if config.https_enabled:
            plans.append(
                {
                    "host": host,
                    "port": config.https_port,
                    "scheme": "https",
                    "certfile": config.tls_cert_file,
                    "keyfile": config.tls_key_file,
                }
            )
    return plans


def build_startup_report(config: Config, bound: list[dict[str, Any]], failed: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    lines.append("=" * 68)
    lines.append("WS_COLLAB is listening")
    lines.append("=" * 68)
    for entry in bound:
        scheme = entry["scheme"]
        ws_scheme = "wss" if scheme == "https" else "ws"
        base = f"{scheme}://{entry['host']}:{entry['port']}"
        lines.append(f"  bound   {base}")
        lines.append(f"          REST  {base}{rest_base()}")
        lines.append(
            f"          WS    {ws_scheme}://{entry['host']}:{entry['port']}"
            f"{websocket_path()}"
        )
        lines.append(f"          ADMIN {base}{admin_base()}")
    loopback = [e for e in bound if e["host"] in {"127.0.0.1", "::1", "localhost"}]
    if loopback:
        first = loopback[0]
        lines.append(
            f"  loopback admin: {first['scheme']}://127.0.0.1:{first['port']}"
            f"{admin_base()}"
        )
    lan = _local_ipv4_addresses()
    if lan and config.exposed_all_interfaces:
        for ip in lan:
            example = bound[0] if bound else {"scheme": "http", "port": config.http_port}
            lines.append(f"  discoverable LAN: {example['scheme']}://{ip}:{example['port']}/ws_collab")
    lines.append("-" * 68)
    lines.append(f"  TLS:            {'enabled' if config.https_enabled else 'DISABLED'}")
    if config.auth_disabled:
        lines.append("  authentication: DISABLED (every caller is a local admin)")
    else:
        lines.append("  authentication: required (bearer token / session)")
    lines.append(f"  admin access:   {'remote enabled' if config.admin_remote else 'loopback only'}")
    lines.append(f"  writable state: {config.state_dir}")
    if config.generated_admin_token:
        lines.append(f"  generated admin token written to: {config.generated_token_path}")
    for failure in failed:
        lines.append(f"  FAILED binding {failure['host']}:{failure['port']} -> {failure['error']}")
    if config.exposed_all_interfaces:
        lines.append("  !! WARNING: bound to ALL interfaces -- ensure firewalling and TLS")
    if not config.https_enabled and not config.is_loopback_only:
        lines.append("  !! WARNING: serving non-loopback traffic WITHOUT TLS (development only)")
    for warning in config.warnings:
        lines.append(f"  ! {warning}")
    lines.append("=" * 68)
    return "\n".join(lines)


async def _serve(config: Config) -> bool:
    import uvicorn

    ctx = build_context(config)
    servers: list[tuple[dict[str, Any], Any]] = []
    restart_requested = False

    def stop_servers() -> None:
        for _, server in servers:
            server.should_exit = True

    def restart_servers() -> None:
        nonlocal restart_requested
        restart_requested = True
        stop_servers()

    ctx.lifecycle = LifecycleController(
        shutdown=stop_servers,
        restart=restart_servers,
    )
    app = build_app(ctx, with_lifespan=False)
    await ctx.ensure_started()

    failed: list[dict[str, Any]] = []
    tasks: list[asyncio.Task] = []
    for plan in _binding_plans(config):
        kwargs: dict[str, Any] = dict(host=plan["host"], port=plan["port"], log_level="warning", lifespan="off")
        if plan["scheme"] == "https":
            kwargs["ssl_certfile"] = plan["certfile"]
            kwargs["ssl_keyfile"] = plan["keyfile"]
        server = uvicorn.Server(uvicorn.Config(app, **kwargs))
        servers.append((plan, server))
        tasks.append(asyncio.create_task(server.serve()))

    # Wait until each server is actually listening (or has failed) before the report.
    deadline = 10.0
    for plan, server in servers:
        waited = 0.0
        while not server.started and waited < deadline:
            await asyncio.sleep(0.05)
            waited += 0.05
            if any(task.done() and task.exception() for task in tasks):
                break
        if not server.started:
            failed.append({"host": plan["host"], "port": plan["port"], "error": "did not start"})

    bound = [plan for plan, server in servers if server.started]
    print(build_startup_report(config, bound, failed), flush=True)

    try:
        await asyncio.gather(*tasks)
    finally:
        await ctx.aclose()
    return restart_requested


def run(config: Config | None = None) -> int:
    config = config or Config.from_env()
    config.prepare_state_dir()
    try:
        restart_requested = asyncio.run(_serve(config))
    except KeyboardInterrupt:
        print("WS_COLLAB shutting down", flush=True)
        return 0
    return RESTART_EXIT_CODE if restart_requested else 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    env_overrides: dict[str, str] = {}
    # Optional positional args: host [http_port] [https_port] (matches workbench style)
    if len(argv) >= 1:
        env_overrides["WS_COLLAB_HOST"] = argv[0]
    if len(argv) >= 2:
        env_overrides["WS_COLLAB_HTTP_PORT"] = argv[1]
    if len(argv) >= 3:
        env_overrides["WS_COLLAB_HTTPS_PORT"] = argv[2]
    import os

    from .config import _plugin_env_variables

    # plugin.json "env:variables" are the lowest-precedence defaults; the real OS
    # environment and CLI positional args still override them.
    merged = {**_plugin_env_variables(), **os.environ, **env_overrides}
    return run(Config.from_env(merged))


if __name__ == "__main__":
    raise SystemExit(main())
