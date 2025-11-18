#!/usr/bin/env python3
"""
Pytest configuration for Master API tests
"""

import asyncio
import logging
import os
import sys

import pytest

# Add the src directory to the Python path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")


@pytest.fixture(scope="session")
def event_loop():
    """Create an event loop for async tests"""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    asyncio.set_event_loop(loop)
    yield loop
    if not loop.is_closed():
        loop.close()


@pytest.fixture(scope="module", autouse=True)
async def ensure_services_ready():
    """Ensure all services are ready before running tests"""
    import httpx

    master_api_url = "http://localhost:8000"
    runner_api_url = "http://localhost:8001"

    max_retries = 30
    for i in range(max_retries):
        try:
            # Test Master API
            async with httpx.AsyncClient(timeout=5.0) as client:
                master_response = await client.get(f"{master_api_url}/health")
                if master_response.status_code == 200:
                    # Test Runner API
                    runner_response = await client.get(f"{runner_api_url}/health")
                    if runner_response.status_code == 200:
                        logging.info("✅ All services are ready for testing")
                        break

        except Exception as e:
            if i == max_retries - 1:
                raise Exception(f"Services not ready after {max_retries} attempts: {e}")
            logging.warning(f"Services not ready yet, retrying... ({i + 1}/{max_retries})")
            await asyncio.sleep(2)
