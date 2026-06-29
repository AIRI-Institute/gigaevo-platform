"""Tests for summarization / ROUGE fixes in chain helper.py."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_CHAIN_TEMPLATE_DIR = (
    REPO_ROOT
    / "master_api"
    / "src"
    / "folder_constructor"
    / "validate_templates"
    / "chain"
)
if str(_CHAIN_TEMPLATE_DIR) not in sys.path:
    sys.path.insert(0, str(_CHAIN_TEMPLATE_DIR))

import helper  # noqa: E402
from chain_types import ChainSpec, LLMStep, PromptBuilder  # noqa: E402


@pytest.fixture
def rouge_helper(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(helper, "METRIC", "ROUGE-L")
    monkeypatch.setattr(helper, "VALIDATION_TYPE", "Continuous (0..1)")
    return helper


@pytest.fixture
def qa_helper(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(helper, "METRIC", "exact_match")
    monkeypatch.setattr(helper, "VALIDATION_TYPE", "Binary (0/1)")
    return helper


class TestSummarizationContext:
    def test_no_solve_suffix_for_rouge(self, rouge_helper):
        ctx = rouge_helper._format_dict_as_context(
            {"input": "Длинный текст статьи.", "expected": "кратко"},
            "expected",
        )
        assert "Solve the problem" not in ctx
        assert "Длинный текст статьи." in ctx

    def test_qa_suffix_preserved(self, qa_helper):
        ctx = qa_helper._format_dict_as_context(
            {"question": "2+2?", "answer": "4"},
            "answer",
        )
        assert "Solve the problem" in ctx

    def test_summarization_system_prompt(self, rouge_helper):
        prompt = rouge_helper._get_system_prompt()
        assert "one concise sentence" in prompt.lower()
        assert "same language" in prompt.lower()


class TestSummarizationExtraction:
    def test_short_text_returned_whole(self, rouge_helper):
        text = "Компания выпустила новый смартфон."
        assert rouge_helper._extract_prediction(text) == text

    def test_long_text_uses_last_line(self, rouge_helper):
        reasoning = " ".join(["word"] * 35)
        text = f"{reasoning}\nИтоговое предложение."
        assert rouge_helper._extract_prediction(text) == "Итоговое предложение."

    def test_no_numeric_fallback_on_dates(self, rouge_helper):
        text = (
            "В 2024 году компания представила устройство с батареей на 2 дня "
            "и улучшенной камерой."
        )
        result = rouge_helper._extract_prediction(text)
        assert result == text
        assert result != "2"
        assert result != "2024"

    def test_qa_still_extracts_numeric(self, qa_helper):
        result = qa_helper._extract_prediction(
            "After many calculation steps the final amount due is $150.50 total."
        )
        assert result == "150.50"


class TestScoringOutputSelection:
    def _make_spec(self, n_llm: int) -> ChainSpec:
        steps = [
            LLMStep(
                number=i + 1,
                title=f"Step {i + 1}",
                step_type="llm",
                aim="A",
                stage_action="S",
            )
            for i in range(n_llm)
        ]
        return ChainSpec(system_prompt="sys", steps=steps, prompt_builder=PromptBuilder())

    def test_explicit_output_step(self, rouge_helper):
        spec = self._make_spec(2)
        config = {
            "steps": [
                {"number": 1, "is_output_step": True},
                {"number": 2},
            ]
        }
        outputs = ["summary sentence", "Verified: OK"]
        assert (
            rouge_helper._select_scoring_output(spec, outputs, config)
            == "summary sentence"
        )

    def test_penultimate_llm_for_rouge(self, rouge_helper):
        spec = self._make_spec(2)
        outputs = ["Actual summary.", "Format check passed."]
        assert (
            rouge_helper._select_scoring_output(spec, outputs, {})
            == "Actual summary."
        )

    def test_last_step_for_qa(self, qa_helper):
        spec = self._make_spec(2)
        outputs = ["long reasoning", "42"]
        assert qa_helper._select_scoring_output(spec, outputs, {}) == "42"
