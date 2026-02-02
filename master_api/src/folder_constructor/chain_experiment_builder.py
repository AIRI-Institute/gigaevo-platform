from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path
from string import Template
from typing import Any, Dict, Optional


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def build_chain_experiment(
    *,
    spec: Dict[str, Any],
    output_root: str | Path,
    template_base: str | Path,
    dataset_path: str | Path,
    experiment_id: Optional[str] = None,
) -> Path:
    output_root = Path(output_root)
    template_base = Path(template_base)
    template_dir = template_base / "chain"
    if not template_dir.exists():
        if template_base.exists():
            template_dir = template_base
        else:
            raise FileNotFoundError(f"Chain template directory not found: {template_dir}")

    exp_suffix = "chain"
    exp_uuid = experiment_id or f"chain_exp_{uuid.uuid4().hex}"
    exp_dir = output_root / f"{exp_uuid}_{exp_suffix}"
    _ensure_dir(exp_dir)

    dataset_dir = exp_dir / "dataset"
    _ensure_dir(dataset_dir)

    dataset_target = dataset_dir / "data.csv"
    shutil.copy(Path(dataset_path), dataset_target)

    target_column: str = spec.get("target_column") or ""
    name: str = spec.get("name") or exp_uuid
    description: str = spec.get("description") or ""
    base_chain_config: str = spec.get("base_chain_config") or "{}"
    llm_model: str = spec.get("llm_model") or "local-inference"
    max_iterations: int = int(spec.get("max_iterations") or 100)
    validation_criteria: Dict[str, Any] = spec.get("validation_criteria") or {}
    evolution_mode: str = spec.get("evolution_mode", "full_chain") or "full_chain"
    step_number: Optional[int] = spec.get("step_number")

    try:
        chain_config = json.loads(base_chain_config)
        steps = chain_config.get("steps", [])
        num_steps = len(steps)
        step_titles = [step.get("title", f"Step {i + 1}") for i, step in enumerate(steps)]
        step_titles_text = (
            "\n".join([f"  - {title}" for title in step_titles]) if step_titles else "  - (no steps defined)"
        )
        
        target_step = None
        target_step_info = ""
        if evolution_mode == "single_step" and step_number is not None:
            step_idx = int(step_number) - 1
            if 0 <= step_idx < len(steps):
                target_step = steps[step_idx]
                target_step_info = f"Step {step_number}: {target_step.get('title', 'Unknown')}"
    except Exception:
        num_steps = 0
        step_titles_text = "  - (invalid chain config)"
        target_step = None
        target_step_info = ""

    validation_type = validation_criteria.get("validation_type") or "Binary (0/1)"
    binary_method = validation_criteria.get("binary_method") or "equality"
    continuous_metric = validation_criteria.get("continuous_metric") or "ROUGE-L"
    regexp_pattern = validation_criteria.get("regexp_pattern") or ""

    if not regexp_pattern:
        regexp_pattern = r"Answer:\s*(.+?)$"

    if validation_type == "Binary (0/1)":
        metric: str = "exact_match" if binary_method == "equality" else binary_method
    else:
        metric = continuous_metric

    placeholders: Dict[str, Any] = {
        "experiment_id": exp_uuid,
        "task_name": exp_uuid,
        "name": name,
        "description": description,
        "target_field": target_column,
        "dataset_abs_path": str(dataset_target.resolve()),
        "base_chain_config": base_chain_config,
        "llm_model": llm_model,
        "max_iterations": str(max_iterations),
        "num_steps": str(num_steps),
        "step_titles": step_titles_text,
        "validation_type": validation_type,
        "metric": metric,
        "regexp_pattern": regexp_pattern,
        "evolution_mode": evolution_mode,
        "step_number": str(step_number) if step_number else "",
        "target_step_info": target_step_info,
        "target_step_json": json.dumps(target_step, indent=2) if target_step else "{}",
    }

    if evolution_mode == "single_step" and step_number:
        task_description_file = "task_description_step.txt"
    else:
        task_description_file = "task_description.txt"
    
    required_template_files = [
        task_description_file,
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
        output_filename = "task_description.txt" if filename == task_description_file else filename
        (exp_dir / output_filename).write_text(rendered)

    (exp_dir / "base_chain_config.json").write_text(base_chain_config)
    chain_config_dict = {
        "experiment_id": exp_uuid,
        "name": name,
        "description": description,
        "target_column": target_column,
        "dataset_path": "dataset/data.csv",
        "llm_model": llm_model,
        "max_iterations": max_iterations,
    }
    (exp_dir / "chain_config.json").write_text(json.dumps(chain_config_dict, indent=2, ensure_ascii=False))

    initial_programs_dir = exp_dir / "initial_programs"
    _ensure_dir(initial_programs_dir)

    escaped_config = base_chain_config.replace('"""', '\\"\\"\\"').replace("'''", "\\'\\'\\'")
    
    if evolution_mode == "single_step" and step_number and target_step:
        target_step_escaped = json.dumps(target_step, indent=2).replace('"""', '\\"\\"\\"').replace("'''", "\\'\\'\\'")
        baseline_content = f'''import json

# EVOLVE-BLOCK-START

BASE_CHAIN_CONFIG: str = """{escaped_config}""".strip()

# You must evolve ONLY step {step_number} while keeping all other steps unchanged.
# Parse the base config, modify only step {step_number}, and return the complete chain.
import json as _json
_base = _json.loads(BASE_CHAIN_CONFIG)
_steps = _base.get("steps", [])
# Keep all steps except step {step_number}
_other_steps = [_s for _s in _steps if _s.get("number") != {step_number}]
# Evolve step {step_number} here - modify the step configuration to improve performance
_target_step = {target_step_escaped}
# TODO: Evolve _target_step fields (title, aim, reasoning_questions, stage_action, example_reasoning, step_context_queries)
# but keep number={step_number} and dependencies unchanged
_evolved_step = _target_step.copy()
# Reconstruct chain with evolved step
_evolved_steps = sorted(_other_steps + [_evolved_step], key=lambda s: s.get("number", 0))
_evolved_chain = {{"steps": _evolved_steps}}
if "search_config" in _base:
    _evolved_chain["search_config"] = _base["search_config"]
if "max_workers" in _base:
    _evolved_chain["max_workers"] = _base["max_workers"]
if "enable_progress" in _base:
    _evolved_chain["enable_progress"] = _base["enable_progress"]

CHAIN_CONFIG_JSON: str = _json.dumps(_evolved_chain, indent=2)

# EVOLVE-BLOCK-END

def entrypoint(context=None):
    return {{"chain_config_json": CHAIN_CONFIG_JSON}}
'''
    else:
        baseline_content = f'''import json

# EVOLVE-BLOCK-START

CHAIN_CONFIG_JSON: str = """{escaped_config}""".strip()

# EVOLVE-BLOCK-END

def entrypoint(context=None):
    return {{"chain_config_json": CHAIN_CONFIG_JSON}}
'''
    (initial_programs_dir / "baseline.py").write_text(baseline_content)

    return exp_dir
