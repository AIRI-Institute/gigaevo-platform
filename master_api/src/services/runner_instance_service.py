#!/usr/bin/env python3

import asyncio
from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx
from loguru import logger

from ..config import RunnerInstanceConfig, load_config
from ..models.database import RunnerInstanceModel
from ..models.instance import RunnerInstance, RunnerInstanceStatus
from .database_service import DatabaseService
from .docker_service import DockerService


class RunnerInstanceService:
    """Service for managing RunnerAPI instances via Docker"""

    def __init__(self, db_service: DatabaseService, config=None):
        self.config = config or load_config()
        self.db_service = db_service
        self.docker_services: Dict[str, DockerService] = {}
        self._http_client: Optional[httpx.AsyncClient] = None
        self._health_check_task: Optional[asyncio.Task] = None

    async def initialize(self):
        """Initialize HTTP client and Docker services"""
        self._http_client = httpx.AsyncClient(timeout=30.0)

        # Initialize Docker services for each configured instance
        for instance_id, instance_config in self.config.runner.instances.items():
            docker_service = DockerService(instance_config)
            self.docker_services[instance_id] = docker_service
            logger.info(f"Initialized Docker service for instance: {instance_id}")

        # Pre-register all configured instances in the database
        await self._register_configured_instances()

        # Start health monitoring task
        if self.config.runner.auto_initialize:
            self._health_check_task = asyncio.create_task(self._health_monitoring_loop())

        logger.info("Runner instance service initialized")

    async def cleanup(self):
        """Cleanup resources"""
        if self._health_check_task:
            self._health_check_task.cancel()
            try:
                await self._health_check_task
            except asyncio.CancelledError:
                pass

        if self._http_client:
            await self._http_client.aclose()

        logger.info("Runner instance service cleaned up")

    async def initialize_all_instances(self) -> Dict[str, bool]:
        """Initialize all configured RunnerAPI instances"""
        results = {}

        for instance_id, instance_config in self.config.runner.instances.items():
            logger.info(f"Initializing RunnerAPI instance: {instance_id}")
            results[instance_id] = await self.initialize_instance(instance_id)

        return results

    async def initialize_instance(self, instance_id: str) -> bool:
        """Initialize a specific RunnerAPI instance"""
        try:
            if instance_id not in self.docker_services:
                logger.error(f"Docker service not found for instance: {instance_id}")
                return False

            docker_service = self.docker_services[instance_id]
            instance_config = self.config.runner.instances[instance_id]

            logger.info(f"Starting Docker container for {instance_id} on {instance_config.host}")

            # Prepare environment variables for the container
            env_vars = {
                "DATABASE__URL": self.config.database.url,
                "STORAGE__ENDPOINT_URL": self.config.storage.endpoint_url,
                "STORAGE__ACCESS_KEY": self.config.storage.access_key,
                "STORAGE__SECRET_KEY": self.config.storage.secret_key,
                "REDIS_URL": self.config.redis_url,
                "HOST": "0.0.0.0",
                "PORT": "8001",
            }

            # Add Kafka config if enabled
            if self.config.kafka.enabled:
                env_vars.update(
                    {
                        "KAFKA__ENABLED": "true",
                        "KAFKA__BOOTSTRAP_SERVERS": self.config.kafka.bootstrap_servers,
                        "KAFKA__GROUP_ID": f"geml-runner-{instance_id}",
                    }
                )

            # Prepare volumes (shared data directory)
            volumes = {
                "/tmp/geml-data": "/app/data",
                "/tmp/gigavolve": "/tmp/gigavolve",
            }

            # Start the container
            success = await docker_service.initialize_container(
                image_name=self.config.runner.image_name,
                network_name=self.config.runner.network_name,
                environment_vars=env_vars,
                volumes=volumes,
            )

            if success:
                # Wait for the container to be healthy
                await self._wait_for_container_health(docker_service, timeout=60)

                # Register the instance in the database
                await self._register_instance_in_db(instance_id, instance_config, docker_service)

                logger.info(f"Successfully initialized RunnerAPI instance: {instance_id}")
                return True
            else:
                logger.error(f"Failed to initialize RunnerAPI instance: {instance_id}")
                return False

        except Exception as e:
            logger.error(f"Error initializing instance {instance_id}: {e}")
            return False

    async def stop_instance(self, instance_id: str) -> bool:
        """Stop a specific RunnerAPI instance"""
        try:
            if instance_id not in self.docker_services:
                logger.error(f"Docker service not found for instance: {instance_id}")
                return False

            docker_service = self.docker_services[instance_id]

            # Stop the container
            success = await docker_service.stop_container()

            # Update database status
            await self._update_instance_status(instance_id, RunnerInstanceStatus.OFFLINE)

            if success:
                logger.info(f"Successfully stopped RunnerAPI instance: {instance_id}")
            else:
                logger.error(f"Failed to stop RunnerAPI instance: {instance_id}")

            return success

        except Exception as e:
            logger.error(f"Error stopping instance {instance_id}: {e}")
            return False

    async def restart_instance(self, instance_id: str) -> bool:
        """Restart a specific RunnerAPI instance"""
        try:
            # Stop the instance first
            await self.stop_instance(instance_id)

            # Wait a bit for cleanup
            await asyncio.sleep(5)

            # Start it again
            return await self.initialize_instance(instance_id)

        except Exception as e:
            logger.error(f"Error restarting instance {instance_id}: {e}")
            return False

    async def get_available_instance(self) -> Optional[RunnerInstance]:
        """Get an available (ready) instance for experiment deployment"""
        try:
            runners = await self.list_instances(RunnerInstanceStatus.READY)
            if runners:
                return runners[0]
            return None

        except Exception as e:
            logger.error(f"Error getting available instance: {e}")
            return None

    async def list_instances(self, status: Optional[RunnerInstanceStatus] = None) -> List[RunnerInstance]:
        """List all runner instances with optional status filter"""
        try:
            # Get instances from database
            status_str = status.value if status else None
            runner_models = await self.db_service.list_runners(status_str)
            logger.debug(f"Found {len(runner_models)} runner models in database")

            # Convert to API models and update with live status
            instances = []
            for model in runner_models:
                logger.debug(f"Processing runner model: {model.id}, status: {model.status}")
                instance = self._model_to_api(model)

                # Update live status from Docker
                instance_id = self._get_instance_id_by_endpoint(instance.endpoint_url)
                if instance_id and instance_id in self.docker_services:
                    docker_service = self.docker_services[instance_id]
                    is_healthy = await docker_service.check_container_health()

                    # Update status based on health check
                    if not is_healthy and instance.status != RunnerInstanceStatus.OFFLINE:
                        await self._update_instance_status(instance_id, RunnerInstanceStatus.ERROR)
                        instance.status = RunnerInstanceStatus.ERROR
                        instance.error_message = "Health check failed"
                    elif is_healthy and instance.status == RunnerInstanceStatus.ERROR:
                        await self._update_instance_status(instance_id, RunnerInstanceStatus.READY)
                        instance.status = RunnerInstanceStatus.READY
                        instance.error_message = None

                instances.append(instance)

            return instances

        except Exception as e:
            logger.error(f"Failed to list instances: {e}")
            raise

    async def get_instance(self, instance_id: str) -> Optional[RunnerInstance]:
        """Get instance by ID"""
        try:
            runner_model = await self.db_service.get_runner(instance_id)
            if not runner_model:
                return None
            return self._model_to_api(runner_model)

        except Exception as e:
            logger.error(f"Failed to get instance {instance_id}: {e}")
            raise

    async def deploy_experiment_to_instance(
        self, instance_id: str, experiment_id: str, experiment_data: Dict[str, Any]
    ) -> bool:
        """Deploy experiment to specific RunnerAPI instance"""
        try:
            instance = await self.get_instance(instance_id)
            if not instance:
                logger.error(f"Runner instance {instance_id} not found")
                return False

            if instance.status != RunnerInstanceStatus.READY:
                logger.error(f"Runner instance {instance_id} not ready (status: {instance.status})")
                return False

            logger.info(f"Deploying experiment {experiment_id} to instance {instance_id}")

            # Send experiment data to RunnerAPI
            upload_url = f"{instance.endpoint_url}/api/v1/experiments/{experiment_id}/upload"

            if self._http_client:
                response = await self._http_client.post(upload_url, json=experiment_data)

                if response.status_code == 200:
                    # Update instance status to busy
                    await self._update_instance_status(instance_id, RunnerInstanceStatus.BUSY)
                    await self._assign_experiment_to_instance(instance_id, experiment_id)

                    logger.info(f"Successfully deployed experiment {experiment_id} to instance {instance_id}")
                    return True
                else:
                    logger.error(
                        f"Failed to deploy experiment {experiment_id} to instance {instance_id}: {response.status_code}"
                    )
                    return False
            else:
                logger.error("HTTP client not available")
                return False

        except Exception as e:
            logger.error(f"Error deploying experiment {experiment_id} to instance {instance_id}: {e}")
            return False

    async def release_experiment_from_instance(self, instance_id: str) -> bool:
        """Release experiment from RunnerAPI instance"""
        try:
            # Update instance status to ready
            await self._update_instance_status(instance_id, RunnerInstanceStatus.READY)
            await self._release_experiment_from_instance(instance_id)

            logger.info(f"Released experiment from instance {instance_id}")
            return True

        except Exception as e:
            logger.error(f"Error releasing experiment from instance {instance_id}: {e}")
            return False

    async def get_instance_logs(self, instance_id: str, lines: int = 50) -> str:
        """Get logs from a specific instance"""
        try:
            if instance_id not in self.docker_services:
                return f"Docker service not found for instance: {instance_id}"

            docker_service = self.docker_services[instance_id]
            return await docker_service.get_container_logs(lines)

        except Exception as e:
            logger.error(f"Error getting logs for instance {instance_id}: {e}")
            return f"Error retrieving logs: {e}"

    async def _wait_for_container_health(self, docker_service: DockerService, timeout: int = 60) -> bool:
        """Wait for container to become healthy"""
        start_time = datetime.now()

        while (datetime.now() - start_time).total_seconds() < timeout:
            if await docker_service.check_container_health():
                logger.info("Container is healthy and responding")
                return True

            await asyncio.sleep(2)

        logger.error("Container failed to become healthy within timeout")
        return False

    async def _register_instance_in_db(
        self, instance_id: str, config: RunnerInstanceConfig, docker_service: DockerService
    ):
        """Register the instance in the database"""
        try:
            await self.db_service.create_or_update_runner(
                runner_id=instance_id,
                endpoint_url=docker_service.endpoint_url,
                status=RunnerInstanceStatus.READY.value,
                capabilities={"docker": True, "max_workers": self.config.runner.max_workers_per_instance},
                resources={"cpu": 2, "memory": "4GB", "disk": "20GB"},
                name=f"Runner Instance {instance_id}",
                last_heartbeat=datetime.utcnow(),
            )
        except Exception as e:
            logger.error(f"Failed to register instance {instance_id} in database: {e}")

    async def _update_instance_status(self, instance_id: str, status: RunnerInstanceStatus):
        """Update instance status in database"""
        try:
            runner_model = await self.db_service.get_runner(instance_id)
            if runner_model:
                await self.db_service.create_or_update_runner(
                    runner_id=instance_id,
                    endpoint_url=runner_model.endpoint_url,
                    status=status.value,
                    last_heartbeat=datetime.utcnow(),
                )
        except Exception as e:
            logger.error(f"Failed to update status for instance {instance_id}: {e}")

    async def _assign_experiment_to_instance(self, instance_id: str, experiment_id: str):
        """Assign experiment to instance in database"""
        try:
            runner_model = await self.db_service.get_runner(instance_id)
            if runner_model:
                await self.db_service.create_or_update_runner(
                    runner_id=instance_id,
                    endpoint_url=runner_model.endpoint_url,
                    status=RunnerInstanceStatus.BUSY.value,
                    current_experiment_id=experiment_id,
                    last_heartbeat=datetime.utcnow(),
                )
        except Exception as e:
            logger.error(f"Failed to assign experiment {experiment_id} to instance {instance_id}: {e}")

    async def _release_experiment_from_instance(self, instance_id: str):
        """Release experiment from instance in database"""
        try:
            runner_model = await self.db_service.get_runner(instance_id)
            if runner_model:
                await self.db_service.create_or_update_runner(
                    runner_id=instance_id,
                    endpoint_url=runner_model.endpoint_url,
                    status=RunnerInstanceStatus.READY.value,
                    current_experiment_id=None,
                    last_heartbeat=datetime.utcnow(),
                )
        except Exception as e:
            logger.error(f"Failed to release experiment from instance {instance_id}: {e}")

    async def _register_configured_instances(self):
        """Register all configured instances in the database"""
        logger.info(
            f"Registering configured instances in database... Found {len(self.config.runner.instances)} instances"
        )

        for instance_id, instance_config in self.config.runner.instances.items():
            try:
                docker_service = self.docker_services[instance_id]
                logger.info(
                    f"Processing instance {instance_id}: endpoint={docker_service.endpoint_url}, is_local={instance_config.is_local}"
                )

                # Check if instance already exists
                existing_instance = await self.db_service.get_runner(instance_id)
                logger.info(f"Instance {instance_id} exists in DB: {existing_instance is not None}")

                if not existing_instance:
                    # Register new instance with offline status
                    logger.info(f"Creating new instance {instance_id} in database...")
                    _ = await self.db_service.create_or_update_runner(
                        runner_id=instance_id,
                        endpoint_url=docker_service.endpoint_url,
                        status=RunnerInstanceStatus.OFFLINE.value,
                        capabilities={"docker": True, "max_workers": self.config.runner.max_workers_per_instance},
                        resources={"cpu": 2, "memory": "4GB", "disk": "20GB"},
                        name=f"Runner Instance {instance_id}",
                        last_heartbeat=datetime.utcnow(),  # Use UTC without timezone for now
                    )
                    logger.info(f"✓ Successfully registered new instance {instance_id} in database (offline)")
                else:
                    # Update existing instance with current config
                    logger.info(f"Updating existing instance {instance_id} in database...")
                    _ = await self.db_service.create_or_update_runner(
                        runner_id=instance_id,
                        endpoint_url=docker_service.endpoint_url,
                        capabilities={"docker": True, "max_workers": self.config.runner.max_workers_per_instance},
                        resources={"cpu": 2, "memory": "4GB", "disk": "20GB"},
                        name=f"Runner Instance {instance_id}",
                        last_heartbeat=datetime.utcnow(),  # Use UTC without timezone for now
                    )
                    logger.info(f"✓ Successfully updated existing instance {instance_id} in database")

            except Exception as e:
                logger.error(f"✗ Failed to register instance {instance_id}: {e}")
                import traceback

                logger.error(f"Full traceback: {traceback.format_exc()}")

        # Verify registration by trying to list all instances
        try:
            all_instances = await self.db_service.list_runners()
            logger.info(f"✓ Total instances in database after registration: {len(all_instances)}")
            for inst in all_instances:
                logger.info(f"  - {inst.id}: {inst.status} at {inst.endpoint_url}")
        except Exception as e:
            logger.error(f"Failed to verify instance registration: {e}")

    def _get_instance_id_by_endpoint(self, endpoint_url: str) -> Optional[str]:
        """Get instance ID from endpoint URL"""
        for instance_id, docker_service in self.docker_services.items():
            if docker_service.endpoint_url == endpoint_url:
                return instance_id
        return None

    async def _health_monitoring_loop(self):
        """Background task for monitoring instance health"""
        logger.info("Starting instance health monitoring loop")

        while True:
            try:
                for instance_id, docker_service in self.docker_services.items():
                    try:
                        is_healthy = await docker_service.check_container_health()

                        # Update database status based on health check
                        runner_model = await self.db_service.get_runner(instance_id)
                        if runner_model:
                            if not is_healthy:
                                if runner_model.status != RunnerInstanceStatus.OFFLINE.value:
                                    await self._update_instance_status(instance_id, RunnerInstanceStatus.ERROR)
                            elif is_healthy:
                                if runner_model.status in [
                                    RunnerInstanceStatus.OFFLINE.value,
                                    RunnerInstanceStatus.ERROR.value,
                                ]:
                                    await self._update_instance_status(instance_id, RunnerInstanceStatus.READY)
                                    logger.info(
                                        f"Updated instance {instance_id} status from {runner_model.status} to READY"
                                    )
                    except Exception as e:
                        logger.debug(f"Health check failed for instance {instance_id}: {e}")

                # Wait for next check
                await asyncio.sleep(self.config.runner.health_check_interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in health monitoring loop: {e}")
                await asyncio.sleep(30)  # Wait before retrying

    def _model_to_api(self, model: RunnerInstanceModel) -> RunnerInstance:
        """Convert database model to API model"""
        return RunnerInstance(
            id=model.id,
            name=model.name,
            endpoint_url=model.endpoint_url,
            status=RunnerInstanceStatus(model.status),
            capabilities=model.capabilities,
            resources=model.resources,
            last_heartbeat=model.last_heartbeat,
            current_experiment_id=model.current_experiment_id,
            created_at=model.created_at,
        )
