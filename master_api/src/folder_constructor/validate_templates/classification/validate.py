import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import f1_score


def validate(
    context: dict[str, np.ndarray], selected_idx: np.ndarray
) -> dict[str, float]:
    """Train a light classifier on selected features and compute F1 (macro).

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

    clf = LogisticRegression(max_iter=500, n_jobs=None)
    clf.fit(X_train_sel, y_train)
    y_pred = clf.predict(X_test_sel)

    if y_true.shape != y_pred.shape:
        raise ValueError(f"Shape mismatch: {y_true.shape} != {y_pred.shape}")
    f1 = float(f1_score(y_true, y_pred, average="macro", zero_division=0))
    return {"fitness": f1, "is_valid": 1}
