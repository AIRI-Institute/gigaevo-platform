import asyncio
from datetime import datetime, timedelta, timezone

from runner_api.src.models.task import Task, TaskCreate, TaskStatus, TaskType
from runner_api.src.services import task_repository
from runner_api.src.services.task_repository import TaskRepository


class FakePipeline:
    def __init__(self, redis):
        self.redis = redis
        self.ops = []

    def hset(self, key, mapping=None, field=None, value=None):
        self.ops.append(("hset", key, mapping, field, value))
        return self

    def hsetnx(self, key, field, value):
        self.ops.append(("hsetnx", key, field, value))
        return self

    def sadd(self, key, *values):
        self.ops.append(("sadd", key, values))
        return self

    def srem(self, key, *values):
        self.ops.append(("srem", key, values))
        return self

    def lpush(self, key, *values):
        self.ops.append(("lpush", key, values))
        return self

    def lrem(self, key, count, value):
        self.ops.append(("lrem", key, count, value))
        return self

    def hgetall(self, key):
        self.ops.append(("hgetall", key))
        return self

    def delete(self, key):
        self.ops.append(("delete", key))
        return self

    async def execute(self):
        results = []
        for op in self.ops:
            name = op[0]
            if name == "hset":
                _, key, mapping, field, value = op
                results.append(self.redis._hset(key, mapping, field, value))
            elif name == "hsetnx":
                _, key, field, value = op
                results.append(self.redis._hsetnx(key, field, value))
            elif name == "sadd":
                _, key, values = op
                results.append(self.redis._sadd(key, *values))
            elif name == "srem":
                _, key, values = op
                results.append(self.redis._srem(key, *values))
            elif name == "lpush":
                _, key, values = op
                results.append(self.redis._lpush(key, *values))
            elif name == "lrem":
                _, key, count, value = op
                results.append(self.redis._lrem(key, count, value))
            elif name == "hgetall":
                _, key = op
                results.append(self.redis._hgetall(key))
            elif name == "delete":
                _, key = op
                results.append(self.redis._delete(key))
        self.ops = []
        return results


class FakeRedis:
    def __init__(self):
        self.hashes = {}
        self.sets = {}
        self.lists = {}

    def _hset(self, key, mapping, field, value):
        if mapping is None:
            mapping = {field: value}
        target = self.hashes.setdefault(key, {})
        for k, v in mapping.items():
            target[k] = v
        return len(mapping)

    async def hset(self, key, mapping=None, field=None, value=None):
        return self._hset(key, mapping, field, value)

    def _hsetnx(self, key, field, value):
        target = self.hashes.setdefault(key, {})
        if field in target:
            return 0
        target[field] = value
        return 1

    async def hsetnx(self, key, field, value):
        return self._hsetnx(key, field, value)

    async def hgetall(self, key):
        return self._hgetall(key)

    def _hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    async def hget(self, key, field):
        return self.hashes.get(key, {}).get(field)

    def _sadd(self, key, *values):
        target = self.sets.setdefault(key, set())
        for value in values:
            target.add(value)
        return len(values)

    async def sadd(self, key, *values):
        return self._sadd(key, *values)

    def _srem(self, key, *values):
        target = self.sets.setdefault(key, set())
        removed = 0
        for value in values:
            if value in target:
                target.remove(value)
                removed += 1
        return removed

    async def srem(self, key, *values):
        return self._srem(key, *values)

    async def smembers(self, key):
        return set(self.sets.get(key, set()))

    def _lpush(self, key, *values):
        target = self.lists.setdefault(key, [])
        for value in values:
            target.insert(0, value)
        return len(target)

    async def lpush(self, key, *values):
        return self._lpush(key, *values)

    def _lrem(self, key, count, value):
        target = self.lists.setdefault(key, [])
        removed = 0
        if count == 0:
            original = list(target)
            target.clear()
            for item in original:
                if item != value:
                    target.append(item)
                else:
                    removed += 1
        return removed

    async def lrem(self, key, count, value):
        return self._lrem(key, count, value)

    async def brpop(self, key, timeout=0):
        target = self.lists.get(key, [])
        if not target:
            return None
        value = target.pop()
        return (key, value)

    def _delete(self, key):
        removed = 0
        if key in self.hashes:
            del self.hashes[key]
            removed += 1
        if key in self.sets:
            del self.sets[key]
            removed += 1
        if key in self.lists:
            del self.lists[key]
            removed += 1
        return removed

    async def delete(self, key):
        return self._delete(key)

    async def exists(self, key):
        return 1 if (key in self.hashes or key in self.sets or key in self.lists) else 0

    def pipeline(self, *args, **kwargs):
        return FakePipeline(self)


