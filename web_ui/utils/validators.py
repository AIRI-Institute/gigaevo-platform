"""Validation utilities for the web UI."""

import re
from typing import List, Optional, Tuple

from config.settings import (
    CLASSIFICATION_TARGET_EXAMPLES,
    REGRESSION_TARGET_EXAMPLES,
    VALIDATION_RULES,
)


def validate_experiment_name(name: str) -> Tuple[bool, Optional[str]]:
    """Validate experiment name.

    Args:
        name: Experiment name to validate

    Returns:
        Tuple of (is_valid, error_message)
    """
    if not name or not name.strip():
        return False, "Experiment name is required"

    name = name.strip()
    rules = VALIDATION_RULES["experiment_name"]

    if len(name) < rules["min_length"]:
        return False, f"Name must be at least {rules['min_length']} characters"

    if len(name) > rules["max_length"]:
        return False, f"Name must be no more than {rules['max_length']} characters"

    if not re.match(rules["pattern"], name):
        return False, rules["description"]

    return True, None


def validate_max_iterations(value: int) -> Tuple[bool, Optional[str]]:
    """Validate max iterations value.

    Args:
        value: Value to validate

    Returns:
        Tuple of (is_valid, error_message)
    """
    rules = VALIDATION_RULES["max_iterations"]

    if value < rules["min"]:
        return False, f"Max iterations must be at least {rules['min']}"

    if value > rules["max"]:
        return False, f"Max iterations must be no more than {rules['max']}"

    return True, None


def validate_log_lines(value: int) -> Tuple[bool, Optional[str]]:
    """Validate number of log lines.

    Args:
        value: Value to validate

    Returns:
        Tuple of (is_valid, error_message)
    """
    rules = VALIDATION_RULES["log_lines"]

    if value < rules["min"]:
        return False, f"Log lines must be at least {rules['min']}"

    if value > rules["max"]:
        return False, f"Log lines must be no more than {rules['max']}"

    if value % rules.get("step", 1) != 0:
        return False, f"Log lines must be a multiple of {rules.get('step', 1)}"

    return True, None


def validate_num_clusters(value: str) -> Tuple[bool, Optional[int], Optional[str]]:
    """Validate number of clusters.

    Args:
        value: String value to validate

    Returns:
        Tuple of (is_valid, parsed_int, error_message)
    """
    if not value or not value.strip():
        # Empty is OK, will use default
        return True, None, None

    try:
        clusters = int(value.strip())
        rules = VALIDATION_RULES["num_clusters"]

        if clusters < rules["min"]:
            return False, None, f"Number of clusters must be at least {rules['min']}"

        if clusters > rules["max"]:
            return False, None, f"Number of clusters must be no more than {rules['max']}"

        return True, clusters, None

    except ValueError:
        return False, None, "Number of clusters must be a valid integer"


def validate_num_classes(value: str) -> Tuple[bool, Optional[int], Optional[str]]:
    """Validate number of classes.

    Args:
        value: String value to validate

    Returns:
        Tuple of (is_valid, parsed_int, error_message)
    """
    if not value or not value.strip():
        # Empty is OK for classification (optional)
        return True, None, None

    try:
        classes = int(value.strip())

        if classes < 2:
            return False, None, "Number of classes must be at least 2"

        if classes > 1000:  # Reasonable upper limit
            return False, None, "Number of classes must be no more than 1000"

        return True, classes, None

    except ValueError:
        return False, None, "Number of classes must be a valid integer"


def validate_task_type(task_type: str) -> Tuple[bool, Optional[str]]:
    """Validate task type.

    Args:
        task_type: Task type to validate

    Returns:
        Tuple of (is_valid, error_message)
    """
    valid_types = ["classification", "regression", "clustering"]

    if not task_type:
        return False, "Task type is required"

    if task_type not in valid_types:
        return False, f"Task type must be one of: {', '.join(valid_types)}"

    return True, None


def validate_experiment_config(
    task_type: str, target_field: Optional[str], num_classes: Optional[str], num_clusters: Optional[str]
) -> Tuple[bool, List[str]]:
    """Validate complete experiment configuration.

    Args:
        task_type: Type of task
        target_field: Target column name
        num_classes: Number of classes (string)
        num_clusters: Number of clusters (string)

    Returns:
        Tuple of (is_valid, error_messages)
    """
    errors = []

    # Validate task type
    is_valid, error = validate_task_type(task_type)
    if not is_valid:
        errors.append(error)

    # Task-specific validation
    if task_type == "regression":
        if not target_field:
            errors.append("Target column is required for regression tasks")

    elif task_type == "classification":
        has_target = bool(target_field and target_field.strip())
        has_classes = bool(num_classes and num_classes.strip())

        if not (has_target or has_classes):
            errors.append("Please provide either target column or number of classes for classification")

        # Validate number of classes if provided
        if num_classes:
            is_valid, _, error = validate_num_classes(num_classes)
            if not is_valid:
                errors.append(error)

    elif task_type == "clustering":
        # Clustering doesn't require target field
        pass

    return len(errors) == 0, errors


def get_default_target_choices(task_type: str, file_columns: List[str]) -> List[str]:
    """Get default target column choices based on task type.

    Args:
        task_type: Type of task
        file_columns: Columns available in uploaded file

    Returns:
        List of target column choices
    """
    if file_columns:
        return file_columns

    # Return examples based on task type
    if task_type == "classification":
        return CLASSIFICATION_TARGET_EXAMPLES
    elif task_type == "regression":
        return REGRESSION_TARGET_EXAMPLES
    else:
        return []


def sanitize_filename(filename: str) -> str:
    """Sanitize filename for safe storage.

    Args:
        filename: Original filename

    Returns:
        Sanitized filename
    """
    if not filename:
        return "unnamed_file"

    # Remove path components
    filename = filename.split("/")[-1]
    filename = filename.split("\\")[-1]

    # Remove problematic characters
    filename = re.sub(r'[<>:"/\\|?*]', "_", filename)
    filename = re.sub(r"\s+", "_", filename)

    # Ensure it's not empty after sanitization
    if not filename or filename == "_":
        filename = "unnamed_file"

    return filename


def validate_file_upload(file_path: str, allowed_types: List[str]) -> Tuple[bool, Optional[str]]:
    """Validate uploaded file.

    Args:
        file_path: Path to uploaded file
        allowed_types: List of allowed file extensions

    Returns:
        Tuple of (is_valid, error_message)
    """
    import os

    if not file_path or not os.path.exists(file_path):
        return False, "File not found"

    # Check file extension
    _, ext = os.path.splitext(file_path)
    if ext.lower() not in [t.lower() for t in allowed_types]:
        return False, f"File type '{ext}' not allowed. Allowed types: {', '.join(allowed_types)}"

    # Check file size (basic check)
    try:
        file_size = os.path.getsize(file_path)
        max_size = 100 * 1024 * 1024  # 100MB
        if file_size > max_size:
            return False, f"File size exceeds maximum allowed size ({max_size // (1024 * 1024)}MB)"
    except OSError:
        return False, "Unable to read file"

    return True, None
