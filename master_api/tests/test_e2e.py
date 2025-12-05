#!/usr/bin/env python3
"""
End-to-End Tests for Master API

This module tests the Master API by making actual HTTP requests to the running containers,
simulating how the WebUI would interact with the APIs.
"""

import asyncio
from datetime import datetime
from typing import Any, Dict

import httpx
import pytest
from loguru import logger


class TestConfig:
    """Test configuration for API endpoints"""

    MASTER_API_BASE_URL = "http://localhost:8000"
    RUNNER_API_BASE_URL = "http://localhost:8001"
    REQUEST_TIMEOUT = 30.0


class E2ETestClient:
    """HTTP client for end-to-end testing"""

    def __init__(self):
        self.master_client = httpx.AsyncClient(
            base_url=TestConfig.MASTER_API_BASE_URL, timeout=TestConfig.REQUEST_TIMEOUT
        )
        self.runner_client = httpx.AsyncClient(
            base_url=TestConfig.RUNNER_API_BASE_URL, timeout=TestConfig.REQUEST_TIMEOUT
        )

    async def close(self):
        """Close HTTP clients"""
        try:
            await self.master_client.aclose()
        except Exception:
            pass  # Ignore cleanup errors
        try:
            await self.runner_client.aclose()
        except Exception:
            pass  # Ignore cleanup errors


