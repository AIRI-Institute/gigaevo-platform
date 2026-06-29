"""Unit tests for lineage-based GA generation metrics."""

import pytest
from src.services.ga_metrics import (
    current_display_generation,
    fitness_history_from_programs,
    ga_limit_reached,
    lineage_generation,
    lineage_to_display_generation,
)


@pytest.fixture(scope="module", autouse=True)
def ensure_services_ready():
    yield


def _program(lineage_gen: int, fitness: float, *, valid: bool = True) -> dict:
    return {
        "lineage": {"generation": lineage_gen},
        "metrics": {
            "generation": lineage_gen,
            "fitness": fitness,
            "is_valid": 1 if valid else 0,
        },
    }


def test_lineage_to_display_generation_seed_is_zero():
    assert lineage_to_display_generation(1) == 0


def test_current_display_generation_uses_max_lineage():
    programs = [
        _program(1, 0.1),
        _program(2, 0.2),
        _program(3, 0.16),
    ]
    assert current_display_generation(programs) == 2


def test_ga_limit_reached_at_configured_depth():
    programs = [_program(i, 0.1) for i in range(1, 12)]
    assert ga_limit_reached(programs, 10) is True
    assert ga_limit_reached(programs[:3], 10) is False


def test_fitness_history_groups_by_display_generation():
    programs = [
        _program(1, 0.1),
        _program(2, 0.15),
        _program(2, 0.20),
        _program(3, 0.16),
    ]
    history = fitness_history_from_programs(programs)
    assert [row["generation"] for row in history] == [0, 1, 2]
    assert history[1]["best_fitness"] == 0.20
    assert history[1]["current_fitness"] == 0.175


def test_lineage_generation_falls_back_to_top_level_generation():
    program = {"generation": 4, "metrics": {"fitness": 0.5, "is_valid": 1}}
    assert lineage_generation(program) == 4
