#!/usr/bin/env python3

from typing import Optional

from loguru import logger

from ..config import load_config
from .database_service import DatabaseService
from .experiment_creation_service import ExperimentCreationService
from .experiment_service import ExperimentService
from .kafka_service import KafkaService
from .runner_deployment_consumer import RunnerDeploymentConsumer
from .runner_instance_service import RunnerInstanceService
from .status_service import StatusService
from .storage_service import StorageService
from .workflow_consumer import WorkflowConsumer


class ServiceManager:
    """Centralized service lifecycle and dependency management"""

    def __init__(self, config=None):
        self.config = config or load_config()

        # Core infrastructure services
        self.db_service: Optional[DatabaseService] = None
        self.kafka_service: Optional[KafkaService] = None
        self.storage_service: Optional[StorageService] = None

        # Business logic services
        self.experiment_service: Optional[ExperimentService] = None
        self.instance_service: Optional[RunnerInstanceService] = None
        self.creation_service: Optional[ExperimentCreationService] = None
        self.status_service: Optional[StatusService] = None

        # Consumer services
        self.workflow_consumer: Optional[WorkflowConsumer] = None
        self.deployment_consumer: Optional[RunnerDeploymentConsumer] = None

        self._initialized = False

    async def initialize(self):
        """Initialize all services in dependency order"""
        if self._initialized:
            logger.warning("ServiceManager already initialized")
            return

        try:
            logger.info("Initializing ServiceManager...")

            # Initialize core infrastructure first
            await self._initialize_infrastructure()

            # Initialize business services
            await self._initialize_business_services()

            # Initialize consumers if Kafka is enabled
            if self.config.kafka.enabled:
                await self._initialize_consumers()
            else:
                logger.info("Kafka disabled - skipping consumer initialization")

            # Initialize experiment service after consumers are available
            await self._initialize_experiment_service()

            self._initialized = True
            logger.info("ServiceManager initialization completed successfully")

        except Exception as e:
            logger.error(f"ServiceManager initialization failed: {e}")
            await self.cleanup()
            raise

    async def _initialize_infrastructure(self):
        """Initialize core infrastructure services"""
        logger.info("Initializing infrastructure services...")

        # Database service
        self.db_service = DatabaseService(self.config)
        await self.db_service.initialize()
        logger.info("Database service initialized")

        # Storage service
        self.storage_service = StorageService(self.config)
        await self.storage_service.initialize()
        logger.info("Storage service initialized")

        # Kafka service (if enabled)
        if self.config.kafka.enabled:
            self.kafka_service = KafkaService(self.config)
            await self.kafka_service.initialize()
            logger.info("Kafka service initialized")

    async def _initialize_business_services(self):
        """Initialize business logic services"""
        logger.info("Initializing business services...")

        if not self.db_service or not self.storage_service:
            raise RuntimeError("Database and storage services must be initialized first")

        # Experiment creation service
        self.creation_service = ExperimentCreationService(self.db_service, self.storage_service, self.config)

        # Instance service
        self.instance_service = RunnerInstanceService(self.db_service, self.config)
        await self.instance_service.initialize()

        # Initialize RunnerAPI instances if auto-initialization is enabled
        if self.config.runner.auto_initialize:
            logger.info("Auto-initializing RunnerAPI instances...")
            init_results = await self.instance_service.initialize_all_instances()

            successful_count = sum(1 for success in init_results.values() if success)
            total_count = len(init_results)

            logger.info(f"Initialized {successful_count}/{total_count} RunnerAPI instances")
            for instance_id, success in init_results.items():
                if success:
                    logger.info(f"✓ {instance_id}: initialized successfully")
                else:
                    logger.error(f"✗ {instance_id}: initialization failed")

        # Status service
        self.status_service = StatusService(service_manager=self)
        logger.info("Business services initialized")

    async def _initialize_experiment_service(self):
        """Initialize experiment service after consumers are ready"""
        logger.info("Initializing experiment service...")

        if not self.db_service or not self.storage_service:
            raise RuntimeError("Database and storage services must be initialized first")

        # Experiment service (main service)
        self.experiment_service = ExperimentService(
            db_service=self.db_service,
            storage_service=self.storage_service,
            kafka_service=self.kafka_service,
            workflow_consumer=self.workflow_consumer,
            deployment_consumer=self.deployment_consumer,
            instance_service=self.instance_service,
            config=self.config,
        )
        await self.experiment_service.initialize()
        logger.info("Experiment service initialized")

    async def _initialize_consumers(self):
        """Initialize Kafka consumer services"""
        logger.info(f"Kafka enabled: {self.config.kafka.enabled}")
        logger.info(f"Kafka service available: {self.kafka_service is not None}")

        if not self.config.kafka.enabled:
            logger.info("Kafka disabled - skipping consumer initialization")
            return

        if not self.kafka_service:
            logger.warning("Cannot initialize consumers - Kafka service not available")
            return

        logger.info("Initializing consumer services...")

        # Workflow consumer
        try:
            logger.info("Creating workflow consumer...")
            self.workflow_consumer = WorkflowConsumer(self.config)
            logger.info("Workflow consumer created, initializing...")
            await self.workflow_consumer.initialize()
            logger.info("Workflow consumer initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize workflow consumer: {e}")
            import traceback

            logger.error(f"Workflow consumer traceback: {traceback.format_exc()}")
            raise

        # Deployment consumer
        try:
            logger.info("Creating deployment consumer...")
            self.deployment_consumer = RunnerDeploymentConsumer(self.config)
            logger.info("Deployment consumer created, initializing...")
            await self.deployment_consumer.initialize(self.workflow_consumer)
            logger.info("Deployment consumer initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize deployment consumer: {e}")
            import traceback

            logger.error(f"Deployment consumer traceback: {traceback.format_exc()}")
            raise

    async def cleanup(self):
        """Cleanup all services in reverse order"""
        logger.info("Starting ServiceManager cleanup...")

        # Cleanup consumers first
        if self.deployment_consumer:
            try:
                await self.deployment_consumer.cleanup()
                logger.info("Deployment consumer cleaned up")
            except Exception as e:
                logger.error(f"Error cleaning up deployment consumer: {e}")

        if self.workflow_consumer:
            try:
                await self.workflow_consumer.cleanup()
                logger.info("Workflow consumer cleaned up")
            except Exception as e:
                logger.error(f"Error cleaning up workflow consumer: {e}")

        # Cleanup business services
        if self.experiment_service:
            try:
                await self.experiment_service.cleanup()
                logger.info("Experiment service cleaned up")
            except Exception as e:
                logger.error(f"Error cleaning up experiment service: {e}")

        # Cleanup infrastructure
        if self.kafka_service:
            try:
                await self.kafka_service.cleanup()
                logger.info("Kafka service cleaned up")
            except Exception as e:
                logger.error(f"Error cleaning up kafka service: {e}")

        if self.storage_service:
            try:
                # Storage service doesn't have cleanup method currently
                pass
                logger.info("Storage service cleaned up")
            except Exception as e:
                logger.error(f"Error cleaning up storage service: {e}")

        if self.db_service:
            try:
                await self.db_service.cleanup()
                logger.info("Database service cleaned up")
            except Exception as e:
                logger.error(f"Error cleaning up database service: {e}")

        self._initialized = False
        logger.info("ServiceManager cleanup completed")

    def health_check(self) -> dict:
        """Get health status of all services"""
        health_status = {"service_manager": "healthy" if self._initialized else "uninitialized", "services": {}}

        # Check infrastructure services
        if self.db_service:
            health_status["services"]["database"] = "configured"
        else:
            health_status["services"]["database"] = "not_initialized"

        if self.kafka_service:
            health_status["services"]["kafka"] = "configured"
        else:
            health_status["services"]["kafka"] = "disabled" if not self.config.kafka.enabled else "not_initialized"

        if self.storage_service:
            health_status["services"]["storage"] = "configured"
        else:
            health_status["services"]["storage"] = "not_initialized"

        # Check business services
        health_status["services"]["experiment_service"] = "configured" if self.experiment_service else "not_initialized"
        health_status["services"]["instance_service"] = "configured" if self.instance_service else "not_initialized"
        health_status["services"]["creation_service"] = "configured" if self.creation_service else "not_initialized"
        health_status["services"]["status_service"] = "configured" if self.status_service else "not_initialized"

        # Check consumers
        if self.config.kafka.enabled:
            health_status["services"]["workflow_consumer"] = (
                "configured" if self.workflow_consumer else "not_initialized"
            )
            health_status["services"]["deployment_consumer"] = (
                "configured" if self.deployment_consumer else "not_initialized"
            )

        return health_status

    # Service accessors
    def get_experiment_service(self) -> ExperimentService:
        """Get experiment service instance"""
        if not self.experiment_service:
            raise RuntimeError("Experiment service not initialized")
        return self.experiment_service

    def get_instance_service(self) -> RunnerInstanceService:
        """Get instance service instance"""
        if not self.instance_service:
            raise RuntimeError("Instance service not initialized")
        return self.instance_service

    def get_db_service(self) -> DatabaseService:
        """Get database service instance"""
        if not self.db_service:
            raise RuntimeError("Database service not initialized")
        return self.db_service

    def get_storage_service(self) -> StorageService:
        """Get storage service instance"""
        if not self.storage_service:
            raise RuntimeError("Storage service not initialized")
        return self.storage_service

    def get_status_service(self) -> StatusService:
        """Get status service instance"""
        if not self.status_service:
            raise RuntimeError("Status service not initialized")
        return self.status_service

    def is_healthy(self) -> bool:
        """Check if all critical services are healthy"""
        if not self._initialized:
            return False

        critical_services = [self.db_service, self.experiment_service]
        return all(service is not None for service in critical_services)
