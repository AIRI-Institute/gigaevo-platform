"""Chain Feedback Service for CARL experiments.

This service provides functionality to generate structured feedback from chain
execution results, similar to the ChainFeedbackFormatter in gigaevo-core-internal.

The feedback can be used as additional reflection for mutation prompts, helping
the LLM understand what went wrong and how to improve the chain.

Three feedback templates are available:
  - DETAILED:    Full input/output pairs, per-step analysis, token usage, suggestions
  - SUMMARY:     Brief performance overview with stats
  - ERRORS_ONLY: Only incorrect examples for minimal token cost
"""

from __future__ import annotations

import json
import re
import unicodedata
import string as _string
from typing import Any, Dict, List, Optional
from enum import Enum
import logging

logger = logging.getLogger(__name__)


class FeedbackTemplate(str, Enum):
    """Available feedback templates."""

    DETAILED = "detailed"  # Full feedback with all sections
    SUMMARY = "summary"  # Brief performance overview
    ERRORS_ONLY = "errors_only"  # Focus on failures and errors


# ---------------------------------------------------------------------------
# Normalisation helpers (mirrored from validate.py so the service is standalone)
# ---------------------------------------------------------------------------


def _normalize_text(s: str) -> str:
    """Normalize text for comparison (lowercase, strip punct/articles, collapse ws)."""
    s = unicodedata.normalize("NFD", str(s))
    s = s.lower()
    s = "".join(ch for ch in s if ch not in _string.punctuation)
    s = re.sub(r"\b(a|an|the)\b", " ", s)
    return " ".join(s.split())


def _normalize_numeric(s: str) -> str:
    s = str(s).strip()
    try:
        if "." in s:
            num = float(s)
            return str(int(num)) if num.is_integer() else str(num)
        return str(int(float(s))) if s else ""
    except (ValueError, TypeError):
        return s


def _is_correct(pred: str, gt: str) -> bool:
    """Determine if a prediction matches ground truth."""
    p = _normalize_text(str(pred).strip())
    g = _normalize_text(str(gt).strip())
    if p == g:
        return True
    try:
        pn = _normalize_numeric(str(pred).strip())
        gn = _normalize_numeric(str(gt).strip())
        if pn and gn and pn == gn:
            return True
    except Exception:
        pass
    return False


def _trunc(text: str, limit: int = 200) -> str:
    """Truncate text with ellipsis."""
    text = str(text)
    return text if len(text) <= limit else text[:limit] + "..."


# ---------------------------------------------------------------------------
# Enriched result helpers
# ---------------------------------------------------------------------------


def _aggregate_tokens(results: List[Dict[str, Any]]) -> Dict[str, int]:
    """Aggregate token usage across all results."""
    prompt = completion = 0
    for r in results:
        tu = r.get("token_usage") or {}
        if isinstance(tu, dict):
            prompt += tu.get("prompt", 0) or tu.get("prompt_tokens", 0)
            completion += tu.get("completion", 0) or tu.get("completion_tokens", 0)
    return {"prompt": prompt, "completion": completion, "total": prompt + completion}


def _classify_errors(results: List[Dict[str, Any]]) -> Dict[str, int]:
    """Classify errors into categories."""
    wrong_answer = 0
    chain_error = 0
    timeout = 0
    for r in results:
        pred = str(r.get("prediction", ""))
        gt = str(r.get("ground_truth", ""))
        if _is_correct(pred, gt):
            continue
        err_type = r.get("error_type", "")
        if err_type == "timeout":
            timeout += 1
        elif not r.get("success"):
            chain_error += 1
        else:
            wrong_answer += 1
    return {"wrong_answer": wrong_answer, "chain_error": chain_error, "timeout": timeout}


