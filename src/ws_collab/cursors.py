"""Persistent, movable cursor management (task section 6).

Cursors are durable checkpoints, not permanent barriers. Each ``(stream,
consumer)`` pair has its own position. Normal processing *commits* forward one
page at a time; operators may *reposition* (rewind for replay/verification or
skip forward with explicit authorization) or *reset* after a stream is repaired,
rotated, or truncated. Every change records the old and new position, stream,
consumer, timestamp, reason, operator, and the replay/skip risk it carries.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .errors import AuthorizationError, NotFoundError
from .events import utc_now_iso
from .ids import decode_cursor

RISK_NONE = "none"
RISK_REPLAY = "replay"  # rewind: events may be reprocessed
RISK_SKIP = "skip"  # forward jump: unprocessed events may be missed

_MAX_HISTORY = 100


@dataclass
class CursorPosition:
    stream: str
    consumer: str
    token: str
    seq: int
    updated_at: str
    reason: str
    operator: str
    history: list[dict[str, Any]] = field(default_factory=list)

    def public(self) -> dict[str, Any]:
        return {
            "stream": self.stream,
            "consumer": self.consumer,
            "token": self.token,
            "seq": self.seq,
            "updated_at": self.updated_at,
            "reason": self.reason,
            "operator": self.operator,
        }


def _seq_of(token: str) -> int:
    try:
        return int(decode_cursor(token).get("seq", 0))
    except ValueError:
        return 0


class CursorManager:
    """Durable per-consumer cursor store with an audited reposition API."""

    def __init__(self, directory: str | Path, audit_sink: Callable[[dict[str, Any]], None] | None = None):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "cursors.json"
        self._lock = threading.RLock()
        self._audit_sink = audit_sink
        self._cursors: dict[str, CursorPosition] = {}
        self._load()

    # ------------------------------------------------------------- persistence
    @staticmethod
    def _key(stream: str, consumer: str) -> str:
        return f"{stream}\x1f{consumer}"

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        for entry in payload.get("cursors", []):
            position = CursorPosition(
                stream=entry["stream"],
                consumer=entry["consumer"],
                token=entry.get("token", ""),
                seq=int(entry.get("seq", 0)),
                updated_at=entry.get("updated_at", utc_now_iso()),
                reason=entry.get("reason", ""),
                operator=entry.get("operator", "system"),
                history=list(entry.get("history", [])),
            )
            self._cursors[self._key(position.stream, position.consumer)] = position

    def _save(self) -> None:
        payload = {"cursors": [
            {
                "stream": position.stream,
                "consumer": position.consumer,
                "token": position.token,
                "seq": position.seq,
                "updated_at": position.updated_at,
                "reason": position.reason,
                "operator": position.operator,
                "history": position.history[-_MAX_HISTORY:],
            }
            for position in self._cursors.values()
        ]}
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    # -------------------------------------------------------------------- reads
    def get(self, stream: str, consumer: str) -> CursorPosition | None:
        with self._lock:
            return self._cursors.get(self._key(stream, consumer))

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [position.public() for position in self._cursors.values()]

    def history(self, stream: str, consumer: str) -> list[dict[str, Any]]:
        with self._lock:
            position = self._cursors.get(self._key(stream, consumer))
            if position is None:
                raise NotFoundError(f"no cursor for {stream}/{consumer}")
            return list(position.history)

    # ------------------------------------------------------------------- writes
    def _record(
        self,
        stream: str,
        consumer: str,
        token: str,
        *,
        action: str,
        reason: str,
        operator: str,
        risk: str,
    ) -> CursorPosition:
        key = self._key(stream, consumer)
        previous = self._cursors.get(key)
        old_seq = previous.seq if previous else 0
        old_token = previous.token if previous else ""
        new_seq = _seq_of(token)
        entry = {
            "action": action,
            "stream": stream,
            "consumer": consumer,
            "old_token": old_token,
            "new_token": token,
            "old_seq": old_seq,
            "new_seq": new_seq,
            "reason": reason,
            "operator": operator,
            "risk": risk,
            "at": utc_now_iso(),
        }
        history = (previous.history if previous else []) + [entry]
        position = CursorPosition(
            stream=stream,
            consumer=consumer,
            token=token,
            seq=new_seq,
            updated_at=entry["at"],
            reason=reason,
            operator=operator,
            history=history[-_MAX_HISTORY:],
        )
        self._cursors[key] = position
        self._save()
        if self._audit_sink is not None:
            self._audit_sink({"type": "CURSOR_MOVED", **entry})
        return position

    def commit(self, stream: str, consumer: str, token: str, *, reason: str = "processed", operator: str = "consumer") -> CursorPosition:
        """Advance a cursor after successful processing.

        Advancing forward or re-committing the same position is always allowed;
        moving backward is rejected here so callers use :meth:`reposition` (which
        records the replay risk) instead of silently rewinding.
        """

        with self._lock:
            previous = self._cursors.get(self._key(stream, consumer))
            new_seq = _seq_of(token)
            if previous and new_seq < previous.seq:
                raise AuthorizationError(
                    "commit would move the cursor backward; use reposition() to rewind",
                    details={"current_seq": previous.seq, "requested_seq": new_seq},
                )
            return self._record(stream, consumer, token, action="commit", reason=reason, operator=operator, risk=RISK_NONE)

    def reposition(
        self,
        stream: str,
        consumer: str,
        token: str,
        *,
        reason: str,
        operator: str,
        allow_replay: bool = False,
        allow_skip: bool = False,
    ) -> CursorPosition:
        """Operator-driven move with explicit authorization for risky directions."""

        with self._lock:
            previous = self._cursors.get(self._key(stream, consumer))
            old_seq = previous.seq if previous else 0
            new_seq = _seq_of(token)
            if new_seq < old_seq:
                if not allow_replay:
                    raise AuthorizationError(
                        "rewind requires allow_replay=true (events will be reprocessed)",
                        details={"current_seq": old_seq, "requested_seq": new_seq},
                    )
                risk = RISK_REPLAY
            elif new_seq > old_seq:
                if not allow_skip:
                    raise AuthorizationError(
                        "forward skip requires allow_skip=true (events will be missed)",
                        details={"current_seq": old_seq, "requested_seq": new_seq},
                    )
                risk = RISK_SKIP
            else:
                risk = RISK_NONE
            return self._record(stream, consumer, token, action="reposition", reason=reason, operator=operator, risk=risk)

    def reset(self, stream: str, consumer: str, token: str, *, reason: str, operator: str) -> CursorPosition:
        """Reset after a stream is replaced, repaired, rotated, or truncated."""

        with self._lock:
            return self._record(stream, consumer, token, action="reset", reason=reason, operator=operator, risk=RISK_REPLAY)
