"""
Bridge to mmar_carl: native ReasoningChain + DAGExecutor + per-step executors.

Uses the library's own deserialization and execution so all step types
(LLM, TOOL, MCP, MEMORY, TRANSFORM, CONDITIONAL, STRUCTURED_OUTPUT) run
through mmar_carl.step_executors with no duplicate TOOL preflight in helper.py.
"""

from __future__ import annotations

from typing import Any, Dict

_RUNTIME_LOGGED = False


def log_mmar_carl_runtime_info() -> None:
    """Log once which step executors are registered in the installed mmar_carl."""
    global _RUNTIME_LOGGED
    if _RUNTIME_LOGGED:
        return
    _RUNTIME_LOGGED = True
    try:
        from mmar_carl.models.enums import StepType
        from mmar_carl.step_executors import get_executor

        lines = []
        for st in StepType:
            try:
                ex = get_executor(st)
                lines.append(f"  {st.value}: {type(ex).__name__}")
            except Exception as e:
                lines.append(f"  {st.value}: (error: {e})")
        print(
            "[mmar_carl_bridge] Registered CARL step executors:\n" + "\n".join(lines),
            flush=True,
        )
    except Exception as e:
        print(f"[mmar_carl_bridge] Could not introspect mmar_carl executors: {e}", flush=True)


def _normalize_step_type_value(raw: Any) -> str:
    if raw is None:
        return "llm"
    s = str(raw).strip().lower()
    return s if s else "llm"


def _ensure_llm_defaults(step: Dict[str, Any]) -> None:
    """Fill minimal LLM fields so LLMStepDescription validation passes."""
    st = _normalize_step_type_value(step.get("step_type"))
    if st != "llm":
        return
    if not (step.get("aim") or "").strip():
        step["aim"] = "Solve the given task"
    if not (step.get("reasoning_questions") or "").strip():
        step["reasoning_questions"] = "What is the key information?"
    if not (step.get("stage_action") or "").strip():
        step["stage_action"] = "Reason step-by-step and provide the answer."
    if "step_context_queries" not in step or not step["step_context_queries"]:
        step["step_context_queries"] = ["problem"]


def prepare_chain_dict_for_library(chain_config: Dict[str, Any]) -> Dict[str, Any]:
    """Copy chain config and normalize fields for ReasoningChain.from_dict."""
    import copy

    data = copy.deepcopy(chain_config)
    steps = data.get("steps") or []
    out_steps: list[Dict[str, Any]] = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        s = dict(step)
        s["step_type"] = _normalize_step_type_value(s.get("step_type"))
        _ensure_llm_defaults(s)
        out_steps.append(s)
    data["steps"] = out_steps
    return data


def build_reasoning_chain_from_config(chain_config: Dict[str, Any]) -> Any:
    """
    Build ReasoningChain using mmar_carl's native parser (all step types).

    Typed steps + library DAGExecutor route each step to the matching executor
    (LLMStepExecutor, ToolStepExecutor, ...).
    """
    from mmar_carl import ReasoningChain

    log_mmar_carl_runtime_info()
    prepared = prepare_chain_dict_for_library(chain_config)
    # Library API: typed steps avoid deprecated StepDescription wrapper warnings.
    return ReasoningChain.from_dict(prepared, use_typed_steps=True)