class ChainFeedbackFormatter:
    """Format chain execution results into structured feedback for mutations.

    This class processes chain execution results and generates markdown feedback
    that can be included in mutation prompts to guide chain evolution.

    Expected result structure (enriched):
    {
        "input_text": str,          # input given to the chain
        "prediction": str,
        "ground_truth": str,
        "success": bool,            # chain executed without exceptions
        "time": float,
        "step_results": [...],
        "full_output": str,
        "token_usage": {},
        "error_type": str,          # "timeout" | "exception" | ""
        "error_message": str,
    }
    """

    def __init__(self, template: FeedbackTemplate = FeedbackTemplate.DETAILED):
        if template is None:
            template = FeedbackTemplate.DETAILED
        elif isinstance(template, str):
            try:
                template = FeedbackTemplate(template)
            except ValueError:
                raise ValueError(
                    f"Invalid feedback_template: '{template}'. Must be one of: {[t.value for t in FeedbackTemplate]}"
                )
        elif not isinstance(template, FeedbackTemplate):
            raise ValueError(
                f"Invalid feedback_template type: {type(template)}. Must be a string or FeedbackTemplate enum."
            )
        self.template = template

    def format_results(self, results: List[Dict[str, Any]], target_field: str = "answer") -> str:
        """Format chain execution results into structured markdown feedback."""
        if not results:
            return "## Chain Execution Feedback\n\nNo execution results available."

        if self.template == FeedbackTemplate.SUMMARY:
            return self._format_summary(results, target_field)
        elif self.template == FeedbackTemplate.ERRORS_ONLY:
            return self._format_errors_only(results, target_field)
        else:
            return self._format_detailed(results, target_field)

    # -----------------------------------------------------------------
    # SUMMARY template
    # -----------------------------------------------------------------
    def _format_summary(self, results: List[Dict[str, Any]], target_field: str) -> str:
        total = len(results)
        correct = sum(1 for r in results if _is_correct(str(r.get("prediction", "")), str(r.get("ground_truth", ""))))
        accuracy = correct / total if total else 0.0
        exec_ok = sum(1 for r in results if r.get("success"))
        times = [r.get("time", 0.0) for r in results if r.get("time") is not None]
        avg_time = sum(times) / len(times) if times else 0.0
        tokens = _aggregate_tokens(results)

        lines = [
            "## Chain Execution Feedback",
            "",
            "### Performance Summary",
            f"- **Total samples**: {total}",
            f"- **Correct answers**: {correct} ({accuracy:.1%})",
            f"- **Incorrect answers**: {total - correct}",
            f"- **Chain executions OK**: {exec_ok}",
            f"- **Chain errors/timeouts**: {total - exec_ok}",
            f"- **Avg execution time**: {avg_time:.2f}s",
        ]
        if tokens["total"]:
            lines.append(
                f"- **Tokens**: prompt={tokens['prompt']}, completion={tokens['completion']}, total={tokens['total']}"
            )
        lines.append("")
        return "\n".join(lines)

    # -----------------------------------------------------------------
    # ERRORS_ONLY template – only incorrect / failed examples
    # -----------------------------------------------------------------
    def _format_errors_only(self, results: List[Dict[str, Any]], target_field: str) -> str:
        total = len(results)
        wrong = [r for r in results if not _is_correct(str(r.get("prediction", "")), str(r.get("ground_truth", "")))]
        correct_count = total - len(wrong)
        accuracy = correct_count / total if total else 0.0
        err_classes = _classify_errors(results)

        lines = [
            "## Chain Execution Feedback",
            "",
            f"**Accuracy**: {accuracy:.1%} ({correct_count} correct / {total} total)",
            "",
        ]

        if err_classes["wrong_answer"] or err_classes["chain_error"] or err_classes["timeout"]:
            lines.append(
                "**Error breakdown**: "
                f"wrong_answer={err_classes['wrong_answer']}, "
                f"chain_error={err_classes['chain_error']}, "
                f"timeout={err_classes['timeout']}"
            )
            lines.append("")

        if wrong:
            lines.append(f"### Incorrect Examples ({len(wrong)} total)")
            lines.append("")
            for i, r in enumerate(wrong[:10], 1):
                pred = _trunc(str(r.get("prediction", "")))
                gt = _trunc(str(r.get("ground_truth", "")))
                inp = _trunc(str(r.get("input_text", "")), 300)
                err_type = r.get("error_type", "")
                err_msg = _trunc(str(r.get("error_message", "")), 150)

                lines.append(f"**{i}.** (time: {r.get('time', 0):.1f}s)")
                if inp:
                    lines.append(f"   - Input: {inp}")
                lines.append(f"   - Expected: {gt}")
                lines.append(f"   - Got: {pred}")
                if err_type:
                    lines.append(f"   - Error: [{err_type}] {err_msg}")
                elif not r.get("success"):
                    lines.append(f"   - Note: chain execution reported failure")
                lines.append("")

            if len(wrong) > 10:
                lines.append(f"... and {len(wrong) - 10} more incorrect examples")
                lines.append("")
        else:
            lines.append("All examples are correct!")
            lines.append("")

        return "\n".join(lines)

    # -----------------------------------------------------------------
    # DETAILED template – full input/output pairs + per-step analysis
    # -----------------------------------------------------------------
    def _format_detailed(self, results: List[Dict[str, Any]], target_field: str) -> str:
        total = len(results)
        correct = sum(1 for r in results if _is_correct(str(r.get("prediction", "")), str(r.get("ground_truth", ""))))
        accuracy = correct / total if total else 0.0
        exec_ok = sum(1 for r in results if r.get("success"))
        times = [r.get("time", 0.0) for r in results if r.get("time") is not None]
        avg_time = sum(times) / len(times) if times else 0.0
        max_time = max(times) if times else 0.0
        tokens = _aggregate_tokens(results)
        err_classes = _classify_errors(results)

        lines = [
            "## Chain Execution Feedback",
            "",
            "### Performance Summary",
            f"- **Total samples**: {total}",
            f"- **Correct answers**: {correct} ({accuracy:.1%})",
            f"- **Incorrect answers**: {total - correct}",
            f"- **Chain executions OK**: {exec_ok}  |  errors/timeouts: {total - exec_ok}",
            f"- **Avg execution time**: {avg_time:.2f}s  |  max: {max_time:.2f}s",
        ]
        if tokens["total"]:
            lines.append(
                f"- **Tokens**: prompt={tokens['prompt']}, completion={tokens['completion']}, total={tokens['total']}"
            )
        if any(v > 0 for v in err_classes.values()):
            lines.append(
                f"- **Error types**: wrong_answer={err_classes['wrong_answer']}, "
                f"chain_error={err_classes['chain_error']}, timeout={err_classes['timeout']}"
            )
        lines.append("")

        # --- Input / Output pairs (up to 5) ---
        lines.append("### Sample Input / Output Pairs")
        lines.append("")
        sample = results[:5]
        for i, r in enumerate(sample, 1):
            pred = _trunc(str(r.get("prediction", "")))
            gt = _trunc(str(r.get("ground_truth", "")))
            inp = _trunc(str(r.get("input_text", "")), 300)
            ok = _is_correct(str(r.get("prediction", "")), str(r.get("ground_truth", "")))
            status = "CORRECT" if ok else "WRONG"

            lines.append(f"**{i}. [{status}]** (time: {r.get('time', 0):.1f}s)")
            if inp:
                lines.append(f"   - Input: {inp}")
            lines.append(f"   - Expected: {gt}")
            lines.append(f"   - Got: {pred}")

            err_type = r.get("error_type", "")
            err_msg = _trunc(str(r.get("error_message", "")), 150)
            if err_type:
                lines.append(f"   - Error: [{err_type}] {err_msg}")
            elif not r.get("success"):
                lines.append(f"   - Note: chain execution reported failure")
            lines.append("")

        # --- Per-step breakdown for failed examples ---
        wrong = [r for r in results if not _is_correct(str(r.get("prediction", "")), str(r.get("ground_truth", "")))]
        if wrong:
            lines.append("### Per-Step Breakdown (Failed Examples)")
            lines.append("")
            for j, r in enumerate(wrong[:3], 1):
                gt = _trunc(str(r.get("ground_truth", "")), 80)
                pred = _trunc(str(r.get("prediction", "")), 80)
                lines.append(f"**Failed #{j}**: expected={gt}  got={pred}")
                step_results = r.get("step_results") or []
                if step_results:
                    for s in step_results:
                        step_num = s.get("step_number", "?")
                        step_title = s.get("step_title", "unnamed")
                        step_ok = "OK" if s.get("success") else "FAIL"
                        step_time = s.get("execution_time", 0)
                        step_time_str = f"{step_time:.2f}s" if step_time else "N/A"
                        err = s.get("error_message", "")
                        line = f"   - Step {step_num} ({step_title}): {step_ok} [{step_time_str}]"
                        if err:
                            line += f"  error: {_trunc(err, 120)}"
                        lines.append(line)
                else:
                    lines.append("   (no step-level data)")
                lines.append("")

        # --- Improvement suggestions ---
        lines.extend(self._generate_improvement_suggestions(accuracy, wrong, total, err_classes))

        return "\n".join(lines)

    # -----------------------------------------------------------------
    # Improvement suggestions
    # -----------------------------------------------------------------
    def _generate_improvement_suggestions(
        self,
        accuracy: float,
        wrong: List[Dict[str, Any]],
        total: int,
        err_classes: Dict[str, int],
    ) -> List[str]:
        lines = ["### Improvement Suggestions", ""]
        suggestions: list[str] = []

        if accuracy < 0.3:
            suggestions.append("Very low accuracy (<30%). Consider completely redesigning the chain approach.")
        elif accuracy < 0.5:
            suggestions.append("Low accuracy (<50%). The current approach may not be suitable for this task.")
        elif accuracy < 0.7:
            suggestions.append("Moderate accuracy (50-70%). Consider refining reasoning steps or adding more context.")
        elif accuracy < 0.9:
            suggestions.append("Good accuracy (70-90%). Fine-tune the prompts and step configurations.")

        if total < 10:
            suggestions.append("Very small sample size. Consider running on more samples for reliable evaluation.")

        if err_classes.get("timeout", 0) > 0:
            suggestions.append(
                f"{err_classes['timeout']} timeout(s). Consider simplifying the chain or adding timeout handling."
            )
        if err_classes.get("chain_error", 0) > 0:
            suggestions.append(f"{err_classes['chain_error']} chain error(s). Review step configs and dependencies.")

        if wrong:
            step_failures = 0
            for r in wrong:
                step_failures += sum(1 for s in (r.get("step_results") or []) if not s.get("success", True))
            if step_failures:
                suggestions.append(f"{step_failures} step-level failure(s) detected in incorrect examples.")

        if suggestions:
            for s in suggestions:
                lines.append(f"- {s}")
        else:
            lines.append(
                "- Chain is performing well. Consider increasing sample size or adding more complex reasoning."
            )
        lines.append("")
        return lines


