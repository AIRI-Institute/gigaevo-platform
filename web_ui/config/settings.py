"""Settings and configuration constants for the web UI."""

import os
from typing import Dict

# API Configuration
MASTER_API_URL = os.getenv("MASTER_API_URL", "http://localhost:8000")
# Public S3 URL (for links in UI, from host)
S3_API_URL = os.getenv("S3_API_URL", "http://localhost:9000")
# Internal S3 URL (used by web-ui container to download files)
INTERNAL_S3_API_URL = os.getenv("INTERNAL_S3_API_URL", os.getenv("S3_API_URL", "http://minio:9000"))
INPUT_DATA_DIR = os.getenv("INPUT_DATA_DIR", "/app/input_data")
STORAGE_BUCKET_NAME = os.getenv("STORAGE_BUCKET_NAME", "gigaevo-data")

# Default timeouts and limits
DEFAULT_TIMEOUTS = {
    "api_request": 5,
    "file_upload": 15,
    "file_download": 30,
    "experiment_results": 60,
}

DEFAULT_LIMITS = {
    "max_iterations": 1000,
    "max_log_lines": 500,
    "min_log_lines": 10,
    "default_log_lines": 50,
    "refresh_interval": 10,  # seconds
    "status_refresh_interval": 15,  # seconds
    "full_data_refresh_interval": 30,  # seconds
}

# Status color variables for visual blocks
STATUS_COLORS: Dict[str, str] = {
    "healthy": "#13c1acff",  # Green - brand primary
    "degraded": "#f59e0bff",  # Amber - warning
    "unhealthy": "#ef4444ff",  # Red - danger
    "configured": "#8c939cff",  # Gray - brand secondary
    "disabled": "#6b7280ff",  # Gray - muted
    "not_configured": "#6b7280ff",  # Gray - muted
    "no_instances": "#3b82f6ff",  # Blue - info
    "unknown": "#6b7280ff",  # Gray - muted
    "online": "#13c1acff",  # Green - brand primary
    "ready": "#13c1acff",  # Green - brand primary
    "offline": "#ef4444ff",  # Red - danger
    "error": "#ef4444ff",  # Red - danger
    "busy": "#f59e0bff",  # Amber - warning
}

# Default example target suggestions
CLASSIFICATION_TARGET_EXAMPLES = ["label", "target", "species", "churn"]
REGRESSION_TARGET_EXAMPLES = ["price", "age", "income", "target"]


# Task types (UI-level). Internal variants like *_automl or *_catboost
# are derived from this selection plus the model dropdown.
TASK_TYPES = ["classification", "regression", "clustering"]

# File upload configuration
ALLOWED_FILE_TYPES = [".csv", ".json", ".txt"]
MAX_FILE_SIZE = 100 * 1024 * 1024  # 100MB

# UI Configuration
UI_CONFIG = {
    "theme": "default",
    "show_footer": False,
    "analytics_enabled": False,
}

# Data processing configuration
DATA_PROCESSING = {
    "csv_preview_rows": 100,
    "max_dataframe_rows": 10000,
}

# Caching configuration
CACHE_CONFIG = {
    "cache_enabled": True,
    "cache_ttl": 300,  # 5 minutes
    "max_cache_size": 100,
}

# Error messages
ERROR_MESSAGES = {
    "no_experiment_selected": "No experiment selected",
    "experiment_not_found": "Experiment not found",
    "no_instance_selected": "No instance selected",
    "instance_not_found": "Instance not found",
    "api_error": "Error communicating with Master API",
    "file_upload_error": "Error uploading file",
    "invalid_task_type": "Invalid task type selected",
    "missing_required_fields": "Please fill in all required fields",
    "network_error": "Network error. Please check your connection.",
}

# Success messages
SUCCESS_MESSAGES = {
    "experiment_created": "Experiment created successfully",
    "experiment_started": "Experiment started successfully",
    "experiment_stopped": "Experiment stopped successfully",
    "instance_initialized": "Instance initialized successfully",
    "instance_stopped": "Instance stopped successfully",
    "instance_restarted": "Instance restarted successfully",
    "file_uploaded": "File uploaded successfully",
    "data_refreshed": "Data refreshed successfully",
}

# Validation rules
VALIDATION_RULES = {
    "experiment_name": {
        "min_length": 1,
        "max_length": 100,
        "pattern": r"^[a-zA-Z0-9_\-\s]+$",
        "description": "Experiment name can contain letters, numbers, underscores, hyphens, and spaces",
    },
    "max_iterations": {
        "min": 1,
        "max": 1000,
        "description": "Max iterations must be between 1 and 1000",
    },
    "log_lines": {
        "min": 10,
        "max": 500,
        "step": 10,
        "description": "Number of log lines must be between 10 and 500",
    },
    "num_clusters": {
        "min": 2,
        "max": 50,
        "default": 3,
        "description": "Number of clusters must be between 2 and 50",
    },
}
