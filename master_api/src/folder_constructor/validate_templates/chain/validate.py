"""Validation module for ${task_name} (self-contained)."""

from typing import Any, Dict, List, Optional
from statistics import mean
import json

import os
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
    _extract_prediction,
)

# Import chain feedback integration (rich DETAILED / ERRORS_ONLY templates)
try:
    from feedback_integration import generate_chain_feedback, add_feedback_to_validation_result
    _FEEDBACK_AVAILABLE = True
except ImportError:
    _FEEDBACK_AVAILABLE = False
    print("[Chain Feedback] feedback_integration module not available — basic fallback will be used", file=sys.stderr)


# ---------------------------------------------------------------------------
# Correctness helper  (prediction vs ground_truth, NOT exec_result.success)
# ---------------------------------------------------------------------------

def _is_correct(pred: str, gt: str) -> bool:
    """Determine if a prediction matches ground truth using normalised comparison."""
    p = _normalize_text(str(pred).strip())
    g = _normalize_text(str(gt).strip())
    if p == g:
        return True
    # Also try numeric normalisation (e.g. "42.0" == "42")
    try:
        pn = _normalize_numeric(str(pred).strip())
        gn = _normalize_numeric(str(gt).strip())
        if pn and gn and pn == gn:
            return True
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------
# BASIC feedback – guaranteed, zero external imports, never empty
# ---------------------------------------------------------------------------

def _build_basic_feedback(
    results: List[Dict[str, Any]],
    fitness: float,
    val_fitness: float,
    target_column: str,
) -> str:
    """Build a minimal but always-available feedback string.

    This function uses ONLY the standard library and the ``results`` list
    already computed by validate().  It is the *fallback* that fires when the
    richer ``feedback_integration`` / ``chain_feedback_service`` path fails.
    """
    total = len(results)
    if total == 0:
        return "## Chain Execution Feedback\n\nNo results — chain did not execute."

    exec_ok = sum(1 for r in results if r.get("success"))
    exec_fail = total - exec_ok
    correct = sum(1 for r in results if _is_correct(
        str(r.get("prediction", "")), str(r.get("ground_truth", ""))))
    incorrect = total - correct
    accuracy = correct / total if total > 0 else 0.0
    times = [r.get("time", 0.0) for r in results if r.get("time") is not None]
    avg_time = sum(times) / len(times) if times else 0.0

    # --- Token usage ---
    total_prompt_tokens = 0
    total_completion_tokens = 0
    for r in results:
        tu = r.get("token_usage") or {}
        total_prompt_tokens += tu.get("prompt", 0)
        total_completion_tokens += tu.get("completion", 0)

    lines: list[str] = [
        "## Chain Execution Feedback",
        "",
        "### Performance Summary",
        f"- **Total samples**: {total}",
        f"- **Correct answers**: {correct} ({accuracy:.1%})",
        f"- **Incorrect answers**: {incorrect}",
        f"- **Chain executions OK**: {exec_ok}",
        f"- **Chain errors/timeouts**: {exec_fail}",
        f"- **Fitness (train)**: {fitness:.4f}",
        f"- **Fitness (val)**: {val_fitness:.4f}" if val_fitness >= 0 else "",
        f"- **Avg execution time**: {avg_time:.2f}s",
    ]
    if total_prompt_tokens or total_completion_tokens:
        lines.append(f"- **Tokens**: prompt={total_prompt_tokens}, completion={total_completion_tokens}, total={total_prompt_tokens + total_completion_tokens}")
    lines.append("")

    # --- Input / Output pairs (up to 5) ---
    sample_results = results[:5]
    lines.append("### Sample Input / Output Pairs")
    lines.append("")
    for i, r in enumerate(sample_results, 1):
        pred_raw = str(r.get("prediction", ""))
        gt_raw = str(r.get("ground_truth", ""))
        is_ok = _is_correct(pred_raw, gt_raw)
        status = "CORRECT" if is_ok else "WRONG"
        input_text = str(r.get("input_text", ""))
        # Truncate long texts
        if len(input_text) > 300:
            input_text = input_text[:300] + "..."
        if len(pred_raw) > 200:
            pred_raw = pred_raw[:200] + "..."
        if len(gt_raw) > 200:
            gt_raw = gt_raw[:200] + "..."

        lines.append(f"**{i}. [{status}]** (time: {r.get('time', 0):.1f}s)")
        if input_text:
            lines.append(f"   - Input: {input_text}")
        lines.append(f"   - Expected: {gt_raw}")
        lines.append(f"   - Got: {pred_raw}")
        # Show error info if chain failed
        err_msg = r.get("error_message") or ""
        err_type = r.get("error_type") or ""
        if err_type:
            lines.append(f"   - Error: [{err_type}] {err_msg[:200]}")
        elif not r.get("success"):
            lines.append(f"   - Note: chain execution reported failure")
        lines.append("")

    # --- Incorrect examples (if any not already shown) ---
    wrong = [r for r in results if not _is_correct(
        str(r.get("prediction", "")), str(r.get("ground_truth", "")))]
    if wrong and len(wrong) > len([r for r in sample_results if not _is_correct(
            str(r.get("prediction", "")), str(r.get("ground_truth", "")))]):
        extra_wrong = [r for r in wrong if r not in sample_results][:3]
        if extra_wrong:
            lines.append("### Additional Incorrect Examples")
            lines.append("")
            for j, r in enumerate(extra_wrong, 1):
                pred_raw = str(r.get("prediction", ""))[:200]
                gt_raw = str(r.get("ground_truth", ""))[:200]
                lines.append(f"{j}. Expected: {gt_raw}  |  Got: {pred_raw}")
            lines.append("")

    return "\n".join(line for line in lines if line is not None)


