"""Validation module for ${task_name} (self-contained)."""

from typing import Any, Dict, List
from statistics import mean

import os
import evaluate
import re
import sys
from pathlib import Path

_current_dir = Path(__file__).parent if "__file__" in globals() else Path.cwd()
if str(_current_dir) not in sys.path:
    sys.path.insert(0, str(_current_dir))

from helper import (
    parse_chain_config_json,
    execute_chain_on_dataset,
    _extract_numeric_answer,
)


VALIDATION_TYPE = "${validation_type}"
METRIC = "${metric}"
EXTRACTION_PATTERN = r"${regexp_pattern}"
def _get_adaptive_sample_size(dataset_size: int, default_max: int = 16, is_train: bool = True) -> int:
    """Calculate sample size based on dataset size.
    
    - Small datasets (< 50): use all or 80% of data
    - Medium datasets (50-200): use 30% of data, max 32
    - Large datasets (> 200): use fixed limit (default_max)
    """
    if dataset_size <= 0:
        return 0
    
    if is_train:
        env_max = os.getenv("CARL__MAX_TRAIN_SAMPLES")
    else:
        env_max = os.getenv("CARL__MAX_VAL_SAMPLES")
    
    if env_max:
        return min(int(env_max), dataset_size)
    
    if dataset_size < 50:
        return max(1, int(dataset_size * 0.8))
    elif dataset_size <= 200:
        return min(32, max(16, int(dataset_size * 0.3)))
    else:
        return min(default_max, dataset_size)


def _compare_substring(gt: str, pred: str) -> int:
    """Check if prediction is contained in ground truth (case-insensitive)."""
    return 1 if str(pred).strip().lower() in str(gt).strip().lower() else 0


def _normalize_numeric(s: str) -> str:
    """Normalize numeric string for comparison (e.g., "42.0" -> "42", " 42 " -> "42")."""
    s = str(s).strip()
    try:
        if "." in s:
            num = float(s)
            if num.is_integer():
                return str(int(num))
            return str(num)
        else:
            return str(int(float(s))) if s else ""
    except (ValueError, TypeError):
        return s


def _prepare_labels(results: List[Dict[str, Any]]) -> tuple[list[str], list[str]]:
    """Prepare ground truth and prediction texts for metric calculation."""
    y_true: list[str] = []
    y_pred: list[str] = []

    for r in results:
        gt = str(r.get("ground_truth", "")).strip()
        
        if VALIDATION_TYPE == "Binary (0/1)" and METRIC in ["exact_match", "equality"]:
            pred = str(r.get("prediction", "")).strip()
            if not pred:
                full_output = str(r.get("full_output", "")).strip()
                pred = _extract_numeric_answer(full_output)
            y_true.append(_normalize_numeric(gt))
            y_pred.append(_normalize_numeric(pred))
        elif METRIC == "regexp" and EXTRACTION_PATTERN:
            raw_text = str(r.get("full_output") or r.get("prediction", ""))
            m = re.search(EXTRACTION_PATTERN, raw_text)
            text = m.group(1).strip() if m else str(r.get("prediction", "")).strip()
            y_true.append(gt)
            y_pred.append(text)
        else:
            raw_text = str(r.get("full_output") or r.get("prediction", ""))
            y_true.append(gt)
            y_pred.append(raw_text)

    return y_true, y_pred


def _validate_binary(y_true: List[Any], y_pred: List[Any], metric: str) -> float:
    """Binary validation: returns accuracy (0-1)."""
    if not y_true:
        return 0.0

    if metric == "substring":
        scores = [_compare_substring(gt, pred) for gt, pred in zip(y_true, y_pred)]
        return sum(scores) / len(scores)

    matches = [
        1 if str(gt).strip() == str(pred).strip() else 0
        for gt, pred in zip(y_true, y_pred)
    ]
    return sum(matches) / len(matches)


def _validate_continuous(y_true: List[Any], y_pred: List[Any], metric: str) -> float:
    """Continuous validation using HuggingFace evaluate metrics."""
    if not y_true:
        return 0.0

    references = [str(gt) for gt in y_true]
    predictions = [str(pred) for pred in y_pred]

    try:
        if metric in ["ROUGE-1", "ROUGE-2", "ROUGE-L"]:
            rouge = evaluate.load("rouge")
            result = rouge.compute(predictions=predictions, references=references)
            metric_key_map = {"ROUGE-1": "rouge1", "ROUGE-2": "rouge2", "ROUGE-L": "rougeL"}
            return float(result[metric_key_map[metric]])

        elif metric in ["BERTScore", "bert_score"]:
            bertscore = evaluate.load("bertscore")
            result = bertscore.compute(predictions=predictions, references=references, lang="en")
            return float(mean(result["f1"]))

        elif metric == "BLEU":
            bleu = evaluate.load("bleu")
            result = bleu.compute(
                predictions=[p.split() for p in predictions],
                references=[[r.split()] for r in references],
            )
            return float(result["bleu"])

        else:
            rouge = evaluate.load("rouge")
            result = rouge.compute(predictions=predictions, references=references)
            return float(result["rougeL"])

    except Exception as e:
        print(f"Warning: Metric calculation failed: {e}", file=sys.stderr)
        return 0.0


