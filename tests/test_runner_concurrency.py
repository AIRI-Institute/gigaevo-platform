"""Per-runner concurrency helpers in DatabaseService.

Default ``max_concurrent_experiments_per_runner=1`` preserves binary
capacity; >1 lets one runner host several experiments. These tests cover
the migration-free active-experiment counting + the limit resolution that
the allocation / release / reconcile paths branch on.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parent.parent
MASTER_SRC = REPO_ROOT / "master_api" / "src"
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(MASTER_SRC.parent))
for _stale in [m for m in list(sys.modules) if m == "src" or m.startswith("src.")]:
    sys.modules.pop(_stale, None)

db_mod = importlib.import_module("src.services.database_service")
DatabaseService = db_mod.DatabaseService


class _FakeScalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return _FakeScalars(self._rows)


class _FakeSession:
    def __init__(self, rows):
        self._rows = rows

    async def execute(self, _query):
        return _FakeResult(self._rows)


def _exp(eid, runner):
    return SimpleNamespace(
        id=eid, status="running", config={"assigned_runner_id": runner}
    )


def _bare_db():
    # Skip __init__ (which needs a DB engine) — we only exercise pure logic.
    return DatabaseService.__new__(DatabaseService)


class TestConcurrencyLimit:
    def test_default_is_one(self):
        db = _bare_db()
        db.config = SimpleNamespace(runner=SimpleNamespace())
        assert db._runner_concurrency_limit() == 1

    def test_reads_config_value(self):
        db = _bare_db()
        db.config = SimpleNamespace(
            runner=SimpleNamespace(max_concurrent_experiments_per_runner=4)
        )
        assert db._runner_concurrency_limit() == 4

    def test_floors_at_one(self):
        db = _bare_db()
        db.config = SimpleNamespace(
            runner=SimpleNamespace(max_concurrent_experiments_per_runner=0)
        )
        assert db._runner_concurrency_limit() == 1


class TestActiveCount:
    def test_counts_per_runner(self):
        db = _bare_db()
        rows = [_exp("e1", "r1"), _exp("e2", "r1"), _exp("e3", "r2")]
        session = _FakeSession(rows)
        assert asyncio.run(db._count_active_experiments_for_runner(session, "r1")) == 2
        assert asyncio.run(db._count_active_experiments_for_runner(session, "r2")) == 1

    def test_exclude_experiment(self):
        db = _bare_db()
        rows = [_exp("e1", "r1"), _exp("e2", "r1")]
        session = _FakeSession(rows)
        n = asyncio.run(
            db._count_active_experiments_for_runner(
                session, "r1", exclude_experiment_id="e1"
            )
        )
        assert n == 1

    def test_first_active_excludes(self):
        db = _bare_db()
        rows = [_exp("e1", "r1"), _exp("e2", "r1")]
        session = _FakeSession(rows)
        first = asyncio.run(
            db._first_active_experiment_for_runner(
                session, "r1", exclude_experiment_id="e1"
            )
        )
        assert first == "e2"
