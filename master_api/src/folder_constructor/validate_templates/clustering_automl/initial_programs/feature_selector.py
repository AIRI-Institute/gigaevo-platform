import numpy as np


def entrypoint(context: dict[str, np.ndarray]) -> np.ndarray:
    """Return selected feature indices for clustering in validation.

    Context keys: X_train, X_test
    Return: (K,) int64 array of unique column indices to keep.
    """
    X_train = context["X_train"]
    D = int(X_train.shape[1])

    k = max(1, int(np.sqrt(D)))
    k = min(D, k)
    selected_mask = np.zeros(D, dtype=bool)
    selected_mask[:k] = True

    if selected_mask.dtype == bool:
        indices = np.flatnonzero(selected_mask)
    else:
        indices = np.asarray(selected_mask, dtype=np.int64)
    indices = np.unique(indices.astype(np.int64))
    return indices


