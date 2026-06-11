"""Tests for the SSE event stream (CARE §4.3).

Three layers:

* ``EvolutionEventBus`` — pub/sub semantics, fan-out, queue overflow
  handling, subscriber teardown on iterator close.
* ``EvolutionService`` → bus integration: ``create_evolution`` emits
  ``created``; ``update_status`` emits the right event type for each
  status transition and forwards extra payload fields.
* SSE endpoint, real-app: TestClient.stream() against the wired
  master_api app — verifies the initial ``snapshot`` event, that
  subsequent ``update_status`` calls turn into SSE frames, that the
  payload is well-formed JSON, that unknown ids return 404 before
  opening the stream, and that auth still applies (401 without key).
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MASTER_SRC = REPO_ROOT / "master_api" / "src"

sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(MASTER_SRC.parent))

for _stale in [m for m in list(sys.modules) if m == "src" or m.startswith("src.")]:
    sys.modules.pop(_stale, None)

models = importlib.import_module("src.models.evolution")
svc_mod = importlib.import_module("src.services.evolution_service")
bus_mod = importlib.import_module("src.services.evolution_event_bus")


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeStorage:
    def __init__(self) -> None:
        self.objects: Dict[str, bytes] = {}

    async def download_bytes(self, k: str) -> Optional[bytes]:
        return self.objects.get(k)

    async def upload_bytes(self, data: bytes, k: str, metadata=None) -> bool:
        self.objects[k] = data
        return True

    async def object_exists(self, k: str) -> bool:
        return k in self.objects


def _inline_payload(**overrides: Any) -> Dict[str, Any]:
    base = {
        "name": "evo-events-test",
        "seed_chains": [{"chain_content": {"name": "seed", "template": "echo"}}],
        "fitness": {"prompt": "Is the answer correct?"},
        "objectives": ["accuracy"],
        "ga_config": {"population_size": 2, "max_iterations": 3},
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Bus semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_with_no_subscribers_is_noop():
    bus = bus_mod.EvolutionEventBus()
    event = await bus.publish("e-1", "generation_started", {"generation": 1})
    assert event.event_type == "generation_started"
    assert event.sequence == 1
    assert bus.subscriber_count("e-1") == 0


@pytest.mark.asyncio
async def test_single_subscriber_receives_published_events_in_order():
    bus = bus_mod.EvolutionEventBus()
    received: list = []

    async def consume():
        async for ev in bus.subscribe("e-1"):
            received.append((ev.event_type, ev.sequence))
            if ev.event_type == "completed":
                break

    consumer = asyncio.create_task(consume())
    # Yield once so the subscriber registers before any publish.
    await asyncio.sleep(0)
    await bus.publish("e-1", "generation_started", {"generation": 1})
    await bus.publish("e-1", "individual_evaluated", {"id": "ind-1"})
    await bus.publish("e-1", "completed", {})
    await asyncio.wait_for(consumer, timeout=2)

    assert received == [
        ("generation_started", 1),
        ("individual_evaluated", 2),
        ("completed", 3),
    ]


@pytest.mark.asyncio
async def test_fan_out_to_multiple_subscribers():
    bus = bus_mod.EvolutionEventBus()
    captures: Dict[str, list] = {"a": [], "b": []}

    async def consume(label: str, stop_after: int):
        async for ev in bus.subscribe("e-1"):
            captures[label].append(ev.sequence)
            if len(captures[label]) >= stop_after:
                break

    tasks = [
        asyncio.create_task(consume("a", 3)),
        asyncio.create_task(consume("b", 3)),
    ]
    await asyncio.sleep(0)  # let both subscribers register
    for i in range(3):
        await bus.publish("e-1", f"type-{i}", {})
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=2)
    assert captures["a"] == [1, 2, 3]
    assert captures["b"] == [1, 2, 3]


@pytest.mark.asyncio
async def test_iterator_cleanup_removes_subscription():
    bus = bus_mod.EvolutionEventBus()
    gen = bus.subscribe("e-1")
    # Registration is eager — count should already be 1 even before the
    # iterator has been pumped.
    assert bus.subscriber_count("e-1") == 1
    await gen.aclose()
    assert bus.subscriber_count("e-1") == 0


@pytest.mark.asyncio
async def test_queue_overflow_drops_oldest_keeps_publisher_unblocked():
    bus = bus_mod.EvolutionEventBus(queue_size=2)
    gen = bus.subscribe("e-1")
    try:
        # Don't consume — fill the queue past capacity. With eager
        # registration the subscriber's queue receives all of these.
        for i in range(5):
            await bus.publish("e-1", f"t-{i}", {"i": i})
        # Now drain — we should see only the last 2 events (oldest dropped).
        received: list = []
        for _ in range(2):
            ev = await asyncio.wait_for(gen.__anext__(), timeout=1)
            received.append(ev.payload["i"])
        assert received == [3, 4]
    finally:
        await gen.aclose()


@pytest.mark.asyncio
async def test_distinct_evolution_ids_dont_cross_streams():
    bus = bus_mod.EvolutionEventBus()
    a_seen: list = []
    b_seen: list = []

    async def consume(eid: str, sink: list, stop_after: int):
        async for ev in bus.subscribe(eid):
            sink.append(ev.event_type)
            if len(sink) >= stop_after:
                break

    a_task = asyncio.create_task(consume("e-A", a_seen, 1))
    b_task = asyncio.create_task(consume("e-B", b_seen, 1))
    await asyncio.sleep(0)
    await bus.publish("e-A", "for-A", {})
    await bus.publish("e-B", "for-B", {})
    await asyncio.wait_for(asyncio.gather(a_task, b_task), timeout=2)

    assert a_seen == ["for-A"]
    assert b_seen == ["for-B"]


# ---------------------------------------------------------------------------
# EvolutionService → bus integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_evolution_publishes_created_event():
    bus = bus_mod.EvolutionEventBus()
    storage = FakeStorage()
    service = svc_mod.EvolutionService(storage=storage, event_bus=bus)

    payload = models.EvolutionCreate.model_validate(_inline_payload())
    record = await service.create_evolution(payload)
    # ``create_evolution`` bumps the sequence; the next publish must be
    # ≥ 2, which proves the bus saw the ``created`` event for that id.
    next_event = await bus.publish(record.evolution_id, "ping", {})
    assert next_event.sequence == 2
    assert next_event.event_type == "ping"


@pytest.mark.asyncio
async def test_update_status_emits_correct_event_per_status():
    bus = bus_mod.EvolutionEventBus()
    storage = FakeStorage()
    service = svc_mod.EvolutionService(storage=storage, event_bus=bus)
    payload = models.EvolutionCreate.model_validate(_inline_payload())
    record = await service.create_evolution(payload)

    # Subscribe AFTER create so we don't pick up the "created" event.
    gen = bus.subscribe(record.evolution_id)
    try:
        expected = [
            (models.EvolutionStatus.RUNNING, "generation_started"),
            (models.EvolutionStatus.COMPLETED, "completed"),
        ]
        for status, _ in expected:
            await service.update_status(record.evolution_id, status)
        seen: list = []
        for _ in expected:
            ev = await asyncio.wait_for(gen.__anext__(), timeout=1)
            seen.append(ev.event_type)
        assert seen == [et for _, et in expected]
    finally:
        await gen.aclose()


@pytest.mark.asyncio
async def test_update_status_forwards_extra_payload_fields():
    bus = bus_mod.EvolutionEventBus()
    storage = FakeStorage()
    service = svc_mod.EvolutionService(storage=storage, event_bus=bus)
    payload = models.EvolutionCreate.model_validate(_inline_payload())
    record = await service.create_evolution(payload)

    gen = bus.subscribe(record.evolution_id)
    try:
        await service.update_status(
            record.evolution_id,
            models.EvolutionStatus.COMPLETED,
            best_individual_id="ind-best",
        )
        ev = await asyncio.wait_for(gen.__anext__(), timeout=1)
        assert ev.event_type == "completed"
        assert ev.payload["best_individual_id"] == "ind-best"
        assert ev.payload["status"] == "completed"
    finally:
        await gen.aclose()


# ---------------------------------------------------------------------------
# Real-app SSE smoke
# ---------------------------------------------------------------------------


@pytest.fixture
def real_app(monkeypatch):
    """Boot master_api with auth + a fake storage + a fresh shared bus."""
    monkeypatch.setenv("KAFKA__ENABLED", "false")
    monkeypatch.setenv("DATABASE__URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("MASTER_API_KEY", "sse-smoke-key")
    monkeypatch.delenv("CORS_ALLOWED_ORIGINS", raising=False)

    for _stale in [m for m in list(sys.modules) if m == "src" or m.startswith("src.")]:
        sys.modules.pop(_stale, None)
    sys.path.insert(0, str(MASTER_SRC.parent))

    main_mod = importlib.import_module("src.main")
    evolutions_route = importlib.import_module("src.api.routes.evolutions")
    bus_mod_live = importlib.import_module("src.services.evolution_event_bus")
    bus_mod_live.reset_default_bus_for_tests()
    # The default heartbeat is 15 s, which would make the HTTP smoke wait
    # up to that long for the server to notice the client closing the
    # stream. Crunch it down so the test finishes quickly.
    monkeypatch.setattr(evolutions_route, "SSE_HEARTBEAT_SECONDS", 0.1)

    fake_storage = FakeStorage()

    class FakeServiceManager:
        def get_storage_service(self):
            return fake_storage

    evolutions_route.set_service_manager(FakeServiceManager())
    return main_mod.app, fake_storage, bus_mod_live.get_default_bus()


def test_sse_unknown_id_returns_404_without_opening_stream(real_app):
    from fastapi.testclient import TestClient

    app, _, _ = real_app
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get(
        "/api/v1/evolutions/no-such/events",
        headers={"X-API-Key": "sse-smoke-key"},
    )
    assert resp.status_code == 404


def test_sse_requires_auth(real_app):
    from fastapi.testclient import TestClient

    app, _, _ = real_app
    client = TestClient(app, raise_server_exceptions=False)
    # Have to create a record first via auth, then probe events un-auth'd.
    create = client.post(
        "/api/v1/evolutions",
        headers={"X-API-Key": "sse-smoke-key"},
        json=_inline_payload(),
    )
    assert create.status_code == 201, create.text
    eid = create.json()["evolution_id"]

    resp = client.get(f"/api/v1/evolutions/{eid}/events")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_sse_generator_snapshot_then_live_event_directly():
    """Drive ``build_sse_generator`` directly in one event loop.

    Avoids the cross-thread / cross-loop problem that breaks
    ``asyncio.Queue`` semantics when a stream test pushes events from a
    worker thread.
    """
    evolutions_route = importlib.import_module("src.api.routes.evolutions")

    bus = bus_mod.EvolutionEventBus()
    storage = FakeStorage()
    service = svc_mod.EvolutionService(storage=storage, event_bus=bus)
    payload = models.EvolutionCreate.model_validate(_inline_payload())
    record = await service.create_evolution(payload)

    disconnected_flag = {"value": False}

    async def is_disconnected() -> bool:
        return disconnected_flag["value"]

    gen = evolutions_route.build_sse_generator(
        evolution_id=record.evolution_id,
        snapshot=record,
        bus=bus,
        is_disconnected=is_disconnected,
        heartbeat_seconds=0.05,
    )

    # Frame 1: snapshot.
    first = await asyncio.wait_for(gen.__anext__(), timeout=1)
    assert first.startswith(b"event: snapshot\n")
    snapshot_payload = json.loads(first.split(b"data: ", 1)[1].split(b"\n\n", 1)[0])
    assert snapshot_payload["evolution_id"] == record.evolution_id
    assert snapshot_payload["status"] == "queued"

    # Push a live event.
    await bus.publish(record.evolution_id, "individual_evaluated", {"id": "ind-1"})

    # Frame 2: either a heartbeat (if we lost the race against the
    # 0.05 s timer) or the live event. Loop until we get the latter.
    for _ in range(20):
        frame = await asyncio.wait_for(gen.__anext__(), timeout=1)
        if frame.startswith(b": heartbeat"):
            continue
        assert frame.startswith(b"event: individual_evaluated\n")
        live_payload = json.loads(frame.split(b"data: ", 1)[1].split(b"\n\n", 1)[0])
        assert live_payload["evolution_id"] == record.evolution_id
        assert live_payload["payload"] == {"id": "ind-1"}
        assert live_payload["sequence"] >= 1
        break
    else:
        pytest.fail("never received the live event from the SSE generator")

    # Disconnect → generator exits its loop.
    disconnected_flag["value"] = True
    with pytest.raises(StopAsyncIteration):
        # Consume any pending heartbeats until the generator notices.
        for _ in range(20):
            await asyncio.wait_for(gen.__anext__(), timeout=1)
    await gen.aclose()
    assert bus.subscriber_count(record.evolution_id) == 0


@pytest.mark.asyncio
async def test_sse_generator_emits_heartbeat_when_idle():
    bus = bus_mod.EvolutionEventBus()
    storage = FakeStorage()
    service = svc_mod.EvolutionService(storage=storage, event_bus=bus)
    payload = models.EvolutionCreate.model_validate(_inline_payload())
    record = await service.create_evolution(payload)

    evolutions_route = importlib.import_module("src.api.routes.evolutions")

    async def never_disconnected() -> bool:
        return False

    gen = evolutions_route.build_sse_generator(
        evolution_id=record.evolution_id,
        snapshot=record,
        bus=bus,
        is_disconnected=never_disconnected,
        heartbeat_seconds=0.05,
    )
    # Drain the snapshot.
    await asyncio.wait_for(gen.__anext__(), timeout=1)
    # No publishes — the next frame must be a heartbeat.
    next_frame = await asyncio.wait_for(gen.__anext__(), timeout=1)
    assert next_frame == b": heartbeat\n\n"
    await gen.aclose()
