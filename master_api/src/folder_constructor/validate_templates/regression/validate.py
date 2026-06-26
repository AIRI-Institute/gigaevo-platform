import numpy as np
from sklearn.linear_model import Ridge
from sklearn.metrics import r2_score


def validate(
    context: dict[str, np.ndarray], selected_idx: np.ndarray
) -> dict[str, float]:
    """Train a light regressor on selected features and compute R2.

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

    model = Ridge(alpha=1.0)
    model.fit(X_train_sel, y_train)
    y_pred = model.predict(X_test_sel).astype(np.float32)

    if y_true.shape != y_pred.shape:
        raise ValueError(f"Shape mismatch: {y_true.shape} != {y_pred.shape}")
    r2 = float(r2_score(y_true, y_pred))
    return {"fitness": r2, "is_valid": 1}
