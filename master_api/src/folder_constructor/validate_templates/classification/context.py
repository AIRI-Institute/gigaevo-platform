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
    test_size: float = 0.2,
    random_state: int = 42,
    features: Optional[list[str]] = None,
    drop_na: bool = True,
) -> dict[str, np.ndarray]:
    """
    Build a standard classification context for feature selection tasks.
    """
    df = pd.read_csv(DATASET_PATH)
    if drop_na:
        df = df.dropna()

    if features is not None:
        X_df = df[features]
    else:
        X_df = df.drop(columns=[TARGET_COLUMN])

    non_numeric = [c for c in X_df.columns if not pd.api.types.is_numeric_dtype(X_df[c])]
    if non_numeric:
        X_df = pd.get_dummies(X_df, columns=non_numeric, drop_first=True)

    y_series = df[TARGET_COLUMN]
    if y_series.dtype.kind not in {"i", "u", "f"}:
        y_series = y_series.astype("category").cat.codes

    X_train, X_test, y_train, y_test = train_test_split(
        X_df.values.astype(np.float32),
        y_series.values.astype(np.int64),
        test_size=test_size,
        random_state=random_state,
        stratify=y_series,
    )

    return {
        "X_train": X_train,
        "X_test": X_test,
        "y_train": y_train,
        "y_test": y_test,
    }
