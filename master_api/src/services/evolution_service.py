"""Evolution record persistence (CARE §4.1).

Stores each evolution as a single JSON object in MinIO under
``evolutions/<evolution_id>.json`` so subsequent §4.2 reads (and §4.3
SSE) can hydrate state without depending on the master_api's
in-memory state.

This service deliberately does **not** start a chain experiment yet —
that wiring is a separate item in TODO.md (the evolution loop). For
§4.1 we just need creation + idempotent persistence so CARE's
EvolutionScreen can POST a config and get an ID back.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import List, Optional

from loguru import logger

from ..models.evolution import (
    EvolutionCreate,
    EvolutionResponse,
    EvolutionsListResponse,
    EvolutionStatus,
    IndividualCreate,
    IndividualResponse,
)
from .evolution_event_bus import EvolutionEventBus, get_default_bus
from .memory_client import MemoryClient, MemoryPromotionError, MemoryPromotionResult
from .storage_service import StorageService

EVOLUTIONS_PREFIX = "evolutions/"


def _individuals_prefix(evolution_id: str) -> str:
    return f"{EVOLUTIONS_PREFIX}{evolution_id}/individuals/"


def _individual_object_key(evolution_id: str, individual_id: str) -> str:
    return f"{_individuals_prefix(evolution_id)}{individual_id}.json"


def _effective_affected_chain_ids(evolution: EvolutionResponse) -> List[str]:
    if evolution.affected_chain_ids:
        return list(evolution.affected_chain_ids)
    return [
        seed.memory_chain_id
        for seed in evolution.seed_chains
        if seed.memory_chain_id
    ]


class EvolutionPersistError(Exception):
    """Raised when the evolution record cannot be written to storage."""


class EvolutionNotFoundError(Exception):
    """Raised by ``get_evolution`` when no record exists for the id."""


class IndividualNotFoundError(Exception):
    """Raised by ``get_individual`` when no record exists for the id."""


class IndividualValidationError(Exception):
    """Fitness scores don't match the evolution's declared objectives."""


class EvolutionAcceptError(Exception):
    """Accept refused — evolution already in a terminal accept state, etc."""


class EvolutionControlError(Exception):
    """Lifecycle control refused for the evolution's current state."""


def _object_key(evolution_id: str) -> str:
    return f"{EVOLUTIONS_PREFIX}{evolution_id}.json"


