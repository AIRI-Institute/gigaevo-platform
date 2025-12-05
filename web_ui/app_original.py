#!/usr/bin/env python3

import base64
import io
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import gradio as gr
import matplotlib
import matplotlib.pyplot as plt
import pandas as pd
import requests
from loguru import logger

# Set matplotlib to use non-interactive backend
matplotlib.use("Agg")

# Configuration
MASTER_API_URL = os.getenv("MASTER_API_URL", "http://localhost:8000")
# Public URL used by the browser to fetch assets from Master API
S3_API_URL = os.getenv("S3_API_URL", "http://localhost:9000")
INPUT_DATA_DIR = os.getenv("INPUT_DATA_DIR", "/app/input_data")
STORAGE_BUCKET_NAME = os.getenv("STORAGE_BUCKET_NAME", "gigaevo-data")
_RESOLVED_BUCKET_NAME: Optional[str] = None


def _resolve_bucket_name() -> str:
    """Resolve bucket name from Master API config, fallback to env/default."""
    global _RESOLVED_BUCKET_NAME
    if _RESOLVED_BUCKET_NAME:
        return _RESOLVED_BUCKET_NAME
    try:
        resp = requests.get(f"{MASTER_API_URL}/api/v1/status/storage", timeout=3)
        if resp.ok:
            data = resp.json()
            name = data.get("bucket_name") or STORAGE_BUCKET_NAME
            _RESOLVED_BUCKET_NAME = str(name)
            return _RESOLVED_BUCKET_NAME
    except Exception:
        pass
    _RESOLVED_BUCKET_NAME = STORAGE_BUCKET_NAME
    return _RESOLVED_BUCKET_NAME


