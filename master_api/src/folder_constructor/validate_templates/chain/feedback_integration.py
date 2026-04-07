"""Chain Feedback Integration Module

This module provides functions to generate chain feedback during validation.
It integrates with the ChainFeedbackFormatter service to provide structured
feedback from chain execution results.

Import strategy (fixed):
  1. Try importing ``chain_feedback_service`` from the *experiment* directory
     (``_current_dir``, which is also where this file lives).
  2. Walk up to ``src/services/`` relative to the experiment if the flat import
     fails.
  3. Fall back to a **basic inline** formatter that never returns None.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List, Optional
from pathlib import Path

_current_dir = Path(__file__).parent
if str(_current_dir) not in sys.path:
    sys.path.insert(0, str(_current_dir))

# ---------------------------------------------------------------------------
# Try to import the rich ChainFeedbackFormatter.
# We attempt multiple strategies because the relative directory layout
# differs between local dev (master_api/src/services/...) and the deployed
# container (runner_api/repos/gigaevo-core-1/problems/<exp_id>/…).
# ---------------------------------------------------------------------------
_ChainFeedbackFormatter = None
_FeedbackTemplate = None

def _try_import_service():
    """Attempt to import ChainFeedbackFormatter via several paths."""
    global _ChainFeedbackFormatter, _FeedbackTemplate

    # Strategy 1: direct import (works if validate.py already set sys.path)
    try:
        from chain_feedback_service import ChainFeedbackFormatter, FeedbackTemplate
        _ChainFeedbackFormatter = ChainFeedbackFormatter
        _FeedbackTemplate = FeedbackTemplate
        return True
    except ImportError:
        pass

    # Strategy 2: look for src/services/ up to 5 levels above this file
    for up in range(1, 6):
        candidate = _current_dir
        for _ in range(up):
            candidate = candidate.parent
        svc_dir = candidate / "src" / "services"
        if svc_dir.is_dir() and str(svc_dir) not in sys.path:
            sys.path.insert(0, str(svc_dir))
            try:
                from chain_feedback_service import ChainFeedbackFormatter, FeedbackTemplate
                _ChainFeedbackFormatter = ChainFeedbackFormatter
                _FeedbackTemplate = FeedbackTemplate
                return True
            except ImportError:
                pass

    # Strategy 3: from src.services (package-style)
    try:
        from src.services.chain_feedback_service import ChainFeedbackFormatter, FeedbackTemplate
        _ChainFeedbackFormatter = ChainFeedbackFormatter
        _FeedbackTemplate = FeedbackTemplate
        return True
    except ImportError:
        pass

    return False

_RICH_AVAILABLE = _try_import_service()
if _RICH_AVAILABLE:
    print("[Chain Feedback] Rich feedback service imported successfully", file=sys.stderr)
else:
    print("[Chain Feedback] Rich feedback service NOT available — "
          "generate_chain_feedback will return None (validate.py basic fallback will be used)",
          file=sys.stderr)


def generate_chain_feedback(
    results: List[Dict[str, Any]],
    target_column: str,
    experiment_dir: Optional[str] = None
) -> Optional[str]:
    """Generate chain feedback from execution results.

    This function reads feedback settings from the experiment directory
    and generates formatted feedback if enabled.

    Args:
        results: List of execution results from chain.run()
        target_column: Name of the target field
        experiment_dir: Path to experiment directory (to read settings)

    Returns:
        Formatted markdown feedback string, or None if feedback is disabled
        or the rich service is not available (validate.py will use basic fallback).
    """
    if not results:
        return None

    # Read feedback settings from experiment directory.
    enable_feedback = True
    feedback_template = "detailed"

    if experiment_dir:
        try:
            chain_spec_path = os.path.join(experiment_dir, "chain_spec.json")
            if os.path.exists(chain_spec_path):
                with open(chain_spec_path, 'r') as f:
                    chain_spec = json.load(f)
                    enable_feedback = chain_spec.get("enable_feedback", True)
                    feedback_template = chain_spec.get("feedback_template", "detailed")
        except Exception as e:
            print(f"[Chain Feedback] Could not read chain_spec: {e}", file=sys.stderr)

    if not enable_feedback:
        return None

    if not _RICH_AVAILABLE or _ChainFeedbackFormatter is None:
        # Caller (validate.py) will use its own _build_basic_feedback() fallback
        return None

    # Create formatter and generate feedback
    try:
        tpl = _FeedbackTemplate(feedback_template) if _FeedbackTemplate else None
        formatter = _ChainFeedbackFormatter(template=tpl)
        feedback = formatter.format_results(results, target_column)

        print(f"[Chain Feedback] Generated {len(feedback)} chars of feedback "
              f"(template: {feedback_template})", file=sys.stderr)

        # Save feedback files for debugging / API access
        if experiment_dir:
            _save_feedback_files(experiment_dir, feedback, feedback_template,
                                len(results), target_column)

        return feedback

    except Exception as e:
        print(f"[Chain Feedback] Failed to generate feedback: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)
        return None


def _save_feedback_files(
    experiment_dir: str,
    feedback: str,
    feedback_template: str,
    results_count: int,
    target_column: str,
) -> None:
    """Save feedback to .md and .json files in the experiment directory."""
    try:
        feedback_path = os.path.join(experiment_dir, "chain_feedback.md")
        with open(feedback_path, 'w', encoding='utf-8') as f:
            f.write(feedback)
        print(f"[Chain Feedback] Saved feedback to {feedback_path}", file=sys.stderr)

        feedback_json_path = os.path.join(experiment_dir, "chain_feedback.json")
        feedback_data = {
            "feedback_template": feedback_template,
            "feedback_text": feedback,
            "results_count": results_count,
            "target_column": target_column,
            "enabled": True,
        }
        with open(feedback_json_path, 'w', encoding='utf-8') as f:
            json.dump(feedback_data, f, indent=2, ensure_ascii=False)
    except Exception as save_err:
        print(f"[Chain Feedback] Failed to save feedback: {save_err}", file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)


def add_feedback_to_validation_result(
    validation_result: Dict[str, Any],
    feedback: Optional[str]
) -> Dict[str, Any]:
    """Add feedback metadata to validation result dictionary.

    Note: The feedback string itself is NOT added to the metrics dict (which must
    contain only floats). Instead, feedback is passed as the artifact (second element
    of the tuple returned by validate()). This function only adds a numeric flag
    ``has_feedback`` to indicate whether feedback was generated.
    """
    if feedback is not None and isinstance(feedback, str) and feedback.strip():
        validation_result["has_feedback"] = 1.0
    else:
        validation_result["has_feedback"] = 0.0
    return validation_result
