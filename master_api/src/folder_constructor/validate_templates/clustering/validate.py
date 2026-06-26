import numpy as np
from sklearn.cluster import KMeans
from sklearn.metrics import silhouette_score


def validate(
    context: dict[str, np.ndarray], selected_idx: np.ndarray
) -> dict[str, float]:
    """Cluster X_test using features selected and compute silhouette score.

    Input:
    - context: contains X_train, X_test
    - selected_idx: (K,) int indices of columns to keep
    """
    X_train = context["X_train"]
    X_test = context["X_test"]

    if selected_idx.ndim != 1:
        selected_idx = selected_idx.reshape(-1)
    selected_idx = np.asarray(selected_idx, dtype=np.int64)
    selected_idx = np.unique(selected_idx)
    if selected_idx.size == 0:
        selected_idx = np.arange(X_train.shape[1], dtype=np.int64)

    X_train_sel = X_train[:, selected_idx]
    X_test_sel = X_test[:, selected_idx]

    # Light clustering baseline
    n_clusters = ${n_clusters}
    kmeans = KMeans(n_clusters=n_clusters, n_init=10, random_state=42)
    kmeans.fit(X_train_sel)
    labels = kmeans.predict(X_test_sel)
    unique_labels = np.unique(labels)
    if unique_labels.size < 2 or unique_labels.size >= X_test_sel.shape[0]:
        return {"fitness": -1.0, "is_valid": 0}
    score = float(silhouette_score(X_test_sel, labels, metric="euclidean"))
    return {"fitness": score, "is_valid": 1}