VALIDATION_TYPE = "${validation_type}"
METRIC = "${metric}"
EXTRACTION_PATTERN = r"${regexp_pattern}"
FITNESS_MODE = "${fitness_mode}"  # "auto", "iou", "accuracy", "continuous"


def _get_adaptive_sample_size(dataset_size: int, default_max: int = 50, is_train: bool = True) -> int:
    """Calculate sample size based on dataset size.

    - Small datasets (≤ 100): use ALL data for reliable fitness signal
    - Medium datasets (101-500): use 50%, at least 50
    - Large datasets (> 500): use fixed limit (default_max)
    """
    if dataset_size <= 0:
        return 0

    if is_train:
        env_max = os.getenv("CARL__MAX_TRAIN_SAMPLES")
    else:
        env_max = os.getenv("CARL__MAX_VAL_SAMPLES")

    if env_max:
        return min(int(env_max), dataset_size)

    if dataset_size <= 100:
        return dataset_size
    elif dataset_size <= 500:
        return min(dataset_size, max(50, int(dataset_size * 0.5)))
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


def _normalize_text(s: str) -> str:
    """Normalize text for exact match comparison (HotpotQA-style).

    Steps: Unicode NFD → lowercase → remove punctuation → remove articles → collapse whitespace.
    """
    import unicodedata
    import string as _string
    s = unicodedata.normalize("NFD", str(s))
    s = s.lower()
    s = "".join(ch for ch in s if ch not in _string.punctuation)
    s = re.sub(r"\b(a|an|the)\b", " ", s)
    return " ".join(s.split())


def _extract_answer_with_pattern(text: str) -> str:
    """Try to extract answer using EXTRACTION_PATTERN, then fall back to numeric extraction."""
    if not text:
        return ""
    # 1) Try extraction pattern (e.g. "Answer: <answer>")
    if EXTRACTION_PATTERN:
        m = re.search(EXTRACTION_PATTERN, text, re.MULTILINE | re.IGNORECASE)
        if m:
            return m.group(1).strip()
    # 2) Fall back to numeric extraction
    return _extract_numeric_answer(text)


