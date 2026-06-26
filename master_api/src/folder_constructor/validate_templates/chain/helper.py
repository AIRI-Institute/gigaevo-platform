"""Chain execution helper — step-batched async engine.

Uses chain_types / chain_client / chain_runner ported from gigaevo-core-internal
for step-batched execution: all samples process each chain step together,
yielding homogeneous LLM request batches for efficient vLLM batching.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import re
import sys
import time
import traceback as _tb
from typing import Any, Callable, Dict, List

import pandas as pd

from chain_types import (
    ChainSpec,
    LLMStep,
    PromptBuilder,
    RawChainSpec,
    ToolStep,
)
from chain_client import LLMClient, create_client_from_env
from chain_runner import run_chain_on_dataset_stepwise


# ---------------------------------------------------------------------------
# Context / prompt helpers
# ---------------------------------------------------------------------------


def _format_row_as_context(row: pd.Series, target_column: str) -> str:
    context_parts = []
    for col in row.index:
        if col != target_column:
            context_parts.append(f"{col}:\n{row[col]}")
    context_text = "\n\n".join(context_parts)
    return f"{context_text}\n\nSolve the problem and compute the correct answer."


def _format_dict_as_context(row_dict: dict, target_column: str) -> str:
    """Build outer_context string from a sample dict (step-batched path)."""
    context_parts = []
    for col, val in row_dict.items():
        if col != target_column:
            context_parts.append(f"{col}:\n{val}")
    context_text = "\n\n".join(context_parts)
    return f"{context_text}\n\nSolve the problem and compute the correct answer."


def _get_system_prompt() -> str:
    return """You are an expert problem solver with extensive experience in reasoning tasks.

Your approach should:
- Read the problem carefully and identify what is being asked
- Break down complex problems into smaller, manageable steps
- Show your reasoning clearly at each step
- Perform operations accurately
- Verify your answer makes sense in the context of the problem
- Provide the final answer clearly