def _compute_fitness(results: List[Dict[str, Any]]) -> float:
    if not results:
        return 0.0

    y_true, y_pred = _prepare_labels(results)

    if VALIDATION_TYPE == "Binary (0/1)":
        return _validate_binary(y_true, y_pred, METRIC)
    else:
        return _validate_continuous(y_true, y_pred, METRIC)


def validate(context, outputs) -> Dict[str, float]:
    chain_config_json = outputs.get("chain_config_json")
    if not chain_config_json:
        return {
            "fitness": -1000.0,
            "val_fitness": -1.0,
            "avg_extraction_failures": 0.0,
            "avg_cost_utilization": 0.0,
            "avg_prompt_cost_utilization": 0.0,
            "avg_response_cost_utilization": 0.0,
            "is_valid": 0.0,
        }

    try:
        chain_config = parse_chain_config_json(chain_config_json)
    except Exception as e:
        print(f"ERROR: Failed to parse chain config JSON: {e}", file=sys.stderr)
        return {
            "fitness": -1000.0,
            "val_fitness": -1.0,
            "avg_extraction_failures": 0.0,
            "avg_cost_utilization": 0.0,
            "avg_prompt_cost_utilization": 0.0,
            "avg_response_cost_utilization": 0.0,
            "is_valid": 0.0,
        }

    train_df = context.get("train_dataset")
    val_df = context.get("val_dataset")
    target_column = context.get("target_column", "${target_field}")

    if train_df is None or len(train_df) == 0:
        return {
            "fitness": -1005.0,
            "val_fitness": -1.0,
            "avg_extraction_failures": 0.0,
            "avg_cost_utilization": 0.0,
            "avg_prompt_cost_utilization": 0.0,
            "avg_response_cost_utilization": 0.0,
            "is_valid": 0.0,
        }

    train_size = len(train_df)
    train_sample_size = _get_adaptive_sample_size(train_size, default_max=16, is_train=True)
    if train_sample_size < train_size:
        try:
            train_df = train_df.sample(n=train_sample_size, random_state=42)
        except Exception:
            train_df = train_df.head(train_sample_size)

    if val_df is not None and len(val_df) > 0:
        val_size = len(val_df)
        val_sample_size = _get_adaptive_sample_size(val_size, default_max=16, is_train=False)
        if val_sample_size < val_size:
            try:
                val_df = val_df.sample(n=val_sample_size, random_state=42)
            except Exception:
                val_df = val_df.head(val_sample_size)

    train_results = execute_chain_on_dataset(chain_config, train_df, target_column)
    val_results = execute_chain_on_dataset(chain_config, val_df, target_column) if val_df is not None and len(val_df) > 0 else []

    if not train_results:
        print("ERROR: No train results returned from execute_chain_on_dataset", file=sys.stderr)
        return {
            "fitness": -1000.0,
            "val_fitness": -1.0,
            "avg_extraction_failures": 0.0,
            "avg_cost_utilization": 0.0,
            "avg_prompt_cost_utilization": 0.0,
            "avg_response_cost_utilization": 0.0,
            "is_valid": 0.0,
        }

    fitness = _compute_fitness(train_results)
    val_fitness = _compute_fitness(val_results) if val_results else -1.0

    all_results = train_results + val_results
    times = [r.get("time", 0.0) for r in all_results if r.get("time") is not None]
    avg_time = float(mean(times)) if times else 0.0

    success_flags = [1.0 if r.get("success") else 0.0 for r in all_results]
    success_rate = float(mean(success_flags)) if success_flags else 0.0

    is_valid = 1.0 if (all_results and fitness >= 0.0) else 0.0

    return {
        "fitness": fitness,
        "val_fitness": val_fitness,
        "avg_extraction_failures": 0.0,
        "avg_cost_utilization": 0.0,
        "avg_prompt_cost_utilization": 0.0,
        "avg_response_cost_utilization": 0.0,
        "is_valid": is_valid,
    }
