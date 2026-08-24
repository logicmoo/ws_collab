"""Structured error types shared by the REST and WebSocket transports.

A single :class:`WsCollabError` hierarchy guarantees that a failure surfaced over
REST (as an HTTP status + JSON body) and the same failure surfaced over WS (as a
structured error frame) carry identical ``code`` and ``message`` values.
"""

from __future__ import annotations

from typing import Any


class WsCollabError(Exception):
    """Base error carrying a stable machine code and an HTTP status."""

    code = "ws_collab_error"
    http_status = 500

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {"error": {"code": self.code, "message": self.message}}
        if self.details:
            body["error"]["details"] = self.details
        return body


class ValidationError(WsCollabError):
    code = "validation_error"
    http_status = 400


class AuthenticationError(WsCollabError):
    code = "authentication_required"
    http_status = 401


class AuthorizationError(WsCollabError):
    code = "forbidden"
    http_status = 403


class NotFoundError(WsCollabError):
    code = "not_found"
    http_status = 404


class ConflictError(WsCollabError):
    code = "conflict"
    http_status = 409


class CursorError(WsCollabError):
    """Raised for expired, rotated, or malformed cursors.

    Carries a ``recovery`` cursor in ``details`` so clients can resume from a
    safe position rather than losing their place in the stream.
    """

    code = "cursor_invalid"
    http_status = 409


class RateLimitError(WsCollabError):
    code = "rate_limited"
    http_status = 429


class PayloadTooLargeError(WsCollabError):
    code = "payload_too_large"
    http_status = 413


class ConfigurationError(WsCollabError):
    code = "configuration_error"
    http_status = 500