Focus on accuracy and step-by-step reasoning."""


# ---------------------------------------------------------------------------
# Prediction extraction
# ---------------------------------------------------------------------------


def _extract_numeric_answer(text: str) -> str:
    if not text:
        return ""
    matches = re.findall(r"[-+]?\d+\.?\d*", text)
    if matches:
        return matches[-1]
    matches = re.findall(r"\d+", text)
    return matches[-1] if matches else ""


def _extract_prediction(text: str) -> str:
    """Extract prediction from chain output (text or numeric).

    Priority:
        1) ``Answer: <answer>`` pattern (handles text QA like HotpotQA)
        2) If text is short (1-3 words), return it as-is
        3) Last non-empty line (handles multi-line responses)
        4) Last numeric value (handles math / numeric tasks)
        5) First 50 chars as fallback
    """
    if not text:
        return ""

    text = text.strip()

    m = re.search(r"Answer:\s*(.+?)(?:\n|$)", text, re.IGNORECASE)
    if m:
        answer = m.group(1).strip()
        if answer:
            return answer

    m = re.search(r"(?:the\s+)?answer\s+is\s+['\"]?([^.'\"\n]+)['\"]?", text, re.IGNORECASE)
    if m:
        answer = m.group(1).strip()
        if answer and len(answer.split()) <= 5:
            return answer

    words = text.split()
    if len(words) <= 3:
        first_word = words[0].lower() if words else ""
        if first_word in ["positive", "negative", "neutral", "yes", "no"] or len(words) == 1:
            return text.strip()

    lines = [line.strip() for line in text.split("\n") if line.strip()]
    if lines:
        last_line = lines[-1]
        if len(last_line.split()) <= 5:
            return last_line
        m = re.search(r"(?:the\s+)?answer\s+is\s+['\"]?([^.'\"\n]+)['\"]?", last_line, re.IGNORECASE)
        if m:
            answer = m.group(1).strip()
            if answer and len(answer.split()) <= 5:
                return answer

    numeric = _extract_numeric_answer(text)
    if numeric:
        return numeric

    return text[:50].strip() if text else ""


# ---------------------------------------------------------------------------
# Chain spec construction
# ---------------------------------------------------------------------------


_SUPPORTED_STEP_TYPES = {"llm", "tool"}


def _ensure_llm_defaults(step: dict) -> None:
    """Fill minimal LLM fields so Pydantic LLMStep validation passes."""
    if not (step.get("aim") or "").strip():
        step["aim"] = "Solve the given task"
    if not (step.get("reasoning_questions") or "").strip():
        step["reasoning_questions"] = "What is the key information?"
    if not (step.get("stage_action") or "").strip():
        step["stage_action"] = "Reason step-by-step and provide the answer."


def _build_chain_spec(chain_config: Dict[str, Any]) -> ChainSpec:
    """Parse chain_config dict into an executable ChainSpec.

    Normalises step_type to lowercase, fills missing LLM fields with defaults,
    filters out unsupported step types (transform, memory, conditional, etc.),
    and cleans up dangling dependency references.
    """
    data = copy.deepcopy(chain_config)
    raw_steps = data.get("steps") or []
    filtered_steps: list[dict] = []
    kept_numbers: set[int] = set()
    dropped_numbers: set[int] = set()

    for step in raw_steps:
        if not isinstance(step, dict):
            continue
        st = str(step.get("step_type", "llm")).strip().lower()
        step["step_type"] = st if st else "llm"
        step_num = step.get("number", 0)

        if st not in _SUPPORTED_STEP_TYPES:
            print(
                f"[_build_chain_spec] Skipping unsupported step type '{st}' "
                f"(step {step_num})",
                file=sys.stderr,
            )
            dropped_numbers.add(step_num)
            continue

        if st == "llm":
            _ensure_llm_defaults(step)

        filtered_steps.append(step)
        kept_numbers.add(step_num)

    if dropped_numbers:
        for step in filtered_steps:
            deps = step.get("dependencies") or []
            step["dependencies"] = [d for d in deps if d not in dropped_numbers]

    data["steps"] = filtered_steps

    parsed = RawChainSpec.model_validate(data)
    steps = sorted(parsed.steps, key=lambda s: s.number)

    system_prompt = parsed.system_prompt or _get_system_prompt()

    return ChainSpec(
        system_prompt=system_prompt,
        steps=steps,
        prompt_builder=PromptBuilder(),
    )


# ---------------------------------------------------------------------------
# Tool registry building (replaces mmar_carl ReasoningContext.register_tool)
# ---------------------------------------------------------------------------


def _wrap_tool_to_str(fn: Callable) -> Callable:
    """Wrap a tool function to guarantee str return (chain_runner contract)."""
    def wrapped(**kwargs: Any) -> str:
        result = fn(**kwargs)
        if isinstance(result, str):
            return result
        if isinstance(result, dict):
            passages = result.get("passages")
            if passages:
                return str(passages)
            return json.dumps(result, ensure_ascii=False)
        return str(result) if result is not None else ""
    return wrapped


def _get_env_tools() -> Dict[str, Callable]:
    """Build tool dict from CARL_TOOL__<NAME>_URL environment variables.

    Each tool forwards all keyword arguments as JSON to the configured URL.
    """
    prefix = "CARL_TOOL__"
    suffix = "_URL"
    registry: Dict[str, Callable] = {}

    tool_urls: Dict[str, str] = {}
    for key, val in os.environ.items():
        if key.startswith(prefix) and key.endswith(suffix):
            name_part = key[len(prefix):-len(suffix)]
            tool_name = name_part.lower()
            tool_urls[tool_name] = val.strip()

    for tool_name, url in tool_urls.items():
        if not url:
            continue
        timeout_key = f"{prefix}{tool_name.upper()}_TIMEOUT"
        timeout_s = float(os.getenv(timeout_key, "120"))

        def _make_tool(t_url: str, t_timeout: float, t_name: str) -> Callable:
            def _tool_fn(**kwargs: Any) -> str:
                try:
                    import requests  # type: ignore
                    print(
                        f"[{t_name}] POST {t_url} timeout={t_timeout}s "
                        f"payload_keys={list(kwargs.keys())}",
                        file=sys.stderr,
                    )
                    resp = requests.post(t_url, json=kwargs, timeout=t_timeout)
                    resp.raise_for_status()
                    data = resp.json()
                    if isinstance(data, str):
                        return data
                    if isinstance(data, dict):
                        passages = data.get("passages")
                        if passages:
                            return str(passages)
                        return json.dumps(data, ensure_ascii=False)
                    return json.dumps(data, ensure_ascii=False)
                except Exception as e:
                    print(f"[{t_name}] EXCEPTION: {e}", file=sys.stderr)
                    return json.dumps({"error": str(e)})
            return _tool_fn

        registry[tool_name] = _make_tool(url, timeout_s, tool_name)
        print(f"[env_tools] Registered tool '{tool_name}' -> {url}", file=sys.stderr)

    return registry


def _load_custom_tools(base_dir: str) -> Dict[str, Callable]:
    """Load callable functions from custom_tools.py as tools (return str)."""
    registry: Dict[str, Callable] = {}
    custom_tools_path = os.path.join(base_dir, "custom_tools.py")

    if not os.path.exists(custom_tools_path):
        return registry

    try:
        spec = importlib.util.spec_from_file_location("custom_tools", custom_tools_path)
        if spec is None or spec.loader is None:
            return registry

        module = importlib.util.module_from_spec(spec)
        sys.modules["custom_tools"] = module
        spec.loader.exec_module(module)

        for attr_name in dir(module):
            if attr_name.startswith("_"):
                continue
            attr = getattr(module, attr_name)
            if callable(attr):
                registry[attr_name] = _wrap_tool_to_str(attr)
                print(
                    f"[custom_tools] Registered '{attr_name}' from {custom_tools_path}",
                    file=sys.stderr,
                )

    except Exception as e:
        print(f"[custom_tools] Failed to load custom_tools.py: {e}", file=sys.stderr)
        _tb.print_exc(file=sys.stderr)

    return registry


# ---------------------------------------------------------------------------
# Local retrieval tool (core-style BM25 / TF-IDF fallback)
# ---------------------------------------------------------------------------


_LOCAL_RETRIEVE_CACHE: dict[str, Any] = {
    "key": None,
    "vectorizer": None,
    "matrix": None,
    "docs": None,
}


def _build_retrieval_corpus(df: pd.DataFrame, target_column: str) -> list[str]:
    """Build text corpus from dataframe for local retrieval fallback."""
    if df is None or len(df) == 0:
        return []

    cols = [c for c in df.columns if isinstance(c, str)]
    lower = {c.lower(): c for c in cols}

    preferred = []
    for name in ["context", "passage", "passages", "document", "documents", "text", "paragraph", "paragraphs"]:
        if name in lower:
            preferred.append(lower[name])

    def _row_to_text(row: pd.Series, use_cols: list[str]) -> str:
        parts: list[str] = []
        for c in use_cols:
            try:
                v = row.get(c, "")
            except Exception:
                v = ""
            if v is None:
                continue
            s = str(v).strip()
            if s:
                parts.append(f"{c}: {s}")
        return "\n".join(parts).strip()

    if preferred:
        corpus = [_row_to_text(r, preferred) for _, r in df.iterrows()]
        corpus = [t for t in corpus if t]
        if corpus:
            return corpus

    non_target = [c for c in cols if c != target_column]
    corpus = [_row_to_text(r, non_target) for _, r in df.iterrows()]
    corpus = [t for t in corpus if t]
    if corpus:
        return corpus

    corpus = [_row_to_text(r, cols) for _, r in df.iterrows()]
    return [t for t in corpus if t]


def _make_local_retrieve_tool(
    df: pd.DataFrame, target_column: str, *, k_default: int = 7
) -> Callable:
    """Create local retrieval tool that returns str.

    Checks (in order): on-disk BM25 index, in-memory TF-IDF, substring search.
    """
    import hashlib
    from pathlib import Path

    corpus = _build_retrieval_corpus(df, target_column)
    if not corpus:
        def _noop(**kwargs: Any) -> str:
            return ""
        return _noop

    index_path = Path("retrieval") / "bm25_index.joblib"
    if index_path.exists():
        try:
            import joblib
            import numpy as np
        except Exception:
            pass
        else:
            def _bm25_tool(**kwargs: Any) -> str:
                query = str(kwargs.get("query", ""))
                if not query:
                    return ""
                try:
                    payload = joblib.load(index_path)
                    vectorizer = payload.get("vectorizer")
                    tf = payload.get("tf_csc")
                    doc_len = payload.get("doc_len")
                    avgdl = float(payload.get("avgdl") or 0.0)
                    idf = payload.get("idf")
                    docs = payload.get("docs") or []
                    k1 = float(payload.get("k1") or 0.9)
                    b = float(payload.get("b") or 0.4)

                    if vectorizer is None or tf is None or idf is None or doc_len is None or not docs:
                        return ""

                    qv = vectorizer.transform([query])
                    term_idx = qv.indices
                    if term_idx is None or len(term_idx) == 0:
                        return ""

                    N = int(tf.shape[0])
                    scores = np.zeros(N, dtype=np.float32)
                    for t in term_idx:
                        col = tf.getcol(int(t))
                        if col.nnz == 0:
                            continue
                        for doc_i, freq in zip(col.indices, col.data):
                            dl = float(doc_len[int(doc_i)]) if avgdl > 0 else 0.0
                            denom = float(freq) + k1 * (1.0 - b + b * (dl / avgdl if avgdl > 0 else 0.0))
                            if denom <= 0:
                                continue
                            scores[int(doc_i)] += float(idf[int(t)]) * (float(freq) * (k1 + 1.0) / denom)

                    k = int(kwargs.get("k", k_default)) if str(kwargs.get("k", "")).isdigit() else k_default
                    k = max(1, k)
                    top = scores.argsort()[::-1][:k]
                    out_docs = [docs[int(i)] for i in top if scores[int(i)] > 0.0]
                    return "\n".join(f"[{j+1}] {t}" for j, t in enumerate(out_docs))
                except Exception as e:
                    print(f"[local_retrieve] BM25 exception: {e}", file=sys.stderr)
                    return ""

            return _bm25_tool

    sample = "\n".join(corpus[:20])
    cache_key = f"{len(corpus)}:{hashlib.sha1(sample.encode('utf-8', errors='ignore')).hexdigest()}"

    def _ensure_index() -> None:
        if _LOCAL_RETRIEVE_CACHE.get("key") == cache_key and _LOCAL_RETRIEVE_CACHE.get("vectorizer") is not None:
            return
        try:
            from sklearn.feature_extraction.text import TfidfVectorizer
        except Exception as e:
            print(f"[local_retrieve] sklearn missing: {e}", file=sys.stderr)
            _LOCAL_RETRIEVE_CACHE["key"] = cache_key
            _LOCAL_RETRIEVE_CACHE["vectorizer"] = None
            _LOCAL_RETRIEVE_CACHE["matrix"] = None
            _LOCAL_RETRIEVE_CACHE["docs"] = corpus
            return

        vectorizer = TfidfVectorizer(stop_words="english", max_features=50000)
        matrix = vectorizer.fit_transform(corpus)
        _LOCAL_RETRIEVE_CACHE["key"] = cache_key
        _LOCAL_RETRIEVE_CACHE["vectorizer"] = vectorizer
        _LOCAL_RETRIEVE_CACHE["matrix"] = matrix
        _LOCAL_RETRIEVE_CACHE["docs"] = corpus

    def _tfidf_tool(**kwargs: Any) -> str:
        query = str(kwargs.get("query", ""))
        _ensure_index()
        vectorizer = _LOCAL_RETRIEVE_CACHE.get("vectorizer")
        matrix = _LOCAL_RETRIEVE_CACHE.get("matrix")
        docs = _LOCAL_RETRIEVE_CACHE.get("docs") or corpus

        if not query:
            return ""

        if vectorizer is None or matrix is None:
            q = query.lower()
            hits = [d for d in docs if q in d.lower()][:k_default]
            return "\n".join(f"[{i+1}] {t}" for i, t in enumerate(hits))

        qv = vectorizer.transform([query])
        scores = (matrix @ qv.T).toarray().ravel()
        if scores.size == 0:
            return ""

        k = int(kwargs.get("k", k_default)) if str(kwargs.get("k", "")).isdigit() else k_default
        top_idx = scores.argsort()[::-1][:max(1, k)]
        top_docs = [docs[int(i)] for i in top_idx if scores[int(i)] > 0.0]
        return "\n".join(f"[{i+1}] {t}" for i, t in enumerate(top_docs))

    return _tfidf_tool


# ---------------------------------------------------------------------------
# Dataset preparation
# ---------------------------------------------------------------------------


def _resolve_base_dir() -> str:
    """Find experiment base directory for resolving relative paths."""
    cwd = os.getcwd()
    for check in [
        os.path.join(cwd, "dataset", "data.csv"),
        "dataset/data.csv",
        "dataset_files",
    ]:
        if os.path.exists(check):
            return cwd

    for check in [
        os.path.join("..", "dataset", "data.csv"),
        os.path.join("..", "dataset_files"),
    ]:
        if os.path.exists(check):
            return os.path.abspath("..")

    return cwd


def _prepare_dataset(
    df: pd.DataFrame, target_column: str, base_dir: str
) -> list[dict]:
    """Convert DataFrame rows to list of dicts with resolved file paths."""
    dataset: list[dict] = []

    for _, row in df.iterrows():
        row_dict = row.to_dict()

        for path_col in ("image_path", "mask_path"):
            if path_col in row_dict and row_dict[path_col]:
                p = str(row_dict[path_col])
                if not os.path.isabs(p):
                    abs_p = os.path.normpath(os.path.join(base_dir, p))
                    if os.path.exists(abs_p):
                        row_dict[path_col] = abs_p

        dataset.append(row_dict)

    return dataset


# ---------------------------------------------------------------------------
# Main execution (step-batched async)
# ---------------------------------------------------------------------------


def _run_split(
    chain_config: Dict[str, Any], df: pd.DataFrame, target_column: str
) -> List[Dict[str, Any]]:
    """Execute chain on dataset using step-batched async engine.

    All samples process each chain step together, yielding homogeneous LLM
    request batches.  Tool calls are parallelised via asyncio.to_thread.
    """
    if df is None or len(df) == 0:
        return []

    # --- Build chain spec ---
    try:
        chain_spec = _build_chain_spec(chain_config)
        step_types = [
            f"{s.number}:{s.step_type}" for s in chain_spec.steps
        ]
        print(
            f"[_run_split] ChainSpec built: {len(chain_spec.steps)} steps "
            f"({', '.join(step_types)}), step-batched async engine",
            file=sys.stderr,
        )
    except Exception as e:
        print(f"[_run_split] ERROR building ChainSpec: {e}", file=sys.stderr)
        _tb.print_exc(file=sys.stderr)
        return []

    # --- Build async LLM client ---
    try:
        client = create_client_from_env()
        print(f"[_run_split] Async LLM client created (model={client.model})", file=sys.stderr)
    except Exception as e:
        print(f"[_run_split] ERROR creating LLM client: {e}", file=sys.stderr)
        _tb.print_exc(file=sys.stderr)
        return []

    # --- Resolve base directory and prepare dataset ---
    base_dir = _resolve_base_dir()
    print(f"[_run_split] Base directory: {base_dir}", file=sys.stderr)

    dataset = _prepare_dataset(df, target_column, base_dir)
    print(f"[_run_split] Dataset prepared: {len(dataset)} samples", file=sys.stderr)

    # --- Build tool registry ---
    tool_registry: Dict[str, Callable] = {}

    env_tools = _get_env_tools()
    tool_registry.update(env_tools)

    local_retrieve = _make_local_retrieve_tool(df, target_column)
    tool_registry.setdefault("retrieve", local_retrieve)

    custom_tools = _load_custom_tools(base_dir)
    tool_registry.update(custom_tools)

    if tool_registry:
        print(
            f"[_run_split] Tool registry: {list(tool_registry.keys())}",
            file=sys.stderr,
        )

    # --- Execute step-batched ---
    max_concurrent = int(os.environ.get("CHAIN_MAX_CONCURRENT", "300"))
    t0 = time.time()

    try:
        chain_results = run_chain_on_dataset_stepwise(
            chain=chain_spec,
            client=client,
            dataset=dataset,
            outer_context_builder=lambda s: _format_dict_as_context(s, target_column),
            tool_registry=tool_registry if tool_registry else None,
            batch_tool_registry=None,
            max_concurrent=max_concurrent,
        )
        total_elapsed = time.time() - t0
        print(
            f"[_run_split] Step-batched execution completed in {total_elapsed:.1f}s "
            f"for {len(dataset)} samples",
            file=sys.stderr,
        )
    except Exception as e:
        total_elapsed = time.time() - t0
        print(
            f"[_run_split] FATAL error in step-batched execution after {total_elapsed:.1f}s: {e}",
            file=sys.stderr,
        )
        _tb.print_exc(file=sys.stderr)
        results: List[Dict[str, Any]] = []
        for i, sample in enumerate(dataset):
            results.append({
                "input_text": _format_dict_as_context(sample, target_column),
                "prediction": "",
                "ground_truth": str(sample.get(target_column, "")),
                "success": False,
                "time": 0.0,
                "step_results": [],
                "chain_history": [],
                "full_output": "",
                "token_usage": {},
                "error_type": "batch_execution_error",
                "error_message": str(e),
            })
        return results

    # --- Token usage summary from client ---
    total_prompt_tokens = sum(log.prompt_tokens for log in client.call_logs)
    total_completion_tokens = sum(log.completion_tokens for log in client.call_logs)
    total_cost = sum(log.cost for log in client.call_logs)
    print(
        f"[_run_split] Total tokens: prompt={total_prompt_tokens}, "
        f"completion={total_completion_tokens}, cost=${total_cost:.4f}",
        file=sys.stderr,
    )

    # --- Convert ChainResult objects to expected dict format ---
    avg_time = total_elapsed / len(dataset) if dataset else 0.0
    results = []

    for i, cr in enumerate(chain_results):
        sample = dataset[i]
        output_text = cr.final_output
        prediction = _extract_prediction(output_text)
        ground_truth = str(sample.get(target_column, ""))

        step_results_list = []
        for j, step in enumerate(chain_spec.steps):
            if j < len(cr.step_outputs):
                step_results_list.append({
                    "step_number": step.number,
                    "step_title": step.title,
                    "result": cr.step_outputs[j],
                    "success": True,
                    "execution_time": 0.0,
                    "error_message": None,
                })

        results.append({
            "input_text": _format_dict_as_context(sample, target_column),
            "prediction": prediction,
            "ground_truth": ground_truth,
            "success": bool(cr.final_output),
            "time": avg_time,
            "step_results": step_results_list,
            "chain_history": cr.history,
            "full_output": cr.final_output,
            "token_usage": {},
            "context_image_path": str(sample.get("image_path", "")) if "image_path" in sample else "",
            "context_mask_path": str(sample.get("mask_path", "")) if "mask_path" in sample else "",
        })

    success_count = sum(1 for r in results if r.get("success"))
    print(
        f"[_run_split] Finished: {len(results)} results, {success_count} successful, "
        f"{len(results) - success_count} failed",
        file=sys.stderr,
    )
    return results


# ---------------------------------------------------------------------------
# JSON parsing
# ---------------------------------------------------------------------------


def _fix_json_common_errors(json_str: str) -> str:
    json_str = json_str.strip()
    json_str = re.sub(r'}\s*{', '},{', json_str)
    json_str = re.sub(r']\s*\[', '],[', json_str)
    json_str = re.sub(r'"\s*"', '","', json_str)
    json_str = re.sub(r'(\d)\s*"', r'\1,"', json_str)
    json_str = re.sub(r'"\s*(\d)', r'",\1', json_str)
    return json_str


def parse_chain_config_json(chain_config_json: str) -> Dict[str, Any]:
    cfg = None
    json_str = chain_config_json.strip()
    original_error = None

    try:
        cfg = json.loads(json_str)
    except json.JSONDecodeError as e:
        original_error = e
        try:
            fixed_json = _fix_json_common_errors(json_str)
            cfg = json.loads(fixed_json)
        except (json.JSONDecodeError, Exception):
            start_idx = json_str.find('{')
            end_idx = json_str.rfind('}')
            if start_idx >= 0 and end_idx > start_idx:
                try:
                    extracted = json_str[start_idx:end_idx + 1]
                    fixed_extracted = _fix_json_common_errors(extracted)
                    cfg = json.loads(fixed_extracted)
                except json.JSONDecodeError:
                    try:
                        fixed_chars = ""
                        for char in extracted:
                            code = ord(char)
                            if 0 <= code <= 0x1F:
                                if char == '\n':
                                    fixed_chars += '\\n'
                                elif char == '\r':
                                    fixed_chars += '\\r'
                                elif char == '\t':
                                    fixed_chars += '\\t'
                                elif char == '\b':
                                    fixed_chars += '\\b'
                                elif char == '\f':
                                    fixed_chars += '\\f'
                                else:
                                    fixed_chars += f'\\u{code:04x}'
                            else:
                                fixed_chars += char
                        cfg = json.loads(fixed_chars)
                    except (json.JSONDecodeError, Exception):
                        try:
                            import json5
                            cfg = json5.loads(json_str)
                        except (ImportError, Exception):
                            error_msg = f"Invalid JSON in CHAIN_CONFIG_JSON: {original_error}"
                            print(f"ERROR: {error_msg}", file=sys.stderr)
                            print(f"JSON (first 500 chars): {json_str[:500]}", file=sys.stderr)
                            raise ValueError(error_msg) from original_error
            else:
                error_msg = f"Invalid JSON in CHAIN_CONFIG_JSON: {original_error}"
                print(f"ERROR: {error_msg}", file=sys.stderr)
                print(f"JSON (first 500 chars): {json_str[:500]}", file=sys.stderr)
                raise ValueError(error_msg) from original_error

    if cfg is None:
        raise ValueError("Failed to parse CHAIN_CONFIG_JSON")

    return cfg


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def execute_chain_on_dataset(
    chain_config: Dict[str, Any], df: pd.DataFrame, target_column: str
) -> List[Dict[str, Any]]:
    return _run_split(chain_config, df, target_column)
