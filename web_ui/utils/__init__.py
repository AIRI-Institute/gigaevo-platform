"""Utility functions for the web UI."""

from .file_handlers import (
    download_preset_dataset,
    extract_source_path_from_upload,
    read_csv_columns,
)
from .formatters import (
    extract_experiment_id_from_selector,
    format_experiments_table,
    format_instance_details,
    format_instances_table,
)
from .ui_helpers import (
    build_image_html,
    create_metric_cards,
    create_pie_chart_for_programs,
    create_status_blocks,
    update_dropdown_choices,
)
from .validators import (
    validate_experiment_name,
    validate_log_lines,
    validate_max_iterations,
    validate_num_clusters,
)

__all__ = [
    "extract_source_path_from_upload",
    "read_csv_columns",
    "download_preset_dataset",
    "extract_experiment_id_from_selector",
    "format_experiments_table",
    "format_instances_table",
    "format_instance_details",
    "validate_experiment_name",
    "validate_max_iterations",
    "validate_log_lines",
    "validate_num_clusters",
    "update_dropdown_choices",
    "create_status_blocks",
    "create_metric_cards",
    "create_pie_chart_for_programs",
    "build_image_html",
]
