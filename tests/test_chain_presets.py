"""Tests for chain preset execution with the step-batched engine.

Validates that:
1. Every preset from CHAIN_TEMPLATES and EVOLUTION_PRESETS builds a valid ChainSpec
2. step_type normalisation (LLM→llm, TOOL→tool) works
3. Unsupported step types (TRANSFORM, CONDITIONAL, MEMORY) are filtered gracefully
4. ToolConfig tolerates extra fields (tool_description, timeout)
5. Dependency cleanup removes references to dropped steps
6. PromptBuilder produces valid prompts for each step
7. The full chain_experiment_builder flow produces a valid experiment directory
"""

import copy
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = REPO_ROOT / "master_api" / "src" / "folder_constructor" / "validate_templates" / "chain"

sys.path.insert(0, str(TEMPLATE_DIR))

from chain_types import (
    ChainResult,
    ChainSpec,
    LLMStep,
    PromptBuilder,
    RawChainSpec,
    ToolConfig,
    ToolStep,
)


# ---------------------------------------------------------------------------
# Preset definitions (copied from create_carl_experiment.py for isolation)
# ---------------------------------------------------------------------------

CHAIN_TEMPLATES = {
    "Two-Step Reasoning": {
        "description": "Analysis → Solution (2 LLM steps) — weak baseline",
        "steps": [
            {
                "number": 1,
                "step_type": "LLM",
                "title": "Look at the problem",
                "aim": "Read the problem.",
                "reasoning_questions": "What is given?",
                "dependencies": [],
                "step_context_queries": ["problem"],
                "stage_action": "Read what is provided.",
                "example_reasoning": "",
                "frozen": False,
            },
            {
                "number": 2,
                "step_type": "LLM",
                "title": "Give answer",
                "aim": "Produce an answer.",
                "reasoning_questions": "What could the answer be?",
                "dependencies": [1],
                "step_context_queries": ["problem"],
                "stage_action": "Try to answer.",
                "example_reasoning": "",
                "frozen": False,
            },
        ],
    },
    "LLM + Tool Eval": {
        "description": "LLM generates → Tool evaluates (frozen)",
        "steps": [
            {
                "number": 1,
                "step_type": "LLM",
                "title": "Generate Output",
                "aim": "Produce some output.",
                "reasoning_questions": "What is expected?",
                "dependencies": [],
                "step_context_queries": ["problem"],
                "stage_action": "Generate output for the tool.",
                "example_reasoning": "",
                "frozen": False,
            },
            {
                "number": 2,
                "step_type": "TOOL",
                "title": "Evaluate",
                "aim": "",
                "reasoning_questions": "",
                "dependencies": [1],
                "step_context_queries": [],
                "stage_action": "",
                "example_reasoning": "",
                "frozen": True,
                "step_config": {
                    "tool_name": "evaluate",
                    "tool_description": "Evaluation tool",
                    "input_mapping": {"input": "@1.output"},
                    "timeout": 120,
                },
            },
        ],
    },
    "Three-Step CoT": {
        "description": "Understand → Plan → Execute (Chain-of-Thought)",
        "steps": [
            {
                "number": 1,
                "step_type": "LLM",
                "title": "Read",
                "aim": "Look at the problem.",
                "reasoning_questions": "What is this about?",
                "dependencies": [],
                "step_context_queries": [],
                "stage_action": "Read the problem text.",
                "example_reasoning": "",
                "frozen": False,
            },
            {
                "number": 2,
                "step_type": "LLM",
                "title": "Think",
                "aim": "Think about it.",
                "reasoning_questions": "How could this be solved?",
                "dependencies": [1],
                "step_context_queries": ["step_1_output"],
                "stage_action": "Consider possible approaches.",
                "example_reasoning": "",
                "frozen": False,
            },
            {
                "number": 3,
                "step_type": "LLM",
                "title": "Answer",
                "aim": "Give the answer.",
                "reasoning_questions": "What is the answer?",
                "dependencies": [1, 2],
                "step_context_queries": ["step_1_output", "step_2_output"],
                "stage_action": "Provide your answer.",
                "example_reasoning": "",
                "frozen": False,
            },
        ],
    },
    "HotpotQA (2-Hop Retrieval, static topology)": {
        "description": "HotpotQA with retrieve tool — static topology",
        "steps": [
            {
                "number": 1,
                "step_type": "TOOL",
                "title": "Retrieve first-hop passages.",
                "aim": "",
                "reasoning_questions": "",
                "dependencies": [],
                "step_context_queries": [],
                "stage_action": "",
                "example_reasoning": "",
                "frozen": True,
                "step_config": {
                    "tool_name": "retrieve",
                    "tool_description": "BM25 retrieval tool.",
                    "input_mapping": {"query": "$outer_context"},
                    "timeout": 120,
                },
            },
            {
                "number": 2,
                "step_type": "LLM",
                "title": "Read passages",
                "aim": "Look at retrieved text.",
                "reasoning_questions": "What is in the passages?",
                "dependencies": [1],
                "step_context_queries": ["problem"],
                "stage_action": "Skim the passages.",
                "example_reasoning": "",
                "frozen": False,
            },
            {
                "number": 3,
                "step_type": "LLM",
                "title": "Second query",
                "aim": "Write a follow-up search query.",
                "reasoning_questions": "What else could help?",
                "dependencies": [2],
                "step_context_queries": ["problem"],
                "stage_action": "Write a search query.\nProvide ONLY the search query, no additional text.",
                "example_reasoning": "",
                "frozen": False,
            },
            {
                "number": 4,
                "step_type": "TOOL",
                "title": "Retrieve second-hop passages.",
                "aim": "",
                "reasoning_questions": "",
                "dependencies": [3],
                "step_context_queries": [],
                "stage_action": "",
                "example_reasoning": "",
                "frozen": True,
                "step_config": {
                    "tool_name": "retrieve",
                    "tool_description": "BM25 retrieval tool.",
                    "input_mapping": {"query": "$history[-1]"},
                    "timeout": 120,
                },
            },
            {
                "number": 5,
                "step_type": "LLM",
                "title": "Combine",
                "aim": "Put information together.",
                "reasoning_questions": "What do I have now?",
                "dependencies": [2, 4],
                "step_context_queries": ["problem"],
                "stage_action": "Combine what you found.",
                "example_reasoning": "",
                "frozen": False,
            },
            {
                "number": 6,
                "step_type": "LLM",
                "title": "Answer",
                "aim": "Give an answer.",
                "reasoning_questions": "What is the answer?",
                "dependencies": [2, 5],
                "step_context_queries": ["problem"],
                "stage_action": "Answer the question. Format: Answer: <your answer>",
                "example_reasoning": "",
                "frozen": False,
            },
        ],
    },
    "Sentiment Classification": {
        "description": "Sentiment analysis: 2 LLM steps",
        "steps": [
            {
                "number": 1,
                "step_type": "LLM",
                "title": "Analyze text sentiment indicators",
                "aim": "Identify sentiment-bearing words",
                "reasoning_questions": "What emotional words are present?",
                "dependencies": [],
                "step_context_queries": [],
                "stage_action": "Read the text carefully.",
                "example_reasoning": "",
                "frozen": False,
            },
            {
                "number": 2,
                "step_type": "LLM",
                "title": "Verify and output final sentiment",
                "aim": "Output positive/negative/neutral",
                "reasoning_questions": "Does the analysis capture the tone?",
                "dependencies": [1],
                "step_context_queries": ["step_1_output"],
                "stage_action": "Output ONLY a single word.",
                "example_reasoning": "",
                "frozen": False,
            },
        ],
    },
    "TOOL + LLM QA": {
        "description": "Retrieve with TOOL → LLM answers",
        "steps": [
            {
                "number": 1,
                "step_type": "TOOL",
                "title": "Retrieve relevant information",
                "aim": "",
                "reasoning_questions": "",
                "dependencies": [],
                "step_context_queries": [],
                "stage_action": "",
                "example_reasoning": "",
                "frozen": False,
                "step_config": {
                    "tool_name": "retrieve",
                    "tool_description": "Built-in retrieval tool.",
                    "input_mapping": {"query": "$outer_context"},
                    "timeout": 120,
                },
            },
            {
                "number": 2,
                "step_type": "LLM",
                "title": "Answer using retrieved context",
                "aim": "Produce a concise answer.",
                "reasoning_questions": "What is the question asking?",
                "dependencies": [1],
                "step_context_queries": ["step_1_output"],
                "stage_action": "Output ONLY the answer.",
                "example_reasoning": "",
                "frozen": False,
            },
        ],
    },
    "QA + Transform (extract)": {
        "description": "LLM + TRANSFORM step to extract answer",
        "steps": [
            {
                "number": 1,
                "step_type": "LLM",
                "title": "Think and answer",
                "aim": "Try to answer the question.",
                "reasoning_questions": "What is being asked?",
                "dependencies": [],
                "step_context_queries": [],
                "stage_action": "Think about the question.",
                "example_reasoning": "",
                "frozen": False,
            },
            {
                "number": 2,
                "step_type": "TRANSFORM",
                "title": "Extract final answer",
                "aim": "",
                "reasoning_questions": "",
                "dependencies": [1],
                "step_context_queries": ["step_1_output"],
                "stage_action": "",
                "example_reasoning": "",
                "frozen": False,
                "step_config": {"transform_type": "extract", "pattern": "Answer:\\s*(.+?)(?:\\n|$)"},
            },
        ],
    },
    "QA + Conditional routing": {
        "description": "LLM classifies → CONDITIONAL routes → LLM answers",
        "steps": [
            {
                "number": 1,
                "step_type": "LLM",
                "title": "Classify",
                "aim": "Decide question type.",
                "reasoning_questions": "Simple or complex?",
                "dependencies": [],
                "step_context_queries": [],
                "stage_action": "Output the single word 'simple' or 'complex'.",
                "example_reasoning": "",
                "frozen": False,
            },
            {
                "number": 2,
                "step_type": "CONDITIONAL",
                "title": "Route by complexity",
                "aim": "",
                "reasoning_questions": "",
                "dependencies": [1],
                "step_context_queries": ["step_1_output"],
                "stage_action": "",
                "example_reasoning": "",
                "frozen": True,
                "step_config": {
                    "condition_type": "contains",
                    "condition_value": "complex",
                    "true_branch_step": 4,
                    "false_branch_step": 3,
                },
            },
            {
                "number": 3,
                "step_type": "LLM",
                "title": "Quick answer",
                "aim": "Answer briefly.",
                "reasoning_questions": "What is the answer?",
                "dependencies": [1],
                "step_context_queries": ["step_1_output"],
                "stage_action": "Give a short answer.",
                "example_reasoning": "",
                "frozen": False,
            },
            {
                "number": 4,
                "step_type": "LLM",
                "title": "Detailed answer",
                "aim": "Answer with more thought.",
                "reasoning_questions": "What could the answer be?",
                "dependencies": [1],
                "step_context_queries": ["step_1_output"],
                "stage_action": "Think a bit and answer.",
                "example_reasoning": "",
                "frozen": False,
            },
        ],
    },
    "🧬 Evolution: Memory-Enhanced Reasoning": {
        "description": "Tests memory operations",
        "steps": [
            {
                "number": 1,
                "step_type": "LLM",
                "title": "Initial Analysis",
                "aim": "Analyze the problem.",
                "reasoning_questions": "What's important?",
                "dependencies": [],
                "step_context_queries": ["problem"],
                "stage_action": "Extract key information.",
                "example_reasoning": "",
                "frozen": False,
            },
            {
                "number": 2,
                "step_type": "MEMORY",
                "title": "Store Key Information",
                "aim": "",
                "reasoning_questions": "",
                "dependencies": [1],
                "step_context_queries": [],
                "stage_action": "",
                "example_reasoning": "",
                "frozen": False,
                "step_config": {
                    "operation": "write",
                    "key": "problem_context",
                    "namespace": "default",
                    "value_source": "$history[-1]",
                },
            },
            {
                "number": 3,
                "step_type": "LLM",
                "title": "Apply Memory",
                "aim": "Use stored information.",
                "reasoning_questions": "What did I store?",
                "dependencies": [2],
                "step_context_queries": ["problem"],
                "stage_action": "Retrieve and use stored information.",
                "example_reasoning": "",
                "frozen": False,
            },
        ],
    },
}

