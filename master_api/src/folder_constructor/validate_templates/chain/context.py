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
    target_column: str


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
    
    return {
        "model_name": _get_model_name(),
        "train_dataset": train_dataset,
        "val_dataset": val_dataset,
        "max_cost": 1.0,
        "target_column": "${target_field}",
    }
