"""UI helper utilities for creating common components."""

import base64
import html as html_lib
import io
import time
from typing import Any, Dict, List, Optional, Tuple

import matplotlib
import matplotlib.pyplot as plt
from config.settings import S3_API_URL, STATUS_COLORS
from loguru import logger

# Set matplotlib to use non-interactive backend
matplotlib.use("Agg")


def update_dropdown_choices(
    file_path: Optional[str], task_type: str, preset_target: Optional[str], file_columns: List[str]
) -> Tuple[bool, List[str], Optional[str]]:
    """Update dropdown choices based on file and task type.

    Args:
        file_path: Path to uploaded file
        task_type: Selected task type
        preset_target: Target from preset configuration
        file_columns: Columns from uploaded file

    Returns:
        Tuple of (visible, choices, value)
    """
    if task_type in ("classification", "regression"):
        # If we have a file, use its columns
        if file_path and file_columns:
            choices = file_columns
            # If preset_target is specified and exists in the file, use it
            # Otherwise, fall back to first column
            if preset_target and preset_target in file_columns:
                value = preset_target
            else:
                value = file_columns[0] if file_columns else None
            return True, choices, value

        # If preset is active, use preset target
        if preset_target:
            return True, [preset_target], preset_target

        # Default examples
        from config.settings import (
            CLASSIFICATION_TARGET_EXAMPLES,
            REGRESSION_TARGET_EXAMPLES,
        )

        examples = CLASSIFICATION_TARGET_EXAMPLES if task_type == "classification" else REGRESSION_TARGET_EXAMPLES
        return True, examples, examples[0] if examples else None

    # Other task types don't show target field
    return False, [], None


def create_status_blocks(status_data: Dict[str, Any]) -> str:
    """Create HTML blocks for system status components.

    Args:
        status_data: Status data dictionary

    Returns:
        HTML string with status blocks
    """
    if not status_data:
        return """
        <div style='color:#666; text-align:center; padding:20px'>
            No status data available
        </div>
        """

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
    html = f"""
    <div style="display: flex; gap: 20px; margin-bottom: 20px; flex-wrap: wrap;">
        <div style="flex: 1; min-width: 200px; color: #ffffff !important; background: {STATUS_COLORS.get(system_status, STATUS_COLORS["unknown"])};
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
            <div style="font-size: 24px; color: #ffffff !important; font-weight: bold; margin-bottom: 4px;">{uptime_text}</div>
            <div style="font-size: 12px; color: #ffffff !important; opacity: 0.8;">Time since start</div>
        </div>
    </div>

    <div style="margin-top: 30px;">
        <h3 style="margin-bottom: 15px; color: #333;">Component Status</h3>
        <div style="display: flex; gap: 15px; flex-wrap: wrap;">
    """

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


