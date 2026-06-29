#!/usr/bin/env python3
"""Unit tests for the chain-experiment SSE frame logic (CARE §P4.2).

These exercise :mod:`api.routes.experiment_sse` directly — it's
dependency-free (stdlib ``json`` only), so no FastAPI app, DB, or runner is
needed. The module-scoped ``ensure_services_ready`` autouse fixture from
``conftest.py`` (which blocks on a live stack) is shadowed with a no-op
below so this file runs in plain ``pytest`` / CI unit runs.
"""

import json

import pytest
from api.routes.experiment_sse import (
    EXPERIMENT_TERMINAL_STATUSES,
    diff_experiment_frames,
    experiment_event_stream,
    new_stream_state,
    sse_frame,
)


@pytest.fixture(scope="module", autouse=True)
def ensure_services_ready():
    """No-op override of conftest's live-stack gate for these unit tests."""
    yield


def _parse(frame: bytes):
    """Return ``(event_type, data_dict|None)`` for an SSE frame."""
    text = frame.decode("utf-8")
    event_type = None
    data = None
    for line in text.strip().split("\n"):
        if line.startswith("event: "):
            event_type = line[len("event: ") :]
        elif line.startswith("data: "):
            data = json.loads(line[len("data: ") :])
    return event_type, data


def _events(frames):
    return [_parse(f)[0] for f in frames]


class TestSseFrame:
    def test_encodes_event_and_data(self):
        frame = sse_frame("status", {"status": "running"})
        event_type, data = _parse(frame)
        assert event_type == "status"
        assert data == {"status": "running"}

    def test_frame_is_bytes_and_blank_line_terminated(self):
        frame = sse_frame("x", {"a": 1})
        assert isinstance(frame, bytes)
        assert frame.endswith(b"\n\n")

    def test_non_json_serialisable_falls_back_to_str(self):
        # ``default=str`` keeps the stream alive even for odd payloads.
        frame = sse_frame("x", {"set": {1, 2}})
        event_type, data = _parse(frame)
        assert event_type == "x"
        assert isinstance(data["set"], str)


class TestNewStreamState:
    def test_initial_keys(self):
        state = new_stream_state()
        assert state == {
            "status": None,
            "gen": -1,
            "best": None,
            "pv": None,
            "pi": None,
            "tokens": 0.0,
        }


class TestCostTickFrames:
    def test_emits_delta_on_token_increase(self):
        state = new_stream_state()
        f1, _ = diff_experiment_frames(state, {"status": "running"}, {"total_tokens": 1000}, "exp_1")
        f2, _ = diff_experiment_frames(state, {"status": "running"}, {"total_tokens": 1500}, "exp_1")
        # First tick → full 1000 delta; second → 500 delta (cumulative-safe).
        d1 = _parse(next(fr for fr in f1 if _parse(fr)[0] == "cost_tick"))[1]
        d2 = _parse(next(fr for fr in f2 if _parse(fr)[0] == "cost_tick"))[1]
        assert d1["total_tokens"] == 1000
        assert d2["total_tokens"] == 500

    def test_no_tick_when_tokens_unchanged(self):
        state = new_stream_state()
        diff_experiment_frames(state, {"status": "running"}, {"total_tokens": 1000}, "exp_1")
        frames, _ = diff_experiment_frames(state, {"status": "running"}, {"total_tokens": 1000}, "exp_1")
        assert "cost_tick" not in _events(frames)

    def test_no_tick_without_token_metric(self):
        state = new_stream_state()
        frames, _ = diff_experiment_frames(state, {"status": "running"}, {}, "exp_1")
        assert "cost_tick" not in _events(frames)


class TestStatusFrames:
    def test_status_emitted_on_first_poll(self):
        state = new_stream_state()
        frames, terminal = diff_experiment_frames(state, {"status": "running"}, {}, "exp_1")
        assert _events(frames) == ["status"]
        assert terminal is False
        _, data = _parse(frames[0])
        assert data["status"] == "running"
        assert data["experiment_id"] == "exp_1"

    def test_status_not_re_emitted_when_unchanged(self):
        state = new_stream_state()
        diff_experiment_frames(state, {"status": "running"}, {}, "exp_1")
        frames, _ = diff_experiment_frames(state, {"status": "running"}, {}, "exp_1")
        assert frames == []

    def test_status_re_emitted_on_change(self):
        state = new_stream_state()
        diff_experiment_frames(state, {"status": "queued"}, {}, "exp_1")
        frames, _ = diff_experiment_frames(state, {"status": "running"}, {}, "exp_1")
        assert _events(frames) == ["status"]


