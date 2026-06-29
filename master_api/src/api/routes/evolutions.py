"""HTTP endpoints for population-level evolutions (CARE §4.1 — §4.4)."""

from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator, Optional

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import ValidationError

from ...models.evolution import (
    AcceptIndividualRequest,
    CareEvolutionLaunch,
    EvolutionCreate,
    EvolutionResponse,
    EvolutionsListResponse,
    EvolutionStatus,
    IndividualCreate,
    IndividualResponse,
    IndividualsListResponse,
)
from ...services.evolution_event_bus import EvolutionEvent, EvolutionEventBus, get_default_bus
from ...services.evolution_service import (
    EvolutionAcceptError,
    EvolutionControlError,
    EvolutionNotFoundError,
    EvolutionPersistError,
    EvolutionService,
    IndividualNotFoundError,
    IndividualValidationError,
)
from ...services.memory_client import MemoryClient
from ...services.service_manager import ServiceManager

SSE_HEARTBEAT_SECONDS = 15.0

router = APIRouter()

_service_manager: Optional[ServiceManager] = None


def set_service_manager(service_manager: ServiceManager) -> None:
    global _service_manager
    _service_manager = service_manager


def _get_service() -> EvolutionService:
    if _service_manager is None:
        raise HTTPException(status_code=503, detail="Service manager not initialized")
    # Two-step lookup: test fixtures sometimes inject a minimal
    # ServiceManager stub without a ``config`` attribute.
    config = getattr(_service_manager, "config", None)
    memory_url = getattr(config, "memory_api_url", None) if config is not None else None
    memory_client = MemoryClient(memory_url) if memory_url else None
    return EvolutionService(
        storage=_service_manager.get_storage_service(),
        memory_client=memory_client,
    )


def _get_bus() -> EvolutionEventBus:
    """Return the shared in-process event bus. Tests can monkeypatch this."""
    return get_default_bus()


def _format_sse(event_type: str, data: object) -> bytes:
    """Encode one SSE frame. ``data`` is JSON-serialised."""
    body = json.dumps(data, default=str)
    return f"event: {event_type}\ndata: {body}\n\n".encode("utf-8")


def _format_sse_event(event: EvolutionEvent) -> bytes:
    return _format_sse(
        event.event_type,
        {
            "evolution_id": event.evolution_id,
            "sequence": event.sequence,
            "emitted_at": event.emitted_at.isoformat(),
            "payload": event.payload,
        },
    )


def _normalise_create_payload(payload: dict) -> EvolutionCreate:
    """Accept canonical Platform payloads and CARE's launch-modal shorthand."""
    if "seed_chains" in payload:
        return EvolutionCreate.model_validate(payload)
    if "base_chain_id" in payload:
        return CareEvolutionLaunch.model_validate(payload).to_evolution_create()
    return EvolutionCreate.model_validate(payload)


@router.post("", response_model=EvolutionResponse, status_code=status.HTTP_201_CREATED)
async def create_evolution(payload: dict) -> EvolutionResponse:
    """Create a new evolution from a population spec.

    Pydantic enforces the schema (seed_chains non-empty, objectives unique,
    GA caps), so a malformed body returns 422 from FastAPI before this
    handler runs. Persist failures bubble up as 500.
    """
    service = _get_service()
    try:
        create_payload = _normalise_create_payload(payload)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors(include_context=False)) from exc
    try:
        return await service.create_evolution(create_payload)
    except EvolutionPersistError as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "persist_failed", "message": str(exc)},
        ) from exc


@router.get("", response_model=EvolutionsListResponse)
async def list_evolutions(
    status_filter: Optional[EvolutionStatus] = Query(None, alias="status"),
    tag: Optional[str] = Query(None, max_length=100),
    q: Optional[str] = Query(None, max_length=200),
    cursor: Optional[str] = Query(None, max_length=128),
    limit: int = Query(20, ge=1, le=200),
) -> EvolutionsListResponse:
    """Paginated list of evolution records (CARE §4.9).

    Ordered most-recent-first by ``created_at``. ``status`` / ``tag`` /
    ``q`` filters compose. ``cursor`` is the ``evolution_id`` of the
    last item from the previous page; pass it verbatim to advance.
    """
    service = _get_service()
    try:
        return await service.list_evolutions(
            status=status_filter,
            tag=tag,
            q=q,
            cursor=cursor,
            limit=limit,
        )
    except EvolutionPersistError as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "record_corrupt", "message": str(exc)},
        ) from exc