def _use_fake_redis(monkeypatch, fake_redis):
    async def fake_get_redis():
        return fake_redis

    monkeypatch.setattr(task_repository, "get_redis", fake_get_redis)


def test_task_serialization_roundtrip():
    repo = TaskRepository()
    task = Task(experiment_id="exp1", task_type=TaskType.RUN_EXPERIMENT, parameters={"k": "v"})
    data = repo.task_to_hash(task)
    restored = repo.hash_to_task(data)
    assert restored is not None
    assert restored.id == task.id
    assert restored.experiment_id == task.experiment_id
    assert restored.parameters == task.parameters
    assert restored.status == task.status


def test_create_task_adds_to_all_tasks_and_queue(monkeypatch):
    repo = TaskRepository()
    fake = FakeRedis()
    _use_fake_redis(monkeypatch, fake)

    task = asyncio.run(repo.create_task(TaskCreate(experiment_id="exp1", task_type=TaskType.RUN_EXPERIMENT)))

    assert str(task.id) in fake.sets.get("all_tasks", set())
    assert fake.lists.get("task_queue") == [str(task.id)]
    assert fake.lists.get(f"experiment:{task.experiment_id}:tasks") == [str(task.id)]


def test_add_enqueue_ops_writes_task_and_indexes():
    repo = TaskRepository()
    fake = FakeRedis()
    task = Task(experiment_id="exp1", task_type=TaskType.RUN_EXPERIMENT)

    pipe = fake.pipeline()
    repo.add_enqueue_ops(pipe, task)
    asyncio.run(pipe.execute())

    data = fake.hashes.get(f"task:{task.id}")
    assert data is not None
    assert data.get("id") == str(task.id)
    assert str(task.id) in fake.sets.get("all_tasks", set())
    assert fake.lists.get("task_queue") == [str(task.id)]
    assert fake.lists.get(f"experiment:{task.experiment_id}:tasks") == [str(task.id)]


def test_list_tasks_filters_and_paginates(monkeypatch):
    repo = TaskRepository()
    fake = FakeRedis()
    _use_fake_redis(monkeypatch, fake)

    now = datetime.now(timezone.utc)
    task_a = Task(experiment_id="exp1", task_type=TaskType.RUN_EXPERIMENT, created_at=now - timedelta(seconds=5))
    task_b = Task(
        experiment_id="exp2",
        task_type=TaskType.GENERATE_CODE,
        status=TaskStatus.COMPLETED,
        created_at=now,
    )

    fake.hashes[f"task:{task_a.id}"] = repo.task_to_hash(task_a)
    fake.hashes[f"task:{task_b.id}"] = repo.task_to_hash(task_b)
    fake.sets["all_tasks"] = {str(task_a.id), str(task_b.id)}

    tasks_all = asyncio.run(repo.list_tasks(limit=10, offset=0))
    assert [t.id for t in tasks_all] == [task_a.id, task_b.id]

    tasks_filtered = asyncio.run(repo.list_tasks(status=TaskStatus.COMPLETED, limit=10, offset=0))
    assert [t.id for t in tasks_filtered] == [task_b.id]

    tasks_page = asyncio.run(repo.list_tasks(limit=1, offset=1))
    assert [t.id for t in tasks_page] == [task_b.id]