EVOLUTION_PRESETS = {
    "Basic 2-Step": {
        "description": "Simple two-step chain",
        "evolution_mode": "full_chain",
        "steps": [
            {
                "number": 1,
                "step_type": "LLM",
                "title": "Analyze Problem",
                "aim": "Read and understand the problem.",
                "reasoning_questions": "What is being asked?",
                "dependencies": [],
                "step_context_queries": ["problem"],
                "stage_action": "Analyze the problem.",
                "example_reasoning": "",
                "frozen": False,
            },
            {
                "number": 2,
                "step_type": "LLM",
                "title": "Solve and Answer",
                "aim": "Provide the final answer.",
                "reasoning_questions": "What method should I use?",
                "dependencies": [1],
                "step_context_queries": ["problem"],
                "stage_action": "Apply solution.",
                "example_reasoning": "",
                "frozen": False,
            },
        ],
    },
    "Improved CoT (3-Step)": {
        "description": "Chain-of-Thought with structured reasoning",
        "evolution_mode": "full_chain",
        "steps": [
            {
                "number": 1,
                "step_type": "LLM",
                "title": "Understand Problem",
                "aim": "Read and understand.",
                "reasoning_questions": "What type of problem is this?",
                "dependencies": [],
                "step_context_queries": ["problem"],
                "stage_action": "Break down the problem.",
                "example_reasoning": "",
                "frozen": False,
            },
            {
                "number": 2,
                "step_type": "LLM",
                "title": "Plan Solution",
                "aim": "Think through the solution.",
                "reasoning_questions": "What steps should I take?",
                "dependencies": [1],
                "step_context_queries": ["problem"],
                "stage_action": "Outline a solution.",
                "example_reasoning": "",
                "frozen": False,
            },
            {
                "number": 3,
                "step_type": "LLM",
                "title": "Execute and Answer",
                "aim": "Execute and answer.",
                "reasoning_questions": "How do I format the answer?",
                "dependencies": [1, 2],
                "step_context_queries": ["problem"],
                "stage_action": "Follow the plan.",
                "example_reasoning": "",
                "frozen": False,
            },
        ],
    },
    "QA with Retrieval": {
        "description": "QA with retrieval tool",
        "evolution_mode": "full_chain",
        "steps": [
            {
                "number": 1,
                "step_type": "TOOL",
                "title": "Retrieve Context",
                "aim": "",
                "reasoning_questions": "",
                "dependencies": [],
                "step_context_queries": [],
                "stage_action": "",
                "example_reasoning": "",
                "frozen": False,
                "step_config": {
                    "tool_name": "retrieve",
                    "tool_description": "Retrieve relevant passages",
                    "input_mapping": {"query": "$outer_context"},
                    "timeout": 120,
                },
            },
            {
                "number": 2,
                "step_type": "LLM",
                "title": "Find Answer in Context",
                "aim": "Find the answer.",
                "reasoning_questions": "What information is relevant?",
                "dependencies": [1],
                "step_context_queries": ["problem"],
                "stage_action": "Analyze and extract the answer.",
                "example_reasoning": "",
                "frozen": False,
            },
        ],
    },
    "Single-Step Focus": {
        "description": "Single-step evolution mode",
        "evolution_mode": "single_step",
        "step_number": 2,
        "steps": [
            {
                "number": 1,
                "step_type": "LLM",
                "title": "Problem Analysis",
                "aim": "Analyze the problem.",
                "reasoning_questions": "What is given?",
                "dependencies": [],
                "step_context_queries": ["problem"],
                "stage_action": "Provide a structured analysis.",
                "example_reasoning": "",
                "frozen": True,
            },
            {
                "number": 2,
                "step_type": "LLM",
                "title": "Core Reasoning",
                "aim": "Think through the solution.",
                "reasoning_questions": "What's the best approach?",
                "dependencies": [1],
                "step_context_queries": ["problem"],
                "stage_action": "Develop reasoning strategy.",
                "example_reasoning": "",
                "frozen": False,
            },
            {
                "number": 3,
                "step_type": "LLM",
                "title": "Final Answer",
                "aim": "Produce final answer.",
                "reasoning_questions": "What is the final answer?",
                "dependencies": [2],
                "step_context_queries": ["problem"],
                "stage_action": "Format and present the answer.",
                "example_reasoning": "",
                "frozen": True,
            },
        ],
    },
}

