"""Step-batched chain execution engine.

Ported from gigaevo-core-internal (problems/chains/chain_runner.py).
All samples process each step together, yielding homogeneous LLM request
batches for efficient vLLM batching.
"""

from __future__ import annotations

import asyncio
import re
import sys
import time
from collections.abc import Callable

from chain_types import (
    ChainResult,
    ChainSpec,
    LLMStep,
    StopCondition,
    ToolStep,
)


def _strip_thinking(text: str) -> str:
    """Strip <think>...</think> blocks from LLM output.

    Handles well-formed tags and truncated blocks (max_tokens cutoff).
    """
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    text = re.sub(r"<think>.*", "", text, flags=re.DOTALL)
    return text.strip()


def _resolve_reference(
    ref: str,
    outer_context: str,
    step_outputs: list[str],
    sample: dict | None = None,
) -> str:
    """Resolve a $-reference to a concrete value.

    Supported syntax:
        $outer_context  — the original sample context string
        $history[-1]    — last completed step's output
        $history[N]     — step output at history index N (0-based)
        $sample.foo     — field from the current sample dict (dot path)
    """
    if ref == "$outer_context":
        return outer_context

    if ref == "$history[-1]":
        return step_outputs[-1] if step_outputs else ""

    if ref.startswith("$sample."):
        if sample is None:
            return ""
        value: object = sample
        for part in ref[len("$sample."):].split("."):
            if isinstance(value, dict) and part in value:
                value = value[part]
            else:
                return ""
        if value is None:
            return ""
        return value if isinstance(value, str) else str(value)

    match = re.match(r"\$history\[(\d+)\]", ref)
    if match:
        idx = int(match.group(1))
        return step_outputs[idx] if idx < len(step_outputs) else ""

    raise ValueError(f"Unknown reference syntax: {ref}")


def _resolve_dependencies(
    step_deps: list[int],
    history: list[str],
    step_outputs: list[str],
) -> tuple[list[str], dict[int, str]]:
    """Resolve dependency-filtered history and outputs for a step.

    Empty deps = all prior steps are visible.
    """
    n_completed = len(step_outputs)

    if not step_deps:
        visible_history = history[:n_completed]
        visible_outputs = {i + 1: step_outputs[i] for i in range(n_completed)}
    else:
        visible_history = []
        visible_outputs = {}
        for dep in step_deps:
            idx = dep - 1
            if 0 <= idx < n_completed:
                visible_history.append(history[idx])
                visible_outputs[dep] = step_outputs[idx]

    return visible_history, visible_outputs


def _matches_stop_condition(output: str, condition: StopCondition | None) -> bool:
    if condition is None:
        return False
    if condition.condition_type == "contains":
        if condition.case_sensitive:
            return condition.pattern in output
        return condition.pattern.lower() in output.lower()
    flags = 0 if condition.case_sensitive else re.IGNORECASE
    return re.search(condition.pattern, output, flags=flags) is not None


def _log(msg: str) -> None:
    sys.__stderr__.write(f"[chain_runner] {msg}\n")
    sys.__stderr__.flush()


