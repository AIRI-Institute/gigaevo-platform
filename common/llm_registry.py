"""Shared LLM registry loader.

Loads repo-level `llm_models.yml` (mounted into containers) and provides:
- UI dropdown choices (label, id)
- server-side validation helpers (allowed ids)
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

DEFAULT_REGISTRY_PATH = Path("/llm_models.yml")


def _find_registry_path() -> Path:
    """Find repo-level registry path.

    Single source of truth is the repo-level `llm_models.yml`.
    - In containers: it must be mounted to `/llm_models.yml`.
    - In local dev: we search upwards for `llm_models.yml`.
    """
    if DEFAULT_REGISTRY_PATH.exists():
        return DEFAULT_REGISTRY_PATH

    for parent in Path(__file__).resolve().parents:
        candidate = parent / "llm_models.yml"
        if candidate.exists():
            return candidate

    raise FileNotFoundError(
        "llm_models.yml not found. Expected it at /llm_models.yml (container) "
        "or somewhere above the `common/` package (local dev)."
    )


@lru_cache(maxsize=1)
def load_llm_registry() -> Dict[str, Any]:
    """Load and cache the LLM registry YAML.

    Loads `/llm_models.yml` which contains all model definitions including API keys.
    """
    path = _find_registry_path()
    import yaml  # type: ignore

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise TypeError("llm_models.yml root must be a mapping")
    if "models" not in data or not isinstance(data["models"], list):
        raise ValueError("llm_models.yml must contain a top-level 'models' list")

    return data


def get_llm_model_entry(model_id: str) -> Dict[str, Any]:
    """Return the model entry dict for a given model id, or raise if missing."""
    reg = load_llm_registry()
    models = reg.get("models", []) or []
    for m in models:
        if isinstance(m, dict) and str(m.get("id", "")).strip() == model_id:
            return m
    raise KeyError(f"llm_models.yml: model id not found: '{model_id}'")


def get_llm_runtime(model_id: str) -> Dict[str, Any]:
    """Return `runtime` mapping for the given model id (may be empty)."""
    entry = get_llm_model_entry(model_id)
    runtime = entry.get("runtime") or {}
    if runtime is None:
        runtime = {}
    if not isinstance(runtime, dict):
        raise TypeError(f"llm_models.yml: runtime for '{model_id}' must be a mapping")
    return runtime


def get_llm_runtime_required(model_id: str, *, required_keys: Set[str]) -> Dict[str, Any]:
    """Return runtime dict and validate required keys exist (non-empty for strings)."""
    runtime = get_llm_runtime(model_id)
    missing: List[str] = []
    for k in sorted(required_keys):
        if k not in runtime:
            missing.append(k)
            continue
        v = runtime.get(k)
        if isinstance(v, str) and not v.strip():
            missing.append(k)
    if missing:
        raise ValueError(f"llm_models.yml: runtime for '{model_id}' missing/empty keys: {', '.join(missing)}")
    return runtime


def get_llm_model_choices() -> List[Tuple[str, str]]:
    """Return dropdown choices as (label, id)."""
    reg = load_llm_registry()
    models = reg.get("models", []) or []
    choices: List[Tuple[str, str]] = []
    for m in models:
        if not isinstance(m, dict):
            continue
        mid = str(m.get("id", "")).strip()
        if not mid:
            continue
        label = str(m.get("label") or mid)
        choices.append((label, mid))
    return choices


def get_default_llm_model_id() -> str:
    reg = load_llm_registry()
    defaults = reg.get("defaults") or {}
    if isinstance(defaults, dict):
        mid = str(defaults.get("llm_model") or "").strip()
        if mid:
            return mid
    choices = get_llm_model_choices()
    if choices:
        return choices[0][1]
    raise ValueError("llm_models.yml has no usable models (missing ids)")


def get_default_prompt_llm_model_id() -> str:
    """Default model id for PROMPT_* settings."""
    reg = load_llm_registry()
    defaults = reg.get("defaults") or {}
    if isinstance(defaults, dict):
        mid = str(defaults.get("prompt_llm_model") or "").strip()
        if mid:
            return mid
    # Fall back to main default for backward compatibility
    return get_default_llm_model_id()


def get_allowed_llm_model_ids() -> Set[str]:
    reg = load_llm_registry()
    models: List[Any] = reg.get("models", []) or []
    out: Set[str] = set()
    for m in models:
        if isinstance(m, dict) and m.get("id"):
            out.add(str(m["id"]))
    return out


def is_allowed_llm_model(model_id: str) -> bool:
    return model_id in get_allowed_llm_model_ids()