def create_metric_cards(summary_data: Dict[str, Any]) -> str:
    """Create big number blocks for key metrics.

    Args:
        summary_data: Summary data dictionary

    Returns:
        HTML string with metric cards
    """
    if not summary_data:
        return """
        <div style='color:#666; text-align:center; padding:20px'>
            No metrics available
        </div>
        """

    # Extract metrics with fallbacks
    best_fitness = summary_data.get("best_fitness")
    best_generations = summary_data.get("best_generations")
    total_iterations = summary_data.get("total_iterations")
    total_programs = summary_data.get("total_programs")
    token_usage = summary_data.get("token_usage") if isinstance(summary_data, dict) else None
    token_totals = token_usage.get("totals", {}) if isinstance(token_usage, dict) else {}
    token_models = token_usage.get("models", []) if isinstance(token_usage, dict) else []

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

    def _format_token_count(value: Any) -> str:
        if value is None:
            return "N/A"
        try:
            return f"{int(float(value)):,}"
        except (ValueError, TypeError):
            return "N/A"

    prompt_tokens_str = _format_token_count(token_totals.get("prompt_tokens"))
    completion_tokens_str = _format_token_count(token_totals.get("completion_tokens"))
    total_tokens_str = _format_token_count(token_totals.get("total_tokens"))

    has_token_usage = any(
        token_totals.get(key) is not None for key in ("prompt_tokens", "completion_tokens", "total_tokens")
    )

    # Create metric cards HTML
    html = f"""
    <div style="display: flex; gap: 20px; margin-bottom: 20px; flex-wrap: wrap;">
        <div style="flex: 1; min-width: 200px; color: #ffffff !important; background: linear-gradient(135deg, #13c1acff, #11a894ff);
                    border-radius: 12px; padding: 20px; text-align: center; color: white; box-shadow: 0 4px 6px rgba(0,0,0,0.1);">
            <div style="font-size: 14px; color: #ffffff !important; opacity: 0.9; margin-bottom: 8px;">Best Fitness</div>
            <div style="font-size: 32px; color: #ffffff !important; font-weight: bold; margin-bottom: 4px;">{fitness_str}</div>
            <div style="font-size: 12px; color: #ffffff !important; opacity: 0.8;">Performance metric</div>
        </div>

        <div style="flex: 1; min-width: 200px; color: #ffffff !important; background: linear-gradient(135deg, #8c939cff, #7a8095ff);
                    border-radius: 12px; padding: 20px; text-align: center; color: white; box-shadow: 0 4px 6px rgba(0,0,0,0.1);">
            <div style="font-size: 14px; color: #ffffff !important; opacity: 0.9; margin-bottom: 8px;">Best Generations</div>
            <div style="font-size: 32px; color: #ffffff !important; font-weight: bold; margin-bottom: 4px;">{generations_str}</div>
            <div style="font-size: 12px; color: #ffffff !important; opacity: 0.8;">Evolution rounds</div>
        </div>

        <div style="flex: 1; min-width: 200px; color: #ffffff !important; background: linear-gradient(135deg, #EDDACCFF, #BC9F89FF);
                    border-radius: 12px; padding: 20px; text-align: center; color: white; box-shadow: 0 4px 6px rgba(0,0,0,0.1);">
            <div style="font-size: 14px; color: #ffffff !important; opacity: 0.9; margin-bottom: 8px;">Total Iterations</div>
            <div style="font-size: 32px; color: #ffffff !important; font-weight: bold; margin-bottom: 4px;">{iterations_str}</div>
            <div style="font-size: 12px; color: #ffffff !important; opacity: 0.8;">Algorithm runs</div>
        </div>

        <div style="flex: 1; min-width: 200px; color: #ffffff !important; background: linear-gradient(135deg, #998FC1FF, #6250ccff);
                    border-radius: 12px; padding: 20px; text-align: center; color: white; box-shadow: 0 4px 6px rgba(0,0,0,0.1);">
            <div style="font-size: 14px; color: #ffffff !important; opacity: 0.9; margin-bottom: 8px;">Total Programs</div>
            <div style="font-size: 32px; color: #ffffff !important; font-weight: bold; margin-bottom: 4px;">{programs_str}</div>
            <div style="font-size: 12px; color: #ffffff !important; opacity: 0.8;">Programs generated</div>
        </div>
    </div>
    """

    if has_token_usage:
        html += f"""
        <div style="display: flex; gap: 20px; margin-bottom: 20px; flex-wrap: wrap;">
            <div style="flex: 1; min-width: 200px; color: #ffffff !important; background: linear-gradient(135deg, #4e8cb8, #3f7294);
                        border-radius: 12px; padding: 20px; text-align: center; color: white; box-shadow: 0 4px 6px rgba(0,0,0,0.1);">
                <div style="font-size: 14px; color: #ffffff !important; opacity: 0.9; margin-bottom: 8px;">Total Tokens</div>
                <div style="font-size: 30px; color: #ffffff !important; font-weight: bold; margin-bottom: 4px;">{total_tokens_str}</div>
                <div style="font-size: 12px; color: #ffffff !important; opacity: 0.8;">Prompt + completion</div>
            </div>

            <div style="flex: 1; min-width: 200px; color: #ffffff !important; background: linear-gradient(135deg, #7294b4, #5f7f9b);
                        border-radius: 12px; padding: 20px; text-align: center; color: white; box-shadow: 0 4px 6px rgba(0,0,0,0.1);">
                <div style="font-size: 14px; color: #ffffff !important; opacity: 0.9; margin-bottom: 8px;">Prompt Tokens</div>
                <div style="font-size: 30px; color: #ffffff !important; font-weight: bold; margin-bottom: 4px;">{prompt_tokens_str}</div>
                <div style="font-size: 12px; color: #ffffff !important; opacity: 0.8;">Input to model</div>
            </div>

            <div style="flex: 1; min-width: 200px; color: #ffffff !important; background: linear-gradient(135deg, #95a9bf, #8196ae);
                        border-radius: 12px; padding: 20px; text-align: center; color: white; box-shadow: 0 4px 6px rgba(0,0,0,0.1);">
                <div style="font-size: 14px; color: #ffffff !important; opacity: 0.9; margin-bottom: 8px;">Completion Tokens</div>
                <div style="font-size: 30px; color: #ffffff !important; font-weight: bold; margin-bottom: 4px;">{completion_tokens_str}</div>
                <div style="font-size: 12px; color: #ffffff !important; opacity: 0.8;">Generated by model</div>
            </div>
        </div>
        """

        if len(token_models) > 1:
            rows = ""
            for model in token_models:
                if not isinstance(model, dict):
                    continue
                rows += f"""
                <tr>
                    <td style="padding:6px 8px;">{html_lib.escape(str(model.get("name", "")))}</td>
                    <td style="padding:6px 8px; text-align:right;">{_format_token_count(model.get("prompt_tokens"))}</td>
                    <td style="padding:6px 8px; text-align:right;">{_format_token_count(model.get("completion_tokens"))}</td>
                    <td style="padding:6px 8px; text-align:right;">{_format_token_count(model.get("total_tokens"))}</td>
                </tr>
                """

            if rows:
                html += f"""
                <details style="margin: 4px 2px 20px 2px; color:#444;">
                    <summary style="cursor: pointer; font-size: 13px;">Token usage by model ({len(token_models)})</summary>
                    <div style="overflow-x:auto; margin-top: 8px;">
                        <table style="width:100%; border-collapse:collapse; font-size:13px;">
                            <thead>
                                <tr>
                                    <th style="text-align:left; padding:6px 8px; border-bottom:1px solid #ddd;">Model</th>
                                    <th style="text-align:right; padding:6px 8px; border-bottom:1px solid #ddd;">Prompt</th>
                                    <th style="text-align:right; padding:6px 8px; border-bottom:1px solid #ddd;">Completion</th>
                                    <th style="text-align:right; padding:6px 8px; border-bottom:1px solid #ddd;">Total</th>
                                </tr>
                            </thead>
                            <tbody>
                                {rows}
                            </tbody>
                        </table>
                    </div>
                </details>
                """

    return html