DATASET_PRESETS = {
    "gsm8k": [
        {
            "number": 1,
            "step_type": "LLM",
            "title": "Decompose and solve",
            "aim": "Break down the word problem",
            "reasoning_questions": "What quantities are given?",
            "dependencies": [],
            "step_context_queries": ["$outer_context"],
            "stage_action": "Solve step by step.",
            "example_reasoning": "",
            "frozen": False,
        },
        {
            "number": 2,
            "step_type": "LLM",
            "title": "Extract numeric answer",
            "aim": "Extract only the final number",
            "reasoning_questions": "What is the final result?",
            "dependencies": [1],
            "step_context_queries": ["step_1_output"],
            "stage_action": "Output ONLY the numeric answer.",
            "example_reasoning": "",
            "frozen": False,
        },
    ],
}


# =====================================================================
# Helper to build chain_config dicts from preset steps
# =====================================================================


def _preset_to_chain_config(steps: list[dict]) -> dict:
    """Wrap steps into a chain_config dict (as the builder does)."""
    return {"steps": copy.deepcopy(steps)}


# =====================================================================
# Tests: chain_types parsing
# =====================================================================


class TestChainTypesParsing:
    """Test that Pydantic models parse preset data correctly."""

    def test_llm_step_with_extra_fields(self):
        """LLMStep with step_context_queries (extra field) should parse."""
        data = {
            "number": 1,
            "step_type": "llm",
            "title": "Test",
            "aim": "Test aim",
            "stage_action": "Test action",
            "dependencies": [],
            "step_context_queries": ["problem"],
            "frozen": False,
        }
        step = LLMStep.model_validate(data)
        assert step.number == 1
        assert step.step_type == "llm"
        assert step.aim == "Test aim"

    def test_tool_step_with_extra_config_fields(self):
        """ToolConfig should ignore extra fields like tool_description, timeout."""
        data = {
            "number": 1,
            "step_type": "tool",
            "title": "Retrieve",
            "dependencies": [],
            "step_config": {
                "tool_name": "retrieve",
                "tool_description": "BM25 retrieval tool",
                "input_mapping": {"query": "$outer_context"},
                "timeout": 120,
            },
        }
        step = ToolStep.model_validate(data)
        assert step.step_config.tool_name == "retrieve"
        assert step.step_config.input_mapping == {"query": "$outer_context"}

    def test_tool_step_non_dollar_ref_warning(self, capsys):
        """ToolConfig with @-reference should warn, not raise."""
        data = {
            "number": 2,
            "step_type": "tool",
            "title": "Evaluate",
            "dependencies": [1],
            "step_config": {
                "tool_name": "evaluate",
                "input_mapping": {"input": "@1.output"},
            },
        }
        step = ToolStep.model_validate(data)
        assert step.step_config.input_mapping == {"input": "@1.output"}

    def test_raw_chain_spec_ignores_extras(self):
        """RawChainSpec should parse with extra top-level fields."""
        data = {
            "system_prompt": "You are helpful.",
            "max_workers": 2,
            "enable_progress": False,
            "steps": [
                {
                    "number": 1,
                    "step_type": "llm",
                    "title": "Test",
                    "aim": "Aim",
                    "stage_action": "Act",
                    "dependencies": [],
                },
            ],
        }
        spec = RawChainSpec.model_validate(data)
        assert len(spec.steps) == 1

    def test_prompt_builder_format(self):
        """PromptBuilder produces expected prompt structure."""
        step = LLMStep(
            number=1,
            title="Analyze",
            step_type="llm",
            aim="Understand the problem",
            stage_action="Read carefully",
            reasoning_questions="What is given?",
        )
        pb = PromptBuilder()
        prompt = pb.build_prompt(
            step=step,
            visible_history=[],
            outer_context="Question: What is 2+2?",
            system_prompt="You are a math expert.",
        )
        assert "System Instructions:" in prompt
        assert "math expert" in prompt
        assert "What is 2+2?" in prompt
        assert "Analyze" in prompt

    def test_prompt_builder_with_history(self):
        """PromptBuilder includes history in prompt."""
        step = LLMStep(
            number=2,
            title="Answer",
            step_type="llm",
            aim="Give answer",
            stage_action="Provide the final answer",
        )
        pb = PromptBuilder()
        history_entry = pb.format_history_entry(number=1, title="Analysis", result="The problem asks for 2+2")
        prompt = pb.build_prompt(
            step=step,
            visible_history=[history_entry],
            outer_context="Question: What is 2+2?",
            system_prompt="",
        )
        assert "Previous steps:" in prompt
        assert "Analysis" in prompt
        assert "2+2" in prompt


