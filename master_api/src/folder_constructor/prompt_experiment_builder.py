from __future__ import annotations

import json
import re
import shutil
import uuid
from pathlib import Path
from string import Template
from typing import Any, Dict, Optional


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
    max_iterations: int = int(spec.get("max_iterations") or 100)
    validation_criteria: Dict[str, Any] = spec.get("validation_criteria") or {}

    # Derive metric placeholders based on validation criteria
    metric_import: str = ""
    custom_metric_impl: str = ""
    metric_fn: str = "accuracy_score"
    failure_value: str | int | float | None = None
    failure_replacement: str | int | float | None = None

    # Determine agent class and metric based on task_type or validation_criteria
    task_type = spec.get("task_type") or ""
    vtype = str(validation_criteria.get("validation_type") or "").lower()

    # Extract required fields from base_prompt
    placeholder_pattern = r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}"
    required_fields = set(re.findall(placeholder_pattern, base_prompt))
    required_fields_set_str = ", ".join([f'"{f}"' for f in sorted(required_fields)])

    # Determine agent class based on task_type
    if task_type == "multi_choice" or (not task_type and "options" in required_fields):
        agent_class = "MultiChoiceAgent"
    elif task_type == "classification":
        agent_class = "ClassificationAgent"
    elif task_type == "math":
        agent_class = "BoxedAnswerAgent"
    elif task_type == "summarization":
        agent_class = "SummarizationAgent"
    else:
        # Default to MultiChoiceAgent if unknown
        agent_class = "MultiChoiceAgent"

    # Determine metric based on validation type
    if vtype.startswith("binary"):
        # Binary classification defaults
        metric_import = "from sklearn.metrics import accuracy_score"
        metric_fn = "accuracy_score"
        failure_value = -1.0
        failure_replacement = 0.0
        metric_name = "Accuracy"
    else:
        # Continuous proxy metric (lightweight ROUGE-L-like token set similarity)
        metric_import = ""
        metric_fn = "rouge_l_score"
        custom_metric_impl = r"""
def rouge_l_score(references, predictions):
    import re
    def _token_set_similarity(a: str, b: str) -> float:
        ta = {t for t in re.findall(r"\w+", str(a).lower()) if t}
        tb = {t for t in re.findall(r"\w+", str(b).lower()) if t}
        if not ta or not tb:
            return 0.0
        inter = len(ta & tb)
        union = len(ta | tb)
        return inter / union if union else 0.0
    scores = []
    for ref, pred in zip(references, predictions):
        scores.append(_token_set_similarity(ref, pred))
    return sum(scores) / len(scores) if scores else 0.0
""".strip("\n")
        failure_value = None
        failure_replacement = None
        metric_name = "ROUGE-L"

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
        "metric_import": metric_import,
        "custom_metric_impl": custom_metric_impl,
        "metric_fn": metric_fn,
        "metric_name": metric_name,
        "failure_value": failure_value if failure_value is not None else "None",
        "failure_replacement": failure_replacement if failure_replacement is not None else "None",
        "required_fields_set": required_fields_set_str,
        "agent_class": agent_class,
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
