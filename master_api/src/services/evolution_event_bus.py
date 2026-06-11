"""In-process pub/sub bus for evolution lifecycle events (CARE §4.3).

# noqa-style header so the file remains self-explanatory:


Each subscriber gets its own bounded ``asyncio.Queue``. Publishes fan out
to every active subscriber for that evolution id. Subscribers that fall
behind (queue full) drop the OLDEST events first — losing a few interim
``individual_evaluated`` ticks is preferable to back-pressuring the
publisher and stalling the evolution loop.

This is a single-process bus. Cross-runner / cross-replica SSE will need
Redis Pub/Sub later; we ship the simpler path first because the platform
already runs master_api as a single process and CARE's EvolutionScreen
only needs to follow one evolution at a time.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional, Set

from pydantic import BaseModel, Field

DEFAULT_QUEUE_SIZE = 64


class _Subscription:
    """Async-iterable view over a single subscriber queue.

    Guarantees ``aclose()`` always tears down the bus registration,
    regardless of whether ``__anext__`` was ever called.
    """

    def __init__(self, queue: asyncio.Queue, on_close: Callable[[], None]) -> None:
        self._queue = queue
        self._on_close = on_close
        self._closed = False

    def __aiter__(self) -> "_Subscription":
        return self

    async def __anext__(self) -> "EvolutionEvent":
        if self._closed:
            raise StopAsyncIteration
        return await self._queue.get()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._on_close()


class EvolutionEvent(BaseModel):
    """Single event delivered to subscribers."""

    event_type: str = Field(..., min_length=1)
    evolution_id: str
    payload: Dict[str, Any] = Field(default_factory=dict)
    emitted_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    sequence: int = Field(0, ge=0, description="Monotonic per-evolution sequence number")


class EvolutionEventBus:
    """Per-evolution fan-out. All methods are coroutine-safe."""

    def __init__(self, queue_size: int = DEFAULT_QUEUE_SIZE) -> None:
        self._queue_size = queue_size
        self._subscribers: Dict[str, Set[asyncio.Queue[EvolutionEvent]]] = {}
        self._sequences: Dict[str, int] = {}

    async def publish(self, evolution_id: str, event_type: str, payload: Optional[Dict[str, Any]] = None) -> EvolutionEvent:
        """Send ``event_type`` to every subscriber of ``evolution_id``.

        Returns the constructed event so callers can persist it / log it.
        No locks: ``dict.get/setdefault/pop`` + ``set.add/discard`` are
        atomic under the GIL, and the bus is intentionally single-process.
        """
        seq = self._sequences.get(evolution_id, 0) + 1
        self._sequences[evolution_id] = seq
        queues = list(self._subscribers.get(evolution_id, ()))

        event = EvolutionEvent(
            event_type=event_type,
            evolution_id=evolution_id,
            payload=payload or {},
            sequence=seq,
        )

        for queue in queues:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # Drop oldest, push newest. Single slot of churn keeps the
                # publisher off the critical path and the subscriber sees
                # the *latest* state on next poll.
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:  # pragma: no cover — race
                    pass
                try:
                    queue.put_nowait(event)
                except asyncio.QueueFull:  # pragma: no cover — race
                    pass
        return event

    def subscribe(self, evolution_id: str) -> "_Subscription":
        """Register a subscription **eagerly** and return its async iterator.

        Returns a :class:`_Subscription`: async-iterable, with an explicit
        ``aclose()`` that *always* removes the registration — even when
        the iterator was never pumped (an async-generator's ``finally``
        block wouldn't run in that case).
        """
        queue: asyncio.Queue[EvolutionEvent] = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers.setdefault(evolution_id, set()).add(queue)

        def _on_close() -> None:
            subs = self._subscribers.get(evolution_id)
            if subs is not None:
                subs.discard(queue)
                if not subs:
                    self._subscribers.pop(evolution_id, None)

        return _Subscription(queue, _on_close)

    def subscriber_count(self, evolution_id: str) -> int:
        """Snapshot helper; mainly useful for tests + /debug endpoints."""
        return len(self._subscribers.get(evolution_id, ()))


# Module-level default bus so the route + service can share it without
# threading it through ServiceManager (which would force a wider refactor).
_default_bus: Optional[EvolutionEventBus] = None


def get_default_bus() -> EvolutionEventBus:
    global _default_bus
    if _default_bus is None:
        _default_bus = EvolutionEventBus()
    return _default_bus


def reset_default_bus_for_tests() -> None:
    """Replace the module-level bus with a fresh one. Tests only."""
    global _default_bus
    _default_bus = EvolutionEventBus()