# =====================================================================
# Tests: _build_chain_spec (helper.py)
# =====================================================================

from helper import _build_chain_spec, _extract_prediction, _format_dict_as_context


class TestBuildChainSpec:
    """Test chain spec construction from preset configs."""

    @pytest.mark.parametrize("name,preset", list(CHAIN_TEMPLATES.items()))
    def test_chain_template_builds(self, name, preset):
        """Every CHAIN_TEMPLATE should build a valid ChainSpec."""
        config = _preset_to_chain_config(preset["steps"])
        spec = _build_chain_spec(config)

        assert isinstance(spec, ChainSpec)
        assert len(spec.steps) > 0
        assert spec.system_prompt

        for step in spec.steps:
            assert step.step_type in ("llm", "tool")
            assert isinstance(step.number, int)
            assert isinstance(step.title, str) and step.title

    @pytest.mark.parametrize("name,preset", list(EVOLUTION_PRESETS.items()))
    def test_evolution_preset_builds(self, name, preset):
        """Every EVOLUTION_PRESET should build a valid ChainSpec."""
        config = _preset_to_chain_config(preset["steps"])
        spec = _build_chain_spec(config)

        assert isinstance(spec, ChainSpec)
        assert len(spec.steps) > 0

    @pytest.mark.parametrize("name,steps", list(DATASET_PRESETS.items()))
    def test_dataset_preset_builds(self, name, steps):
        """Every DATASET preset should build a valid ChainSpec."""
        config = _preset_to_chain_config(steps)
        spec = _build_chain_spec(config)

        assert isinstance(spec, ChainSpec)
        assert len(spec.steps) == len(steps)

    def test_transform_step_filtered(self):
        """TRANSFORM steps should be filtered out."""
        config = _preset_to_chain_config(CHAIN_TEMPLATES["QA + Transform (extract)"]["steps"])
        spec = _build_chain_spec(config)

        step_types = [s.step_type for s in spec.steps]
        assert "transform" not in step_types
        assert len(spec.steps) == 1
        assert spec.steps[0].title == "Think and answer"

    def test_conditional_step_filtered(self):
        """CONDITIONAL steps should be filtered out and deps cleaned."""
        config = _preset_to_chain_config(CHAIN_TEMPLATES["QA + Conditional routing"]["steps"])
        spec = _build_chain_spec(config)

        step_types = [s.step_type for s in spec.steps]
        assert "conditional" not in step_types
        assert len(spec.steps) == 3

        for step in spec.steps:
            for dep in step.dependencies:
                assert dep != 2, "Dependency on dropped CONDITIONAL step 2 should be removed"

    def test_memory_step_filtered(self):
        """MEMORY steps should be filtered out and deps cleaned."""
        config = _preset_to_chain_config(CHAIN_TEMPLATES["🧬 Evolution: Memory-Enhanced Reasoning"]["steps"])
        spec = _build_chain_spec(config)

        step_types = [s.step_type for s in spec.steps]
        assert "memory" not in step_types

        step3 = next((s for s in spec.steps if s.number == 3), None)
        if step3:
            assert 2 not in step3.dependencies

    def test_step_type_normalisation(self):
        """Uppercase step_type should be normalised to lowercase."""
        config = {
            "steps": [
                {
                    "number": 1,
                    "step_type": "LLM",
                    "title": "Test",
                    "aim": "Test",
                    "stage_action": "Test",
                    "dependencies": [],
                },
                {
                    "number": 2,
                    "step_type": "TOOL",
                    "title": "Retrieve",
                    "dependencies": [1],
                    "step_config": {"tool_name": "retrieve", "input_mapping": {"query": "$outer_context"}},
                },
            ]
        }
        spec = _build_chain_spec(config)
        assert spec.steps[0].step_type == "llm"
        assert spec.steps[1].step_type == "tool"

    def test_empty_aim_gets_default(self):
        """LLM step with empty aim should get a default value."""
        config = {
            "steps": [
                {"number": 1, "step_type": "LLM", "title": "Test", "aim": "", "stage_action": "", "dependencies": []},
            ]
        }
        spec = _build_chain_spec(config)
        assert spec.steps[0].aim
        assert spec.steps[0].stage_action

    def test_steps_sorted_by_number(self):
        """Steps should be sorted by number regardless of input order."""
        config = {
            "steps": [
                {
                    "number": 3,
                    "step_type": "LLM",
                    "title": "Third",
                    "aim": "A3",
                    "stage_action": "S3",
                    "dependencies": [1],
                },
                {
                    "number": 1,
                    "step_type": "LLM",
                    "title": "First",
                    "aim": "A1",
                    "stage_action": "S1",
                    "dependencies": [],
                },
                {
                    "number": 2,
                    "step_type": "LLM",
                    "title": "Second",
                    "aim": "A2",
                    "stage_action": "S2",
                    "dependencies": [1],
                },
            ]
        }
        spec = _build_chain_spec(config)
        nums = [s.number for s in spec.steps]
        assert nums == [1, 2, 3]

    def test_retrieval_tool_dollar_refs(self):
        """TOOL steps with $outer_context and $history[-1] should parse."""
        config = _preset_to_chain_config(CHAIN_TEMPLATES["HotpotQA (2-Hop Retrieval, static topology)"]["steps"])
        spec = _build_chain_spec(config)

        tool_steps = [s for s in spec.steps if isinstance(s, ToolStep)]
        assert len(tool_steps) == 2
        assert tool_steps[0].step_config.input_mapping["query"] == "$outer_context"
        assert tool_steps[1].step_config.input_mapping["query"] == "$history[-1]"