@router.get("/{evolution_id}", response_model=EvolutionResponse)
async def get_evolution(evolution_id: str) -> EvolutionResponse:
    """Look up a previously-created evolution by id (CARE §4.2 stub)."""
    service = _get_service()
    try:
        return await service.get_evolution(evolution_id)
    except EvolutionNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={"error": "evolution_not_found", "evolution_id": str(exc)},
        ) from exc
    except EvolutionPersistError as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "record_corrupt", "message": str(exc)},
        ) from exc


@router.post(
    "/{evolution_id}/individuals",
    response_model=IndividualResponse,
    status_code=status.HTTP_201_CREATED,
)
async def record_individual(evolution_id: str, payload: IndividualCreate) -> IndividualResponse:
    """Record an evaluated individual produced by the GA loop (CARE §4.2)."""
    service = _get_service()
    try:
        return await service.record_individual(evolution_id, payload)
    except EvolutionNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={"error": "evolution_not_found", "evolution_id": str(exc)},
        ) from exc
    except IndividualValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "invalid_individual", "message": str(exc)},
        ) from exc
    except EvolutionPersistError as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "persist_failed", "message": str(exc)},
        ) from exc


@router.get("/{evolution_id}/individuals", response_model=IndividualsListResponse)
async def list_individuals(
    evolution_id: str,
    generation: Optional[int] = Query(None, ge=0),
    pareto: bool = Query(False),
    limit: int = Query(100, ge=1, le=1000),
) -> IndividualsListResponse:
    """List individuals for an evolution with optional generation + Pareto filters."""
    service = _get_service()
    try:
        evolution = await service.get_evolution(evolution_id)
        items = await service.list_individuals(
            evolution_id, generation=generation, pareto=pareto, limit=limit
        )
    except EvolutionNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={"error": "evolution_not_found", "evolution_id": str(exc)},
        ) from exc
    except EvolutionPersistError as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "record_corrupt", "message": str(exc)},
        ) from exc
    return IndividualsListResponse(
        evolution_id=evolution_id,
        total=len(items),
        items=items,
        pareto_front=pareto,
        primary_objective=evolution.objectives[0] if evolution.objectives else None,
        higher_is_better=evolution.fitness.higher_is_better,
    )


@router.get(
    "/{evolution_id}/individuals/{individual_id}",
    response_model=IndividualResponse,
)
async def get_individual(evolution_id: str, individual_id: str) -> IndividualResponse:
    service = _get_service()
    try:
        return await service.get_individual(evolution_id, individual_id)
    except IndividualNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "individual_not_found",
                "evolution_id": evolution_id,
                "individual_id": str(exc),
            },
        ) from exc
    except EvolutionPersistError as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "record_corrupt", "message": str(exc)},
        ) from exc


@router.post("/{evolution_id}/accept", response_model=EvolutionResponse)
async def accept_individual(
    evolution_id: str, payload: AcceptIndividualRequest
) -> EvolutionResponse:
    """Promote a chosen individual to be the evolution's winner (CARE §4.4).

    Sets the evolution's ``status`` to ``accepted``, pins
    ``accepted_individual_id`` and ``best_individual_id`` to the supplied
    id, flips the individual's ``accepted`` flag, and publishes an
    ``accepted`` event on the SSE stream. Idempotent on the same
    individual; switching individuals on an already-accepted evolution
    returns 409.
    """
    service = _get_service()
    try:
        return await service.accept_individual(
            evolution_id,
            payload.individual_id,
            chain_id_to_update=payload.chain_id_to_update,
            note=payload.note,
        )
    except EvolutionNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={"error": "evolution_not_found", "evolution_id": str(exc)},
        ) from exc
    except IndividualNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "individual_not_found",
                "evolution_id": evolution_id,
                "individual_id": str(exc),
            },
        ) from exc
    except EvolutionAcceptError as exc:
        message = str(exc)
        error = "already_accepted" if "already accepted" in message else "accept_failed"
        raise HTTPException(
            status_code=409,
            detail={"error": error, "message": message},
        ) from exc
    except EvolutionPersistError as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "persist_failed", "message": str(exc)},
        ) from exc


