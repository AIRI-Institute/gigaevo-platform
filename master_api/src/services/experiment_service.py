#!/usr/bin/env python3

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
from .runner_deployment_consumer import RunnerDeploymentConsumer
from .runner_instance_service import RunnerInstanceService
from .storage_service import StorageService
from .workflow_consumer import WorkflowConsumer


class ExperimentService:
    """Enhanced experiment service with database and workflow integration"""

    def __init__(
        self,
        db_service: DatabaseService,
        storage_service: StorageService,
        kafka_service: Optional[KafkaService] = None,
        workflow_consumer: Optional[WorkflowConsumer] = None,
        deployment_consumer: Optional[RunnerDeploymentConsumer] = None,
        instance_service: Optional[RunnerInstanceService] = None,
        config: Optional[Config] = None,
    ):
        self.config = config or load_config()
        self.db_service = db_service
        self.kafka_service = kafka_service
        self.storage_service = storage_service
        self.workflow_consumer = workflow_consumer
        self.deployment_consumer = deployment_consumer
        self.instance_service = instance_service

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
                if not self.deployment_consumer:
                    logger.warning("Kafka is enabled but deployment_consumer not provided")

            logger.info("Experiment service initialized successfully")

        except Exception as e:
            logger.error(f"Failed to initialize experiment service: {e}")
            raise

    async def cleanup(self):
        """Cleanup resources"""
        if self.deployment_consumer:
            await self.deployment_consumer.cleanup()
        if self.workflow_consumer:
            await self.workflow_consumer.cleanup()
        if self.kafka_service:
            await self.kafka_service.cleanup()
        if self.storage_service:
            pass  # Storage service doesn't have cleanup currently
        if self.db_service:
            await self.db_service.cleanup()

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

            # Sync status from Redis to database (runner API is source of truth for running experiments)
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

            # Sync status from Redis to database (runner API is source of truth for running experiments)
            if await self._sync_status_from_redis(experiment_model):
                # Refresh model from database after sync
                experiment_model = await self.db_service.get_experiment(experiment_id)

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

    async def start_experiment(self, experiment_id: str) -> tuple[bool, str]:
        """Start experiment execution"""
        try:
            experiment_model = await self.db_service.get_experiment(experiment_id)
            if not experiment_model:
                error_msg = f"Experiment {experiment_id} not found"
                logger.warning(error_msg)
                return False, error_msg

            if experiment_model.status in [
                ExperimentStatus.PENDING.value,
                ExperimentStatus.INITIALIZING.value,
                ExperimentStatus.PREPARING.value,
                ExperimentStatus.RUNNING.value,
            ]:
                error_msg = f"Cannot start experiment {experiment_id} in status '{experiment_model.status}'"
                logger.warning(error_msg)
                return False, error_msg

            # Update status to running
            # success = await self.db_service.update_experiment_status(
            #     experiment_id, ExperimentStatus.INITIALIZING.value, started_at=datetime.now(tz=self.config.timezone)
            # )
            success = True  # TODO - remove and uncomment above text

            if success and self.kafka_service:
                # If runner is assigned, send start command
                assigned_runner_id = experiment_model.config.get("assigned_runner_id")
                if assigned_runner_id:
                    await self.kafka_service.publish_experiment_start_command(experiment_id, assigned_runner_id)
                    logger.info(f"Published start command for experiment {experiment_id}")
                else:
                    # Use deployment consumer to manually deploy
                    if self.deployment_consumer:
                        deployment_success = await self.deployment_consumer.manually_deploy_experiment(experiment_id)
                        if deployment_success:
                            logger.info(f"Manually deployed experiment {experiment_id}")
                        else:
                            logger.warning(f"Failed to manually deploy experiment {experiment_id}")

            logger.info(f"Started experiment {experiment_id}")
            return True, ""

        except Exception as e:
            error_msg = f"Failed to start experiment {experiment_id}: {e}"
            logger.error(error_msg)
            return False, error_msg

    async def start_experiment_forward(self, experiment_id: str, instance_id: Optional[str] = None) -> tuple[bool, str]:
        """Start experiment execution by directly calling RunnerAPI endpoints step by step"""
        try:
            experiment_model = await self.db_service.get_experiment(experiment_id)
            if not experiment_model:
                error_msg = f"Experiment {experiment_id} not found"
                logger.warning(error_msg)
                return False, error_msg

            if experiment_model.status in [
                ExperimentStatus.PENDING.value,
                ExperimentStatus.INITIALIZING.value,
                ExperimentStatus.PREPARING.value,
                ExperimentStatus.RUNNING.value,
            ]:
                error_msg = f"Cannot start experiment {experiment_id} in status '{experiment_model.status}'"
                logger.warning(error_msg)
                return False, error_msg

            # Find available instance or use specified one
            if not self.instance_service:
                error_msg = "Instance service not available"
                logger.error(error_msg)
                return False, error_msg

            if instance_id:
                instance = await self.instance_service.get_instance(instance_id)
                if not instance:
                    error_msg = f"Specified instance {instance_id} not found"
                    logger.error(error_msg)
                    return False, error_msg
            else:
                # Get first available instance
                instance = await self.instance_service.get_available_instance()
                if not instance:
                    error_msg = "No available instances for experiment deployment"
                    logger.error(error_msg)
                    return False, error_msg
                instance_id = instance.id

            logger.info(f"Starting experiment {experiment_id} on instance {instance_id}")

            # Step 1: Update status to initializing
            await self.db_service.update_experiment_status(
                experiment_id, ExperimentStatus.INITIALIZING.value, started_at=datetime.now()
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
            }

            # Remove None values from config
            experiment_config = {k: v for k, v in experiment_config.items() if v is not None}

            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    logger.info(f"Sending initialize request to {initialize_url}")
                    init_response = await client.post(initialize_url, json=experiment_config)

                    if init_response.status_code != 200:
                        error_msg = f"Failed to initialize experiment on RunnerAPI: {init_response.status_code} - {init_response.text}"
                        logger.error(error_msg)
                        await self.db_service.update_experiment_status(
                            experiment_id, ExperimentStatus.FAILED.value, error_message=error_msg
                        )
                        return False, error_msg

                    _ = init_response.json()
                    logger.info(f"Successfully initialized experiment {experiment_id} on RunnerAPI")

            except httpx.RequestError as e:
                error_msg = f"Failed to connect to RunnerAPI at {runner_base_url}: {e}"
                logger.error(error_msg)
                await self.db_service.update_experiment_status(
                    experiment_id, ExperimentStatus.FAILED.value, error_message=error_msg
                )
                return False, error_msg

            # Step 3: Update status to preparing
            await self.db_service.update_experiment_status(experiment_id, ExperimentStatus.PREPARING.value)
            logger.info(f"Updated experiment {experiment_id} status to preparing")

            # Step 4: Call RunnerAPI start endpoint
            start_url = f"{runner_base_url}/api/v1/experiments/{experiment_id}/start"

            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    logger.info(f"Sending start request to {start_url}")
                    start_response = await client.post(start_url)

                    if start_response.status_code != 200:
                        error_msg = f"Failed to start experiment on RunnerAPI: {start_response.status_code} - {start_response.text}"
                        logger.error(error_msg)
                        await self.db_service.update_experiment_status(
                            experiment_id, ExperimentStatus.FAILED.value, error_message=error_msg
                        )
                        return False, error_msg

                    _ = start_response.json()
                    logger.info(f"Successfully started experiment {experiment_id} on RunnerAPI")

            except httpx.RequestError as e:
                error_msg = f"Failed to connect to RunnerAPI at {runner_base_url}: {e}"
                logger.error(error_msg)
                await self.db_service.update_experiment_status(
                    experiment_id, ExperimentStatus.FAILED.value, error_message=error_msg
                )
                return False, error_msg

            # Step 5: Update status to running and assign runner
            await self.db_service.update_experiment_status(experiment_id, ExperimentStatus.RUNNING.value)

            # Store the assigned runner ID in the experiment config
            updated_config = experiment_model.config.copy()
            updated_config["assigned_runner_id"] = instance_id
            await self.db_service.update_experiment(experiment_id, config=updated_config)

            logger.info(f"Successfully started experiment {experiment_id} on instance {instance_id}")
            return True, ""

        except Exception as e:
            error_msg = f"Failed to start experiment {experiment_id}: {e}"
            logger.error(error_msg)
            # Update experiment status to failed on any error
            try:
                await self.db_service.update_experiment_status(
                    experiment_id, ExperimentStatus.FAILED.value, error_message=error_msg
                )
            except Exception as db_error:
                logger.error(f"Failed to update experiment status to failed: {db_error}")
            return False, error_msg

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

            if experiment_model.status not in [
                ExperimentStatus.RUNNING.value,
            ]:
                error_msg = f"Cannot stop experiment {experiment_id} in status '{experiment_model.status}'"
                logger.warning(error_msg)
                return False, error_msg

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
            logger.info(
                f"Successfully stopped experiment {experiment_id} via RunnerAPI on instance {target_instance_id}"
            )
            return True, ""

        except Exception as e:
            error_msg = f"Failed to stop experiment {experiment_id}: {e}"
            logger.error(error_msg)
            return False, error_msg

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

    async def manually_deploy_experiment(self, experiment_id: str, instance_id: Optional[str] = None) -> bool:
        """Manually deploy experiment to available RunnerAPI instance"""
        try:
            # Get experiment details
            experiment = await self.db_service.get_experiment(experiment_id)
            if not experiment:
                logger.error(f"Experiment {experiment_id} not found")
                return False

            if not self.instance_service:
                logger.error("Instance service not available")
                return False

            # Find available instance or use specified one
            if instance_id:
                instance = await self.instance_service.get_instance(instance_id)
                if not instance:
                    logger.error(f"Specified instance {instance_id} not found")
                    return False
            else:
                # Get first available instance
                instance = await self.instance_service.get_available_instance()
                if not instance:
                    logger.error("No available instances for experiment deployment")
                    return False
                instance_id = instance.id

            # Prepare experiment data for RunnerAPI
            experiment_data = {
                "experiment_id": experiment_id,
                "config": experiment.config,
                "data_path": experiment.data_path,
                "metadata": {"created_at": experiment.created_at.isoformat(), "source": "master_api_manual_deployment"},
            }

            # Deploy to instance
            success = await self.instance_service.deploy_experiment_to_instance(
                instance_id, experiment_id, experiment_data
            )

            if success:
                logger.info(f"Successfully deployed experiment {experiment_id} to instance {instance_id}")

                # Update experiment status
                await self.db_service.update_experiment_status(experiment_id, "deployed")

                return True
            else:
                logger.error(f"Failed to deploy experiment {experiment_id} to instance {instance_id}")
                return False

        except Exception as e:
            logger.error(f"Failed to manually deploy experiment {experiment_id}: {e}")
            return False

    async def drop_all_experiments(self) -> Dict[str, Any]:
        """Drop all experiments and their data"""
        try:
            logger.info("Starting to drop all experiments and data")

            deleted_experiments = 0
            deleted_objects = 0

            # Delete all experiments from database
            if self.db_service:
                deleted_experiments = await self.db_service.delete_all_experiments()

            # Delete all experiment data from storage
            if self.storage_service:
                deleted_objects = await self.storage_service.delete_all_experiment_data()

            logger.info(f"Successfully dropped {deleted_experiments} experiments and {deleted_objects} storage objects")

            return {
                "deleted_experiments": deleted_experiments,
                "deleted_storage_objects": deleted_objects,
                "message": f"Successfully dropped {deleted_experiments} experiments and {deleted_objects} storage objects",
            }

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
            data_path=model.data_path,
            status=ExperimentStatus(model.status),
            created_at=model.created_at,
            updated_at=model.updated_at,
            metrics=model.metrics or {},
            error_message=model.error_message,
        )
