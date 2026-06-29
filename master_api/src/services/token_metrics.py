#!/usr/bin/env python3
"""Pure aggregation for the runner's cumulative LLM-token metrics.

Dependency-free (stdlib only) so the de-duplication logic can be unit-tested
without Redis. The runner books token usage to gigavolve Redis under
``<uuid>:metrics:history:llm:tokens:<agent>:<model>:cumulative_total_tokens``
(per-model) and ``…:<agent>:<stage>:<model>:cumulative_total_tokens``
(per-stage). Both granularities already sum to the same grand total, so
naively summing every matching key double-counts.
"""

from __future__ import annotations

from typing import Any, Iterable, Tuple

# Redis key (history list) suffix the runner writes cumulative totals under.
TOKEN_TOTAL_SUFFIX = ":cumulative_total_tokens"


def token_scan_pattern(problem_uuid: str) -> str:
    """``SCAN MATCH`` glob for a problem's cumulative-token history keys."""
    return f"{problem_uuid}:metrics:history:llm:tokens:*{TOKEN_TOTAL_SUFFIX}"


def aggregate_cumulative_tokens(items: Iterable[Tuple[str, Any]]) -> int:
    """Sum cumulative-token values without double-counting granularities.

    ``items`` is ``(redis_key, latest_value)`` pairs. Per-model keys carry
    fewer ``:``-separated segments than per-stage keys; since each
    granularity independently sums to the grand total, we sum only the
    *coarsest* granularity present (the fewest segments) — which yields the
    grand total in either runner topology, and avoids double-counting when
    both are emitted. Returns 0 when nothing usable is present.
    """
    by_depth: dict[int, float] = {}
    for key, value in items:
        if not isinstance(key, str) or not key.endswith(TOKEN_TOTAL_SUFFIX):
            continue
        try:
            v = float(value)
        except (TypeError, ValueError):
            continue
        if v < 0:
            continue
        depth = key.count(":")
        by_depth[depth] = by_depth.get(depth, 0.0) + v
    if not by_depth:
        return 0
    coarsest = min(by_depth)  # fewest segments == per-model == coarsest
    return int(round(by_depth[coarsest]))
