"""Pydantic schemas for the ``/api/v1/evolutions`` endpoints (CARE §4.1+).

An *evolution* is a population-level GA run over one or more seed chains
pulled from Memory. It's a higher-level concept than a chain experiment:
one evolution spans `population_size` individuals across `max_iterations`
generations, with optional multi-objective fitness.

For §4.1 we land the create + persist flow — §4.2 (read), §4.3 (SSE),
§4.4 (accept) build on the same record shape.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, model_validator


class EvolutionStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ACCEPTED = "accepted"  # an individual was promoted (§4.4); strictly past COMPLETED


class SeedChain(BaseModel):
    """A single seed for the initial population.

    Either ``chain_content`` is provided inline (useful for tests / CARE's
    bring-up before §4.6 wires Memory integration), OR ``memory_chain_id``
    points at a stored chain that the platform will hydrate from Memory.
    Exactly one of the two must be set.
    """

    memory_chain_id: Optional[str] = Field(
        None,
        description="Chain entity ID in gigaevo-memory; resolved at evolution start",
    )
    memory_version_id: Optional[str] = Field(
        None,
        description="Optional specific version; defaults to channel 'latest'",
    )
    chain_content: Optional[Dict[str, Any]] = Field(
        None,
        description="Inline chain JSON (used when memory_chain_id is absent)",
    )

    @model_validator(mode="after")
    def _exactly_one_source(self) -> "SeedChain":
        if self.memory_chain_id and self.chain_content is not None:
            raise ValueError("seed_chain: pass either memory_chain_id OR chain_content, not both")
        if not self.memory_chain_id and self.chain_content is None:
            raise ValueError("seed_chain: must provide memory_chain_id or chain_content")
        return self


class FitnessSpec(BaseModel):
    """Natural-language fitness criterion + optional judge model id."""

    prompt: str = Field(..., min_length=1, description="Judge prompt the GA evaluates each individual against")
    model_id: Optional[str] = Field(
        None,
        description="LLM model id (from llm_models.yml) used as judge; default = platform default",
    )
    # ``higher_is_better`` is True for accuracy-like metrics, False for
    # latency/cost-like ones. CARE's EvolutionScreen will surface this so
    # users don't accidentally minimise their fitness.
    higher_is_better: bool = True


class GAConfig(BaseModel):
    """Genetic-algorithm hyperparameters."""

    population_size: int = Field(8, ge=1, le=256)
    max_iterations: int = Field(20, ge=1, le=1000)
    mutation_rate: float = Field(0.3, ge=0.0, le=1.0)
    crossover_rate: float = Field(0.5, ge=0.0, le=1.0)
    elitism: int = Field(1, ge=0)
    random_seed: Optional[int] = Field(None, description="Pin RNG for reproducibility; omit for fresh seed")


class EvolutionCreate(BaseModel):
    """Body of ``POST /api/v1/evolutions``."""

    name: str = Field(..., min_length=1, max_length=200)
    description: Optional[str] = Field(None, max_length=2000)
    seed_chains: List[SeedChain] = Field(..., min_length=1, max_length=64)
    fitness: FitnessSpec
    objectives: List[str] = Field(
        default_factory=lambda: ["fitness"],
        description="Names of evaluation axes; first one is treated as primary",
        max_length=8,
    )
    ga_config: GAConfig = Field(default_factory=GAConfig)
    tags: List[str] = Field(default_factory=list, max_length=32)
    launch_config: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("objectives")
    @classmethod
    def _validate_objectives_distinct(cls, v: List[str]) -> List[str]:
        cleaned = [o.strip() for o in v if isinstance(o, str) and o.strip()]
        if not cleaned:
            raise ValueError("objectives: at least one non-empty objective name required")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("objectives: must be unique")
        return cleaned


class CareEvolutionLaunch(BaseModel):
    """CARE-facing shorthand body for ``POST /api/v1/evolutions``.

    CARE's TUI launch modal submits the user-facing fields it owns. The
    platform normalises them into :class:`EvolutionCreate` at the HTTP
    boundary so internal persistence keeps one canonical record shape.
    """

    base_chain_id: str = Field(..., min_length=1)
    evolution_mode: str = Field("full_chain", max_length=64)
    max_iterations: int = Field(10, ge=1, le=1000)
    population_size: int = Field(8, ge=1, le=256)
    validation_criteria: Optional[str] = Field(None, max_length=4000)
    test_data_path: Optional[str] = Field(None, max_length=2000)
    validation_threshold: Optional[float] = Field(None, ge=0.0, le=1.0)
    objectives: List[str] = Field(default_factory=lambda: ["fitness"], max_length=8)
    tags: List[str] = Field(default_factory=list, max_length=32)
    name: Optional[str] = Field(None, max_length=200)
    description: Optional[str] = Field(None, max_length=2000)
    constraints: Dict[str, Any] = Field(default_factory=dict)
    extras: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("objectives")
    @classmethod
    def _validate_objectives_distinct(cls, v: List[str]) -> List[str]:
        cleaned = [o.strip() for o in v if isinstance(o, str) and o.strip()]
        if not cleaned:
            raise ValueError("objectives: at least one non-empty objective name required")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("objectives: must be unique")
        return cleaned

    def to_evolution_create(self) -> "EvolutionCreate":
        prompt = (self.validation_criteria or "").strip() or "Improve the selected chain."
        return EvolutionCreate(
            name=self.name or f"CARE evolution for {self.base_chain_id}",
            description=self.description,
            seed_chains=[SeedChain(memory_chain_id=self.base_chain_id)],
            fitness=FitnessSpec(prompt=prompt),
            objectives=list(self.objectives),
            ga_config=GAConfig(
                population_size=self.population_size,
                max_iterations=self.max_iterations,
            ),
            tags=list(self.tags),
            launch_config={
                "base_chain_id": self.base_chain_id,
                "evolution_mode": self.evolution_mode,
                "test_data_path": self.test_data_path,
                "validation_threshold": self.validation_threshold,
                "constraints": dict(self.constraints),
                "extras": dict(self.extras),
            },
        )


class EvolutionResponse(BaseModel):
    """Server's view of an evolution record. Returned by create + get."""

    evolution_id: str = Field(default_factory=lambda: str(uuid4()))
    name: str
    description: Optional[str] = None
    status: EvolutionStatus = EvolutionStatus.QUEUED
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    seed_chains: List[SeedChain]
    affected_chain_ids: List[str] = Field(
        default_factory=list,
        description="Memory chain IDs whose mutable channel pointers may change when this run is accepted.",
    )
    fitness: FitnessSpec
    objectives: List[str]
    ga_config: GAConfig
    tags: List[str] = Field(default_factory=list)
    launch_config: Dict[str, Any] = Field(default_factory=dict)
    # Populated as the evolution progresses; absent until then.
    current_generation: int = 0
    best_individual_id: Optional[str] = None
    accepted_individual_id: Optional[str] = Field(
        None,
        description="Set by §4.4 POST /accept. CARE writes this back to Memory's `stable` channel.",
    )
    accepted_at: Optional[datetime] = None
    accepted_chain_id: Optional[str] = None
    previous_version: Optional[int] = None
    new_version: Optional[int] = None
    new_version_id: Optional[str] = None
    error_message: Optional[str] = None