# ---------------------------------------------------------------------------
# Public convenience functions
# ---------------------------------------------------------------------------


def generate_feedback_from_results(
    results: List[Dict[str, Any]],
    target_field: str = "answer",
    template: FeedbackTemplate = FeedbackTemplate.DETAILED,
) -> str:
    """Convenience function to generate feedback from chain results.

    Args:
        results: List of execution results (enriched with input_text, token_usage, etc.)
        target_field: Name of the target field
        template: Feedback template to use

    Returns:
        Formatted markdown feedback string
    """
    formatter = ChainFeedbackFormatter(template=template)
    return formatter.format_results(results, target_field)


def parse_feedback_from_artifact(
    artifact: Dict[str, Any],
    template: FeedbackTemplate = FeedbackTemplate.DETAILED,
) -> str:
    """Parse feedback from a validation artifact dictionary.

    This function is compatible with the artifact format produced by chain validators
    in gigaevo-core-internal.

    Args:
        artifact: Validation artifact dictionary with summary, category_breakdown, failed_examples, etc.
        template: Feedback template to use

    Returns:
        Formatted markdown feedback string
    """
    if not artifact or not isinstance(artifact, dict):
        return "## Chain Execution Feedback\n\nNo artifact data available."

    lines = ["## Chain Execution Feedback", ""]

    # Summary section
    if "summary" in artifact and template != FeedbackTemplate.ERRORS_ONLY:
        s = artifact["summary"]
        lines.append("### Performance Summary")
        lines.append(f"- **Total samples**: {s.get('total_samples', 'N/A')}")
        lines.append(f"- **Correct**: {s.get('correct', 'N/A')}")
        lines.append(f"- **Incorrect**: {s.get('incorrect', 'N/A')}")

        fitness = s.get("fitness_exact_match", 0)
        lines.append(f"- **Accuracy**: {fitness:.1%}")

        if s.get("extraction_failures", 0) > 0:
            lines.append(f"- **Extraction failures**: {s['extraction_failures']:.1%}")
        lines.append("")

    # Category breakdown
    if "category_breakdown" in artifact and template == FeedbackTemplate.DETAILED:
        lines.append("### Performance by Category")
        for cat, stats in artifact["category_breakdown"].items():
            if stats.get("total", 0) > 0:
                acc = stats.get("accuracy", 0)
                lines.append(f"- **{cat}**: {stats['correct']}/{stats['total']} ({acc:.1%})")
        lines.append("")

    # Failed examples
    if "failed_examples" in artifact and artifact["failed_examples"]:
        if template == FeedbackTemplate.ERRORS_ONLY:
            lines.append(f"### Failed Examples ({len(artifact['failed_examples'])} total)")
        else:
            lines.append("### Failed Examples")
        lines.append("")

        failed = artifact["failed_examples"][: 10 if template == FeedbackTemplate.ERRORS_ONLY else 5]
        for i, ex in enumerate(failed, 1):
            lines.append(f"{i}. **Question**: {ex.get('question', 'N/A')}")
            lines.append(f"   - Expected: {ex.get('gold_answer', 'N/A')}")
            lines.append(f"   - Got: {ex.get('tool_extracted_answer', 'N/A')}")
            if ex.get("category"):
                lines.append(f"   - Category: {ex['category']}")
            lines.append("")

        if len(artifact["failed_examples"]) > len(failed):
            remaining = len(artifact["failed_examples"]) - len(failed)
            lines.append(f"... and {remaining} more failed examples")
            lines.append("")

    # Improvement suggestions
    if "next_steps" in artifact and artifact["next_steps"] and template != FeedbackTemplate.ERRORS_ONLY:
        lines.append("### Improvement Suggestions")
        for step in artifact["next_steps"]:
            lines.append(f"- {step}")
        lines.append("")

    return "\n".join(lines) if len(lines) > 2 else "No feedback available."
