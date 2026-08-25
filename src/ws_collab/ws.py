"""WebSocket (WS/WSS) transport for WS_COLLAB.

Full parity with REST over a single ``/ws_collab/ws`` endpoint: authentication,
capability negotiation, subscribe/unsubscribe with per-stream cursors and filters,
gap-free catch-up-then-live delivery, event publication with acknowledgements,
cursor operations, structured errors, liveness ping/pong, and resume from the
last acknowledged cursor. It shares the service layer with REST, so events written
over either transport appear on the other immediately.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .context import AppContext
from .errors import AuthenticationError, ValidationError, WsCollabError
from .events import utc_now_iso
from .notify import Subscription


def create_ws_router(ctx: AppContext, mount: str = "/ws_collab") -> APIRouter:
    mount = mount.rstrip("/")
    router = APIRouter()
    service = ctx.service
    security = ctx.security

    @router.websocket(f"{mount}/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        await ctx.ensure_started()
        client_ip = websocket.client.host if websocket.client else None
        try:
            security.check_allowlist(client_ip)
            security.check_origin(websocket.headers.get("origin"), required=False)
        except WsCollabError as error:
            await websocket.close(code=1008)
            return
        try:
            security.acquire_connection()
        except WsCollabError:
            await websocket.close(code=1013)  # try again later
            return
        connection = _WsConnection(ctx, websocket, client_ip)
        try:
            await websocket.accept()
            await connection.run()
        except WebSocketDisconnect:
            pass
        finally:
            connection.teardown()
            security.release_connection()

    return router


class _WsConnection:
    def __init__(self, ctx: AppContext, websocket: WebSocket, client_ip: str | None):
        self.ctx = ctx
        self.ws = websocket
        self.service = ctx.service
        self.security = ctx.security
        self.client_ip = client_ip
        self.principal = None
        self.sub: Subscription | None = None
        self.streams: set[str] = set()
        self.filters: dict[str, Any] = {}
        self.delivered_seq: dict[str, int] = {}
        self._closing = False

    # ------------------------------------------------------------------ helpers
    async def _send(self, payload: dict[str, Any]) -> None:
        await self.ws.send_text(json.dumps(payload, ensure_ascii=False))

    async def _error(self, error: WsCollabError, ref: str | None = None) -> None:
        message = error.to_dict()
        if ref is not None:
            message["ref"] = ref
        await self._send({"type": "error", **message})

    def _predicate(self):
        from .service import _build_predicate

        return _build_predicate(self.filters)

    # --------------------------------------------------------------------- loop
    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        self.sub = self.service.broker.subscribe(set(), loop, self._predicate())
        writer = asyncio.create_task(self._writer())
        pinger = asyncio.create_task(self._pinger())
        try:
            await self._reader()
        finally:
            for task in (writer, pinger):
                task.cancel()
            for task in (writer, pinger):
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    async def _reader(self) -> None:
        while True:
            message = await self.ws.receive()
            if message["type"] == "websocket.disconnect":
                return
            raw = message.get("text")
            if raw is None and message.get("bytes") is not None:
                raw = message["bytes"].decode("utf-8", "replace")
            if raw is None:
                continue
            try:
                self.security.check_ws_message_size(len(raw.encode("utf-8")))
            except WsCollabError as error:
                await self._error(error)
                continue
            try:
                frame = json.loads(raw)
                if not isinstance(frame, dict):
                    raise ValidationError("frame must be a JSON object")
            except (ValueError, ValidationError) as error:
                await self._error(ValidationError(f"invalid frame: {error}"))
                continue
            await self._handle(frame)

    async def _handle(self, frame: dict[str, Any]) -> None:
        kind = frame.get("type")
        ref = frame.get("ack_id") or frame.get("ref")
        try:
            if kind == "auth":
                return await self._handle_auth(frame)
            if self.principal is None:
                raise AuthenticationError("authenticate first")
            self.security.rate_limit(self.principal.label or (self.client_ip or "ws"))
            if kind == "subscribe":
                await self._handle_subscribe(frame)
            elif kind == "unsubscribe":
                await self._handle_unsubscribe(frame)
            elif kind == "resume":
                await self._handle_subscribe(frame)  # resume == subscribe from cursors
            elif kind == "publish":
                await self._handle_publish(frame)
            elif kind == "stt_ingest":
                await self._handle_ingest(frame)
            elif kind == "cursor":
                await self._handle_cursor(frame)
            elif kind == "ping":
                await self._send({"type": "pong", "server_time": utc_now_iso()})
            elif kind == "pong":
                # Client heartbeat/keepalive reply — accept and ignore.
                return
            else:
                raise ValidationError(f"unknown message type: {kind!r}")
        except WsCollabError as error:
            await self._error(error, ref)

    async def _handle_auth(self, frame: dict[str, Any]) -> None:
        token = frame.get("token", "")
        principal = self.security.authenticate_token(token)
        if principal is None:
            raise AuthenticationError("invalid token")
        self.principal = principal
        self.security.audit("ws_auth", label=principal.label, role=principal.role)
        await self._send({
            "type": "auth_ok",
            "principal": principal.public(),
            "capabilities": self.service.capabilities(),
        })

    def _require_role(self, role: str) -> None:
        self.security.require_role(self.principal, role)

    async def _handle_subscribe(self, frame: dict[str, Any]) -> None:
        self._require_role("viewer")
        streams = frame.get("streams") or []
        if not isinstance(streams, list):
            raise ValidationError("streams must be a list")
        cursors = frame.get("cursors") or {}
        self.filters = frame.get("filters") or {}
        self.streams |= {str(s) for s in streams}
        # Update the persistent subscription (same queue the writer reads) so live
        # events start buffering BEFORE catch-up runs -- this guarantees no gap and
        # avoids re-binding the writer to a new queue.
        if self.sub is not None:
            self.service.broker.update(self.sub.id, set(self.streams), self._predicate())
        await self._send({"type": "subscribed", "streams": sorted(self.streams), "server_time": utc_now_iso()})
        # Catch-up per stream from the provided cursor (bounded pages).
        for stream in streams:
            await self._catch_up(str(stream), cursors.get(str(stream)))

    async def _catch_up(self, stream: str, cursor: str | None) -> None:
        pages = 0
        while pages < 200:
            page = self.service.read_events(stream, after=cursor, limit=200, filters=self.filters)
            for event in page["events"]:
                await self._deliver(event)
            cursor = page["next_cursor"]
            pages += 1
            if not page["has_more"]:
                break
        await self._send({"type": "caught_up", "stream": stream, "cursor": cursor})

    async def _handle_unsubscribe(self, frame: dict[str, Any]) -> None:
        streams = {str(s) for s in (frame.get("streams") or [])}
        self.streams -= streams
        if self.sub is not None:
            self.service.broker.update(self.sub.id, set(self.streams), self._predicate())
        await self._send({"type": "unsubscribed", "streams": sorted(self.streams)})

    async def _handle_publish(self, frame: dict[str, Any]) -> None:
        self._require_role("worker")
        from .rest import _kind_for_role

        result = self.service.publish(
            stream=frame.get("stream"),
            type=frame.get("event_type") or frame.get("event") or frame.get("kind"),
            data=frame.get("data") or {},
            source_id=frame.get("source_id") or self.principal.label,
            source_kind=frame.get("source_kind") or _kind_for_role(self.principal.role),
            correlation_id=frame.get("correlation_id"),
            idempotency_key=frame.get("idempotency_key"),
        )
        await self._send({"type": "ack", "ack_id": frame.get("ack_id"), **result})

    async def _handle_ingest(self, frame: dict[str, Any]) -> None:
        self._require_role("worker")
        result = self.service.ingest_transcript(
            engine=frame.get("engine", "external"),
            text=frame.get("text", ""),
            correlation_id=frame.get("correlation_id"),
            confidence=float(frame.get("confidence", 0.9)),
            is_final=bool(frame.get("is_final", True)),
            language=frame.get("language", "en"),
            source_kind=frame.get("source_kind", "operator"),
            expected_tts_text=frame.get("expected_tts_text"),
            resolve=bool(frame.get("resolve", True)),
            audio_meta=frame.get("audio_meta"),
        )
        await self._send({"type": "ingest_result", "ack_id": frame.get("ack_id"), "result": result})

    async def _handle_cursor(self, frame: dict[str, Any]) -> None:
        action = frame.get("action")
        stream = frame.get("stream")
        consumer = frame.get("consumer")
        if action == "commit":
            self._require_role("worker")
            result = self.service.cursor_commit(stream, consumer, frame.get("token"), frame.get("reason", "processed"))
        elif action == "reposition":
            self._require_role("operator")
            result = self.service.cursor_reposition(
                stream, consumer, token=frame.get("token"), seq=frame.get("seq"),
                reason=frame.get("reason", "ws reposition"), operator=self.principal.label,
                allow_replay=bool(frame.get("allow_replay", False)), allow_skip=bool(frame.get("allow_skip", False)),
            )
        elif action == "reset":
            self._require_role("operator")
            result = self.service.cursor_reset(stream, consumer, to=frame.get("to", "start"), reason=frame.get("reason", ""), operator=self.principal.label)
        elif action == "get":
            self._require_role("viewer")
            result = self.service.cursor_get(stream, consumer)
        else:
            raise ValidationError(f"unknown cursor action: {action!r}")
        await self._send({"type": "cursor_result", "action": action, "result": result, "ref": frame.get("ack_id")})

    async def _deliver(self, event: dict[str, Any]) -> None:
        stream = event.get("stream")
        seq = event.get("seq") or 0
        if seq <= self.delivered_seq.get(stream, 0):
            return  # dedupe across the catch-up/live boundary
        self.delivered_seq[stream] = seq
        await self._send({"type": "event", "event": event})

    async def _writer(self) -> None:
        assert self.sub is not None
        while True:
            event = await self.sub.queue.get()
            await self._deliver(event.to_dict())

    async def _pinger(self) -> None:
        while True:
            await asyncio.sleep(20)
            try:
                await self._send({"type": "ping", "server_time": utc_now_iso()})
            except Exception:
                return

    def teardown(self) -> None:
        if self.sub is not None:
            self.service.broker.unsubscribe(self.sub.id)
            self.sub = None
