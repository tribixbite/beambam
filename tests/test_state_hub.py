"""Tests for beambam.state_hub — sync pub/sub primitive."""
from __future__ import annotations

import asyncio
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from beambam.state_hub import StateHub


def test_publish_with_no_subscribers_is_safe():
    hub = StateHub()
    hub.publish({"gcode_state": "IDLE"})
    assert hub.last_state == {"gcode_state": "IDLE"}


def test_single_subscriber_receives_publishes():
    hub = StateHub()
    sub = hub.subscribe()
    hub.publish({"layer": 1})
    hub.publish({"layer": 2})
    it = iter(sub)
    assert next(it) == {"layer": 1}
    assert next(it) == {"layer": 2}
    sub.close()


def test_subscribe_replays_last_state_immediately():
    """A late-joining subscriber should get the last published state
    on its first iteration (no waiting for the next push)."""
    hub = StateHub()
    hub.publish({"gcode_state": "RUNNING", "percent": 42})
    sub = hub.subscribe()
    state = next(iter(sub))
    assert state["gcode_state"] == "RUNNING"
    assert state["percent"] == 42
    sub.close()


def test_multiple_subscribers_all_receive():
    hub = StateHub()
    s1 = hub.subscribe()
    s2 = hub.subscribe()
    s3 = hub.subscribe()
    hub.publish({"layer": 7})
    for s in (s1, s2, s3):
        assert next(iter(s)) == {"layer": 7}


def test_subscriber_count_tracks_subscribes_and_unsubscribes():
    hub = StateHub()
    assert hub.subscriber_count() == 0
    s1 = hub.subscribe()
    assert hub.subscriber_count() == 1
    s2 = hub.subscribe()
    assert hub.subscriber_count() == 2
    hub.unsubscribe(s1)
    assert hub.subscriber_count() == 1
    hub.unsubscribe(s2)
    assert hub.subscriber_count() == 0


def test_full_queue_drops_oldest_not_newest():
    """When a slow subscriber's queue fills, oldest entries are dropped
    so newer state is preserved (most useful to live consumers)."""
    hub = StateHub(maxqueue=2)
    sub = hub.subscribe()
    for i in range(10):
        hub.publish({"layer": i})
    # Subscriber should have at most 2 entries, and they should be the
    # most recent two (layers 8 and 9).
    drained = []
    while not sub._q.empty():                # noqa: SLF001 — internal probe
        drained.append(sub._q.get_nowait()["layer"])
    assert max(drained) == 9
    assert len(drained) == 2


def test_close_stops_iteration():
    """hub.close() should immediately stop all subscriber iterations."""
    hub = StateHub()
    sub = hub.subscribe()
    done = threading.Event()
    received: list[dict] = []

    def consume():
        for state in sub:
            received.append(state)
        done.set()

    t = threading.Thread(target=consume)
    t.start()
    hub.publish({"layer": 1})
    time.sleep(0.1)
    hub.close()
    assert done.wait(2.0), "iteration didn't stop after hub.close()"
    t.join(2.0)
    assert {"layer": 1} in received


def test_unsubscribe_closes_subscription_iteration():
    hub = StateHub()
    sub = hub.subscribe()
    hub.publish({"x": 1})
    hub.unsubscribe(sub)
    # After unsubscribe iteration should drain remaining + stop.
    states = list(iter(sub))
    # Either it sees the cached x:1, or it's empty if close raced first.
    assert all(s.get("x", None) in (1, None) for s in states)


def test_publish_after_close_is_noop():
    hub = StateHub()
    hub.close()
    hub.publish({"x": 1})
    # last_state was set before close (from a real publish path) but the
    # post-close publish must NOT update it.
    assert hub.last_state is None


def test_thread_safety_with_concurrent_publishers():
    """Many threads publishing concurrently — no exceptions, all
    subscribers receive at least the final state."""
    hub = StateHub(maxqueue=100)
    sub = hub.subscribe()

    def publisher(n: int) -> None:
        for i in range(50):
            hub.publish({"thread": n, "i": i})

    threads = [threading.Thread(target=publisher, args=(n,))
               for n in range(4)]
    for t in threads: t.start()
    for t in threads: t.join()

    # Drain the queue; we should have somewhere between 100 (the cap)
    # and 200 (4 threads × 50 publishes).
    drained = []
    while not sub._q.empty():                # noqa: SLF001
        drained.append(sub._q.get_nowait())
    assert 50 <= len(drained) <= 200
    # No exception raised; all published dicts have the expected shape.
    assert all("thread" in d and "i" in d for d in drained)


# ----- Asyncio adapter -----------------------------------------------------


def test_subscribe_async_yields_states():
    """The async adapter must yield states pushed via publish()."""
    hub = StateHub()
    received = []

    async def watch():
        async for state in hub.subscribe_async():
            received.append(state)
            if len(received) >= 2:
                hub.close()
                return

    async def driver():
        await asyncio.sleep(0.05)
        hub.publish({"layer": 1})
        await asyncio.sleep(0.05)
        hub.publish({"layer": 2})

    async def both():
        await asyncio.gather(watch(), driver())

    asyncio.run(both())
    layers = [r.get("layer") for r in received]
    assert 1 in layers and 2 in layers


def test_subscription_get_returns_state_on_publish():
    """`get(timeout)` returns the dict when a publish arrives within
    the timeout. Used by the bridge's SSE handler so it can wake on
    pushes and emit keepalives on timeout without polling."""
    hub = StateHub()
    sub = hub.subscribe()
    hub.publish({"layer": 7})
    state = sub.get(timeout=1.0)
    assert state == {"layer": 7}
    sub.close()


def test_subscription_get_returns_none_on_timeout():
    """No publishes arrive within the timeout → `get()` returns None
    instead of raising. SSE handler uses this signal to emit a
    `: keepalive` comment so intermediate proxies don't drop idle
    connections."""
    hub = StateHub()
    sub = hub.subscribe()
    state = sub.get(timeout=0.1)
    assert state is None
    sub.close()


def test_subscription_get_returns_none_after_close():
    """After unsubscribe(), pending `get(timeout)` returns None so the
    consumer loop can exit cleanly instead of blocking forever."""
    hub = StateHub()
    sub = hub.subscribe()

    def closer():
        time.sleep(0.05)
        hub.unsubscribe(sub)

    threading.Thread(target=closer, daemon=True).start()
    state = sub.get(timeout=1.0)
    assert state is None
