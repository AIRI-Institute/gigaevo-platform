"""Validation module for ${task_name} (self-contained)."""

from typing import Any, Callable, Dict, List
from statistics import mean

${metric_import}

${custom_metric_impl}


def _collect_auxiliary_metrics(outputs: Dict[str, Any], failure_value: Any = None) -> Dict[str, float]:
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

    if failure_value is not None:
        extraction_failures = mean(pred == failure_value for pred in predictions) if predictions else 0.0
    else:
        extraction_failures = 0.0

    if flat_logs:
        avg_prompt_util = mean(_log_val(cl, "prompt_cost_utilization") for cl in flat_logs)
        avg_response_util = mean(_log_val(cl, "response_cost_utilization") for cl in flat_logs)
        avg_total_util = mean(_log_val(cl, "prompt_cost_utilization") + _log_val(cl, "response_cost_utilization") for cl in flat_logs)
    else:
        avg_prompt_util = 0.0
        avg_response_util = 0.0
        avg_total_util = 0.0

    return {
        "avg_extraction_failures": extraction_failures,
        "avg_prompt_cost_utilization": avg_prompt_util,
        "avg_response_cost_utilization": avg_response_util,
        "avg_cost_utilization": avg_total_util,
    }


def _validate_with_metric(
    y_true: List[Any],
    y_pred: List[Any],
    metric_fn: Callable,
    failure_value: Any = None,
    failure_replacement: Any = None,
) -> float:
    if failure_value is not None and failure_replacement is not None:
        y_pred = [failure_replacement if pred == failure_value else pred for pred in y_pred]
    try:
        return float(metric_fn(y_true, y_pred))
    except Exception as e:
        print(f"Warning: Metric calculation failed: {e}")
        return 0.0


def validate(context, outputs) -> Dict[str, float]:
    target_field = "${target_field}"
    metric_fn = ${metric_fn}
    failure_value = ${failure_value}
    failure_replacement = ${failure_replacement}

    train_dataset = context["train_dataset"]
    train_outputs = outputs["train"]
    y_true = train_dataset[target_field].tolist()
    y_pred = train_outputs["predictions"]

    metrics = _collect_auxiliary_metrics(train_outputs, failure_value)
    fitness = _validate_with_metric(y_true, y_pred, metric_fn, failure_value, failure_replacement)
    metrics["fitness"] = fitness

    val_dataset = context.get("val_dataset")
    if val_dataset is not None:
        val_outputs = outputs["val"]
        y_true_val = val_dataset[target_field].tolist()
        y_pred_val = val_outputs["predictions"]
        val_fitness = _validate_with_metric(
            y_true_val, y_pred_val, metric_fn, failure_value, failure_replacement
        )
        metrics["val_fitness"] = val_fitness
    else:
        metrics["val_fitness"] = -1.0

    return {**metrics, "is_valid": 1}
