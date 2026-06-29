"""Context builder for ${task_name} (self-contained)."""

from typing import TypedDict, Optional
import os
import pandas as pd
from pathlib import Path
from sklearn.model_selection import train_test_split


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


def _load_dataset(filename: str = "train.csv", dataset_size: Optional[int] = None) -> pd.DataFrame:
    # 1) Repo-root relative path (standard runner cwd)
    repo_rel = Path("problems") / "${task_name}" / "dataset" / filename
    if repo_rel.exists():
        df = pd.read_csv(repo_rel).reset_index(drop=True)
        if dataset_size is not None and dataset_size > 0 and len(df) > dataset_size:
            df = df.head(dataset_size)
        return df
    # 2) Local directory fallback (if executed from problem dir)
    local_rel = Path("dataset") / filename
    if local_rel.exists():
        df = pd.read_csv(local_rel).reset_index(drop=True)
        if dataset_size is not None and dataset_size > 0 and len(df) > dataset_size:
            df = df.head(dataset_size)
        return df
    raise FileExistsError(f"Dataset names '{filename}' not found, check paths: {repo_rel} or {local_rel}")


def build_context(
    dataset_size: Optional[int] = None,
    test_size: Optional[float] = None,
    random_state: int = 42,
) -> ContextDict:
    """
    Build context for prompt experiments.
    
    Args:
        dataset_size: Maximum number of rows to use from dataset (None = use all)
        test_size: Fraction of dataset to use for validation (0.0-1.0)
        random_state: Random seed for train/test split
    """
    # Read parameters from environment variables if not provided
    if dataset_size is None:
        env_size = os.getenv("DATASET_SIZE")
        if env_size:
            dataset_size = int(env_size)
    if test_size is None:
        test_size = float(os.getenv("TEST_SIZE", "0.2"))
    
    # Load full dataset
    df = _load_dataset("data.csv", dataset_size)
    
    # Split into train and validation sets
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
    
    return {
        "model_name": _get_model_name(),
        "train_dataset": train_dataset,
        "val_dataset": val_dataset,
        "max_cost": 1.0,
    }
