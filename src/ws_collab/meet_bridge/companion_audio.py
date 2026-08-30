"""Bounded, serial outbound audio arbitration for the Meet companion."""

from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from typing import Any, Callable


def _estimated_duration(text: str, rate: Any) -> float:
    try:
        speed = max(0.25, float(rate or 1.0))
    except (TypeError, ValueError):
        speed = 1.0
    return max(0.3, len(text) * 0.06 / speed)


class CompanionAudioArbiter:
    """Serialize agent speech and interjectors onto one companion mic track."""

    def __init__(
        self,
        readiness: Callable[[str | None], dict[str, Any]],
        playback: Callable[[dict[str, Any], threading.Event], Any],
        cancel_playback: Callable[[str], None],
        *,
        max_pending: int = 8,
    ) -> None:
        self._readiness = readiness
        self._playback = playback
        self._cancel_playback = cancel_playback
        self._max_pending = max(1, int(max_pending))
        self._pending: deque[dict[str, Any]] = deque()
        self._condition = threading.Condition(threading.RLock())
        self._stop = False
        self._worker: threading.Thread | None = None
        self._generation = 0
        self._current: dict[str, Any] | None = None
        self._current_cancel = threading.Event()
        self._counters = {
            "accepted": 0,
            "sent": 0,
            "completed": 0,
            "dropped": 0,
            "rejected": 0,
            "cancelled": 0,
            "failed": 0,
        }
        self._last_utterance: dict[str, Any] | None = None
        self._utterances: dict[str, dict[str, Any]] = {}
        self._last_error: str | None = None
        self._last_destination: dict[str, Any] | None = None

    def start(self) -> None:
        with self._condition:
            if self._worker and self._worker.is_alive():
                return
            self._stop = False
            self._worker = threading.Thread(
                target=self._run,
                name="meet-companion-audio",
                daemon=True,
            )
            self._worker.start()

    def stop(self) -> None:
        self.invalidate("bridge stopping")
        with self._condition:
            self._stop = True
            self._condition.notify_all()

    def submit(
        self,
        *,
        kind: str,
        text: str = "",
        meeting_url: str | None = None,
        source: str = "virtual-agent-tts",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if kind not in {"speech", "interject"}:
            return self._reject("unsupported companion audio kind")
        if kind == "speech" and not str(text or "").strip():
            return self._reject("speech text is required")
        ready = self._readiness(meeting_url)
        if not ready.get("ready"):
            return self._reject(str(ready.get("error") or "companion is not attached and ready"), ready)
        item = {
            "id": str((metadata or {}).get("utterance_id") or uuid.uuid4().hex),
            "kind": kind,
            "text": str(text or "").strip(),
            "source": source,
            "meetingUrl": ready.get("meetingUrl"),
            "tabId": ready.get("tabId"),
            "generation": self._generation,
            "enqueuedAt": time.time(),
            "metadata": dict(metadata or {}),
        }
        item["estimatedDurationSeconds"] = (
            _estimated_duration(item["text"], item["metadata"].get("rate"))
            if kind == "speech"
            else 0.3
        )
        with self._condition:
            if len(self._pending) >= self._max_pending:
                self._counters["dropped"] += 1
                self._counters["rejected"] += 1
                self._last_error = f"companion audio queue full ({self._max_pending})"
                return {
                    "ok": False,
                    "accepted": False,
                    "reason": "queue-full",
                    "error": self._last_error,
                    "destination": self._destination(ready),
                    "status": self.status(),
                }
            self._pending.append(item)
            self._utterances[item["id"]] = {
                **self._public_item(item),
                "state": "queued",
                "terminal": False,
                "error": None,
            }
            self._trim_utterances()
            self._counters["accepted"] += 1
            position = len(self._pending) - 1 + (1 if self._current else 0)
            ahead = [*([] if self._current is None else [self._current]), *list(self._pending)[:-1]]
            queue_delay = sum(float(entry.get("estimatedDurationSeconds") or 0.0) for entry in ahead)
            self._condition.notify()
        return {
            "ok": True,
            "accepted": True,
            "id": item["id"],
            "queuePosition": position,
            "estimatedDurationSeconds": item["estimatedDurationSeconds"],
            "estimatedQueueDelaySeconds": queue_delay,
            "destination": self._destination(ready),
            "artifact": {"source": source, "kind": kind},
        }

    def invalidate(self, reason: str) -> int:
        with self._condition:
            self._generation += 1
            dropped = len(self._pending)
            for item in self._pending:
                self._record_terminal(item, "cancelled", reason)
            self._pending.clear()
            self._counters["cancelled"] += dropped
            if self._current is not None and not self._current_cancel.is_set():
                self._current_cancel.set()
                self._counters["cancelled"] += 1
            self._last_error = reason
            self._condition.notify_all()
        try:
            self._cancel_playback(reason)
        except Exception:
            pass
        return dropped

    def cancel(self, item_id: str) -> bool:
        with self._condition:
            for item in list(self._pending):
                if item.get("id") == item_id:
                    self._pending.remove(item)
                    self._counters["cancelled"] += 1
                    self._last_error = "utterance cancelled"
                    self._record_terminal(item, "cancelled", self._last_error)
                    self._condition.notify_all()
                    return True
            if (
                self._current is not None
                and self._current.get("id") == item_id
                and not self._current_cancel.is_set()
            ):
                self._current_cancel.set()
                self._counters["cancelled"] += 1
                self._last_error = "utterance cancelled"
                current = True
            else:
                current = False
        if current:
            try:
                self._cancel_playback("utterance cancelled")
            except Exception:
                pass
        return current

    def utterance_status(self, item_id: str, *, wait_seconds: float = 0.0) -> dict[str, Any]:
        deadline = time.monotonic() + max(0.0, min(float(wait_seconds), 60.0))
        with self._condition:
            while True:
                state = self._utterances.get(item_id)
                if state is None:
                    return {
                        "ok": False,
                        "id": item_id,
                        "state": "unknown",
                        "terminal": True,
                        "error": "unknown companion utterance",
                    }
                if state.get("terminal") or time.monotonic() >= deadline:
                    return {"ok": True, **dict(state)}
                self._condition.wait(deadline - time.monotonic())

    def report_duration(self, item_id: str, duration: float) -> None:
        """Publish the synthesized clip duration to bounded status waiters."""

        value = max(0.0, float(duration))
        with self._condition:
            state = self._utterances.get(item_id)
            if state is None or state.get("terminal"):
                return
            state["audioDurationSeconds"] = value
            if self._current is not None and self._current.get("id") == item_id:
                self._current["audioDurationSeconds"] = value
            self._condition.notify_all()

    def status(self) -> dict[str, Any]:
        ready = self._readiness(None)
        with self._condition:
            current = self._public_item(self._current)
            payload = {
                "destination": "companion",
                "companionReady": bool(ready.get("ready")),
                "companion": self._destination(ready),
                "capacity": self._max_pending,
                "queued": len(self._pending),
                "speaking": self._current is not None,
                "current": current,
                **self._counters,
                "lastUtterance": dict(self._last_utterance) if self._last_utterance else None,
                "lastError": self._last_error or ready.get("error"),
                "lastDestination": dict(self._last_destination) if self._last_destination else None,
            }
        return payload

    def process_next(self) -> bool:
        with self._condition:
            if not self._pending:
                return False
            item = self._pending.popleft()
            self._current = item
            self._current_cancel = threading.Event()
            cancel_event = self._current_cancel
            self._utterances[item["id"]] = {
                **self._utterances[item["id"]],
                "state": "playing",
                "startedAt": time.time(),
            }
            self._condition.notify_all()
        ready = self._readiness(item.get("meetingUrl"))
        if (
            not ready.get("ready")
            or ready.get("meetingUrl") != item.get("meetingUrl")
            or ready.get("tabId") != item.get("tabId")
            or item.get("generation") != self._generation
        ):
            self._finish(item, error=str(ready.get("error") or "stale companion destination"), dropped=True)
            return True
        error = None
        try:
            with self._condition:
                self._counters["sent"] += 1
                self._last_destination = self._destination(ready)
            self._playback(item, cancel_event)
            if cancel_event.is_set() or item.get("generation") != self._generation:
                error = "cancelled before completion"
            else:
                with self._condition:
                    self._counters["completed"] += 1
                    self._last_error = None
        except Exception as exc:  # noqa: BLE001
            error = str(exc)
            with self._condition:
                if not cancel_event.is_set() and item.get("generation") == self._generation:
                    self._counters["failed"] += 1
                self._last_error = error
        self._finish(item, error=error)
        return True

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._pending and not self._stop:
                    self._condition.wait(timeout=0.5)
                if self._stop:
                    return
            self.process_next()

    def _finish(self, item: dict[str, Any], *, error: str | None, dropped: bool = False) -> None:
        with self._condition:
            if dropped:
                self._counters["dropped"] += 1
                self._last_error = error
            self._last_utterance = {
                **self._public_item(item),
                "completedAt": time.time(),
                "error": error,
            }
            state = (
                "cancelled"
                if self._current_cancel.is_set() or item.get("generation") != self._generation
                else "failed"
                if error
                else "completed"
            )
            self._record_terminal(item, state, error)
            if self._current is item:
                self._current = None
            self._condition.notify_all()

    def _record_terminal(self, item: dict[str, Any], state: str, error: str | None) -> None:
        self._utterances[item["id"]] = {
            **self._utterances.get(item["id"], self._public_item(item) or {}),
            "state": state,
            "terminal": True,
            "completedAt": time.time(),
            "error": error,
        }
        self._trim_utterances()

    def _trim_utterances(self) -> None:
        while len(self._utterances) > 256:
            oldest = next(iter(self._utterances))
            if not self._utterances[oldest].get("terminal"):
                break
            self._utterances.pop(oldest, None)

    def _reject(self, error: str, ready: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._condition:
            self._counters["rejected"] += 1
            self._last_error = error
        return {
            "ok": False,
            "accepted": False,
            "reason": "companion-not-ready",
            "error": error,
            "destination": self._destination(ready or {}),
        }

    @staticmethod
    def _destination(ready: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "companion",
            "meetingUrl": ready.get("meetingUrl"),
            "tabId": ready.get("tabId"),
            "ready": bool(ready.get("ready")),
            "state": ready.get("state"),
            "syntheticMicReady": bool(ready.get("syntheticMicReady")),
            "error": ready.get("error"),
        }

    @staticmethod
    def _public_item(item: dict[str, Any] | None) -> dict[str, Any] | None:
        if item is None:
            return None
        return {
            key: item.get(key)
            for key in (
                "id", "kind", "text", "source", "meetingUrl", "tabId", "enqueuedAt",
                "estimatedDurationSeconds", "audioDurationSeconds",
            )
        }
