"""Data formatting utilities for the web UI."""

import json
from typing import Any, Dict, List, Tuple

import pandas as pd


def extract_experiment_id_from_selector(selector_value: str) -> str:
    """Extract experiment ID from selector string in format 'Name (ID)'.

    Args:
        selector_value: Selector string

    Returns:
        Experiment ID string
    """
    if not selector_value:
        return ""

    # Extract ID from format "Name (ID)"
    if "(" in selector_value and ")" in selector_value:
        return selector_value.split("(")[-1].strip(")")

    # Fallback: treat as ID directly if no parentheses
    return selector_value


def format_experiments_table(experiments: List[Dict[str, Any]]) -> pd.DataFrame:
    """Format experiments list as pandas DataFrame for display.

    Args:
        experiments: List of experiment dictionaries

    Returns:
        Formatted DataFrame
    """
    if not experiments:
        return pd.DataFrame(columns=["ID", "Name", "Status", "Status Message", "Created", "Progress"])

    data = []
    for exp in experiments:
        progress = exp.get("metrics", {}).get("progress", 0)
        status_msg = exp.get("status_message") or ""
        if not status_msg and str(exp.get("status") or "").lower() == "preparation_failed":
            status_msg = exp.get("error_message") or ""
        data.append(
            {
                "ID": str(exp["id"]),
                "Name": exp["name"],
                "Status": exp["status"],
                "Status Message": status_msg,
                "Created": exp["created_at"][:19] if exp["created_at"] else "N/A",
                "Progress": f"{progress:.1f}%",
            }
        )

    return pd.DataFrame(data)


def format_instances_table(instances: List[Dict[str, Any]]) -> pd.DataFrame:
    """Format instances list as pandas DataFrame for display.

    Args:
        instances: List of instance dictionaries

    Returns:
        Formatted DataFrame
    """
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


def format_instance_details(instance: Dict[str, Any]) -> str:
    """Format instance details for display.

    Args:
        instance: Instance dictionary

    Returns:
        Formatted markdown string
    """
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


def format_experiment_details(experiment: Dict[str, Any]) -> Tuple[str, str]:
    """Format experiment details and metrics for display.

    Args:
        experiment: Experiment dictionary

    Returns:
        Tuple of (details_text, metrics_text)
    """
    config = experiment.get("config", {})
    details = f"""
    **Experiment Details**

    **Name:** {experiment["name"]}
    **Status:** {experiment["status"]}
    **Created:** {experiment["created_at"][:19] if experiment["created_at"] else "N/A"}

    **Configuration:**
    - LLM Model: {config.get("llm_model", "N/A")}
    - Max Iterations: {config.get("max_iterations", "N/A")}
    - Timeout: {config.get("timeout_seconds", "N/A")}s
    """

    # Add dataset configuration if available
    dataset_size = config.get("dataset_size")
    test_size = config.get("test_size")
    if dataset_size is not None or test_size is not None:
        details += "\n    **Dataset Configuration:**\n"
        if dataset_size is not None:
            details += f"    - Dataset Size: {dataset_size:,} rows\n"
        if test_size is not None:
            test_size_pct = test_size * 100
            train_size_pct = (1.0 - test_size) * 100
            details += f"    - Test Size: {test_size} ({test_size_pct:.1f}%)\n"

            # Calculate train/test split if dataset_size is available
            if dataset_size is not None:
                train_rows = int(dataset_size * (1.0 - test_size))
                test_rows = int(dataset_size * test_size)
                details += f"    - **Train/Test Split:**\n"
                details += f"      - Train: {train_rows:,} rows ({train_size_pct:.1f}%)\n"
                details += f"      - Test: {test_rows:,} rows ({test_size_pct:.1f}%)\n"

    details += f"\n    **Data Path:** {experiment.get('data_path', 'N/A')}"

    if experiment.get("error_message"):
        details += f"\n\n    **Error:** {experiment['error_message']}"

    metrics = experiment.get("metrics", {})
    metrics_text = json.dumps(metrics, indent=2) if metrics else "No metrics available"

    return details, metrics_text


def format_health_summary(health_data: Dict[str, Any]) -> str:
    """Format health summary for display.

    Args:
        health_data: Health summary dictionary

    Returns:
        Formatted markdown string
    """
    if "error" in health_data:
        return f"Error: {health_data['error']}"

    summary = f"""
    **Instance Health Summary**

    **Total Instances:** {health_data.get("total_instances", 0)}
    **Healthy Instances:** {health_data.get("healthy_instances", 0)}
    **Unhealthy Instances:** {health_data.get("unhealthy_instances", 0)}
    **Busy Instances:** {health_data.get("busy_instances", 0)}
    **Ready Instances:** {health_data.get("ready_instances", 0)}
    **Offline Instances:** {health_data.get("offline_instances", 0)}

    **Instance Details:**
    """

    instances_detail = health_data.get("instances_detail", [])
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


def build_experiment_selector_choices(experiments: List[Dict[str, Any]]) -> List[str]:
    """Build choices for experiment selector dropdown.

    Args:
        experiments: List of experiment dictionaries

    Returns:
        List of formatted choice strings
    """
    if not experiments:
        return []

    return [f"{exp.get('name')} ({exp.get('id')})" for exp in experiments]


def build_instance_selector_choices(instances: List[Dict[str, Any]]) -> List[str]:
    """Build choices for instance selector dropdown.

    Args:
        instances: List of instance dictionaries

    Returns:
        List of instance IDs
    """
    if not instances:
        return []

    return [inst["id"] for inst in instances]
