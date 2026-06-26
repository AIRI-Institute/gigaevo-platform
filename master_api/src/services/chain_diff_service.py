"""Chain Diff Service for comparing CARL chain configurations.

Compares an initial (base) chain config with an evolved chain config,
producing a structured diff that highlights additions, deletions,
and modifications at the step level.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class DiffType(str, Enum):
    ADDED = "added"
    REMOVED = "removed"
    MODIFIED = "modified"
    UNCHANGED = "unchanged"


@dataclass
class FieldDiff:
    """Diff of a single field within a step."""

    field_name: str
    diff_type: DiffType
    old_value: Any = None
    new_value: Any = None


@dataclass
class StepDiff:
    """Diff of a single step."""

    step_number: int
    diff_type: DiffType
    step_title: str = ""
    old_step_type: str = "llm"
    new_step_type: str = "llm"
    field_diffs: List[FieldDiff] = field(default_factory=list)

    @property
    def has_type_change(self) -> bool:
        return self.old_step_type != self.new_step_type

    @property
    def modified_fields_count(self) -> int:
        return sum(1 for fd in self.field_diffs if fd.diff_type == DiffType.MODIFIED)


@dataclass
class ChainDiff:
    """Complete diff between two chain configurations."""

    step_diffs: List[StepDiff] = field(default_factory=list)
    chain_level_changes: List[FieldDiff] = field(default_factory=list)

    @property
    def total_added(self) -> int:
        return sum(1 for sd in self.step_diffs if sd.diff_type == DiffType.ADDED)

    @property
    def total_removed(self) -> int:
        return sum(1 for sd in self.step_diffs if sd.diff_type == DiffType.REMOVED)

    @property
    def total_modified(self) -> int:
        return sum(1 for sd in self.step_diffs if sd.diff_type == DiffType.MODIFIED)

    @property
    def total_unchanged(self) -> int:
        return sum(1 for sd in self.step_diffs if sd.diff_type == DiffType.UNCHANGED)

    @property
    def has_changes(self) -> bool:
        return (
            self.total_added > 0
            or self.total_removed > 0
            or self.total_modified > 0
            or len(self.chain_level_changes) > 0
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "summary": {
                "added_steps": self.total_added,
                "removed_steps": self.total_removed,
                "modified_steps": self.total_modified,
                "unchanged_steps": self.total_unchanged,
                "chain_level_changes": len(self.chain_level_changes),
                "has_changes": self.has_changes,
            },
            "step_diffs": [
                {
                    "step_number": sd.step_number,
                    "diff_type": sd.diff_type.value,
                    "step_title": sd.step_title,
                    "old_step_type": sd.old_step_type,
                    "new_step_type": sd.new_step_type,
                    "has_type_change": sd.has_type_change,
                    "modified_fields_count": sd.modified_fields_count,
                    "field_diffs": [
                        {
                            "field_name": fd.field_name,
                            "diff_type": fd.diff_type.value,
                            "old_value": fd.old_value,
                            "new_value": fd.new_value,
                        }
                        for fd in sd.field_diffs
                        if fd.diff_type != DiffType.UNCHANGED
                    ],
                }
                for sd in self.step_diffs
                if sd.diff_type != DiffType.UNCHANGED
            ],
            "chain_level_changes": [
                {
                    "field_name": fd.field_name,
                    "diff_type": fd.diff_type.value,
                    "old_value": fd.old_value,
                    "new_value": fd.new_value,
                }
                for fd in self.chain_level_changes
            ],
        }


# Fields that are semantically important for step comparison
_STEP_COMPARE_FIELDS = [
    "title",
    "aim",
    "reasoning_questions",
    "stage_action",
    "example_reasoning",
    "step_type",
    "step_config",
    "step_context_queries",
    "dependencies",
    "frozen",
]

# Chain-level fields to compare (outside steps)
_CHAIN_LEVEL_FIELDS = [
    "search_config",
    "enable_progress",
    "chain_size_limit",
]


def _normalize_value(value: Any) -> Any:
    """Normalize value for comparison (handle None, lists, dicts)."""
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return value
    return value


def _values_equal(a: Any, b: Any) -> bool:
    """Deep comparison of two values."""
    a = _normalize_value(a)
    b = _normalize_value(b)
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    if isinstance(a, dict) and isinstance(b, dict):
        if set(a.keys()) != set(b.keys()):
            return False
        return all(_values_equal(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return False
        return all(_values_equal(ai, bi) for ai, bi in zip(a, b))
    return a == b


def compute_chain_diff(
    initial_config: Dict[str, Any],
    evolved_config: Dict[str, Any],
) -> ChainDiff:
    """Compare two CARL chain configurations and produce a structured diff.

    Args:
        initial_config: The original/base chain configuration
        evolved_config: The evolved/modified chain configuration

    Returns:
        ChainDiff with step-level and chain-level changes
    """
    diff = ChainDiff()

    # --- Chain-level field diffs ---
    for fld in _CHAIN_LEVEL_FIELDS:
        old_val = initial_config.get(fld)
        new_val = evolved_config.get(fld)
        if not _values_equal(old_val, new_val):
            if old_val is None:
                dt = DiffType.ADDED
            elif new_val is None:
                dt = DiffType.REMOVED
            else:
                dt = DiffType.MODIFIED
            diff.chain_level_changes.append(
                FieldDiff(field_name=fld, diff_type=dt, old_value=old_val, new_value=new_val)
            )

    # --- Step-level diffs ---
    initial_steps = initial_config.get("steps", [])
    evolved_steps = evolved_config.get("steps", [])

    initial_by_num: Dict[int, Dict] = {}
    for s in initial_steps:
        num = s.get("number")
        if num is not None:
            initial_by_num[int(num)] = s

    evolved_by_num: Dict[int, Dict] = {}
    for s in evolved_steps:
        num = s.get("number")
        if num is not None:
            evolved_by_num[int(num)] = s

    all_step_numbers = sorted(set(initial_by_num.keys()) | set(evolved_by_num.keys()))

    for step_num in all_step_numbers:
        old_step = initial_by_num.get(step_num)
        new_step = evolved_by_num.get(step_num)

        if old_step is None and new_step is not None:
            # Added step
            sd = StepDiff(
                step_number=step_num,
                diff_type=DiffType.ADDED,
                step_title=new_step.get("title", ""),
                new_step_type=str(new_step.get("step_type", "llm")).lower(),
            )
            # All fields are "added"
            for fld in _STEP_COMPARE_FIELDS:
                val = new_step.get(fld)
                if val is not None:
                    sd.field_diffs.append(FieldDiff(field_name=fld, diff_type=DiffType.ADDED, new_value=val))
            diff.step_diffs.append(sd)

        elif old_step is not None and new_step is None:
            # Removed step
            sd = StepDiff(
                step_number=step_num,
                diff_type=DiffType.REMOVED,
                step_title=old_step.get("title", ""),
                old_step_type=str(old_step.get("step_type", "llm")).lower(),
            )
            for fld in _STEP_COMPARE_FIELDS:
                val = old_step.get(fld)
                if val is not None:
                    sd.field_diffs.append(FieldDiff(field_name=fld, diff_type=DiffType.REMOVED, old_value=val))
            diff.step_diffs.append(sd)

        else:
            # Both exist — compare field by field
            old_type = str(old_step.get("step_type", "llm")).lower()
            new_type = str(new_step.get("step_type", "llm")).lower()
            sd = StepDiff(
                step_number=step_num,
                diff_type=DiffType.UNCHANGED,
                step_title=new_step.get("title", old_step.get("title", "")),
                old_step_type=old_type,
                new_step_type=new_type,
            )

            for fld in _STEP_COMPARE_FIELDS:
                old_val = old_step.get(fld)
                new_val = new_step.get(fld)
                if not _values_equal(old_val, new_val):
                    sd.field_diffs.append(
                        FieldDiff(
                            field_name=fld,
                            diff_type=DiffType.MODIFIED,
                            old_value=old_val,
                            new_value=new_val,
                        )
                    )
                    sd.diff_type = DiffType.MODIFIED

            # Also check any extra fields not in standard list
            all_keys = set(old_step.keys()) | set(new_step.keys())
            checked = set(_STEP_COMPARE_FIELDS) | {"number"}
            for k in sorted(all_keys - checked):
                old_val = old_step.get(k)
                new_val = new_step.get(k)
                if not _values_equal(old_val, new_val):
                    sd.field_diffs.append(
                        FieldDiff(
                            field_name=k,
                            diff_type=DiffType.MODIFIED,
                            old_value=old_val,
                            new_value=new_val,
                        )
                    )
                    sd.diff_type = DiffType.MODIFIED

            diff.step_diffs.append(sd)

    return diff


def _escape_html(text: str) -> str:
    """Escape HTML special characters."""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _truncate(text: str, max_len: int = 300) -> str:
    """Truncate text for display."""
    text = str(text)
    if len(text) > max_len:
        return text[:max_len] + "..."
    return text


def _format_value_readable(value: Any, field_name: str = "") -> str:
    """Format a field value for human-readable display.

    Strings are shown directly (no JSON quotes), dicts/lists are shown as
    compact JSON. Long text is wrapped in a <pre> block.
    """
    if value is None:
        return "<em>none</em>"
    if isinstance(value, str):
        escaped = _escape_html(value)
        if len(value) > 120 or "\n" in value:
            return f'<pre style="white-space:pre-wrap;margin:2px 0;max-height:200px;overflow:auto">{escaped}</pre>'
        return escaped
    if isinstance(value, (dict, list)):
        raw = json.dumps(value, indent=2, ensure_ascii=False)
        escaped = _escape_html(raw)
        if len(raw) > 120:
            return f'<pre style="white-space:pre-wrap;margin:2px 0;max-height:200px;overflow:auto">{escaped}</pre>'
        return f"<code>{escaped}</code>"
    return _escape_html(str(value))


# Step type icons (matching CARL builder)
_STEP_TYPE_ICONS = {
    "LLM": "🧠",
    "TOOL": "🔧",
    "MCP": "🔌",
    "MEMORY": "💾",
    "TRANSFORM": "🔄",
    "CONDITIONAL": "🔀",
    "STRUCTURED_OUTPUT": "📋",
}

# Step type colors (matching CARL builder)
_STEP_TYPE_COLORS = {
    "LLM": "#4A90D9",
    "TOOL": "#E67E22",
    "MCP": "#8E44AD",
    "MEMORY": "#27AE60",
    "TRANSFORM": "#F39C12",
    "CONDITIONAL": "#E74C3C",
    "STRUCTURED_OUTPUT": "#1ABC9C",
}

# ---- shared CSS for both diff and summary views ----
_SHARED_CSS = """
<style>
.chain-diff { font-family: system-ui, -apple-system, sans-serif; font-size: 13px; }
.chain-diff .summary { background: #f0f4ff; border: 1px solid #c0d0ff; border-radius: 8px; padding: 12px 16px; margin-bottom: 12px; }
.chain-diff .summary-badges { display: flex; gap: 10px; flex-wrap: wrap; }
.chain-diff .badge { padding: 3px 10px; border-radius: 12px; font-size: 12px; font-weight: 600; }
.chain-diff .badge-added { background: #d4edda; color: #155724; }
.chain-diff .badge-removed { background: #f8d7da; color: #721c24; }
.chain-diff .badge-modified { background: #fff3cd; color: #856404; }
.chain-diff .badge-ok { background: #d4edda; color: #155724; }
.chain-diff .step-card { border: 1px solid #dee2e6; border-radius: 8px; margin-bottom: 10px; overflow: hidden; }
.chain-diff .step-header { padding: 8px 14px; font-weight: 600; font-size: 13px; }
.chain-diff .step-header-added { background: #d4edda; color: #155724; border-left: 4px solid #28a745; }
.chain-diff .step-header-removed { background: #f8d7da; color: #721c24; border-left: 4px solid #dc3545; }
.chain-diff .step-header-modified { background: #fff3cd; color: #856404; border-left: 4px solid #ffc107; }
.chain-diff .step-header-unchanged { background: #f8f9fa; color: #495057; border-left: 4px solid #adb5bd; }
.chain-diff .field-row { padding: 6px 14px; border-top: 1px solid #f0f0f0; font-size: 12px; }
.chain-diff .field-name { font-weight: 600; color: #495057; min-width: 140px; display: inline-block; }
.chain-diff .field-old { background: #ffeef0; padding: 2px 6px; border-radius: 3px; margin: 2px 0; display: block; }
.chain-diff .field-new { background: #e6ffed; padding: 2px 6px; border-radius: 3px; margin: 2px 0; display: block; }
.chain-diff .field-val { padding: 2px 6px; margin: 2px 0; display: block; }
.chain-diff .type-change { background: #e8daef; padding: 2px 8px; border-radius: 3px; font-weight: 600; }
.chain-diff-visual .step-card-visual { background: #fff; border: 1px solid #e0e0e0; border-radius: 8px; padding: 10px 14px; margin-bottom: 2px; }
.chain-diff-visual .step-card-visual.added { background: #f0fdf4; border-left: 4px solid #28a745; }
.chain-diff-visual .step-card-visual.removed { background: #fef2f2; border-left: 4px solid #dc3545; }
.chain-diff-visual .step-card-visual.modified { background: #fffbeb; border-left: 4px solid #ffc107; }
.chain-diff-visual .step-card-visual.unchanged { border-left: 4px solid #adb5bd; }
.chain-diff-visual .field-diff-old { background: #ffeef0; color: #721c24; text-decoration: line-through; padding: 2px 6px; border-radius: 3px; margin: 2px 0; display: inline-block; }
.chain-diff-visual .field-diff-new { background: #e6ffed; color: #155724; padding: 2px 6px; border-radius: 3px; margin: 2px 0; display: inline-block; }
.chain-diff-visual .field-diff-arrow { color: #666; margin: 0 4px; }
</style>
"""


def format_chain_summary_html(config: Dict[str, Any]) -> str:
    """Render a read-only, user-friendly summary of a CARL chain configuration.

    Used when there is no diff to show (e.g. both configs are identical, or only
    one config is available).
    """
    steps = config.get("steps", [])
    parts = [_SHARED_CSS, '<div class="chain-diff">']

    # header
    step_types = {}
    for s in steps:
        st = str(s.get("step_type", "llm")).upper()
        step_types[st] = step_types.get(st, 0) + 1
    type_desc = ", ".join(f"{v}x {k}" for k, v in sorted(step_types.items()))
    parts.append(
        f'<div class="summary"><b>Chain overview</b> &mdash; {len(steps)} step(s): {_escape_html(type_desc)}</div>'
    )

    # each step as a card
    for s in steps:
        num = s.get("number", "?")
        title = s.get("title", "Untitled")
        stype = str(s.get("step_type", "llm")).upper()
        parts.append('<div class="step-card">')
        parts.append(
            f'<div class="step-header step-header-unchanged">Step {num}: {_escape_html(title)} [{stype}]</div>'
        )
        # Show key readable fields
        for fld in ("aim", "stage_action", "reasoning_questions", "example_reasoning"):
            val = s.get(fld)
            if val:
                parts.append(
                    f'<div class="field-row">'
                    f'<span class="field-name">{_escape_html(fld)}</span>'
                    f'<span class="field-val">{_format_value_readable(val, fld)}</span>'
                    f"</div>"
                )
        # step_config — show as compact JSON if present
        sc = s.get("step_config")
        if sc:
            parts.append(
                f'<div class="field-row">'
                f'<span class="field-name">step_config</span>'
                f'<span class="field-val">{_format_value_readable(sc, "step_config")}</span>'
                f"</div>"
            )
        parts.append("</div>")

    parts.append("</div>")
    return "\n".join(parts)


def format_visual_chain_diff_html(
    diff: ChainDiff,
    evolved_config: Optional[Dict[str, Any]] = None,
    initial_config: Optional[Dict[str, Any]] = None,
) -> str:
    """Format a ChainDiff as rich HTML cards (like CARL builder) showing all steps with diff coloring.

    Shows all steps including unchanged ones, with visual diff highlighting:
    - Added steps: green left-border and background
    - Removed steps: red left-border and background with strikethrough
    - Modified steps: yellow/orange left-border, with field-level diffs shown inline
    - Unchanged steps: neutral gray left-border

    Args:
        diff: The computed ChainDiff
        evolved_config: The evolved chain config (for step details)
        initial_config: The initial chain config (for removed step details)

    Returns:
        HTML string with card-based visual diff
    """
    if not evolved_config and not initial_config:
        return (
            _SHARED_CSS + '<div class="chain-diff"><div class="summary">'
            "No chain configurations available to compare."
            "</div></div>"
        )

    if not diff.has_changes and evolved_config:
        # No changes — show a readable chain summary
        header = (
            '<div class="chain-diff-visual"><div class="summary">'
            '<span class="badge badge-ok">Chain unchanged</span> '
            "The evolved chain is identical to the initial chain."
            "</div></div>\n"
        )
        return _SHARED_CSS + header + format_chain_summary_html(evolved_config)

    parts = [_SHARED_CSS, '<div class="chain-diff-visual">']

    # Summary bar
    parts.append('<div class="summary"><div class="summary-badges">')
    if diff.total_added:
        parts.append(f'<span class="badge badge-added">+ {diff.total_added} added</span>')
    if diff.total_removed:
        parts.append(f'<span class="badge badge-removed">- {diff.total_removed} removed</span>')
    if diff.total_modified:
        parts.append(f'<span class="badge badge-modified">~ {diff.total_modified} modified</span>')
    if diff.total_unchanged:
        parts.append(f'<span class="badge badge-ok">{diff.total_unchanged} unchanged</span>')
    if diff.chain_level_changes:
        parts.append(f'<span class="badge badge-modified">chain-level: {len(diff.chain_level_changes)}</span>')
    parts.append("</div></div>")

    # Chain-level changes
    if diff.chain_level_changes:
        parts.append('<div class="step-card">')
        parts.append('<div class="step-header step-header-modified">Chain-level changes</div>')
        for fd in diff.chain_level_changes:
            parts.append('<div class="field-row">')
            parts.append(f'<span class="field-name">{_escape_html(fd.field_name)}</span>')
            if fd.old_value is not None:
                parts.append(f'<span class="field-old">- {_format_value_readable(fd.old_value, fd.field_name)}</span>')
            if fd.new_value is not None:
                parts.append(f'<span class="field-new">+ {_format_value_readable(fd.new_value, fd.field_name)}</span>')
            parts.append("</div>")
        parts.append("</div>")

    # Build step lookup maps
    evolved_steps_by_num: Dict[int, Dict[str, Any]] = {}
    if evolved_config:
        for s in evolved_config.get("steps", []):
            num = s.get("number")
            if num is not None:
                evolved_steps_by_num[int(num)] = s

    initial_steps_by_num: Dict[int, Dict[str, Any]] = {}
    if initial_config:
        for s in initial_config.get("steps", []):
            num = s.get("number")
            if num is not None:
                initial_steps_by_num[int(num)] = s

    # Sort step diffs by step number
    sorted_step_diffs = sorted(diff.step_diffs, key=lambda sd: sd.step_number)

    # Render each step as a card
    for i, sd in enumerate(sorted_step_diffs):
        # Get step data (prefer evolved, fallback to initial for removed steps)
        step_data = evolved_steps_by_num.get(sd.step_number) or initial_steps_by_num.get(sd.step_number) or {}

        # Determine step type and icon
        step_type = str(sd.new_step_type if sd.diff_type != DiffType.REMOVED else sd.old_step_type).upper()
        icon = _STEP_TYPE_ICONS.get(step_type, "⚙️")
        type_color = _STEP_TYPE_COLORS.get(step_type, "#95A5A6")

        # Card class based on diff type
        card_class = sd.diff_type.value
        if sd.diff_type == DiffType.UNCHANGED:
            card_class = "unchanged"

        # Title with strikethrough for removed steps
        title = sd.step_title or f"Step {sd.step_number}"
        if sd.diff_type == DiffType.REMOVED:
            title = f'<span style="text-decoration: line-through;">{_escape_html(title)}</span>'
        else:
            title = _escape_html(title)

        # Frozen badge
        frozen = step_data.get("frozen", False)
        frozen_badge = (
            ' <span style="background:#3498DB;color:#fff;padding:1px 7px;'
            'border-radius:8px;font-size:11px;">❄ frozen</span>'
            if frozen
            else ""
        )

        # Dependencies
        deps = step_data.get("dependencies", [])
        deps_text = f" ← steps {', '.join(str(d) for d in deps)}" if deps else ""

        # Type change indicator
        type_change_html = ""
        if sd.has_type_change:
            type_change_html = (
                f' <span style="background:#e8daef;padding:2px 8px;border-radius:3px;'
                f'font-weight:600;font-size:11px;">'
                f"{_escape_html(sd.old_step_type)} → {_escape_html(sd.new_step_type)}</span>"
            )

        # Build field diffs map for quick lookup
        field_diffs_map: Dict[str, FieldDiff] = {}
        for fd in sd.field_diffs:
            if fd.diff_type != DiffType.UNCHANGED:
                field_diffs_map[fd.field_name] = fd

        # Build card header
        parts.append(f'<div class="step-card-visual {card_class}">')
        parts.append(
            f'<div style="display:flex;align-items:center;gap:8px;">'
            f'<span style="font-size:20px;">{icon}</span>'
            f'<span style="background:{type_color};color:#fff;padding:2px 8px;border-radius:4px;'
            f'font-size:12px;font-weight:600;">{step_type}</span>'
            f'<b style="font-size:14px;">#{sd.step_number} {title}</b>'
            f"{frozen_badge}{type_change_html}"
            f'<span style="color:#aaa;font-size:11px;margin-left:auto;">{deps_text}</span>'
            f"</div>"
        )

        # Show key fields with diff highlighting
        key_fields = ["aim", "stage_action", "reasoning_questions", "example_reasoning"]
        for field_name in key_fields:
            field_value = step_data.get(field_name)
            if field_value or field_name in field_diffs_map:
                field_html = ""
                if field_name in field_diffs_map:
                    # Show diff
                    fd = field_diffs_map[field_name]
                    if fd.old_value is not None:
                        old_val_html = _format_value_readable(fd.old_value, field_name)
                        field_html += f'<span class="field-diff-old">{old_val_html}</span>'
                        field_html += '<span class="field-diff-arrow">→</span>'
                    if fd.new_value is not None:
                        new_val_html = _format_value_readable(fd.new_value, field_name)
                        field_html += f'<span class="field-diff-new">{new_val_html}</span>'
                elif field_value:
                    # Show unchanged value
                    val_html = _format_value_readable(field_value, field_name)
                    field_html = f'<span style="color:#666;">{val_html}</span>'

                if field_html:
                    parts.append(
                        f'<div style="color:#666;font-size:12px;margin-top:4px;">'
                        f"<b>{_escape_html(field_name)}:</b> {field_html}"
                        f"</div>"
                    )

        # Show step_config if present
        step_config = step_data.get("step_config", {}) or {}
        if step_config or "step_config" in field_diffs_map:
            config_html = ""
            if "step_config" in field_diffs_map:
                # Show diff
                fd = field_diffs_map["step_config"]
                if fd.old_value is not None:
                    old_val_html = _format_value_readable(fd.old_value, "step_config")
                    config_html += f'<span class="field-diff-old">{old_val_html}</span>'
                    config_html += '<span class="field-diff-arrow">→</span>'
                if fd.new_value is not None:
                    new_val_html = _format_value_readable(fd.new_value, "step_config")
                    config_html += f'<span class="field-diff-new">{new_val_html}</span>'
            elif step_config:
                # Show unchanged value
                val_html = _format_value_readable(step_config, "step_config")
                config_html = f'<span style="color:#888;">{val_html}</span>'

            if config_html:
                parts.append(
                    f'<div style="color:#888;font-size:11px;margin-top:2px;"><b>step_config:</b> {config_html}</div>'
                )

        # Show other modified fields not in key_fields
        for fd in sd.field_diffs:
            if (
                fd.field_name not in key_fields
                and fd.field_name != "step_config"
                and fd.diff_type != DiffType.UNCHANGED
            ):
                field_html = ""
                if fd.old_value is not None:
                    old_val_html = _format_value_readable(fd.old_value, fd.field_name)
                    field_html += f'<span class="field-diff-old">{old_val_html}</span>'
                    field_html += '<span class="field-diff-arrow">→</span>'
                if fd.new_value is not None:
                    new_val_html = _format_value_readable(fd.new_value, fd.field_name)
                    field_html += f'<span class="field-diff-new">{new_val_html}</span>'

                if field_html:
                    parts.append(
                        f'<div style="color:#666;font-size:12px;margin-top:4px;">'
                        f"<b>{_escape_html(fd.field_name)}:</b> {field_html}"
                        f"</div>"
                    )

        parts.append("</div>")

        # Arrow between cards
        if i < len(sorted_step_diffs) - 1:
            parts.append('<div style="text-align:center;color:#bbb;font-size:16px;line-height:20px;">↓</div>')

    parts.append("</div>")
    return "\n".join(parts)
