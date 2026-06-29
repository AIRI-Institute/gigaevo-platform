"""Validation module for ${task_name} (self-contained)."""

from typing import Any, Dict, List
from statistics import mean

import evaluate
import re


# Configuration from template placeholders
VALIDATION_TYPE = "${validation_type}"  # "Binary (0/1)" or "Continuous (0..1)"
METRIC = "${metric}"  # "exact_match", "substring", "regexp" (Binary) or "rouge1", "rouge2", "rougeL", "bert_score", "bleu" (Continuous)
FAILURE_VALUE = ${failure_value}
FAILURE_REPLACEMENT = ${failure_replacement}
EXTRACTION_PATTERN = r"${regexp_pattern}"  # Regexp pattern for extraction/validation


def _collect_auxiliary_metrics(outputs: Dict[str, Any], failure_value: Any = None) -> Dict[str, float]:
    """Collect auxiliary metrics: avg_cost_utilization and avg_extraction_failures."""
    predictions: List[Any] = outputs["predictions"]
    call_logs = outputs["call_logs"]  # List[List[CallLog]] or List[List[Dict[str, float]]]

    flat_logs = [cl[0] for cl in call_logs if cl]

    def _log_val(cl: Any, key: str) -> float:
        # Supports both dataclass with attributes and dicts
        try:
            return float(getattr(cl, key))
        except Exception:
            try:
                return float(cl.get(key, 0.0))
            except Exception:
                return 0.0

    # Calculate extraction failures
    if failure_value is not None:
        extraction_failures = mean(pred == failure_value for pred in predictions) if predictions else 0.0
    else:
        extraction_failures = 0.0

    # Calculate total cost utilization (prompt + response)
    if flat_logs:
        avg_total_util = mean(
            _log_val(cl, "prompt_cost_utilization") + _log_val(cl, "response_cost_utilization")
            for cl in flat_logs
        )
    else:
        avg_total_util = 0.0

    return {
        "avg_extraction_failures": extraction_failures,
        "avg_cost_utilization": avg_total_util,
    }


def _compare_substring(gt: str, pred: str) -> int:
    """Check if prediction is contained in ground truth."""
    return 1 if str(pred).strip().lower() in str(gt).strip().lower() else 0


def _validate_binary(y_true: List[Any], y_pred: List[Any], metric: str, failure_value: Any, failure_replacement: Any) -> float:
    """Binary validation: returns accuracy (0-1)."""
    if not y_true:
        return 0.0

    # Replace failure values
    if failure_value is not None and failure_replacement is not None:
        y_pred = [failure_replacement if pred == failure_value else pred for pred in y_pred]

    if metric == "substring":
        scores = [_compare_substring(gt, pred) for gt, pred in zip(y_true, y_pred)]
        return sum(scores) / len(scores)
    else:
        # Default to exact_match (HuggingFace metric) & includes regexp case
        exact_match = evaluate.load("exact_match")
        references = [str(gt) for gt in y_true]
        predictions = [str(pred) for pred in y_pred]
        result = exact_match.compute(predictions=predictions, references=references)
        return float(result["exact_match"])


def _validate_continuous(y_true: List[Any], y_pred: List[Any], metric: str, failure_value: Any, failure_replacement: Any) -> float:
    """Continuous validation using HuggingFace evaluate metrics."""
    if not y_true:
        return 0.0

    # Replace failure values (instead of filtering them out to avoid overestimating metric)
    if failure_value is not None and failure_replacement is not None:
        y_pred = [failure_replacement if pred == failure_value else pred for pred in y_pred]

    references = [str(gt) for gt in y_true]
    predictions = [str(pred) for pred in y_pred]

    try:
        if metric in ["ROUGE-1", "ROUGE-2", "ROUGE-L"]:
            rouge = evaluate.load("rouge")
            result = rouge.compute(predictions=predictions, references=references)
            # Map to HuggingFace evaluate result keys
            metric_key_map = {"ROUGE-1": "rouge1", "ROUGE-2": "rouge2", "ROUGE-L": "rougeL"}
            return float(result[metric_key_map[metric]])

        elif metric in ["BERTScore", "bert_score"]:
            bertscore = evaluate.load("bertscore")
            result = bertscore.compute(predictions=predictions, references=references, lang="en")
            # Return mean F1 score
            return float(mean(result["f1"]))

        elif metric == "BLEU":
            bleu = evaluate.load("bleu")
            # BLEU expects tokenized input
            result = bleu.compute(
                predictions=[p.split() for p in predictions],
                references=[[r.split()] for r in references]
            )
            return float(result["bleu"])

        else:
            # Default to ROUGE-L
            rouge = evaluate.load("rouge")
            result = rouge.compute(predictions=predictions, references=references)
            return float(result["rougeL"])

    except Exception as e:
        print(f"Warning: Metric calculation failed: {e}")
        return 0.0


def _compute_fitness(y_true: List[Any], y_pred: List[Any]) -> float:
    """Compute fitness based on validation type."""
    if VALIDATION_TYPE == "Binary (0/1)":
        return _validate_binary(y_true, y_pred, METRIC, FAILURE_VALUE, FAILURE_REPLACEMENT)
    else:  # Continuous (0..1)
        return _validate_continuous(y_true, y_pred, METRIC, FAILURE_VALUE, FAILURE_REPLACEMENT)


def validate(context, outputs) -> Dict[str, float]:
    target_field = "${target_field}"

    train_dataset = context["train_dataset"]
    train_outputs = outputs["train"]
    y_true = train_dataset[target_field].tolist()
    y_pred = train_outputs["predictions"]

    metrics = _collect_auxiliary_metrics(train_outputs, FAILURE_VALUE)
    fitness = _compute_fitness(y_true, y_pred)
    metrics["fitness"] = fitness

    val_dataset = context.get("val_dataset")
    if val_dataset is not None:
        val_outputs = outputs["val"]
        y_true_val = val_dataset[target_field].tolist()
        y_pred_val = val_outputs["predictions"]
        val_fitness = _compute_fitness(y_true_val, y_pred_val)
        metrics["val_fitness"] = val_fitness
    else:
        metrics["val_fitness"] = -1.0

    return {**metrics, "is_valid": 1}
