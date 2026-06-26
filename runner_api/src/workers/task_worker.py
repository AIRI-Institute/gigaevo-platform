#!/usr/bin/env python3

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from redis.asyncio import Redis

from ..config import load_config
from ..models.task import Task, TaskStatus, TaskType
from ..models.worker import WorkerStatus
from ..sandbox import SandboxError
from ..services.experiment_service import ExperimentService
from ..services.gigavolve_service import GigaEvolveService
from ..services.redis_client import get_redis
from ..services.skill_executor import SkillExecutionRequest, SkillExecutor
from ..services.task_repository import TaskRepository

logger = logging.getLogger(__name__)


class TaskWorker:
    def __init__(
        self,
        worker_id: str,
        name: str,
        gigavolve_service: GigaEvolveService,
        task_repository: TaskRepository,
        experiment_service: Optional[ExperimentService] = None,
        skill_executor: Optional[SkillExecutor] = None,
    ):
        self.worker_id = worker_id
        self.name = name
        self.status = WorkerStatus.IDLE
        self.current_task: Optional[Task] = None
        self.gigavolve_service = gigavolve_service
        self.task_repository = task_repository
        self.experiment_service = experiment_service
        self.config = load_config()
        # Skill executor — created lazily from config when not injected so
        # existing call sites (and tests) don't have to know about it.
        self.skill_executor: SkillExecutor = skill_executor or SkillExecutor(self.config.sandbox)
        self._running = False

    async def start(self):
        """Start the worker"""
        self._running = True
        logger.info(f"Worker {self.worker_id} started")
        # Register worker presence and initial heartbeat
        await self._update_worker_status(WorkerStatus.IDLE, current_task_id=None)

        while self._running:
            try:
                # Stop flag check
                if not await self._should_continue_running():
                    break
                # Heartbeat
                await self._heartbeat()
                # Get next task from queue
                task = await self._get_next_task()
                if task:
                    await self._execute_task(task)
                else:
                    # No tasks available, wait
                    await asyncio.sleep(self.config.worker.polling_interval)

            except Exception as e:
                logger.error(f"Worker {self.worker_id} error: {e}")
                await asyncio.sleep(5)

    async def stop(self):
        """Stop the worker"""
        self._running = False
        if self.current_task:
            await self.task_repository.cancel_task(self.current_task.id)
        logger.info(f"Worker {self.worker_id} stopped")

    async def _get_next_task(self) -> Optional[Task]:
        """Get next task from Redis queue"""
        # Blocking pop from right (acts as queue FIFO if producers LPUSH)
        timeout = max(1, int(self.config.worker.polling_interval))
        logger.debug(f"Worker {self.worker_id} waiting for task from task_queue (timeout={timeout}s)")
        task = await self.task_repository.claim_next_task(timeout=timeout)
        if not task:
            logger.debug(f"Worker {self.worker_id} no task available after {timeout}s timeout")
            return None
        logger.info(f"Worker {self.worker_id} got task from queue: {task.id}")
        return task

    async def _execute_task(self, task: Task):
        """Execute a task"""
        logger.info(
            f"Worker {self.worker_id} executing task {task.id} (type={task.task_type}, experiment={task.experiment_id})"
        )
        self.current_task = task
        self.status = WorkerStatus.BUSY

        try:
            # Early cancellation/status check before attempting to run
            if await self.task_repository.is_task_cancelled(str(task.id)):
                task.status = TaskStatus.CANCELLED
                task.error_message = task.error_message or "Cancelled before execution"
                return

            # Mark RUNNING in model and Redis
            task.status = TaskStatus.RUNNING
            task.started_at = datetime.now(timezone.utc)
            task.worker_id = self.worker_id
            await self.task_repository.persist_task_state(task)
            await self._update_worker_status(WorkerStatus.BUSY, current_task_id=str(task.id))

            # Check once more right after RUNNING update for a late-arriving cancel
            if await self.task_repository.is_task_cancelled(str(task.id)):
                task.status = TaskStatus.CANCELLED
                task.error_message = task.error_message or "Cancelled before execution"
                return

            if task.task_type == TaskType.GENERATE_CODE:
                await self._handle_generate_code(task)
            elif task.task_type == TaskType.RUN_EXPERIMENT:
                await self._handle_run_experiment(task)
            elif task.task_type == TaskType.COLLECT_RESULTS:
                await self._handle_collect_results(task)
            elif task.task_type == TaskType.RUN_AGENT_SKILL:
                await self._handle_run_agent_skill(task)
            else:
                raise ValueError(f"Unknown task type: {task.task_type}")

        except Exception as e:
            task.status = TaskStatus.FAILED
            task.error_message = str(e)
            logger.error(f"Task {task.id} failed: {e}")

        finally:
            # Persist final state
            was_cancelled = await self.task_repository.is_task_cancelled(str(task.id))
            if was_cancelled and task.status != TaskStatus.CANCELLED:
                task.status = TaskStatus.CANCELLED
                if not task.error_message:
                    task.error_message = "Cancelled"
            if not was_cancelled and task.status == TaskStatus.COMPLETED:
                task.progress = 100.0
            if task.completed_at is None:
                task.completed_at = datetime.now(timezone.utc)
            await self.task_repository.persist_task_state(task)
            self.current_task = None
            self.status = WorkerStatus.IDLE
            # increment tasks completed counter best-effort
            try:
                redis = await get_redis()
                await redis.hincrby(f"worker:{self.worker_id}", "total_tasks_completed", 1)
            except Exception:
                pass
            await self._update_worker_status(WorkerStatus.IDLE, current_task_id=None)
            # If deletion after cancel was requested and task is cancelled, remove task hash and references
            try:
                await self._maybe_delete_after_cancel(str(task.id), str(task.experiment_id))
            except Exception:
                pass
            # For RUN_EXPERIMENT tasks, update experiment-level status in Redis based on final task status
            try:
                if task.task_type == TaskType.RUN_EXPERIMENT and task.experiment_id:
                    await self._update_experiment_status_after_run(str(task.experiment_id), task.status)
            except Exception:
                # Best-effort only; failures here should not break worker loop
                pass

    async def _handle_generate_code(self, task: Task):
        """Handle code generation task"""
        prompt = task.parameters.get("prompt", "")
        generated_code = await self.gigavolve_service.generate_code_from_llm(prompt)

        if generated_code:
            task.status = TaskStatus.COMPLETED
            task.result = {"code": generated_code}
        else:
            task.status = TaskStatus.FAILED
            task.error_message = "Failed to generate code"

    async def _check_iterations_completed(
        self, experiment_id: str, expected_max_iterations: Optional[int]
    ) -> tuple[bool, Optional[int]]:
        """
        Check if experiment completed the expected number of iterations.
        Returns (is_complete, actual_iterations) where is_complete is True if
        actual_iterations >= expected_max_iterations or expected_max_iterations is None.
        """
        if expected_max_iterations is None:
            # No limit set, consider any completion as successful
            return True, None

        try:
            # Try to read evolution_report.json to get actual iterations
            # Check multiple possible locations for the report
            clone_path = Path(self.gigavolve_service.config.clone_path).resolve()

            # Try outputs/{experiment_id}/evolution_report.json first
            output_dir = clone_path / "outputs" / experiment_id
            report_path = output_dir / "evolution_report.json"

            # If not found, try outputs/exp_{experiment_id}/evolution_report.json
            if not report_path.exists() and not experiment_id.startswith("exp_"):
                output_dir = clone_path / "outputs" / f"exp_{experiment_id}"
                report_path = output_dir / "evolution_report.json"

            if not report_path.exists():
                # Report not generated yet, try to generate it first
                logger.info(
                    f"Report not found for {experiment_id} at {report_path}, "
                    "attempting to generate it from Redis data..."
                )
                try:
                    # Try multiple possible output subfolders
                    possible_subfolders = [
                        experiment_id,
                        f"exp_{experiment_id}"
                        if not experiment_id.startswith("exp_")
                        else experiment_id.replace("exp_", ""),
                    ]

                    report_generated = False
                    for subfolder in possible_subfolders:
                        try:
                            out_subfolder = f"outputs/{subfolder}"
                            result = await self.gigavolve_service.generate_evolution_report(
                                experiment_id, out_subfolder
                            )
                            if result.get("success"):
                                # Report generated, try reading it
                                generated_path = result.get("output_json_file")
                                if generated_path and Path(generated_path).exists():
                                    report_path = Path(generated_path)
                                    report_generated = True
                                    logger.info(f"Successfully generated report for {experiment_id} at {report_path}")
                                    break
                        except Exception as e:
                            logger.debug(f"Failed to generate report in {out_subfolder} for {experiment_id}: {e}")
                            continue

                    if not report_generated:
                        # Try the standard location again after generation attempt
                        report_path = output_dir / "evolution_report.json"
                except Exception as e:
                    logger.warning(f"Failed to generate report for {experiment_id}: {e}")

            if not report_path.exists():
                # Report still not available, try CSV as fallback
                csv_path = output_dir / "evolution_report.csv"
                if csv_path.exists():
                    try:
                        import pandas as pd

                        df = pd.read_csv(csv_path)
                        if "metadata_iteration" in df.columns:
                            _iter_series = pd.to_numeric(df["metadata_iteration"], errors="coerce")
                            if not _iter_series.empty and _iter_series.notna().any():
                                actual_iterations = int(_iter_series.max()) + 1
                                is_complete = actual_iterations >= expected_max_iterations
                                logger.info(
                                    f"Found iterations from CSV for {experiment_id}: {actual_iterations}/{expected_max_iterations}"
                                )
                                return is_complete, actual_iterations
                    except Exception as e:
                        logger.warning(f"Failed to read CSV report for {experiment_id}: {e}")

                # Try to generate CSV report from Redis data as a last resort
                logger.info(f"Report and CSV not found for {experiment_id}, attempting to generate CSV from Redis...")
                try:
                    # Try multiple possible output subfolders
                    possible_subfolders = [
                        experiment_id,
                        f"exp_{experiment_id}"
                        if not experiment_id.startswith("exp_")
                        else experiment_id.replace("exp_", ""),
                    ]

                    for subfolder in possible_subfolders:
                        try:
                            out_subfolder = f"outputs/{subfolder}"
                            # Use generate_evolution_report which will create both CSV and JSON
                            result = await self.gigavolve_service.generate_evolution_report(
                                experiment_id, out_subfolder
                            )
                            if result.get("success"):
                                # Check if JSON was generated and can be read
                                generated_json = result.get("output_json_file")
                                if generated_json and Path(generated_json).exists():
                                    report_path = Path(generated_json)
                                    # Try reading the JSON report
                                    try:
                                        report_data = json.loads(report_path.read_text(encoding="utf-8"))
                                        actual_iterations = report_data.get("total_iterations", 0)
                                        is_complete = actual_iterations >= expected_max_iterations
                                        logger.info(
                                            f"Generated and read JSON report for {experiment_id}: {actual_iterations}/{expected_max_iterations}"
                                        )
                                        return is_complete, actual_iterations
                                    except Exception as e:
                                        logger.debug(f"Failed to read generated JSON for {experiment_id}: {e}")

                                # Also try CSV (it's generated as intermediate file)
                                clone_path = Path(self.gigavolve_service.config.clone_path).resolve()
                                csv_path = clone_path / out_subfolder / "evolution_report.csv"
                                if csv_path.exists():
                                    try:
                                        import pandas as pd

                                        df = pd.read_csv(csv_path)
                                        if "metadata_iteration" in df.columns:
                                            _iter_series = pd.to_numeric(df["metadata_iteration"], errors="coerce")
                                            if not _iter_series.empty and _iter_series.notna().any():
                                                actual_iterations = int(_iter_series.max()) + 1
                                                is_complete = actual_iterations >= expected_max_iterations
                                                logger.info(
                                                    f"Generated and read CSV for {experiment_id}: {actual_iterations}/{expected_max_iterations}"
                                                )
                                                return is_complete, actual_iterations
                                    except Exception as e:
                                        logger.debug(f"Failed to read generated CSV for {experiment_id}: {e}")

                                # If JSON was generated, use it
                                if generated_json and Path(generated_json).exists():
                                    report_path = Path(generated_json)
                                    break
                        except Exception as e:
                            logger.debug(f"Failed to generate report in {out_subfolder} for {experiment_id}: {e}")
                            continue
                except Exception as e:
                    logger.warning(f"Failed to generate CSV from Redis for {experiment_id}: {e}")

                # No report available - this could mean:
                # 1. Process was killed before generating report (likely timeout or error)
                # 2. Process completed but report generation failed
                # 3. Redis data is not available or corrupted
                # In this case, we can't verify completion, so assume not complete
                logger.warning(
                    f"Cannot verify iterations for {experiment_id}: report not found and CSV generation failed. "
                    "This may indicate the process was interrupted before completion."
                )
                return False, None

            # Read JSON report
            report_data = json.loads(report_path.read_text(encoding="utf-8"))
            actual_iterations = report_data.get("total_iterations", 0)
            is_complete = actual_iterations >= expected_max_iterations

            logger.info(
                f"Iterations check for {experiment_id}: {actual_iterations}/{expected_max_iterations}, "
                f"complete={is_complete}"
            )
            return is_complete, actual_iterations

        except Exception as e:
            logger.warning(f"Failed to check iterations for {experiment_id}: {e}")
            # If we can't check, assume not complete to be safe
            return False, None

    async def _handle_run_experiment(self, task: Task):
        """Handle experiment execution task"""
        experiment_id = str(task.experiment_id)
        config = task.parameters.get("config", {})
        logger.info(f"Processing RUN_EXPERIMENT task for experiment {experiment_id}")

        async def _cancel_check() -> bool:
            return await self.task_repository.is_task_cancelled(str(task.id))

        logger.info(f"Calling gigavolve_service.run_experiment for {experiment_id}")
        result = await self.gigavolve_service.run_experiment(experiment_id, config, cancel_check=_cancel_check)
        logger.info(f"run_experiment returned for {experiment_id}: {result}")

        # Check if experiment timed out
        if result and result.get("timed_out", False):
            task.status = TaskStatus.TERMINATED
            task.error_message = None
            task.result = result
        elif result and result.get("success", False):
            # Process completed with exit code 0, but verify it reached expected iterations
            max_iterations = config.get("max_iterations")
            is_complete, actual_iterations = await self._check_iterations_completed(experiment_id, max_iterations)

            if is_complete:
                # Successfully completed all expected iterations
                task.status = TaskStatus.COMPLETED
                task.result = result
                if actual_iterations is not None:
                    logger.info(
                        f"Experiment {experiment_id} completed successfully: "
                        f"{actual_iterations}/{max_iterations} iterations"
                    )
            else:
                # Process exited with code 0 but didn't complete expected iterations
                # This could happen if process was killed or stopped prematurely
                task.status = TaskStatus.FAILED
                actual_str = f"{actual_iterations}" if actual_iterations is not None else "unknown"
                expected_str = f"{max_iterations}" if max_iterations else "unlimited"
                task.error_message = (
                    f"Experiment stopped prematurely: completed {actual_str} iterations "
                    f"(expected {expected_str}). Process may have been interrupted."
                )
                task.result = result
                logger.warning(
                    f"Experiment {experiment_id} exited with code 0 but only completed "
                    f"{actual_str}/{expected_str} iterations"
                )
        else:
            # If cancelled mid-run, honor CANCELLED status
            err_msg = result.get("error", "Unknown error") if result else "No result"
            if result and str(err_msg).lower().find("cancel") != -1:
                task.status = TaskStatus.CANCELLED
                task.error_message = err_msg
            else:
                task.status = TaskStatus.FAILED
                task.error_message = err_msg

        # Upload chain feedback to storage even after run ends (loop only processes RUNNING experiments)
        if self.experiment_service:
            try:
                n = await self.experiment_service.collect_and_upload_chain_feedback(experiment_id)
                if n:
                    logger.info(f"Post-run chain feedback upload for {experiment_id}: {n} file(s)")
            except Exception as fb_e:
                logger.warning(f"Post-run chain feedback upload failed for {experiment_id}: {fb_e}")

    async def _handle_collect_results(self, task: Task):
        """Handle results collection task"""
        # The periodic analysis loop in main.py handles plot generation and uploads.
        task.status = TaskStatus.COMPLETED
        task.result = {"info": "Results collection handled by analysis loop"}

    async def _handle_run_agent_skill(self, task: Task):
        """Dispatch a sandboxed SKILL.md execution (CARE §4.5b).

        Maps :class:`SkillExecutor` outcomes onto task status:

        * sandbox config / payload errors → ``FAILED`` with the message;
        * non-zero exit code → ``FAILED`` (full RunResult kept in ``result``);
        * timeout → ``TERMINATED`` (RunResult.timed_out=True);
        * exit 0 within deadline → ``COMPLETED``.
        """
        try:
            req = SkillExecutionRequest.from_task_parameters(task.parameters or {})
        except (ValueError, SandboxError) as exc:
            task.status = TaskStatus.FAILED
            task.error_message = f"Invalid RUN_AGENT_SKILL payload: {exc}"
            return

        try:
            result = await self.skill_executor.execute(req)
        except SandboxError as exc:
            task.status = TaskStatus.FAILED
            task.error_message = f"Sandbox refused to run: {exc}"
            return

        task.result = result
        if result.get("timed_out"):
            task.status = TaskStatus.TERMINATED
            task.error_message = task.error_message or "Skill execution timed out"
        elif result.get("succeeded"):
            task.status = TaskStatus.COMPLETED
        else:
            task.status = TaskStatus.FAILED
            task.error_message = task.error_message or (
                f"Skill exited with code {result.get('exit_code')}"
            )

    async def _should_continue_running(self) -> bool:
        """Check for a stop signal from Redis."""
        redis = await get_redis()
        data = await redis.hgetall(f"worker:{self.worker_id}")
        if data.get("stop_requested") == "1":
            await redis.hset(f"worker:{self.worker_id}", "status", WorkerStatus.OFFLINE.value)
            return False
        return True

    async def _heartbeat(self) -> None:
        """Update last heartbeat timestamp in Redis and ensure registration."""
        redis = await get_redis()
        key = f"worker:{self.worker_id}"
        await redis.hset(key, "last_heartbeat", datetime.now(timezone.utc).isoformat())
        await redis.sadd("workers", self.worker_id)

    async def _update_worker_status(self, status: WorkerStatus, current_task_id: Optional[str]) -> None:
        """Update worker status/name and current task in Redis."""
        self.status = status
        redis = await get_redis()
        key = f"worker:{self.worker_id}"
        mapping = {
            "id": self.worker_id,
            "name": self.name,
            "status": status.value,
            "current_task_id": current_task_id or "",
            "last_heartbeat": datetime.now(timezone.utc).isoformat(),
        }
        pipe = redis.pipeline()
        pipe.hset(key, mapping=mapping)
        pipe.sadd("workers", self.worker_id)
        await pipe.execute()

    async def _maybe_delete_after_cancel(self, task_id: str, experiment_id: str) -> None:
        """Delete task hash and references if flagged and status is CANCELLED."""
        redis = await get_redis()
        key = f"task:{task_id}"
        data = await redis.hgetall(key)
        if not data:
            return
        if data.get("delete_after_cancel") == "1" and data.get("status") == TaskStatus.CANCELLED.value:
            pipe = redis.pipeline()
            pipe.delete(key)
            pipe.srem("all_tasks", task_id)
            if experiment_id:
                pipe.lrem(f"experiment:{experiment_id}:tasks", 0, task_id)
            await pipe.execute()

    async def _update_experiment_status_after_run(self, experiment_id: str, status: TaskStatus) -> None:
        """
        Update experiment:{id}:status hash based on the final RUN_EXPERIMENT task status.
        This keeps runner-side experiment status in sync with task outcome.
        """
        redis = await get_redis()
        status_key = f"experiment:{experiment_id}:status"
        now = datetime.now(timezone.utc).isoformat()
        mapping = {}

        if status == TaskStatus.COMPLETED:
            mapping["status"] = "completed"
            mapping["completed_at"] = now
            mapping["status_message"] = ""
        elif status == TaskStatus.FAILED:
            mapping["status"] = "failed"
            mapping["completed_at"] = now
            mapping["status_message"] = ""
        elif status == TaskStatus.TERMINATED:
            mapping["status"] = "terminated"
            mapping["completed_at"] = now
            mapping["status_message"] = "Time limit reached"
        elif status == TaskStatus.CANCELLED:
            mapping["status"] = "cancelled"
            mapping["stopped_at"] = now
            mapping["status_message"] = ""

        if mapping:
            await redis.hset(status_key, mapping=mapping)