class TestMasterAPIE2E:
    """End-to-end tests for Master API"""

    @pytest.fixture(scope="function")
    async def client(self):
        """HTTP client fixture"""
        client = E2ETestClient()
        try:
            yield client
        finally:
            await client.close()

    @pytest.fixture(scope="class", autouse=True)
    async def ensure_services_ready(self):
        """Ensure all services are ready before running tests"""
        client = E2ETestClient()

        # Wait for services to be ready
        max_retries = 30
        for i in range(max_retries):
            try:
                # Test Master API health
                master_response = await client.master_client.get("/health")
                if master_response.status_code == 200:
                    logger.info("Master API is ready")

                    # Test Runner API health
                    runner_response = await client.runner_client.get("/health")
                    if runner_response.status_code == 200:
                        logger.info("Runner API is ready")
                        break
            except Exception:
                if i == max_retries - 1:
                    raise
                logger.warning(f"Services not ready yet, retrying... ({i + 1}/{max_retries})")
                await asyncio.sleep(2)

        await client.close()

    async def create_experiment_sample(self) -> Dict[str, Any]:
        """Create a sample experiment configuration"""
        return {
            "name": f"E2E Test Experiment {datetime.now().strftime('%H:%M:%S')}",
            "config": {
                "description": "Test experiment for end-to-end validation",
                "parameters": {"learning_rate": 0.001, "batch_size": 32, "epochs": 10},
                "llm_model": "local-inference",
                "max_iterations": 100,
                "timeout_seconds": 3600,
            },
            "data_path": "/tmp/e2e_test_data.csv",
        }

    @pytest.mark.asyncio
    async def test_master_api_health_endpoint(self, client: E2ETestClient):
        """Test Master API health endpoint"""
        response = await client.master_client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["service"] == "master-api"
        assert "kafka" in data
        logger.info("✅ Master API health endpoint test passed")

    @pytest.mark.asyncio
    async def test_runner_api_health_endpoint(self, client: E2ETestClient):
        """Test Runner API health endpoint"""
        response = await client.runner_client.get("/health")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["service"] == "GigaEvo Platform Runner API"
        logger.info("✅ Runner API health endpoint test passed")

    @pytest.mark.asyncio
    async def test_create_experiment(self, client: E2ETestClient):
        """Test experiment creation"""
        experiment_data = await self.create_experiment_sample()

        response = await client.master_client.post("/api/v1/experiments/", json=experiment_data)

        assert response.status_code == 200
        experiment = response.json()

        # Verify experiment structure
        assert "id" in experiment
        assert experiment["name"] == experiment_data["name"]
        assert experiment["status"] == "pending"
        assert experiment["data_path"] == experiment_data["data_path"]
        assert "created_at" in experiment
        assert "config" in experiment

        logger.info(f"✅ Created experiment with ID: {experiment['id']}")
        return experiment

    @pytest.mark.asyncio
    async def test_list_experiments_empty(self, client: E2ETestClient):
        """Test listing experiments when empty"""
        response = await client.master_client.get("/api/v1/experiments/")

        assert response.status_code == 200
        experiments = response.json()
        assert isinstance(experiments, list)

        # Should return empty list or experiments created in previous tests
        logger.info(f"✅ Listed {len(experiments)} experiments")

    @pytest.mark.asyncio
    async def test_list_experiments_with_data(self, client: E2ETestClient):
        """Test listing experiments with data"""
        # First create an experiment
        created_experiment = await self.test_create_experiment(client)

        # Then list experiments
        response = await client.master_client.get("/api/v1/experiments/")

        assert response.status_code == 200
        experiments = response.json()
        assert isinstance(experiments, list)
        assert len(experiments) >= 1

        # Find our created experiment in the list
        found_experiment = None
        for exp in experiments:
            if exp["id"] == created_experiment["id"]:
                found_experiment = exp
                break

        assert found_experiment is not None
        assert found_experiment["name"] == created_experiment["name"]

        logger.info("✅ Experiment listing with data test passed")

    @pytest.mark.asyncio
    async def test_get_experiment_by_id(self, client: E2ETestClient):
        """Test getting a specific experiment by ID"""
        # Create an experiment first
        created_experiment = await self.test_create_experiment(client)
        experiment_id = created_experiment["id"]

        # Get the experiment by ID
        response = await client.master_client.get(f"/api/v1/experiments/{experiment_id}")

        assert response.status_code == 200
        experiment = response.json()

        assert experiment["id"] == experiment_id
        assert experiment["name"] == created_experiment["name"]
        assert experiment["status"] == created_experiment["status"]

        logger.info(f"✅ Retrieved experiment {experiment_id}")
        return experiment

    @pytest.mark.asyncio
    async def test_get_experiment_status(self, client: E2ETestClient):
        """Test getting experiment status"""
        # Create an experiment first
        created_experiment = await self.test_create_experiment(client)
        experiment_id = created_experiment["id"]

        # Get experiment status
        response = await client.master_client.get(f"/api/v1/experiments/{experiment_id}/status")

        assert response.status_code == 200
        status = response.json()

        assert "experiment_id" in status
        assert status["experiment_id"] == experiment_id
        assert "status" in status
        assert "updated_at" in status

        logger.info(f"✅ Retrieved status for experiment {experiment_id}")

    @pytest.mark.asyncio
    async def test_start_experiment(self, client: E2ETestClient):
        """Test starting an experiment"""
        # Create an experiment first
        created_experiment = await self.test_create_experiment(client)
        experiment_id = created_experiment["id"]

        # Start the experiment
        response = await client.master_client.post(f"/api/v1/experiments/{experiment_id}/start")

        assert response.status_code == 200
        result = response.json()

        assert "message" in result
        assert "experiment_id" in result
        assert result["experiment_id"] == experiment_id

        logger.info(f"✅ Started experiment {experiment_id}")
        return experiment_id

    @pytest.mark.asyncio
    async def test_stop_experiment(self, client: E2ETestClient):
        """Test stopping an experiment"""
        # Create and start an experiment
        experiment_id = await self.test_start_experiment(client)

        # Give it a moment to start
        await asyncio.sleep(1)

        # Stop the experiment
        response = await client.master_client.post(f"/api/v1/experiments/{experiment_id}/stop")

        assert response.status_code == 200
        result = response.json()

        assert "message" in result
        assert "experiment_id" in result
        assert result["experiment_id"] == experiment_id

        logger.info(f"✅ Stopped experiment {experiment_id}")

    @pytest.mark.asyncio
    async def test_experiment_lifecycle(self, client: E2ETestClient):
        """Test complete experiment lifecycle"""
        # 1. Create experiment
        created_experiment = await self.test_create_experiment(client)
        experiment_id = created_experiment["id"]

        # 2. Get experiment details
        experiment_details = await self.test_get_experiment_by_id(client)
        assert experiment_details["status"] == "pending"

        # 3. Start experiment
        await self.test_start_experiment(client)

        # 4. Check status after starting
        await asyncio.sleep(1)
        status_response = await client.master_client.get(f"/api/v1/experiments/{experiment_id}/status")
        assert status_response.status_code == 200

        # 5. Stop experiment
        await self.test_stop_experiment(client)

        # 6. Verify final status
        final_status_response = await client.master_client.get(f"/api/v1/experiments/{experiment_id}/status")
        assert final_status_response.status_code == 200

        logger.info(f"✅ Completed experiment lifecycle test for {experiment_id}")

    @pytest.mark.asyncio
    async def test_experiment_with_custom_config(self, client: E2ETestClient):
        """Test creating experiment with custom configuration"""
        custom_config = {
            "name": "Custom Config Test Experiment",
            "config": {
                "description": "Testing custom configuration",
                "parameters": {
                    "learning_rate": 0.01,
                    "batch_size": 64,
                    "epochs": 20,
                    "optimizer": "adam",
                    "loss_function": "categorical_crossentropy",
                },
                "llm_model": "gpt-4",
                "max_iterations": 200,
                "timeout_seconds": 7200,
            },
            "data_path": "/tmp/custom_test_data.csv",
        }

        response = await client.master_client.post("/api/v1/experiments/", json=custom_config)

        assert response.status_code == 200
        experiment = response.json()

        # Verify custom configuration was saved
        assert experiment["name"] == custom_config["name"]
        assert experiment["config"]["parameters"]["learning_rate"] == 0.01
        assert experiment["config"]["parameters"]["batch_size"] == 64
        assert experiment["config"]["llm_model"] == "gpt-4"

        logger.info(f"✅ Created experiment with custom config: {experiment['id']}")

    @pytest.mark.asyncio
    async def test_experiment_results(self, client: E2ETestClient):
        """Test getting experiment results"""
        # Create an experiment
        created_experiment = await self.test_create_experiment(client)
        experiment_id = created_experiment["id"]

        # Try to get results (should return 404 for non-completed experiments)
        response = await client.master_client.get(f"/api/v1/experiments/{experiment_id}/results")

        assert response.status_code == 404

        # Results endpoint returns 404 for non-completed experiments, which is expected
        logger.info(f"✅ Correctly got 404 for results of non-completed experiment {experiment_id}")

    @pytest.mark.asyncio
    async def test_api_error_handling(self, client: E2ETestClient):
        """Test API error handling"""
        # Test getting non-existent experiment
        response = await client.master_client.get("/api/v1/experiments/non-existent-id")
        assert response.status_code == 404

        # Test stopping non-existent experiment
        response = await client.master_client.post("/api/v1/experiments/non-existent-id/stop")
        assert response.status_code == 400

        # Test starting non-existent experiment
        response = await client.master_client.post("/api/v1/experiments/non-existent-id/start")
        assert response.status_code == 400

        logger.info("✅ API error handling test passed")

    @pytest.mark.asyncio
    async def test_multiple_experiments(self, client: E2ETestClient):
        """Test creating and managing multiple experiments"""
        experiments = []

        # Create multiple experiments
        for i in range(3):
            experiment_data = await self.create_experiment_sample()
            experiment_data["name"] = f"E2E Multi Test Experiment {i + 1}"

            response = await client.master_client.post("/api/v1/experiments/", json=experiment_data)
            assert response.status_code == 200
            experiments.append(response.json())

        # List experiments should contain all created experiments
        list_response = await client.master_client.get("/api/v1/experiments/")
        assert list_response.status_code == 200
        listed_experiments = list_response.json()

        # All our experiments should be in the list
        created_ids = {exp["id"] for exp in experiments}
        listed_ids = {exp["id"] for exp in listed_experiments}
        assert created_ids.issubset(listed_ids)

        logger.info(f"✅ Successfully created and managed {len(experiments)} experiments")

    @pytest.mark.asyncio
    async def test_status_endpoints(self, client: E2ETestClient):
        """Test status-related endpoints"""
        # Test overall system status
        response = await client.master_client.get("/api/v1/status/health")
        assert response.status_code == 200
        system_health = response.json()
        assert "status" in system_health
        assert "timestamp" in system_health
        assert "version" in system_health
        assert "components" in system_health

        # Test experiments status
        response = await client.master_client.get("/api/v1/status/experiments")
        assert response.status_code == 200

        # Test runners status
        response = await client.master_client.get("/api/v1/status/runners")
        assert response.status_code == 200

        logger.info("✅ Status endpoints test passed")


if __name__ == "__main__":
    # Run tests directly

    async def run_tests():
        print("🚀 Running E2E Tests for Master API...")

        # Simple test to verify connectivity
        client = E2ETestClient()
        try:
            response = await client.master_client.get("/health")
            if response.status_code == 200:
                print("✅ Master API is accessible")
            else:
                print(f"❌ Master API returned status {response.status_code}")
                return
        except Exception as e:
            print(f"❌ Failed to connect to Master API: {e}")
            return

        await client.close()
        print("📋 Run tests with: python -m pytest test_e2e.py -v")

    asyncio.run(run_tests())