def test_claim_next_task_returns_queued_task(monkeypatch):
    repo = TaskRepository()
    fake = FakeRedis()
    _use_fake_redis(monkeypatch, fake)

    created = asyncio.run(repo.create_task(TaskCreate(experiment_id="exp1", task_type=TaskType.RUN_EXPERIMENT)))
    claimed = asyncio.run(repo.claim_next_task(timeout=0))

    assert claimed is not None
    assert claimed.id == created.id
    assert fake.lists.get("task_queue") == []


def test_claim_next_task_returns_none_for_cancelled_task(monkeypatch):
    repo = TaskRepository()
    fake = FakeRedis()
    _use_fake_redis(monkeypatch, fake)

    task = asyncio.run(repo.create_task(TaskCreate(experiment_id="exp1", task_type=TaskType.RUN_EXPERIMENT)))
    fake.hashes[f"task:{task.id}"]["status"] = TaskStatus.CANCELLED.value

    claimed = asyncio.run(repo.claim_next_task(timeout=0))

    assert claimed is None
    assert fake.lists.get("task_queue") == []


def test_cancel_task_sets_cancelled_and_removes_from_queue(monkeypatch):
    repo = TaskRepository()
    fake = FakeRedis()
    _use_fake_redis(monkeypatch, fake)

    task = asyncio.run(repo.create_task(TaskCreate(experiment_id="exp1", task_type=TaskType.RUN_EXPERIMENT)))
    ok = asyncio.run(repo.cancel_task(task.id))
    assert ok is True

    data = fake.hashes.get(f"task:{task.id}")
    assert data is not None
    assert data.get("status") == TaskStatus.CANCELLED.value
    assert data.get("completed_at")
    assert str(task.id) not in fake.lists.get("task_queue", [])


def test_cancel_task_returns_false_for_terminal_status(monkeypatch):
    repo = TaskRepository()
    fake = FakeRedis()
    _use_fake_redis(monkeypatch, fake)

    task = asyncio.run(repo.create_task(TaskCreate(experiment_id="exp1", task_type=TaskType.RUN_EXPERIMENT)))
    fake.hashes[f"task:{task.id}"]["status"] = TaskStatus.COMPLETED.value

    ok = asyncio.run(repo.cancel_task(task.id))

    assert ok is False
    assert fake.hashes[f"task:{task.id}"]["status"] == TaskStatus.COMPLETED.value


def test_delete_task_removes_non_running_task_from_indexes(monkeypatch):
    repo = TaskRepository()
    fake = FakeRedis()
    _use_fake_redis(monkeypatch, fake)

    task = asyncio.run(repo.create_task(TaskCreate(experiment_id="exp1", task_type=TaskType.RUN_EXPERIMENT)))
    ok = asyncio.run(repo.delete_task(task.id))

    assert ok is True
    assert f"task:{task.id}" not in fake.hashes
    assert str(task.id) not in fake.sets.get("all_tasks", set())
    assert str(task.id) not in fake.lists.get("task_queue", [])
    assert str(task.id) not in fake.lists.get(f"experiment:{task.experiment_id}:tasks", [])


def test_delete_running_task_marks_for_cleanup(monkeypatch):
    repo = TaskRepository()
    fake = FakeRedis()
    _use_fake_redis(monkeypatch, fake)

    task = asyncio.run(repo.create_task(TaskCreate(experiment_id="exp1", task_type=TaskType.RUN_EXPERIMENT)))
    fake.hashes[f"task:{task.id}"]["status"] = TaskStatus.RUNNING.value

    ok = asyncio.run(repo.delete_task(task.id))

    assert ok is True
    data = fake.hashes.get(f"task:{task.id}")
    assert data is not None
    assert data.get("status") == TaskStatus.CANCELLED.value
    assert data.get("delete_after_cancel") == "1"
    assert data.get("completed_at")