class AcceptIndividualRequest(BaseModel):
    """Body of ``POST /api/v1/evolutions/{id}/accept``."""

    individual_id: str = Field(..., min_length=1)
    chain_id_to_update: Optional[str] = Field(
        None,
        min_length=1,
        description="Memory chain id whose latest pointer should advance; defaults to the run's first affected chain.",
    )
    note: Optional[str] = Field(
        None,
        max_length=1000,
        description="Optional human-readable rationale; passed through to Memory in §4.6.",
    )


class IndividualCreate(BaseModel):
    """Body accepted by ``record_individual`` (called by the GA loop)."""

    generation: int = Field(..., ge=0)
    chain_content: Dict[str, Any]
    fitness_scores: Dict[str, float] = Field(
        ...,
        description=(
            "Map of objective name → score. Keys MUST be a subset of "
            "EvolutionResponse.objectives (validated on record_individual)."
        ),
    )
    parent_ids: List[str] = Field(default_factory=list, max_length=32)
    mutation_kind: Optional[str] = Field(
        None,
        description="Free-form label for how this individual was derived (mutation, crossover, seed, …)",
        max_length=64,
    )


class IndividualResponse(IndividualCreate):
    """Server's view of an individual."""

    individual_id: str = Field(default_factory=lambda: str(uuid4()))
    evolution_id: str
    accepted: bool = Field(False, description="True once §4.4 accept promoted this individual")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class IndividualsListResponse(BaseModel):
    """Paginated list payload returned by ``GET /{id}/individuals``."""

    evolution_id: str
    total: int
    items: List[IndividualResponse]
    pareto_front: bool = False
    primary_objective: Optional[str] = None
    higher_is_better: bool = True


class EvolutionsListResponse(BaseModel):
    """Paginated list payload returned by ``GET /api/v1/evolutions`` (§4.9).

    Ordering is ``created_at`` descending — most recent first — so CARE's
    "recent evolutions" tab is one fetch. ``next_cursor`` is opaque to
    the caller; pass it back verbatim as the ``cursor`` query param to
    keep paging forward. ``None`` means no further pages.
    """

    items: List[EvolutionResponse]
    next_cursor: Optional[str] = None
    total_scanned: int = Field(
        ...,
        description=(
            "How many storage objects the server inspected while building "
            "this page (post-filter). Useful for spotting filter-heavy "
            "queries that scan a lot to return little."
        ),
    )