# Status color variables for visual blocks
STATUS_COLORS = {
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

# AIRI theme overrides
AIRI_CSS = """
footer {visibility: hidden}

/* Light theme colors */
@media (prefers-color-scheme: light) {
  :root {
    /* Background */
    --body-background-fill: #f2eee8ff;
  }
}

/* Dark theme colors */
@media (prefers-color-scheme: dark) {
  :root {
    /* Background - use default dark theme */
    --body-background-fill: unset;
  }
}

/* Universal brand colors for both themes */
:root {
  /* Primary buttons */
  --button-primary-background-fill: #13c1acff;
  --button-primary-background-fill-hover: #13c1acff;
  --button-primary-text-color: #ffffff;

  /* Secondary buttons */
  --button-secondary-background-fill: #8c939cff;
  --button-secondary-background-fill-hover: #8c939cff;
  --button-secondary-text-color: #ffffff;

  /* Stop/Danger buttons (variant="stop") */
  --button-cancel-background-fill: #e96c6cff;
  --button-cancel-background-fill-hover: #e96c6cff;
  --button-cancel-text-color: #ffffff;
  /* Additional aliases used by some Gradio themes */
  --button-danger-background-fill: #e96c6cff;
  --button-danger-background-fill-hover: #e96c6cff;
  --button-danger-text-color: #ffffff;
  --color-error: #e96c6cff;

  /* Tabs */
  --tab-active-background-fill: #13c1acff;
  --tab-active-text-color: #ffffff;

  /* Global accent for sliders and focused controls */
  --color-accent: #13c1acff;
}

/* Ensure page background follows the brand color, but respect dark mode preference */
@media (prefers-color-scheme: light) {
  body, .gradio-container {
    background: #f2eee8ff !important;
  }
}

/* Active tab fallback (Gradio v4 tabs are buttons with role="tab") */
button[role="tab"][aria-selected="true"] {
  background: #13c1acff !important;
  color: #ffffff !important;
}

/* Sliders: broad support via accent-color */
input[type="range"] {
  accent-color: #13c1acff;
}

/* Preset Buttons Styling - Ensure buttons display in a nice row */
div.gradio-container .gr-row:has(.gr-button) {
  gap: 0.5rem !important;
  flex-wrap: wrap !important;
}

.preset-button {
  min-width: fit-content !important;
  max-width: 200px !important;
}

/* Fallbacks to ensure stop/danger buttons are painted even if variables differ */
button.stop, .gr-button.stop, .gr-button[class*="stop"], button[class*="stop"] {
  background: #e96c6cff !important;
  color: #ffffff !important;
  border-color: transparent !important;
}
button.stop:hover, .gr-button.stop:hover, .gr-button[class*="stop"]:hover, button[class*="stop"]:hover {
  background: #e96c6cff !important;
  color: #ffffff !important;
}

/* Dark theme button color overrides to ensure brand colors persist */
@media (prefers-color-scheme: dark) {
  /* Force primary buttons to maintain brand color in dark theme */
  button.primary, button.btn-primary, .gr-button[variant="primary"] {
    background: #13c1acff !important;
    color: #ffffff !important;
    border-color: transparent !important;
  }

  button.primary:hover, button.btn-primary:hover, .gr-button[variant="primary"]:hover {
    background: #13c1acff !important;
    color: #ffffff !important;
    filter: brightness(0.9);
  }

  /* Force secondary buttons to maintain brand color in dark theme */
  button.secondary, button.btn-secondary, .gr-button[variant="secondary"] {
    background: #8c939cff !important;
    color: #ffffff !important;
    border-color: transparent !important;
  }

  button.secondary:hover, button.btn-secondary:hover, .gr-button[variant="secondary"]:hover {
    background: #8c939cff !important;
    color: #ffffff !important;
    filter: brightness(0.9);
  }

  /* Ensure stop/danger buttons maintain color in dark theme */
  button.stop, .gr-button.cancel, .gr-button.danger,
  button[variant="stop"], button[variant="cancel"], button[variant="danger"] {
    background: #e96c6cff !important;
    color: #ffffff !important;
    border-color: transparent !important;
  }

  .gr-button.stop:hover, .gr-button.cancel:hover, .gr-button.danger:hover,
  button[variant="stop"]:hover, button[variant="cancel"]:hover, button[variant="danger"]:hover {
    background: #e96c6cff !important;
    color: #ffffff !important;
    filter: brightness(0.9);
  }

  /* Ensure active tabs maintain brand color in dark theme */
  button[role="tab"][aria-selected="true"] {
    background: #13c1acff !important;
    color: #ffffff !important;
  }
  button[role="tab"][aria-selected="true"]::after, button.selected::after {
    background-color: #13c1acff !important;
  }
  button.selected {
    color: #13c1acff !important;
  }
  .overflow-item-selected svg {
    color: #13c1acff !important;
  }
}
"""


class ExperimentManager:
    def __init__(self):
        self.base_url = MASTER_API_URL

    def create_experiment(self, name: str, config: Dict[str, Any], data_path: str) -> Dict[str, Any]:
        """Create a new experiment"""
        try:
            # Extend payload with task-specific fields wen provided
            payload: Dict[str, Any] = {"name": name, "config": config, "data_path": data_path}
            params: Dict[str, Any] = config.get("parameters", {}) if isinstance(config, dict) else {}
            if "target_field" in params:
                payload["target_field"] = params["target_field"]
            if "n_classes" in params:
                payload["n_classes"] = params["n_classes"]
            if "n_clusters" in params:
                payload["n_clusters"] = params["n_clusters"]

            response = requests.post(f"{self.base_url}/api/v1/experiments/", json=payload)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            return {"error": str(e)}

    # Examples management
    def list_examples(self) -> list[dict[str, str]] | list:
        try:
            response = requests.get(f"{self.base_url}/api/v1/examples/")
            response.raise_for_status()
            data = response.json()
            return data.get("examples", [])
        except requests.RequestException:
            return []

    def get_example_spec(self, name: str) -> Optional[Dict[str, Any]]:
        try:
            response = requests.get(f"{self.base_url}/api/v1/examples/{name}")
            response.raise_for_status()
            return response.json()
        except requests.RequestException:
            return None

    def upload_example_dataset(self, name: str) -> Dict[str, Any]:
        try:
            response = requests.post(f"{self.base_url}/api/v1/examples/{name}/upload")
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            return {"error": str(e)}

    def list_experiments(self) -> list:
        """List all experiments"""
        try:
            response = requests.get(f"{self.base_url}/api/v1/experiments/")
            response.raise_for_status()
            return response.json()
        except requests.RequestException:
            return []

    def get_experiment(self, experiment_id: str) -> Optional[Dict[str, Any]]:
        """Get experiment by ID"""
        try:
            response = requests.get(f"{self.base_url}/api/v1/experiments/{experiment_id}")
            response.raise_for_status()
            return response.json()
        except requests.RequestException:
            return None

    def get_experiment_status(self, experiment_id: str) -> Optional[Dict[str, Any]]:
        """Get experiment status"""
        try:
            response = requests.get(f"{self.base_url}/api/v1/experiments/{experiment_id}/status")
            response.raise_for_status()
            return response.json()
        except requests.RequestException:
            return None

    def get_experiment_results(self, experiment_id: str) -> Optional[Dict[str, Any]]:
        """Get experiment results"""
        try:
            response = requests.get(f"{self.base_url}/api/v1/experiments/{experiment_id}/results")
            response.raise_for_status()
            return response.json()
        except requests.RequestException:
            return None

    def start_experiment(self, experiment_id: str) -> Dict[str, Any]:
        """Start an experiment"""
        try:
            response = requests.post(f"{self.base_url}/api/v1/experiments/{experiment_id}/start")
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            return {"error": str(e)}

    def stop_experiment(self, experiment_id: str) -> Dict[str, Any]:
        """Stop an experiment"""
        try:
            response = requests.post(f"{self.base_url}/api/v1/experiments/{experiment_id}/stop")
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            return {"error": str(e)}

    def get_system_status(self) -> Dict[str, Any]:
        """Get system status"""
        try:
            response = requests.get(f"{self.base_url}/api/v1/status/health")
            response.raise_for_status()
            return response.json()
        except requests.RequestException:
            return {"status": "unknown"}

    def drop_all_experiments(self) -> Dict[str, Any]:
        """Drop all experiments and their data"""
        try:
            response = requests.delete(f"{self.base_url}/api/v1/experiments/drop-all")
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            return {"error": str(e)}

    def upload_data_file(self, file_path: str, filename: str) -> Dict[str, Any]:
        """Upload data file to S3 storage via Master API"""
        try:
            # Use the upload endpoint that handles S3 upload
            with open(file_path, "rb") as f:
                files = {"file": (filename, f.read())}
                response = requests.post(f"{self.base_url}/api/v1/experiments/upload", files=files)
                response.raise_for_status()
                return response.json()
        except requests.RequestException as e:
            return {"error": str(e)}

    # Instance management methods
    def list_instances(self) -> list:
        """List all runner instances"""
        try:
            response = requests.get(f"{self.base_url}/api/v1/instances/")
            response.raise_for_status()
            return response.json()
        except requests.RequestException:
            return []

    def get_instance(self, instance_id: str) -> Optional[Dict[str, Any]]:
        """Get instance by ID"""
        try:
            response = requests.get(f"{self.base_url}/api/v1/instances/{instance_id}")
            response.raise_for_status()
            return response.json()
        except requests.RequestException:
            return None

    def initialize_instance(self, instance_id: str) -> Dict[str, Any]:
        """Initialize a runner instance"""
        try:
            response = requests.post(f"{self.base_url}/api/v1/instances/{instance_id}/initialize")
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            return {"error": str(e)}

    def initialize_all_instances(self) -> Dict[str, Any]:
        """Initialize all configured runner instances"""
        try:
            response = requests.post(f"{self.base_url}/api/v1/instances/initialize-all")
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            return {"error": str(e)}

    def stop_instance(self, instance_id: str) -> Dict[str, Any]:
        """Stop a runner instance"""
        try:
            response = requests.post(f"{self.base_url}/api/v1/instances/{instance_id}/stop")
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            return {"error": str(e)}

    def restart_instance(self, instance_id: str) -> Dict[str, Any]:
        """Restart a runner instance"""
        try:
            response = requests.post(f"{self.base_url}/api/v1/instances/{instance_id}/restart")
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            return {"error": str(e)}

    def get_instance_logs(self, instance_id: str, lines: int = 50) -> Dict[str, Any]:
        """Get logs from a specific runner instance"""
        try:
            response = requests.get(f"{self.base_url}/api/v1/instances/{instance_id}/logs?lines={lines}")
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            return {"error": str(e)}

    def get_available_instance(self) -> Optional[Dict[str, Any]]:
        """Get an available instance for experiment deployment"""
        try:
            response = requests.get(f"{self.base_url}/api/v1/instances/available/instance")
            response.raise_for_status()
            return response.json()
        except requests.RequestException:
            return None

    def get_health_summary(self) -> Dict[str, Any]:
        """Get health summary of all runner instances"""
        try:
            response = requests.get(f"{self.base_url}/api/v1/instances/health/summary")
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            return {"error": str(e)}


# Global experiment manager
exp_manager = ExperimentManager()


def _extract_source_path_from_upload(data_file) -> Optional[str]:
    """Extract local source path from a Gradio file upload value."""
    src_path: Optional[str] = None
    if data_file is None:
        return None
    if isinstance(data_file, (str, Path)):
        src_path = str(data_file)
    else:
        src_path = getattr(data_file, "name", None)
    return src_path if (src_path and os.path.exists(src_path)) else None


def _read_csv_columns(file_path: str) -> list:
    """Read CSV header and return column names; empty list on failure."""
    try:
        df = pd.read_csv(file_path, nrows=100)
        return list(df.columns)
    except Exception:
        return []


def build_spec_preview(
    description: str,
    task_type: str,
    target_field: str,
    num_classes: Optional[str],
    num_clusters: Optional[str],
    data_file,
    preset_example: Optional[str],
) -> str:
    """Build a JSON preview of the spec based on current UI inputs."""
    src_path = _extract_source_path_from_upload(data_file)
    spec = {
        "task_type": task_type,
        "task_description": description or "",
        "dataset_path": src_path or "",
    }
    # If no uploaded file but preset selected, show dataset from preset
    if (not src_path) and preset_example:
        try:
            data = exp_manager.get_example_spec(str(preset_example).strip())
            ds_rel = (data or {}).get("spec", {}).get("dataset_path", "")
            # Prefer object name under data/ if present
            ds_basename = os.path.basename(ds_rel) if ds_rel else ""
            if ds_basename:
                spec["dataset_path"] = f"data/{ds_basename}"
        except Exception:
            pass
    # Target optional for classification; required downstream for regression
    if task_type == "classification" and target_field:
        spec["target_field"] = target_field
    if task_type == "regression" and target_field:
        spec["target_field"] = target_field
    if task_type == "classification" and num_classes:
        try:
            spec["n_classes"] = int(str(num_classes).strip())
        except Exception:
            pass
    if task_type == "clustering":
        try:
            spec["n_clusters"] = int(str(num_clusters).strip()) if num_clusters and str(num_clusters).strip() else 3
        except Exception:
            spec["n_clusters"] = 3
    try:
        return json.dumps(spec, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"Error building spec: {e}"


def load_and_configure_preset_buttons():
    """Load preset examples and configure buttons visibility and text"""
    examples = exp_manager.list_examples()
    if not examples:
        # Hide all buttons if no examples
        updates = []
        for i in range(8):  # We have 8 buttons total
            updates.append(gr.update(visible=False))
        return tuple(updates)

    # Create updates for each button (max 8 buttons)
    updates = []
    button_names = [example.get("name", "") for example in examples if example.get("name")]

    for i in range(8):
        if i < len(button_names):
            # Show button with preset name
            updates.append(gr.update(value=button_names[i], visible=True))
        else:
            # Hide unused button
            updates.append(gr.update(visible=False))

    return tuple(updates)


def clean_form():
    """Clear all form fields and reset to defaults"""
    return (
        gr.update(value=""),  # name_input
        gr.update(value=""),  # description_input
        gr.update(value=None),  # data_file_input
        gr.update(value=None),  # task_type_input
        gr.update(choices=[], value=None, visible=False),  # target_field_input
        gr.update(visible=False, value=""),  # num_classes_input
        gr.update(visible=False, value=""),  # num_clusters_input
        gr.update(visible=False),  # clusters_hint
        gr.update(value="No dataset selected"),  # dataset_info
        None,  # preset_active_state
        None,  # preset_target_state
        gr.update(value=""),  # spec_preview_output
    )


def create_new_experiment(
    name: str,
    description: str,
    data_file,
    max_iterations: int,
    llm_model: str,
    task_type: str,
    target_field: str,
    num_classes: Optional[str],
    num_clusters: Optional[str],
    preset_example: Optional[str],
) -> str:
    """Create a new experiment"""
    if not name:
        return "Error: Experiment name is required"
    if task_type not in ("classification", "regression", "clustering"):
        return "Error: Select valid task type"
    # Validation rules per task type
    if task_type == "regression" and not target_field:
        return "Please fill the Target Column."
    if task_type == "classification":
        has_target = bool(target_field)
        has_nclasses = bool(num_classes and str(num_classes).strip())
        if not (has_target or has_nclasses):
            return "Please fill at least one: Target Column or Number of Classes."

    # Parameters used by experiment folder builder
    parameters: Dict[str, Any] = {
        "task_type": task_type,
        "task_description": description or "",
        "target_column": target_field,  # Use target_column as expected by backend
    }
    if task_type == "regression":
        if target_field:
            parameters["target_column"] = target_field
    elif task_type == "classification":
        if target_field:
            parameters["target_column"] = target_field
    elif task_type == "clustering":
        # target not applicable
        pass

    # Handle data source: preset example or uploaded file
    data_path = ""
    # If preset selected and no uploaded file, upload example dataset
    if (preset_example and str(preset_example).strip()) and (data_file is None):
        try:
            up_res = exp_manager.upload_example_dataset(str(preset_example).strip())
            if "error" in up_res:
                return f"Error uploading example dataset: {up_res['error']}"
            data_path = up_res.get("data_path", "")
        except Exception as e:
            return f"Error uploading example dataset: {e}"

    if data_file is not None:
        try:
            # Determine source path provided by Gradio
            src_path = _extract_source_path_from_upload(data_file)

            if src_path and os.path.exists(src_path):
                filename = os.path.basename(src_path)

                # Upload file directly to S3 via Master API
                upload_result = exp_manager.upload_data_file(src_path, filename)

                if "error" in upload_result:
                    return f"Error uploading file to S3: {upload_result['error']}"

                # Use the S3 path returned by the API
                data_path = upload_result.get("data_path", "")

                # Delete the local temporary file since it's now in S3
                try:
                    os.unlink(src_path)
                except Exception:
                    pass  # Ignore cleanup errors

            else:
                return "Error: Uploaded file is not accessible"
        except Exception as e:
            return f"Error uploading file: {e}"

    # Create experiment config
    config = {
        "description": description,
        "llm_model": llm_model,
        "max_iterations": max_iterations,
        "timeout_seconds": 3600,
        "parameters": parameters,
    }

    # Optional task-specific parameters
    if task_type == "classification" and num_classes and str(num_classes).strip():
        try:
            config["parameters"]["n_classes"] = int(str(num_classes).strip())
        except Exception:
            pass
    if task_type == "clustering":
        try:
            config["parameters"]["n_clusters"] = (
                int(str(num_clusters).strip()) if num_clusters and str(num_clusters).strip() else 3
            )
        except Exception:
            config["parameters"]["n_clusters"] = 3

    # Create experiment without local file (S3 path will be used by workflow)
    result = exp_manager.create_experiment(name, config, data_path)

    if "error" in result:
        return f"Error: {result['error']}"

    return f"Experiment '{name}' created successfully with ID: {result['id']}\nFile uploaded to S3: {data_path}"


def get_experiments_table() -> pd.DataFrame:
    """Get experiments as a pandas DataFrame"""
    experiments = exp_manager.list_experiments()

    if not experiments:
        return pd.DataFrame(columns=["ID", "Name", "Status", "Created", "Progress"])

    data = []
    for exp in experiments:
        data.append(
            {
                "ID": str(exp["id"]),
                "Name": exp["name"],
                "Status": exp["status"],
                "Created": exp["created_at"][:19] if exp["created_at"] else "N/A",
                "Progress": f"{exp.get('metrics', {}).get('progress', 0):.1f}%",
            }
        )

    return pd.DataFrame(data)


def get_experiment_details(experiment_id: str) -> Tuple[str, str]:
    """Get detailed information about an experiment"""
    if not experiment_id:
        return "No experiment selected", ""

    experiment = exp_manager.get_experiment(experiment_id)

    if not experiment:
        return "Experiment not found", ""

    details = f"""
    **Experiment Details**

    **Name:** {experiment["name"]}
    **Status:** {experiment["status"]}
    **Created:** {experiment["created_at"][:19] if experiment["created_at"] else "N/A"}

    **Configuration:**
    - LLM Model: {experiment["config"]["llm_model"]}
    - Max Iterations: {experiment["config"]["max_iterations"]}
    - Timeout: {experiment["config"]["timeout_seconds"]}s

    **Data Path:** {experiment["data_path"]}
    """

    if experiment.get("error_message"):
        details += f"\n**Error:** {experiment['error_message']}"

    metrics = experiment.get("metrics", {})
    metrics_text = json.dumps(metrics, indent=2) if metrics else "No metrics available"

    return details, metrics_text


def _extract_experiment_id_from_selector(selector_value: str) -> str:
    """Extract experiment ID from selector string in format 'Name (ID)'"""
    if not selector_value:
        return ""

    # Extract ID from format "Name (ID)"
    if "(" in selector_value and ")" in selector_value:
        return selector_value.split("(")[-1].strip(")")

    # Fallback: treat as ID directly if no parentheses
    return selector_value


def start_experiment_action(experiment_selector: str) -> str:
    """Start the selected experiment"""
    if not experiment_selector:
        return "No experiment selected"

    experiment_id = _extract_experiment_id_from_selector(experiment_selector)
    result = exp_manager.start_experiment(experiment_id)

    if "error" in result:
        return f"Error: {result['error']}"

    return f"Experiment {experiment_id} started successfully"


def stop_experiment_action(experiment_selector: str) -> str:
    """Stop the selected experiment"""
    if not experiment_selector:
        return "No experiment selected"

    experiment_id = _extract_experiment_id_from_selector(experiment_selector)
    result = exp_manager.stop_experiment(experiment_id)

    if "error" in result:
        return f"Error: {result['error']}"

    return f"Experiment {experiment_id} stopped successfully"


def drop_all_experiments_action() -> str:
    """Drop all experiments and their data"""
    try:
        result = exp_manager.drop_all_experiments()

        if "error" in result:
            return f"Error: {result['error']}"

        deleted_experiments = result.get("deleted_experiments", 0)
        deleted_objects = result.get("deleted_storage_objects", 0)
        message = result.get("message", "Operation completed")

        return f"✅ {message}\n📊 Deleted {deleted_experiments} experiments and {deleted_objects} storage objects"

    except Exception as e:
        return f"Error dropping experiments: {str(e)}"


def create_status_blocks(status_data: dict) -> str:
    """Create visual blocks for system status components"""
    if not status_data:
        return "<div style='color:#666; text-align:center; padding:20px'>No status data available</div>"

    # Extract main system info
    system_status = status_data.get("status", "unknown")
    version = status_data.get("version", "unknown")

    # Get uptime if available
    uptime_text = "N/A"
    if "uptime_seconds" in status_data:
        uptime_seconds = status_data["uptime_seconds"]
        hours = uptime_seconds // 3600
        minutes = (uptime_seconds % 3600) // 60
        uptime_text = f"{hours}h {minutes}m"

    # Get components
    components = status_data.get("components", {})

    # Create status blocks HTML
    html = """
    <div style="display: flex; gap: 20px; margin-bottom: 20px; flex-wrap: wrap;">
        <div style="flex: 1; min-width: 200px; color: #ffffff !important; background: {system_color};
                    border-radius: 12px; padding: 20px; text-align: center; color: white; box-shadow: 0 4px 6px rgba(0,0,0,0.1);">
            <div style="font-size: 14px; color: #ffffff !important; opacity: 0.9; margin-bottom: 8px;">System Status</div>
            <div style="font-size: 24px; color: #ffffff !important; font-weight: bold; margin-bottom: 4px; text-transform: uppercase;">{system_status}</div>
            <div style="font-size: 12px; color: #ffffff !important; opacity: 0.8;">Overall system health</div>
        </div>

        <div style="flex: 1; min-width: 200px; color: #ffffff !important; background: #d1a584ff;
                    border-radius: 12px; padding: 20px; text-align: center; color: white; box-shadow: 0 4px 6px rgba(0,0,0,0.1);">
            <div style="font-size: 14px; color: #ffffff !important; opacity: 0.9; margin-bottom: 8px;">Version</div>
            <div style="font-size: 24px; color: #ffffff !important; font-weight: bold; margin-bottom: 4px;">{version}</div>
            <div style="font-size: 12px; color: #ffffff !important; opacity: 0.8;">Platform version</div>
        </div>

        <div style="flex: 1; min-width: 200px; color: #ffffff !important; background: #8c939cff;
                    border-radius: 12px; padding: 20px; text-align: center; color: white; box-shadow: 0 4px 6px rgba(0,0,0,0.1);">
            <div style="font-size: 14px; color: #ffffff !important; opacity: 0.9; margin-bottom: 8px;">Uptime</div>
            <div style="font-size: 24px; color: #ffffff !important; font-weight: bold; margin-bottom: 4px;">{uptime}</div>
            <div style="font-size: 12px; color: #ffffff !important; opacity: 0.8;">Time since start</div>
        </div>
    </div>

    <div style="margin-top: 30px;">
        <h3 style="margin-bottom: 15px; color: #333;">Component Status</h3>
        <div style="display: flex; gap: 15px; flex-wrap: wrap;">
    """.format(
        system_color=STATUS_COLORS.get(system_status, STATUS_COLORS["unknown"]),
        system_status=system_status,
        version=version,
        uptime=uptime_text,
    )

    # Add component blocks
    for component, comp_status in components.items():
        color = STATUS_COLORS.get(comp_status, STATUS_COLORS["unknown"])
        html += f"""
            <div style="flex: 1; min-width: 150px; background: {color}; border-radius: 12px; padding: 15px; text-align: center; color: white; box-shadow: 0 4px 6px rgba(0,0,0,0.1);">
                <div style="font-size: 13px; opacity: 0.9; margin-bottom: 6px; color: white;">{component.replace("_", " ").title()}</div>
                <div style="font-size: 18px; font-weight: bold; text-transform: uppercase; color: white;">{comp_status}</div>
            </div>
        """

    html += """
        </div>
    </div>
    """

    return html


def get_system_status_text() -> str:
    """Get system status as formatted text (legacy function for fallback)"""
    try:
        # Get system health
        status = exp_manager.get_system_status()

        # Build status text with proper formatting
        lines = [
            f"**System Status:** {status.get('status', 'unknown')}",
            f"**Version:** {status.get('version', 'unknown')}",
        ]

        # Add uptime if available
        if "uptime_seconds" in status:
            uptime_seconds = status["uptime_seconds"]
            hours = uptime_seconds // 3600
            minutes = (uptime_seconds % 3600) // 60
            lines.append(f"**Uptime:** {hours}h {minutes}m")

        lines.extend(["", "**Components:**"])

        components = status.get("components", {})
        emoji_map = {
            "healthy": "🟢",
            "degraded": "🟡",
            "unhealthy": "🔴",
            "configured": "🟦",
            "disabled": "⚪",
            "not_configured": "⚪",
            "no_instances": "⚠️",
        }

        for component, comp_status in components.items():
            emoji = emoji_map.get(comp_status, "❓")
            lines.append(f"{emoji} **{component.capitalize()}:** {comp_status}")

        # Add timestamp
        if "timestamp" in status:
            lines.extend(["", f"**Last Updated:** {status['timestamp'][:19].replace('T', ' ')}"])

        return "\n\n".join(lines)

    except Exception as e:
        # Fallback if there's an error fetching status
        return f"""
**System Status:** Error fetching status
**Error:** {str(e)}

Please check the Master API connection.
        """.strip()


def get_system_status_blocks() -> str:
    """Get system status as visual blocks"""
    try:
        # Get system health
        status = exp_manager.get_system_status()
        return create_status_blocks(status)
    except Exception as e:
        # Fallback if there's an error fetching status
        return f"""
        <div style="background: #ef4444ff; border-radius: 12px; padding: 20px; text-align: center; color: white; box-shadow: 0 4px 6px rgba(0,0,0,0.1);">
            <h3 style="margin-bottom: 10px;">Error fetching system status</h3>
            <div style="font-size: 16px; opacity: 0.9;">{str(e)}</div>
            <div style="font-size: 12px; opacity: 0.8; margin-top: 10px;">Please check the Master API connection</div>
        </div>
        """


# Instance management functions
def get_instances_table() -> pd.DataFrame:
    """Get instances as a pandas DataFrame"""
    instances = exp_manager.list_instances()

    if not instances:
        return pd.DataFrame(columns=["ID", "Name", "Status", "Endpoint", "Last Heartbeat", "Current Experiment"])

    data = []
    for instance in instances:
        data.append(
            {
                "ID": instance["id"],
                "Name": instance["name"],
                "Status": instance["status"],
                "Endpoint": instance["endpoint_url"],
                "Last Heartbeat": instance["last_heartbeat"][:19] if instance["last_heartbeat"] else "Never",
                "Current Experiment": instance.get("current_experiment_id", "None"),
            }
        )

    return pd.DataFrame(data)


def get_instance_details(instance_id: str) -> str:
    """Get detailed information about an instance"""
    if not instance_id:
        return "No instance selected"

    instance = exp_manager.get_instance(instance_id)

    if not instance:
        return "Instance not found"

    details = f"""
    **Instance Details**

    **ID:** {instance["id"]}
    **Name:** {instance["name"]}
    **Status:** {instance["status"]}
    **Endpoint URL:** {instance["endpoint_url"]}
    **Created:** {instance["created_at"][:19] if instance["created_at"] else "N/A"}
    **Last Heartbeat:** {instance["last_heartbeat"][:19] if instance["last_heartbeat"] else "Never"}

    **Capabilities:** {json.dumps(instance.get("capabilities", {}), indent=2)}
    **Resources:** {json.dumps(instance.get("resources", {}), indent=2)}

    **Current Experiment:** {instance.get("current_experiment_id", "None")}
    """

    return details


def initialize_instance_action(instance_id: str) -> str:
    """Initialize the selected instance"""
    if not instance_id:
        return "No instance selected"

    result = exp_manager.initialize_instance(instance_id)

    if "error" in result:
        return f"Error: {result['error']}"

    return result.get("message", "Instance initialized successfully")


def stop_instance_action(instance_id: str) -> str:
    """Stop the selected instance"""
    if not instance_id:
        return "No instance selected"

    result = exp_manager.stop_instance(instance_id)

    if "error" in result:
        return f"Error: {result['error']}"

    return result.get("message", "Instance stopped successfully")


def restart_instance_action(instance_id: str) -> str:
    """Restart the selected instance"""
    if not instance_id:
        return "No instance selected"

    result = exp_manager.restart_instance(instance_id)

    if "error" in result:
        return f"Error: {result['error']}"

    return result.get("message", "Instance restarted successfully")


def initialize_all_instances_action() -> str:
    """Initialize all configured instances"""
    result = exp_manager.initialize_all_instances()

    if "error" in result:
        return f"Error: {result['error']}"

    message = result.get("message", "")
    summary = result.get("summary", {})

    if summary:
        return f"{message}\n📊 Total: {summary.get('total', 0)}, Successful: {summary.get('successful', 0)}, Failed: {summary.get('failed', 0)}"

    return message


def get_instance_logs_action(instance_id: str, lines: int = 50) -> str:
    """Get logs from the selected instance"""
    if not instance_id:
        return "No instance selected"

    result = exp_manager.get_instance_logs(instance_id, lines)

    if "error" in result:
        return f"Error: {result['error']}"

    logs = result.get("logs", "")
    if not logs:
        return "No logs available"

    return f"**Logs for {instance_id} (last {lines} lines):**\n\n```\n{logs}\n```"


def get_health_summary_text() -> str:
    """Get health summary as formatted text"""
    health = exp_manager.get_health_summary()

    if "error" in health:
        return f"Error: {health['error']}"

    summary = f"""
    **Instance Health Summary**

    **Total Instances:** {health.get("total_instances", 0)}
    **Healthy Instances:** {health.get("healthy_instances", 0)}
    **Unhealthy Instances:** {health.get("unhealthy_instances", 0)}
    **Busy Instances:** {health.get("busy_instances", 0)}
    **Ready Instances:** {health.get("ready_instances", 0)}
    **Offline Instances:** {health.get("offline_instances", 0)}

    **Instance Details:**
    """

    instances_detail = health.get("instances_detail", [])
    for instance in instances_detail:
        status_emoji = (
            "🟢"
            if instance["status"] in ["ready", "online"]
            else "🔴"
            if instance["status"] in ["error", "offline"]
            else "🟡"
        )
        summary += f"\n{status_emoji} **{instance['id']}** - {instance['status'].upper()}"
        summary += f"\n   - Endpoint: {instance['endpoint_url']}"
        summary += (
            f"\n   - Last Heartbeat: {instance['last_heartbeat'][:19] if instance['last_heartbeat'] else 'Never'}"
        )
        if instance.get("current_experiment_id"):
            summary += f"\n   - Current Experiment: {instance['current_experiment_id']}"
        summary += "\n"

    return summary


def create_pie_chart_for_programs(total_programs: int, total_programs_complete: int) -> str:
    """Create a pie chart showing program completion status using matplotlib"""
    if total_programs is None or total_programs_complete is None:
        return "<div style='color:#666; text-align:center; padding:20px'>No program data available</div>"

    incomplete_programs = total_programs - total_programs_complete

    if total_programs == 0:
        return "<div style='color:#666; text-align:center; padding:20px'>No programs to display</div>"

    try:
        # Create matplotlib figure
        plt.figure(figsize=(8, 6))

        # Data for pie chart
        labels = ["Completed", "Incomplete"]
        sizes = [total_programs_complete, incomplete_programs]
        colors = ["#13c1acff", "#a8afba"]
        explode = (0.05, 0.05)  # Slightly separate slices

        # Create pie chart
        wedges, texts, autotexts = plt.pie(
            sizes,
            explode=explode,
            labels=labels,
            colors=colors,
            autopct="%1.1f%%",  # Show percentages with 1 decimal
            startangle=90,
            textprops={"fontsize": 12, "fontweight": "bold", "color": "white"},
            wedgeprops={"edgecolor": "white", "linewidth": 2},
            shadow=False,
        )

        # Add title
        plt.title(
            f"Program Completion ({total_programs_complete}/{total_programs})",
            fontsize=16,
            fontweight="bold",
            pad=20,
            color="#333",
        )

        # Make percentage text more visible
        for autotext in autotexts:
            autotext.set_color("white")
            autotext.set_fontweight("bold")
            autotext.set_fontsize(11)

        # Add legend below the chart
        plt.legend(loc="lower center", bbox_to_anchor=(0.5, -0.15), ncol=2, fontsize=11, frameon=False)

        # Adjust layout to prevent label cutoff
        plt.tight_layout()
        plt.subplots_adjust(bottom=0.2)  # Extra space for legend

        # Save to buffer
        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=150, bbox_inches="tight", facecolor="white", edgecolor="none")
        buf.seek(0)

        # Convert to base64
        image_base64 = base64.b64encode(buf.getvalue()).decode("utf-8")

        # Close plot to free memory
        plt.close()

        # Create HTML with embedded image
        return f"""
        <div style="background: white; border-radius: 8px; padding: 10px; border:1px solid #ddd;border-radius:4px; text-align: center;">
            <img src="data:image/png;base64,{image_base64}"
                 alt="Program Completion Chart"
                 style="max-width: 100%; height: auto; border-radius: 4px;"
                 onerror="this.onerror=null; this.alt='Chart generation failed';" />
        </div>
        """

    except Exception as e:
        # Fallback to simple HTML if matplotlib fails
        logger.error(f"Error generating pie chart: {e}")
        return f"""
        <div style="background: white; border-radius: 8px; padding: 20px; text-align: center; box-shadow: 0 2px 4px rgba(0,0,0,0.1);">
            <h3 style="color: #333; margin-bottom: 15px;">Program Completion</h3>
            <div style="display: flex; justify-content: space-around; margin: 20px 0;">
                <div style="text-align: center;">
                    <div style="font-size: 24px; font-weight: bold; color: #13c1acff;">{total_programs_complete}</div>
                    <div style="color: #666;">Completed</div>
                </div>
                <div style="text-align: center;">
                    <div style="font-size: 24px; font-weight: bold; color: #666666;">{incomplete_programs}</div>
                    <div style="color: #666;">Incomplete</div>
                </div>
            </div>
            <div style="color: #666; font-size: 12px;">Total: {total_programs} programs</div>
        </div>
        """


def create_metric_cards(summary_data: dict) -> str:
    """Create big number blocks for key metrics"""
    if not summary_data:
        return "<div style='color:#666; text-align:center; padding:20px'>No metrics available</div>"

    # Extract metrics with fallbacks
    best_fitness = summary_data.get("best_fitness")
    best_generations = summary_data.get("best_generations")
    total_iterations = summary_data.get("total_iterations")
    total_programs = summary_data.get("total_programs")

    # Format fitness value
    fitness_str = "N/A"
    if best_fitness is not None:
        try:
            fitness_str = f"{float(best_fitness):.6f}"
        except (ValueError, TypeError):
            fitness_str = str(best_fitness)

    # Format generations
    generations_str = "N/A"
    if best_generations is not None:
        generations_str = f"{int(best_generations)}"

    # Format iterations
    iterations_str = "N/A"
    if total_iterations is not None:
        iterations_str = f"{int(total_iterations)}"

    # Format total programs
    programs_str = "N/A"
    if total_programs is not None:
        programs_str = f"{int(total_programs)}"

    # Create metric cards HTML
    html = """
    <div style="display: flex; gap: 20px; margin-bottom: 20px; flex-wrap: wrap;">
        <div style="flex: 1; min-width: 200px; color: #ffffff !important; background: linear-gradient(135deg, #13c1acff, #11a894ff);
                    border-radius: 12px; padding: 20px; text-align: center; color: white; box-shadow: 0 4px 6px rgba(0,0,0,0.1);">
            <div style="font-size: 14px; color: #ffffff !important; opacity: 0.9; margin-bottom: 8px;">Best Fitness</div>
            <div style="font-size: 32px; color: #ffffff !important; font-weight: bold; margin-bottom: 4px;">{fitness}</div>
            <div style="font-size: 12px; color: #ffffff !important; opacity: 0.8;">Performance metric</div>
        </div>

        <div style="flex: 1; min-width: 200px; color: #ffffff !important; background: linear-gradient(135deg, #8c939cff, #7a8095ff);
                    border-radius: 12px; padding: 20px; text-align: center; color: white; box-shadow: 0 4px 6px rgba(0,0,0,0.1);">
            <div style="font-size: 14px; color: #ffffff !important; opacity: 0.9; margin-bottom: 8px;">Best Generations</div>
            <div style="font-size: 32px; color: #ffffff !important; font-weight: bold; margin-bottom: 4px;">{generations}</div>
            <div style="font-size: 12px; color: #ffffff !important; opacity: 0.8;">Evolution rounds</div>
        </div>

        <div style="flex: 1; min-width: 200px; color: #ffffff !important; background: linear-gradient(135deg, #EDDACCFF, #BC9F89FF);
                    border-radius: 12px; padding: 20px; text-align: center; color: white; box-shadow: 0 4px 6px rgba(0,0,0,0.1);">
            <div style="font-size: 14px; color: #ffffff !important; opacity: 0.9; margin-bottom: 8px;">Total Iterations</div>
            <div style="font-size: 32px; color: #ffffff !important; font-weight: bold; margin-bottom: 4px;">{iterations}</div>
            <div style="font-size: 12px; color: #ffffff !important; opacity: 0.8;">Algorithm runs</div>
        </div>

        <div style="flex: 1; min-width: 200px; color: #ffffff !important; background: linear-gradient(135deg, #998FC1FF, #6250ccff);
                    border-radius: 12px; padding: 20px; text-align: center; color: white; box-shadow: 0 4px 6px rgba(0,0,0,0.1);">
            <div style="font-size: 14px; color: #ffffff !important; opacity: 0.9; margin-bottom: 8px;">Total Programs</div>
            <div style="font-size: 32px; color: #ffffff !important; font-weight: bold; margin-bottom: 4px;">{programs}</div>
            <div style="font-size: 12px; color: #ffffff !important; opacity: 0.8;">Programs generated</div>
        </div>
    </div>
    """.format(fitness=fitness_str, generations=generations_str, iterations=iterations_str, programs=programs_str)

    return html


def create_interface():
    """Create the Gradio interface"""
    with gr.Blocks(title="GigaEvo", css=AIRI_CSS) as interface:
        gr.Markdown("# GigaEvo Platform")

        # Example target suggestions when no dataset is uploaded
        classification_target_examples = ["label", "target", "species", "churn"]
        regression_target_examples = ["price", "age", "income", "target"]

        with gr.Tabs():
            # Tab 1: Create Experiment
            with gr.Tab("Create Experiment"):
                gr.Markdown("## Create New Experiment")

                # Preset Example Selection (moved above the form)
                with gr.Row():
                    with gr.Column():
                        gr.Markdown("### Start with a Preset Example")
                        with gr.Row():
                            preset_btn_1 = gr.Button(
                                "", variant="secondary", size="sm", visible=False, elem_classes=["preset-button"]
                            )
                            preset_btn_2 = gr.Button(
                                "", variant="secondary", size="sm", visible=False, elem_classes=["preset-button"]
                            )
                            preset_btn_3 = gr.Button(
                                "", variant="secondary", size="sm", visible=False, elem_classes=["preset-button"]
                            )
                            preset_btn_4 = gr.Button(
                                "", variant="secondary", size="sm", visible=False, elem_classes=["preset-button"]
                            )
                            preset_btn_5 = gr.Button(
                                "", variant="secondary", size="sm", visible=False, elem_classes=["preset-button"]
                            )
                            preset_btn_6 = gr.Button(
                                "", variant="secondary", size="sm", visible=False, elem_classes=["preset-button"]
                            )
                            preset_btn_7 = gr.Button(
                                "", variant="secondary", size="sm", visible=False, elem_classes=["preset-button"]
                            )
                            preset_btn_8 = gr.Button(
                                "", variant="secondary", size="sm", visible=False, elem_classes=["preset-button"]
                            )
                        preset_active_state = gr.State(value=None)
                        preset_target_state = gr.State(value=None)

                gr.Markdown("---")  # Separator line
                gr.Markdown("### Or Create Custom Experiment")

                with gr.Row():
                    with gr.Column():
                        name_input = gr.Textbox(label="Experiment Name", placeholder="Enter experiment name...")
                        description_input = gr.Textbox(
                            label="Description", placeholder="Optional description...", lines=3
                        )
                        data_file_input = gr.File(label="Data File", file_types=[".csv", ".json", ".txt"])
                        dataset_info = gr.Textbox(
                            label="Dataset Source",
                            value="No dataset selected",
                            interactive=False,
                            info="Current dataset being used for the experiment",
                        )
                        task_type_input = gr.Dropdown(
                            label="Task Type",
                            choices=["classification", "regression", "clustering"],
                            value=None,
                            interactive=True,
                        )
                        target_field_input = gr.Dropdown(
                            label="Target Column",
                            choices=[],
                            interactive=True,
                            value=None,
                            visible=False,
                        )
                        num_classes_input = gr.Textbox(
                            label="Number of Classes",
                            placeholder="e.g., 2 (optional; fill either Target Column or this)",
                            visible=False,
                        )
                        num_clusters_input = gr.Textbox(
                            label="Number of Clusters",
                            placeholder="e.g., 3 (defaults to 3 if empty)",
                            visible=False,
                        )
                        clusters_hint = gr.Markdown(
                            "If left empty, the number of clusters defaults to 3.",
                            visible=False,
                        )

                    with gr.Column():
                        max_iterations_input = gr.Slider(minimum=1, maximum=1000, value=100, label="Max Iterations")
                        llm_model_input = gr.Dropdown(
                            choices=[
                                "gigachat-max-2",
                            ],
                            value="gigachat-max-2",
                            label="LLM Model",
                        )
                        spec_preview_btn = gr.Button("Preview Spec JSON")
                        spec_preview_output = gr.Code(label="Spec JSON", language="json")

                with gr.Row():
                    create_btn = gr.Button("Create Experiment", variant="primary")
                    clean_btn = gr.Button("🧹 Clean Form", variant="secondary")
                create_output = gr.Textbox(label="Status", interactive=False)

                # Populate target choices when file changes
                def _update_target_choices(file, task_type, preset_active, preset_target):
                    src = _extract_source_path_from_upload(file)
                    cols = _read_csv_columns(src) if src else []

                    # Update dataset info when user uploads a file (override preset if user uploads)
                    dataset_info_update = gr.update()
                    if src:
                        filename = os.path.basename(src)
                        dataset_info_update = gr.update(value=f"📁 Using uploaded file: {filename}")

                    # If user uploaded a file, use actual columns from file, not preset target
                    if src:
                        if task_type == "classification":
                            choices = cols if cols else classification_target_examples
                            default_value = choices[0] if choices else None
                            return (gr.update(choices=choices, value=default_value, visible=True), dataset_info_update)
                        if task_type == "regression":
                            choices = cols if cols else regression_target_examples
                            default_value = choices[0] if choices else None
                            return (gr.update(choices=choices, value=default_value, visible=True), dataset_info_update)
                        # clustering or None -> hide
                        return (gr.update(visible=False, value=None), dataset_info_update)

                    # If no file uploaded and preset is active, lock target to preset_target
                    if preset_active and (preset_target or preset_target == ""):
                        if task_type in ("classification", "regression"):
                            return (
                                gr.update(
                                    choices=([preset_target] if preset_target else []),
                                    value=preset_target,
                                    visible=True,
                                ),
                                dataset_info_update,
                            )
                        # clustering or None -> hide
                        return (gr.update(visible=False, value=None), dataset_info_update)

                    # Default behavior when no file and no preset
                    if task_type == "classification":
                        choices = classification_target_examples
                        default_value = choices[0] if choices else None
                        return (gr.update(choices=choices, value=default_value, visible=True), dataset_info_update)
                    if task_type == "regression":
                        choices = regression_target_examples
                        default_value = choices[0] if choices else None
                        return (gr.update(choices=choices, value=default_value, visible=True), dataset_info_update)
                    # clustering or None -> hide
                    return (gr.update(visible=False, value=None), dataset_info_update)

                data_file_input.change(
                    _update_target_choices,
                    inputs=[data_file_input, task_type_input, preset_active_state, preset_target_state],
                    outputs=[target_field_input, dataset_info],
                )

                # Show/hide inputs depending on task type
                def _on_task_type_change(task_type, file, preset_active, preset_target):
                    src = _extract_source_path_from_upload(file)
                    cols = _read_csv_columns(src) if src else []

                    # If preset is active, keep preset target and visibility
                    if preset_active:
                        if task_type == "classification":
                            return (
                                gr.update(
                                    choices=([preset_target] if preset_target else []),
                                    visible=True,
                                    value=preset_target,
                                ),
                                gr.update(visible=True, value=""),
                                gr.update(visible=False, value=""),
                                gr.update(visible=False),
                            )
                        if task_type == "regression":
                            return (
                                gr.update(
                                    choices=([preset_target] if preset_target else []),
                                    visible=True,
                                    value=preset_target,
                                ),
                                gr.update(visible=False, value=""),
                                gr.update(visible=False, value=""),
                                gr.update(visible=False),
                            )
                        if task_type == "clustering":
                            return (
                                gr.update(visible=False, value=None),
                                gr.update(visible=False, value=""),
                                gr.update(visible=True, value=""),
                                gr.update(visible=True),
                            )
                        return (
                            gr.update(visible=False, value=None),
                            gr.update(visible=False),
                            gr.update(visible=False),
                            gr.update(visible=False),
                        )

                    if task_type == "classification":
                        choices = cols if cols else classification_target_examples
                        return (
                            gr.update(choices=choices, visible=True, value=(choices[0] if choices else None)),
                            gr.update(visible=True, value=""),
                            gr.update(visible=False, value=""),
                            gr.update(visible=False),
                        )
                    if task_type == "regression":
                        choices = cols if cols else regression_target_examples
                        return (
                            gr.update(choices=choices, visible=True, value=(choices[0] if choices else None)),
                            gr.update(visible=False, value=""),
                            gr.update(visible=False, value=""),
                            gr.update(visible=False),
                        )
                    if task_type == "clustering":
                        return (
                            gr.update(visible=False, value=None),
                            gr.update(visible=False, value=""),
                            gr.update(visible=True, value=""),
                            gr.update(visible=True),
                        )
                    # None or invalid -> hide all
                    return (
                        gr.update(visible=False, value=None),
                        gr.update(visible=False),
                        gr.update(visible=False),
                        gr.update(visible=False),
                    )

                task_type_input.change(
                    _on_task_type_change,
                    inputs=[task_type_input, data_file_input, preset_active_state, preset_target_state],
                    outputs=[target_field_input, num_classes_input, num_clusters_input, clusters_hint],
                )
                # Also handle any input change events to ensure immediate UI response
                task_type_input.input(
                    _on_task_type_change,
                    inputs=[task_type_input, data_file_input, preset_active_state, preset_target_state],
                    outputs=[target_field_input, num_classes_input, num_clusters_input, clusters_hint],
                )
                # Also handle direct selection events to ensure immediate UI response
                task_type_input.select(
                    _on_task_type_change,
                    inputs=[task_type_input, data_file_input, preset_active_state, preset_target_state],
                    outputs=[target_field_input, num_classes_input, num_clusters_input, clusters_hint],
                )

                # Ensure correct initial visibility on load
                def _init_visibility(task_type, file, preset_active, preset_target):
                    return _on_task_type_change(task_type, file, preset_active, preset_target)

                interface.load(
                    _init_visibility,
                    inputs=[task_type_input, data_file_input, preset_active_state, preset_target_state],
                    outputs=[target_field_input, num_classes_input, num_clusters_input, clusters_hint],
                )

                # Spec preview
                spec_preview_btn.click(
                    build_spec_preview,
                    inputs=[
                        description_input,
                        task_type_input,
                        target_field_input,
                        num_classes_input,
                        num_clusters_input,
                        data_file_input,
                        preset_active_state,  # Use state instead of radio value
                    ],
                    outputs=spec_preview_output,
                )

                clean_btn.click(
                    clean_form,
                    outputs=[
                        name_input,
                        description_input,
                        data_file_input,
                        task_type_input,
                        target_field_input,
                        num_classes_input,
                        num_clusters_input,
                        clusters_hint,
                        dataset_info,
                        preset_active_state,
                        preset_target_state,
                        spec_preview_output,
                    ],
                )

                create_btn.click(
                    create_new_experiment,
                    inputs=[
                        name_input,
                        description_input,
                        data_file_input,
                        max_iterations_input,
                        llm_model_input,
                        task_type_input,
                        target_field_input,
                        num_classes_input,
                        num_clusters_input,
                        preset_active_state,  # Use state instead of radio value
                    ],
                    outputs=create_output,
                )

                # Load and configure preset buttons
                interface.load(
                    load_and_configure_preset_buttons,
                    outputs=[
                        preset_btn_1,
                        preset_btn_2,
                        preset_btn_3,
                        preset_btn_4,
                        preset_btn_5,
                        preset_btn_6,
                        preset_btn_7,
                        preset_btn_8,
                    ],
                )

                # Individual preset button click handlers
                def _on_preset_button_click(example_name: str):
                    """Handle preset button click - populate form with preset configuration and generate spec preview"""
                    if not example_name:
                        # Return default values if no example name
                        return (
                            gr.update(value=""),  # description
                            gr.update(value=None),  # task_type
                            gr.update(choices=[], value=None, visible=False),  # target_field
                            gr.update(visible=False, value=""),  # num_classes
                            gr.update(visible=False, value=""),  # num_clusters
                            gr.update(value=""),  # name
                            gr.update(value=None),  # data_file
                            gr.update(value="No dataset selected"),  # dataset_info
                            gr.update(value=""),  # spec_preview_output
                            None,  # preset_active_state
                            None,  # preset_target_state
                        )

                    data = exp_manager.get_example_spec(example_name)
                    if not data:
                        return (
                            gr.update(value=""),  # description
                            gr.update(value=None),  # task_type
                            gr.update(choices=[], value=None, visible=False),  # target_field
                            gr.update(visible=False, value=""),  # num_classes
                            gr.update(visible=False, value=""),  # num_clusters
                            gr.update(value=""),  # name
                            gr.update(value=None),  # data_file
                            gr.update(value="No dataset selected"),  # dataset_info
                            gr.update(value=""),  # spec_preview_output
                            None,  # preset_active_state
                            None,  # preset_target_state
                        )

                    spec = data.get("spec") or {}
                    label = data.get("label") or example_name
                    default_name = str(label)  # simple name without timestamp
                    task = spec.get("task_type")
                    desc = spec.get("task_description", "")
                    target = spec.get("target_field") or None
                    n_clusters = str(spec.get("n_clusters", "")) if "n_clusters" in spec else ""

                    # Get dataset info for display
                    dataset_info_update = gr.update(value="No dataset selected")
                    file_update = gr.update(value=None)

                    try:
                        ds_rel = spec.get("dataset_path", "")
                        ds_basename = os.path.basename(ds_rel) if ds_rel else ""
                        if ds_basename:
                            # Update dataset info to show preset dataset
                            dataset_info_update = gr.update(value=f"📁 Using preset dataset: {ds_basename}")

                            # Optionally download the file for user convenience
                            bucket = _resolve_bucket_name()
                            url = f"{S3_API_URL}/{bucket}/data/{ds_basename}"
                            os.makedirs(INPUT_DATA_DIR, exist_ok=True)
                            local_path = os.path.join(INPUT_DATA_DIR, ds_basename)
                            resp = requests.get(url, timeout=5)
                            if resp.ok:
                                with open(local_path, "wb") as f:
                                    f.write(resp.content)
                                file_update = gr.update(value=local_path)
                    except Exception:
                        # If we can't access the file, still show the dataset name
                        ds_rel = spec.get("dataset_path", "")
                        ds_basename = os.path.basename(ds_rel) if ds_rel else "Preset dataset"
                        if ds_basename:
                            dataset_info_update = gr.update(value=f"📁 Using preset dataset: {ds_basename}")

                    # Generate spec preview
                    # Get the actual file path if available
                    file_data = None
                    if file_update and hasattr(file_update, "value") and file_update.value:
                        file_data = file_update.value

                    spec_preview = build_spec_preview(
                        description=desc,
                        task_type=task or "",
                        target_field=target or "",
                        num_classes="",  # Empty for preset since not filled yet
                        num_clusters=n_clusters,
                        data_file=file_data,
                        preset_example=example_name,
                    )
                    spec_preview_update = gr.update(value=spec_preview)

                    # Update UI depending on task
                    if task == "classification":
                        return (
                            gr.update(value=desc),
                            gr.update(value="classification"),
                            gr.update(choices=[target] if target else [], value=target, visible=True),
                            gr.update(visible=True, value=""),  # num_classes visible but empty
                            gr.update(visible=False, value=""),  # num_clusters hidden
                            gr.update(value=default_name),
                            file_update,
                            dataset_info_update,
                            spec_preview_update,
                            example_name,
                            target,
                        )
                    if task == "regression":
                        return (
                            gr.update(value=desc),
                            gr.update(value="regression"),
                            gr.update(choices=[target] if target else [], value=target, visible=True),
                            gr.update(visible=False, value=""),
                            gr.update(visible=False, value=""),
                            gr.update(value=default_name),
                            file_update,
                            dataset_info_update,
                            spec_preview_update,
                            example_name,
                            target,
                        )
                    if task == "clustering":
                        return (
                            gr.update(value=desc),
                            gr.update(value="clustering"),
                            gr.update(choices=[], value=None, visible=False),
                            gr.update(visible=False, value=""),
                            gr.update(visible=True, value=n_clusters),
                            gr.update(value=default_name),
                            file_update,
                            dataset_info_update,
                            spec_preview_update,
                            example_name,
                            target,
                        )
                    # default
                    return (
                        gr.update(value=desc),
                        gr.update(value=None),
                        gr.update(choices=[], value=None, visible=False),
                        gr.update(visible=False, value=""),
                        gr.update(visible=False, value=""),
                        gr.update(value=default_name),
                        file_update,
                        dataset_info_update,
                        spec_preview_update,
                        example_name,
                        target,
                    )

                # Create individual click handlers for each preset button
                def _create_preset_button_handler(preset_name):
                    """Create a handler function for a specific preset button"""

                    def handler():
                        return _on_preset_button_click(preset_name)

                    return handler

                # Wire up individual preset buttons with their click handlers
                examples = exp_manager.list_examples()
                button_refs = [
                    preset_btn_1,
                    preset_btn_2,
                    preset_btn_3,
                    preset_btn_4,
                    preset_btn_5,
                    preset_btn_6,
                    preset_btn_7,
                    preset_btn_8,
                ]
                example_names = [example.get("name", "") for example in examples if example.get("name")]

                for i, (btn, example_name) in enumerate(zip(button_refs, example_names)):
                    if example_name:
                        # Create a closure to capture the correct example_name
                        def create_handler(name):
                            return lambda: _on_preset_button_click(name)

                        handler = create_handler(example_name)
                        btn.click(
                            handler,
                            outputs=[
                                description_input,
                                task_type_input,
                                target_field_input,
                                num_classes_input,
                                num_clusters_input,
                                name_input,
                                data_file_input,
                                dataset_info,
                                spec_preview_output,
                                preset_active_state,
                                preset_target_state,
                            ],
                        )

            # Tab 2: Experiments List
            with gr.Tab("Experiments"):
                gr.Markdown("## Active and Historical Experiments")

                experiments_df = gr.Dataframe(value=get_experiments_table(), label="Experiments", interactive=False)  # type: ignore

                with gr.Row():
                    refresh_btn = gr.Button("Refresh")
                    drop_all_btn = gr.Button("🗑️ Drop All Experiments", variant="stop", size="sm")

                with gr.Row():
                    experiment_selector = gr.Dropdown(label="Select Experiment", choices=[], interactive=True)

                    start_btn = gr.Button("Start", variant="primary")
                    stop_btn = gr.Button("Stop", variant="stop")

                action_output = gr.Textbox(label="Action Status", interactive=False)

                # Wire up button handlers
                def refresh_and_update_selector():
                    """Refresh experiments table and update selector"""
                    updated_table = get_experiments_table()
                    updated_selector = update_experiment_selector(updated_table)
                    return updated_table, updated_selector

                refresh_btn.click(refresh_and_update_selector, outputs=[experiments_df, experiment_selector])

                # Drop all experiments functionality
                def drop_all_and_refresh():
                    """Drop all experiments and refresh the table"""
                    result = drop_all_experiments_action()
                    updated_table = get_experiments_table()
                    return result, updated_table, gr.Dropdown(choices=[], interactive=True)

                drop_all_btn.click(drop_all_and_refresh, outputs=[action_output, experiments_df, experiment_selector])

                # Update experiment selector when dataframe changes
                def update_experiment_selector(df):
                    if df is not None and len(df) > 0:
                        # Create choices as "Name (ID)" format for better usability
                        # Using itertuples() which is more efficient and returns named tuples
                        choices = [f"{row.Name} ({row.ID})" for row in df.itertuples()]
                        return gr.Dropdown(choices=choices, interactive=True)
                    return gr.Dropdown(choices=[], interactive=True)

                experiments_df.change(update_experiment_selector, inputs=experiments_df, outputs=experiment_selector)

                start_btn.click(start_experiment_action, inputs=experiment_selector, outputs=action_output)
                stop_btn.click(stop_experiment_action, inputs=experiment_selector, outputs=action_output)

            # Tab 3.5: Experiment Details & Visualization
            with gr.Tab("Experiment Details and Visualization"):
                gr.Markdown("## Experiment Details and Visualization")

                results_selector = gr.Dropdown(label="Select Experiment", choices=[], interactive=True)

                # Key Metrics Cards (Big Numbers)
                gr.Markdown("### Key Metrics")
                metrics_cards_display = gr.HTML(label="Metrics Cards")

                with gr.Row():
                    # Left column: Pie Chart (1 column width)
                    with gr.Column(scale=1):
                        pie_chart_display = gr.HTML(label="Program Completion Chart")

                    # Right column: Visualization Image (2 columns width)
                    with gr.Column(scale=2):
                        visualization_display = gr.HTML(label="Experiment Visualization")

                refresh_results_btn = gr.Button("Refresh Results")

                # Build HTML for live image
                def _build_image_html(experiment_selector_value: str) -> str:
                    exp_id = _extract_experiment_id_from_selector(experiment_selector_value)
                    if not exp_id:
                        return "<div style='color:#666'>Select an experiment to view visualization</div>"
                    ts = int(time.time())
                    bucket = _resolve_bucket_name()
                    url = f"{S3_API_URL}/{bucket}/experiments_results/{exp_id}/metrics_plot.png?ts={ts}"
                    return f"<img src='{url}' style='max-width:100%;border:1px solid #ddd;border-radius:4px' alt='visual'/>"

                def _fetch_report_summary_from_master(experiment_selector_value: str) -> str:
                    exp_id = _extract_experiment_id_from_selector(experiment_selector_value)
                    if not exp_id:
                        return "{}"
                    ts = int(time.time())
                    try:
                        # Use summary endpoint which returns robust subset
                        url = f"{MASTER_API_URL}/results/{exp_id}/summary?ts={ts}"
                        resp = requests.get(url, timeout=5)
                        if resp.ok:
                            return json.dumps(resp.json(), indent=2)
                        # Fallback: skeleton
                        return json.dumps(
                            {
                                "total_iterations": None,
                                "total_programs": None,
                                "total_programs_complete": None,
                                "best_fitness": None,
                                "best_generations": None,
                            },
                            indent=2,
                        )
                    except Exception:
                        return json.dumps(
                            {
                                "total_iterations": None,
                                "total_programs": None,
                                "total_programs_complete": None,
                                "best_fitness": None,
                                "best_generations": None,
                            },
                            indent=2,
                        )

                # Function to load experiment visualizations (metrics cards + pie chart + live image)
                def load_experiment_visualizations(experiment_selector_value: str):
                    if not experiment_selector_value:
                        return create_metric_cards({}), create_pie_chart_for_programs(0, 0), _build_image_html("")

                    experiment_id = _extract_experiment_id_from_selector(experiment_selector_value)
                    if not experiment_id:
                        return create_metric_cards({}), create_pie_chart_for_programs(0, 0), _build_image_html("")

                    # Parse summary data
                    summary_json_str = _fetch_report_summary_from_master(experiment_selector_value)
                    try:
                        summary_data = json.loads(summary_json_str)
                    except json.JSONDecodeError:
                        summary_data = {}

                    # Create metric cards and pie chart from summary data
                    metric_cards_html = create_metric_cards(summary_data)

                    total_programs = summary_data.get("total_programs", 0)
                    total_programs_complete = summary_data.get("total_programs_complete", 0)
                    pie_chart_html = create_pie_chart_for_programs(total_programs, total_programs_complete)

                    # Build visualization image HTML
                    image_html = _build_image_html(experiment_selector_value)

                    return (metric_cards_html, pie_chart_html, image_html)

                results_selector.change(
                    load_experiment_visualizations,
                    inputs=results_selector,
                    outputs=[metrics_cards_display, pie_chart_display, visualization_display],
                )

                # Wire refresh button to reload current results without errors
                refresh_results_btn.click(
                    load_experiment_visualizations,
                    inputs=results_selector,
                    outputs=[metrics_cards_display, pie_chart_display, visualization_display],
                )

                # Populate results selector on load
                def _results_selector_choices():
                    exps = exp_manager.list_experiments()
                    choices = [f"{e.get('name')} ({e.get('id')})" for e in exps] if exps else []
                    return gr.Dropdown(choices=choices, interactive=True)

                interface.load(_results_selector_choices, outputs=results_selector)

                # Auto-refresh all visualization components every 10 seconds
                def update_all_visualizations(experiment_selector_value: str):
                    """Update visualization image, pie chart, and metrics cards"""
                    if not experiment_selector_value:
                        return create_metric_cards({}), create_pie_chart_for_programs(0, 0), _build_image_html("")

                    experiment_id = _extract_experiment_id_from_selector(experiment_selector_value)
                    if not experiment_id:
                        return create_metric_cards({}), create_pie_chart_for_programs(0, 0), _build_image_html("")

                    # Parse summary data
                    summary_json_str = _fetch_report_summary_from_master(experiment_selector_value)
                    try:
                        summary_data = json.loads(summary_json_str)
                    except json.JSONDecodeError:
                        summary_data = {}

                    # Create metric cards and pie chart from summary data
                    metric_cards_html = create_metric_cards(summary_data)

                    total_programs = summary_data.get("total_programs", 0)
                    total_programs_complete = summary_data.get("total_programs_complete", 0)
                    pie_chart_html = create_pie_chart_for_programs(total_programs, total_programs_complete)

                    # Build visualization image HTML
                    image_html = _build_image_html(experiment_selector_value)

                    return metric_cards_html, pie_chart_html, image_html

                vis_timer = gr.Timer(10)
                vis_timer.tick(
                    update_all_visualizations,
                    inputs=results_selector,
                    outputs=[metrics_cards_display, pie_chart_display, visualization_display],
                )

                # Auto-refresh selector choices every 10 seconds while preserving selection
                def _refresh_results_selector(current_value: str):
                    exps = exp_manager.list_experiments()
                    choices = [f"{e.get('name')} ({e.get('id')})" for e in exps] if exps else []
                    value = current_value if current_value in choices else (choices[0] if choices else None)
                    return gr.Dropdown(choices=choices, value=value, interactive=True)

                selector_timer = gr.Timer(10)
                selector_timer.tick(_refresh_results_selector, inputs=results_selector, outputs=results_selector)

            # Tab 3: Experiment Results
            with gr.Tab("Experiment Results"):
                gr.Markdown("## Experiment Results")

                res_selector = gr.Dropdown(label="Select Experiment", choices=[], interactive=True)

                with gr.Column():
                    best_program_code = gr.Code(label="Best Program (.py)", language="python", lines=32)
                    refresh_res_btn = gr.Button("Refresh Results")
                    download_btn = gr.DownloadButton(label="Download Results", value=None, variant="primary")
                    download_hint = gr.HTML("<div style='color:#666'>Select an experiment to enable download</div>")

                def _results_selector_choices2():
                    exps = exp_manager.list_experiments()
                    choices = [f"{e.get('name')} ({e.get('id')})" for e in exps] if exps else []
                    return gr.Dropdown(choices=choices, interactive=True)

                interface.load(_results_selector_choices2, outputs=res_selector)

                def _fetch_best_program_code(experiment_selector_value: str) -> str:
                    exp_id = _extract_experiment_id_from_selector(experiment_selector_value)
                    if not exp_id:
                        return ""
                    ts = int(time.time())
                    try:
                        url = f"{MASTER_API_URL}/results/{exp_id}/evolution_report.json?ts={ts}"
                        resp = requests.get(url, timeout=5)
                        if not resp.ok:
                            return ""
                        data = resp.json()
                        code = data.get("best_program") or ""
                        return code
                    except Exception:
                        return ""

                def _prepare_download_file(experiment_selector_value: str) -> Tuple[Optional[str], str]:
                    """Fetch a ready archive with best program and validation from Master API and return a temp file path."""
                    exp_id = _extract_experiment_id_from_selector(experiment_selector_value)
                    if not exp_id:
                        return None, "<div style='color:#666'>No experiment selected</div>"
                    try:
                        import tempfile

                        import requests as _requests

                        ts = int(time.time())
                        url = f"{MASTER_API_URL}/results/{exp_id}/download.zip?ts={ts}"
                        resp = _requests.get(url, timeout=15)
                        if not resp.ok or not resp.content:
                            return None, "<div style='color:#a00'>Failed to fetch archive</div>"
                        tmp = tempfile.NamedTemporaryFile(prefix="results_", suffix=".zip", delete=False)
                        tmp.write(resp.content)
                        tmp.flush()
                        tmp.close()
                        return tmp.name, "<div>Ready to download results.zip</div>"
                    except Exception:
                        return None, "<div style='color:#a00'>Failed to prepare download</div>"

                # Update code display when selection changes
                res_selector.change(_fetch_best_program_code, inputs=res_selector, outputs=best_program_code)
                # Manual refresh button mirrors selection change
                refresh_res_btn.click(_fetch_best_program_code, inputs=res_selector, outputs=best_program_code)
                # Wire download button: on click, prepare temp file
                download_btn.click(_prepare_download_file, inputs=res_selector, outputs=[download_btn, download_hint])

                # Auto-refresh code every 10 seconds
                code_timer = gr.Timer(10)
                code_timer.tick(_fetch_best_program_code, inputs=res_selector, outputs=best_program_code)

                # Auto-refresh selector choices every 10 seconds while preserving selection
                def _refresh_res_selector(current_value: str):
                    exps = exp_manager.list_experiments()
                    choices = [f"{e.get('name')} ({e.get('id')})" for e in exps] if exps else []
                    value = current_value if current_value in choices else (choices[0] if choices else None)
                    return gr.Dropdown(choices=choices, value=value, interactive=True)

                res_selector_timer = gr.Timer(10)
                res_selector_timer.tick(_refresh_res_selector, inputs=res_selector, outputs=res_selector)

            # Tab 4: Runner Instances
            with gr.Tab("Runner Instances"):
                gr.Markdown("## Runner API Instance Management")

                with gr.Tabs():
                    # Sub-tab 4.1: Instance List
                    with gr.Tab("Instance List"):
                        gr.Markdown("### All Runner Instances")

                        instances_df = gr.Dataframe(
                            value=get_instances_table(), label="Runner Instances", interactive=False
                        )  # type: ignore

                        with gr.Row():
                            refresh_instances_btn = gr.Button("Refresh List")
                            initialize_all_btn = gr.Button("🚀 Initialize All", variant="primary")

                        with gr.Row():
                            instance_selector = gr.Dropdown(label="Select Instance", choices=[], interactive=True)

                            with gr.Column():
                                init_btn = gr.Button("Initialize", variant="primary", size="sm")
                                stop_btn = gr.Button("Stop", variant="stop", size="sm")
                                restart_btn = gr.Button("Restart", variant="secondary", size="sm")

                        instance_action_output = gr.Textbox(label="Action Status", interactive=False)

                    # Sub-tab 4.2: Instance Details
                    with gr.Tab("Instance Details"):
                        gr.Markdown("### Detailed Instance Information")

                        detail_instance_selector = gr.Dropdown(label="Select Instance", choices=[], interactive=True)
                        details_display = gr.Markdown(label="Instance Details")

                    # Sub-tab 4.3: Instance Logs
                    with gr.Tab("Instance Logs"):
                        gr.Markdown("### View Instance Logs")

                        logs_instance_selector = gr.Dropdown(label="Select Instance", choices=[], interactive=True)

                        with gr.Row():
                            log_lines_slider = gr.Slider(
                                minimum=10, maximum=500, value=50, step=10, label="Number of Log Lines"
                            )
                            get_logs_btn = gr.Button("Get Logs", variant="primary")

                        logs_display = gr.Markdown(label="Instance Logs")

                    # Sub-tab 4.4: Health Summary
                    with gr.Tab("Health Summary"):
                        gr.Markdown("### Instance Health Overview")

                        health_display = gr.Markdown(get_health_summary_text())
                        refresh_health_btn = gr.Button("Refresh Health")

                # Wire up button handlers for instances tab
                refresh_instances_btn.click(get_instances_table, outputs=instances_df)

                def initialize_all_and_refresh():
                    """Initialize all instances and refresh the table"""
                    result = initialize_all_instances_action()
                    updated_table = get_instances_table()
                    # Update selectors
                    instances = exp_manager.list_instances()
                    choices = [str(inst["id"]) for inst in instances] if instances else []
                    return (
                        result,
                        updated_table,
                        gr.Dropdown(choices=choices, interactive=True),
                        gr.Dropdown(choices=choices, interactive=True),
                        gr.Dropdown(choices=choices, interactive=True),
                    )

                initialize_all_btn.click(
                    initialize_all_and_refresh,
                    outputs=[
                        instance_action_output,
                        instances_df,
                        instance_selector,
                        detail_instance_selector,
                        logs_instance_selector,
                    ],
                )

                # Update instance selectors when dataframe changes
                def update_instance_selectors(df):
                    if df is not None and len(df) > 0:
                        choices = df["ID"].tolist()
                        return (
                            gr.Dropdown(choices=choices, interactive=True),
                            gr.Dropdown(choices=choices, interactive=True),
                            gr.Dropdown(choices=choices, interactive=True),
                        )
                    return (
                        gr.Dropdown(choices=[], interactive=True),
                        gr.Dropdown(choices=[], interactive=True),
                        gr.Dropdown(choices=[], interactive=True),
                    )

                instances_df.change(
                    update_instance_selectors,
                    inputs=instances_df,
                    outputs=[instance_selector, detail_instance_selector, logs_instance_selector],
                )

                # Instance action handlers
                init_btn.click(initialize_instance_action, inputs=instance_selector, outputs=instance_action_output)
                stop_btn.click(stop_instance_action, inputs=instance_selector, outputs=instance_action_output)
                restart_btn.click(restart_instance_action, inputs=instance_selector, outputs=instance_action_output)

                # Details handler
                detail_instance_selector.change(
                    get_instance_details, inputs=detail_instance_selector, outputs=details_display
                )

                # Logs handler
                def get_logs_and_display(instance_id, lines):
                    logs = get_instance_logs_action(instance_id, lines)
                    return logs

                get_logs_btn.click(
                    get_logs_and_display, inputs=[logs_instance_selector, log_lines_slider], outputs=logs_display
                )

                # Health refresh
                refresh_health_btn.click(get_health_summary_text, outputs=health_display)

            # Tab 5: System Status
            with gr.Tab("System Status"):
                gr.Markdown("## System Health and Status")
                status_blocks_display = gr.HTML(label="Status Blocks")

                refresh_status_btn = gr.Button("Refresh Status")
                refresh_status_btn.click(get_system_status_blocks, outputs=status_blocks_display)

        # Auto-refresh experiments and instances lists
        def load_experiments_and_selector():
            """Load experiments table and update selector on interface load"""
            table = get_experiments_table()
            selector = update_experiment_selector(table)
            return table, selector

        interface.load(load_experiments_and_selector, outputs=[experiments_df, experiment_selector])
        interface.load(get_instances_table, outputs=instances_df)
        interface.load(get_system_status_blocks, outputs=status_blocks_display)

        # Add a timer for real-time updates (refresh every 30 seconds)
        timer = gr.Timer(30)  # Refresh every 30 seconds

        def refresh_all_data():
            """Refresh both experiments and instances data, and update experiment selector"""
            exp_table = get_experiments_table()
            exp_selector = update_experiment_selector(exp_table)
            inst_table = get_instances_table()
            return exp_table, exp_selector, inst_table

        timer.tick(refresh_all_data, outputs=[experiments_df, experiment_selector, instances_df])

        # Add a timer for system status updates (refresh every 15 seconds)
        system_status_timer = gr.Timer(15)  # Refresh every 15 seconds
        system_status_timer.tick(get_system_status_blocks, outputs=status_blocks_display)

    return interface


if __name__ == "__main__":
    interface = create_interface()
    interface.launch(server_name="0.0.0.0", server_port=7860, share=False)
