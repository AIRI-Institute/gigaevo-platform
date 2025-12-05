"""Base component class for common UI patterns."""

import json
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Tuple

import gradio as gr
from loguru import logger
from services.experiment_manager import ExperimentManager
from services.instance_manager import InstanceManager
from services.status_service import StatusService
from utils.formatters import extract_experiment_id_from_selector


class BaseComponent(ABC):
    """Base class for UI components with common functionality."""

    def __init__(
        self,
        experiment_manager: ExperimentManager,
        instance_manager: InstanceManager,
        status_service: StatusService,
    ):
        """Initialize base component.

        Args:
            experiment_manager: Service for experiment operations
            instance_manager: Service for instance operations
            status_service: Service for status operations
        """
        self.exp_manager = experiment_manager
        self.inst_manager = instance_manager
        self.status_service = status_service

    @abstractmethod
    def build(self) -> Any:
        """Build the component UI. Must be implemented by subclasses."""
        pass

    def handle_api_error(self, error: Dict[str, Any], operation: str) -> str:
        """Handle API errors consistently.

        Args:
            error: Error dictionary from API call
            operation: Description of the operation that failed

        Returns:
            User-friendly error message
        """
        if "error" in error:
            logger.error(f"{operation} failed: {error['error']}")
            return f"Error: {error['error']}"
        else:
            logger.error(f"{operation} failed with unknown error")
            return f"Error: {operation} failed"

    def create_status_display(self, success_message: Optional[str] = None, error_message: Optional[str] = None) -> str:
        """Create a status display message.

        Args:
            success_message: Success message to display
            error_message: Error message to display

        Returns:
            Formatted status message
        """
        if error_message:
            return f"❌ {error_message}"
        elif success_message:
            return f"✅ {success_message}"
        else:
            return ""

    def update_selector_with_fallback(
        self, current_choices: List[str], current_value: Optional[str] = None
    ) -> gr.Dropdown:
        """Update selector with fallback to first choice.

        Args:
            current_choices: New choices for the selector
            current_value: Current selected value (will be preserved if in choices)

        Returns:
            Updated gradio Dropdown component
        """
        if not current_choices:
            return gr.Dropdown(choices=[], interactive=True, value=None)

        value = current_value if current_value in current_choices else current_choices[0]
        return gr.Dropdown(choices=current_choices, interactive=True, value=value)

    def refresh_data_safely(self, fetch_function, error_message: str = "Failed to refresh data") -> Any:
        """Safely refresh data with error handling.

        Args:
            fetch_function: Function to call to fetch data
            error_message: Error message to use on failure

        Returns:
            Fetched data or empty fallback
        """
        try:
            return fetch_function()
        except Exception as e:
            logger.error(f"{error_message}: {e}")
            return []

    def build_spec_preview(
        self,
        description: str,
        task_type: str,
        target_field: Optional[str],
        num_classes: Optional[str],
        num_clusters: Optional[str],
        data_file_path: Optional[str],
        preset_example: Optional[str],
    ) -> str:
        """Build a JSON preview of the experiment specification.

        Args:
            description: Task description
            task_type: Type of task
            target_field: Target column name
            num_classes: Number of classes
            num_clusters: Number of clusters
            data_file_path: Path to data file
            preset_example: Name of preset example

        Returns:
            JSON string of the specification
        """
        spec = {
            "task_type": task_type,
            "task_description": description or "",
            "dataset_path": data_file_path or "",
        }

        # Add task-specific fields
        if task_type == "classification" and target_field:
            spec["target_field"] = target_field
        if task_type == "regression" and target_field:
            spec["target_field"] = target_field
        if task_type == "classification" and num_classes:
            try:
                spec["n_classes"] = int(str(num_classes).strip())  # type: ignore
            except ValueError:
                pass
        if task_type == "clustering":
            try:
                spec["n_clusters"] = int(str(num_clusters).strip()) if num_clusters and str(num_clusters).strip() else 3  # type: ignore
            except ValueError:
                spec["n_clusters"] = 3  # type: ignore

        # Handle preset example
        if (not data_file_path) and preset_example:
            try:
                data = self.exp_manager.get_example_spec(str(preset_example).strip())
                if data:
                    ds_rel = (data.get("spec") or {}).get("dataset_path", "")
                    ds_basename = ds_rel.split("/")[-1] if ds_rel else ""
                    if ds_basename:
                        spec["dataset_path"] = f"data/{ds_basename}"
            except Exception:
                pass

        try:
            return json.dumps(spec, ensure_ascii=False, indent=2)
        except Exception as e:
            return f"Error building spec: {e}"

    def extract_experiment_id(self, selector_value: str) -> str:
        """Extract experiment ID from selector.

        Args:
            selector_value: Selector string in format "Name (ID)"

        Returns:
            Experiment ID string
        """
        return extract_experiment_id_from_selector(selector_value)

    def create_loading_indicator(self) -> str:
        """Create a loading indicator message.

        Returns:
            HTML string for loading indicator
        """
        return """
        <div style="text-align: center; padding: 20px; color: #666;">
            <div style="display: inline-block; animation: spin 1s linear infinite;">
                ⏳
            </div>
            <div style="margin-top: 10px;">Loading...</div>
        </div>
        <style>
        @keyframes spin {
            from { transform: rotate(0deg); }
            to { transform: rotate(360deg); }
        }
        </style>
        """

    def format_file_size(self, file_path: Optional[str]) -> str:
        """Format file size for display.

        Args:
            file_path: Path to file

        Returns:
            Formatted file size string
        """
        if not file_path:
            return "No file selected"

        try:
            import os

            size = os.path.getsize(file_path)
            for unit in ["B", "KB", "MB", "GB"]:
                if size < 1024:
                    return f"{size:.1f} {unit}"
                size /= 1024
            return f"{size:.1f} TB"
        except (OSError, TypeError):
            return "Unknown size"

    def validate_inputs(self, **kwargs) -> Tuple[bool, List[str]]:
        """Validate common inputs across components.

        Args:
            **kwargs: Input values to validate

        Returns:
            Tuple of (is_valid, error_messages)
        """
        errors = []

        # Check for None values in required fields
        for field_name, value in kwargs.items():
            if value is None:
                errors.append(f"{field_name} is required")

        return len(errors) == 0, errors
