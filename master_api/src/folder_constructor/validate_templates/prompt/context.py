"""Context builder for ${task_name} (self-contained)."""

from typing import TypedDict
import os
import pandas as pd
from pathlib import Path


class ContextDict(TypedDict, total=False):
    model_name: str
    train_dataset: pd.DataFrame
    val_dataset: pd.DataFrame
    max_cost: float


def _get_model_name() -> str:
    # 1) Environment variable (check both PROMPT_MODEL_NAME and PROMPT_MODEL for compatibility)
    env_model = os.getenv("PROMPT_MODEL_NAME") or os.getenv("PROMPT_MODEL")
    if env_model:
        return env_model

    # In this repo, PROMPT_* vars are provided by Runner API based on repo-level llm_models.yml.
    raise FileNotFoundError(
        "Model name not configured. Runner should provide PROMPT_MODEL_NAME (or PROMPT_MODEL) "
        "based on repo-level llm_models.yml."
    )


def _load_dataset(filename: str = "train.csv") -> pd.DataFrame:
    # 1) Repo-root relative path (standard runner cwd)
    repo_rel = Path("problems") / "${task_name}" / "dataset" / filename
    if repo_rel.exists():
        return pd.read_csv(repo_rel).reset_index(drop=True).head(8) # TODO: extend
    # 2) Local directory fallback (if executed from problem dir)
    local_rel = Path("dataset") / filename
    if local_rel.exists():
        return pd.read_csv(local_rel).reset_index(drop=True).head(8) # TODO: extend
    raise FileExistsError(f"Dataset names '{filename}' not found, check paths: {repo_rel} or {local_rel}")


def build_context() -> ContextDict:
    return {
        "model_name": _get_model_name(),
        "train_dataset": _load_dataset("data.csv"),
        "val_dataset": None,
        "max_cost": 1.0,
    }
