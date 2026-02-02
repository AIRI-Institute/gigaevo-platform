from __future__ import annotations

import argparse
import json
import shutil
import tempfile
import urllib.request
import uuid
from pathlib import Path
from string import Template
from typing import Any, Dict, List, Optional

import pandas as pd

TASK_TYPES = (
    "classification",
    "regression",
    "clustering",
    "classification_automl",
    "regression_automl",
    "clustering_automl",
    "classification_catboost",
)


def _default_output_root() -> Path:
    # master_api/temp_experiments relative to this file
    return Path(__file__).resolve().parent.parent / "temp_experiments"


def _default_template_base() -> Path:
    # master_api/folder_constructor/validate_templates
    return Path(__file__).resolve().parent / "validate_templates"


def _infer_schema_fields(df: pd.DataFrame, exclude_target: Optional[str]) -> tuple[str, List[str]]:
    fields: List[str] = []
    column_names: List[str] = []
    for col in df.columns:
        if exclude_target and col == exclude_target:
            continue
        dtype = df[col].dtype
        if dtype == "object":
            type_str = "str"
        elif str(dtype).startswith("int"):
            type_str = "int"
        elif str(dtype).startswith("float"):
            type_str = "float"
        elif str(dtype) == "bool":
            type_str = "bool"
        else:
            type_str = str(dtype)
        fields.append(f"    - {col} ({type_str})")
        column_names.append(col)
    return "\n".join(fields), column_names


def build_uuid_experiment(
    *,
    spec: Dict[str, Any],
    output_root: str | Path,
    template_base: str | Path,
    dataset: Optional[pd.DataFrame] = None,
    dataset_path: Optional[str | Path] = None,
) -> Path:
    """
    Create an experiment directory named by UUID using ML templates.

    Args:
        spec: Parsed spec with keys: task_type, task_description, target_column/target_field (optional), n_clusters (optional).
        output_root: Directory where the experiment folder will be created.
        template_base: Base directory with task templates (classification/regression/clustering).
        dataset: Optional in-memory DataFrame to persist as CSV. If provided, takes precedence over dataset_path.
        dataset_path: Optional path or URL to CSV. Used if dataset is None.

    Returns:
        Path to the created experiment directory.
    """
    task_type: str = spec.get("task_type")
    task_description: str = spec.get("task_description")
    # Accept both keys; prefer target_column if present
    target_field: Optional[str] = spec.get("target_column") or spec.get("target_field")
    n_clusters: int = int(spec.get("n_clusters", 3))

    if task_type not in TASK_TYPES:
        raise ValueError(f"Unsupported task_type: {task_type}")
    if not task_description:
        raise ValueError("spec.task_description is required")

    output_root = Path(output_root)
    template_base = Path(template_base)
    template_dir = template_base / task_type
    if not template_dir.exists():
        raise FileNotFoundError(f"Template directory not found: {template_dir}")

    experiment_id = uuid.uuid4().hex
    # Prefix with 'exp_' to align with external problem naming conventions
    exp_dir = output_root / f"{experiment_id}_{task_type}"
    # exp_dir = output_root / f"exp_{experiment_id}_{task_type}"
    exp_dir.mkdir(parents=True, exist_ok=True)

    dataset_dir = exp_dir / "dataset"
    dataset_dir.mkdir(parents=True, exist_ok=True)
    initial_programs_dir = exp_dir / "initial_programs"
    initial_programs_dir.mkdir(parents=True, exist_ok=True)

    dataset_target = dataset_dir / "data.csv"

    # Load dataset and compute schema
    if dataset is not None:
        df = dataset
    else:
        if not dataset_path:
            raise ValueError("Either dataset (DataFrame) or dataset_path must be provided")
        ds_str = str(dataset_path)
        if ds_str.startswith("http://") or ds_str.startswith("https://"):
            with tempfile.NamedTemporaryFile(delete=False) as tmp:
                urllib.request.urlretrieve(ds_str, tmp.name)
                df = pd.read_csv(tmp.name)
        else:
            df = pd.read_csv(ds_str)

    # Validate target field presence for supervised tasks
    if task_type in (
        "classification",
        "regression",
        "classification_automl",
        "regression_automl",
        "classification_catboost",
    ):
        if not target_field:
            raise ValueError(
                "Target column is required for supervised tasks. "
                "Provide 'target_column' (preferred) or 'target_field' in spec."
            )
        if target_field not in df.columns:
            raise ValueError(
                f"Target column '{target_field}' not found in dataset. Available columns: {list(df.columns)}"
            )

    # Map internal variants (e.g. *_automl, *_catboost) to their base task type
    base_task_type = task_type
    for suffix in ("_automl", "_catboost"):
        if base_task_type.endswith(suffix):
            base_task_type = base_task_type[: -len(suffix)]
            break

    exclude = target_field if base_task_type in ("classification", "regression") else None
    input_fields_formatted, input_columns = _infer_schema_fields(df, exclude)

    # Compute fitness bounds per task type (used by metrics.yaml).
    # AutoML variants share the same bounds as their base task type.
    min_max = {
        "classification": (0.0, 1.0),
        "regression": (0.0, 1.0),
        "clustering": (-1.0, 1.0),
    }[base_task_type]

    placeholders = {
        "task_description": task_description,
        "task_hints": "Keep models simple and robust; ensure inference efficiency.",
        "target_field": target_field or "",
        "input_fields": input_fields_formatted,
        "required_input_fields": input_columns,
        "n_clusters": str(n_clusters),
        "dataset_abs_path": str(dataset_target.resolve()),
        "fitness_lower_bound": str(min_max[0]),
        "fitness_upper_bound": str(min_max[1]),
    }

    required_template_files = [
        "task_description.txt",
        "task_hints.txt",
        "mutation_system_prompt.txt",
        "mutation_user_prompt.txt",
        "validate.py",
        "context.py",
        # New metrics spec file rendered into each problem directory
        "metrics.yaml",
    ]

    for filename in required_template_files:
        src = template_dir / filename
        if not src.exists():
            raise FileNotFoundError(f"Template not found: {src}")
        content = src.read_text()
        rendered = Template(content).safe_substitute(placeholders)
        (exp_dir / filename).write_text(rendered)

    src_baseline = template_dir / "initial_programs" / "baseline.py"
    src_feature_selector = template_dir / "initial_programs" / "feature_selector.py"
    if src_baseline.exists():
        baseline_content = src_baseline.read_text()
    elif src_feature_selector.exists():
        baseline_content = src_feature_selector.read_text()
    else:
        raise FileNotFoundError(f"Initial program not found: {src_baseline} or {src_feature_selector}")
    baseline_rendered = Template(baseline_content).safe_substitute(placeholders)
    (initial_programs_dir / "baseline.py").write_text(baseline_rendered)

    # Persist dataset
    if dataset is not None:
        df.to_csv(dataset_target, index=False)
    else:
        ds_str = str(dataset_path)
        if ds_str.startswith("http://") or ds_str.startswith("https://"):
            with tempfile.NamedTemporaryFile(delete=False) as tmp:
                urllib.request.urlretrieve(ds_str, tmp.name)
                shutil.copy(tmp.name, dataset_target)
        else:
            shutil.copy(Path(ds_str), dataset_target)

    return exp_dir


