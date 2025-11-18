#!/usr/bin/env python3

from datetime import datetime
from typing import Any, Dict

from loguru import logger

from ..config import load_config
from ..services.database_service import DatabaseService
from ..services.experiment_creation_service import ExperimentCreationService
from ..services.kafka_service import KafkaService
from ..services.storage_service import StorageService


class WorkflowConsumer:
    """Consumer for processing experiment workflow events"""

    def __init__(self, config=None):
        self.config = config or load_config()
        self.db_service: DatabaseService
        self.storage_service: StorageService
        self.creation_service: ExperimentCreationService
        self.kafka_service: KafkaService

    async def initialize(self):
        """Initialize all required services"""
        try:
            logger.info("Initializing workflow consumer...")

            # Initialize database service
            logger.info("Initializing database service...")
            self.db_service = DatabaseService(self.config)
            await self.db_service.initialize()
            logger.info("Database service initialized successfully")

            # Initialize storage service
            logger.info("Initializing storage service...")
            self.storage_service = StorageService(self.config)
            await self.storage_service.initialize()
            logger.info("Storage service initialized successfully")

            # Initialize Kafka service
            logger.info(f"KAFKA ENABLED: {self.config.kafka.enabled}")
            if self.config.kafka.enabled:
                logger.info("Initializing Kafka service...")
                self.kafka_service = KafkaService(self.config)
                await self.kafka_service.initialize()
                logger.info("Kafka service initialized successfully")

                # Initialize creation service
                logger.info("Initializing experiment creation service...")
                self.creation_service = ExperimentCreationService(self.db_service, self.storage_service, self.config)
                logger.info("Experiment creation service initialized successfully")

                # Start consumer for experiment configuration messages
                topic_name = self.config.kafka.topics["experiment_config"]
                logger.info(f"Starting consumer for topic: {topic_name}")
                await self.kafka_service.start_consumer(topic_name, self._handle_experiment_config)
                logger.info(f"Consumer started successfully for topic: {topic_name}")

                logger.info("Workflow consumer initialized with Kafka support")
            else:
                logger.warning("Kafka disabled, workflow consumer not started")

        except Exception as e:
            logger.error(f"Failed to initialize workflow consumer: {e}")
            import traceback

            logger.error(f"Traceback: {traceback.format_exc()}")
            raise

    async def cleanup(self):
        """Cleanup resources"""
        if self.kafka_service:
            await self.kafka_service.cleanup()
        if self.storage_service:
            # Storage service doesn't have cleanup method currently
            pass
        if self.db_service:
            await self.db_service.cleanup()

    async def _handle_experiment_config(self, message: Dict[str, Any], key: str, topic: str):
        """Handle experiment configuration message and create experiment files"""
        experiment_id = message.get("experiment_id")
        config_data = message.get("config", {})

        if not experiment_id:
            logger.error("Received experiment config message without experiment_id")
            return

        try:
            logger.info(f"Processing experiment config for {experiment_id}")

            # Get experiment from database
            experiment = await self.db_service.get_experiment(experiment_id)
            if not experiment:
                logger.error(f"Experiment {experiment_id} not found in database")
                return

            # Update experiment status to "initializing"
            await self.db_service.update_experiment_status(
                experiment_id, "initializing", started_at=datetime.now(tz=self.config.timezone)
            )

            # Create task for file creation
            task = await self.db_service.create_task(experiment_id, "create_experiment_files")

            # Store task data in result field instead of config
            await self.db_service.update_task(
                str(task.id), result={"experiment_config": config_data, "data_path": experiment.data_path}
            )

            # Update task status to "running"
            await self.db_service.update_task(
                str(task.id), status="running", started_at=datetime.now(tz=self.config.timezone)
            )

            # Create experiment files
            storage_path = await self.creation_service.create_experiment_files(
                experiment_id, config_data, experiment.data_path
            )

            if storage_path:
                # File creation successful
                await self.db_service.update_task(
                    str(task.id),
                    status="completed",
                    completed_at=datetime.now(tz=self.config.timezone),
                    result={"storage_path": storage_path},
                )

                # Update experiment's data_path with storage base path for runner deployment
                await self.db_service.update_experiment(experiment_id, data_path=storage_path)

                # Update experiment status
                await self.db_service.update_experiment_status(experiment_id, "prepared")

                # Publish experiment prepared event
                await self._publish_experiment_prepared(experiment_id, storage_path)

                logger.info(f"Successfully processed experiment config for {experiment_id}")

            else:
                # File creation failed
                await self.db_service.update_task(
                    str(task.id),
                    status="failed",
                    completed_at=datetime.now(tz=self.config.timezone),
                    error_message="Failed to create experiment files",
                )

                await self.db_service.update_experiment_status(
                    experiment_id, "failed", error_message="Failed to create experiment files"
                )

                logger.error(f"Failed to create experiment files for {experiment_id}")

        except Exception as e:
            logger.error(f"Error processing experiment config for {experiment_id}: {e}")

            # Update experiment status to failed
            try:
                await self.db_service.update_experiment_status(experiment_id, "failed", error_message=str(e))
            except Exception as db_error:
                logger.error(f"Failed to update experiment status: {db_error}")

    async def _publish_experiment_prepared(self, experiment_id: str, storage_path: str):
        """Publish experiment prepared event to Kafka"""
        if not self.kafka_service:
            logger.debug("Kafka not available, skipping experiment prepared message")
            return

        try:
            message = {
                "experiment_id": experiment_id,
                "storage_path": storage_path,
                "status": "prepared",
                "timestamp": datetime.now(tz=self.config.timezone).isoformat(),
            }

            await self.kafka_service.send_message(
                self.config.kafka.topics["experiment_prepared"], message, key=experiment_id
            )

            logger.info(f"Published experiment prepared event for {experiment_id}")

        except Exception as e:
            logger.error(f"Failed to publish experiment prepared event for {experiment_id}: {e}")

    async def handle_runner_deployment(self, experiment_id: str, runner_id: str) -> bool:
        """Handle deployment of experiment files to a specific runner"""
        try:
            logger.info(f"Deploying experiment {experiment_id} to runner {runner_id}")

            # Get experiment details
            experiment = await self.db_service.get_experiment(experiment_id)
            if not experiment:
                logger.error(f"Experiment {experiment_id} not found")
                return False

            # Get the storage base path for experiment files
            # (Now stored in the experiment's data_path after file creation)
            storage_base_path = experiment.data_path
            if not storage_base_path:
                logger.error(f"No files available for experiment {experiment_id}")
                return False

            # Create deployment task
            task = await self.db_service.create_task(experiment_id, "deploy_to_runner")

            # Store deployment data in result field
            await self.db_service.update_task(
                str(task.id), result={"runner_id": runner_id, "storage_base_path": storage_base_path}
            )

            # Update task status
            await self.db_service.update_task(
                str(task.id), status="running", started_at=datetime.now(tz=self.config.timezone)
            )

            # For folder structure, we need to provide the base path for the runner to download
            # The runner will need to handle downloading multiple files from the folder
            deployment_info = {
                "experiment_id": experiment_id,
                "storage_base_path": storage_base_path,
                "source": "master_api",
                "is_folder_structure": True,
            }

            # Send deployment request to runner API with folder info instead of single download URL
            success = await self._send_deployment_request(runner_id, deployment_info)

            if success:
                # Update task status
                await self.db_service.update_task(
                    str(task.id),
                    status="completed",
                    completed_at=datetime.now(tz=self.config.timezone),
                    result={"runner_id": runner_id, "storage_base_path": storage_base_path},
                )

                # Update experiment status
                await self.db_service.update_experiment_status(experiment_id, "deployed")

                logger.info(f"Successfully deployed experiment {experiment_id} to runner {runner_id}")
                return True
            else:
                await self.db_service.update_task(
                    str(task.id),
                    status="failed",
                    completed_at=datetime.now(tz=self.config.timezone),
                    error_message="Failed to deploy to runner",
                )
                return False

        except Exception as e:
            logger.error(f"Error deploying experiment {experiment_id} to runner {runner_id}: {e}")
            return False

    async def _send_deployment_request(self, runner_id: str, deployment_info: dict) -> bool:
        """Send deployment request to runner API"""
        try:
            import httpx

            # Get runner instance details
            runner = await self.db_service.get_runner(runner_id)
            if not runner:
                logger.error(f"Runner {runner_id} not found in database")
                return False

            # Prepare deployment request
            deployment_url = f"{runner.endpoint_url}/api/v1/experiments/deploy"

            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(deployment_url, json=deployment_info)

                if response.status_code == 200:
                    logger.info(f"Deployment request successful for runner {runner_id}")
                    return True
                else:
                    logger.error(f"Deployment request failed for runner {runner_id}: {response.status_code}")
                    logger.error(f"Response: {response.text}")
                    return False

        except Exception as e:
            logger.error(f"Error sending deployment request to runner {runner_id}: {e}")
            return False
