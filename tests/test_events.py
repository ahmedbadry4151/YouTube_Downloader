from __future__ import annotations

import asyncio

from app.events import EventBus


def test_event_bus_fanout():
    async def scenario():
        bus = EventBus()
        bus.attach_loop(asyncio.get_running_loop())
        first = bus.subscribe()
        second = bus.subscribe()

        await asyncio.to_thread(bus.publish, {"type": "item", "n": 1})
        event_a = await asyncio.wait_for(first.get(), timeout=2)
        event_b = await asyncio.wait_for(second.get(), timeout=2)
        assert event_a == {"type": "item", "n": 1}
        assert event_b == event_a

        bus.unsubscribe(first)
        bus.unsubscribe(second)
        assert bus._subscribers == set()

    asyncio.run(scenario())


def test_event_bus_without_loop_is_noop():
    bus = EventBus()
    bus.publish({"type": "job"})  # must not raise


def test_event_bus_drops_oldest_when_full():
    async def scenario():
        bus = EventBus()
        bus.attach_loop(asyncio.get_running_loop())
        queue = bus.subscribe()
        for index in range(queue.maxsize + 5):
            bus.publish({"n": index})
        await asyncio.sleep(0)
        assert queue.qsize() == queue.maxsize
        assert queue.get_nowait()["n"] == 5

    asyncio.run(scenario())
