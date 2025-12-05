"""Service for managing experiments via Master API."""

from typing import Any, Dict, List, Optional

import requests
from config.settings import DEFAULT_TIMEOUTS, MASTER_API_URL
from loguru import logger


class ExperimentManager:
    """Handles all experiment-related operations through the Master API."""

    def __init__(self, base_url: Optional[str] = None):
        """Initialize the experiment manager.

        Args:
            base_url: Base URL for the Master API. Falls back to environment variable.
        """
        self.base_url = base_url or MASTER_API_URL
        self.timeout = DEFAULT_TIMEOUTS["api_request"]

    def create_experiment(self, name: str, config: Dict[str, Any], data_path: str) -> Dict[str, Any]:
        """Create a new experiment.

        Args:
            name: Name of the experiment
            config: Experiment configuration dictionary
            data_path: Path to the dataset

        Returns:
            Dictionary containing the created experiment details or error
        """
        try:
            # Extend payload with task-specific fields when provided
            payload: Dict[str, Any] = {
                "name": name,
                "config": config,
                "data_path": data_path,
            }

            # Add task-specific parameters from config if present
            params: Dict[str, Any] = config.get("parameters", {}) if isinstance(config, dict) else {}
            if "target_field" in params:
                payload["target_field"] = params["target_field"]
            if "n_classes" in params:
                payload["n_classes"] = params["n_classes"]
            if "n_clusters" in params:
                payload["n_clusters"] = params["n_clusters"]

            response = requests.post(f"{self.base_url}/api/v1/experiments/", json=payload, timeout=self.timeout)
            response.raise_for_status()
            return response.json()

        except requests.RequestException as e:
            logger.error(f"Failed to create experiment: {e}")
            return {"error": str(e)}

    def list_experiments(self) -> List[Dict[str, Any]]:
        """List all experiments.

        Returns:
            List of experiment dictionaries
        """
        try:
            response = requests.get(f"{self.base_url}/api/v1/experiments/", timeout=self.timeout)
            response.raise_for_status()
            return response.json()

        except requests.RequestException as e:
            logger.error(f"Failed to list experiments: {e}")
            return []

    def get_experiment(self, experiment_id: str) -> Optional[Dict[str, Any]]:
        """Get experiment details by ID.

        Args:
            experiment_id: ID of the experiment

        Returns:
            Experiment dictionary or None if not found
        """
        try:
            response = requests.get(f"{self.base_url}/api/v1/experiments/{experiment_id}", timeout=self.timeout)
            response.raise_for_status()
            return response.json()

        except requests.RequestException as e:
            logger.error(f"Failed to get experiment {experiment_id}: {e}")
            return None

    def get_experiment_status(self, experiment_id: str) -> Optional[Dict[str, Any]]:
        """Get experiment status.

        Args:
            experiment_id: ID of the experiment

        Returns:
            Status dictionary or None if not found
        """
        try:
            response = requests.get(f"{self.base_url}/api/v1/experiments/{experiment_id}/status", timeout=self.timeout)
            response.raise_for_status()
            return response.json()

        except requests.RequestException as e:
            logger.error(f"Failed to get experiment status {experiment_id}: {e}")
            return None

    def get_experiment_results(self, experiment_id: str) -> Optional[Dict[str, Any]]:
        """Get experiment results.

        Args:
            experiment_id: ID of the experiment

        Returns:
            Results dictionary or None if not found
        """
        try:
            response = requests.get(
                f"{self.base_url}/api/v1/experiments/{experiment_id}/results",
                timeout=DEFAULT_TIMEOUTS["experiment_results"],
            )
            response.raise_for_status()
            return response.json()

        except requests.RequestException as e:
            logger.error(f"Failed to get experiment results {experiment_id}: {e}")
            return None

    def start_experiment(self, experiment_id: str) -> Dict[str, Any]:
        """Start an experiment.

        Args:
            experiment_id: ID of the experiment

        Returns:
            Result dictionary
        """
        try:
            response = requests.post(f"{self.base_url}/api/v1/experiments/{experiment_id}/start", timeout=self.timeout)
            response.raise_for_status()
            return response.json()

        except requests.RequestException as e:
            logger.error(f"Failed to start experiment {experiment_id}: {e}")
            return {"error": str(e)}

    def stop_experiment(self, experiment_id: str) -> Dict[str, Any]:
        """Stop an experiment.

        Args:
            experiment_id: ID of the experiment

        Returns:
            Result dictionary
        """
        try:
            response = requests.post(f"{self.base_url}/api/v1/experiments/{experiment_id}/stop", timeout=self.timeout)
            response.raise_for_status()
            return response.json()

        except requests.RequestException as e:
            logger.error(f"Failed to stop experiment {experiment_id}: {e}")
            return {"error": str(e)}

    def create_prompt_experiment(self, prompt_experiment_data: Dict[str, Any]) -> Dict[str, Any]:
        """Create a new prompt-based experiment.

        Args:
            prompt_experiment_data: Prompt experiment data matching PromptExperimentCreate schema

        Returns:
            Dictionary containing created experiment details or error
        """
        try:
            response = requests.post(
                f"{self.base_url}/api/v1/experiments/prompts",
                json=prompt_experiment_data,
                timeout=self.timeout
            )
            response.raise_for_status()
            return response.json()

        except requests.RequestException as e:
            logger.error(f"Failed to create prompt experiment: {e}")
            return {"error": str(e)}

    def drop_all_experiments(self) -> Dict[str, Any]:
        """Drop all experiments and their data.

        Returns:
            Result dictionary with deletion summary
        """
        try:
            response = requests.delete(f"{self.base_url}/api/v1/experiments/drop-all", timeout=self.timeout)
            response.raise_for_status()
            return response.json()

        except requests.RequestException as e:
            logger.error(f"Failed to drop all experiments: {e}")
            return {"error": str(e)}

    def upload_data_file(self, file_path: str, filename: str) -> Dict[str, Any]:
        """Upload data file to S3 storage via Master API.

        Args:
            file_path: Local path to the file
            filename: Name of the file

        Returns:
            Upload result dictionary
        """
        try:
            timeout = DEFAULT_TIMEOUTS["file_upload"]
            with open(file_path, "rb") as f:
                files = {"file": (filename, f.read())}
                response = requests.post(f"{self.base_url}/api/v1/experiments/upload", files=files, timeout=timeout)
                response.raise_for_status()
                return response.json()

        except requests.RequestException as e:
            logger.error(f"Failed to upload file {filename}: {e}")
            return {"error": str(e)}
        except OSError as e:
            logger.error(f"Failed to read file {file_path}: {e}")
            return {"error": f"File read error: {e}"}

    # Examples management methods
    def list_examples(self) -> List[Dict[str, str]]:
        """List available example datasets.

        Returns:
            List of example dictionaries
        """
        try:
            logger.info(f"Fetching examples from {self.base_url}/api/v1/examples/")
            response = requests.get(f"{self.base_url}/api/v1/examples/", timeout=self.timeout)
            response.raise_for_status()
            data = response.json()

            examples = data.get("examples", [])
            if "error" in data:
                logger.error(f"API returned error: {data['error']}")
                return []

            logger.info(f"Successfully retrieved {len(examples)} examples")
            return examples

        except requests.exceptions.ConnectionError as e:
            error_msg = f"Cannot connect to Master API at {self.base_url}. Please ensure the service is running: {e}"
            logger.error(error_msg)
            # Return a special error indicator that can be used by UI components
            return [{"name": "CONNECTION_ERROR", "label": f"⚠️ Cannot connect to Master API", "error": str(e)}]

        except requests.exceptions.Timeout as e:
            error_msg = f"Connection timeout to Master API at {self.base_url}: {e}"
            logger.error(error_msg)
            return [{"name": "TIMEOUT_ERROR", "label": f"⏰ API connection timeout", "error": str(e)}]

        except requests.exceptions.HTTPError as e:
            status_code = e.response.status_code if e.response else "unknown"
            error_msg = f"HTTP error {status_code} from Master API: {e}"
            logger.error(error_msg)
            return [{"name": "HTTP_ERROR", "label": f"🔥 HTTP {status_code} error", "error": str(e)}]

        except requests.RequestException as e:
            error_msg = f"Failed to list examples from Master API: {e}"
            logger.error(error_msg)
            return [{"name": "REQUEST_ERROR", "label": f"❌ API request failed", "error": str(e)}]

        except Exception as e:
            error_msg = f"Unexpected error listing examples: {e}"
            logger.error(error_msg)
            return [{"name": "UNEXPECTED_ERROR", "label": f"💥 Unexpected error", "error": str(e)}]

    def test_api_connection(self) -> Dict[str, Any]:
        """Test connectivity to the Master API.

        Returns:
            Dictionary with connection status and details
        """
        try:
            logger.info(f"Testing API connectivity to {self.base_url}")
            response = requests.get(f"{self.base_url}/health", timeout=5)
            response.raise_for_status()

            # Test the examples endpoint specifically
            examples_response = requests.get(f"{self.base_url}/api/v1/examples/", timeout=5)
            examples_response.raise_for_status()
            examples_data = examples_response.json()

            return {
                "status": "success",
                "message": f"✅ Successfully connected to Master API at {self.base_url}",
                "api_url": self.base_url,
                "examples_count": len(examples_data.get("examples", [])),
                "examples": examples_data.get("examples", [])
            }

        except requests.exceptions.ConnectionError as e:
            return {
                "status": "connection_error",
                "message": f"❌ Cannot connect to Master API at {self.base_url}. Make sure the service is running.",
                "api_url": self.base_url,
                "error": str(e),
                "suggestion": "Run 'make dev' to start all services"
            }
        except requests.exceptions.Timeout as e:
            return {
                "status": "timeout_error",
                "message": f"⏰ Connection timeout to Master API at {self.base_url}. Service may be starting up.",
                "api_url": self.base_url,
                "error": str(e),
                "suggestion": "Wait a moment and try again"
            }
        except requests.exceptions.HTTPError as e:
            status_code = e.response.status_code if e.response else "unknown"
            return {
                "status": "http_error",
                "message": f"🔥 HTTP {status_code} error from Master API",
                "api_url": self.base_url,
                "status_code": status_code,
                "error": str(e)
            }
        except Exception as e:
            return {
                "status": "unexpected_error",
                "message": f"💥 Unexpected error testing API connection",
                "api_url": self.base_url,
                "error": str(e)
            }

    def get_example_spec(self, name: str) -> Optional[Dict[str, Any]]:
        """Get example specification by name.

        Args:
            name: Name of the example

        Returns:
            Example specification dictionary or None if not found
        """
        try:
            response = requests.get(f"{self.base_url}/api/v1/examples/{name}", timeout=self.timeout)
            response.raise_for_status()
            return response.json()

        except requests.RequestException as e:
            logger.error(f"Failed to get example spec {name}: {e}")
            return None

    def upload_example_dataset(self, name: str) -> Dict[str, Any]:
        """Upload example dataset to storage.

        Args:
            name: Name of the example dataset

        Returns:
            Upload result dictionary
        """
        try:
            response = requests.post(
                f"{self.base_url}/api/v1/examples/{name}/upload", timeout=DEFAULT_TIMEOUTS["file_upload"]
            )
            response.raise_for_status()
            return response.json()

        except requests.RequestException as e:
            logger.error(f"Failed to upload example dataset {name}: {e}")
            return {"error": str(e)}

    def get_experiment_summary(self, experiment_id: str) -> Dict[str, Any]:
        """Get experiment summary for visualization.

        Args:
            experiment_id: ID of the experiment

        Returns:
            Summary dictionary with key metrics
        """
        try:
            import time

            ts = int(time.time())
            url = f"{self.base_url}/results/{experiment_id}/summary?ts={ts}"
            response = requests.get(url, timeout=self.timeout)

            if response.ok:
                return response.json()
            else:
                # Return empty summary structure
                return {
                    "total_iterations": None,
                    "total_programs": None,
                    "total_programs_complete": None,
                    "best_fitness": None,
                    "best_generations": None,
                }

        except requests.RequestException as e:
            logger.error(f"Failed to get experiment summary {experiment_id}: {e}")
            return {
                "total_iterations": None,
                "total_programs": None,
                "total_programs_complete": None,
                "best_fitness": None,
                "best_generations": None,
            }

    def get_evolution_report(self, experiment_id: str) -> Optional[Dict[str, Any]]:
        """Get evolution report with best program.

        Args:
            experiment_id: ID of the experiment

        Returns:
            Evolution report dictionary or None if not found
        """
        try:
            import time

            ts = int(time.time())
            url = f"{self.base_url}/results/{experiment_id}/evolution_report.json?ts={ts}"
            response = requests.get(url, timeout=self.timeout)

            if response.ok:
                return response.json()
            return None

        except requests.RequestException as e:
            logger.error(f"Failed to get evolution report {experiment_id}: {e}")
            return None

    def get_download_archive(self, experiment_id: str) -> Optional[bytes]:
        """Get downloadable archive for experiment.

        Args:
            experiment_id: ID of the experiment

        Returns:
            Archive content as bytes or None if failed
        """
        try:
            import time

            ts = int(time.time())
            url = f"{self.base_url}/results/{experiment_id}/download.zip?ts={ts}"
            response = requests.get(url, timeout=DEFAULT_TIMEOUTS["file_download"])

            if response.ok and response.content:
                return response.content
            return None

        except requests.RequestException as e:
            logger.error(f"Failed to get download archive {experiment_id}: {e}")
            return None

    # Prompt presets API
    # Remote master_api/prompt_examples-based examples (already added above)
    def list_prompt_examples(self) -> List[Dict[str, str]]:
        """List available prompt evolution examples."""
        try:
            response = requests.get(f"{self.base_url}/api/v1/prompt-examples/", timeout=self.timeout)
            response.raise_for_status()
            data = response.json()
            return data.get("examples", [])
        except requests.RequestException as e:
            logger.error(f"Failed to list prompt examples: {e}")
            return []

    def get_prompt_example_details(self, name: str) -> Optional[Dict[str, Any]]:
        """Get details for a prompt example."""
        try:
            response = requests.get(f"{self.base_url}/api/v1/prompt-examples/{name}", timeout=self.timeout)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            logger.error(f"Failed to get prompt example details {name}: {e}")
            return None

    def create_prompt_experiment_from_example(self, name: str) -> Dict[str, Any]:
        """Create a new prompt experiment from a prompt example preset."""
        try:
            response = requests.post(
                f"{self.base_url}/api/v1/prompt-examples/{name}/create", timeout=DEFAULT_TIMEOUTS["api_request"]
            )
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            logger.error(f"Failed to create prompt experiment from example {name}: {e}")
            return {"error": str(e)}

    # Local prompt presets helpers
    def upload_local_prompt_preset_dataset(self, name: str) -> Dict[str, Any]:
        """Upload train.csv of local prompt preset to storage."""
        try:
            response = requests.post(
                f"{self.base_url}/api/v1/prompt-presets/local/{name}/upload", timeout=DEFAULT_TIMEOUTS["file_upload"]
            )
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            logger.error(f"Failed to upload local prompt preset dataset {name}: {e}")
            return {"error": str(e)}
    # Local prompt presets (master_api/prompt_examples)
    def list_local_prompt_presets(self) -> List[Dict[str, str]]:
        """List local prompt presets discovered by Master API."""
        try:
            response = requests.get(f"{self.base_url}/api/v1/prompt-presets/local/", timeout=self.timeout)
            response.raise_for_status()
            return response.json().get("examples", [])
        except requests.RequestException as e:
            logger.error(f"Failed to list local prompt presets: {e}")
            return []

    def get_local_prompt_preset(self, name: str) -> Optional[Dict[str, Any]]:
        """Get local prompt preset details with baseline_prompt."""
        try:
            response = requests.get(f"{self.base_url}/api/v1/prompt-presets/local/{name}", timeout=self.timeout)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            logger.error(f"Failed to get local prompt preset {name}: {e}")
            return None
