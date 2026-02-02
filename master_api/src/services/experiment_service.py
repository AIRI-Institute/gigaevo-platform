#!/usr/bin/env python3

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import uuid4

import httpx
from loguru import logger
from src.config import Config

from ..config import load_config
from ..models.database import ExperimentModel
from ..models.experiment import Experiment, ExperimentCreate, ExperimentStatus
from .database_service import DatabaseService
from .kafka_service import KafkaService
from .runner_instance_service import RunnerInstanceService
from .storage_service import StorageService
from .workflow_consumer import WorkflowConsumer


@dataclass(frozen=True)
class StartResult:
    ok: bool
    http_status: int
    payload: Optional[Dict[str, Any]] = None
    detail: Optional[str] = None


class ExperimentService:
    """Enhanced experiment service with database and workflow integration"""

    def __init__(
        self,
        db_service: DatabaseService,
        storage_service: StorageService,
        kafka_service: Optional[KafkaService] = None,
        workflow_consumer: Optional[WorkflowConsumer] = None,
        instance_service: Optional[RunnerInstanceService] = None,
        config: Optional[Config] = None,
    ):
        self.config = config or load_config()
        self.db_service = db_service
        self.kafka_service = kafka_service
        self.storage_service = storage_service
        self.workflow_consumer = workflow_consumer
        self.instance_service = instance_service

        # Track consecutive 404s per (runner_id, experiment_id) to avoid premature releases
        # when RunnerAPI hasn't yet created its status record right after /start.
        self._missing_status_404_counts: Dict[tuple[str, str], int] = {}

    async def initialize(self):
        """Initialize the experiment service"""
        try:
            logger.info("Initializing experiment service with dependency injection")

            if not self.db_service:
                raise RuntimeError("Database service is required")
            if not self.storage_service:
                raise RuntimeError("Storage service is required")

            # Verify Kafka consistency
            if self.config.kafka.enabled:
                if not self.kafka_service:
                    logger.warning("Kafka is enabled but kafka_service not provided")
                if not self.workflow_consumer:
                    logger.warning("Kafka is enabled but workflow_consumer not provided")

            logger.info("Experiment service initialized successfully")

            # Background scheduler for queued experiments (pool orchestration).
            self._queue_scheduler_task: Optional[asyncio.Task] = None
            if bool(getattr(self.config.runner, "queueing_enabled", True)):
                self._queue_scheduler_task = asyncio.create_task(self._queue_scheduler_loop())

        except Exception as e:
            logger.error(f"Failed to initialize experiment service: {e}")
            raise

    async def cleanup(self):
        """Cleanup resources"""
        if getattr(self, "_queue_scheduler_task", None):
            self._queue_scheduler_task.cancel()
            try:
                await self._queue_scheduler_task
            except asyncio.CancelledError:
                pass
        # NOTE: Do not cleanup shared dependencies here; ServiceManager owns their lifecycle.

    async def create_experiment(self, experiment_create: ExperimentCreate) -> Experiment:
        """Create a new experiment"""
        try:
            logger.info(f"Creating new experiment: {experiment_create.name}")

            # Generate experiment ID with "exp_" prefix
            experiment_id = f"exp_{uuid4()}"

            # Create experiment in database
            experiment_model = await self.db_service.create_experiment(experiment_create, experiment_id)

            # Convert to API model
            experiment = self._model_to_api(experiment_model)

            # Publish experiment config to Kafka if enabled
            if self.kafka_service:
                await self.kafka_service.publish_experiment_config(
                    str(experiment.id),
                    experiment.config.model_dump(),
                )
                logger.info(f"Published experiment config for {experiment.id}")
            else:
                logger.info(f"Kafka disabled, skipping config publish for {experiment.id}")

            return experiment

        except Exception as e:
            logger.error(f"Failed to create experiment: {e}")
            raise

    async def list_experiments(
        self, status: Optional[ExperimentStatus] = None, limit: int = 100, offset: int = 0
    ) -> List[Experiment]:
        """List experiments"""
        try:
            status_str = status.value if status else None
            experiment_models = await self.db_service.list_experiments(status_str, limit, offset)

            # Keep statuses fresh for the UI:
            # - Prefer RunnerAPI for RUNNING experiments (Kafka/Redis cache can be stale or absent)
            # - Fall back to cached Kafka/Redis sync for non-running experiments
            try:
                running = [
                    m
                    for m in experiment_models
                    if str(getattr(m, "status", "") or "").lower() == ExperimentStatus.RUNNING.value
                ]
                if running:
                    sem = asyncio.Semaphore(10)

                    async def _refresh_one(m: ExperimentModel) -> None:
                        async with sem:
                            try:
                                payload = await self.get_experiment_status(str(getattr(m, "id")))
                                if payload and payload.get("status"):
                                    m.status = str(payload["status"])
                                    if payload.get("error_message") is not None:
                                        m.error_message = payload.get("error_message")
                            except Exception:
                                pass

                    await asyncio.gather(*[asyncio.create_task(_refresh_one(m)) for m in running])
            except Exception:
                pass

            # Best-effort cached sync (may update queued/preparing/etc.)
            for model in experiment_models:
                await self._sync_status_from_redis(model, update_in_place=True)

            return [self._model_to_api(model) for model in experiment_models]
        except Exception as e:
            logger.error(f"Failed to list experiments: {e}")
            raise

    async def get_experiment(self, experiment_id: str) -> Optional[Experiment]:
        """Get experiment by ID"""
        try:
            experiment_model = await self.db_service.get_experiment(experiment_id)
            if not experiment_model:
                return None

            # Sync status from Redis to database (runner API is source of truth for running experiments)
            if await self._sync_status_from_redis(experiment_model):
                # Refresh model from database after sync
                experiment_model = await self.db_service.get_experiment(experiment_id)

            return self._model_to_api(experiment_model)

        except Exception as e:
            logger.error(f"Failed to get experiment {experiment_id}: {e}")
            raise

    async def get_experiment_status(self, experiment_id: str) -> Optional[Dict[str, Any]]:
        """Get experiment status"""
        try:
            experiment_model = await self.db_service.get_experiment(experiment_id)
            if not experiment_model:
                return None

            assigned_runner_id = (experiment_model.config or {}).get("assigned_runner_id")
            terminal_statuses = {
                ExperimentStatus.COMPLETED.value,
                ExperimentStatus.FAILED.value,
                ExperimentStatus.CANCELLED.value,
            }
            grace = int(getattr(self.config.runner, "missing_status_grace_seconds", 30) or 30)
            grace = max(0, grace)
            max_404s = int(getattr(self.config.runner, "status_404_release_threshold", 2) or 2)
            max_404s = max(1, max_404s)

            # Prefer querying the assigned runner directly (pool source of truth)
            if assigned_runner_id and self.instance_service:
                inst = await self.instance_service.get_instance(str(assigned_runner_id))
                if inst:
                    runner_base = inst.endpoint_url.rstrip("/")
                    status_url = f"{runner_base}/api/v1/experiments/{experiment_id}/status"
                    try:
                        async with httpx.AsyncClient(timeout=15.0) as client:
                            resp = await client.get(status_url)
                        if resp.status_code == 200:
                            payload = resp.json()
                            runner_status = str(payload.get("status", "")).lower()
                            runner_error = payload.get("error_message")
                            _ = self._missing_status_404_counts.pop((str(assigned_runner_id), str(experiment_id)), None)
                            # If runner reports a terminal status, reflect it in DB and release runner.
                            if runner_status in terminal_statuses:
                                update_kwargs: Dict[str, Any] = {}
                                # Always try to capture runner-side error details for FAILED,
                                # even if DB status is already FAILED (status may have been set earlier without error).
                                if (
                                    runner_status == ExperimentStatus.FAILED.value
                                    and runner_error
                                    and not getattr(experiment_model, "error_message", None)
                                ):
                                    update_kwargs["error_message"] = str(runner_error)
                                if runner_status != experiment_model.status or update_kwargs:
                                    await self.db_service.update_experiment_status(
                                        experiment_id, runner_status, **update_kwargs
                                    )
                                    experiment_model = await self.db_service.get_experiment(experiment_id)
                            if runner_status in terminal_statuses:
                                await self.instance_service.release_runner_by_id_if_experiment(
                                    str(assigned_runner_id), experiment_id
                                )
                        elif resp.status_code == 404:
                            # Hardening: RunnerAPI may return 404 briefly right after /start.
                            within_grace = False
                            try:
                                started_at = getattr(experiment_model, "started_at", None)
                                if started_at and grace > 0:
                                    age = (datetime.now() - started_at).total_seconds()
                                    within_grace = age >= 0 and age < grace
                            except Exception:
                                within_grace = False

                            if within_grace:
                                logger.debug(
                                    f"{experiment_id}: runner {assigned_runner_id} returned 404 for /status; "
                                    f"skipping release (within grace {grace}s)"
                                )
                            else:
                                key = (str(assigned_runner_id), str(experiment_id))
                                self._missing_status_404_counts[key] = self._missing_status_404_counts.get(key, 0) + 1
                                if self._missing_status_404_counts[key] >= max_404s:
                                    _ = self._missing_status_404_counts.pop(key, None)
                                    await self.instance_service.release_runner_by_id_if_experiment(
                                        str(assigned_runner_id), experiment_id
                                    )
                                else:
                                    logger.debug(
                                        f"{experiment_id}: runner {assigned_runner_id} returned 404 for /status; "
                                        f"not releasing yet ({self._missing_status_404_counts[key]}/{max_404s})"
                                    )
                    except Exception:
                        # Best-effort: fall back to cached sync below
                        pass

            # Sync status from cached Redis/Kafka status if enabled (best-effort fallback)
            if await self._sync_status_from_redis(experiment_model):
                experiment_model = await self.db_service.get_experiment(experiment_id)

            # If DB is terminal, also release runner (covers cases where status was synced without polling)
            if (
                self.instance_service
                and assigned_runner_id
                and str(experiment_model.status).lower() in terminal_statuses
            ):
                await self.instance_service.release_runner_by_id_if_experiment(str(assigned_runner_id), experiment_id)

            # Return current status from database (now synced with Redis)
            return {
                "experiment_id": experiment_id,
                "status": experiment_model.status,
                "updated_at": experiment_model.updated_at.isoformat(),
                "runner_id": experiment_model.config.get("assigned_runner_id"),
                "error_message": experiment_model.error_message,
            }

        except Exception as e:
            logger.error(f"Failed to get experiment status {experiment_id}: {e}")
            raise

    async def get_experiment_results(self, experiment_id: str) -> Optional[Dict[str, Any]]:
        """Get experiment results (live during RUNNING, final when COMPLETED)."""
        try:
            experiment_model = await self.db_service.get_experiment(experiment_id)
            if not experiment_model:
                return None

            # Return current metrics regardless of status; include status for the client
            # Compute S3 object paths under experiments_results/
            base_prefix = f"experiments_results/{experiment_id}/"
            plot_object = f"{base_prefix}metrics_plot.png"
            program_object = f"{base_prefix}best_program.py"
            validation_object = f"{base_prefix}validate.py"
            archive_object = f"{base_prefix}results.zip"

            # Optional presigned URLs for convenience (short expiration)
            plot_url = await self.storage_service.get_presigned_url(plot_object, expires_in_seconds=120)
            program_url = await self.storage_service.get_presigned_url(program_object, expires_in_seconds=120)
            archive_url = await self.storage_service.get_presigned_url(archive_object, expires_in_seconds=120)

            payload: Dict[str, Any] = {
                "experiment_id": experiment_id,
                "status": experiment_model.status,
                "metrics": experiment_model.metrics or {},
                "completed_at": experiment_model.completed_at.isoformat() if experiment_model.completed_at else None,
                "runner_id": experiment_model.config.get("assigned_runner_id"),
                "artifacts": {
                    "plot_image_s3": plot_object,
                    "best_program_s3": program_object,
                    "validation_s3": validation_object,
                    "archive_s3": archive_object,
                    "plot_url": plot_url,
                    "best_program_url": program_url,
                    "archive_url": archive_url,
                },
            }
            return payload

        except Exception as e:
            logger.error(f"Failed to get experiment results {experiment_id}: {e}")
            raise

    async def start_experiment_forward(
        self, experiment_id: str, instance_id: Optional[str] = None, *, from_scheduler: bool = False
    ) -> StartResult:
        """Start experiment execution by directly calling RunnerAPI endpoints step by step"""
        allocated_runner_id: Optional[str] = None
        try:
            experiment_model = await self.db_service.get_experiment(experiment_id)
            if not experiment_model:
                msg = f"Experiment {experiment_id} not found"
                logger.warning(msg)
                return StartResult(ok=False, http_status=404, detail=msg)

            status = str(experiment_model.status or "").lower()

            # Prerequisite: experiment files must be prepared (experiment_files_path present in config)
            cfg = dict(getattr(experiment_model, "config", {}) or {})
            if not cfg.get("experiment_files_path"):
                msg = "Experiment files are not ready yet (missing experiment_files_path)"
                logger.warning(f"{experiment_id}: {msg}")
                return StartResult(ok=False, http_status=409, detail=msg)

            if status == ExperimentStatus.PREPARATION_FAILED.value:
                msg = f"Experiment files failed to prepare: {experiment_model.error_message or ''}".strip()
                return StartResult(ok=False, http_status=409, detail=msg or "Experiment preparation failed")

            # Idempotent start for user-initiated calls
            if not from_scheduler and status in {
                ExperimentStatus.QUEUED.value,
                ExperimentStatus.DISPATCHING.value,
            }:
                return StartResult(
                    ok=True,
                    http_status=202,
                    payload={
                        "message": "Experiment already queued",
                        "experiment_id": experiment_id,
                        "status": status,
                        "status_message": getattr(experiment_model, "status_message", None),
                    },
                )

            if status in {
                ExperimentStatus.PENDING.value,
                ExperimentStatus.INITIALIZING.value,
                ExperimentStatus.PREPARING.value,
                ExperimentStatus.RUNNING.value,
            }:
                msg = f"Cannot start experiment in status '{status}'"
                logger.warning(f"{experiment_id}: {msg}")
                return StartResult(ok=False, http_status=409, detail=msg)

            # Find available instance or use specified one
            if not self.instance_service:
                msg = "Instance service not available"
                logger.error(msg)
                return StartResult(ok=False, http_status=503, detail=msg)

            # Allocate a runner from the pool (binary READY/BUSY).
            # If instance_id is provided, try to allocate that specific runner.
            if instance_id:
                instance = await self.instance_service.allocate_specific_runner(str(instance_id), experiment_id)
            else:
                instance = await self.instance_service.allocate_runner(experiment_id)
            if not instance:
                waiting_msg = "Waiting for runner capacity"
                await self.db_service.update_experiment(
                    experiment_id,
                    status=ExperimentStatus.QUEUED.value,
                    status_message=waiting_msg,
                    error_message=None,
                )
                return StartResult(
                    ok=True,
                    http_status=202,
                    payload={
                        "message": "Experiment queued",
                        "experiment_id": experiment_id,
                        "status": ExperimentStatus.QUEUED.value,
                        "status_message": waiting_msg,
                    },
                )
            instance_id = instance.id
            allocated_runner_id = str(instance_id)

            logger.info(f"Starting experiment {experiment_id} on instance {instance_id}")

            # Step 1: Update status to initializing
            await self.db_service.update_experiment(
                experiment_id,
                status=ExperimentStatus.INITIALIZING.value,
                started_at=datetime.now(),
                status_message=None,
                error_message=None,
            )
            logger.info(f"Updated experiment {experiment_id} status to initializing")

            # Step 2: Call RunnerAPI initialize endpoint
            runner_base_url = instance.endpoint_url.rstrip("/")
            initialize_url = f"{runner_base_url}/api/v1/experiments/{experiment_id}/initialize"

            # Prepare experiment config for RunnerAPI
            experiment_config = {
                "task_type": experiment_model.config.get("parameters", {}).get("task_type", "classification"),
                "task_description": experiment_model.config.get("description", ""),
                "dataset_path": experiment_model.data_path or "",
                "target_column": experiment_model.config.get("parameters", {}).get("target_column"),
                "n_classes": experiment_model.config.get("parameters", {}).get("n_classes"),
                "n_clusters": experiment_model.config.get("parameters", {}).get("n_clusters"),
                "llm_model": experiment_model.config.get("llm_model", "local-inference"),
                "prompt_llm_model": experiment_model.config.get("prompt_llm_model"),
                "max_iterations": experiment_model.config.get("max_iterations", 100),
                "timeout_seconds": experiment_model.config.get("timeout_seconds", 3600),
                "dataset_size": experiment_model.config.get("dataset_size"),
                "test_size": experiment_model.config.get("test_size"),
            }

            # Remove None values from config
            experiment_config = {k: v for k, v in experiment_config.items() if v is not None}

            # Initialize experiment on RunnerAPI with small built-in retry to handle
            # transient DNS / connectivity issues (e.g., name resolution right after startup).
            try:
                init_max_attempts = 3
                last_init_error: Optional[Exception] = None

                for attempt in range(1, init_max_attempts + 1):
                    try:
                        async with httpx.AsyncClient(timeout=30.0) as client:
                            logger.info(f"Sending initialize request to {initialize_url} (attempt {attempt})")
                            init_response = await client.post(initialize_url, json=experiment_config)

                        if init_response.status_code != 200:
                            error_msg = (
                                f"Failed to initialize experiment on RunnerAPI: "
                                f"{init_response.status_code} - {init_response.text}"
                            )
                            logger.error(error_msg)
                            await self.db_service.update_experiment(
                                experiment_id,
                                status=ExperimentStatus.FAILED.value,
                                error_message=error_msg,
                                status_message=None,
                            )
                            # Release allocated runner on failure
                            if allocated_runner_id and self.instance_service:
                                await self.instance_service.release_runner_by_id_if_experiment(
                                    allocated_runner_id, experiment_id
                                )
                            return StartResult(ok=False, http_status=502, detail=error_msg)

                        _ = init_response.json()
                        logger.info(f"Successfully initialized experiment {experiment_id} on RunnerAPI")
                        last_init_error = None
                        break
                    except httpx.RequestError as e:
                        last_init_error = e
                        logger.warning(
                            f"Initialize request to RunnerAPI at {runner_base_url} failed on attempt {attempt}: {e}"
                        )
                        if attempt < init_max_attempts:
                            # Backoff before retrying
                            await asyncio.sleep(1 * attempt)

                if last_init_error:
                    error_msg = f"Failed to connect to RunnerAPI at {runner_base_url}: {last_init_error}"
                    logger.error(error_msg)
                    await self.db_service.update_experiment(
                        experiment_id,
                        status=ExperimentStatus.FAILED.value,
                        error_message=error_msg,
                        status_message=None,
                    )
                    # Release allocated runner on failure
                    if allocated_runner_id and self.instance_service:
                        await self.instance_service.release_runner_by_id_if_experiment(
                            allocated_runner_id, experiment_id
                        )
                    return StartResult(ok=False, http_status=502, detail=error_msg)

            except Exception as e:
                # Catch-all to avoid leaving experiment in inconsistent state
                error_msg = f"Error during initialization on RunnerAPI at {runner_base_url}: {e}"
                logger.error(error_msg)
                await self.db_service.update_experiment(
                    experiment_id,
                    status=ExperimentStatus.FAILED.value,
                    error_message=error_msg,
                    status_message=None,
                )
                if allocated_runner_id and self.instance_service:
                    await self.instance_service.release_runner_by_id_if_experiment(allocated_runner_id, experiment_id)
                return StartResult(ok=False, http_status=502, detail=error_msg)

            # Step 3: Update status to preparing
            await self.db_service.update_experiment(experiment_id, status=ExperimentStatus.PREPARING.value)
            logger.info(f"Updated experiment {experiment_id} status to preparing")

            # Step 4: Call RunnerAPI start endpoint
            start_url = f"{runner_base_url}/api/v1/experiments/{experiment_id}/start"

            # Start experiment on RunnerAPI with small built-in retry similar to initialize step.
            try:
                start_max_attempts = 3
                last_start_error: Optional[Exception] = None

                for attempt in range(1, start_max_attempts + 1):
                    try:
                        async with httpx.AsyncClient(timeout=30.0) as client:
                            logger.info(f"Sending start request to {start_url} (attempt {attempt})")
                            start_response = await client.post(start_url)

                        if start_response.status_code != 200:
                            error_msg = (
                                f"Failed to start experiment on RunnerAPI: "
                                f"{start_response.status_code} - {start_response.text}"
                            )
                            logger.error(error_msg)
                            await self.db_service.update_experiment(
                                experiment_id,
                                status=ExperimentStatus.FAILED.value,
                                error_message=error_msg,
                                status_message=None,
                            )
                            # Release allocated runner on failure
                            if allocated_runner_id and self.instance_service:
                                await self.instance_service.release_runner_by_id_if_experiment(
                                    allocated_runner_id, experiment_id
                                )
                            return StartResult(ok=False, http_status=502, detail=error_msg)

                        _ = start_response.json()
                        logger.info(f"Successfully started experiment {experiment_id} on RunnerAPI")
                        last_start_error = None
                        break
                    except httpx.RequestError as e:
                        last_start_error = e
                        logger.warning(
                            f"Start request to RunnerAPI at {runner_base_url} failed on attempt {attempt}: {e}"
                        )
                        if attempt < start_max_attempts:
                            await asyncio.sleep(1 * attempt)

                if last_start_error:
                    error_msg = f"Failed to connect to RunnerAPI at {runner_base_url}: {last_start_error}"
                    logger.error(error_msg)
                    await self.db_service.update_experiment(
                        experiment_id,
                        status=ExperimentStatus.FAILED.value,
                        error_message=error_msg,
                        status_message=None,
                    )
                    # Release allocated runner on failure
                    if allocated_runner_id and self.instance_service:
                        await self.instance_service.release_runner_by_id_if_experiment(
                            allocated_runner_id, experiment_id
                        )
                    return StartResult(ok=False, http_status=502, detail=error_msg)

            except Exception as e:
                error_msg = f"Error during start on RunnerAPI at {runner_base_url}: {e}"
                logger.error(error_msg)
                await self.db_service.update_experiment(
                    experiment_id,
                    status=ExperimentStatus.FAILED.value,
                    error_message=error_msg,
                    status_message=None,
                )
                if allocated_runner_id and self.instance_service:
                    await self.instance_service.release_runner_by_id_if_experiment(allocated_runner_id, experiment_id)
                return StartResult(ok=False, http_status=502, detail=error_msg)

            # Step 5: Update status to running and assign runner
            await self.db_service.update_experiment(
                experiment_id, status=ExperimentStatus.RUNNING.value, status_message=None
            )

            logger.info(f"Successfully started experiment {experiment_id} on instance {instance_id}")
            return StartResult(
                ok=True,
                http_status=200,
                payload={"message": "Experiment started", "experiment_id": experiment_id},
            )

        except Exception as e:
            error_msg = f"Failed to start experiment {experiment_id}: {e}"
            logger.error(error_msg)
            # Update experiment status to failed on any error
            try:
                await self.db_service.update_experiment(
                    experiment_id,
                    status=ExperimentStatus.FAILED.value,
                    error_message=error_msg,
                    status_message=None,
                )
            except Exception as db_error:
                logger.error(f"Failed to update experiment status to failed: {db_error}")
            # Rollback/release runner if we allocated one
            try:
                if allocated_runner_id and self.instance_service:
                    await self.instance_service.release_runner_by_id_if_experiment(allocated_runner_id, experiment_id)
            except Exception:
                pass
            return StartResult(ok=False, http_status=500, detail=error_msg)

    async def stop_experiment(self, experiment_id: str) -> bool:
        """Stop experiment execution"""
        try:
            experiment_model = await self.db_service.get_experiment(experiment_id)
            if not experiment_model:
                logger.warning(f"Experiment {experiment_id} not found")
                return False

            if experiment_model.status != "running":
                logger.warning(f"Cannot stop experiment {experiment_id} in status {experiment_model.status}")
                return False

            # Update status to cancelled
            success = await self.db_service.update_experiment_status(experiment_id, "cancelled")

            if success and self.kafka_service:
                assigned_runner_id = experiment_model.config.get("assigned_runner_id")
                if assigned_runner_id:
                    # Send stop command to runner
                    await self.kafka_service.publish_experiment_stop_command(experiment_id, assigned_runner_id)
                logger.info(f"Published stop command for experiment {experiment_id}")

            logger.info(f"Stopped experiment {experiment_id}")
            return True

        except Exception as e:
            logger.error(f"Failed to stop experiment {experiment_id}: {e}")
            return False

    async def stop_experiment_forward(self, experiment_id: str, instance_id: Optional[str] = None) -> tuple[bool, str]:
        """Stop experiment execution by directly calling RunnerAPI."""
        try:
            # Resolve experiment and instance
            experiment_model = await self.db_service.get_experiment(experiment_id)
            if not experiment_model:
                error_msg = f"Experiment {experiment_id} not found"
                logger.warning(error_msg)
                return False, error_msg

            status = str(experiment_model.status or "").lower()

            # If not running, treat stop as cancel (remove from queue/scheduler eligibility).
            if status != ExperimentStatus.RUNNING.value:
                await self.db_service.update_experiment(
                    experiment_id,
                    status=ExperimentStatus.CANCELLED.value,
                    status_message=None,
                )
                # Best-effort release if somehow assigned
                assigned_runner_id = (experiment_model.config or {}).get("assigned_runner_id")
                if assigned_runner_id and self.instance_service:
                    await self.instance_service.release_runner_by_id_if_experiment(
                        str(assigned_runner_id), experiment_id
                    )
                return True, ""

            if not self.instance_service:
                error_msg = "Instance service not available"
                logger.error(error_msg)
                return False, error_msg

            # Prefer explicitly provided instance_id, else use assigned runner from experiment config
            target_instance_id = instance_id or experiment_model.config.get("assigned_runner_id")
            if not target_instance_id:
                error_msg = "No assigned runner instance found for this experiment"
                logger.error(error_msg)
                return False, error_msg

            instance = await self.instance_service.get_instance(target_instance_id)
            if not instance:
                error_msg = f"Runner instance '{target_instance_id}' not found"
                logger.error(error_msg)
                return False, error_msg

            runner_base_url = instance.endpoint_url.rstrip("/")
            stop_url = f"{runner_base_url}/api/v1/experiments/{experiment_id}/stop"

            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    logger.info(f"Sending stop request to {stop_url}")
                    resp = await client.post(stop_url)
                    if resp.status_code != 200:
                        error_msg = f"Failed to stop experiment on RunnerAPI: {resp.status_code} - {resp.text}"
                        logger.error(error_msg)
                        return False, error_msg
            except httpx.RequestError as e:
                error_msg = f"Failed to connect to RunnerAPI at {runner_base_url}: {e}"
                logger.error(error_msg)
                return False, error_msg

            # Reflect cancelled state in DB
            await self.db_service.update_experiment_status(experiment_id, ExperimentStatus.CANCELLED.value)
            # Release runner back to pool (best-effort, ownership-checked)
            await self.instance_service.release_runner_by_id_if_experiment(target_instance_id, experiment_id)
            logger.info(
                f"Successfully stopped experiment {experiment_id} via RunnerAPI on instance {target_instance_id}"
            )
            return True, ""

        except Exception as e:
            error_msg = f"Failed to stop experiment {experiment_id}: {e}"
            logger.error(error_msg)
            return False, error_msg

    async def _queue_scheduler_loop(self) -> None:
        """Background loop to start queued experiments when capacity is available."""
        poll = int(getattr(self.config.runner, "queue_poll_interval_seconds", 3) or 3)
        poll = max(1, poll)
        ttl = int(getattr(self.config.runner, "dispatching_ttl_seconds", 60) or 60)
        ttl = max(10, ttl)
        logger.info(f"Starting experiment queue scheduler (poll={poll}s, dispatching_ttl={ttl}s)")
        while True:
            try:
                # Recover stale dispatching -> queued
                try:
                    await self.db_service.recover_stale_dispatching(ttl_seconds=ttl)
                except Exception:
                    pass

                # Best UX: do not flip QUEUED->DISPATCHING unless there is runner capacity.
                try:
                    if not await self.db_service.has_ready_runner_capacity():
                        await asyncio.sleep(poll)
                        continue
                except Exception:
                    # If capacity check fails, fall back to previous behavior
                    pass

                claimed = await self.db_service.claim_next_queued_for_dispatching()
                if not claimed:
                    await asyncio.sleep(poll)
                    continue

                exp_id = str(claimed.id)

                # Enforce prerequisites (files ready) at scheduler time too
                cfg = dict(getattr(claimed, "config", {}) or {})
                if not cfg.get("experiment_files_path"):
                    await self.db_service.update_experiment(
                        exp_id,
                        status=ExperimentStatus.PREPARATION_FAILED.value,
                        error_message="Experiment files not ready (missing experiment_files_path)",
                        status_message=None,
                    )
                    continue

                res = await self.start_experiment_forward(exp_id, from_scheduler=True)
                if res.ok and res.http_status == 202:
                    # Still no runners available -> back to queued
                    await self.db_service.update_experiment(
                        exp_id,
                        status=ExperimentStatus.QUEUED.value,
                        status_message="Waiting for runner capacity",
                    )
                await asyncio.sleep(0)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Queue scheduler tick error: {e}")
                await asyncio.sleep(poll)

    async def upload_experiment_data(self, experiment_id: str, file_path: str, filename: str) -> bool:
        """Upload data file for experiment"""
        try:
            # Upload file to storage
            storage_path = await self.storage_service.upload_experiment_data(file_path, filename)
            if not storage_path:
                logger.error(f"Failed to upload data file {filename}")
                return False

            # Update experiment with data path
            success = await self.db_service.update_experiment(experiment_id, data_path=storage_path)
            if success:
                logger.info(f"Uploaded data file for experiment {experiment_id}: {storage_path}")
            else:
                logger.error("Failed to update experiment data path")

            return success

        except Exception as e:
            logger.error(f"Failed to upload experiment data for {experiment_id}: {e}")
            return False

    async def drop_all_experiments(self) -> Dict[str, Any]:
        """Drop all experiments and their data"""
        try:
            logger.info("Starting to drop all experiments and data")

            deleted_experiments = 0
            deleted_objects = 0
            stopped_runner_ids: set[str] = set()
            stop_failed_runner_ids: set[str] = set()
            runner_release_failed = False
            experiments_deletion_ok = True
            storage_deletion_ok = True
            errors: List[str] = []

            # Best-effort stop/cancel before wiping.
            try:
                if self.db_service:
                    # Fetch all experiments (page through to avoid hard-coded limits).
                    all_models: List[ExperimentModel] = []
                    limit = 1000
                    offset = 0
                    while True:
                        batch = await self.db_service.list_experiments(status=None, limit=limit, offset=offset)
                        if not batch:
                            break
                        all_models.extend(batch)
                        offset += len(batch)
                        if len(batch) < limit:
                            break

                    inflight_statuses = {
                        ExperimentStatus.PENDING.value,
                        ExperimentStatus.QUEUED.value,
                        ExperimentStatus.DISPATCHING.value,
                        ExperimentStatus.INITIALIZING.value,
                        ExperimentStatus.PREPARING.value,
                        ExperimentStatus.PREPARED.value,
                        ExperimentStatus.DEPLOYED.value,
                    }

                    sem = asyncio.Semaphore(5)

                    async def _stop_one(exp_id: str, runner_id: Optional[str]) -> None:
                        nonlocal stopped_runner_ids, stop_failed_runner_ids
                        async with sem:
                            try:
                                ok, err = await asyncio.wait_for(
                                    self.stop_experiment_forward(exp_id),
                                    timeout=30,
                                )
                                if ok:
                                    if runner_id:
                                        stopped_runner_ids.add(str(runner_id))
                                else:
                                    if runner_id:
                                        stop_failed_runner_ids.add(str(runner_id))
                                    logger.warning(f"Drop-all stop failed for {exp_id}: {err}")
                            except Exception as e:
                                if runner_id:
                                    stop_failed_runner_ids.add(str(runner_id))
                                logger.warning(f"Drop-all stop exception for {exp_id}: {e}")

                    stop_tasks: List[asyncio.Task] = []
                    for m in all_models:
                        st = str(getattr(m, "status", "") or "").lower()
                        exp_id = str(getattr(m, "id"))
                        assigned_runner_id = (getattr(m, "config", {}) or {}).get("assigned_runner_id")

                        if st == ExperimentStatus.RUNNING.value:
                            stop_tasks.append(asyncio.create_task(_stop_one(exp_id, assigned_runner_id)))
                            continue

                        # Cancel any inflight non-running statuses to avoid stale UI/DB state
                        # and best-effort release runners if assigned.
                        if st in inflight_statuses:
                            try:
                                await self.db_service.update_experiment_status(exp_id, ExperimentStatus.CANCELLED.value)
                            except Exception:
                                pass
                            if assigned_runner_id and self.instance_service:
                                try:
                                    await self.instance_service.release_runner_by_id_if_experiment(
                                        str(assigned_runner_id), exp_id
                                    )
                                except Exception:
                                    runner_release_failed = True
                                    pass

                    if stop_tasks:
                        await asyncio.gather(*stop_tasks, return_exceptions=True)

            except Exception as e:
                # Best-effort only; proceed with wipe regardless
                msg = f"Drop-all: pre-wipe stop/cancel phase failed/skipped: {e}"
                logger.warning(msg)
                runner_release_failed = True
                errors.append(msg)

            # Delete all experiments from database
            if self.db_service:
                try:
                    deleted_experiments = await self.db_service.delete_all_experiments()
                except Exception as e:
                    experiments_deletion_ok = False
                    msg = f"Drop-all: experiments DB deletion failed: {e}"
                    logger.error(msg)
                    errors.append(msg)

            # Delete all experiment data from storage
            if self.storage_service:
                try:
                    deleted_objects = await self.storage_service.delete_all_experiment_data()
                except Exception as e:
                    storage_deletion_ok = False
                    msg = f"Drop-all: storage deletion failed: {e}"
                    logger.error(msg)
                    errors.append(msg)

            logger.info(f"Successfully dropped {deleted_experiments} experiments and {deleted_objects} storage objects")

            failed_runner_count = len(stop_failed_runner_ids)
            runners_releasing_ok = (failed_runner_count == 0) and (runner_release_failed is False)

            message = (
                f"Experiments deletion: {'succeeded' if experiments_deletion_ok else 'failed'}\n"
                f"Storage objects deletion: {'succeeded' if storage_deletion_ok else 'failed'}\n"
                f"Runners releasing: {'succeeded' if runners_releasing_ok else 'failed'}"
            )

            error: Optional[str] = None
            if errors:
                # Keep response concise; full details already in logs.
                error = "; ".join(errors)

            resp: Dict[str, Any] = {
                "deleted_experiments": deleted_experiments,
                "deleted_storage_objects": deleted_objects,
                "stopped_runner_ids": sorted(stopped_runner_ids) if stopped_runner_ids else [],
                "failed_runner_ids": sorted(stop_failed_runner_ids) if stop_failed_runner_ids else [],
                "message": message,
            }
            # Only include error field when we actually have one (UI checks key presence).
            if error:
                resp["error"] = error
            return resp

        except Exception as e:
            logger.error(f"Failed to drop all experiments: {e}")
            raise

    async def _sync_status_from_redis(self, experiment_model: ExperimentModel, update_in_place: bool = False) -> bool:
        """
        Sync experiment status from Redis to database.
        Returns True if status was synced, False otherwise.
        If update_in_place is True, updates the model's status field directly.
        """
        if not self.kafka_service:
            return False

        try:
            cached_status = await self.kafka_service.get_cached_experiment_status(str(experiment_model.id))
            if not cached_status or not cached_status.get("status"):
                return False

            redis_status = str(cached_status.get("status"))
            # Only sync if status differs (avoid unnecessary DB writes)
            if redis_status == experiment_model.status:
                return False

            # Update database to match Redis status
            update_kwargs = {}
            if cached_status.get("updated_at"):
                try:
                    update_kwargs["updated_at"] = datetime.fromisoformat(str(cached_status.get("updated_at")))
                except (ValueError, TypeError) as e:
                    logger.debug(f"Failed to parse updated_at from Redis for {experiment_model.id}: {e}")

            success = await self.db_service.update_experiment_status(
                str(experiment_model.id), redis_status, **update_kwargs
            )

            if success:
                if update_in_place:
                    experiment_model.status = redis_status
                return True
            else:
                logger.warning(f"Failed to sync Redis status to database for experiment {experiment_model.id}")
                return False

        except Exception as sync_error:
            logger.debug(f"Failed to sync status from Redis for experiment {experiment_model.id}: {sync_error}")
            return False

    def _model_to_api(self, model: ExperimentModel) -> Experiment:
        """Convert database model to API model"""
        return Experiment(
            id=str(model.id),
            name=model.name,
            config=model.config,
            data_path=model.data_path or "",
            status=ExperimentStatus(model.status),
            created_at=model.created_at,
            updated_at=model.updated_at,
            started_at=getattr(model, "started_at", None),
            completed_at=getattr(model, "completed_at", None),
            metrics=model.metrics or {},
            best_result=getattr(model, "best_result", None),
            error_message=model.error_message,
            status_message=getattr(model, "status_message", None),
        )
