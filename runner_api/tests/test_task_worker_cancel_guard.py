import asyncio

from runner_api.src.models.task import Task, TaskStatus, TaskType
from runner_api.src.workers import task_worker
from runner_api.src.workers.task_worker import TaskWorker


class DummyRepo:
    def __init__(self):
        self.cancelled = False
        self.persisted_status = None

    async def is_task_cancelled(self, task_id: str) -> bool:
        return self.cancelled

    async def persist_task_state(self, task: Task) -> None:
        self.persisted_status = task.status

    async def cancel_task(self, task_id):
        self.cancelled = True
        return True


class DummyGigaEvolve:
    async def generate_code_from_llm(self, *args, **kwargs):
        return "ok"

    async def run_experiment(self, *args, **kwargs):
        return {"success": True}


class DummyRedis:
    async def hincrby(self, *args, **kwargs):
        return 1


def _use_fake_redis(monkeypatch):
    async def fake_get_redis():
        return DummyRedis()

    monkeypatch.setattr(task_worker, "get_redis", fake_get_redis)


def test_task_worker_cancel_guard(monkeypatch):
    repo = DummyRepo()
    _use_fake_redis(monkeypatch)

    worker = TaskWorker(
        worker_id="worker-1",
        name="test-worker",
        gigavolve_service=DummyGigaEvolve(),
        task_repository=repo,
    )

    async def no_op(*args, **kwargs):
        return None

    worker._update_worker_status = no_op
    worker._maybe_delete_after_cancel = no_op
    worker._update_experiment_status_after_run = no_op

    async def handle_generate_code(task):
        task.status = TaskStatus.COMPLETED
        repo.cancelled = True

    worker._handle_generate_code = handle_generate_code

    task = Task(experiment_id="exp1", task_type=TaskType.GENERATE_CODE)
    asyncio.run(worker._execute_task(task))

    assert repo.persisted_status == TaskStatus.CANCELLED
