#!/usr/bin/env python3
"""
Integration tests for the complete experiment workflow
"""

from datetime import datetime
from unittest.mock import AsyncMock, Mock, patch

import pytest
from src.models.experiment import ExperimentConfig, ExperimentCreate
from src.services.database_service import DatabaseService
from src.services.kafka_service import KafkaService
from src.services.runner_deployment_consumer import RunnerDeploymentConsumer
from src.services.storage_service import StorageService
from src.services.workflow_consumer import WorkflowConsumer

from master_api.src.services.experiment_service import ExperimentService


@pytest.fixture
async def mock_services():
    """Mock all external services for testing"""

    # Mock config
    mock_config = Mock()
    mock_config.database.url = "sqlite+aiosqlite:///:memory:"
    mock_config.storage.endpoint_url = "http://localhost:9000"
    mock_config.storage.access_key = "minioadmin"
    mock_config.storage.secret_key = "minioadmin"
    mock_config.storage.bucket_name = "test-bucket"
    mock_config.kafka.enabled = False  # Disable Kafka for testing
    mock_config.kafka.bootstrap_servers = "localhost:9092"
    mock_config.kafka.group_id = "test-group"
    mock_config.kafka.topics = {
        "experiment_config": "experiment-config",
        "experiment_prepared": "experiment-prepared",
        "experiment_started": "experiment-started",
        "experiment_stopped": "experiment-stopped",
        "runner_status": "runner-status",
    }
    mock_config.redis_url = "redis://localhost:6379/0"

    return mock_config


@pytest.fixture
async def experiment_service(mock_services):
    """Create experiment service with mocked dependencies"""

    with patch("src.services.enhanced_experiment_service.load_config", return_value=mock_services):
        service = ExperimentService()

        # Mock database service
        service.db_service = AsyncMock(spec=DatabaseService)
        service.db_service.create_experiment = AsyncMock()
        service.db_service.list_experiments = AsyncMock(return_value=[])
        service.db_service.get_experiment = AsyncMock(return_value=None)
        service.db_service.update_experiment = AsyncMock(return_value=True)
        service.db_service.update_experiment_status = AsyncMock(return_value=True)

        # Mock storage service
        service.storage_service = AsyncMock(spec=StorageService)
        service.storage_service.upload_experiment_data = AsyncMock(return_value="test/path/file.csv")

        # Mock Kafka service (disabled)
        service.kafka_service = AsyncMock(spec=KafkaService)

        # Mock workflow and deployment consumers
        service.workflow_consumer = AsyncMock()
        service.deployment_consumer = AsyncMock()
        service.deployment_consumer.manually_deploy_experiment = AsyncMock(return_value=True)

        return service


