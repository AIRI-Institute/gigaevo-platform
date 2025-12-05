"""Service for managing runner instances via Master API."""

from typing import Any, Dict, List, Optional

import requests
from config.settings import DEFAULT_TIMEOUTS, MASTER_API_URL
from loguru import logger


class InstanceManager:
    """Handles all runner instance operations through the Master API."""

    def __init__(self, base_url: Optional[str] = None):
        """Initialize the instance manager.

        Args:
            base_url: Base URL for the Master API. Falls back to environment variable.
        """
        self.base_url = base_url or MASTER_API_URL
        self.timeout = DEFAULT_TIMEOUTS["api_request"]

    def list_instances(self) -> List[Dict[str, Any]]:
        """List all runner instances.

        Returns:
            List of instance dictionaries
        """
        try:
            response = requests.get(f"{self.base_url}/api/v1/instances/", timeout=self.timeout)
            response.raise_for_status()
            return response.json()

        except requests.RequestException as e:
            logger.error(f"Failed to list instances: {e}")
            return []

    def get_instance(self, instance_id: str) -> Optional[Dict[str, Any]]:
        """Get instance details by ID.

        Args:
            instance_id: ID of the instance

        Returns:
            Instance dictionary or None if not found
        """
        try:
            response = requests.get(f"{self.base_url}/api/v1/instances/{instance_id}", timeout=self.timeout)
            response.raise_for_status()
            return response.json()

        except requests.RequestException as e:
            logger.error(f"Failed to get instance {instance_id}: {e}")
            return None

    def initialize_instance(self, instance_id: str) -> Dict[str, Any]:
        """Initialize a runner instance.

        Args:
            instance_id: ID of the instance

        Returns:
            Result dictionary
        """
        try:
            response = requests.post(f"{self.base_url}/api/v1/instances/{instance_id}/initialize", timeout=self.timeout)
            response.raise_for_status()
            return response.json()

        except requests.RequestException as e:
            logger.error(f"Failed to initialize instance {instance_id}: {e}")
            return {"error": str(e)}

    def initialize_all_instances(self) -> Dict[str, Any]:
        """Initialize all configured runner instances.

        Returns:
            Result dictionary with summary
        """
        try:
            response = requests.post(f"{self.base_url}/api/v1/instances/initialize-all", timeout=self.timeout)
            response.raise_for_status()
            return response.json()

        except requests.RequestException as e:
            logger.error(f"Failed to initialize all instances: {e}")
            return {"error": str(e)}

    def stop_instance(self, instance_id: str) -> Dict[str, Any]:
        """Stop a runner instance.

        Args:
            instance_id: ID of the instance

        Returns:
            Result dictionary
        """
        try:
            response = requests.post(f"{self.base_url}/api/v1/instances/{instance_id}/stop", timeout=self.timeout)
            response.raise_for_status()
            return response.json()

        except requests.RequestException as e:
            logger.error(f"Failed to stop instance {instance_id}: {e}")
            return {"error": str(e)}

    def restart_instance(self, instance_id: str) -> Dict[str, Any]:
        """Restart a runner instance.

        Args:
            instance_id: ID of the instance

        Returns:
            Result dictionary
        """
        try:
            response = requests.post(f"{self.base_url}/api/v1/instances/{instance_id}/restart", timeout=self.timeout)
            response.raise_for_status()
            return response.json()

        except requests.RequestException as e:
            logger.error(f"Failed to restart instance {instance_id}: {e}")
            return {"error": str(e)}

    def get_instance_logs(self, instance_id: str, lines: int = 50) -> Dict[str, Any]:
        """Get logs from a specific runner instance.

        Args:
            instance_id: ID of the instance
            lines: Number of log lines to retrieve

        Returns:
            Result dictionary containing logs
        """
        try:
            response = requests.get(
                f"{self.base_url}/api/v1/instances/{instance_id}/logs?lines={lines}", timeout=self.timeout
            )
            response.raise_for_status()
            return response.json()

        except requests.RequestException as e:
            logger.error(f"Failed to get logs for instance {instance_id}: {e}")
            return {"error": str(e)}

    def get_available_instance(self) -> Optional[Dict[str, Any]]:
        """Get an available instance for experiment deployment.

        Returns:
            Instance dictionary or None if no available instance
        """
        try:
            response = requests.get(f"{self.base_url}/api/v1/instances/available/instance", timeout=self.timeout)
            response.raise_for_status()
            return response.json()

        except requests.RequestException as e:
            logger.error(f"Failed to get available instance: {e}")
            return None

    def get_health_summary(self) -> Dict[str, Any]:
        """Get health summary of all runner instances.

        Returns:
            Health summary dictionary
        """
        try:
            response = requests.get(f"{self.base_url}/api/v1/instances/health/summary", timeout=self.timeout)
            response.raise_for_status()
            return response.json()

        except requests.RequestException as e:
            logger.error(f"Failed to get health summary: {e}")
            return {"error": str(e)}
