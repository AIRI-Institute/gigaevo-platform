import io
import contextlib

import numpy as np
import pandas as pd
from sklearn.metrics import f1_score
from catboost import CatBoostClassifier


def validate(
    context: dict[str, np.ndarray],
    selected_idx: np.ndarray,
) -> dict[str, float]:
    """Train a CatBoost classifier on selected features and compute F1 (macro).

    Input:
    - context: contains X_train, y_train, X_test, y_test
    - selected_idx: (K,) int indices of columns to keep
    """

    X_train = context["X_train"]
    y_train = context["y_train"]
    X_test = context["X_test"]
    y_true = context["y_test"]

    if selected_idx.ndim != 1:
        selected_idx = selected_idx.reshape(-1)
    selected_idx = np.asarray(selected_idx, dtype=np.int64)
    selected_idx = np.unique(selected_idx)
    if selected_idx.size == 0:
        selected_idx = np.arange(X_train.shape[1], dtype=np.int64)

    X_train_sel = X_train[:, selected_idx]
    X_test_sel = X_test[:, selected_idx]

    n_features = X_train_sel.shape[1]
    feature_names = [f"f_{i}" for i in range(n_features)]

    train_df = pd.DataFrame(X_train_sel, columns=feature_names)
    train_df["target"] = y_train
    test_df = pd.DataFrame(X_test_sel, columns=feature_names)

    classes = np.unique(y_train)
    loss_function = "MultiClass" if classes.size > 2 else "Logloss"

    model = CatBoostClassifier(
        loss_function=loss_function,
        depth=6,
        learning_rate=0.1,
        iterations=200,
        random_seed=42,
        verbose=False,
    )

    buf_out = io.StringIO()
    buf_err = io.StringIO()
    with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
        model.fit(train_df[feature_names], train_df["target"])
        preds = model.predict(test_df[feature_names])

    y_pred = np.asarray(preds).reshape(-1)
    # CatBoost returns target-like values; cast to int64 to match encoded labels.
    y_pred = y_pred.astype(np.int64)

    if y_true.shape != y_pred.shape:
        raise ValueError(f"Shape mismatch: {y_true.shape} != {y_pred.shape}")

    f1 = float(f1_score(y_true, y_pred, average="macro", zero_division=0))
    return {"fitness": f1, "is_valid": 1}


