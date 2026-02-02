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
        self._reconciler_task: Optional[asyncio.Task] = None
        # Track consecutive 404s per (runner_id, experiment_id) to avoid premature releases.
        self._missing_status_404_counts: Dict[tuple[str, str], int] = {}

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

        # Start health monitoring task (always ON for pool orchestration)
        if getattr(self.config.runner, "health_monitoring_enabled", True):
            self._health_check_task = asyncio.create_task(self._health_monitoring_loop())

        # Periodic reconciliation to release BUSY runners on terminal outcomes
        self._reconciler_task = asyncio.create_task(self._reconcile_busy_runners_loop())

        logger.info("Runner instance service initialized")

    async def cleanup(self):
        """Cleanup resources"""
        if self._health_check_task:
            self._health_check_task.cancel()
            try:
                await self._health_check_task
            except asyncio.CancelledError:
                pass

        if self._reconciler_task:
            self._reconciler_task.cancel()
            try:
                await self._reconciler_task
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

            manage_containers = getattr(self.config.runner, "manage_containers", True)
            logger.info(
                f"Initializing runner instance {instance_id} (host={instance_config.host}, manage_containers={manage_containers})"
            )

            if not manage_containers:
                # Compose-managed mode: containers already exist; we can only start/restart/stop them.
                success = await docker_service.ensure_compose_service_running()
                if not success:
                    logger.error(f"Failed to start Compose-managed RunnerAPI service for instance: {instance_id}")
                    return False

                await self._wait_for_container_health(docker_service, timeout=60)
                responding, repo_ready = await docker_service.get_health_status()
                derived_status = (
                    self._derive_status_from_health(repo_ready) if responding else RunnerInstanceStatus.ERROR
                )
                await self._register_instance_in_db(instance_id, instance_config, docker_service, derived_status)
                logger.info(f"Successfully initialized Compose-managed RunnerAPI instance: {instance_id}")
                return True

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
                        "KAFKA__GROUP_ID": f"gigaevo-runner-{instance_id}",
                    }
                )

            # Prepare volumes (shared data directory)
            volumes = {
                "/tmp/gigaevo-data": "/app/data",
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

                responding, repo_ready = await docker_service.get_health_status()
                if responding:
                    derived_status = self._derive_status_from_health(repo_ready)
                else:
                    derived_status = RunnerInstanceStatus.ERROR

                # Register the instance in the database
                await self._register_instance_in_db(instance_id, instance_config, docker_service, derived_status)

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

            manage_containers = getattr(self.config.runner, "manage_containers", True)
            if manage_containers:
                # Master-managed mode: stop and remove the container.
                success = await docker_service.stop_container()
            else:
                # Compose-managed mode: stop the existing container (do not remove).
                success = await docker_service.stop_compose_service()

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
            manage_containers = getattr(self.config.runner, "manage_containers", True)
            if not manage_containers:
                if instance_id not in self.docker_services:
                    logger.error(f"Docker service not found for instance: {instance_id}")
                    return False

                docker_service = self.docker_services[instance_id]
                instance_config = self.config.runner.instances[instance_id]

                success = await docker_service.restart_compose_service()
                if not success:
                    logger.error(f"Failed to restart Compose-managed RunnerAPI instance: {instance_id}")
                    await self._update_instance_status(instance_id, RunnerInstanceStatus.ERROR)
                    return False

                await self._wait_for_container_health(docker_service, timeout=60)
                responding, repo_ready = await docker_service.get_health_status()
                derived_status = (
                    self._derive_status_from_health(repo_ready) if responding else RunnerInstanceStatus.ERROR
                )
                await self._register_instance_in_db(instance_id, instance_config, docker_service, derived_status)
                logger.info(f"Successfully restarted Compose-managed RunnerAPI instance: {instance_id}")
                return True

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

    async def allocate_runner(self, experiment_id: str) -> Optional[RunnerInstance]:
        """Atomically allocate a READY runner for an experiment (binary capacity)."""
        try:
            model = await self.db_service.allocate_runner_for_experiment(experiment_id)
            if not model:
                return None
            return self._model_to_api(model)
        except Exception as e:
            logger.error(f"Failed to allocate runner for experiment {experiment_id}: {e}")
            return None

    async def allocate_specific_runner(self, runner_id: str, experiment_id: str) -> Optional[RunnerInstance]:
        """Atomically allocate a specific READY runner for an experiment."""
        try:
            model = await self.db_service.allocate_specific_runner_for_experiment(runner_id, experiment_id)
            if not model:
                return None
            return self._model_to_api(model)
        except Exception as e:
            logger.error(f"Failed to allocate runner {runner_id} for experiment {experiment_id}: {e}")
            return None

    async def release_runner_if_assigned(self, experiment_id: str) -> bool:
        """Release the runner back to READY if it is still assigned to this experiment."""
        try:
            return await self.db_service.release_runner_if_assigned(experiment_id)
        except Exception as e:
            logger.error(f"Failed to release runner for experiment {experiment_id}: {e}")
            return False

    async def release_runner_by_id_if_experiment(self, runner_id: str, experiment_id: str) -> bool:
        """Release a specific runner if it is still assigned to this experiment."""
        try:
            return await self.db_service.release_runner_by_id_if_experiment(runner_id, experiment_id)
        except Exception as e:
            logger.error(f"Failed to release runner {runner_id} for experiment {experiment_id}: {e}")
            return False

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
                    responding, repo_ready = await docker_service.get_health_status()

                    # Update status based on health check
                    if not responding and instance.status != RunnerInstanceStatus.OFFLINE:
                        await self._update_instance_status(instance_id, RunnerInstanceStatus.ERROR)
                        instance.status = RunnerInstanceStatus.ERROR
                        instance.error_message = "Health check failed"
                    elif responding and instance.status not in [
                        RunnerInstanceStatus.BUSY,
                        RunnerInstanceStatus.TERMINATING,
                    ]:
                        derived = self._derive_status_from_health(repo_ready)
                        if instance.status != derived:
                            await self._update_instance_status(instance_id, derived)
                            instance.status = derived
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
            manage_containers = getattr(self.config.runner, "manage_containers", True)
            if manage_containers:
                logs = await docker_service.get_named_container_logs(lines)
            else:
                logs = await docker_service.get_compose_service_logs(lines)

            if logs is None:
                return "Error retrieving logs: Docker logs unavailable (is /var/run/docker.sock mounted?)"
            return logs

        except Exception as e:
            logger.error(f"Error getting logs for instance {instance_id}: {e}")
            return f"Error retrieving logs: {e}"

    async def _wait_for_container_health(self, docker_service: DockerService, timeout: int = 60) -> bool:
        """Wait for container to become responsive"""
        start_time = datetime.now()

        while (datetime.now() - start_time).total_seconds() < timeout:
            responding, _ = await docker_service.get_health_status()
            if responding:
                logger.info("Container is responding")
                return True

            await asyncio.sleep(2)

        logger.error("Container failed to become responsive within timeout")
        return False

    async def _register_instance_in_db(
        self,
        instance_id: str,
        config: RunnerInstanceConfig,
        docker_service: DockerService,
        status: RunnerInstanceStatus,
    ):
        """Register the instance in the database"""
        try:
            await self.db_service.create_or_update_runner(
                runner_id=instance_id,
                endpoint_url=docker_service.endpoint_url,
                status=status.value,
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

    def _derive_status_from_health(self, repo_ready: Optional[bool]) -> RunnerInstanceStatus:
        """Map health response to runner instance status."""
        if repo_ready is True:
            return RunnerInstanceStatus.READY
        if repo_ready is False:
            return RunnerInstanceStatus.INITIALIZING
        return RunnerInstanceStatus.ONLINE

    async def _health_monitoring_loop(self):
        """Background task for monitoring instance health"""
        logger.info("Starting instance health monitoring loop")

        while True:
            try:
                for instance_id, docker_service in self.docker_services.items():
                    try:
                        responding, repo_ready = await docker_service.get_health_status()

                        # Update database status based on health check
                        runner_model = await self.db_service.get_runner(instance_id)
                        if runner_model:
                            if not responding:
                                if runner_model.status != RunnerInstanceStatus.OFFLINE.value:
                                    await self._update_instance_status(instance_id, RunnerInstanceStatus.ERROR)
                            else:
                                if runner_model.status not in [
                                    RunnerInstanceStatus.BUSY.value,
                                    RunnerInstanceStatus.TERMINATING.value,
                                ]:
                                    derived = self._derive_status_from_health(repo_ready)
                                    if runner_model.status != derived.value:
                                        await self._update_instance_status(instance_id, derived)
                                        logger.info(
                                            f"Updated instance {instance_id} status from {runner_model.status} to {derived.value.upper()}"
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

    async def _reconcile_busy_runners_loop(self) -> None:
        """Periodically release BUSY runners whose experiments are terminal or missing."""
        interval = int(getattr(self.config.runner, "reconcile_interval_seconds", 15) or 15)
        interval = max(5, interval)
        grace = int(getattr(self.config.runner, "missing_status_grace_seconds", 30) or 30)
        grace = max(0, grace)
        max_404s = int(getattr(self.config.runner, "status_404_release_threshold", 2) or 2)
        max_404s = max(1, max_404s)
        terminal_statuses = {"completed", "failed", "cancelled"}

        logger.info(f"Starting runner pool reconciler loop (interval={interval}s)")
        while True:
            try:
                busy_models = await self.db_service.list_runners(status=RunnerInstanceStatus.BUSY.value)
                if not busy_models:
                    await asyncio.sleep(interval)
                    continue

                for runner in busy_models:
                    exp_id = getattr(runner, "current_experiment_id", None)
                    if not exp_id:
                        # Inconsistent state: BUSY without an experiment -> free it
                        await self.db_service.set_runner_ready(runner.id)
                        continue

                    # Resolve endpoint url
                    runner_endpoint = getattr(runner, "endpoint_url", "").rstrip("/")
                    if not runner_endpoint:
                        continue

                    # Query runner for experiment status; if 404, treat as missing/cleaned -> release.
                    try:
                        if not self._http_client:
                            continue
                        status_url = f"{runner_endpoint}/api/v1/experiments/{exp_id}/status"
                        resp = await self._http_client.get(status_url)
                        if resp.status_code == 404:
                            # Hardening: right after /start the runner may briefly return 404
                            # while its status record is being created. Do not release too early.
                            within_grace = False
                            try:
                                exp = await self.db_service.get_experiment(str(exp_id))
                                started_at = getattr(exp, "started_at", None) if exp else None
                                if started_at and grace > 0:
                                    age = (datetime.now() - started_at).total_seconds()
                                    within_grace = age >= 0 and age < grace
                            except Exception:
                                within_grace = False

                            if within_grace:
                                logger.debug(
                                    f"Runner {runner.id} status=404 for {exp_id}; skipping release (within grace {grace}s)"
                                )
                                continue

                            key = (str(runner.id), str(exp_id))
                            self._missing_status_404_counts[key] = self._missing_status_404_counts.get(key, 0) + 1
                            if self._missing_status_404_counts[key] < max_404s:
                                logger.debug(
                                    f"Runner {runner.id} status=404 for {exp_id}; "
                                    f"not releasing yet ({self._missing_status_404_counts[key]}/{max_404s})"
                                )
                                continue

                            _ = self._missing_status_404_counts.pop(key, None)
                            await self.db_service.release_runner_by_id_if_experiment(runner.id, exp_id)
                            continue
                        resp.raise_for_status()
                        payload = resp.json()
                        st = str(payload.get("status", "")).lower()
                        # Reset 404 counter on any non-404 response
                        _ = self._missing_status_404_counts.pop((str(runner.id), str(exp_id)), None)
                        if st in terminal_statuses:
                            await self.db_service.release_runner_by_id_if_experiment(runner.id, exp_id)
                    except httpx.RequestError:
                        # Runner unreachable: keep state; health monitor will handle ERROR transitions
                        continue
                    except Exception:
                        continue

                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Runner pool reconciler tick error: {e}")
                await asyncio.sleep(interval)

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
