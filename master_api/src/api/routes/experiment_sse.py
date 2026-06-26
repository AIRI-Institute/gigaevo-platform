#!/usr/bin/env python3
"""SSE logic for the chain-experiment event stream (CARE §P4.2).

Dependency-free (stdlib only — ``json`` + ``asyncio``) so both the
frame-emission rules (:func:`diff_experiment_frames`) and the full polling
loop (:func:`experiment_event_stream`) can be unit-tested without standing
up the FastAPI app, the DB, or a runner. ``experiments.py``'s
``_experiment_event_stream`` is a thin wrapper that binds the loop to an
``ExperimentService``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

# Safety caps for the live stream so a stuck experiment can't pin an open
# SSE connection forever (the client-side poll path bounds itself the same
# way). ``max_duration`` is the absolute backstop; ``max_idle`` stops a
# stream that has produced no frames for a while (runner never reported).
_DEFAULT_MAX_DURATION_SECONDS = 6 * 60 * 60
_DEFAULT_MAX_IDLE_SECONDS = 15 * 60

EXPERIMENT_TERMINAL_STATUSES = {
    "completed",
    "failed",
    "cancelled",
    "terminated",
    "error",
}


def sse_frame(event_type: str, payload: dict[str, Any]) -> bytes:
    """Encode one SSE ``event:``/``data:`` frame."""
    return (f"event: {event_type}\n" f"data: {json.dumps(payload, default=str)}\n\n").encode("utf-8")


def new_stream_state() -> dict[str, Any]:
    """Fresh change-tracking state for :func:`diff_experiment_frames`."""
    return {"status": None, "gen": -1, "best": None, "pv": None, "pi": None, "tokens": 0.0}


def diff_experiment_frames(
    state: dict[str, Any],
    status_payload: dict[str, Any],
    metrics: dict[str, Any],
    experiment_id: str,
) -> tuple[list[bytes], bool]:
    """Return ``(frames, terminal)`` for one poll, mutating ``state`` in place.

    Pure (no I/O). ``state`` carries the last-seen status / generation /
    best-fitness / program counts so a frame is only emitted when something
    changed. ``fitness_history`` and ``frontier_programs`` are *snapshots* —
    re-emitted every poll while present so a late subscriber catches up.
    ``terminal`` is True once the status reaches a terminal value, at which
    point the caller stops the stream after yielding the returned frames.
    """
    status_payload = status_payload or {}
    metrics = metrics or {}
    frames: list[bytes] = []

    status = str(status_payload.get("status") or "").lower()
    if status != state["status"]:
        frames.append(sse_frame("status", {**status_payload, "experiment_id": experiment_id}))
        state["status"] = status

    history = metrics.get("fitness_history")
    if isinstance(history, list) and history:
        frames.append(
            sse_frame(
                "fitness_history_snapshot",
                {"history": history, "experiment_id": experiment_id, "source": "platform"},
            )
        )
    frontier = metrics.get("frontier_programs")
    if isinstance(frontier, list) and frontier:
        frames.append(
            sse_frame(
                "frontier_programs_snapshot",
                {"frontier": frontier, "experiment_id": experiment_id},
            )
        )

    gen = metrics.get("generation")
    best = metrics.get("best_fitness")
    current = metrics.get("current_fitness")
    pv = metrics.get("programs_valid")
    pi = metrics.get("programs_invalid")
    if isinstance(gen, int) and gen > state["gen"]:
        frames.append(
            sse_frame(
                "generation_started",
                {
                    "generation": gen,
                    "experiment_id": experiment_id,
                    "current_fitness": current,
                    "programs_valid": pv,
                    "programs_invalid": pi,
                },
            )
        )
        state["gen"] = gen
    if isinstance(best, (int, float)) and best != state["best"]:
        frames.append(
            sse_frame(
                "best_updated",
                {
                    "best_fitness": best,
                    "generation": gen,
                    "experiment_id": experiment_id,
                    "current_fitness": current,
                    "programs_valid": pv,
                    "programs_invalid": pi,
                },
            )
        )
        state["best"] = best
    if (pv != state["pv"] or pi != state["pi"]) and (isinstance(pv, int) or isinstance(pi, int)):
        frames.append(
            sse_frame(
                "programs_snapshot",
                {
                    "programs_valid": pv if isinstance(pv, int) else -1,
                    "programs_invalid": pi if isinstance(pi, int) else -1,
                    "experiment_id": experiment_id,
                },
            )
        )
        state["pv"], state["pi"] = pv, pi

    # Cumulative token spend → emit the *delta* as a cost_tick so CARE's
    # additive cost aggregator stays correct (cumulative would double-count).
    tokens = metrics.get("total_tokens")
    if isinstance(tokens, (int, float)) and not isinstance(tokens, bool) and tokens > state["tokens"]:
        delta = tokens - state["tokens"]
        frames.append(
            sse_frame(
                "cost_tick",
                {"total_tokens": delta, "experiment_id": experiment_id},
            )
        )
        state["tokens"] = tokens

    terminal = status in EXPERIMENT_TERMINAL_STATUSES
    if terminal:
        kind = "completed" if status == "completed" else ("cancelled" if status == "cancelled" else "failed")
        frames.append(sse_frame(kind, {**status_payload, "experiment_id": experiment_id}))
    return frames, terminal


async def experiment_event_stream(
    fetch_status,
    fetch_results,
    is_disconnected,
    experiment_id: str,
    *,
    interval: float = 2.0,
    heartbeat_seconds: float = 15.0,
    max_duration_seconds: float = _DEFAULT_MAX_DURATION_SECONDS,
    max_idle_seconds: float = _DEFAULT_MAX_IDLE_SECONDS,
    sleep=None,
):
    """Poll ``fetch_status`` / ``fetch_results`` and yield SSE frames.

    ``fetch_status`` / ``fetch_results`` are async, no-arg callables (the
    route binds them to ``ExperimentService.get_experiment_*``);
    ``is_disconnected`` is an optional async predicate (FastAPI's
    ``request.is_disconnected``). The per-poll frame rules live in
    :func:`diff_experiment_frames`; this loop adds heartbeats, disconnect
    handling, and the duration / idle safety caps so a never-terminal
    experiment can't keep the connection (and a server task) open forever.

    ``sleep`` is injectable for tests (defaults to ``asyncio.sleep``);
    elapsed / idle time is accumulated from ``interval`` so the caps are
    deterministic without a real clock.
    """
    if sleep is None:
        sleep = asyncio.sleep
    state = new_stream_state()
    idle = 0.0  # since last wire write (frame or heartbeat) → heartbeat timer
    stale = 0.0  # since last real frame → no-progress watchdog
    elapsed = 0.0  # total time in the loop → absolute backstop
    while True:
        if is_disconnected is not None:
            try:
                if await is_disconnected():
                    return
            except Exception:
                pass

        try:
            status_payload = await fetch_status() or {}
        except Exception:
            status_payload = {}
        try:
            results = await fetch_results() or {}
        except Exception:
            results = {}
        metrics = results.get("metrics") if isinstance(results.get("metrics"), dict) else {}

        frames, terminal = diff_experiment_frames(state, status_payload, metrics, experiment_id)
        for frame in frames:
            yield frame
            idle = 0.0
            stale = 0.0
        if terminal:
            return

        await sleep(interval)
        elapsed += interval
        idle += interval
        stale += interval
        if max_duration_seconds and elapsed >= max_duration_seconds:
            return
        if max_idle_seconds and stale >= max_idle_seconds:
            return
        if idle >= heartbeat_seconds:
            # A real ``heartbeat`` data frame (not a bare ``:`` SSE comment)
            # so it reaches CARE and refreshes its liveness clock — a comment
            # keeps the socket warm but is dropped by the client parser, so a
            # quiet generation would otherwise read as "stalled".
            yield sse_frame("heartbeat", {"experiment_id": experiment_id})
            idle = 0.0