# =====================================================================
# Tests: chain_runner reference resolution
# =====================================================================

from chain_runner import _resolve_reference, _resolve_dependencies, _strip_thinking


class TestChainRunner:
    """Test chain_runner utility functions."""

    def test_resolve_outer_context(self):
        assert _resolve_reference("$outer_context", "Hello", []) == "Hello"

    def test_resolve_history_last(self):
        assert _resolve_reference("$history[-1]", "", ["A", "B", "C"]) == "C"

    def test_resolve_history_last_empty(self):
        assert _resolve_reference("$history[-1]", "", []) == ""

    def test_resolve_history_index(self):
        assert _resolve_reference("$history[0]", "", ["First", "Second"]) == "First"
        assert _resolve_reference("$history[1]", "", ["First", "Second"]) == "Second"

    def test_resolve_history_index_out_of_range(self):
        assert _resolve_reference("$history[5]", "", ["A"]) == ""

    def test_resolve_sample_field(self):
        sample = {"question": "What is 2+2?", "context": "Math"}
        assert _resolve_reference("$sample.question", "", [], sample) == "What is 2+2?"

    def test_resolve_sample_nested(self):
        sample = {"meta": {"source": "test"}}
        assert _resolve_reference("$sample.meta.source", "", [], sample) == "test"

    def test_resolve_sample_missing_field(self):
        assert _resolve_reference("$sample.missing", "", [], {"a": 1}) == ""

    def test_resolve_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown reference"):
            _resolve_reference("@1.output", "", [])

    def test_resolve_dependencies_empty(self):
        """Empty deps = all prior visible."""
        history = ["H1", "H2", "H3"]
        outputs = ["O1", "O2", "O3"]
        visible_h, visible_o = _resolve_dependencies([], history, outputs)
        assert visible_h == ["H1", "H2", "H3"]
        assert visible_o == {1: "O1", 2: "O2", 3: "O3"}

    def test_resolve_dependencies_subset(self):
        """Specific deps = only those visible."""
        history = ["H1", "H2", "H3"]
        outputs = ["O1", "O2", "O3"]
        visible_h, visible_o = _resolve_dependencies([1, 3], history, outputs)
        assert len(visible_h) == 2
        assert 1 in visible_o and 3 in visible_o
        assert 2 not in visible_o

    def test_strip_thinking_wellformed(self):
        text = "Before <think>internal thought</think> After"
        assert _strip_thinking(text) == "Before  After"

    def test_strip_thinking_truncated(self):
        text = "Result <think>truncated by max tokens..."
        assert _strip_thinking(text) == "Result"


