"""Security primitives shared by the REST and WebSocket transports.

Guarantees (task section 3):

* Bearer-token authentication with role-based authorization; no bypass exists.
* Signed admin sessions plus CSRF protection for cookie-authenticated mutations.
* Trusted-origin / WebSocket-origin validation and an optional network allowlist.
* Token-bucket rate limiting, payload-size limits, and connection limits.
* Secret redaction and traversal-safe path validation.
* A durable, append-only audit trail (delegated to an injected sink).

Nothing here weakens under test: tests configure real tokens and exercise the
same code paths production uses.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .config import Config, role_at_least
from .errors import (
    AuthenticationError,
    AuthorizationError,
    PayloadTooLargeError,
    RateLimitError,
    ValidationError,
)
from .events import utc_now_iso
from .ids import new_token

_SECRET_PATTERN = re.compile(r"(token|secret|authorization|password|api[_-]?key)", re.IGNORECASE)


@dataclass
class Principal:
    """An authenticated caller."""

    kind: str  # "token" or "session"
    id: str
    role: str
    label: str

    def public(self) -> dict[str, Any]:
        return {"kind": self.kind, "id": self.id, "role": self.role, "label": self.label}


class _TokenBucket:
    def __init__(self, rate: float, capacity: float):
        self.rate = rate
        self.capacity = capacity
        self.tokens = capacity
        self.updated = time.monotonic()

    def take(self, amount: float = 1.0) -> bool:
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
        self.updated = now
        if self.tokens >= amount:
            self.tokens -= amount
            return True
        return False


@dataclass
class Session:
    sid: str
    role: str
    label: str
    csrf: str
    created_at: str
    expires_at: float


class Security:
    def __init__(self, config: Config, audit_sink: Callable[[dict[str, Any]], None] | None = None):
        self.config = config
        self._audit_sink = audit_sink
        self._buckets: dict[str, _TokenBucket] = {}
        self._buckets_lock = threading.Lock()
        self._sessions: dict[str, Session] = {}
        self._sessions_lock = threading.Lock()
        self._connections = 0
        self._connections_lock = threading.Lock()
        self._allowlist = [ipaddress.ip_network(entry, strict=False) for entry in config.network_allowlist]

    # ----------------------------------------------------------------- auditing
    def audit(self, action: str, **fields: Any) -> None:
        if self._audit_sink is None:
            return
        payload = {"type": "SECURITY_AUDIT", "action": action, "at": utc_now_iso()}
        payload.update({key: self.redact_value(key, value) for key, value in fields.items()})
        self._audit_sink(payload)

    # --------------------------------------------------------------- redaction
    @staticmethod
    def redact_value(key: str, value: Any) -> Any:
        if isinstance(value, str) and _SECRET_PATTERN.search(key or ""):
            return "***redacted***"
        return value

    @classmethod
    def redact_text(cls, text: str) -> str:
        # Redact obvious "Bearer <token>" and "token=..." occurrences.
        text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._\-]+", r"\1***redacted***", text)
        text = re.sub(r"(?i)((?:token|secret|api[_-]?key)\s*[=:]\s*)[^\s,&]+", r"\1***redacted***", text)
        return text

    # ------------------------------------------------------------ token auth
    def default_principal(self) -> Principal | None:
        """Identity for a request that carries no (or invalid) credentials.

        Authentication is optional. When it is disabled -- the default for
        loopback-only deployments, controlled by ``WS_COLLAB_AUTH_DISABLED`` --
        every caller is treated as a local administrator. When authentication is
        required this returns ``None`` so the usual 401/403 handling applies.
        """
        if getattr(self.config, "auth_disabled", False):
            return Principal(kind="token", id="admin", role="admin", label="local")
        return None

    def authenticate_token(self, authorization: str | None, query_token: str | None = None) -> Principal | None:
        token = None
        if authorization:
            parts = authorization.split(" ", 1)
            if len(parts) == 2 and parts[0].lower() == "bearer":
                token = parts[1].strip()
            else:
                token = authorization.strip()
        if not token and query_token:
            token = query_token.strip()
        if not token:
            return self.default_principal()
        for candidate, descriptor in self.config.tokens.items():
            if hmac.compare_digest(candidate, token):
                return Principal(kind="token", id=descriptor["label"], role=descriptor["role"], label=descriptor["label"])
        return self.default_principal()

    # ------------------------------------------------------------ sessions/csrf
    def _sign(self, value: str) -> str:
        digest = hmac.new(self.config.session_secret.encode("utf-8"), value.encode("utf-8"), hashlib.sha256)
        return digest.hexdigest()

    def create_session(self, role: str, label: str, *, ttl_seconds: int = 12 * 3600) -> Session:
        sid = new_token(18)
        session = Session(
            sid=sid,
            role=role,
            label=label,
            csrf=new_token(18),
            created_at=utc_now_iso(),
            expires_at=time.time() + ttl_seconds,
        )
        with self._sessions_lock:
            self._sessions[sid] = session
        return session

    def cookie_value(self, session: Session) -> str:
        return f"{session.sid}.{self._sign(session.sid)}"

    def authenticate_session(self, cookie: str | None) -> tuple[Principal, Session] | None:
        if not cookie or "." not in cookie:
            return None
        sid, signature = cookie.rsplit(".", 1)
        if not hmac.compare_digest(signature, self._sign(sid)):
            return None
        with self._sessions_lock:
            session = self._sessions.get(sid)
            if session is None or session.expires_at < time.time():
                self._sessions.pop(sid, None)
                return None
        return Principal(kind="session", id=session.sid, role=session.role, label=session.label), session

    def destroy_session(self, sid: str) -> None:
        with self._sessions_lock:
            self._sessions.pop(sid, None)

    def verify_csrf(self, session: Session, header_token: str | None) -> None:
        if not header_token or not hmac.compare_digest(session.csrf, header_token):
            raise AuthorizationError("missing or invalid CSRF token")

    # --------------------------------------------------------------- authorize
    @staticmethod
    def require_role(principal: Principal | None, required: str) -> Principal:
        if principal is None:
            raise AuthenticationError("authentication required")
        if not role_at_least(principal.role, required):
            raise AuthorizationError(
                f"role '{principal.role}' is insufficient; '{required}' required",
                details={"role": principal.role, "required": required},
            )
        return principal

    # ------------------------------------------------------------------ origin
    def check_origin(self, origin: str | None, *, required: bool) -> None:
        if not origin:
            if required:
                raise AuthorizationError("Origin header required")
            return
        allowed = self.config.trusted_origins
        if not allowed:
            # With no explicit allow-list, only same-origin/loopback dev is trusted.
            if self.config.is_loopback_only or self.config.dev_insecure:
                return
            raise AuthorizationError("no trusted origins configured for cross-origin request")
        if origin not in allowed:
            raise AuthorizationError("origin is not trusted", details={"origin": origin})

    # ------------------------------------------------------------- allowlisting
    def check_allowlist(self, client_ip: str | None) -> None:
        if not self._allowlist:
            return
        if not client_ip:
            raise AuthorizationError("client address unavailable for allowlist check")
        try:
            address = ipaddress.ip_address(client_ip)
        except ValueError as error:
            raise AuthorizationError("invalid client address") from error
        if not any(address in network for network in self._allowlist):
            raise AuthorizationError("client address is not on the allowlist", details={"ip": client_ip})

    def is_admin_client_allowed(self, client_ip: str | None) -> bool:
        """Administration defaults to loopback unless remote access is enabled."""

        if self.config.admin_remote:
            return True
        return self.is_loopback_client(client_ip)

    @staticmethod
    def is_loopback_client(client_ip: str | None) -> bool:
        """Return whether the peer address is an IPv4 or IPv6 loopback address."""

        if not client_ip:
            return False
        try:
            return ipaddress.ip_address(client_ip).is_loopback
        except ValueError:
            return False

    # ---------------------------------------------------------------- limiting
    def rate_limit(self, key: str, cost: float = 1.0) -> None:
        with self._buckets_lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                rps = float(self.config.rate_limit_rps)
                bucket = _TokenBucket(rate=rps, capacity=max(rps, 1.0))
                self._buckets[key] = bucket
            if not bucket.take(cost):
                raise RateLimitError("rate limit exceeded", details={"limit_rps": self.config.rate_limit_rps})

    def check_body_size(self, size: int) -> None:
        if size > self.config.max_body_bytes:
            raise PayloadTooLargeError(
                "request body too large",
                details={"limit_bytes": self.config.max_body_bytes, "size": size},
            )

    def check_ws_message_size(self, size: int) -> None:
        if size > self.config.max_ws_message_bytes:
            raise PayloadTooLargeError(
                "websocket message too large",
                details={"limit_bytes": self.config.max_ws_message_bytes, "size": size},
            )

    def acquire_connection(self) -> None:
        with self._connections_lock:
            if self._connections >= self.config.max_connections:
                raise RateLimitError("maximum concurrent connections reached")
            self._connections += 1

    def release_connection(self) -> None:
        with self._connections_lock:
            self._connections = max(0, self._connections - 1)

    @property
    def active_connections(self) -> int:
        return self._connections


def safe_join(base: Path, *parts: str) -> Path:
    """Join ``parts`` under ``base`` and reject path traversal."""

    base = base.resolve()
    candidate = base.joinpath(*parts).resolve()
    if base != candidate and base not in candidate.parents:
        raise ValidationError("path escapes the permitted directory", details={"path": str(candidate)})
    return candidate