def _prepare_labels(results: List[Dict[str, Any]]) -> tuple[list[str], list[str]]:
    """Prepare ground truth and prediction texts for metric calculation."""
    y_true: list[str] = []
    y_pred: list[str] = []

    for r in results:
        gt = str(r.get("ground_truth", "")).strip()
        full_output = str(r.get("full_output", "")).strip()

        if VALIDATION_TYPE == "Binary (0/1)" and METRIC in ["exact_match", "equality"]:
            # Priority chain for prediction extraction:
            # 1) "prediction" field (if non-empty)
            # 2) Extraction pattern on full_output  (e.g. "Answer: <answer>")
            # 3) Extraction pattern on prediction   (strip prefix)
            # 4) Numeric extraction as last resort
            pred = str(r.get("prediction", "")).strip()

            # If prediction already present, try stripping pattern prefix
            if pred and EXTRACTION_PATTERN:
                m = re.search(EXTRACTION_PATTERN, pred, re.MULTILINE | re.IGNORECASE)
                if m:
                    pred = m.group(1).strip()

            # If still empty, extract from full_output
            if not pred:
                pred = _extract_answer_with_pattern(full_output)

            y_true.append(gt)
            y_pred.append(pred)
        elif METRIC == "regexp" and EXTRACTION_PATTERN:
            raw_text = full_output or str(r.get("prediction", ""))
            m = re.search(EXTRACTION_PATTERN, raw_text, re.MULTILINE | re.IGNORECASE)
            text = m.group(1).strip() if m else str(r.get("prediction", "")).strip()
            y_true.append(gt)
            y_pred.append(text)
        else:
            pred = str(r.get("prediction", "")).strip()
            raw_text = pred or full_output
            cleaned = _extract_prediction(raw_text) if raw_text else ""
            y_true.append(gt)
            y_pred.append(cleaned or raw_text)

    return y_true, y_pred


def _validate_binary(y_true: List[Any], y_pred: List[Any], metric: str) -> float:
    """Binary validation: returns accuracy (0-1)."""
    if not y_true:
        return 0.0

    if metric == "substring":
        scores = [_compare_substring(gt, pred) for gt, pred in zip(y_true, y_pred)]
        return sum(scores) / len(scores)

    # Use text normalization for robust exact match
    # (lowercase, remove articles/punctuation, collapse whitespace)
    matches = [
        1 if _normalize_text(gt) == _normalize_text(pred) else 0
        for gt, pred in zip(y_true, y_pred)
    ]
    return sum(matches) / len(matches)


def _word_tokenize(text: str) -> List[str]:
    """Whitespace tokenization — works for Cyrillic and Latin without NLTK."""
    return str(text).split()


class _WhitespaceTokenizer:
    """rouge_score-compatible tokenizer (``.tokenize()`` API)."""

    def tokenize(self, text: str) -> List[str]:
        return _word_tokenize(text)


def _lcs_length(a: List[str], b: List[str]) -> int:
    m, n = len(a), len(b)
    prev = [0] * (n + 1)
    for i in range(m):
        cur = [0] * (n + 1)
        for j in range(n):
            if a[i] == b[j]:
                cur[j + 1] = prev[j] + 1
            else:
                cur[j + 1] = max(cur[j + 1], prev[j + 1], cur[j])
        prev = cur
    return prev[n]


def _f1_from_overlap(overlap: int, pred_len: int, ref_len: int) -> float:
    if overlap <= 0 or pred_len <= 0 or ref_len <= 0:
        return 0.0
    precision = overlap / pred_len
    recall = overlap / ref_len
    return (2.0 * precision * recall / (precision + recall)) if (precision + recall) else 0.0


def _word_rouge_pair(pred: str, ref: str, metric: str) -> float:
    """Pure-Python word ROUGE fallback (no external deps)."""
    pred_t = _word_tokenize(pred)
    ref_t = _word_tokenize(ref)
    if metric == "ROUGE-L":
        lcs = _lcs_length(pred_t, ref_t)
        return _f1_from_overlap(lcs, len(pred_t), len(ref_t))
    if metric == "ROUGE-2":
        pred_bi = list(zip(pred_t, pred_t[1:]))
        ref_bi = list(zip(ref_t, ref_t[1:]))
        if not pred_bi or not ref_bi:
            return 0.0
        overlap = sum(1 for bg in pred_bi if bg in set(ref_bi))
        return _f1_from_overlap(overlap, len(pred_bi), len(ref_bi))
    if not pred_t or not ref_t:
        return 0.0
    ref_set = set(ref_t)
    overlap = sum(1 for tok in pred_t if tok in ref_set)
    return _f1_from_overlap(overlap, len(pred_t), len(ref_t))