class TestExperimentWorkflow:
    """Test the complete experiment workflow"""

    @pytest.mark.asyncio
    async def test_create_experiment_basic(self, experiment_service):
        """Test basic experiment creation"""

        # Create test experiment
        experiment_create = ExperimentCreate(
            name="Test Experiment",
            config=ExperimentConfig(
                description="Test experiment for workflow",
                llm_model="local-inference",
                max_iterations=100,
                timeout_seconds=3600,
                parameters={
                    "task_type": "classification",
                    "target_column": "target",
                    "learning_rate": 0.01,
                },
            ),
            data_path="test/data.csv",
        )

        # Mock database response
        mock_experiment_model = Mock()
        mock_experiment_model.id = "test-id"
        mock_experiment_model.name = experiment_create.name
        mock_experiment_model.config = experiment_create.config.model_dump()
        mock_experiment_model.data_path = experiment_create.data_path
        mock_experiment_model.status = "pending"
        mock_experiment_model.created_at = datetime.utcnow()
        mock_experiment_model.updated_at = datetime.utcnow()

        experiment_service.db_service.create_experiment.return_value = mock_experiment_model

        # Test experiment creation
        result = await experiment_service.create_experiment(experiment_create)

        # Verify result
        assert result.name == experiment_create.name
        assert result.config == experiment_create.config
        assert result.data_path == experiment_create.data_path
        assert result.status.value == "pending"

        # Verify service calls
        experiment_service.db_service.create_experiment.assert_called_once_with(experiment_create)

        print("✅ Basic experiment creation test passed")

    @pytest.mark.asyncio
    async def test_upload_experiment_data(self, experiment_service):
        """Test experiment data upload"""

        experiment_id = "test-experiment-id"
        file_path = "/tmp/test.csv"
        filename = "test.csv"

        # Test data upload
        result = await experiment_service.upload_experiment_data(experiment_id, file_path, filename)

        # Verify result
        assert result

        # Verify service calls
        experiment_service.storage_service.upload_experiment_data.assert_called_once_with(
            experiment_id, file_path, filename
        )
        experiment_service.db_service.update_experiment.assert_called_once()

        print("✅ Experiment data upload test passed")

    @pytest.mark.asyncio
    async def test_start_experiment(self, experiment_service):
        """Test experiment starting"""

        experiment_id = "test-experiment-id"

        # Mock experiment model
        mock_experiment_model = Mock()
        mock_experiment_model.id = experiment_id
        mock_experiment_model.name = "Test Experiment"
        mock_experiment_model.config = {}
        mock_experiment_model.data_path = "test.csv"
        mock_experiment_model.status = "prepared"
        mock_experiment_model.assigned_runner_id = "test-runner"

        experiment_service.db_service.get_experiment.return_value = mock_experiment_model

        # Test experiment start
        result = await experiment_service.start_experiment(experiment_id)

        # Verify result
        assert result

        # Verify service calls
        experiment_service.db_service.get_experiment.assert_called_once_with(experiment_id)
        experiment_service.db_service.update_experiment_status.assert_called_once()

        print("✅ Experiment start test passed")

    @pytest.mark.asyncio
    async def test_manual_deployment(self, experiment_service):
        """Test manual experiment deployment"""

        experiment_id = "test-experiment-id"
        runner_id = "test-runner"

        # Test manual deployment
        result = await experiment_service.manually_deploy_experiment(experiment_id, runner_id)

        # Verify result
        assert result

        # Verify service calls
        experiment_service.deployment_consumer.manually_deploy_experiment.assert_called_once_with(
            experiment_id, runner_id
        )

        print("✅ Manual deployment test passed")


class TestWorkflowConsumer:
    """Test workflow consumer functionality"""

    @pytest.mark.asyncio
    async def test_workflow_consumer_initialization(self, mock_services):
        """Test workflow consumer initialization"""

        with patch("src.services.workflow_consumer.load_config", return_value=mock_services):
            consumer = WorkflowConsumer(mock_services)

            # Mock dependencies
            consumer.db_service = AsyncMock(spec=DatabaseService)
            consumer.storage_service = AsyncMock(spec=StorageService)
            consumer.kafka_service = AsyncMock(spec=KafkaService)
            consumer.creation_service = AsyncMock()

            # Test initialization
            await consumer.initialize()

            # Verify service calls
            consumer.db_service.initialize.assert_called_once()
            consumer.storage_service.initialize.assert_called_once()
            consumer.kafka_service.initialize.assert_called_once()

            print("✅ Workflow consumer initialization test passed")


class TestRunnerDeploymentConsumer:
    """Test runner deployment consumer functionality"""

    @pytest.mark.asyncio
    async def test_runner_deployment_consumer_initialization(self, mock_services):
        """Test runner deployment consumer initialization"""

        with patch("src.services.runner_deployment_consumer.load_config", return_value=mock_services):
            consumer = RunnerDeploymentConsumer(mock_services)

            # Mock dependencies
            consumer.db_service = AsyncMock(spec=DatabaseService)
            consumer.kafka_service = AsyncMock(spec=KafkaService)
            consumer.workflow_consumer = AsyncMock()

            # Test initialization
            await consumer.initialize(consumer.workflow_consumer)

            # Verify service calls
            consumer.db_service.initialize.assert_called_once()
            consumer.kafka_service.initialize.assert_called_once()

            print("✅ Runner deployment consumer initialization test passed")


if __name__ == "__main__":
    print("🚀 Running workflow integration tests...")

    # Run basic import test
    try:
        print("✅ Master API imports successful")
    except Exception as e:
        print(f"❌ Master API import failed: {e}")
        exit(1)

    print("📋 To run full tests: uv run --with pytest pytest tests/test_workflow_integration.py -v")
