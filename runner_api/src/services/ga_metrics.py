"""GA generation metrics derived from gigaevo program lineages.

gigaevo-core stores scheduler step indices in Redis metric list ``s`` fields.
Those counters grow faster than user-facing GA generations and must **not** be
shown as ``generation`` in dashboards.  The lineage ``generation`` on each
``<uuid>:program:*`` record is the authoritative GA round index (seed = 1).
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional


def lineage_generation(program: Dict[str, Any]) -> Optional[int]:
    """Return the raw 1-based lineage generation for a program dict."""
    lineage = program.get("lineage")
    if isinstance(lineage, dict):
        raw = lineage.get("generation")
        if isinstance(raw, (int, float)) and raw >= 1:
            return int(raw)
    raw = program.get("generation")
    if isinstance(raw, (int, float)) and raw >= 1:
        return int(raw)
    return None


def lineage_to_display_generation(lineage_gen: int) -> int:
    """Map gigaevo lineage (seed = 1) to 0-based UI generation."""
    return max(0, int(lineage_gen) - 1)


def current_display_generation(programs: List[Dict[str, Any]]) -> int:
    """Highest 0-based GA generation across program payloads."""
    max_lineage = 0
    for program in programs:
        gen = lineage_generation(program)
        if gen is not None:
            max_lineage = max(max_lineage, gen)
    if max_lineage <= 0:
        return 0
    return lineage_to_display_generation(max_lineage)


def ga_limit_reached(programs: List[Dict[str, Any]], max_iterations: int) -> bool:
    """True when the deepest lineage has completed ``max_iterations`` GA rounds."""
    if max_iterations <= 0:
        return False
    max_lineage = 0
    for program in programs:
        gen = lineage_generation(program)
        if gen is not None:
            max_lineage = max(max_lineage, gen)
    if max_lineage <= 0:
        return False
    return lineage_to_display_generation(max_lineage) >= max_iterations


def fitness_history_from_programs(
    programs: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Build ``{generation, best_fitness, current_fitness}`` rows by GA generation."""
    buckets: Dict[int, List[float]] = {}
    for program in programs:
        gen = lineage_generation(program)
        if gen is None:
            continue
        metrics = program.get("metrics")
        if not isinstance(metrics, dict):
            continue
        if metrics.get("is_valid") not in (1, 1.0, True):
            continue
        fitness = metrics.get("fitness")
        if not isinstance(fitness, (int, float)) or fitness <= -999:
            continue
        display_gen = lineage_to_display_generation(gen)
        buckets.setdefault(display_gen, []).append(float(fitness))

    history: List[Dict[str, Any]] = []
    for display_gen in sorted(buckets):
        values = buckets[display_gen]
        if not values:
            continue
        history.append(
            {
                "generation": display_gen,
                "best_fitness": max(values),
                "current_fitness": sum(values) / len(values),
            }
        )
    return history


def parse_program_payload(raw: Any) -> Optional[Dict[str, Any]]:
    if not raw:
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (bytes, str)):
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def completed_generations_count(programs: List[Dict[str, Any]]) -> int:
    """Number of completed GA rounds (display generation + 1, minimum 0)."""
    return current_display_generation(programs) + 1 if programs else 0
