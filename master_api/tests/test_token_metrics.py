#!/usr/bin/env python3
"""Unit tests for the cumulative-token aggregator (CARE cost-meter fix).

Exercises :mod:`services.token_metrics` directly — stdlib-only, so no Redis
or app. The module-scoped ``ensure_services_ready`` autouse fixture from
``conftest.py`` (which blocks on a live stack) is shadowed with a no-op.
"""

import pytest
from services.token_metrics import aggregate_cumulative_tokens, token_scan_pattern


@pytest.fixture(scope="module", autouse=True)
def ensure_services_ready():
    yield


def _per_model(agent, model):
    return f"uuid:metrics:history:llm:tokens:{agent}:{model}:cumulative_total_tokens"


def _per_stage(agent, stage, model):
    return f"uuid:metrics:history:llm:tokens:{agent}:{stage}:{model}:cumulative_total_tokens"


class TestTokenScanPattern:
    def test_builds_glob(self):
        assert token_scan_pattern("abc") == "abc:metrics:history:llm:tokens:*:cumulative_total_tokens"


class TestAggregateCumulativeTokens:
    def test_sums_per_model_keys(self):
        items = [(_per_model("mutation", "gpt"), 1000), (_per_model("insights", "gpt"), 500)]
        assert aggregate_cumulative_tokens(items) == 1500

    def test_prefers_coarsest_granularity_no_double_count(self):
        # Both per-model and per-stage present; each granularity already sums
        # to the grand total (1500). Must NOT return 3000.
        items = [
            (_per_model("mutation", "gpt"), 1500),
            (_per_stage("mutation", "propose", "gpt"), 1000),
            (_per_stage("mutation", "refine", "gpt"), 500),
        ]
        assert aggregate_cumulative_tokens(items) == 1500

    def test_falls_back_to_per_stage_when_no_per_model(self):
        items = [
            (_per_stage("mutation", "propose", "gpt"), 1000),
            (_per_stage("mutation", "refine", "gpt"), 500),
        ]
        assert aggregate_cumulative_tokens(items) == 1500

    def test_ignores_non_token_and_malformed(self):
        items = [
            (_per_model("mutation", "gpt"), 1000),
            ("uuid:metrics:history:program_metrics:programs_valid_count", 7),
            (_per_model("insights", "gpt"), "not-a-number"),
            (_per_model("lineage", "gpt"), -5),
        ]
        assert aggregate_cumulative_tokens(items) == 1000

    def test_empty_is_zero(self):
        assert aggregate_cumulative_tokens([]) == 0
