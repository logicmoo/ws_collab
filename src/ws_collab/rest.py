"""REST transport for WS_COLLAB.

Every capability is reachable here (task section 4). The router is mounted under
the configured prefix (default ``/ws_collab``) with versioned resources beneath
``/ws_collab/v1``. It is a thin shell over :class:`~ws_collab.service.WsCollabService`
so REST and WebSocket stay in parity. Features: bearer-token or session auth,
CSRF for cookie mutations, origin/allowlist/rate/size limits, cursor pagination
with ``has_more``/server time, idempotent writes, conditional (ETag) reads, and
bounded long polling.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from .context import AppContext
from .errors import AuthenticationError, WsCollabError
from .security import Principal, Session

_ADMIN_DIR = Path(__file__).resolve().parent / "admin"
_SESSION_COOKIE = "ws_collab_session"
_CSRF_HEADER = "x-ws-collab-csrf"


@dataclass
class Auth:
    principal: Principal
    session: Session | None = None


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def guarded(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except WsCollabError as error:
        raise HTTPException(status_code=error.http_status, detail=error.to_dict()["error"])


async def guarded_async(coro):
    try:
        return await coro
    except WsCollabError as error:
        raise HTTPException(status_code=error.http_status, detail=error.to_dict()["error"])


def _kind_for_role(role: str) -> str:
    return {"worker": "worker", "operator": "operator", "admin": "operator", "viewer": "operator"}.get(role, "system")


# Extra MIME types the workbench may grow into; FileResponse guesses the rest.
_EXTRA_MEDIA_TYPES = {
    ".mjs": "text/javascript",
    ".map": "application/json",
    ".woff2": "font/woff2",
    ".webmanifest": "application/manifest+json",
}


def _serve_admin_asset(request: Request, relative: str) -> Response:
    """Serve a file from the workbench directory, traversal-safe.

    This is what a dev server would do: any real asset is returned with a sane
    content type, and anything that is not a file falls through so the caller can
    decide (SPA fallback or 404).
    """

    from .security import safe_join

    try:
        path = safe_join(_ADMIN_DIR, relative)
    except WsCollabError as error:
        raise HTTPException(status_code=error.http_status, detail=error.to_dict()["error"])
    if path.is_dir():
        path = path / "index.html"
    if not path.is_file():
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": relative})
    media_type = _EXTRA_MEDIA_TYPES.get(path.suffix.lower())
    return FileResponse(path, media_type=media_type)


def create_rest_router(ctx: AppContext, mount: str = "/ws_collab", *, in_schema: bool = True) -> APIRouter:
    """Build the REST surface under ``mount``.

    Every route is defined relative to a single mount root, so the same API can
    be mounted at several roots by calling this more than once. The server mounts
    it at ``/``, ``/v1``, ``/ws_collab``, and ``/ws_collab/v1`` -- any of those
    combinations answers identically. Only the canonical mount appears in the
    OpenAPI schema, so each operation is documented once.
    """

    mount = mount.rstrip("/")
    root = mount or "/"
    router = APIRouter(tags=["ws_collab"], include_in_schema=in_schema)
    service = ctx.service
    security = ctx.security

    # ---------------------------------------------------------------- auth core
    def _authenticate(request: Request) -> Auth:
        header = request.headers.get("authorization")
        query_token = request.query_params.get("access_token")
        principal = security.authenticate_token(header, query_token)
        if principal is not None:
            return Auth(principal=principal)
        cookie = request.cookies.get(_SESSION_COOKIE)
        session_auth = security.authenticate_session(cookie)
        if session_auth is not None:
            principal, session = session_auth
            return Auth(principal=principal, session=session)
        raise AuthenticationError("authentication required")

    async def _require(request: Request, role: str, *, mutating: bool = False) -> Auth:
        try:
            await ctx.ensure_started()
            security.check_allowlist(_client_ip(request))
            auth = _authenticate(request)
            security.rate_limit(auth.principal.label or (_client_ip(request) or "anon"))
            security.require_role(auth.principal, role)
            if auth.session is not None and mutating:
                security.check_origin(request.headers.get("origin"), required=False)
                security.verify_csrf(auth.session, request.headers.get(_CSRF_HEADER))
            return auth
        except WsCollabError as error:
            raise HTTPException(status_code=error.http_status, detail=error.to_dict()["error"])

    def _source(auth: Auth, body: dict[str, Any]) -> tuple[str, str]:
        source_id = body.get("source_id") or auth.principal.label
        source_kind = body.get("source_kind") or _kind_for_role(auth.principal.role)
        return source_id, source_kind

    def _filters(request: Request) -> dict[str, Any]:
        params = request.query_params
        filters: dict[str, Any] = {}
        for key in ("type", "source_id", "source_kind", "correlation_id", "since", "until"):
            if params.get(key):
                filters[key] = params[key]
        if params.get("q"):
            filters["text"] = params["q"]
        return filters

    # -------------------------------------------------------------- public info
    @router.get(root, include_in_schema=False)
    @router.get(f"{root}/" if root != "/" else "/index.html", include_in_schema=False)
    @router.get(f"{mount}/health")
    async def health(request: Request) -> Response:
        """Health for API clients; the workbench itself for browsers.

        Hitting a mount root in a browser serves the operations workbench inline
        (so ``/ws_collab/#voices`` works directly), while API clients and the
        explicit ``/health`` path always get the liveness payload.
        """

        if request.url.path.rstrip("/").endswith("/health"):
            return JSONResponse(service.health())
        if "text/html" in request.headers.get("accept", ""):
            if not security.is_admin_client_allowed(_client_ip(request)):
                raise HTTPException(
                    status_code=403,
                    detail={"code": "forbidden", "message": "admin is loopback-only; set WS_COLLAB_ADMIN_REMOTE=1"},
                )
            index = _ADMIN_DIR / "index.html"
            if index.is_file():
                return FileResponse(index)
        return JSONResponse(service.health())

    @router.get(f"{mount}/app.css", include_in_schema=False)
    @router.get(f"{mount}/app.js", include_in_schema=False)
    async def mount_asset(request: Request) -> Response:
        """Fast path for the two core assets; the catch-all below serves the rest."""

        if not security.is_admin_client_allowed(_client_ip(request)):
            raise HTTPException(status_code=403, detail={"code": "forbidden", "message": "admin is loopback-only"})
        return _serve_admin_asset(request, Path(request.url.path).name)

    @router.get(f"{mount}/status")
    async def status() -> dict[str, Any]:
        """Subsystem rollup. Public and secret-free, for status pages/monitors."""

        await ctx.ensure_started()
        return service.status()

    @router.get(f"{mount}/ready")
    async def ready() -> Response:
        """Readiness probe: 503 while the service cannot serve traffic."""

        await ctx.ensure_started()
        is_ready, body = service.readiness()
        return JSONResponse(body, status_code=200 if is_ready else 503)

    @router.get(f"{mount}/endpoints")
    async def endpoints(request: Request) -> dict[str, Any]:
        await _require(request, "viewer")
        origin = str(request.base_url).rstrip("/")
        return service.endpoints(origin=origin)

    @router.get(f"{mount}/capabilities")
    async def capabilities() -> dict[str, Any]:
        await ctx.ensure_started()
        return service.capabilities()

    # ------------------------------------------------------- docs / ui / files
    @router.get(f"{mount}/docs")
    async def list_docs(request: Request) -> dict[str, Any]:
        await _require(request, "viewer")
        return service.list_docs()

    @router.get(f"{mount}/docs/{{name}}")
    async def read_doc(request: Request, name: str) -> Response:
        await _require(request, "viewer")
        text = guarded(service.read_doc, name)
        return Response(content=text, media_type="text/markdown; charset=utf-8")

    @router.get(f"{mount}/ui/links")
    async def ui_links(request: Request) -> dict[str, Any]:
        await _require(request, "viewer")
        return service.ui_links(origin=str(request.base_url).rstrip("/"))

    @router.get(f"{mount}/files")
    async def list_files(request: Request, path: str = Query("")) -> dict[str, Any]:
        await _require(request, "operator")
        return guarded(service.list_files, path)

    @router.get(f"{mount}/files/content")
    async def read_file(request: Request, path: str = Query(...)) -> dict[str, Any]:
        await _require(request, "operator")
        return guarded(service.read_file, path)

    # --------------------------------------------------------------------- auth
    @router.post(f"{mount}/auth/login")
    async def login(request: Request, body: dict[str, Any] = Body(default_factory=dict)) -> Response:
        await ctx.ensure_started()
        security.check_allowlist(_client_ip(request))
        token = body.get("token", "")
        principal = security.authenticate_token(token)
        if principal is None:
            raise HTTPException(status_code=401, detail={"code": "authentication_required", "message": "invalid token"})
        session = security.create_session(principal.role, principal.label)
        security.audit("login", label=principal.label, role=principal.role)
        response = JSONResponse({"role": principal.role, "csrf": session.csrf, "label": principal.label})
        response.set_cookie(
            _SESSION_COOKIE,
            security.cookie_value(session),
            httponly=True,
            samesite="strict",
            secure=ctx.config.https_enabled,
            max_age=12 * 3600,
        )
        return response

    @router.post(f"{mount}/auth/logout")
    async def logout(request: Request) -> Response:
        auth = await _require(request, "viewer")
        if auth.session is not None:
            security.destroy_session(auth.session.sid)
        response = JSONResponse({"ok": True})
        response.delete_cookie(_SESSION_COOKIE)
        return response

    @router.get(f"{mount}/auth/whoami")
    async def whoami(request: Request) -> dict[str, Any]:
        auth = await _require(request, "viewer")
        return {"principal": auth.principal.public(), "csrf": auth.session.csrf if auth.session else None}

    # ------------------------------------------------------------------- events
    @router.get(f"{mount}/events")
    async def read_events(
        request: Request,
        stream: str = Query(...),
        after: str | None = Query(None),
        limit: int = Query(100, ge=1, le=1000),
        wait_ms: int = Query(0, ge=0, le=30000),
        if_none_match: str | None = Header(None),
    ) -> Response:
        await _require(request, "viewer")
        filters = _filters(request)
        page = await guarded_async(
            service.read_events_wait(stream, after=after, limit=limit, filters=filters, wait_ms=wait_ms)
        )
        # Conditional request: if the caller is already at the tail and nothing
        # new arrived, answer 304 instead of resending an empty page.
        if not page["events"] and if_none_match and after and if_none_match == after:
            return Response(status_code=304, headers={"ETag": after})
        response = JSONResponse(page)
        response.headers["ETag"] = page["next_cursor"]
        return response

    @router.post(f"{mount}/events")
    async def write_event(
        request: Request,
        body: dict[str, Any] = Body(...),
        idempotency_key: str | None = Header(None),
    ) -> dict[str, Any]:
        auth = await _require(request, "worker", mutating=True)
        source_id, source_kind = _source(auth, body)
        return guarded(
            service.publish,
            stream=body.get("stream"),
            type=body.get("type"),
            data=body.get("data") or {},
            source_id=source_id,
            source_kind=source_kind,
            correlation_id=body.get("correlation_id"),
            idempotency_key=body.get("idempotency_key") or idempotency_key,
        )

    @router.get(f"{mount}/streams/{{stream}}/tail")
    async def tail_stream(request: Request, stream: str, count: int = Query(50, ge=1, le=2000)) -> dict[str, Any]:
        await _require(request, "viewer")
        return guarded(service.tail, stream, count, _filters(request))

    # ------------------------------------------------------------- conversation
    @router.post(f"{mount}/conversation/events")
    async def post_conversation(
        request: Request,
        body: dict[str, Any] = Body(...),
        idempotency_key: str | None = Header(None),
    ) -> dict[str, Any]:
        auth = await _require(request, "worker", mutating=True)
        source_id, source_kind = _source(auth, body)
        return guarded(
            service.add_conversation,
            body.get("text", ""),
            source_id=source_id,
            source_kind=source_kind,
            correlation_id=body.get("correlation_id"),
            idempotency_key=body.get("idempotency_key") or idempotency_key,
            data=body.get("data"),
        )

    @router.get(f"{mount}/conversation")
    async def get_conversation(request: Request, after: str | None = Query(None), limit: int = Query(100, ge=1, le=1000)) -> dict[str, Any]:
        await _require(request, "viewer")
        return guarded(service.read_events, "conversation", after=after, limit=limit, filters=_filters(request))

    # --------------------------------------------------------- mailboxes (streams)
    # Every durable JSONL stream is exposed as a "mailbox" so the shared workbench
    # ChatConversation UI can browse ws_collab streams (mailbox == stream file).
    @router.get(f"{mount}/mailbox/mailboxes")
    async def mailbox_mailboxes(request: Request) -> dict[str, Any]:
        await _require(request, "viewer")
        return guarded(service.list_mailboxes)

    @router.get(f"{mount}/mailbox/agents")
    async def mailbox_agents(request: Request) -> dict[str, Any]:
        await _require(request, "viewer")
        return guarded(service.mailbox_agents)

    @router.get(f"{mount}/mailbox/messages")
    async def mailbox_messages(
        request: Request,
        mailbox: str | None = Query(None),
        send_to: str | None = Query(None),
        to: str | None = Query(None),
        sender: str | None = Query(None, alias="from"),
        text: str | None = Query(None),
        filter: bool = Query(False),  # noqa: A002 - matches the UI query param
        limit: int = Query(300, ge=1, le=2000),
    ) -> dict[str, Any]:
        await _require(request, "viewer")
        stream = mailbox or send_to or "conversation"
        return guarded(
            service.mailbox_messages,
            stream,
            to=to,
            sender=sender,
            send_to=send_to,
            text=text,
            do_filter=filter,
            limit=limit,
            filters=_filters(request),
        )

    @router.post(f"{mount}/mailbox/send")
    async def mailbox_send(
        request: Request,
        body: dict[str, Any] = Body(...),
        idempotency_key: str | None = Header(None),
    ) -> dict[str, Any]:
        auth = await _require(request, "worker", mutating=True)
        source_id, source_kind = _source(auth, {"source_id": body.get("sender"), "source_kind": body.get("source_kind")})
        return guarded(
            service.mailbox_send,
            to=str(body.get("to") or ""),
            text=str(body.get("text") or ""),
            sender=source_id,
            source_kind=source_kind,
            send_to=body.get("send_to"),
            idempotency_key=body.get("idempotency_key") or idempotency_key,
        )

    @router.get(f"{mount}/mailbox/cursor")
    async def mailbox_cursor_get(request: Request, mailbox: str = Query(...), agent: str = Query(...)) -> dict[str, Any]:
        await _require(request, "viewer")
        return guarded(service.mailbox_cursor, mailbox, agent)

    @router.post(f"{mount}/mailbox/cursor")
    async def mailbox_cursor_move(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        await _require(request, "operator", mutating=True)
        return guarded(service.mailbox_cursor, str(body.get("mailbox") or ""), str(body.get("agent") or ""))

    @router.delete(f"{mount}/mailbox/cursor")
    async def mailbox_cursor_clear(request: Request, mailbox: str = Query(...), agent: str = Query(...)) -> dict[str, Any]:
        await _require(request, "operator", mutating=True)
        return guarded(service.mailbox_cursor, mailbox, agent)

    @router.get(f"{mount}/mailbox/mailbox-config")
    async def mailbox_config_get(request: Request, mailbox: str = Query(...)) -> dict[str, Any]:
        await _require(request, "viewer")
        return {"mailbox": mailbox, "config": {}}

    @router.post(f"{mount}/mailbox/mailbox-config")
    async def mailbox_config_set(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        await _require(request, "operator", mutating=True)
        return {"mailbox": body.get("mailbox"), "config": body.get("config") or {}, "subscribed": []}

    @router.post(f"{mount}/mailbox/subscription")
    async def mailbox_subscription(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        await _require(request, "operator", mutating=True)
        return {"agent": body.get("agent"), "mailbox": body.get("mailbox"), "state": body.get("state")}

    @router.post(f"{mount}/mailbox/agents")
    async def mailbox_add_agent(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        await _require(request, "worker", mutating=True)
        agent_id = str(body.get("id") or "").strip()
        if not agent_id:
            raise HTTPException(status_code=400, detail={"code": "invalid", "message": "id is required"})
        props = body.get("properties") if isinstance(body.get("properties"), dict) else None
        return guarded(service.set_agent, agent_id, props)

    @router.post(f"{mount}/mailbox/create")
    async def mailbox_create(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        auth = await _require(request, "worker", mutating=True)
        return guarded(
            service.create_mailbox,
            str(body.get("id") or body.get("name") or ""),
            purpose=str(body.get("purpose") or ""),
            hidden=bool(body.get("hidden", False)),
            writable=bool(body.get("writable", True)),
            source=str(body.get("source") or "jsonl"),
            created_by=body.get("created_by") or auth.principal.label or "operator",
        )

    # Alias so the classic "add mailbox" control behaves like /mailbox/create.
    @router.post(f"{mount}/mailbox/mailboxes")
    async def mailbox_add(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        auth = await _require(request, "worker", mutating=True)
        return guarded(
            service.create_mailbox,
            str(body.get("id") or body.get("name") or ""),
            purpose=str(body.get("purpose") or ""),
            hidden=bool(body.get("hidden", False)),
            writable=bool(body.get("writable", True)),
            source=str(body.get("source") or "jsonl"),
            created_by=body.get("created_by") or auth.principal.label or "operator",
        )

    @router.post(f"{mount}/mailbox/delete")
    async def mailbox_delete_post(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        await _require(request, "operator", mutating=True)
        return guarded(service.delete_mailbox, str(body.get("id") or body.get("name") or ""))

    @router.delete(f"{mount}/mailbox/mailboxes")
    async def mailbox_delete(request: Request, id: str = Query(...)) -> dict[str, Any]:  # noqa: A002 - matches UI param
        await _require(request, "operator", mutating=True)
        return guarded(service.delete_mailbox, id)

    @router.post(f"{mount}/mailbox/record")
    async def mailbox_record(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        await _require(request, "operator", mutating=True)
        record_id = str(body.get("id") or "")
        record = body.get("record")
        if not record_id or not isinstance(record, dict):
            raise HTTPException(status_code=400, detail={"code": "invalid", "message": "id and record object are required"})
        return guarded(service.mailbox_record, record_id, record, str(body.get("mode") or "at-end"))

    @router.post(f"{mount}/mailbox/entity")
    async def mailbox_entity(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        await _require(request, "operator", mutating=True)
        return {"kind": body.get("kind"), "id": body.get("id"), "entry": body.get("entry")}

    # ------------------------------------------------------------------ workers
    @router.post(f"{mount}/workers/register")
    async def register_worker(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        await _require(request, "worker", mutating=True)
        return guarded(service.register_worker, body.get("worker_id"), body.get("task", ""), body.get("meta"))

    @router.post(f"{mount}/workers/{{worker_id}}/status")
    async def worker_status(request: Request, worker_id: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        await _require(request, "worker", mutating=True)
        return guarded(service.worker_status, worker_id, body.get("status", ""), body.get("data"), body.get("errors"))

    @router.get(f"{mount}/workers")
    async def list_workers(request: Request) -> dict[str, Any]:
        await _require(request, "viewer")
        return service.list_workers()

    @router.post(f"{mount}/workers/monitor")
    async def run_monitor(request: Request) -> dict[str, Any]:
        await _require(request, "operator", mutating=True)
        return service.run_monitor_cycle()

    # -------------------------------------------------------------------- audio
    @router.post(f"{mount}/audio/utterance")
    async def inject_utterance(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        await _require(request, "operator", mutating=True)
        segment = await guarded_async(
            service.capture.inject_utterance(
                body.get("text", ""),
                source_kind=body.get("source_kind", "operator"),
                correlation_id=body.get("correlation_id"),
                device_id=body.get("device_id"),
                is_loopback=bool(body.get("is_loopback", False)),
                expected_tts_text=body.get("expected_tts_text"),
            )
        )
        if segment is None:
            return {"accepted": False, "reason": "input muted during TTS"}
        return {"accepted": True, "segment_id": segment.id, "correlation_id": segment.correlation_id}

    @router.get(f"{mount}/audio/capture")
    async def capture_state(request: Request) -> dict[str, Any]:
        await _require(request, "viewer")
        return service.capture_state()

    @router.post(f"{mount}/audio/capture/start")
    async def capture_start(request: Request, body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
        await _require(request, "operator", mutating=True)
        return guarded(service.start_capture, body.get("device_id"))

    @router.post(f"{mount}/audio/capture/stop")
    async def capture_stop(request: Request) -> dict[str, Any]:
        await _require(request, "operator", mutating=True)
        return guarded(service.stop_capture)

    @router.get(f"{mount}/audio/devices")
    async def list_devices(request: Request) -> dict[str, Any]:
        await _require(request, "viewer")
        return service.list_devices()

    @router.post(f"{mount}/audio/devices/refresh")
    async def refresh_devices(request: Request) -> dict[str, Any]:
        await _require(request, "operator", mutating=True)
        return service.refresh_devices()

    @router.post(f"{mount}/audio/devices/test")
    async def test_output_device(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        await _require(request, "operator", mutating=True)
        return guarded(service.test_output_device, body.get("device_id", ""), text=body.get("text"))

    @router.get(f"{mount}/audio/routing")
    async def routing_matrix(request: Request) -> dict[str, Any]:
        await _require(request, "viewer")
        return service.routing_matrix()

    @router.get(f"{mount}/audio/engines")
    async def stt_engine_routes(request: Request) -> dict[str, Any]:
        """One row per STT engine with the input device it listens on."""

        await _require(request, "viewer")
        return service.stt_engine_routes()

    @router.post(f"{mount}/audio/engines/{{engine}}/device")
    async def set_engine_device(request: Request, engine: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        auth = await _require(request, "operator", mutating=True)
        return guarded(service.set_engine_device, engine, body.get("device_id", ""), operator=auth.principal.label)

    @router.get(f"{mount}/audio/defaults")
    async def audio_defaults(request: Request) -> dict[str, Any]:
        await _require(request, "viewer")
        return service.get_audio_defaults()

    @router.post(f"{mount}/audio/defaults/output")
    async def set_default_output(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        auth = await _require(request, "operator", mutating=True)
        return guarded(service.set_default_output_device, body.get("device_id", ""), operator=auth.principal.label)

    @router.post(f"{mount}/audio/routing")
    async def set_route(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        auth = await _require(request, "operator", mutating=True)
        params = {k: v for k, v in body.items() if k not in {"source", "engine", "device_id"}}
        return guarded(service.set_route, body.get("source"), body.get("engine"), body.get("device_id"), auth.principal.label, **params)

    # --------------------------------------------------------------- transcripts
    @router.get(f"{mount}/stt/transcripts")
    async def stt_transcripts(request: Request, after: str | None = Query(None), limit: int = Query(100, ge=1, le=1000)) -> dict[str, Any]:
        await _require(request, "viewer")
        return guarded(service.read_events, "stt_transcripts", after=after, limit=limit, filters=_filters(request))

    @router.post(f"{mount}/stt/ingest")
    async def stt_ingest(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Ingest an external recognizer's transcript (e.g. the Copilot app)."""

        await _require(request, "worker", mutating=True)
        return guarded(
            service.ingest_transcript,
            engine=body.get("engine", "external"),
            text=body.get("text", ""),
            correlation_id=body.get("correlation_id"),
            confidence=float(body.get("confidence", 0.9)),
            is_final=bool(body.get("is_final", True)),
            language=body.get("language", "en"),
            source_kind=body.get("source_kind", "operator"),
            expected_tts_text=body.get("expected_tts_text"),
            resolve=bool(body.get("resolve", True)),
            audio_meta=body.get("audio_meta"),
        )

    @router.get(f"{mount}/transcripts")
    async def translated_audio(request: Request, after: str | None = Query(None), limit: int = Query(100, ge=1, le=1000)) -> dict[str, Any]:
        await _require(request, "viewer")
        return guarded(service.read_events, "translated_audio", after=after, limit=limit, filters=_filters(request))

    # ---------------------------------------------------------------------- tts
    @router.post(f"{mount}/tts/speak")
    async def tts_speak(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        await _require(request, "worker", mutating=True)
        return guarded(
            service.speak,
            body.get("agent_id", "agent"),
            body.get("text", ""),
            priority=body.get("priority"),
            interrupt=bool(body.get("interrupt", False)),
            correlation_id=body.get("correlation_id"),
        )

    @router.get(f"{mount}/tts")
    async def tts_state(request: Request) -> dict[str, Any]:
        await _require(request, "viewer")
        return service.tts.state()

    @router.post(f"{mount}/tts/cancel")
    async def tts_cancel(request: Request, body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
        await _require(request, "operator", mutating=True)
        if body.get("id"):
            return {"cancelled": service.tts.cancel(body["id"])}
        if body.get("agent_id"):
            return {"cancelled_count": service.tts.cancel_agent(body["agent_id"])}
        return {"cancelled": False}

    @router.post(f"{mount}/tts/measure")
    async def tts_measure(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        await _require(request, "operator", mutating=True)
        return await guarded_async(service.measure_tts_accuracy(body.get("agent_id", "agent"), body.get("text", "")))

    @router.get(f"{mount}/tts/accuracy")
    async def tts_accuracy(request: Request) -> dict[str, Any]:
        await _require(request, "viewer")
        return service.accuracy_summary()

    # ------------------------------------------------------------------- voices
    @router.get(f"{mount}/voices")
    async def list_voices(request: Request) -> dict[str, Any]:
        await _require(request, "viewer")
        return service.list_voices()

    @router.post(f"{mount}/voices/assign")
    async def assign_voices(request: Request, body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
        await _require(request, "operator", mutating=True)
        return guarded(service.assign_voices, body.get("policy"))

    @router.post(f"{mount}/voices/preview")
    async def preview_voice(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        await _require(request, "operator", mutating=True)
        return guarded(
            service.preview_voice,
            body.get("voice_id", ""),
            text=body.get("text"),
            rate=float(body.get("rate", 1.0)),
            pitch=float(body.get("pitch", 0.0)),
            volume=float(body.get("volume", 1.0)),
        )

    @router.post(f"{mount}/voices/clone")
    async def clone_voice(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        auth = await _require(request, "operator", mutating=True)
        return guarded(
            service.clone_voice,
            body.get("base_voice_id", ""),
            body.get("name", ""),
            rate=float(body.get("rate", 1.0)),
            pitch=float(body.get("pitch", 0.0)),
            volume=float(body.get("volume", 1.0)),
            style=body.get("style", ""),
            operator=auth.principal.label,
        )

    @router.post(f"{mount}/voices/clone/delete")
    async def delete_clone(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        auth = await _require(request, "operator", mutating=True)
        return guarded(service.delete_clone, body.get("clone_id", ""), auth.principal.label)

    # Registered after the static routes above so "assign" is never captured as
    # an agent id by this parameterised path.
    @router.post(f"{mount}/voices/{{agent_id}}")
    async def set_voice(request: Request, agent_id: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        auth = await _require(request, "operator", mutating=True)
        return guarded(service.set_voice_profile, agent_id, body, auth.principal.label)

    # ---------------------------------------------------------------- convert
    @router.post(f"{mount}/convert")
    async def convert_representation(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Render a value (or batch of {id,value} items) as MeTTa or pretty JSON."""

        await _require(request, "viewer")
        return guarded(
            service.convert_representation,
            value=body.get("value"),
            to=body.get("to", "metta"),
            items=body.get("items"),
        )

    # ------------------------------------------------------------------ cursors
    @router.get(f"{mount}/cursors")
    async def list_cursors(request: Request) -> dict[str, Any]:
        await _require(request, "viewer")
        return service.cursor_list()

    @router.get(f"{mount}/cursors/{{stream}}/{{consumer}}")
    async def get_cursor(request: Request, stream: str, consumer: str) -> dict[str, Any]:
        await _require(request, "viewer")
        return guarded(service.cursor_get, stream, consumer)

    @router.get(f"{mount}/cursors/{{stream}}/{{consumer}}/history")
    async def cursor_history(request: Request, stream: str, consumer: str) -> dict[str, Any]:
        await _require(request, "viewer")
        return guarded(service.cursor_history, stream, consumer)

    @router.post(f"{mount}/cursors/{{stream}}/{{consumer}}/commit")
    async def cursor_commit(request: Request, stream: str, consumer: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        await _require(request, "worker", mutating=True)
        return guarded(service.cursor_commit, stream, consumer, body.get("token"), body.get("reason", "processed"))

    @router.post(f"{mount}/cursors/{{stream}}/{{consumer}}/reposition")
    async def cursor_reposition(request: Request, stream: str, consumer: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        auth = await _require(request, "operator", mutating=True)
        return guarded(
            service.cursor_reposition,
            stream,
            consumer,
            token=body.get("token"),
            seq=body.get("seq"),
            reason=body.get("reason", "operator reposition"),
            operator=auth.principal.label,
            allow_replay=bool(body.get("allow_replay", False)),
            allow_skip=bool(body.get("allow_skip", False)),
        )

    @router.post(f"{mount}/cursors/{{stream}}/{{consumer}}/reset")
    async def cursor_reset(request: Request, stream: str, consumer: str, body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
        auth = await _require(request, "operator", mutating=True)
        return guarded(service.cursor_reset, stream, consumer, to=body.get("to", "start"), reason=body.get("reason", ""), operator=auth.principal.label)

    # ------------------------------------------------------------------- prompt
    @router.get(f"{mount}/prompt")
    async def get_prompt(request: Request) -> dict[str, Any]:
        await _require(request, "viewer")
        return service.prompt_get()

    @router.post(f"{mount}/prompt")
    async def save_prompt(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        auth = await _require(request, "operator", mutating=True)
        return guarded(service.prompt_save, body.get("text", ""), auth.principal.label, body.get("note", ""))

    @router.get(f"{mount}/prompt/history")
    async def prompt_history(request: Request) -> dict[str, Any]:
        await _require(request, "viewer")
        return service.prompt_history()

    @router.post(f"{mount}/prompt/preview-diff")
    async def prompt_preview(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        await _require(request, "operator", mutating=True)
        return guarded(service.prompt_preview_diff, body.get("text", ""))

    @router.post(f"{mount}/prompt/rollback")
    async def prompt_rollback(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        auth = await _require(request, "operator", mutating=True)
        return guarded(service.prompt_rollback, int(body.get("version", 0)), auth.principal.label)

    # --------------------------------------------------------- config/diag/audit
    @router.get(f"{mount}/config")
    async def get_config(request: Request) -> dict[str, Any]:
        await _require(request, "viewer")
        return service.get_config_public()

    @router.get(f"{mount}/diagnostics")
    async def diagnostics(request: Request) -> dict[str, Any]:
        await _require(request, "viewer")
        return service.diagnostics()

    @router.get(f"{mount}/alerts")
    async def alerts(request: Request, after: str | None = Query(None), limit: int = Query(100, ge=1, le=1000)) -> dict[str, Any]:
        await _require(request, "viewer")
        return guarded(service.read_events, "system_alerts", after=after, limit=limit, filters=_filters(request))

    @router.get(f"{mount}/audit")
    async def audit(request: Request, after: str | None = Query(None), limit: int = Query(100, ge=1, le=1000)) -> dict[str, Any]:
        await _require(request, "operator")
        return guarded(service.read_audit, after, limit)

    # -------------------------------------------------------------------- admin
    @router.get(f"{mount}/admin")
    async def admin_redirect(request: Request) -> Response:
        # Redirect to the trailing-slash form so the page's relative asset URLs
        # resolve under /admin/ rather than the REST root. Keeping this relative
        # means the admin page works under any configured route prefix.
        from fastapi.responses import RedirectResponse

        return RedirectResponse(url=f"{mount}/admin/", status_code=307)

    @router.get(f"{mount}/admin/")
    async def admin_index(request: Request) -> Response:
        if not security.is_admin_client_allowed(_client_ip(request)):
            raise HTTPException(status_code=403, detail={"code": "forbidden", "message": "admin is loopback-only; set WS_COLLAB_ADMIN_REMOTE=1"})
        index = _ADMIN_DIR / "index.html"
        if not index.is_file():
            raise HTTPException(status_code=404, detail={"code": "not_found", "message": "admin bundle missing"})
        return FileResponse(index)

    @router.get(f"{mount}/admin/{{asset:path}}", include_in_schema=False)
    async def admin_asset(request: Request, asset: str) -> Response:
        if not security.is_admin_client_allowed(_client_ip(request)):
            raise HTTPException(status_code=403, detail={"code": "forbidden", "message": "admin is loopback-only"})
        return _serve_admin_asset(request, asset)

    return router


def create_static_router(ctx: AppContext) -> APIRouter:
    """Static file serving and SPA fallback, mounted last.

    This is what a dev server provides: any real asset is returned with a sane
    content type, and an unknown HTML navigation falls back to the app shell so
    client-side routes survive a reload.

    It must be included *after* every API router, because a catch-all would
    otherwise shadow routes registered later -- including the alias mounts.
    """

    router = APIRouter(include_in_schema=False)
    security = ctx.security

    @router.get("/{asset:path}")
    async def static_or_app_shell(request: Request, asset: str) -> Response:
        if not security.is_admin_client_allowed(_client_ip(request)):
            raise HTTPException(status_code=403, detail={"code": "forbidden", "message": "admin is loopback-only"})
        from .security import safe_join

        # Strip any mount prefix so "/ws_collab/v1/logo.svg" finds "logo.svg".
        relative = asset
        for mount in ("ws_collab/v1/", "ws_collab/", "v1/"):
            if relative.startswith(mount):
                relative = relative[len(mount):]
                break

        if relative:
            try:
                candidate = safe_join(_ADMIN_DIR, relative)
                if candidate.is_file():
                    media_type = _EXTRA_MEDIA_TYPES.get(candidate.suffix.lower())
                    return FileResponse(candidate, media_type=media_type)
            except WsCollabError:
                pass  # traversal attempt: fall through to the 404 below

        if "text/html" in request.headers.get("accept", ""):
            index = _ADMIN_DIR / "index.html"
            if index.is_file():
                return FileResponse(index)
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": asset or "/"})

    return router

