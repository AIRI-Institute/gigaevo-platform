#!/usr/bin/env python3

import uuid
from datetime import datetime, timedelta
from typing import List, Optional
from uuid import uuid4

from loguru import logger
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ..config import load_config
from ..models.database import Base, ExperimentModel, FileUploadModel, RunnerInstanceModel, TaskModel
from ..models.experiment import ExperimentCreate, ExperimentStatus
from ..models.instance import RunnerInstanceStatus


class DatabaseService:
    """Service for managing database operations"""

    def __init__(self, config=None):
        self.config = config or load_config()
        self.engine = None
        self.session_factory = None

    async def _ensure_columns_exist(self, conn):
        """Ensure all required columns exist in tables (for schema migrations)"""
        try:
            # Check and add missing columns for experiments table
            experiments_columns = {
                "status_message": "TEXT",
            }

            for column_name, column_type in experiments_columns.items():
                # Check if column exists
                check_query = text(
                    """
                    SELECT column_name 
                    FROM information_schema.columns 
                    WHERE table_name = 'experiments' AND column_name = :column_name
                    """
                )
                result = await conn.execute(check_query, {"column_name": column_name})
                row = result.fetchone()
                if not row:
                    # Column doesn't exist, add it
                    alter_query = text(f"ALTER TABLE experiments ADD COLUMN {column_name} {column_type}")
                    await conn.execute(alter_query)
                    logger.info(f"Added missing column '{column_name}' to experiments table")
        except Exception as e:
            logger.warning(f"Failed to ensure columns exist (non-critical): {e}")

    async def initialize(self):
        """Initialize database connection and create tables"""
        try:
            # Create async engine with appropriate parameters based on database type
            engine_kwargs = {"echo": False}  # Disable SQL statement logging

            # Only add pooling parameters for non-SQLite databases
            if not self.config.database.url.startswith("sqlite"):
                engine_kwargs.update(
                    {
                        "pool_size": self.config.database.pool_size,
                        "max_overflow": self.config.database.max_overflow,
                    }
                )

            self.engine = create_async_engine(self.config.database.url, **engine_kwargs)

            # Create session factory
            self.session_factory = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)

            # Create tables
            async with self.engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)

                # Ensure missing columns are added (for existing databases)
                if not self.config.database.url.startswith("sqlite"):
                    await self._ensure_columns_exist(conn)

            logger.info("Database initialized successfully")

        except Exception as e:
            logger.error(f"Failed to initialize database: {e}")
            raise

    async def cleanup(self):
        """Cleanup database connections"""
        if self.engine:
            await self.engine.dispose()
            logger.info("Database connections closed")

    async def get_session(self) -> AsyncSession:
        """Get database session"""
        if not self.session_factory:
            raise RuntimeError("Database not initialized")
        return self.session_factory()

    # Experiment operations
    async def create_experiment(
        self, experiment_create: ExperimentCreate, experiment_id: Optional[str] = None
    ) -> ExperimentModel:
        """Create a new experiment in database"""
        async with await self.get_session() as session:
            if experiment_id is None:
                experiment_id = f"exp_{uuid4()}"

            experiment = ExperimentModel(
                id=experiment_id,
                name=experiment_create.name,
                status=ExperimentStatus.PENDING.value,
                config=experiment_create.config.model_dump(),
                data_path=experiment_create.data_path,
            )

            session.add(experiment)
            await session.commit()
            await session.refresh(experiment)

            logger.info(f"Created experiment in database: {experiment.id}")
            return experiment

    async def get_experiment(self, experiment_id: str) -> Optional[ExperimentModel]:
        """Get experiment by ID"""
        async with await self.get_session() as session:
            result = await session.execute(select(ExperimentModel).where(ExperimentModel.id == experiment_id))
            return result.scalar_one_or_none()

    async def list_experiments(
        self, status: Optional[str] = None, limit: int = 100, offset: int = 0
    ) -> List[ExperimentModel]:
        """List experiments with optional filtering"""
        async with await self.get_session() as session:
            query = select(ExperimentModel)

            if status:
                query = query.where(ExperimentModel.status == status)

            query = query.order_by(ExperimentModel.created_at.desc()).offset(offset).limit(limit)

            result = await session.execute(query)
            return result.scalars().all()

    async def update_experiment(self, experiment_id: str, **kwargs) -> bool:
        """Update experiment fields"""
        async with await self.get_session() as session:
            try:
                stmt = update(ExperimentModel).where(ExperimentModel.id == experiment_id).values(**kwargs)
                result = await session.execute(stmt)
                await session.commit()
                return result.rowcount > 0
            except Exception as e:
                logger.error(f"Failed to update experiment {experiment_id}: {e}")
                await session.rollback()
                return False

    async def update_experiment_status(self, experiment_id: str, status: str, **kwargs) -> bool:
        """Update experiment status"""
        update_data = {"status": status, **kwargs}
        return await self.update_experiment(experiment_id, **update_data)

    async def assign_runner(self, experiment_id: str, runner_id: str) -> bool:
        """Assign experiment to runner"""
        # Store runner ID in config instead of a separate column
        experiment = await self.get_experiment(experiment_id)
        if experiment:
            updated_config = experiment.config.copy()
            updated_config["assigned_runner_id"] = runner_id
            return await self.update_experiment(experiment_id, config=updated_config)
        return False

    # Runner operations
    async def create_or_update_runner(self, runner_id: str, endpoint_url: str, **kwargs) -> RunnerInstanceModel:
        """Create or update runner instance"""
        async with await self.get_session() as session:
            # Try to find existing runner
            result = await session.execute(select(RunnerInstanceModel).where(RunnerInstanceModel.id == runner_id))
            runner = result.scalar_one_or_none()

            if runner:
                # Update existing runner
                for key, value in kwargs.items():
                    if hasattr(runner, key):
                        setattr(runner, key, value)
                runner.endpoint_url = endpoint_url
            else:
                # Create new runner
                runner = RunnerInstanceModel(id=runner_id, endpoint_url=endpoint_url, **kwargs)
                session.add(runner)

            await session.commit()
            await session.refresh(runner)

            logger.info(f"{'Updated' if runner.id else 'Created'} runner: {runner_id}")
            return runner

    async def get_runner(self, runner_id: str) -> Optional[RunnerInstanceModel]:
        """Get runner by ID"""
        async with await self.get_session() as session:
            result = await session.execute(select(RunnerInstanceModel).where(RunnerInstanceModel.id == runner_id))
            return result.scalar_one_or_none()

    async def list_runners(self, status: Optional[str] = None) -> List[RunnerInstanceModel]:
        """List runner instances"""
        async with await self.get_session() as session:
            query = select(RunnerInstanceModel)

            if status:
                query = query.where(RunnerInstanceModel.status == status)

            result = await session.execute(query)
            return result.scalars().all()

    # Experiment statuses that mean "still occupying a runner slot".
    _RUNNER_ACTIVE_STATUSES = (
        ExperimentStatus.DISPATCHING.value,
        ExperimentStatus.INITIALIZING.value,
        ExperimentStatus.RUNNING.value,
    )

    def _runner_concurrency_limit(self) -> int:
        """How many experiments one runner may execute concurrently.

        ``1`` (default) keeps the historical binary-capacity behaviour;
        ``>1`` enables per-runner concurrency."""
        try:
            limit = int(getattr(self.config.runner, "max_concurrent_experiments_per_runner", 1) or 1)
        except (TypeError, ValueError):
            limit = 1
        return max(1, limit)

    async def _count_active_experiments_for_runner(
        self, session: AsyncSession, runner_id: str, *, exclude_experiment_id: Optional[str] = None
    ) -> int:
        """Count experiments currently assigned to ``runner_id`` and still
        active. Migration-free: reads ``config.assigned_runner_id`` in
        Python so it's portable across Postgres / SQLite (pools are small).
        """
        q = select(ExperimentModel).where(
            ExperimentModel.status.in_(self._RUNNER_ACTIVE_STATUSES)
        )
        rows = (await session.execute(q)).scalars().all()
        count = 0
        for exp in rows:
            if exclude_experiment_id is not None and str(exp.id) == str(exclude_experiment_id):
                continue
            if (exp.config or {}).get("assigned_runner_id") == runner_id:
                count += 1
        return count

    async def count_active_experiments_for_runner(self, runner_id: str) -> int:
        """Public: active experiment count assigned to ``runner_id`` (for the
        reconciler in concurrency mode)."""
        async with await self.get_session() as session:
            try:
                return await self._count_active_experiments_for_runner(session, runner_id)
            except Exception:
                return 0

    async def has_ready_runner_capacity(self) -> bool:
        """
        Return True if there is spare runner capacity to dispatch another
        experiment.

        Used by the queue scheduler to avoid flipping experiments into DISPATCHING
        when we already know there is no capacity (better UX).
        """
        limit = self._runner_concurrency_limit()
        async with await self.get_session() as session:
            try:
                if limit <= 1:
                    q = (
                        select(RunnerInstanceModel.id)
                        .where(
                            RunnerInstanceModel.status == RunnerInstanceStatus.READY.value,
                            RunnerInstanceModel.current_experiment_id.is_(None),
                        )
                        .limit(1)
                    )
                    res = await session.execute(q)
                    return res.scalar_one_or_none() is not None

                # Concurrency mode: any non-disabled runner whose active
                # experiment count is below the per-runner limit has room.
                runners = (
                    await session.execute(
                        select(RunnerInstanceModel).where(
                            RunnerInstanceModel.status.in_(
                                (
                                    RunnerInstanceStatus.READY.value,
                                    RunnerInstanceStatus.BUSY.value,
                                )
                            )
                        )
                    )
                ).scalars().all()
                for runner in runners:
                    active = await self._count_active_experiments_for_runner(session, runner.id)
                    if active < limit:
                        return True
                return False
            except Exception:
                return False

    async def allocate_runner_for_experiment(self, experiment_id: str) -> Optional[RunnerInstanceModel]:
        """
        Atomically allocate a READY runner for an experiment (binary capacity).

        - Select one READY runner (no current experiment)
        - Mark it BUSY and set current_experiment_id
        - Persist assigned_runner_id in experiment.config
        """
        limit = self._runner_concurrency_limit()
        async with await self.get_session() as session:
            try:
                async with session.begin():
                    if limit <= 1:
                        # Binary capacity: one READY runner with no current experiment.
                        q = (
                            select(RunnerInstanceModel)
                            .where(
                                RunnerInstanceModel.status == RunnerInstanceStatus.READY.value,
                                RunnerInstanceModel.current_experiment_id.is_(None),
                            )
                            .with_for_update(skip_locked=True)
                            .limit(1)
                        )
                        res = await session.execute(q)
                        runner = res.scalar_one_or_none()
                    else:
                        # Concurrency: pick a READY/BUSY runner with spare slots,
                        # preferring the least-loaded so work spreads evenly.
                        runner = await self._pick_runner_with_capacity(session, limit)
                    if not runner:
                        return None

                    runner.status = RunnerInstanceStatus.BUSY.value
                    # current_experiment_id tracks the most-recent assignment
                    # (informational once concurrency is on; release recomputes).
                    runner.current_experiment_id = experiment_id
                    runner.last_heartbeat = datetime.utcnow()

                    exp = await session.get(ExperimentModel, experiment_id)
                    if exp:
                        cfg = dict(exp.config or {})
                        cfg["assigned_runner_id"] = runner.id
                        exp.config = cfg
                        exp.updated_at = datetime.utcnow()

                # Refresh outside transaction to return up-to-date row
                await session.refresh(runner)
                return runner
            except Exception as e:
                logger.error(f"Failed to allocate runner for experiment {experiment_id}: {e}")
                await session.rollback()
                return None

    async def _pick_runner_with_capacity(
        self, session: AsyncSession, limit: int
    ) -> Optional[RunnerInstanceModel]:
        """Lock + return the least-loaded runner with an open slot, or None."""
        runners = (
            await session.execute(
                select(RunnerInstanceModel)
                .where(
                    RunnerInstanceModel.status.in_(
                        (
                            RunnerInstanceStatus.READY.value,
                            RunnerInstanceStatus.BUSY.value,
                        )
                    )
                )
                .with_for_update(skip_locked=True)
            )
        ).scalars().all()
        best: Optional[RunnerInstanceModel] = None
        best_active = limit
        for runner in runners:
            active = await self._count_active_experiments_for_runner(session, runner.id)
            if active < limit and active < best_active:
                best, best_active = runner, active
        return best

    async def allocate_specific_runner_for_experiment(
        self, runner_id: str, experiment_id: str
    ) -> Optional[RunnerInstanceModel]:
        """Allocate a specific runner if it is READY; otherwise return None."""
        async with await self.get_session() as session:
            try:
                limit = self._runner_concurrency_limit()
                async with session.begin():
                    runner = await session.get(RunnerInstanceModel, runner_id, with_for_update=True)
                    if not runner:
                        return None
                    if limit <= 1:
                        if runner.status != RunnerInstanceStatus.READY.value or runner.current_experiment_id is not None:
                            return None
                    else:
                        # Concurrency: accept while the runner has an open slot
                        # (and isn't disabled/offline).
                        if runner.status not in (
                            RunnerInstanceStatus.READY.value,
                            RunnerInstanceStatus.BUSY.value,
                        ):
                            return None
                        active = await self._count_active_experiments_for_runner(session, runner.id)
                        if active >= limit:
                            return None
                    runner.status = RunnerInstanceStatus.BUSY.value
                    runner.current_experiment_id = experiment_id
                    runner.last_heartbeat = datetime.utcnow()

                    exp = await session.get(ExperimentModel, experiment_id)
                    if exp:
                        cfg = dict(exp.config or {})
                        cfg["assigned_runner_id"] = runner.id
                        exp.config = cfg
                        exp.updated_at = datetime.utcnow()

                await session.refresh(runner)
                return runner
            except Exception as e:
                logger.error(f"Failed to allocate runner {runner_id} for experiment {experiment_id}: {e}")
                await session.rollback()
                return None

    async def _recompute_runner_release(
        self, session: AsyncSession, runner: RunnerInstanceModel, finished_experiment_id: str
    ) -> None:
        """Concurrency-mode release: free the slot held by
        ``finished_experiment_id`` and keep the runner BUSY only while it
        still has other active experiments."""
        remaining = await self._count_active_experiments_for_runner(
            session, runner.id, exclude_experiment_id=finished_experiment_id
        )
        if remaining <= 0:
            runner.status = RunnerInstanceStatus.READY.value
            runner.current_experiment_id = None
        else:
            runner.status = RunnerInstanceStatus.BUSY.value
            # current_experiment_id is informational in concurrency mode;
            # point it at a still-active experiment if it referenced the
            # finished one.
            if runner.current_experiment_id in (None, finished_experiment_id):
                runner.current_experiment_id = await self._first_active_experiment_for_runner(
                    session, runner.id, exclude_experiment_id=finished_experiment_id
                )
        runner.last_heartbeat = datetime.utcnow()

    async def _first_active_experiment_for_runner(
        self, session: AsyncSession, runner_id: str, *, exclude_experiment_id: Optional[str] = None
    ) -> Optional[str]:
        rows = (
            await session.execute(
                select(ExperimentModel).where(
                    ExperimentModel.status.in_(self._RUNNER_ACTIVE_STATUSES)
                )
            )
        ).scalars().all()
        for exp in rows:
            if exclude_experiment_id is not None and str(exp.id) == str(exclude_experiment_id):
                continue
            if (exp.config or {}).get("assigned_runner_id") == runner_id:
                return str(exp.id)
        return None

    async def release_runner_if_assigned(self, experiment_id: str) -> bool:
        """
        Release whichever runner is currently assigned to this experiment_id.
        Safe/idempotent. In concurrency mode the runner stays BUSY while it
        still has other active experiments.
        """
        limit = self._runner_concurrency_limit()
        async with await self.get_session() as session:
            try:
                async with session.begin():
                    if limit <= 1:
                        q = (
                            select(RunnerInstanceModel)
                            .where(RunnerInstanceModel.current_experiment_id == experiment_id)
                            .with_for_update()
                        )
                        runner = (await session.execute(q)).scalar_one_or_none()
                        if not runner:
                            return False
                        runner.status = RunnerInstanceStatus.READY.value
                        runner.current_experiment_id = None
                        runner.last_heartbeat = datetime.utcnow()
                        return True

                    # Concurrency: find the runner via current_experiment_id
                    # or the experiment's recorded assigned_runner_id.
                    runner = (
                        await session.execute(
                            select(RunnerInstanceModel)
                            .where(RunnerInstanceModel.current_experiment_id == experiment_id)
                            .with_for_update()
                        )
                    ).scalar_one_or_none()
                    if runner is None:
                        exp = await session.get(ExperimentModel, experiment_id)
                        assigned = (exp.config or {}).get("assigned_runner_id") if exp else None
                        if assigned:
                            runner = await session.get(
                                RunnerInstanceModel, assigned, with_for_update=True
                            )
                    if runner is None:
                        return False
                    await self._recompute_runner_release(session, runner, experiment_id)
                    return True
            except Exception as e:
                logger.error(f"Failed to release runner for experiment {experiment_id}: {e}")
                await session.rollback()
                return False

    async def release_runner_by_id_if_experiment(self, runner_id: str, experiment_id: str) -> bool:
        """Release a specific runner's slot for the given experiment."""
        limit = self._runner_concurrency_limit()
        async with await self.get_session() as session:
            try:
                async with session.begin():
                    runner = await session.get(RunnerInstanceModel, runner_id, with_for_update=True)
                    if not runner:
                        return False
                    if limit <= 1:
                        if runner.current_experiment_id != experiment_id:
                            return False
                        runner.status = RunnerInstanceStatus.READY.value
                        runner.current_experiment_id = None
                        runner.last_heartbeat = datetime.utcnow()
                        return True
                    await self._recompute_runner_release(session, runner, experiment_id)
                    return True
            except Exception as e:
                logger.error(f"Failed to release runner {runner_id} for experiment {experiment_id}: {e}")
                await session.rollback()
                return False

    async def set_runner_ready(self, runner_id: str) -> bool:
        """Force runner to READY (used for reconciliation of inconsistent states)."""
        async with await self.get_session() as session:
            try:
                async with session.begin():
                    runner = await session.get(RunnerInstanceModel, runner_id, with_for_update=True)
                    if not runner:
                        return False
                    runner.status = RunnerInstanceStatus.READY.value
                    runner.current_experiment_id = None
                    runner.last_heartbeat = datetime.utcnow()
                return True
            except Exception as e:
                logger.error(f"Failed to set runner {runner_id} READY: {e}")
                await session.rollback()
                return False

    async def claim_next_queued_for_dispatching(self) -> Optional[ExperimentModel]:
        """Atomically claim the oldest queued experiment by moving it to DISPATCHING."""
        async with await self.get_session() as session:
            try:
                async with session.begin():
                    q = (
                        select(ExperimentModel)
                        .where(ExperimentModel.status == ExperimentStatus.QUEUED.value)
                        .order_by(ExperimentModel.created_at.asc())
                        .with_for_update(skip_locked=True)
                        .limit(1)
                    )
                    res = await session.execute(q)
                    exp = res.scalar_one_or_none()
                    if not exp:
                        return None
                    exp.status = ExperimentStatus.DISPATCHING.value
                    exp.status_message = "Dispatching to runner..."
                    exp.updated_at = datetime.utcnow()
                await session.refresh(exp)
                return exp
            except Exception as e:
                logger.error(f"Failed to claim queued experiment: {e}")
                await session.rollback()
                return None

    async def recover_stale_dispatching(self, ttl_seconds: int = 60) -> int:
        """Revert stale dispatching experiments back to queued (best-effort)."""
        ttl_seconds = max(10, int(ttl_seconds or 60))
        threshold = datetime.utcnow() - timedelta(seconds=ttl_seconds)
        async with await self.get_session() as session:
            try:
                async with session.begin():
                    stmt = (
                        update(ExperimentModel)
                        .where(
                            ExperimentModel.status == ExperimentStatus.DISPATCHING.value,
                            ExperimentModel.updated_at < threshold,
                        )
                        .values(
                            status=ExperimentStatus.QUEUED.value,
                            status_message="Retrying dispatch (previous attempt timed out)",
                        )
                    )
                    res = await session.execute(stmt)
                return int(res.rowcount or 0)
            except Exception as e:
                logger.debug(f"Stale dispatching recovery skipped/failed: {e}")
                await session.rollback()
                return 0

    # Task operations
    async def create_task(self, experiment_id: str, task_type: str) -> TaskModel:
        """Create a new task"""
        async with await self.get_session() as session:
            task = TaskModel(experiment_id=experiment_id, task_type=task_type)

            session.add(task)
            await session.commit()
            await session.refresh(task)

            logger.info(f"Created task: {task.id} for experiment: {experiment_id}")
            return task

    async def update_task(self, task_id: str, **kwargs) -> bool:
        """Update task fields"""
        async with await self.get_session() as session:
            try:
                task_uuid = uuid.UUID(task_id)
                stmt = update(TaskModel).where(TaskModel.id == task_uuid).values(**kwargs)
                result = await session.execute(stmt)
                await session.commit()
                return result.rowcount > 0
            except (ValueError, AttributeError):
                # Invalid UUID format
                logger.error(f"Invalid UUID format for task {task_id}")
                return False
            except Exception as e:
                logger.error(f"Failed to update task {task_id}: {e}")
                await session.rollback()
                return False

    async def get_tasks_for_experiment(self, experiment_id: str, task_type: Optional[str] = None) -> List[TaskModel]:
        """Get tasks for experiment"""
        async with await self.get_session() as session:
            query = select(TaskModel).where(TaskModel.experiment_id == experiment_id)

            if task_type:
                query = query.where(TaskModel.task_type == task_type)

            result = await session.execute(query)
            return result.scalars().all()

    # File upload operations
    async def create_file_upload(self, filename: str, storage_path: str, **kwargs) -> FileUploadModel:
        """Create file upload record"""
        async with await self.get_session() as session:
            # Handle reserved word 'metadata' -> 'upload_metadata'
            if "metadata" in kwargs:
                kwargs["upload_metadata"] = kwargs.pop("metadata")

            upload = FileUploadModel(original_filename=filename, storage_path=storage_path, **kwargs)

            session.add(upload)
            await session.commit()
            await session.refresh(upload)

            logger.info(f"Created file upload record: {filename} -> {storage_path}")
            return upload

    async def get_file_upload(self, upload_id: str) -> Optional[FileUploadModel]:
        """Get file upload by ID"""
        async with await self.get_session() as session:
            try:
                upload_uuid = uuid.UUID(upload_id)
                result = await session.execute(select(FileUploadModel).where(FileUploadModel.id == upload_uuid))
                return result.scalar_one_or_none()
            except (ValueError, AttributeError):
                # Invalid UUID format
                return None

    async def delete_all_experiments(self) -> int:
        """Delete all experiments and their related data"""
        async with await self.get_session() as session:
            try:
                # Get all experiment IDs for cleanup
                result = await session.execute(select(ExperimentModel.id))
                experiment_ids = [row[0] for row in result.fetchall()]

                deleted_count = 0

                # Delete related tasks first
                for exp_id in experiment_ids:
                    # Delete tasks for this experiment
                    stmt = delete(TaskModel).where(TaskModel.experiment_id == exp_id)
                    task_result = await session.execute(stmt)
                    deleted_count += task_result.rowcount

                # Delete all experiments
                stmt = delete(ExperimentModel)
                exp_result = await session.execute(stmt)
                deleted_count += exp_result.rowcount

                await session.commit()
                logger.info(f"Deleted {exp_result.rowcount} experiments and related tasks")
                return exp_result.rowcount

            except Exception as e:
                logger.error(f"Failed to delete all experiments: {e}")
                await session.rollback()
                raise

    # Health check
    async def health_check(self) -> bool:
        """Check database connection health"""
        try:
            async with await self.get_session() as session:
                await session.execute(select(1))
                return True
        except Exception as e:
            logger.error(f"Database health check failed: {e}")
            return False