def _word_rouge_corpus(
    predictions: List[str], references: List[str], metric: str,
) -> float:
    scores = [
        _word_rouge_pair(pred, ref, metric)
        for pred, ref in zip(predictions, references)
    ]
    return float(mean(scores)) if scores else 0.0


def _validate_rouge_word_split(
    predictions: List[str], references: List[str], metric: str,
) -> float:
    """Word-level ROUGE via rouge_score (Cyrillic-safe; no NLTK/HF evaluate)."""
    rouge_type_map = {
        "ROUGE-1": "rouge1",
        "ROUGE-2": "rouge2",
        "ROUGE-L": "rougeL",
    }
    rouge_type = rouge_type_map.get(metric, "rougeL")
    try:
        from rouge_score import rouge_scorer

        scorer = rouge_scorer.RougeScorer(
            [rouge_type],
            use_stemmer=False,
            tokenizer=_WhitespaceTokenizer(),
        )
        scores = [
            float(scorer.score(ref, pred)[rouge_type].fmeasure)
            for pred, ref in zip(predictions, references)
        ]
        return float(mean(scores)) if scores else 0.0
    except Exception as exc:
        print(
            f"Warning: rouge_score unavailable ({exc}); using built-in word ROUGE",
            file=sys.stderr,
        )
        return _word_rouge_corpus(predictions, references, metric)


def _validate_continuous(y_true: List[Any], y_pred: List[Any], metric: str) -> float:
    """Continuous validation (ROUGE via word-split; other metrics via evaluate)."""
    if not y_true:
        return 0.0

    references = [str(gt) for gt in y_true]
    predictions = [str(pred) for pred in y_pred]

    if metric in ["ROUGE-1", "ROUGE-2", "ROUGE-L"]:
        return _validate_rouge_word_split(predictions, references, metric)

    try:
        import evaluate

        if metric in ["BERTScore", "bert_score"]:
            bertscore = evaluate.load("bertscore")
            result = bertscore.compute(predictions=predictions, references=references, lang="en")
            return float(mean(result["f1"]))

        if metric == "BLEU":
            bleu = evaluate.load("bleu")
            result = bleu.compute(
                predictions=[p.split() for p in predictions],
                references=[[r.split()] for r in references],
            )
            return float(result["bleu"])

        return _validate_rouge_word_split(predictions, references, "ROUGE-L")

    except Exception as e:
        print(f"Warning: Metric calculation failed: {e}", file=sys.stderr)
        return 0.0


# ---------------------------------------------------------------------------
# TOOL-output fitness extractors (IoU, custom numeric fields, etc.)
# ---------------------------------------------------------------------------

