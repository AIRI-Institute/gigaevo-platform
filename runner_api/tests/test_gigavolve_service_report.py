import pandas as pd

from runner_api.src.services.gigavolve_service import _build_evolution_summary


def _build_summary(rows, *, higher_is_better=True):
    return _build_evolution_summary(pd.DataFrame(rows), "exp_test", higher_is_better)


def test_done_valid_row_populates_best_fields():
    summary = _build_summary(
        [
            {
                "program_id": "prog-1",
                "state": "done",
                "metric_is_valid": "1",
                "metric_fitness": "0.75",
                "generation": "3",
                "created_at": "2026-03-11T10:00:00Z",
                "metadata_iteration": "2",
                "code": "def solve():\n    return 42\n",
            }
        ]
    )

    assert summary["total_iterations"] == 3
    assert summary["total_programs"] == 1
    assert summary["total_programs_complete"] == 1
    assert summary["best_program_id"] == "prog-1"
    assert summary["best_fitness"] == 0.75
    assert summary["best_generation"] == 3
    assert summary["best_created_at"] == "2026-03-11 10:00:00+00:00"
    assert summary["best_program"] == "def solve():\n    return 42\n"


def test_multiple_done_rows_pick_highest_fitness_for_maximize():
    summary = _build_summary(
        [
            {
                "program_id": "prog-low",
                "state": "done",
                "metric_is_valid": 1,
                "metric_fitness": 0.5,
                "generation": 2,
                "created_at": "2026-03-11T10:00:00Z",
                "code": "low",
            },
            {
                "program_id": "prog-high",
                "state": "done",
                "metric_is_valid": 1,
                "metric_fitness": 0.9,
                "generation": 1,
                "created_at": "2026-03-11T09:00:00Z",
                "code": "high",
            },
        ]
    )

    assert summary["best_program_id"] == "prog-high"
    assert summary["best_fitness"] == 0.9


def test_multiple_done_rows_pick_lowest_fitness_for_minimize():
    summary = _build_summary(
        [
            {
                "program_id": "prog-high",
                "state": "done",
                "metric_is_valid": 1,
                "metric_fitness": 10.0,
                "generation": 2,
                "created_at": "2026-03-11T10:00:00Z",
                "code": "high",
            },
            {
                "program_id": "prog-low",
                "state": "done",
                "metric_is_valid": 1,
                "metric_fitness": 1.5,
                "generation": 1,
                "created_at": "2026-03-11T09:00:00Z",
                "code": "low",
            },
        ],
        higher_is_better=False,
    )

    assert summary["best_program_id"] == "prog-low"
    assert summary["best_fitness"] == 1.5


def test_discarded_rows_do_not_count_as_complete_or_best_even_if_marked_complete():
    summary = _build_summary(
        [
            {
                "program_id": "discarded-prog",
                "state": "discarded",
                "is_complete": True,
                "metric_is_valid": 1,
                "metric_fitness": 999.0,
                "generation": 9,
                "created_at": "2026-03-11T12:00:00Z",
                "code": "discarded",
            },
            {
                "program_id": "done-prog",
                "state": "done",
                "is_complete": True,
                "metric_is_valid": 1,
                "metric_fitness": 10.0,
                "generation": 2,
                "created_at": "2026-03-11T10:00:00Z",
                "code": "done",
            },
        ]
    )

    assert summary["total_programs_complete"] == 1
    assert summary["best_program_id"] == "done-prog"


def test_invalid_done_rows_do_not_populate_best_fields():
    summary = _build_summary(
        [
            {
                "program_id": "invalid-done",
                "state": "done",
                "metric_is_valid": 0,
                "metric_fitness": 100.0,
                "generation": 5,
                "created_at": "2026-03-11T10:00:00Z",
                "code": "invalid",
            }
        ]
    )

    assert summary["total_programs_complete"] == 1
    assert summary["best_program_id"] is None
    assert summary["best_fitness"] is None
    assert summary["best_generation"] is None
    assert summary["best_created_at"] is None
    assert summary["best_program"] is None


def test_mixed_states_count_only_done_program_ids():
    summary = _build_summary(
        [
            {"program_id": "queued-1", "state": "queued"},
            {"program_id": "running-1", "state": "running"},
            {"program_id": "discarded-1", "state": "discarded", "is_complete": True},
            {"program_id": "done-1", "state": "done", "metric_is_valid": 1, "metric_fitness": 0.1},
            {"program_id": "done-2", "state": "done", "metric_is_valid": 1, "metric_fitness": 0.2},
        ]
    )

    assert summary["total_programs"] == 5
    assert summary["total_programs_complete"] == 2