class TestSnapshotFrames:
    def test_fitness_history_snapshot_each_poll_while_present(self):
        state = new_stream_state()
        metrics = {"fitness_history": [{"generation": 0, "best_fitness": 0.1}]}
        f1, _ = diff_experiment_frames(state, {"status": "running"}, metrics, "exp_1")
        f2, _ = diff_experiment_frames(state, {"status": "running"}, metrics, "exp_1")
        # status only on the first poll; history snapshot re-emits every poll.
        assert "fitness_history_snapshot" in _events(f1)
        assert _events(f2) == ["fitness_history_snapshot"]
        _, data = _parse(next(fr for fr in f2 if _parse(fr)[0] == "fitness_history_snapshot"))
        assert data["source"] == "platform"
        assert data["history"] == metrics["fitness_history"]

    def test_empty_history_list_emits_nothing(self):
        state = new_stream_state()
        frames, _ = diff_experiment_frames(state, {"status": "running"}, {"fitness_history": []}, "exp_1")
        assert "fitness_history_snapshot" not in _events(frames)

    def test_frontier_snapshot_emitted_when_present(self):
        state = new_stream_state()
        frames, _ = diff_experiment_frames(
            state, {"status": "running"}, {"frontier_programs": [{"id": "p1"}]}, "exp_1"
        )
        assert "frontier_programs_snapshot" in _events(frames)


class TestGenerationFrames:
    def test_generation_started_only_on_increase(self):
        state = new_stream_state()
        f0, _ = diff_experiment_frames(state, {"status": "running"}, {"generation": 0}, "exp_1")
        f0b, _ = diff_experiment_frames(state, {"status": "running"}, {"generation": 0}, "exp_1")
        f1, _ = diff_experiment_frames(state, {"status": "running"}, {"generation": 1}, "exp_1")
        assert "generation_started" in _events(f0)
        assert "generation_started" not in _events(f0b)
        assert "generation_started" in _events(f1)

    def test_generation_does_not_emit_on_decrease(self):
        state = new_stream_state()
        diff_experiment_frames(state, {"status": "running"}, {"generation": 5}, "exp_1")
        frames, _ = diff_experiment_frames(state, {"status": "running"}, {"generation": 2}, "exp_1")
        assert "generation_started" not in _events(frames)


class TestBestUpdatedFrames:
    def test_best_updated_on_change_only(self):
        state = new_stream_state()
        f1, _ = diff_experiment_frames(state, {"status": "running"}, {"best_fitness": 0.5}, "exp_1")
        f2, _ = diff_experiment_frames(state, {"status": "running"}, {"best_fitness": 0.5}, "exp_1")
        f3, _ = diff_experiment_frames(state, {"status": "running"}, {"best_fitness": 0.6}, "exp_1")
        assert "best_updated" in _events(f1)
        assert "best_updated" not in _events(f2)
        assert "best_updated" in _events(f3)

    def test_best_zero_is_emitted(self):
        # 0.0 is a real fitness, not "missing".
        state = new_stream_state()
        frames, _ = diff_experiment_frames(state, {"status": "running"}, {"best_fitness": 0.0}, "exp_1")
        assert "best_updated" in _events(frames)


class TestProgramsSnapshot:
    def test_emitted_on_count_change(self):
        state = new_stream_state()
        f1, _ = diff_experiment_frames(
            state, {"status": "running"}, {"programs_valid": 7, "programs_invalid": 2}, "exp_1"
        )
        f2, _ = diff_experiment_frames(
            state, {"status": "running"}, {"programs_valid": 7, "programs_invalid": 2}, "exp_1"
        )
        f3, _ = diff_experiment_frames(
            state, {"status": "running"}, {"programs_valid": 9, "programs_invalid": 2}, "exp_1"
        )
        assert "programs_snapshot" in _events(f1)
        assert "programs_snapshot" not in _events(f2)
        assert "programs_snapshot" in _events(f3)
        _, data = _parse(next(fr for fr in f1 if _parse(fr)[0] == "programs_snapshot"))
        assert data["programs_valid"] == 7
        assert data["programs_invalid"] == 2

    def test_missing_count_coerced_to_minus_one(self):
        state = new_stream_state()
        frames, _ = diff_experiment_frames(state, {"status": "running"}, {"programs_valid": 3}, "exp_1")
        _, data = _parse(next(fr for fr in frames if _parse(fr)[0] == "programs_snapshot"))
        assert data["programs_valid"] == 3
        assert data["programs_invalid"] == -1


class TestTerminal:
    @pytest.mark.parametrize(
        "status,kind",
        [
            ("completed", "completed"),
            ("failed", "failed"),
            ("cancelled", "cancelled"),
            ("terminated", "failed"),
            ("error", "failed"),
        ],
    )
    def test_terminal_status_emits_terminal_frame(self, status, kind):
        state = new_stream_state()
        frames, terminal = diff_experiment_frames(state, {"status": status}, {}, "exp_1")
        assert terminal is True
        # last frame is the terminal frame of the mapped kind.
        assert _events(frames)[-1] == kind

    def test_running_is_not_terminal(self):
        state = new_stream_state()
        _, terminal = diff_experiment_frames(state, {"status": "running"}, {}, "exp_1")
        assert terminal is False

    def test_terminal_constant_membership(self):
        assert "completed" in EXPERIMENT_TERMINAL_STATUSES
        assert "running" not in EXPERIMENT_TERMINAL_STATUSES


