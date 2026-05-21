"""beambam.state_hub — in-memory pub/sub primitive for printer state.

Tiny synchronous broadcast hub so multiple subsystems (HA publisher,
MCP server, web UI SSE, timelapse trigger, queue dispatcher) can
subscribe to the same printer-state stream without each opening its
own MQTT client.

Design intent:
  * One Hub per printer (keyed by serial in the daemon)
  * publish() is non-blocking: each subscriber gets the new state
    via its own bounded queue; slow subscribers drop oldest entries
    rather than block the producer
  * No asyncio dependency — subscribers can be threads, asyncio
    loops, or sync iterators. Asyncio adapter included.
  * Late subscribers immediately get the last known state on join
    (so a freshly-opened HTTP/SSE client doesn't wait until the next
    push to learn the printer state)

v1.1.0 ships the hub as a building block; the daemon doesn't wire it
yet (the existing per-printer state cache + HTTP polling works fine
for current consumers). v1.2.0 migrates HA / MCP / WebUI / timelapse
to subscribe via Hub instead of polling.

Example:

    hub = StateHub(maxqueue=8)
    sub = hub.subscribe()                            # iterable of dicts
    hub.publish({"gcode_state": "RUNNING"})
    next(sub)                                        # → {'gcode_state': 'RUNNING'}

    # Async:
    async def watch():
        async for state in hub.subscribe_async():
            print(state["gcode_state"])
"""
from __future__ import annotations

import asyncio
import queue
import threading
from collections.abc import AsyncIterator, Iterator
from typing import Any


class _Subscription:
    """One subscriber's bounded queue.

    `maxsize=0` means unbounded (slow subscribers can OOM the process —
    use a bound unless you're certain). When the queue is full, the
    oldest entry is dropped to make room — newer state is always more
    useful than stale state."""

    def __init__(self, maxsize: int = 8) -> None:
        self._q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=maxsize)
        self._closed = threading.Event()

    def put(self, state: dict[str, Any]) -> None:
        # Drop-oldest-on-full so the producer never blocks.
        while True:
            try:
                self._q.put_nowait(state)
                return
            except queue.Full:
                try:
                    self._q.get_nowait()
                except queue.Empty:
                    pass

    def close(self) -> None:
        self._closed.set()
        # Wake any blocked iterator.
        try:
            self._q.put_nowait({})
        except queue.Full:
            pass

    def __iter__(self) -> Iterator[dict[str, Any]]:
        while not self._closed.is_set():
            try:
                state = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            if self._closed.is_set():
                return
            yield state

    def get(self, timeout: float | None = None) -> dict[str, Any] | None:
        """Block up to `timeout` seconds for the next state push.

        Returns the dict on success, or `None` on timeout or if the
        subscription was closed. Use this when you need timeout-aware
        receive (e.g. SSE keepalive every N seconds without polling)."""
        try:
            state = self._q.get(timeout=timeout)
        except queue.Empty:
            return None
        if self._closed.is_set():
            return None
        return state


class StateHub:
    """Sync pub/sub for printer state dicts.

    Thread-safe: publish() and subscribe() can be called from any thread.
    Subscriber iteration returns immediately when close() is called on
    the StateHub or the subscription itself."""

    def __init__(self, *, maxqueue: int = 8) -> None:
        self._maxqueue = maxqueue
        self._lock = threading.Lock()
        self._subs: list[_Subscription] = []
        self._last_state: dict[str, Any] | None = None
        self._closed = False

    def publish(self, state: dict[str, Any]) -> None:
        """Fan out a state push to every subscriber. Non-blocking; slow
        subscribers drop oldest entries."""
        with self._lock:
            if self._closed:
                return
            self._last_state = state
            subs = list(self._subs)
        for s in subs:
            s.put(state)

    @property
    def last_state(self) -> dict[str, Any] | None:
        """Last published state, or None if nothing has been published."""
        return self._last_state

    def subscribe(self) -> _Subscription:
        """Returns a sync iterator yielding every state push. If
        last_state is set, the iterator's first yield is that state
        immediately (so HTTP/SSE consumers see current state on connect
        rather than waiting for the next push)."""
        sub = _Subscription(maxsize=self._maxqueue)
        with self._lock:
            self._subs.append(sub)
        if self._last_state is not None:
            sub.put(self._last_state)
        return sub

    def unsubscribe(self, sub: _Subscription) -> None:
        with self._lock:
            try:
                self._subs.remove(sub)
            except ValueError:
                pass
        sub.close()

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)

    async def subscribe_async(self) -> AsyncIterator[dict[str, Any]]:
        """Async generator — yields every state push. Wraps the sync
        subscription with run_in_executor so blocking queue.get doesn't
        starve the event loop."""
        sub = self.subscribe()
        loop = asyncio.get_running_loop()
        try:
            while True:
                state = await loop.run_in_executor(
                    None, lambda: next(iter(sub), None))
                if state is None:
                    return
                yield state
        finally:
            self.unsubscribe(sub)

    def close(self) -> None:
        """Tear down the hub. All active subscriptions stop iterating."""
        with self._lock:
            self._closed = True
            subs = list(self._subs)
            self._subs.clear()
        for s in subs:
            s.close()