def create_pie_chart_for_programs(total_programs: int, total_programs_complete: int) -> str:
    """Create a pie chart showing program completion status using matplotlib.

    Args:
        total_programs: Total number of programs
        total_programs_complete: Number of completed programs

    Returns:
        HTML string with embedded pie chart
    """
    if total_programs is None or total_programs_complete is None:
        return """
        <div style='color:#666; text-align:center; padding:20px'>
            No program data available
        </div>
        """

    incomplete_programs = total_programs - total_programs_complete

    if total_programs == 0:
        return """
        <div style='color:#666; text-align:center; padding:20px'>
            No programs to display
        </div>
        """

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


def build_image_html(experiment_id: str, bucket_name: str) -> str:
    """Build HTML for experiment visualization image.

    Args:
        experiment_id: ID of the experiment
        bucket_name: Name of the S3 bucket

    Returns:
        HTML string with image
    """
    if not experiment_id:
        return "<div style='color:#666'>Select an experiment to view visualization</div>"

    ts = int(time.time())
    url = f"{S3_API_URL}/{bucket_name}/experiments_results/{experiment_id}/metrics_plot.png?ts={ts}"

    return f"""
    <img src='{url}'
         style='max-width:100%;border:1px solid #ddd;border-radius:4px'
         alt='Experiment visualization'
         onerror="this.onerror=null; this.alt='Visualization not available';" />
    """


def format_logs_output(logs: str, instance_id: str, lines: int) -> str:
    """Format logs output for display.

    Args:
        logs: Raw logs string
        instance_id: ID of the instance
        lines: Number of lines requested

    Returns:
        Formatted markdown string
    """
    if not logs:
        return "No logs available"

    return f"**Logs for {instance_id} (last {lines} lines):**\n\n```\n{logs}\n```"


def create_error_display(message: str) -> str:
    """Create standardized error display.

    Args:
        message: Error message to display

    Returns:
        HTML string for error display
    """
    return f"""
    <div style="background: #ef4444ff; border-radius: 8px; padding: 15px; margin: 10px 0; color: white;">
        <strong>Error:</strong> {message}
    </div>
    """


def create_success_display(message: str) -> str:
    """Create standardized success display.

    Args:
        message: Success message to display

    Returns:
        HTML string for success display
    """
    return f"""
    <div style="background: #13c1acff; border-radius: 8px; padding: 15px; margin: 10px 0; color: white;">
        <strong>Success:</strong> {message}
    </div>
    """