class TestNoMetrics:
    def test_only_status_frame_when_no_metrics(self):
        state = new_stream_state()
        frames, terminal = diff_experiment_frames(state, {"status": "running"}, {}, "exp_1")
        assert _events(frames) == ["status"]
        assert terminal is False

    def test_empty_status_payload_is_safe(self):
        state = new_stream_state()
        frames, terminal = diff_experiment_frames(state, {}, {}, "exp_1")
        # empty status string differs from initial None → one status frame.
        assert _events(frames) == ["status"]
        assert terminal is False


# ---------------------------------------------------------------------------
# Full polling loop (experiment_event_stream) — driven with fakes + a no-op
# sleep so the duration/idle caps are deterministic and fast.
# ---------------------------------------------------------------------------


async def _noop_sleep(_seconds):
    return


def _status_returning(*statuses):
    """Async status fetcher that walks ``statuses`` then repeats the last."""
    seq = list(statuses)

    async def _fetch():
        if len(seq) > 1:
            return seq.pop(0)
        return seq[0] if seq else {}

    return _fetch


async def _empty_results():
    return {}


async def _collect(agen, limit=500):
    out = []
    async for frame in agen:
        out.append(frame)
        if len(out) >= limit:
            break
    return out


class TestExperimentEventStreamLoop:
    async def test_terminal_status_ends_stream(self):
        frames = await _collect(
            experiment_event_stream(
                _status_returning({"status": "running"}, {"status": "completed"}),
                _empty_results,
                None,
                "exp_1",
                interval=1.0,
                sleep=_noop_sleep,
            )
        )
        assert _events(frames)[-1] == "completed"
        # running status + completed status + terminal completed frame.
        assert _events(frames) == ["status", "status", "completed"]

    async def test_max_duration_cap_stops_a_never_terminal_run(self):
        frames = await _collect(
            experiment_event_stream(
                _status_returning({"status": "running"}),
                _empty_results,
                None,
                "exp_1",
                interval=1.0,
                heartbeat_seconds=1000.0,
                max_duration_seconds=3.0,
                max_idle_seconds=1000.0,
                sleep=_noop_sleep,
            )
        )
        # Only the first status frame; loop then exits on the duration cap
        # instead of spinning forever.
        assert _events(frames) == ["status"]

    async def test_max_idle_cap_stops_a_silent_run(self):
        frames = await _collect(
            experiment_event_stream(
                _status_returning({"status": "running"}),
                _empty_results,
                None,
                "exp_1",
                interval=1.0,
                heartbeat_seconds=1000.0,
                max_duration_seconds=1000.0,
                max_idle_seconds=3.0,
                sleep=_noop_sleep,
            )
        )
        assert _events(frames) == ["status"]

    async def test_progress_resets_idle_watchdog(self):
        # A run that keeps reporting fitness history (snapshot re-emits every
        # poll) must NOT be killed by the idle cap — stale resets each frame.
        async def _results_with_history():
            return {"metrics": {"fitness_history": [{"generation": 0, "best_fitness": 0.1}]}}

        frames = await _collect(
            experiment_event_stream(
                _status_returning({"status": "running"}),
                _results_with_history,
                None,
                "exp_1",
                interval=1.0,
                heartbeat_seconds=1000.0,
                max_duration_seconds=5.0,
                max_idle_seconds=2.0,
                sleep=_noop_sleep,
            )
        )
        # It ran until the *duration* cap (not the idle cap), emitting a
        # history snapshot every poll → more than the 2 the idle cap allows.
        assert _events(frames).count("fitness_history_snapshot") >= 3

    async def test_heartbeat_emitted_after_idle(self):
        frames = await _collect(
            experiment_event_stream(
                _status_returning({"status": "running"}),
                _empty_results,
                None,
                "exp_1",
                interval=1.0,
                heartbeat_seconds=2.0,
                max_duration_seconds=4.0,
                max_idle_seconds=1000.0,
                sleep=_noop_sleep,
            )
        )
        # Heartbeat is a real data frame (reaches CARE), not a `:` comment.
        assert "heartbeat" in _events(frames)
        assert b": heartbeat\n\n" not in frames

    async def test_disconnect_stops_stream_immediately(self):
        async def _disconnected():
            return True

        frames = await _collect(
            experiment_event_stream(
                _status_returning({"status": "running"}),
                _empty_results,
                _disconnected,
                "exp_1",
                interval=1.0,
                sleep=_noop_sleep,
            )
        )
        assert frames == []

    async def test_fetch_exceptions_do_not_crash_stream(self):
        async def _boom():
            raise RuntimeError("transient DB error")

        # Both fetchers raise → the loop swallows, emits one empty-status
        # frame, then exits on the idle cap instead of propagating.
        frames = await _collect(
            experiment_event_stream(
                _boom,
                _boom,
                None,
                "exp_1",
                interval=1.0,
                heartbeat_seconds=1000.0,
                max_duration_seconds=1000.0,
                max_idle_seconds=3.0,
                sleep=_noop_sleep,
            )
        )
        assert _events(frames) == ["status"]