# =====================================================================
# Tests: helper utility functions
# =====================================================================


class TestHelperUtilities:
    """Test extraction and formatting functions from helper.py."""

    def test_extract_prediction_answer_pattern(self):
        assert _extract_prediction("Some reasoning\nAnswer: 42") == "42"

    def test_extract_prediction_answer_is_pattern(self):
        assert _extract_prediction("The answer is Paris") == "Paris"

    def test_extract_prediction_short_text(self):
        assert _extract_prediction("positive") == "positive"

    def test_extract_prediction_numeric(self):
        result = _extract_prediction("The total cost is $150.50")
        assert result  # Should extract a number

    def test_extract_prediction_empty(self):
        assert _extract_prediction("") == ""

    def test_format_dict_as_context(self):
        row = {"question": "What is 2+2?", "context": "Math", "answer": "4"}
        ctx = _format_dict_as_context(row, "answer")
        assert "question:" in ctx
        assert "What is 2+2?" in ctx
        assert "answer" not in ctx.split("\n\n")[0]


# =====================================================================
# Tests: chain_experiment_builder integration
# =====================================================================


class TestChainExperimentBuilder:
    """Test that the builder produces valid experiment directories."""

    @pytest.fixture
    def tmp_output(self, tmp_path):
        return tmp_path / "experiments"

    @pytest.fixture
    def template_base(self):
        return REPO_ROOT / "master_api" / "src" / "folder_constructor" / "validate_templates"

    @pytest.fixture
    def sample_dataset(self, tmp_path):
        csv_path = tmp_path / "data.csv"
        csv_path.write_text("question,answer\nWhat is 2+2?,4\nWhat is 3+3?,6\n")
        return csv_path

    def test_build_two_step_preset(self, tmp_output, template_base, sample_dataset):
        """Build experiment from Two-Step Reasoning preset."""
        sys.path.insert(0, str(REPO_ROOT / "master_api" / "src" / "folder_constructor"))
        from chain_experiment_builder import build_chain_experiment

        steps = CHAIN_TEMPLATES["Two-Step Reasoning"]["steps"]
        config = json.dumps({"steps": copy.deepcopy(steps)})

        spec = {
            "name": "test-two-step",
            "description": "Test two-step preset",
            "target_column": "answer",
            "base_chain_config": config,
            "llm_model": "test-model",
            "max_iterations": 10,
            "validation_criteria": {"validation_type": "Binary (0/1)", "binary_method": "equality"},
            "evolution_mode": "full_chain",
        }

        exp_dir = build_chain_experiment(
            spec=spec,
            output_root=tmp_output,
            template_base=template_base,
            dataset_path=sample_dataset,
        )

        assert exp_dir.exists()

        required_files = [
            "task_description.txt",
            "validate.py",
            "context.py",
            "helper.py",
            "chain_types.py",
            "chain_client.py",
            "chain_runner.py",
            "chain_validation.py",
            "base_chain_config.json",
            "chain_config.json",
            "chain_spec.json",
            "dataset/data.csv",
            "initial_programs/baseline.py",
        ]
        for f in required_files:
            assert (exp_dir / f).exists(), f"Missing: {f}"

    def test_build_retrieval_preset(self, tmp_output, template_base, sample_dataset):
        """Build experiment from retrieval preset — tool steps present."""
        sys.path.insert(0, str(REPO_ROOT / "master_api" / "src" / "folder_constructor"))
        from chain_experiment_builder import build_chain_experiment

        steps = CHAIN_TEMPLATES["TOOL + LLM QA"]["steps"]
        config = json.dumps({"steps": copy.deepcopy(steps)})

        spec = {
            "name": "test-retrieval",
            "description": "Test retrieval preset",
            "target_column": "answer",
            "base_chain_config": config,
            "llm_model": "test-model",
            "max_iterations": 10,
            "validation_criteria": {},
            "evolution_mode": "full_chain",
        }

        exp_dir = build_chain_experiment(
            spec=spec,
            output_root=tmp_output,
            template_base=template_base,
            dataset_path=sample_dataset,
        )

        assert exp_dir.exists()

        baseline = (exp_dir / "initial_programs" / "baseline.py").read_text()
        assert "CHAIN_CONFIG_JSON" in baseline
        assert "entrypoint" in baseline

        chain_config = json.loads((exp_dir / "base_chain_config.json").read_text())
        assert any(s.get("step_type", "").upper() == "TOOL" for s in chain_config.get("steps", []))

    def test_build_frozen_steps_preset(self, tmp_output, template_base, sample_dataset):
        """Build experiment with frozen steps (Single-Step Focus)."""
        sys.path.insert(0, str(REPO_ROOT / "master_api" / "src" / "folder_constructor"))
        from chain_experiment_builder import build_chain_experiment

        preset = EVOLUTION_PRESETS["Single-Step Focus"]
        steps = copy.deepcopy(preset["steps"])
        config = json.dumps({"steps": steps})
        frozen_steps = [s["number"] for s in steps if s.get("frozen")]

        spec = {
            "name": "test-frozen",
            "description": "Test frozen steps",
            "target_column": "answer",
            "base_chain_config": config,
            "llm_model": "test-model",
            "max_iterations": 10,
            "validation_criteria": {},
            "evolution_mode": "single_step",
            "step_number": 2,
            "frozen_steps": frozen_steps,
        }

        exp_dir = build_chain_experiment(
            spec=spec,
            output_root=tmp_output,
            template_base=template_base,
            dataset_path=sample_dataset,
        )

        assert exp_dir.exists()
        baseline = (exp_dir / "initial_programs" / "baseline.py").read_text()
        assert "CHAIN_CONFIG_JSON" in baseline

    def test_baseline_produces_valid_json(self, tmp_output, template_base, sample_dataset):
        """baseline.py entrypoint() should produce valid chain JSON."""
        sys.path.insert(0, str(REPO_ROOT / "master_api" / "src" / "folder_constructor"))
        from chain_experiment_builder import build_chain_experiment

        steps = CHAIN_TEMPLATES["Two-Step Reasoning"]["steps"]
        config = json.dumps({"steps": copy.deepcopy(steps)})

        spec = {
            "name": "test-baseline-json",
            "description": "Test",
            "target_column": "answer",
            "base_chain_config": config,
            "llm_model": "test-model",
            "max_iterations": 10,
            "validation_criteria": {},
            "evolution_mode": "full_chain",
        }

        exp_dir = build_chain_experiment(
            spec=spec,
            output_root=tmp_output,
            template_base=template_base,
            dataset_path=sample_dataset,
        )

        baseline_path = exp_dir / "initial_programs" / "baseline.py"
        baseline_code = baseline_path.read_text()

        ns: dict = {}
        exec(compile(baseline_code, str(baseline_path), "exec"), ns)

        result = ns["entrypoint"]()
        assert "chain_config_json" in result

        chain_json = result["chain_config_json"]
        parsed = json.loads(chain_json)
        assert "steps" in parsed
        assert len(parsed["steps"]) == 2

        cfg = _preset_to_chain_config(parsed["steps"])
        spec_built = _build_chain_spec(cfg)
        assert len(spec_built.steps) == 2


