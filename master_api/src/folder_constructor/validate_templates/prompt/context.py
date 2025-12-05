"""Context builder for ${task_name} (self-contained)."""

from typing import TypedDict
import os
import yaml
import pandas as pd
from pathlib import Path


class ContextDict(TypedDict, total=False):
    model_name: str
    train_dataset: pd.DataFrame
    val_dataset: pd.DataFrame
    max_cost: float


def _find_env_file() -> Path | None:
    """Find .env file by searching in multiple locations."""
    # 1) Try PROBLEM_DIR (set by runner_api when executing experiments)
    problem_dir = os.getenv("PROBLEM_DIR")
    if problem_dir:
        # Look for .env relative to problem directory (go up to repo root)
        current = Path(problem_dir).resolve()
        for _ in range(5):
            env_path = current / ".env"
            if env_path.exists():
                return env_path
            parent = current.parent
            if parent == current:  # Reached filesystem root
                break
            current = parent
    
    # 2) Try from current file location (for local development)
    current = Path(__file__).resolve().parent
    for _ in range(10):
        env_path = current / ".env"
        if env_path.exists():
            return env_path
        parent = current.parent
        if parent == current:  # Reached filesystem root
            break
        current = parent
    
    # 3) Try common project root locations
    common_roots = [
        Path("/app"),  # Docker container root
        Path("/app/gigaevo-platform"),  # Project root in container
        Path.cwd(),  # Current working directory
    ]
    for root in common_roots:
        env_path = root / ".env"
        if env_path.exists():
            return env_path
    
    return None


def _load_env_file(path: Path) -> None:
    """Load simple KEY=VALUE lines from a .env file into os.environ (no deps)."""
    try:
        if not path.exists():
            return
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    except Exception:
        # Best-effort; ignore parsing errors
        pass


def _get_model_name() -> str:
    # 1) Environment variable (check both PROMPT_MODEL_NAME and PROMPT_MODEL for compatibility)
    env_model = os.getenv("PROMPT_MODEL_NAME") or os.getenv("PROMPT_MODEL")
    if env_model:
        return env_model

    # 2) Load from .env file then re-check env
    # Search for .env file starting from this file's location
    env_file = _find_env_file()
    if env_file:
        _load_env_file(env_file)

    # Check again after loading .env (both variable names)
    env_model = os.getenv("PROMPT_MODEL_NAME") or os.getenv("PROMPT_MODEL")
    if env_model:
        return env_model

    # If still not configured, instruct user to set .env or env var
    raise FileNotFoundError(
        "Model name not configured. Either:\n"
        "1. Set PROMPT_MODEL_NAME or PROMPT_MODEL in .env file (project root), or\n"
        "2. Export PROMPT_MODEL_NAME or PROMPT_MODEL in environment"
    )


def _load_dataset(filename: str = "train.csv") -> pd.DataFrame:
    # 1) Repo-root relative path (standard runner cwd)
    repo_rel = Path("problems") / "${task_name}" / "dataset" / filename
    if repo_rel.exists():
        return pd.read_csv(repo_rel).reset_index(drop=True).head(8) # TODO: extend
    # 2) Local directory fallback (if executed from problem dir)
    local_rel = Path("dataset") / filename
    if local_rel.exists():
        return pd.read_csv(local_rel).reset_index(drop=True).head(8) # TODO: extend
    raise FileExistsError(f"Dataset names '{filename}' not found, check paths: {repo_rel} or {local_rel}")


def build_context() -> ContextDict:
    return {
        "model_name": _get_model_name(),
        "train_dataset": _load_dataset("data.csv"),
        "val_dataset": None,
        "max_cost": 1.0,
    }