class EvolutionService:
    def __init__(
        self,
        storage: StorageService,
        event_bus: Optional[EvolutionEventBus] = None,
        memory_client: Optional[MemoryClient] = None,
    ) -> None:
        self._storage = storage
        self._bus = event_bus if event_bus is not None else get_default_bus()
        # Memory forwarding is fire-and-forget: a misbehaving Memory must
        # never break the platform's own evolution state machine. We
        # accept a configured client and skip when None.
        self._memory = memory_client

    def _evolution_meta(
        self,
        evolution: EvolutionResponse,
        individual: IndividualResponse,
        *,
        primary_objective: Optional[str],
    ) -> dict:
        """Build the gigaevo-memory ``evolution_meta`` block for a chain entity.

        Schema matches PREPARE.md §1.6: ``parent_version_ids``,
        ``fitness_score`` (primary objective only), ``generation``,
        ``experiment_id`` (the evolution id), ``objectives``,
        ``mutation_kind``.
        """
        fitness = (
            individual.fitness_scores.get(primary_objective)
            if primary_objective
            else None
        )
        objective_scores = {
            objective: individual.fitness_scores[objective]
            for objective in evolution.objectives
            if objective in individual.fitness_scores
        }
        return {
            "parent_version_ids": list(individual.parent_ids),
            "fitness_score": fitness,
            "generation": individual.generation,
            "experiment_id": evolution.evolution_id,
            "objectives": objective_scores,
            "mutation_kind": individual.mutation_kind,
        }

    def _accept_evolution_meta(
        self,
        evolution: EvolutionResponse,
        individual: IndividualResponse,
        *,
        accepted_at: datetime,
        primary_objective: Optional[str],
        chain_id: str,
        note: Optional[str],
    ) -> dict:
        meta = self._evolution_meta(
            evolution,
            individual,
            primary_objective=primary_objective,
        )
        metrics = {
            "acceptance_id": f"{evolution.evolution_id}:{individual.individual_id}:{chain_id}",
            "accepted_at": accepted_at.isoformat(),
            "accepted_individual_id": individual.individual_id,
            "accepted_chain_id": chain_id,
        }
        if note:
            metrics["note"] = note
        meta["metrics"] = metrics
        return meta

    async def _forward_individual_to_memory(
        self,
        evolution: EvolutionResponse,
        individual: IndividualResponse,
        *,
        channel: str,
    ) -> Optional[str]:
        """Best-effort save of one individual to Memory.

        Returns the Memory ``entity_id`` on success and ``None`` otherwise.
        Never raises — callers don't unwind state on Memory faults.
        """
        if self._memory is None or not self._memory.is_configured:
            return None
        primary = evolution.objectives[0] if evolution.objectives else None
        try:
            return await self._memory.save_chain(
                name=f"{evolution.name}#gen{individual.generation}",
                content=individual.chain_content,
                evolution_meta=self._evolution_meta(
                    evolution, individual, primary_objective=primary
                ),
                tags=list(evolution.tags),
                description=evolution.description,
                channel=channel,
            )
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning(f"memory forward failed for {individual.individual_id}: {exc}")
            return None

    async def create_evolution(self, payload: EvolutionCreate) -> EvolutionResponse:
        """Validate, materialise a record, persist to MinIO, return it."""
        affected_chain_ids = [
            seed.memory_chain_id
            for seed in payload.seed_chains
            if seed.memory_chain_id
        ]
        record = EvolutionResponse(
            name=payload.name,
            description=payload.description,
            status=EvolutionStatus.QUEUED,
            seed_chains=payload.seed_chains,
            affected_chain_ids=affected_chain_ids,
            fitness=payload.fitness,
            objectives=payload.objectives,
            ga_config=payload.ga_config,
            tags=payload.tags,
            launch_config=payload.launch_config,
        )
        ok = await self._persist(record)
        if not ok:
            raise EvolutionPersistError(f"failed to persist evolution {record.evolution_id} to MinIO")
        await self._bus.publish(
            record.evolution_id,
            "created",
            {"status": record.status.value, "name": record.name},
        )
        logger.info(
            f"evolution created id={record.evolution_id} name={record.name!r} "
            f"seeds={len(record.seed_chains)} pop={record.ga_config.population_size}"
        )
        return record

    async def list_evolutions(
        self,
        *,
        status: Optional[EvolutionStatus] = None,
        tag: Optional[str] = None,
        q: Optional[str] = None,
        cursor: Optional[str] = None,
        limit: int = 20,
    ) -> EvolutionsListResponse:
        """Paginated read of every evolution record under
        ``evolutions/<id>.json``.

        Filters compose:

        * ``status`` — exact match against ``EvolutionResponse.status``.
        * ``tag``  — case-insensitive membership in the record's
          ``tags`` list.
        * ``q``    — case-insensitive substring match against ``name``.

        Cursor pagination: ``cursor`` is the ``evolution_id`` of the
        FIRST item AFTER which to start, ordered by ``created_at``
        descending. We use that ordering so CARE's "most recent" view
        is the natural first page.
        """
        keys = await self._storage.list_objects(prefix=EVOLUTIONS_PREFIX, recursive=False)
        records: List[EvolutionResponse] = []
        for key in keys:
            # Skip the per-evolution ``individuals/`` subdirectory keys —
            # only the bare ``evolutions/<id>.json`` blobs are records.
            if not key.endswith(".json") or "/individuals/" in key:
                continue
            raw = await self._storage.download_bytes(key)
            if not raw:
                continue
            try:
                records.append(EvolutionResponse.model_validate_json(raw))
            except ValueError:
                logger.warning(f"skipping corrupt evolution at {key}")
                continue

        if status is not None:
            records = [r for r in records if r.status == status]
        if tag is not None:
            needle = tag.strip().lower()
            records = [r for r in records if any(needle == t.lower() for t in r.tags)]
        if q is not None:
            substr = q.strip().lower()
            if substr:
                records = [r for r in records if substr in r.name.lower()]

        records.sort(key=lambda r: r.created_at, reverse=True)
        total_scanned = len(records)

        start = 0
        if cursor:
            # Find the cursor record (by id) and start AFTER it. If the
            # cursor no longer matches a record (deleted, filtered out),
            # fall back to start=0 — fewer surprises than 404.
            for i, r in enumerate(records):
                if r.evolution_id == cursor:
                    start = i + 1
                    break
        page = records[start : start + limit]
        next_cursor = page[-1].evolution_id if len(page) == limit and start + limit < len(records) else None
        return EvolutionsListResponse(
            items=page,
            next_cursor=next_cursor,
            total_scanned=total_scanned,
        )

    async def get_evolution(self, evolution_id: str) -> EvolutionResponse:
        raw = await self._storage.download_bytes(_object_key(evolution_id))
        if not raw:
            raise EvolutionNotFoundError(evolution_id)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EvolutionPersistError(f"evolution record {evolution_id} is corrupt: {exc}") from exc
        return EvolutionResponse.model_validate(payload)

    async def _persist(self, record: EvolutionResponse) -> bool:
        record.updated_at = datetime.now(timezone.utc)
        payload = record.model_dump_json(indent=2).encode("utf-8")
        return await self._storage.upload_bytes(
            payload,
            _object_key(record.evolution_id),
            metadata={"type": "evolution_record", "status": record.status.value},
        )

    async def update_status(
        self,
        evolution_id: str,
        status: EvolutionStatus,
        *,
        error_message: Optional[str] = None,
        current_generation: Optional[int] = None,
        best_individual_id: Optional[str] = None,
    ) -> EvolutionResponse:
        """Patch a stored evolution. Used by §4.2 status reads + §4.4 accept."""
        record = await self.get_evolution(evolution_id)
        record.status = status
        if error_message is not None:
            record.error_message = error_message
        if current_generation is not None:
            record.current_generation = current_generation
        if best_individual_id is not None:
            record.best_individual_id = best_individual_id
        ok = await self._persist(record)
        if not ok:
            raise EvolutionPersistError(f"failed to update evolution {evolution_id}")
        # Map status transitions onto the canonical SSE event types CARE
        # listens for; richer events (individual_evaluated, best_updated)
        # are published by the evolution loop directly.
        event_type = {
            EvolutionStatus.RUNNING: "generation_started",
            EvolutionStatus.PAUSED: "paused",
            EvolutionStatus.COMPLETED: "completed",
            EvolutionStatus.FAILED: "failed",
            EvolutionStatus.CANCELLED: "cancelled",
        }.get(status, "status_changed")
        payload: dict = {"status": status.value, "current_generation": record.current_generation}
        if best_individual_id is not None:
            payload["best_individual_id"] = best_individual_id
        if error_message is not None:
            payload["error_message"] = error_message
        await self._bus.publish(evolution_id, event_type, payload)
        return record

    async def cancel_evolution(self, evolution_id: str) -> EvolutionResponse:
        record = await self.get_evolution(evolution_id)
        if record.status in {
            EvolutionStatus.COMPLETED,
            EvolutionStatus.FAILED,
            EvolutionStatus.CANCELLED,
            EvolutionStatus.ACCEPTED,
        }:
            raise EvolutionControlError(f"cannot cancel evolution in status {record.status.value}")
        record.status = EvolutionStatus.CANCELLED
        ok = await self._persist(record)
        if not ok:
            raise EvolutionPersistError(f"failed to cancel evolution {evolution_id}")
        await self._bus.publish(
            evolution_id,
            "cancelled",
            {"status": record.status.value, "current_generation": record.current_generation},
        )
        return record

    async def pause_evolution(self, evolution_id: str) -> EvolutionResponse:
        record = await self.get_evolution(evolution_id)
        if record.status not in {EvolutionStatus.QUEUED, EvolutionStatus.RUNNING}:
            raise EvolutionControlError(f"cannot pause evolution in status {record.status.value}")
        record.status = EvolutionStatus.PAUSED
        ok = await self._persist(record)
        if not ok:
            raise EvolutionPersistError(f"failed to pause evolution {evolution_id}")
        await self._bus.publish(
            evolution_id,
            "paused",
            {"status": record.status.value, "current_generation": record.current_generation},
        )
        return record

    async def resume_evolution(self, evolution_id: str) -> EvolutionResponse:
        record = await self.get_evolution(evolution_id)
        if record.status != EvolutionStatus.PAUSED:
            raise EvolutionControlError(f"cannot resume evolution in status {record.status.value}")
        record.status = EvolutionStatus.RUNNING
        ok = await self._persist(record)
        if not ok:
            raise EvolutionPersistError(f"failed to resume evolution {evolution_id}")
        await self._bus.publish(
            evolution_id,
            "resumed",
            {"status": record.status.value, "current_generation": record.current_generation},
        )
        return record

    # ------------------------------------------------------------------
    # Individuals (§4.2)
    # ------------------------------------------------------------------

    async def record_individual(
        self,
        evolution_id: str,
        payload: IndividualCreate,
    ) -> IndividualResponse:
        """Persist one evaluated individual and publish bus events.

        Side effects on the parent evolution record:

        * ``current_generation`` is bumped to ``max(current_generation,
          payload.generation)``.
        * ``best_individual_id`` flips to the new individual when its
          primary-objective score wins (direction comes from
          ``fitness.higher_is_better``).

        Publishes an ``individual_evaluated`` event on every call and a
        follow-up ``best_updated`` event when the best changes.
        """
        evolution = await self.get_evolution(evolution_id)
        unknown = [obj for obj in payload.fitness_scores if obj not in evolution.objectives]
        if unknown:
            raise IndividualValidationError(
                f"unknown objectives: {sorted(unknown)} (expected subset of {evolution.objectives})"
            )

        individual = IndividualResponse(
            evolution_id=evolution_id,
            generation=payload.generation,
            chain_content=payload.chain_content,
            fitness_scores=dict(payload.fitness_scores),
            parent_ids=list(payload.parent_ids),
            mutation_kind=payload.mutation_kind,
        )
        ok = await self._storage.upload_bytes(
            individual.model_dump_json(indent=2).encode("utf-8"),
            _individual_object_key(evolution_id, individual.individual_id),
            metadata={
                "type": "evolution_individual",
                "evolution_id": evolution_id,
                "generation": str(individual.generation),
            },
        )
        if not ok:
            raise EvolutionPersistError(
                f"failed to persist individual {individual.individual_id} for {evolution_id}"
            )

        await self._bus.publish(
            evolution_id,
            "individual_evaluated",
            {
                "individual_id": individual.individual_id,
                "generation": individual.generation,
                "fitness": individual.fitness_scores.get(evolution.objectives[0])
                if evolution.objectives
                else None,
                "fitness_scores": individual.fitness_scores,
                "objectives": individual.fitness_scores,
                "parent_id": individual.parent_ids[0] if individual.parent_ids else None,
                "parent_ids": list(individual.parent_ids),
                "mutation_kind": individual.mutation_kind,
                "chain_content": individual.chain_content,
            },
        )

        memory_entity_id = await self._forward_individual_to_memory(
            evolution, individual, channel="latest"
        )
        if memory_entity_id:
            logger.debug(
                f"memory: saved individual {individual.individual_id} as entity "
                f"{memory_entity_id}"
            )

        primary = evolution.objectives[0]
        new_score = individual.fitness_scores.get(primary)
        previous_best_id = evolution.best_individual_id
        best_changed = False
        if new_score is not None:
            previous_best = await self._maybe_get_individual(evolution_id, previous_best_id)
            previous_best_score = (
                previous_best.fitness_scores.get(primary) if previous_best is not None else None
            )
            higher_is_better = evolution.fitness.higher_is_better
            if previous_best_score is None or _wins(new_score, previous_best_score, higher_is_better):
                best_changed = True

        update_args: dict = {}
        if individual.generation > evolution.current_generation:
            update_args["current_generation"] = individual.generation
        if best_changed:
            update_args["best_individual_id"] = individual.individual_id
        if update_args:
            # Patch + persist + emit a status_changed event for the bus.
            await self.update_status(evolution_id, evolution.status, **update_args)
            if best_changed:
                await self._bus.publish(
                    evolution_id,
                    "best_updated",
                    {
                        "individual_id": individual.individual_id,
                        "generation": individual.generation,
                        "primary_objective": primary,
                        "score": new_score,
                        "fitness_scores": individual.fitness_scores,
                        "objectives": individual.fitness_scores,
                    },
                )
        return individual

    async def _maybe_get_individual(
        self, evolution_id: str, individual_id: Optional[str]
    ) -> Optional[IndividualResponse]:
        if not individual_id:
            return None
        try:
            return await self.get_individual(evolution_id, individual_id)
        except IndividualNotFoundError:
            return None

    async def accept_individual(
        self,
        evolution_id: str,
        individual_id: str,
        *,
        chain_id_to_update: Optional[str] = None,
        note: Optional[str] = None,
    ) -> EvolutionResponse:
        """Promote ``individual_id`` to be the evolution's accepted winner.

        Side effects (all persisted to MinIO before returning):

        1. The individual record's ``accepted`` flag is flipped to True so a
           ``GET /individuals/<id>`` reflects the decision.
        2. The parent evolution record is patched: ``status=ACCEPTED``,
           ``accepted_individual_id=<id>``, ``accepted_at=now``, and
           ``best_individual_id=<id>`` (operator override of the metric-based
           best — even if the chosen one isn't top of the primary objective,
           the human's call wins).
        3. An ``accepted`` event is published on the bus.

        Idempotent on the *same* individual: a second accept call with the
        same id just re-emits the event. Switching to a different individual
        on an already-accepted evolution is rejected with
        :class:`EvolutionAcceptError` so we don't silently invalidate a
        downstream Memory write (§4.6).
        """
        evolution = await self.get_evolution(evolution_id)
        affected_chain_ids = _effective_affected_chain_ids(evolution)
        if chain_id_to_update and chain_id_to_update not in affected_chain_ids:
            raise EvolutionAcceptError(
                f"chain_id_to_update {chain_id_to_update!r} is not affected by evolution {evolution_id}"
            )
        if (
            evolution.status == EvolutionStatus.ACCEPTED
            and evolution.accepted_individual_id
            and evolution.accepted_individual_id != individual_id
        ):
            raise EvolutionAcceptError(
                f"evolution {evolution_id} already accepted "
                f"{evolution.accepted_individual_id}; refusing to switch to {individual_id}"
            )
        if (
            evolution.status == EvolutionStatus.ACCEPTED
            and evolution.accepted_individual_id == individual_id
        ):
            if (
                chain_id_to_update
                and evolution.accepted_chain_id
                and chain_id_to_update != evolution.accepted_chain_id
            ):
                raise EvolutionAcceptError(
                    f"evolution {evolution_id} already accepted into "
                    f"{evolution.accepted_chain_id}; refusing {chain_id_to_update}"
                )
            await self._bus.publish(
                evolution_id,
                "accepted",
                {
                    "individual_id": individual_id,
                    "chain_id": evolution.accepted_chain_id,
                    "previous_version": evolution.previous_version,
                    "new_version": evolution.new_version,
                    "version_id": evolution.new_version_id,
                    "note": note,
                },
            )
            return evolution

        individual = await self.get_individual(evolution_id, individual_id)
        now = datetime.now(timezone.utc)
        chain_id = chain_id_to_update or (affected_chain_ids[0] if affected_chain_ids else None)
        promotion: Optional[MemoryPromotionResult] = None
        if chain_id:
            if self._memory is None or not self._memory.is_configured:
                raise EvolutionAcceptError("Memory API URL is required to accept a Memory-backed evolution")
            primary = evolution.objectives[0] if evolution.objectives else None
            try:
                promotion = await self._memory.promote_evolved_chain(
                    chain_id=chain_id,
                    content=individual.chain_content,
                    evolution_meta=self._accept_evolution_meta(
                        evolution,
                        individual,
                        accepted_at=now,
                        primary_objective=primary,
                        chain_id=chain_id,
                        note=note,
                    ),
                    name=evolution.name,
                    tags=list(evolution.tags),
                    description=evolution.description,
                )
            except MemoryPromotionError as exc:
                raise EvolutionAcceptError(str(exc)) from exc

        if not individual.accepted:
            individual = individual.model_copy(update={"accepted": True})
            ok = await self._storage.upload_bytes(
                individual.model_dump_json(indent=2).encode("utf-8"),
                _individual_object_key(evolution_id, individual_id),
                metadata={
                    "type": "evolution_individual",
                    "evolution_id": evolution_id,
                    "generation": str(individual.generation),
                    "accepted": "true",
                },
            )
            if not ok:
                raise EvolutionPersistError(
                    f"failed to flip accepted=True on {individual_id}"
                )

        record = await self.get_evolution(evolution_id)
        record.status = EvolutionStatus.ACCEPTED
        record.accepted_individual_id = individual_id
        record.accepted_at = now
        # Operator override: the human's choice IS the best for downstream
        # consumers, even if a higher metric was hit elsewhere.
        record.best_individual_id = individual_id
        if promotion is not None:
            record.accepted_chain_id = promotion.chain_id
            record.previous_version = promotion.previous_version
            record.new_version = promotion.new_version
            record.new_version_id = promotion.new_version_id
        ok = await self._persist(record)
        if not ok:
            raise EvolutionPersistError(f"failed to persist acceptance for {evolution_id}")

        await self._bus.publish(
            evolution_id,
            "accepted",
            {
                "individual_id": individual_id,
                "chain_id": record.accepted_chain_id,
                "previous_version": record.previous_version,
                "new_version": record.new_version,
                "version_id": record.new_version_id,
                "generation": individual.generation,
                "fitness_scores": individual.fitness_scores,
                "note": note,
            },
        )

        logger.info(
            f"evolution {evolution_id} accepted individual={individual_id} "
            f"gen={individual.generation}"
        )
        return record

    async def get_individual(self, evolution_id: str, individual_id: str) -> IndividualResponse:
        raw = await self._storage.download_bytes(_individual_object_key(evolution_id, individual_id))
        if not raw:
            raise IndividualNotFoundError(individual_id)
        try:
            return IndividualResponse.model_validate_json(raw)
        except ValueError as exc:
            raise EvolutionPersistError(
                f"individual {individual_id} for {evolution_id} is corrupt: {exc}"
            ) from exc

    async def list_individuals(
        self,
        evolution_id: str,
        *,
        generation: Optional[int] = None,
        pareto: bool = False,
        limit: int = 100,
    ) -> List[IndividualResponse]:
        """Return every individual matching the filters.

        ``generation`` keeps only matching items; ``pareto`` reduces the
        set to its non-dominated front under the evolution's objectives
        (after the generation filter, if any). ``limit`` is applied last.
        Existence of the parent evolution is required — unknown ids raise
        :class:`EvolutionNotFoundError` so the route can map to 404.
        """
        evolution = await self.get_evolution(evolution_id)
        # ``list_objects`` returns keys; we then fetch + parse each.
        keys = await self._storage.list_objects(
            prefix=_individuals_prefix(evolution_id), recursive=True
        )
        items: List[IndividualResponse] = []
        for key in keys:
            if not key.endswith(".json"):
                continue
            raw = await self._storage.download_bytes(key)
            if not raw:
                continue
            try:
                items.append(IndividualResponse.model_validate_json(raw))
            except ValueError:
                logger.warning(f"skipping corrupt individual at {key}")
                continue

        if generation is not None:
            items = [i for i in items if i.generation == generation]
        if pareto:
            items = pareto_front(items, evolution.objectives, evolution.fitness.higher_is_better)
        items.sort(key=lambda i: (i.generation, i.created_at))
        return items[:limit]