def _try_extract_numeric_from_json(text: str, field_names: set[str]) -> List[float]:
    """Attempt to parse JSON and extract numeric fields by name."""
    if not text:
        return []
    try:
        data = json.loads(text)
    except Exception:
        return []
    candidates: List[float] = []

    def _collect(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if str(k).lower() in field_names:
                    try:
                        candidates.append(float(v))
                    except Exception:
                        pass
                _collect(v)
        elif isinstance(obj, list):
            for it in obj:
                _collect(it)
    _collect(data)
    return candidates


def _try_extract_iou_from_json(text: str) -> List[float]:
    """Attempt to parse JSON and extract IoU-like fields."""
    raw = _try_extract_numeric_from_json(text, {"iou", "miou", "mean_iou", "iou_score"})
    out: List[float] = []
    for val in raw:
        if val > 1.0 and val <= 100.0:
            val = val / 100.0
        if 0.0 <= val <= 1.0:
            out.append(val)
    return out


def _try_extract_pred_mask_path(text: str) -> Optional[str]:
    if not text:
        return None
    try:
        data = json.loads(text)
    except Exception:
        return None

    def _find(obj) -> Optional[str]:
        if isinstance(obj, dict):
            for k, v in obj.items():
                if str(k).lower() in {"pred_mask_path", "predicted_mask_path", "mask_path_pred"}:
                    return str(v)
                found = _find(v)
                if found:
                    return found
        elif isinstance(obj, list):
            for it in obj:
                found = _find(it)
                if found:
                    return found
        return None
    return _find(data)


def _compute_iou_from_masks(pred_path: str, gt_path: str) -> Optional[float]:
    try:
        from PIL import Image
        import numpy as np
        if not pred_path or not gt_path:
            return None
        if not os.path.isabs(pred_path):
            pred_path = os.path.abspath(pred_path)
        if not os.path.isabs(gt_path):
            gt_path = os.path.abspath(gt_path)
        pred = Image.open(pred_path).convert("L")
        gt = Image.open(gt_path).convert("L")
        if pred.size != gt.size:
            pred = pred.resize(gt.size)
        pred_arr = (np.array(pred) > 127).astype(np.uint8)
        gt_arr = (np.array(gt) > 127).astype(np.uint8)
        inter = int((pred_arr & gt_arr).sum())
        union = int((pred_arr | gt_arr).sum())
        if union == 0:
            return 0.0
        return float(inter / union)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Fitness computation modes
# ---------------------------------------------------------------------------

def _compute_fitness_iou(results: List[Dict[str, Any]]) -> float:
    """IoU-based fitness (e.g., segmentation tasks with TOOL steps returning IoU)."""
    if not results:
        print("[_compute_fitness_iou] No results provided", file=sys.stderr)
        return 0.0
    print(f"[_compute_fitness_iou] Processing {len(results)} result(s)", file=sys.stderr)

    ious: list[float] = []
    for idx, r in enumerate(results):
        text = str(r.get("full_output") or r.get("prediction") or "")
        print(f"[_compute_fitness_iou] Result {idx}: full_output length={len(text)}", file=sys.stderr)

        # 0) Predicted mask vs ground-truth mask
        pred_mask = _try_extract_pred_mask_path(text)
        gt_mask = str(r.get("context_mask_path") or "")
        if pred_mask and gt_mask:
            iou_val = _compute_iou_from_masks(pred_mask, gt_mask)
            if iou_val is not None:
                print(f"[_compute_fitness_iou] Result {idx}: IoU from mask comparison = {iou_val}", file=sys.stderr)
                ious.append(iou_val)
                continue

        # 1) JSON IoU fields
        json_iou = _try_extract_iou_from_json(text)
        if json_iou:
            print(f"[_compute_fitness_iou] Result {idx}: IoU from JSON = {json_iou}", file=sys.stderr)
            ious.extend(json_iou)
            continue

        # 2) Regex fallback
        m = re.search(r"(?:^|[^a-zA-Z])(iou|mean_iou)\s*[:=]?\s*([0-9]*\.?[0-9]+)", text, flags=re.IGNORECASE)
        if m:
            val = float(m.group(2))
            if val > 1.0 and val <= 100.0:
                val = val / 100.0
            if 0.0 <= val <= 1.0:
                print(f"[_compute_fitness_iou] Result {idx}: IoU from regex = {val}", file=sys.stderr)
                ious.append(val)
        else:
            print(f"[_compute_fitness_iou] Result {idx}: No IoU found", file=sys.stderr)

    if ious:
        from statistics import mean as _mean
        final = float(_mean(ious))
        print(f"[_compute_fitness_iou] Final fitness = {final} from {len(ious)} value(s)", file=sys.stderr)
        return final
    return 0.0


def _compute_fitness_tool_numeric(results: List[Dict[str, Any]]) -> float:
    """Extract a generic numeric fitness from the last TOOL step result JSON.
    
    Looks for common metric fields: score, fitness, accuracy, f1, precision, recall, result.
    """
    if not results:
        return 0.0
    metric_keys = {"score", "fitness", "accuracy", "f1", "precision", "recall", "result", "metric", "value"}
    values: list[float] = []
    for r in results:
        text = str(r.get("full_output") or r.get("prediction") or "")
        nums = _try_extract_numeric_from_json(text, metric_keys)
        if nums:
            values.append(nums[0])
    if values:
        return float(mean(values))
    return 0.0


def _compute_fitness_accuracy(results: List[Dict[str, Any]]) -> float:
    """Accuracy-based fitness (text comparison)."""
    if not results:
        return 0.0
    y_true, y_pred = _prepare_labels(results)
    if VALIDATION_TYPE == "Binary (0/1)":
        return _validate_binary(y_true, y_pred, METRIC)
    else:
        return _validate_continuous(y_true, y_pred, METRIC)


def _compute_fitness_auto(results: List[Dict[str, Any]]) -> float:
    """Auto-detect fitness mode: try IoU first, then tool numeric, then accuracy."""
    if not results:
        return 0.0

    # Try IoU extraction first (covers segmentation tasks)
    iou_fitness = _compute_fitness_iou(results)
    if iou_fitness > 0.0:
        return iou_fitness

    # Try generic TOOL numeric extraction
    tool_fitness = _compute_fitness_tool_numeric(results)
    if tool_fitness > 0.0:
        return tool_fitness

    # Fallback to text accuracy
    return _compute_fitness_accuracy(results)


def _compute_fitness(results: List[Dict[str, Any]]) -> float:
    """Dispatch to the appropriate fitness computation based on FITNESS_MODE."""
    mode = FITNESS_MODE.strip().lower() if FITNESS_MODE else "auto"
    if mode == "iou":
        return _compute_fitness_iou(results)
    elif mode == "accuracy":
        return _compute_fitness_accuracy(results)
    elif mode in ("continuous", "rouge", "bertscore", "bleu"):
        if not results:
            return 0.0
        y_true, y_pred = _prepare_labels(results)
        return _validate_continuous(y_true, y_pred, METRIC)
    elif mode == "tool_numeric":
        return _compute_fitness_tool_numeric(results)
    else:
        # "auto" or unknown → smart fallback
        return _compute_fitness_auto(results)


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

    # --- CHAIN FEEDBACK GENERATION ---
    # Feedback is passed as the artifact in a (metrics_dict, artifact) tuple so that
    # the Gigaevo-core DAG pipeline can route it through:
    #   FetchArtifact → FormatterStage → MutationContextStage (PreformattedMutationContext)
    # This gives the mutation LLM structured performance feedback for the next evolution step.

    all_results = train_results + val_results
    times = [r.get("time", 0.0) for r in all_results if r.get("time") is not None]
    avg_time = float(mean(times)) if times else 0.0

    success_flags = [1.0 if r.get("success") else 0.0 for r in all_results]
    success_rate = float(mean(success_flags)) if success_flags else 0.0

    is_valid = 1.0 if (all_results and fitness >= 0.0) else 0.0

    # 1) Always build the BASIC feedback (guaranteed, no external deps)
    print(f"[Chain Feedback] Building feedback: {len(train_results)} train results, fitness={fitness:.4f}", file=sys.stderr)
    basic_feedback = _build_basic_feedback(
        results=train_results,
        fitness=fitness,
        val_fitness=val_fitness,
        target_column=target_column,
    )
    print(f"[Chain Feedback] Basic feedback generated: {len(basic_feedback)} chars", file=sys.stderr)

    # 2) Try to build richer feedback via feedback_integration (DETAILED / ERRORS_ONLY)
    rich_feedback = None
    if _FEEDBACK_AVAILABLE:
        try:
            rich_feedback = generate_chain_feedback(
                results=train_results,
                target_column=target_column,
                experiment_dir=str(_current_dir)
            )
            if rich_feedback and not rich_feedback.strip():
                print(f"[Chain Feedback] Rich feedback was empty, using basic", file=sys.stderr)
                rich_feedback = None
            elif rich_feedback:
                print(f"[Chain Feedback] Rich feedback generated: {len(rich_feedback)} chars", file=sys.stderr)
        except Exception as feedback_err:
            print(f"[Chain Feedback] Error generating rich feedback: {feedback_err}", file=sys.stderr)
            import traceback
            traceback.print_exc(file=sys.stderr)
            rich_feedback = None

    # 3) Select the best available feedback (rich > basic)
    feedback = rich_feedback if rich_feedback else basic_feedback
    if not feedback or not feedback.strip():
        print(f"[Chain Feedback] ERROR: Feedback is empty! train_results={len(train_results)}, basic_len={len(basic_feedback) if basic_feedback else 0}", file=sys.stderr)
        # Force a minimal feedback to ensure something is saved
        feedback = f"## Chain Execution Feedback\n\nNo feedback generated. Train results: {len(train_results)}, Fitness: {fitness:.4f}"
    print(f"[Chain Feedback] Final feedback: {len(feedback)} chars "
          f"(source: {'rich' if rich_feedback else 'basic'})", file=sys.stderr)

    result = {
        "fitness": fitness,
        "val_fitness": val_fitness,
        "avg_extraction_failures": 0.0,
        "avg_cost_utilization": 0.0,
        "avg_prompt_cost_utilization": 0.0,
        "avg_response_cost_utilization": 0.0,
        "is_valid": is_valid,
    }

    # Add feedback metadata to metrics dict for tracking (only numeric flag)
    if _FEEDBACK_AVAILABLE:
        result = add_feedback_to_validation_result(result, feedback)
    else:
        result["has_feedback"] = 1.0 if (feedback and feedback.strip()) else 0.0

    # --- Save feedback to file for S3 upload by runner_api results collection loop ---
    # Try multiple locations: PROBLEM_DIR (if set), current working directory, or _current_dir
    # This ensures feedback is saved where gigaevo-core expects it (possibly in iteration_N subdirs)
    save_dirs = []
    if os.getenv("PROBLEM_DIR"):
        save_dirs.append(Path(os.getenv("PROBLEM_DIR")))
    # Current working directory (may be iteration_N subdirectory)
    save_dirs.append(Path.cwd())
    # Fallback to _current_dir (experiment directory)
    save_dirs.append(_current_dir)
    
    saved_any = False
    for save_dir in save_dirs:
        try:
            if not save_dir.exists():
                continue
            feedback_path = save_dir / "chain_feedback.md"
            with open(feedback_path, 'w', encoding='utf-8') as f:
                f.write(feedback)
            print(f"[Chain Feedback] Saved feedback to {feedback_path} ({len(feedback)} chars)", file=sys.stderr)
            saved_any = True
            
            # Also save JSON metadata
            try:
                import json as _json
                meta = {
                    "feedback_template": "basic" if not rich_feedback else "detailed",
                    "results_count": len(train_results),
                    "target_column": target_column,
                    "fitness": fitness,
                    "val_fitness": val_fitness,
                }
                meta_path = save_dir / "chain_feedback.json"
                with open(meta_path, 'w', encoding='utf-8') as f:
                    _json.dump(meta, f, indent=2)
                print(f"[Chain Feedback] Saved metadata to {meta_path}", file=sys.stderr)
            except Exception as meta_err:
                print(f"[Chain Feedback] Failed to save metadata: {meta_err}", file=sys.stderr)
            
            # Save in first available location only (avoid duplicates)
            break
        except Exception as save_err:
            print(f"[Chain Feedback] Failed to save feedback to {save_dir}: {save_err}", file=sys.stderr)
            continue
    
    if not saved_any:
        print(f"[Chain Feedback] WARNING: Failed to save feedback to any location. Tried: {[str(d) for d in save_dirs]}", file=sys.stderr)

    # ALWAYS return (metrics_dict, feedback_string) tuple.
    # Gigaevo-core FetchArtifact extracts data[1] → FormatterStage → MutationContextStage
    return (result, feedback)
