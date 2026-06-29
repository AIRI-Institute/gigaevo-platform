"""Unit tests for lineage-based GA generation metrics."""

from src.services.ga_metrics import (
    completed_generations_count,
    current_display_generation,
    ga_limit_reached,
)


def _program(lineage_gen: int) -> dict:
    return {"lineage": {"generation": lineage_gen}}


def test_completed_generations_count():
    programs = [_program(1), _program(2), _program(3)]
    assert current_display_generation(programs) == 2
    assert completed_generations_count(programs) == 3


def test_ga_limit_reached_respects_zero_limit():
    assert ga_limit_reached([_program(5)], 0) is False
