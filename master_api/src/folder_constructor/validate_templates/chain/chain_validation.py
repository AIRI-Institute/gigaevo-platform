"""Chain specification semantic validation.

Ported from gigaevo-core-internal (problems/chains/chain_validation.py).
Structural validation (field presence, types) is handled by Pydantic models
in chain_types.py.  This module adds: DAG acyclicity, topology matching,
frozen-step equality, mode-specific rules.

Supports two evolution modes:
- static:  fixed topology, evolve structured fields of non-frozen LLM steps
- full_chain:  everything evolved (step count, types, deps, content)
"""

from __future__ import annotations

from pydantic import ValidationError

from chain_types import (
    ChainSpec,
    LLMStep,
    PromptBuilder,
    RawChainSpec,
    ToolStep,
)


def _validate_dag(steps: list[LLMStep | ToolStep]) -> None:
    """No cycles, no forward deps, all refs exist."""
    step_numbers = {s.number for s in steps}

    for step in steps:
        for dep in step.dependencies:
            if dep not in step_numbers:
                raise ValueError(
                    f"Step {step.number} depends on non-existent step {dep}"
                )
            if dep >= step.number:
                raise ValueError(f"Step {step.number} depends on later step {dep}")

    in_degree: dict[int, int] = {s.number: 0 for s in steps}
    adj: dict[int, list[int]] = {s.number: [] for s in steps}

    for step in steps:
        for dep in step.dependencies:
            in_degree[step.number] += 1
            adj[dep].append(step.number)

    queue = [n for n, d in in_degree.items() if d == 0]
    visited = 0

    while queue:
        node = queue.pop(0)
        visited += 1
        for neighbor in adj[node]:
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)

    if visited != len(steps):
        raise ValueError("Cycle detected in dependency graph")


def _check_frozen_steps(
    steps: list[LLMStep | ToolStep],
    frozen_baseline: dict,
    frozen_step_numbers: set[int],
) -> None:
    baseline_parsed = RawChainSpec.model_validate(frozen_baseline)
    baseline_by_num = {s.number: s for s in baseline_parsed.steps}

    for step in steps:
        if step.number not in frozen_step_numbers:
            continue
        baseline_step = baseline_by_num.get(step.number)
        if baseline_step is None:
            raise ValueError(f"Step {step.number} is frozen but not found in baseline")
        if step != baseline_step:
            raise ValueError(f"Step {step.number} is frozen but differs from baseline")


def _validate_static(
    steps: list[LLMStep | ToolStep],
    topology: dict | None,
    frozen_baseline: dict | None,
) -> None:
    if topology is None:
        raise ValueError("Static mode requires 'topology' parameter")

    expected_steps = topology["steps"]
    num_steps = topology["num_steps"]

    if len(steps) != num_steps:
        raise ValueError(f"Expected {num_steps} steps, got {len(steps)}")

    topo_by_number = {s["number"]: s for s in expected_steps}

    for step in steps:
        topo_step = topo_by_number.get(step.number)
        if topo_step is None:
            raise ValueError(f"Step {step.number} not found in topology")
        if step.step_type != topo_step["step_type"]:
            raise ValueError(
                f"Step {step.number} type mismatch: spec has '{step.step_type}', "
                f"topology expects '{topo_step['step_type']}'"
            )
        if sorted(step.dependencies) != sorted(topo_step["dependencies"]):
            raise ValueError(
                f"Step {step.number} dependencies mismatch: spec has "
                f"{step.dependencies}, topology expects "
                f"{topo_step['dependencies']}"
            )

    if frozen_baseline is not None:
        frozen_numbers = {s["number"] for s in expected_steps if s.get("frozen", False)}
        if frozen_numbers:
            _check_frozen_steps(steps, frozen_baseline, frozen_numbers)


def _validate_full_chain(
    steps: list[LLMStep | ToolStep],
    full_chain_config: dict | None,
) -> None:
    if full_chain_config is None:
        raise ValueError("Full-chain mode requires 'full_chain_config' parameter")

    max_steps = full_chain_config.get("max_steps", 10)
    allowed_types = set(full_chain_config.get("allowed_step_types", ["llm", "tool"]))
    available_tools = set(full_chain_config.get("available_tools", []))
    require_final_llm = full_chain_config.get("require_final_llm", True)

    if len(steps) > max_steps:
        raise ValueError(f"Too many steps: {len(steps)} > max {max_steps}")

    for step in steps:
        if step.step_type not in allowed_types:
            raise ValueError(
                f"Step {step.number} has disallowed type '{step.step_type}'"
            )

    for step in steps:
        if isinstance(step, ToolStep):
            if step.step_config.tool_name not in available_tools:
                raise ValueError(
                    f"Step {step.number} references unknown tool "
                    f"'{step.step_config.tool_name}'. Available: {available_tools}"
                )

    if require_final_llm:
        last_step = max(steps, key=lambda s: s.number)
        if last_step.step_type != "llm":
            raise ValueError(
                f"Last step (step {last_step.number}) must be 'llm' "
                f"but is '{last_step.step_type}'"
            )


def validate_chain_spec(
    raw: dict,
    mode: str = "static",
    topology: dict | None = None,
    frozen_baseline: dict | None = None,
    full_chain_config: dict | None = None,
    prompt_builder: PromptBuilder | None = None,
) -> ChainSpec:
    """Validate chain specification and build executable ChainSpec.

    Returns:
        Executable ChainSpec

    Raises:
        ValueError on validation failure
    """
    try:
        parsed = RawChainSpec.model_validate(raw)
    except ValidationError as e:
        raise ValueError(str(e)) from e

    steps = list(parsed.steps)

    numbers = [s.number for s in steps]
    if len(numbers) != len(set(numbers)):
        duplicates = [n for n in numbers if numbers.count(n) > 1]
        raise ValueError(f"Duplicate step numbers found: {sorted(set(duplicates))}")

    _validate_dag(steps)

    if mode == "static":
        _validate_static(steps, topology, frozen_baseline)
    elif mode == "full_chain":
        _validate_full_chain(steps, full_chain_config)
    else:
        raise ValueError(f"Unknown validation mode: '{mode}'")

    kwargs: dict[str, object] = {
        "system_prompt": parsed.system_prompt,
        "steps": sorted(steps, key=lambda s: s.number),
    }
    if prompt_builder is not None:
        kwargs["prompt_builder"] = prompt_builder
    return ChainSpec(**kwargs)
