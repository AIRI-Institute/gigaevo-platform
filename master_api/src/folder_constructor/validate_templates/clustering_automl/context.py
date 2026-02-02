from pathlib import Path
import os
from typing import Optional
import sys

import numpy as np
import pandas as pd
from sklearn.model_selection import train_test_split

DATASET_BASE = Path(os.getenv("PROBLEM_DIR") or (sys.path[0] if sys.path else ".")).resolve()
DATASET_PATH = DATASET_BASE / "dataset" / "data.csv"
TARGET_COLUMN = "${target_field}"


def build_context(
    test_size: Optional[float] = None,
    random_state: int = 42,
    features: Optional[list[str]] = None,
    drop_na: bool = True,
    dataset_size: Optional[int] = None,
) -> dict[str, np.ndarray]:
    """
    Build context for clustering tasks.
    Currently identical to the classic clustering template; kept separate
    so that AutoML-specific variants can evolve independently later.
    """
    # Read parameters from environment variables if not provided
    if test_size is None:
        test_size = float(os.getenv("TEST_SIZE", "0.2"))
    if dataset_size is None:
        env_size = os.getenv("DATASET_SIZE")
        if env_size:
            dataset_size = int(env_size)
    
    df = pd.read_csv(DATASET_PATH)
    if drop_na:
        df = df.dropna()
    
    # Limit dataset size if specified
    if dataset_size is not None and dataset_size > 0 and len(df) > dataset_size:
        df = df.head(dataset_size)

    if features is not None:
        X_df = df[features]
    else:
        if TARGET_COLUMN and TARGET_COLUMN in df.columns:
            X_df = df.drop(columns=[TARGET_COLUMN])
        else:
            X_df = df

    non_numeric = [c for c in X_df.columns if not pd.api.types.is_numeric_dtype(X_df[c])]
    if non_numeric:
        X_df = pd.get_dummies(X_df, columns=non_numeric, drop_first=True)
    X_df = X_df.select_dtypes(include=[np.number])

    X_train, X_test = train_test_split(
        X_df.values.astype(np.float32),
        test_size=test_size,
        random_state=random_state,
    )

    context: dict[str, np.ndarray] = {
        "X_train": X_train,
        "X_test": X_test,
    }
    if TARGET_COLUMN and TARGET_COLUMN in df.columns:
        context["y_test_full"] = df[TARGET_COLUMN].values
    return context