async def _run_chain_on_dataset_stepwise(
    chain: ChainSpec,
    client: object,
    dataset: list[dict],
    outer_context_builder: Callable[[dict], str],
    tool_registry: dict[str, Callable] | None = None,
    batch_tool_registry: dict[str, Callable] | None = None,
    step_max_tokens: dict[int, int] | None = None,
    max_concurrent: int = 300,
) -> list[ChainResult]:
    """Step-batched execution: all samples process each step together.

    Processes ALL samples through step 1, then ALL through step 2, etc.
    This yields homogeneous LLM request batches (same prompt structure and
    similar length) which vLLM can batch far more efficiently.

    Args:
        chain: Validated ChainSpec
        client: LLMClient instance (must have async __call__ and .copy())
        dataset: List of sample dicts
        outer_context_builder: Builds data context string from sample dict
        tool_registry: tool_name -> callable(**kwargs) -> str  (individual)
        batch_tool_registry: tool_name -> callable(list[dict]) -> list[str]
        step_max_tokens: step_number -> max_tokens override
        max_concurrent: Max parallel LLM calls per step
    """
    n = len(dataset)
    total_steps = len(chain.steps)
    outer_contexts = [outer_context_builder(s) for s in dataset]
    all_step_outputs: list[list[str]] = [[] for _ in range(n)]
    all_histories: list[list[str]] = [[] for _ in range(n)]
    is_finished = [False] * n
    chain_t0 = time.time()

    for step_idx, step in enumerate(chain.steps):
        active_indices = [i for i in range(n) if not is_finished[i]]
        if not active_indices:
            break

        step_t0 = time.time()
        step_type = "tool" if isinstance(step, ToolStep) else "llm"
        _log(
            f"step {step_idx + 1}/{total_steps} "
            f"({step_type}) '{step.title}' — {len(active_indices)} active samples"
        )

        if isinstance(step, ToolStep):
            tool_name = step.step_config.tool_name

            all_resolved = []
            for i in active_indices:
                resolved_kwargs = {
                    param: _resolve_reference(
                        ref, outer_contexts[i], all_step_outputs[i], dataset[i]
                    )
                    for param, ref in step.step_config.input_mapping.items()
                }
                all_resolved.append(resolved_kwargs)

            if batch_tool_registry and tool_name in batch_tool_registry:
                results = batch_tool_registry[tool_name](all_resolved)
            elif tool_registry and tool_name in tool_registry:
                results = list(
                    await asyncio.gather(
                        *[
                            asyncio.to_thread(tool_registry[tool_name], **kw)
                            for kw in all_resolved
                        ]
                    )
                )
            else:
                raise ValueError(
                    f"Tool '{tool_name}' not found in any registry. "
                    f"Available: tool={list(tool_registry or {})}, "
                    f"batch={list(batch_tool_registry or {})}"
                )

            for idx_in_batch, i in enumerate(active_indices):
                result_str = results[idx_in_batch]
                if not isinstance(result_str, str):
                    result_str = str(result_str)
                all_step_outputs[i].append(result_str)
                all_histories[i].append(
                    chain.prompt_builder.format_history_entry(
                        number=step.number,
                        title=step.title,
                        result=result_str,
                    )
                )

        elif isinstance(step, LLMStep):
            prompts = []
            for i in active_indices:
                visible_history, _ = _resolve_dependencies(
                    step.dependencies, all_histories[i], all_step_outputs[i]
                )
                prompts.append(
                    chain.prompt_builder.build_prompt(
                        step=step,
                        visible_history=visible_history,
                        outer_context=outer_contexts[i],
                        system_prompt=chain.system_prompt,
                    )
                )

            overrides: dict[str, object] = {}
            if step_max_tokens and step.number in step_max_tokens:
                overrides["max_tokens"] = step_max_tokens[step.number]

            semaphore = asyncio.Semaphore(max_concurrent)

            async def _call_llm(
                prompt: str,
                sem: asyncio.Semaphore,
                **kw: object,
            ) -> str:
                async with sem:
                    return await client.copy()(prompt, **kw)

            raw_results = await asyncio.gather(
                *[_call_llm(p, semaphore, **overrides) for p in prompts]
            )
            results = [_strip_thinking(r) for r in raw_results]

            for idx_in_batch, i in enumerate(active_indices):
                all_step_outputs[i].append(results[idx_in_batch])
                all_histories[i].append(
                    chain.prompt_builder.format_history_entry(
                        number=step.number,
                        title=step.title,
                        result=results[idx_in_batch],
                    )
                )
                if _matches_stop_condition(results[idx_in_batch], step.stop_condition):
                    is_finished[i] = True

        else:
            raise ValueError(f"Unknown step type: {type(step).__name__}")

        elapsed = time.time() - step_t0
        total_elapsed = time.time() - chain_t0
        remaining = total_steps - step_idx - 1
        eta = (total_elapsed / (step_idx + 1)) * remaining
        _log(
            f"step {step_idx + 1}/{total_steps} done in {elapsed:.1f}s "
            f"(total {total_elapsed:.1f}s, ETA ~{eta:.0f}s)"
        )

    return [
        ChainResult(
            history=all_histories[i],
            final_output=all_step_outputs[i][-1] if all_step_outputs[i] else "",
            step_outputs=all_step_outputs[i],
        )
        for i in range(n)
    ]


def run_chain_on_dataset_stepwise(
    chain: ChainSpec,
    client: object,
    dataset: list[dict],
    outer_context_builder: Callable[[dict], str],
    tool_registry: dict[str, Callable] | None = None,
    batch_tool_registry: dict[str, Callable] | None = None,
    step_max_tokens: dict[int, int] | None = None,
    max_concurrent: int = 300,
) -> list[ChainResult]:
    """Run chain on dataset using step-batched execution (sync wrapper).

    All samples process each step together before moving to the next step.
    LLM calls are batched and run concurrently via asyncio; tool calls use
    batch_tool_registry (vectorized) or tool_registry (individual, threaded).
    """
    return asyncio.run(
        _run_chain_on_dataset_stepwise(
            chain,
            client,
            dataset,
            outer_context_builder,
            tool_registry,
            batch_tool_registry,
            step_max_tokens,
            max_concurrent,
        )
    )