def _cli() -> None:
    parser = argparse.ArgumentParser(
        "UUID Experiment Builder",
        description="Create a UUID-named experiment directory from a JSON spec and CSV",
    )
    parser.add_argument(
        "--spec-json",
        type=Path,
        required=True,
        help="Path to JSON spec (task_type, task_description, [target_field], [n_clusters], [dataset_path])",
    )
    parser.add_argument(
        "--dataset-path",
        type=str,
        default=None,
        help="Optional CSV path or URL; overrides spec dataset_path",
    )
    parser.add_argument(
        "--template-base",
        type=str,
        default=str(_default_template_base()),
        help="Base directory with ML templates (classification/regression/clustering)",
    )
    parser.add_argument(
        "--output-root",
        type=str,
        default=str(_default_output_root()),
        help="Directory where the experiment folder will be created",
    )

    args = parser.parse_args()

    with open(args.spec_json, "r") as f:
        spec = json.load(f)

    ds_path = args.dataset_path or spec.get("dataset_path")
    # Resolve relative dataset paths relative to spec file location
    if isinstance(ds_path, str) and ds_path and not ds_path.lower().startswith(("http://", "https://")):
        ds_path_obj = Path(ds_path)
        if not ds_path_obj.is_absolute():
            ds_path = str((args.spec_json.parent / ds_path_obj).resolve())

    # Ensure output root exists
    Path(args.output_root).mkdir(parents=True, exist_ok=True)

    exp_dir = build_uuid_experiment(
        spec=spec,
        output_root=args.output_root,
        template_base=args.template_base,
        dataset=None,
        dataset_path=ds_path,
    )
    print(f"\n✓ Experiment created at {exp_dir}")


if __name__ == "__main__":
    _cli()
