"""Surfaces the platform version to runtime code.

The version is declared once, in the root ``pyproject.toml``
(``[project].version``). This module does not store the number; it resolves
it so callers can keep using ``from common.version import __version__``.

Resolution order:
  1. Installed package metadata — when ``gigaevo-platform`` is pip-installed.
  2. The nearest ``pyproject.toml`` that declares this project — covers the
     container/source layout (``PYTHONPATH``-based, package not installed),
     where the root pyproject travels alongside ``common/``.
  3. ``"0.0.0"`` as a last resort.
"""

from __future__ import annotations


def _resolve_version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("gigaevo-platform")
    except PackageNotFoundError:
        pass

    import tomllib
    from pathlib import Path

    for parent in Path(__file__).resolve().parents:
        candidate = parent / "pyproject.toml"
        if not candidate.is_file():
            continue
        try:
            data = tomllib.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            continue
        project = data.get("project", {})
        if project.get("name") == "gigaevo-platform" and project.get("version"):
            return project["version"]

    return "0.0.0"


__version__ = _resolve_version()
