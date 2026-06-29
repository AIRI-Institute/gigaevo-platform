from __future__ import annotations

from pathlib import Path
from string import Template


class ProblemLayout:
    """Standardized problem directory layout for prompt evolution tasks."""

    # Filenames
    TASK_DESCRIPTION = "task_description.txt"
    VALIDATOR = "validate.py"
    CONTEXT_FILE = "context.py"
    METRICS_FILE = "metrics.yaml"

    # Directories
    INITIAL_PROGRAMS_DIR = "initial_programs"

    @classmethod
    def scaffold_prompt(
        cls,
        task_name: str,
        placeholders: dict[str, str],
        template_dir: Path = Path("problems/promptevolve/templates"),
        output_dir: Path = Path("problems/promptevolve"),
        overwrite: bool = False,
        baseline_content: str | None = None,
    ) -> None:
        """
        Create scaffold for prompt evolution task using templates.

        Args:
            task_name: Task name (used as directory name)
            placeholders: Placeholder values for template substitution (${key} -> value)
            template_dir: Directory containing template files
            output_dir: Base output directory
            overwrite: Whether to overwrite existing files
            baseline_content: Optional baseline program content to write to initial_programs/baseline.py
        """
        target_dir = output_dir / task_name
        target_dir.mkdir(parents=True, exist_ok=True)

        # Create dataset directory
        dataset_dir = target_dir / "dataset"
        dataset_dir.mkdir(parents=True, exist_ok=True)

        # Create initial_programs directory
        initial_programs_dir = target_dir / cls.INITIAL_PROGRAMS_DIR
        initial_programs_dir.mkdir(parents=True, exist_ok=True)

        # Add task_name to placeholders for templates that need it
        placeholders = {**placeholders, "task_name": task_name}

        # Format required_input_fields for different contexts
        required_fields = placeholders.get("required_input_fields", [])
        if required_fields:
            # For Python set literal: {"field1", "field2"}
            placeholders["required_fields_set"] = ", ".join(f'"{f}"' for f in required_fields)
            # For readable text: `field1`, `field2`
            placeholders["required_fields_text"] = ", ".join(f"`{f}`" for f in required_fields)
        else:
            placeholders["required_fields_set"] = ""
            placeholders["required_fields_text"] = "none"

        # All template files to process
        template_files = [
            cls.TASK_DESCRIPTION,
            cls.METRICS_FILE,
            cls.VALIDATOR,
            cls.CONTEXT_FILE,
            "helper.py",  # Task-specific helper with agent selection
        ]

        # Process all template files using string.Template
        for filename in template_files:
            template_path = template_dir / filename
            if not template_path.exists():
                raise FileNotFoundError(f"Template not found: {template_path}")

            template_content = template_path.read_text()
            filled_content = Template(template_content).safe_substitute(placeholders)

            output_path = target_dir / filename
            if output_path.exists() and not overwrite:
                continue
            output_path.write_text(filled_content)

        # Write baseline program if provided
        if baseline_content:
            baseline_output = initial_programs_dir / "baseline.py"
            if not baseline_output.exists() or overwrite:
                baseline_output.write_text(baseline_content)
