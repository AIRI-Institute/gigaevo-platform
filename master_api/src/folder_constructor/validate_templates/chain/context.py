from typing import TypedDict, Optional, Any
import os
import pandas as pd
from pathlib import Path
from sklearn.model_selection import train_test_split
import json


class ContextDict(TypedDict, total=False):
    model_name: str
    train_dataset: pd.DataFrame
    val_dataset: pd.DataFrame
    max_cost: float
    target_column: str
    retrieval_corpus_path: str
    retrieval_index_path: str


def _get_model_name() -> str:
    env_model = os.getenv("LLM__MODEL") or os.getenv("PROMPT_MODEL_NAME") or os.getenv("PROMPT_MODEL")
    if env_model:
        return env_model
    return "unknown"


def _load_dataset(filename: str = "data.csv", dataset_size: Optional[int] = None) -> Optional[pd.DataFrame]:
    repo_rel = Path("problems") / "${task_name}" / "dataset" / filename
    if repo_rel.exists():
        df = pd.read_csv(repo_rel).reset_index(drop=True)
        if dataset_size is not None and dataset_size > 0 and len(df) > dataset_size:
            df = df.head(dataset_size)
        return df
    local_rel = Path("dataset") / filename
    if local_rel.exists():
        df = pd.read_csv(local_rel).reset_index(drop=True)
        if dataset_size is not None and dataset_size > 0 and len(df) > dataset_size:
            df = df.head(dataset_size)
        return df
    return None


def _chain_uses_retrieve_tool() -> bool:
    """Detect whether the experiment chain uses tool_name='retrieve'."""
    try:
        p = Path("base_chain_config.json")
        if not p.exists():
            return False
        cfg = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return False

    steps = cfg.get("steps", []) if isinstance(cfg, dict) else []
    for s in steps:
        if not isinstance(s, dict):
            continue
        stype = str(s.get("step_type", "LLM")).strip().upper()
        if stype != "TOOL":
            continue
        scfg = s.get("step_config") or {}
        if not isinstance(scfg, dict):
            continue
        if str(scfg.get("tool_name", "")).strip().lower() == "retrieve":
            return True
    return False


def _ensure_retrieval_assets(
    train_df: pd.DataFrame,
    target_column: str,
    *,
    max_docs: int = 20000,
) -> tuple[str, str]:
    """Ensure local retrieval corpus + BM25 index exist in ./retrieval.

    This runs inside the runner venv (gigaevo-core), so sklearn/scipy/numpy/joblib
    are available. The corpus is generated at experiment creation time when possible,
    but we can also build it here as a fallback.
    """
    retrieval_dir = Path("retrieval")
    retrieval_dir.mkdir(parents=True, exist_ok=True)

    corpus_path = retrieval_dir / "corpus.jsonl"
    index_path = retrieval_dir / "bm25_index.joblib"

    # 1) If corpus missing, build a simple one from the dataset
    if not corpus_path.exists():
        cols = [c for c in train_df.columns if isinstance(c, str)]
        lower = {c.lower(): c for c in cols}
        preferred: list[str] = []
        for name in ["context", "passage", "passages", "document", "documents", "text", "paragraph", "paragraphs"]:
            if name in lower:
                preferred.append(lower[name])

        def row_to_text(row: pd.Series, use_cols: list[str]) -> str:
            parts: list[str] = []
            for c in use_cols:
                v = row.get(c, "")
                if v is None:
                    continue
                s = str(v).strip()
                if s:
                    parts.append(s if len(use_cols) == 1 else f"{c}: {s}")
            return "\n".join(parts).strip()

        df = train_df
        if max_docs and len(df) > max_docs:
            df = df.head(max_docs)

        if preferred:
            docs = [row_to_text(r, preferred) for _, r in df.iterrows()]
            docs = [d for d in docs if d]
        else:
            non_target = [c for c in cols if c != target_column]
            docs = [row_to_text(r, non_target) for _, r in df.iterrows()]
            docs = [d for d in docs if d]

        with corpus_path.open("w", encoding="utf-8") as f:
            for text in docs:
                f.write(json.dumps({"text": text}, ensure_ascii=False) + "\n")

    # 2) If index exists, return paths
    if index_path.exists():
        return str(corpus_path), str(index_path)

    # 3) Build BM25 index from corpus (core-style: local index persisted to disk)
    try:
        import joblib
        import numpy as np
        from sklearn.feature_extraction.text import CountVectorizer
        from scipy import sparse
    except Exception:
        # If deps missing, just return corpus path (helper will fallback to TF-IDF/substring)
        return str(corpus_path), str(index_path)

    docs: list[str] = []
    try:
        with corpus_path.open("r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                obj = json.loads(line)
                text = str(obj.get("text", "")).strip()
                if text:
                    docs.append(text)
    except Exception:
        docs = []

    if not docs:
        return str(corpus_path), str(index_path)

    # Reasonable cap for index build time
    if max_docs and len(docs) > max_docs:
        docs = docs[:max_docs]

    vectorizer = CountVectorizer(stop_words="english", lowercase=True, max_features=50000)
    tf = vectorizer.fit_transform(docs)
    tf = tf.tocsc()  # efficient column access for BM25 scoring

    N = tf.shape[0]
    # doc lengths
    doc_len = np.asarray(tf.sum(axis=1)).ravel().astype(np.float32)
    avgdl = float(doc_len.mean()) if N > 0 else 0.0

    # document frequency per term
    df = np.asarray((tf > 0).sum(axis=0)).ravel().astype(np.float32)
    idf = np.log((N - df + 0.5) / (df + 0.5) + 1.0).astype(np.float32)

    payload: dict[str, Any] = {
        "vectorizer": vectorizer,
        "tf_csc": tf,
        "doc_len": doc_len,
        "avgdl": avgdl,
        "idf": idf,
        "docs": docs,
        # core-like defaults
        "k1": 0.9,
        "b": 0.4,
    }
    joblib.dump(payload, index_path)

    return str(corpus_path), str(index_path)


def build_context(
    dataset_size: Optional[int] = None,
    test_size: Optional[float] = None,
    random_state: int = 42,
) -> ContextDict:
    if dataset_size is None:
        env_size = os.getenv("DATASET_SIZE")
        if env_size:
            dataset_size = int(env_size)
    if test_size is None:
        test_size = float(os.getenv("TEST_SIZE", "0.2"))
    
    df = _load_dataset("data.csv", dataset_size)
    
    if df is None or len(df) == 0:
        raise FileNotFoundError("Dataset 'data.csv' not found in expected locations")
    
    if test_size > 0 and test_size < 1.0 and len(df) > 1:
        train_df, val_df = train_test_split(
            df,
            test_size=test_size,
            random_state=random_state,
            shuffle=True,
        )
        val_dataset = val_df.reset_index(drop=True)
    else:
        train_df = df
        val_dataset = None
    
    train_dataset = train_df.reset_index(drop=True)

    retrieval_corpus_path = ""
    retrieval_index_path = ""
    if _chain_uses_retrieve_tool():
        retrieval_corpus_path, retrieval_index_path = _ensure_retrieval_assets(
            train_dataset, "${target_field}"
        )
    
    return {
        "model_name": _get_model_name(),
        "train_dataset": train_dataset,
        "val_dataset": val_dataset,
        "max_cost": 1.0,
        "target_column": "${target_field}",
        "retrieval_corpus_path": retrieval_corpus_path,
        "retrieval_index_path": retrieval_index_path,
    }
