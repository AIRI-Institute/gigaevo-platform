from __future__ import annotations

import json
import re
import shutil
import uuid
from pathlib import Path
from string import Template
from typing import Any, Dict, Optional

import pandas as pd

# Metric descriptions for task_description.txt
METRIC_DESCRIPTIONS = {
    # Binary metrics
    "exact_match": "binary accuracy based on exact string equality",
    "substring": "binary accuracy based on prediction containment in ground truth",
    # Continuous metrics (uppercase format)
    "ROUGE-1": "unigram overlap score",
    "ROUGE-2": "bigram overlap score",
    "ROUGE-L": "longest common subsequence score",
    "BERTScore": "semantic similarity using BERT embeddings",
    "BLEU": "n-gram precision score with brevity penalty",
}


def _ensure_dir(path: Path) -> None:
    """Create directory if it doesn't exist."""
    path.mkdir(parents=True, exist_ok=True)


def build_prompt_experiment(
    *,
    spec: Dict[str, Any],
    output_root: str | Path,
    template_base: str | Path,
    dataset_path: str | Path,
    experiment_id: Optional[str] = None,
) -> Path:
    """
    Build a self-contained prompt evolution problem directory.
    The directory contains dataset, prompts, config and embedded validation.

    Args:
            spec: Dict with keys:
                    - name: str
                    - description: Optional[str]
                    - data_path: str (original storage path, informational)
                    - target_column: str
                    - base_prompt: str
                    - validation_criteria: Dict[str, Any]
                    - llm_model: str
                    - max_iterations: int
            output_root: Base directory where a new experiment folder will be created
            template_base: Base directory with prompt templates
            dataset_path: Local path to dataset file to copy into the experiment folder
            experiment_id: Optional id to use for naming and storage consistency

    Returns:
            Path to the created experiment directory.
    """
    # Resolve paths
    output_root = Path(output_root)
    template_base = Path(template_base)
    # Support two layouts:
    # 1) template_base/prompt/ contains files (internal default)
    # 2) template_base/ contains files directly (external constructor path)
    template_dir = template_base / "prompt"
    if not template_dir.exists():
        # Fallback to base itself
        if template_base.exists():
            template_dir = template_base
        else:
            raise FileNotFoundError(f"Prompt template directory not found: {template_dir}")

    # Normalize experiment id for directory name
    exp_suffix = "prompt"
    exp_uuid = experiment_id or f"prompt_exp_{uuid.uuid4().hex}"
    exp_dir = output_root / f"{exp_uuid}_{exp_suffix}"
    _ensure_dir(exp_dir)

    # Create subdirectories
    dataset_dir = exp_dir / "dataset"
    _ensure_dir(dataset_dir)

    # Copy dataset into problem folder
    dataset_target = dataset_dir / "data.csv"
    shutil.copy(Path(dataset_path), dataset_target)

    # Prepare placeholders for templates
    target_column: str = spec.get("target_column") or ""
    name: str = spec.get("name") or exp_uuid
    description: str = spec.get("description") or ""
    base_prompt: str = spec.get("base_prompt") or ""
    llm_model: str = spec.get("llm_model") or "local-inference"
    prompt_llm_model: str = spec.get("prompt_llm_model") or ""
    max_iterations: int = int(spec.get("max_iterations") or 100)
    validation_criteria: Dict[str, Any] = spec.get("validation_criteria") or {}

    # Extract required fields from base_prompt
    placeholder_pattern = r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}"
    required_fields = set(re.findall(placeholder_pattern, base_prompt))
    required_fields_set_str = ", ".join([f'"{f}"' for f in sorted(required_fields)])

    # Extract validation criteria fields
    validation_type = validation_criteria.get("validation_type") or "Binary (0/1)"
    binary_method = validation_criteria.get("binary_method") or "equality"
    continuous_metric = validation_criteria.get("continuous_metric") or "ROUGE-L"
    regexp_pattern = validation_criteria.get("regexp_pattern") or ""

    # Default regexp pattern if not provided: Answer:\s*(.+?)$
    if not regexp_pattern:
        regexp_pattern = r"Answer:\s*(.+?)$"

    failure_value: float = -1.0
    failure_replacement: str = ""
    if validation_type == "Binary (0/1)":
        metric: str = "exact_match" if binary_method == "equality" else binary_method
    else:  # Continuous (0..1)
        metric: str = continuous_metric

    # Generate metric_description for task context
    metric_description: str = METRIC_DESCRIPTIONS.get(metric, metric)

    # Generate input_fields description - list ALL available fields (except target) with dtype
    try:
        df = pd.read_csv(dataset_target)
        input_columns = [col for col in df.columns if col != target_column]
        input_fields_lines = []
        for col in sorted(input_columns):
            dtype_str = str(df[col].dtype)
            input_fields_lines.append(f"  - `{col}` ({dtype_str})")
        input_fields: str = "\n".join(input_fields_lines) if input_fields_lines else "  - (no additional fields)"
    except Exception:
        # Fallback if dataset cannot be read
        input_fields = "\n".join([f"  - `{f}`" for f in sorted(required_fields)])

    # Generate required_fields_text - comma-separated fields that MUST be in prompt
    required_fields_text: str = ", ".join([f"`{f}`" for f in sorted(required_fields)])

    # Generate optional_fields_text - comma-separated fields that CAN be used
    try:
        all_available_fields = set(input_columns)  # Already computed from df.columns
        optional_fields = all_available_fields - required_fields
        optional_fields_text: str = ", ".join([f"`{f}`" for f in sorted(optional_fields)])
        if not optional_fields_text:
            optional_fields_text = ""
    except Exception:
        optional_fields_text = ""

    # Generate output_specification based on regexp_pattern
    if regexp_pattern:
        output_specification: str = f"The LLM final output must match the extraction pattern: `{regexp_pattern}`"
    else:
        output_specification = "Free-form output (no extraction pattern specified)."

    placeholders: Dict[str, Any] = {
        "experiment_id": exp_uuid,
        "task_name": exp_uuid,  # Use experiment_id as task_name for context.py
        "name": name,
        "description": description,
        "target_field": target_column,
        "dataset_abs_path": str(dataset_target.resolve()),
        "base_prompt": base_prompt,
        "llm_model": llm_model,
        "max_iterations": str(max_iterations),
        "required_fields_set": required_fields_set_str,
        # Validation criteria placeholders
        "validation_type": validation_type,
        "metric": metric,  # Unified metric name (exact_match, substring, rouge1, etc.)
        "metric_description": metric_description,
        "regexp_pattern": regexp_pattern,
        "failure_value": failure_value if failure_value is not None else "None",
        "failure_replacement": f'"{failure_replacement}"' if failure_replacement is not None else "None",
        # Task description placeholders
        "input_fields": input_fields,
        "required_fields_text": required_fields_text,
        "optional_fields_text": optional_fields_text,
        "output_specification": output_specification,
    }

    # Render template files into the problem directory
    # Note: task_hints.txt, mutation_system_prompt.txt, mutation_user_prompt.txt
    # are not needed for prompt experiments (as per emotion_v2 example)
    required_template_files = [
        "task_description.txt",
        "validate.py",
        "context.py",
        "helper.py",
        "metrics.yaml",
    ]

    for filename in required_template_files:
        src = template_dir / filename
        if not src.exists():
            raise FileNotFoundError(f"Template not found: {src}")
        content = src.read_text()
        rendered = Template(content).safe_substitute(placeholders)
        (exp_dir / filename).write_text(rendered)

    # Persist prompt config and base prompt for programmatic access
    (exp_dir / "base_prompt.txt").write_text(base_prompt)
    prompt_config = {
        "experiment_id": exp_uuid,
        "name": name,
        "description": description,
        "target_column": target_column,
        "dataset_path": "dataset/data.csv",
        "llm_model": llm_model,
        **({"prompt_llm_model": prompt_llm_model} if prompt_llm_model else {}),
        "max_iterations": max_iterations,
        "validation_criteria": validation_criteria,
    }
    (exp_dir / "prompt_config.json").write_text(json.dumps(prompt_config, indent=2, ensure_ascii=False))

    # Create initial_programs/baseline.py required by run.py
    initial_programs_dir = exp_dir / "initial_programs"
    _ensure_dir(initial_programs_dir)

    # Generate baseline.py with PROMPT_TEMPLATE from base_prompt
    # Escape triple quotes in base_prompt for Python string literal
    escaped_prompt = base_prompt.replace('"""', '\\"\\"\\"').replace("'''", "\\'\\'\\'")
    baseline_content = f'''from helper import run_agent
from context import build_context

# EVOLVE-BLOCK-START

PROMPT_TEMPLATE: str = """{escaped_prompt}"""

# EVOLVE-BLOCK-END

def entrypoint(context=None):
    ctx = context or build_context()
    return run_agent(PROMPT_TEMPLATE, ctx)
'''
    (initial_programs_dir / "baseline.py").write_text(baseline_content)

    return exp_dir
