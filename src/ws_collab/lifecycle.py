"""Host-owned, request-independent server lifecycle control."""

from __future__ import annotations

import inspect
import threading
from dataclasses import dataclass
from typing import Awaitable, Callable

from .errors import ConflictError

LifecycleCallback = Callable[[], Awaitable[None] | None]
RESTART_EXIT_CODE = 75


@dataclass(frozen=True)
class LifecycleReservation:
    action: str
    accepted: bool

    @property
    def status(self) -> str:
        return "scheduled" if self.accepted else "already-scheduled"


class LifecycleController:
    """Serialize lifecycle requests and invoke callbacks supplied by the host."""

    def __init__(
        self,
        *,
        shutdown: LifecycleCallback | None = None,
        restart: LifecycleCallback | None = None,
    ) -> None:
        self._callbacks = {"shutdown": shutdown, "restart": restart}
        self._pending: str | None = None
        self._lock = threading.Lock()

    @property
    def pending(self) -> str | None:
        with self._lock:
            return self._pending

    def reserve(self, action: str) -> LifecycleReservation:
        callback = self._callbacks.get(action)
        if callback is None:
            raise ConflictError(f"{action} unavailable in embedded host")
        with self._lock:
            if self._pending is None:
                self._pending = action
                return LifecycleReservation(action=action, accepted=True)
            if self._pending == action:
                return LifecycleReservation(action=action, accepted=False)
            raise ConflictError(
                f"{self._pending} already scheduled; cannot schedule {action}",
                details={"pending": self._pending, "requested": action},
            )

    async def execute(self, reservation: LifecycleReservation) -> None:
        """Run a newly accepted action after its acknowledgement was sent."""

        if not reservation.accepted:
            return
        callback = self._callbacks[reservation.action]
        if callback is None:  # Callback ownership cannot change after reserve().
            return
        result = callback()
        if inspect.isawaitable(result):
            await result
