"""Worker registry and health monitoring (task section 16).

The monitor tracks the latest check-in for each worker and derives a state from
configurable thresholds: ``ok`` -> ``warn`` -> ``overdue`` -> ``unresponsive``.
State transitions raise deduplicated alerts, escalate as the situation worsens,
emit recovery events on check-in, and flag team-wide failure when every worker
has gone quiet. Workers are only ever reported as overdue/unresponsive -- never
"terminated" -- unless termination is independently confirmed, because the last
worker able to report may itself be the final responsive observer.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .config import Config
from .events import (
    ALERT_RAISED,
    ALERT_RECOVERED,
    STREAM_ALERTS,
    STREAM_STATUSES,
    WORKER_REGISTERED,
    WORKER_STATE_CHANGED,
    WORKER_STATUS,
)

STATE_OK = "ok"
STATE_WARN = "warn"
STATE_OVERDUE = "overdue"
STATE_UNRESPONSIVE = "unresponsive"
STATE_TERMINATED = "terminated"

_STATE_RANK = {STATE_OK: 0, STATE_WARN: 1, STATE_OVERDUE: 2, STATE_UNRESPONSIVE: 3, STATE_TERMINATED: 4}

PublishFn = Callable[..., dict[str, Any]]


@dataclass
class WorkerRecord:
    worker_id: str
    task: str = ""
    registered_at: float = field(default_factory=time.time)
    last_status_at: float = field(default_factory=time.time)
    last_status: str = ""
    last_state: str = STATE_OK
    last_alert_state: str | None = None
    errors: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    last_conversation_id: str | None = None
    last_audio_id: str | None = None
    terminated_confirmed: bool = False

    def age(self, now: float) -> float:
        return max(0.0, now - self.last_status_at)


class WorkerMonitor:
    def __init__(self, config: Config, publish: PublishFn, announce: Callable[[str, str], None] | None = None):
        self.config = config
        self._publish = publish
        self._announce = announce
        self._workers: dict[str, WorkerRecord] = {}
        self._lock = threading.RLock()
        self._team_alerted = False

    # ------------------------------------------------------------- registration
    def register(self, worker_id: str, task: str = "", meta: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            record = self._workers.get(worker_id)
            now = time.time()
            if record is None:
                record = WorkerRecord(worker_id=worker_id, registered_at=now)
                self._workers[worker_id] = record
            record.task = task or record.task
            record.last_status_at = now
            record.terminated_confirmed = False
            if meta:
                record.meta.update(meta)
        self._publish(
            stream=STREAM_STATUSES,
            type=WORKER_REGISTERED,
            data={"worker_id": worker_id, "task": task, "meta": meta or {}},
            source_id=worker_id,
            source_kind="worker",
        )
        return self.snapshot(worker_id)

    def record_status(
        self,
        worker_id: str,
        status: str,
        data: dict[str, Any] | None = None,
        *,
        errors: list[str] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            record = self._workers.get(worker_id)
            if record is None:
                record = WorkerRecord(worker_id=worker_id)
                self._workers[worker_id] = record
            record.last_status_at = time.time()
            record.last_status = status
            record.terminated_confirmed = False
            if errors:
                record.errors = (record.errors + errors)[-25:]
            if data:
                record.meta.update({k: v for k, v in data.items() if k in {"task", "phase", "progress"}})
                if data.get("last_conversation_id"):
                    record.last_conversation_id = data["last_conversation_id"]
                if data.get("last_audio_id"):
                    record.last_audio_id = data["last_audio_id"]
        self._publish(
            stream=STREAM_STATUSES,
            type=WORKER_STATUS,
            data={"worker_id": worker_id, "status": status, **(data or {})},
            source_id=worker_id,
            source_kind="worker",
        )
        # A fresh check-in may clear an outstanding alert immediately.
        self.evaluate()
        return self.snapshot(worker_id)

    def confirm_terminated(self, worker_id: str, operator: str = "operator") -> dict[str, Any]:
        with self._lock:
            record = self._workers.get(worker_id)
            if record is None:
                record = WorkerRecord(worker_id=worker_id)
                self._workers[worker_id] = record
            record.terminated_confirmed = True
            record.last_state = STATE_TERMINATED
        self._publish(
            stream=STREAM_ALERTS,
            type=WORKER_STATE_CHANGED,
            data={"worker_id": worker_id, "state": STATE_TERMINATED, "confirmed_by": operator},
            source_id="monitor",
            source_kind="system",
        )
        return self.snapshot(worker_id)

    # --------------------------------------------------------------- evaluation
    def _state_for(self, record: WorkerRecord, now: float) -> str:
        if record.terminated_confirmed:
            return STATE_TERMINATED
        age = record.age(now)
        if age > self.config.worker_unresponsive_seconds:
            return STATE_UNRESPONSIVE
        if age > self.config.worker_overdue_seconds:
            return STATE_OVERDUE
        if age > self.config.worker_warn_seconds:
            return STATE_WARN
        return STATE_OK

    def evaluate(self) -> list[dict[str, Any]]:
        """Run one bounded monitoring cycle and return any alerts emitted."""

        emitted: list[dict[str, Any]] = []
        with self._lock:
            now = time.time()
            records = list(self._workers.values())
            for record in records:
                new_state = self._state_for(record, now)
                if new_state != record.last_state:
                    previous = record.last_state
                    record.last_state = new_state
                    self._publish(
                        stream=STREAM_ALERTS,
                        type=WORKER_STATE_CHANGED,
                        data={
                            "worker_id": record.worker_id,
                            "from": previous,
                            "to": new_state,
                            "age_seconds": round(record.age(now), 1),
                        },
                        source_id="monitor",
                        source_kind="system",
                    )
                    emitted.append(self._maybe_alert(record, new_state, now))
            emitted.extend(self._check_team_failure(records, now))
        return [event for event in emitted if event]

    def _maybe_alert(self, record: WorkerRecord, state: str, now: float) -> dict[str, Any] | None:
        if state in (STATE_OK, STATE_TERMINATED):
            if record.last_alert_state not in (None, STATE_OK):
                record.last_alert_state = STATE_OK
                event = self._publish(
                    stream=STREAM_ALERTS,
                    type=ALERT_RECOVERED,
                    data={"worker_id": record.worker_id, "state": state},
                    source_id="monitor",
                    source_kind="system",
                )
                return event
            record.last_alert_state = STATE_OK
            return None
        # Deduplicate: only alert when the alert-worthy state actually changes.
        if record.last_alert_state == state:
            return None
        record.last_alert_state = state
        severity = {"warn": "warning", "overdue": "warning", "unresponsive": "danger"}[state]
        if self._announce and state == STATE_UNRESPONSIVE:
            self._announce(record.worker_id, f"Worker {record.worker_id} is unresponsive")
        return self._publish(
            stream=STREAM_ALERTS,
            type=ALERT_RAISED,
            data={
                "worker_id": record.worker_id,
                "state": state,
                "severity": severity,
                "task": record.task,
                "age_seconds": round(record.age(now), 1),
                "confirmation_required": state == STATE_UNRESPONSIVE,
            },
            source_id="monitor",
            source_kind="system",
        )

    def _check_team_failure(self, records: list[WorkerRecord], now: float) -> list[dict[str, Any]]:
        active = [r for r in records if not r.terminated_confirmed]
        if not active:
            return []
        all_down = all(self._state_for(r, now) in (STATE_OVERDUE, STATE_UNRESPONSIVE) for r in active)
        events: list[dict[str, Any]] = []
        if all_down and not self._team_alerted:
            self._team_alerted = True
            events.append(
                self._publish(
                    stream=STREAM_ALERTS,
                    type=ALERT_RAISED,
                    data={
                        "scope": "team",
                        "severity": "danger",
                        "message": "All workers are overdue or unresponsive",
                        "worker_count": len(active),
                    },
                    source_id="monitor",
                    source_kind="system",
                )
            )
        elif not all_down and self._team_alerted:
            self._team_alerted = False
            events.append(
                self._publish(
                    stream=STREAM_ALERTS,
                    type=ALERT_RECOVERED,
                    data={"scope": "team", "message": "At least one worker is responsive again"},
                    source_id="monitor",
                    source_kind="system",
                )
            )
        return events

    # ------------------------------------------------------------------ queries
    def snapshot(self, worker_id: str) -> dict[str, Any]:
        with self._lock:
            record = self._workers.get(worker_id)
            if record is None:
                return {}
            now = time.time()
            return self._public(record, now)

    def list_workers(self) -> list[dict[str, Any]]:
        with self._lock:
            now = time.time()
            return [self._public(record, now) for record in self._workers.values()]

    def _public(self, record: WorkerRecord, now: float) -> dict[str, Any]:
        return {
            "worker_id": record.worker_id,
            "task": record.task,
            "state": self._state_for(record, now),
            "last_status": record.last_status,
            "last_status_age_seconds": round(record.age(now), 1),
            "registered_at": record.registered_at,
            "last_status_at": record.last_status_at,
            "errors": list(record.errors),
            "meta": dict(record.meta),
            "last_conversation_id": record.last_conversation_id,
            "last_audio_id": record.last_audio_id,
            "terminated_confirmed": record.terminated_confirmed,
        }

    def rebuild_from_events(self, statuses: list[dict[str, Any]]) -> None:
        """Rehydrate the registry from recent durable status events after restart."""

        with self._lock:
            for raw in statuses:
                data = raw.get("data", {})
                worker_id = data.get("worker_id")
                if not worker_id:
                    continue
                record = self._workers.setdefault(worker_id, WorkerRecord(worker_id=worker_id))
                if raw.get("type") == WORKER_REGISTERED:
                    record.task = data.get("task", record.task)
                record.last_status = data.get("status", record.last_status)