def _wins(candidate: float, current_best: float, higher_is_better: bool) -> bool:
    return candidate > current_best if higher_is_better else candidate < current_best


def pareto_front(
    items: List[IndividualResponse],
    objectives: List[str],
    higher_is_better: bool,
) -> List[IndividualResponse]:
    """Non-dominated subset of ``items`` under ``objectives``.

    Multi-objective: ``higher_is_better`` applies uniformly to every axis
    (matching the single-direction fitness model in §4.1). An item A
    dominates B iff A is at least as good on every objective and strictly
    better on at least one. Items missing a score are treated as worst on
    that axis so they're cheaply weeded out of the front.
    """

    def score(item: IndividualResponse, obj: str) -> float:
        v = item.fitness_scores.get(obj)
        if v is None:
            return float("-inf") if higher_is_better else float("inf")
        return v

    def dominates(a: IndividualResponse, b: IndividualResponse) -> bool:
        any_strict = False
        for obj in objectives:
            sa, sb = score(a, obj), score(b, obj)
            if higher_is_better:
                if sa < sb:
                    return False
                if sa > sb:
                    any_strict = True
            else:
                if sa > sb:
                    return False
                if sa < sb:
                    any_strict = True
        return any_strict

    front: List[IndividualResponse] = []
    for candidate in items:
        if any(dominates(other, candidate) for other in items if other is not candidate):
            continue
        front.append(candidate)
    return front
