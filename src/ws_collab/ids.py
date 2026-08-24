"""Sortable identifiers and opaque cursor encoding for WS_COLLAB.

Event identifiers are ULID-like: a 48-bit millisecond timestamp followed by 80
bits of randomness, rendered with Crockford base32. They are globally unique,
monotonically sortable by creation time, and require no external dependency.

Cursors are opaque, stable, base64url-encoded tokens describing a position in a
single stream. Encoding the stream name and a byte offset plus the last event id
lets the store detect rotation/truncation and recover from rotated tokens.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import threading
import time
from typing import Any

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

_lock = threading.Lock()
_last_ms = 0
_last_rand = 0


def _encode_crockford(value: int, length: int) -> str:
    chars = []
    for _ in range(length):
        chars.append(_CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


def new_event_id(now_ms: int | None = None) -> str:
    """Return a 26-character ULID-like identifier.

    Monotonicity is guaranteed within a process even when several ids are minted
    in the same millisecond: the random component is incremented instead of being
    regenerated, which keeps lexical ordering aligned with creation order.
    """

    global _last_ms, _last_rand
    with _lock:
        ms = now_ms if now_ms is not None else int(time.time() * 1000)
        if ms <= _last_ms:
            ms = _last_ms
            _last_rand = (_last_rand + 1) & ((1 << 80) - 1)
            if _last_rand == 0:  # extremely unlikely overflow; bump the clock
                ms += 1
                _last_ms = ms
                _last_rand = int.from_bytes(secrets.token_bytes(10), "big")
        else:
            _last_ms = ms
            _last_rand = int.from_bytes(secrets.token_bytes(10), "big")
        time_part = _encode_crockford(ms, 10)
        rand_part = _encode_crockford(_last_rand, 16)
        return time_part + rand_part


def event_id_timestamp_ms(event_id: str) -> int:
    """Recover the millisecond timestamp encoded in an event id."""

    value = 0
    for char in event_id[:10].upper():
        value = (value << 5) | _CROCKFORD.index(char)
    return value


def new_token(nbytes: int = 24) -> str:
    """Return a URL-safe random token (used for sessions and idempotency)."""

    return secrets.token_urlsafe(nbytes)


def encode_cursor(payload: dict[str, Any]) -> str:
    """Encode an opaque, stable cursor token from a small dict payload."""

    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(token: str) -> dict[str, Any]:
    """Decode a cursor token produced by :func:`encode_cursor`.

    Raises ``ValueError`` for malformed tokens so callers can respond with a
    controlled ``expired/rotated cursor`` recovery path instead of crashing.
    """

    if not isinstance(token, str) or not token:
        raise ValueError("empty cursor")
    padding = "=" * (-len(token) % 4)
    try:
        raw = base64.urlsafe_b64decode(token + padding)
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, json.JSONDecodeError) as error:
        raise ValueError(f"malformed cursor: {error}") from error
    if not isinstance(payload, dict):
        raise ValueError("cursor payload must be an object")
    return payload


def stable_hash(*parts: Any) -> str:
    """Return a short stable hash for idempotency/dedupe keys."""

    import hashlib

    hasher = hashlib.sha256()
    for part in parts:
        hasher.update(repr(part).encode("utf-8"))
        hasher.update(b"\x1f")
    return hasher.hexdigest()[:32]


def pid_token() -> str:
    """Return a token unique to this process (used to tag the single writer)."""

    return f"{os.getpid()}-{secrets.token_hex(4)}"
