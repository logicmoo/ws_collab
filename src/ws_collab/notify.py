"""In-process publish/subscribe broker bridging sync writes to async consumers.

The durable store is synchronous; WebSocket consumers are asynchronous. The
broker lets any thread ``publish`` an :class:`~ws_collab.events.Event` after it is
durably appended, and delivers it to each subscriber's asyncio queue using that
subscriber's own event loop, so no cross-thread races occur.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from .events import Event


@dataclass
class Subscription:
    id: int
    streams: set[str] | None  # None means "all streams"
    queue: "asyncio.Queue[Event]"
    loop: asyncio.AbstractEventLoop
    predicate: Callable[[Event], bool] | None = None
    dropped: int = 0
    delivered: int = 0

    def matches(self, event: Event) -> bool:
        if self.streams is not None and event.stream not in self.streams:
            return False
        if self.predicate is not None and not self.predicate(event):
            return False
        return True


class Broker:
    def __init__(self, max_queue: int = 2000):
        self._subs: dict[int, Subscription] = {}
        self._lock = threading.Lock()
        self._counter = 0
        self._max_queue = max_queue

    def subscribe(
        self,
        streams: set[str] | None,
        loop: asyncio.AbstractEventLoop,
        predicate: Callable[[Event], bool] | None = None,
    ) -> Subscription:
        with self._lock:
            self._counter += 1
            sub = Subscription(
                id=self._counter,
                streams=streams,
                queue=asyncio.Queue(maxsize=self._max_queue),
                loop=loop,
                predicate=predicate,
            )
            self._subs[sub.id] = sub
            return sub

    def unsubscribe(self, sub_id: int) -> None:
        with self._lock:
            self._subs.pop(sub_id, None)

    def update(self, sub_id: int, streams: set[str] | None, predicate: Callable[[Event], bool] | None = None) -> None:
        with self._lock:
            sub = self._subs.get(sub_id)
            if sub is not None:
                sub.streams = streams
                sub.predicate = predicate

    def update_streams(self, sub_id: int, streams: set[str] | None) -> None:
        with self._lock:
            sub = self._subs.get(sub_id)
            if sub is not None:
                sub.streams = streams

    def publish(self, event: Event) -> None:
        with self._lock:
            targets = [sub for sub in self._subs.values() if sub.matches(event)]
        for sub in targets:
            self._deliver(sub, event)

    def _deliver(self, sub: Subscription, event: Event) -> None:
        def _put() -> None:
            try:
                sub.queue.put_nowait(event)
                sub.delivered += 1
            except asyncio.QueueFull:
                # Backpressure: drop for this slow consumer and let it resync from
                # its durable cursor rather than blocking every other subscriber.
                sub.dropped += 1

        try:
            sub.loop.call_soon_threadsafe(_put)
        except RuntimeError:
            # Loop already closed; drop the subscription lazily.
            self.unsubscribe(sub.id)

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "subscriptions": len(self._subs),
                "dropped": sum(sub.dropped for sub in self._subs.values()),
                "delivered": sum(sub.delivered for sub in self._subs.values()),
            }
