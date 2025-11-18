#!/usr/bin/env python3
import asyncio
from datetime import datetime
from typing import Any, Dict, Optional

from loguru import logger

from ..config import load_config
from ..models.experiment import ExperimentStatus
from ..services.database_service import DatabaseService
from ..services.kafka_service import KafkaService


class RunnerDeploymentConsumer:
    """Consumer for deploying experiment files to runner API instances"""

    def __init__(self, config=None):
        self.config = config or load_config()
        self.db_service: DatabaseService
        self.kafka_service: KafkaService
        self.workflow_consumer = None  # Reference to workflow consumer for deployment

    async def initialize(self, workflow_consumer=None):
        """Initialize the deployment consumer"""
        try:
            # Initialize database service
            self.db_service = DatabaseService(self.config)
            await self.db_service.initialize()

            # Store reference to workflow consumer for deployment operations
            self.workflow_consumer = workflow_consumer

            # Initialize Kafka service
            if self.config.kafka.enabled:
                self.kafka_service = KafkaService(self.config)
                await self.kafka_service.initialize()

                # Start consumer for experiment prepared messages
                await self.kafka_service.start_consumer(
                    self.config.kafka.topics["experiment_prepared"], self._handle_experiment_prepared
                )

                logger.info("Runner deployment consumer initialized with Kafka support")
            else:
                logger.warning("Kafka disabled, runner deployment consumer not started")

        except Exception as e:
            logger.error(f"Failed to initialize runner deployment consumer: {e}")
            raise

    async def cleanup(self):
        """Cleanup resources"""
        if self.kafka_service:
            await self.kafka_service.cleanup()
        if self.db_service:
            await self.db_service.cleanup()

    async def _handle_experiment_prepared(self, message: Dict[str, Any], key: str, topic: str):
        """Handle experiment prepared event and deploy to available runner"""
        experiment_id = message.get("experiment_id")
        _storage_path = message.get("storage_path")
        status = message.get("status")

        if not experiment_id:
            logger.error("Received experiment prepared message without experiment_id")
            return

        if status != "prepared":
            logger.debug(f"Ignoring experiment {experiment_id} with status {status}")
            return

        try:
            logger.info(f"Processing experiment prepared for {experiment_id}")

            # Get experiment from database
            experiment = await self.db_service.get_experiment(experiment_id)
            if not experiment:
                logger.error(f"Experiment {experiment_id} not found in database")
                return

            # Find available runner
            runner = await self._find_available_runner()
            if not runner:
                logger.warning(f"No available runners for experiment {experiment_id}, will retry later")
                # Schedule retry
                await self._schedule_deployment_retry(experiment_id)
                return

            logger.info(f"Selected runner {runner.runner_id} for experiment {experiment_id}")

            # Assign runner to experiment
            await self.db_service.assign_runner(experiment_id, runner.runner_id)

            # Deploy experiment to runner
            success = await self._deploy_experiment_to_runner(experiment_id, runner.runner_id)

            if success:
                logger.info(f"Successfully deployed experiment {experiment_id} to runner {runner.runner_id}")
                await self._publish_experiment_started(experiment_id, runner.runner_id)
            else:
                logger.error(f"Failed to deploy experiment {experiment_id} to runner {runner.runner_id}")
                await self._handle_deployment_failure(experiment_id, runner.runner_id)

        except Exception as e:
            logger.error(f"Error processing experiment prepared for {experiment_id}: {e}")

    async def _find_available_runner(self) -> Optional[Dict[str, Any]]:
        """Find an available runner instance"""
        try:
            # Get online runners
            runners = await self.db_service.list_runners(status="online")

            if not runners:
                logger.warning("No online runners available")
                return None

            # Find runners without current experiments
            available_runners = [r for r in runners if r.current_experiment_id is None]

            if not available_runners:
                logger.warning("All online runners are busy")
                return None

            # Select the first available runner (could implement load balancing here)
            runner = available_runners[0]
            return {
                "runner_id": runner.runner_id,
                "endpoint_url": runner.endpoint_url,
                "capabilities": runner.capabilities,
                "resources": runner.resources,
            }

        except Exception as e:
            logger.error(f"Error finding available runner: {e}")
            return None

    async def _deploy_experiment_to_runner(self, experiment_id: str, runner_id: str) -> bool:
        """Deploy experiment to specific runner"""
        try:
            if self.workflow_consumer:
                return await self.workflow_consumer.handle_runner_deployment(experiment_id, runner_id)
            else:
                logger.error("Workflow consumer not available for deployment")
                return False

        except Exception as e:
            logger.error(f"Error deploying experiment {experiment_id} to runner {runner_id}: {e}")
            return False

    async def _schedule_deployment_retry(self, experiment_id: str):
        """Schedule retry for deployment when runners become available"""
        try:
            # Create retry task
            task = await self.db_service.create_task(experiment_id, "retry_deployment")

            # Store retry count in result field
            await self.db_service.update_task(str(task.id), result={"retry_count": 0})

            # Schedule retry after 30 seconds
            asyncio.create_task(self._retry_deployment(experiment_id, str(task.id), delay=30))

            logger.info(f"Scheduled deployment retry for experiment {experiment_id}")

        except Exception as e:
            logger.error(f"Error scheduling deployment retry for {experiment_id}: {e}")

    async def _retry_deployment(self, experiment_id: str, task_id: str, delay: int = 30):
        """Retry deployment after delay"""
        try:
            await asyncio.sleep(delay)

            # Get task details
            task = await self.db_service.get_tasks_for_experiment(experiment_id, "retry_deployment")
            if not task:
                return

            retry_task = task[0]  # Get the retry task
            retry_count = retry_task.result.get("retry_count", 0) if retry_task.result else 0

            if retry_count >= 5:  # Max 5 retries
                logger.error(f"Max retries exceeded for experiment {experiment_id}")
                await self.db_service.update_task(
                    str(retry_task.id), status="failed", error_message="Max retries exceeded"
                )
                await self.db_service.update_experiment_status(
                    experiment_id, "failed", error_message="No available runners after multiple retries"
                )
                return

            # Update task status
            await self.db_service.update_task(str(retry_task.id), status="running")

            # Try to find available runner again
            runner = await self._find_available_runner()
            if runner:
                logger.info(f"Found runner {runner.runner_id} for retry deployment of {experiment_id}")

                # Assign runner and deploy
                await self.db_service.assign_runner(experiment_id, runner.runner_id)
                success = await self._deploy_experiment_to_runner(experiment_id, runner.runner_id)

                if success:
                    await self.db_service.update_task(
                        str(retry_task.id), status="completed", result={"runner_id": runner.runner_id}
                    )
                    await self._publish_experiment_started(experiment_id, runner.runner_id)
                    return

            # If still no runner available, schedule another retry
            retry_result = {"retry_count": retry_count + 1}
            await self.db_service.update_task(
                str(retry_task.id), status=ExperimentStatus.PENDING.value, result=retry_result
            )

            # Exponential backoff
            next_delay = delay * 2
            asyncio.create_task(self._retry_deployment(experiment_id, str(retry_task.id), next_delay))

        except Exception as e:
            logger.error(f"Error in deployment retry for {experiment_id}: {e}")

    async def _publish_experiment_started(self, experiment_id: str, runner_id: str):
        """Publish experiment started event to Kafka"""
        if not self.kafka_service:
            logger.debug("Kafka not available, skipping experiment started message")
            return

        try:
            message = {
                "experiment_id": experiment_id,
                "runner_id": runner_id,
                "status": "started",
                "timestamp": datetime.now(tz=self.config.timezone).isoformat(),
            }

            await self.kafka_service.send_message(
                self.config.kafka.topics["experiment_started"], message, key=experiment_id
            )

            logger.info(f"Published experiment started event for {experiment_id}")

        except Exception as e:
            logger.error(f"Failed to publish experiment started event for {experiment_id}: {e}")

    async def _handle_deployment_failure(self, experiment_id: str, runner_id: str):
        """Handle deployment failure"""
        try:
            # Update experiment status to failed
            await self.db_service.update_experiment_status(
                experiment_id, "failed", error_message=f"Failed to deploy to runner {runner_id}"
            )

            # Clear runner assignment
            experiment = await self.db_service.get_experiment(experiment_id)
            if experiment and experiment.config.get("assigned_runner_id"):
                updated_config = experiment.config.copy()
                del updated_config["assigned_runner_id"]
                await self.db_service.update_experiment(experiment_id, config=updated_config)

            logger.error(f"Deployment failed for experiment {experiment_id}")

        except Exception as e:
            logger.error(f"Error handling deployment failure for {experiment_id}: {e}")

    # Manual deployment methods for API endpoints
    async def manually_deploy_experiment(
        self,
        experiment_id: str,
        runner_id: Optional[str] = None,
    ) -> bool:
        """Manually deploy experiment to specific or available runner"""
        try:
            experiment = await self.db_service.get_experiment(experiment_id)
            if not experiment:
                logger.error(f"Experiment {experiment_id} not found")
                return False

            # Use specified runner or find available one
            if runner_id:
                runner = await self.db_service.get_runner(runner_id)
                if not runner:
                    logger.error(f"Runner {runner_id} not found")
                    return False
                runner_dict = {"runner_id": runner.runner_id, "endpoint_url": runner.endpoint_url}
            else:
                runner_dict = await self._find_available_runner()
                if not runner_dict:
                    logger.error("No available runners")
                    return False
                runner_id = runner_dict["runner_id"]

            # Assign runner and deploy
            await self.db_service.assign_runner(experiment_id, runner_id)
            success = await self._deploy_experiment_to_runner(experiment_id, runner_id)

            if success:
                await self._publish_experiment_started(experiment_id, runner_id)

            return success

        except Exception as e:
            logger.error(f"Error in manual deployment of experiment {experiment_id}: {e}")
            return False
