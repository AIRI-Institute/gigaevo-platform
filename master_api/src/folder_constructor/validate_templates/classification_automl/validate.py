import io
import contextlib
import warnings

import numpy as np
import pandas as pd
from sklearn.metrics import f1_score


def _build_lama_task(y: np.ndarray) -> "Task":
    """Infer LightAutoML task type (binary vs multiclass) from labels."""
    from lightautoml.tasks import Task

    classes = np.unique(y)
    if classes.size <= 2:
        return Task(name="binary", metric="auc")
    return Task(name="multiclass", metric="f1_macro")


def validate(
    context: dict[str, np.ndarray],
    selected_idx: np.ndarray,
) -> dict[str, float]:
    """Train an AutoML classifier on selected features and compute F1 (macro).

    Input:
    - context: contains X_train, y_train, X_test, y_test
    - selected_idx: (K,) int indices of columns to keep
    """
    # Silence LightAutoML text extras warnings BEFORE importing it, so
    # nothing is printed to stdout/stderr which would break the cloudpickle protocol.
    warnings.filterwarnings(
        "ignore",
        message="'nltk' - package isn't installed",
        category=UserWarning,
    )
    warnings.filterwarnings(
        "ignore",
        message="'fasttext' - package isn't installed",
        category=UserWarning,
    )

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

    buf_out = io.StringIO()
    buf_err = io.StringIO()
    with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
        # Import LightAutoML only inside the redirection context so that
        # library warnings/messages do not leak into external logs.
        from lightautoml.automl.presets.tabular_presets import TabularAutoML

        task = _build_lama_task(y_train)

        automl = TabularAutoML(
            task=task,
            timeout=60,
            cpu_limit=1,
            reader_params={"n_jobs": 1},
        )
        _ = automl.fit_predict(train_df, roles={"target": "target"})
        preds = automl.predict(test_df).data

    if preds.ndim == 2 and preds.shape[1] > 1:
        y_pred = preds.argmax(axis=1)
    else:
        y_pred = (preds.ravel() > 0.5).astype(int)

    if y_true.shape != y_pred.shape:
        raise ValueError(f"Shape mismatch: {y_true.shape} != {y_pred.shape}")

    f1 = float(f1_score(y_true, y_pred, average="macro", zero_division=0))
    return {"fitness": f1, "is_valid": 1}