# =====================================================================
# Tests: chain_validation module
# =====================================================================

from chain_validation import validate_chain_spec, _validate_dag


class TestChainValidation:
    """Test DAG and structural validation."""

    def test_valid_dag(self):
        steps = [
            LLMStep(number=1, title="A", step_type="llm", aim="A", stage_action="A"),
            LLMStep(number=2, title="B", step_type="llm", aim="B", stage_action="B", dependencies=[1]),
        ]
        _validate_dag(steps)

    def test_cycle_detected(self):
        steps = [
            LLMStep(number=1, title="A", step_type="llm", aim="A", stage_action="A", dependencies=[2]),
            LLMStep(number=2, title="B", step_type="llm", aim="B", stage_action="B", dependencies=[1]),
        ]
        with pytest.raises(ValueError, match="depends on later step"):
            _validate_dag(steps)

    def test_validate_full_chain_mode(self):
        raw = {
            "steps": [
                {"number": 1, "step_type": "llm", "title": "A", "aim": "A", "stage_action": "A", "dependencies": []},
                {"number": 2, "step_type": "llm", "title": "B", "aim": "B", "stage_action": "B", "dependencies": [1]},
            ]
        }
        spec = validate_chain_spec(
            raw,
            mode="full_chain",
            full_chain_config={"max_steps": 10, "allowed_step_types": ["llm", "tool"]},
        )
        assert len(spec.steps) == 2


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