async def _run_control(evolution_id: str, action: str) -> EvolutionResponse:
    service = _get_service()
    try:
        if action == "cancel":
            return await service.cancel_evolution(evolution_id)
        if action == "pause":
            return await service.pause_evolution(evolution_id)
        if action == "resume":
            return await service.resume_evolution(evolution_id)
    except EvolutionNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={"error": "evolution_not_found", "evolution_id": str(exc)},
        ) from exc
    except EvolutionControlError as exc:
        raise HTTPException(
            status_code=409,
            detail={"error": "invalid_transition", "message": str(exc)},
        ) from exc
    except EvolutionPersistError as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "persist_failed", "message": str(exc)},
        ) from exc
    raise HTTPException(status_code=500, detail={"error": "unknown_control_action"})


@router.post("/{evolution_id}/cancel", response_model=EvolutionResponse)
async def cancel_evolution(evolution_id: str) -> EvolutionResponse:
    return await _run_control(evolution_id, "cancel")


@router.post("/{evolution_id}/pause", response_model=EvolutionResponse)
async def pause_evolution(evolution_id: str) -> EvolutionResponse:
    return await _run_control(evolution_id, "pause")


@router.post("/{evolution_id}/resume", response_model=EvolutionResponse)
async def resume_evolution(evolution_id: str) -> EvolutionResponse:
    return await _run_control(evolution_id, "resume")


@router.get("/{evolution_id}/events")
async def stream_evolution_events(evolution_id: str, request: Request) -> StreamingResponse:
    """SSE event stream for one evolution (CARE §4.3).

    Wire shape:

    - Connection opens with a ``snapshot`` event carrying the current
      record so CARE can render initial state without a separate GET.
    - Every subsequent publish on the in-process bus is forwarded as an
      ``event: <type>`` / ``data: <json>`` frame.
    - A `: heartbeat` comment is emitted every ~15 s so proxies don't
      reap idle connections.
    - The stream terminates cleanly when the client disconnects (the
      bus subscription is torn down in a ``finally`` inside the bus).

    A 404 is returned upfront when the evolution id doesn't exist —
    we don't open a stream for nothing.
    """
    service = _get_service()
    try:
        snapshot = await service.get_evolution(evolution_id)
    except EvolutionNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={"error": "evolution_not_found", "evolution_id": str(exc)},
        ) from exc

    bus = _get_bus()

    return StreamingResponse(
        build_sse_generator(
            evolution_id=evolution_id,
            snapshot=snapshot,
            bus=bus,
            is_disconnected=request.is_disconnected,
            heartbeat_seconds=SSE_HEARTBEAT_SECONDS,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",  # disable nginx output buffering
            "Connection": "keep-alive",
        },
    )


async def build_sse_generator(
    *,
    evolution_id: str,
    snapshot: EvolutionResponse,
    bus: EvolutionEventBus,
    is_disconnected,
    heartbeat_seconds: float = SSE_HEARTBEAT_SECONDS,
) -> AsyncIterator[bytes]:
    """SSE byte stream: snapshot → live events → heartbeat on idle.

    Pulled out of the route handler so unit tests can drive it directly
    with a stub ``is_disconnected`` callable instead of needing a real
    HTTP layer + threading dance.
    """
    # Subscribe BEFORE yielding the snapshot: any publish that happens
    # between the snapshot read and the consumer pulling frame 2 must
    # land in the buffer rather than be lost. Consumers can de-dupe by
    # ``sequence`` if they need to.
    events = bus.subscribe(evolution_id)
    yield _format_sse("snapshot", json.loads(snapshot.model_dump_json()))
    # Keep the same pending ``__anext__`` task alive across heartbeat
    # timeouts — cancelling it would also cancel the queue ``get()``
    # underneath, which would terminate the subscriber iterator.
    anext_task = asyncio.ensure_future(events.__anext__())
    try:
        while True:
            if await is_disconnected():
                return
            done, _pending = await asyncio.wait(
                {anext_task}, timeout=heartbeat_seconds
            )
            if anext_task in done:
                try:
                    event = anext_task.result()
                except StopAsyncIteration:
                    return
                yield _format_sse_event(event)
                # Only after we've consumed the previous result do we
                # ask the iterator for the next one.
                anext_task = asyncio.ensure_future(events.__anext__())
            else:
                yield b": heartbeat\n\n"
    finally:
        anext_task.cancel()
        try:
            await anext_task
        except (asyncio.CancelledError, StopAsyncIteration):
            pass
        await events.aclose()
