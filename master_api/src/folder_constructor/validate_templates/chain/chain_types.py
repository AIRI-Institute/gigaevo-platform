"""Chain data structures for step-batched chain execution.

Ported from gigaevo-core-internal (problems/chains/types.py).
Uses Pydantic v2 for structural validation (field presence, types, constraints).
Semantic validation (DAG, topology, frozen equality) lives in chain_validation.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    constr,
    field_validator,
    model_validator,
)

STRUCTURED_FIELDS = {"aim", "stage_action", "reasoning_questions", "example_reasoning"}
METADATA_FIELDS = {"number", "title", "step_type", "dependencies", "frozen"}


class ToolConfig(BaseModel):
    """Tool step configuration with $-reference validation.

    Extra fields (tool_description, timeout, etc.) from UI presets are
    silently ignored — only tool_name and input_mapping are used at runtime.
    """

    model_config = ConfigDict(extra="ignore")

    tool_name: str
    input_mapping: dict[str, str]

    @model_validator(mode="after")
    def validate_dollar_refs(self) -> ToolConfig:
        import sys as _sys
        for param_name, ref in self.input_mapping.items():
            if not ref.startswith("$"):
                print(
                    f"[ToolConfig] WARNING: input_mapping['{param_name}'] = '{ref}' "
                    f"does not use $-reference syntax — may fail at runtime",
                    file=_sys.stderr,
                )
        return self


class StopCondition(BaseModel):
    """Optional per-step early-stop condition based on step output."""

    model_config = ConfigDict(extra="forbid")

    condition_type: Literal["contains", "regex"] = "contains"
    pattern: constr(min_length=1)  # type: ignore[valid-type]
    case_sensitive: bool = False


def _coerce_to_str(v: object) -> str:
    """Coerce list/tuple to joined string (common LLM mutation artefact)."""
    if isinstance(v, (list, tuple)):
        return " ".join(str(item) for item in v)
    return v  # type: ignore[return-value]


class LLMStep(BaseModel):
    """LLM reasoning step with structured fields."""

    model_config = ConfigDict(extra="ignore")

    number: int
    title: str
    step_type: Literal["llm"]
    dependencies: list[int] = Field(default_factory=list)
    frozen: bool = False

    aim: constr(min_length=1)  # type: ignore[valid-type]
    stage_action: constr(min_length=1)  # type: ignore[valid-type]

    reasoning_questions: str = ""
    example_reasoning: str = ""
    stop_condition: StopCondition | None = None

    @field_validator(
        "aim", "stage_action", "reasoning_questions", "example_reasoning", mode="before"
    )
    @classmethod
    def _coerce_sequences_to_str(cls, v: object) -> object:
        return _coerce_to_str(v)


class ToolStep(BaseModel):
    """Tool execution step with step_config."""

    model_config = ConfigDict(extra="ignore")

    number: int
    title: str
    step_type: Literal["tool"]
    dependencies: list[int] = Field(default_factory=list)
    frozen: bool = False

    step_config: ToolConfig


Step = Annotated[LLMStep | ToolStep, Field(discriminator="step_type")]


class RawChainSpec(BaseModel):
    """Pydantic model for parsing raw chain config output.

    Handles structural validation: field presence, types, constraints,
    $-reference syntax.  Extra fields from mmar_carl-style configs
    (e.g. step_context_queries) are silently ignored.
    """

    model_config = ConfigDict(extra="ignore")

    system_prompt: str = ""
    steps: list[Step] = Field(min_length=1)


class PromptBuilder(BaseModel):
    """CARL-style configurable prompt assembler.

    Templates use ``{placeholder}`` syntax.  Available placeholders per
    template are documented inline.
    """

    model_config = ConfigDict(extra="forbid")

    step_template: str = (
        "Step {number}. {title}\n"
        "Objective: {aim}\n"
        "Task: {stage_action}\n"
        "Questions: {reasoning_questions}\n"
        "Example reasoning: {example_reasoning}"
    )
    chain_template: str = "Data:\n{outer_context}\n\n{step_prompt}"
    history_template: str = (
        "Previous steps:\n{history}\n\n"
        "Based on the results of previous steps, "
        "perform the following task:\n{current_task}"
    )
    history_entry_template: str = "Step {number}. {title}\nResult: {result}\n"

    def format_step_prompt(self, step: LLMStep) -> str:
        return self.step_template.format(
            number=step.number,
            title=step.title,
            aim=step.aim,
            stage_action=step.stage_action,
            reasoning_questions=step.reasoning_questions,
            example_reasoning=step.example_reasoning,
        )

    def build_prompt(
        self,
        step: LLMStep,
        visible_history: list[str],
        outer_context: str,
        system_prompt: str,
    ) -> str:
        step_prompt = self.format_step_prompt(step)

        if visible_history:
            history_text = "\n".join(visible_history)
            step_prompt = self.history_template.format(
                history=history_text,
                current_task=step_prompt,
            )

        full_prompt = self.chain_template.format(
            outer_context=outer_context,
            step_prompt=step_prompt,
        )

        if system_prompt and system_prompt.strip():
            return f"System Instructions:\n{system_prompt}\n\n{full_prompt}"

        return full_prompt

    def format_history_entry(self, number: int, title: str, result: str) -> str:
        return self.history_entry_template.format(
            number=number,
            title=title,
            result=result,
        )


@dataclass
class ChainSpec:
    """Executable chain — validated, sorted, ready for runner."""

    system_prompt: str
    steps: list[LLMStep | ToolStep] = field(default_factory=list)
    prompt_builder: PromptBuilder = field(default_factory=PromptBuilder)


@dataclass
class ChainResult:
    """Result of running a chain on one sample."""

    history: list[str] = field(default_factory=list)
    final_output: str = ""
    step_outputs: list[str] = field(default_factory=list)
