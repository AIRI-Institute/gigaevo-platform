from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path
from string import Template
from typing import Any, Dict, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    import pandas as pd


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _chain_uses_tool(chain_config: Dict[str, Any], tool_name: str) -> bool:
    """Return True if the chain has a TOOL step with step_config.tool_name == tool_name."""
    steps = chain_config.get("steps", []) if isinstance(chain_config, dict) else []
    for s in steps:
        if not isinstance(s, dict):
            continue
        stype = str(s.get("step_type", "LLM")).strip().upper()
        if stype != "TOOL":
            continue
        cfg = s.get("step_config") or {}
        if not isinstance(cfg, dict):
            continue
        if str(cfg.get("tool_name", "")).strip().lower() == tool_name.lower():
            return True
    return False


def _build_retrieval_docs_from_df(df: Any, target_column: str) -> list[str]:
    """Build a text corpus from the dataset for local retrieval indexing."""
    if df is None or len(df) == 0:
        return []

    cols = [c for c in df.columns if isinstance(c, str)]
    lower = {c.lower(): c for c in cols}

    preferred_cols: list[str] = []
    for name in ["context", "passage", "passages", "document", "documents", "text", "paragraph", "paragraphs"]:
        if name in lower:
            preferred_cols.append(lower[name])

    def row_to_text(row: Any, use_cols: list[str]) -> str:
        parts: list[str] = []
        for c in use_cols:
            try:
                v = row.get(c, "")
            except Exception:
                v = ""
            if v is None:
                continue
            s = str(v).strip()
            if s:
                parts.append(s if len(use_cols) == 1 else f"{c}: {s}")
        return "\n".join(parts).strip()

    # 1) Preferred context-like columns
    if preferred_cols:
        docs = [row_to_text(r, preferred_cols) for _, r in df.iterrows()]
        docs = [d for d in docs if d]
        if docs:
            return docs

    # 2) Non-target columns
    non_target = [c for c in cols if c != target_column]
    docs = [row_to_text(r, non_target) for _, r in df.iterrows()]
    docs = [d for d in docs if d]
    if docs:
        return docs

    # 3) Fallback: all columns (including target)
    docs = [row_to_text(r, cols) for _, r in df.iterrows()]
    return [d for d in docs if d]


def _prepare_local_retrieval_assets(
    *,
    exp_dir: Path,
    dataset_csv: Path,
    target_column: str,
    max_docs: int = 20000,
) -> None:
    """Create retrieval corpus file inside experiment folder (index is built at runtime).

    We intentionally generate the corpus at experiment creation time (master-api),
    but build the BM25 index at experiment start inside runner venv (context.py),
    so we don't need BM25 dependencies in master-api.
    """
    retrieval_dir = exp_dir / "retrieval"
    _ensure_dir(retrieval_dir)

    corpus_path = retrieval_dir / "corpus.jsonl"
    if corpus_path.exists():
        return

    try:
        import pandas as pd  # type: ignore

        df = pd.read_csv(dataset_csv).reset_index(drop=True)
    except Exception:
        return

    if max_docs and len(df) > max_docs:
        df = df.head(max_docs)

    docs = _build_retrieval_docs_from_df(df, target_column)
    if not docs:
        return

    with corpus_path.open("w", encoding="utf-8") as f:
        for text in docs:
            # JSONL compatible with simple loaders
            f.write(json.dumps({"text": text}, ensure_ascii=False) + "\n")


def _try_import_carl_schemas():
    """Try to import get_all_step_type_schemas from the installed mmar_carl package.

    The library is installed via pip/uv from GitHub (see pyproject.toml).
    Returns the function or None if the package is not available.
    """
    import logging

    logger = logging.getLogger(__name__)

    try:
        from mmar_carl import get_all_step_type_schemas

        logger.debug("Successfully imported CARL schemas from installed library")
        return get_all_step_type_schemas
    except ImportError:
        logger.warning("mmar_carl package is not installed; using fallback documentation")
        return None


def get_carl_types_documentation() -> str:
    """Build formatted documentation for all CARL step types.

    Uses the installed mmar_carl package when available, otherwise falls back
    to a static documentation string.

    Returns:
        Formatted documentation string describing all CARL step types.
    """
    try:
        get_schemas_func = _try_import_carl_schemas()
        if not get_schemas_func:
            return _get_fallback_documentation()

        schemas = get_schemas_func()
        if not schemas:
            return _get_fallback_documentation()

        doc_parts = []

        # Common fields for all steps
        doc_parts.append("""  REQUIRED FIELDS:
  
  - "steps" (REQUIRED): List of step objects defining the reasoning chain. Each step object MUST contain:
    
    * "number" (REQUIRED, int): Sequential step number starting from 1. Defines the execution order 
      of steps in the reasoning chain. Steps are executed in ascending order of their numbers.
    
    * "step_type" (OPTIONAL, str): Type of step. If omitted, defaults to "LLM". Valid values:""")

        # List available step types
        step_types_list = []
        for step_type in sorted(schemas.keys()):
            schema = schemas[step_type]
            step_types_list.append(f'      - "{step_type}": {schema.get("description", "")}')

        doc_parts.append("\n".join(step_types_list))
        doc_parts.append("""    
    * "title" (REQUIRED, str): Short descriptive name for the step (e.g., "Problem Understanding", 
      "Solution Computation", "Evaluate Result"). Used for identification and logging purposes.
    
    * "dependencies" (REQUIRED, list): List of step numbers (integers) that must be completed 
      before this step can execute. Empty list [] means the step has no dependencies and can 
      run immediately. Used to define the execution graph and ensure proper ordering.""")

        # Detailed documentation for each step type
        for step_type in sorted(schemas.keys()):
            schema = schemas[step_type]
            title = schema.get("title", step_type)
            description = schema.get("description", "")
            fields = schema.get("fields", {})

            doc_parts.append(
                f'\n    For {step_type} steps (step_type="{step_type}"), the following fields are REQUIRED:'
            )

            # Group fields by category
            common_fields = ["number", "title", "dependencies", "step_type"]
            llm_specific_fields = [
                "aim",
                "reasoning_questions",
                "step_context_queries",
                "stage_action",
                "example_reasoning",
            ]
            config_fields = {k: v for k, v in fields.items() if k.startswith("config.") or k.startswith("step_config")}
            other_fields = {
                k: v
                for k, v in fields.items()
                if k not in common_fields and k not in llm_specific_fields and not k.startswith("config.")
            }

            # LLM steps — special handling
            if step_type == "LLM":
                doc_parts.append("")
                for field_name in llm_specific_fields:
                    if field_name in fields:
                        field_desc = fields[field_name]
                        doc_parts.append(f'    * "{field_name}" (REQUIRED for LLM, str): {field_desc}')
            else:
                # Non-LLM steps — step_config is required
                doc_parts.append(
                    f'    * "step_config" (REQUIRED for {step_type}, dict): Configuration for the {title.lower()}. Must contain:'
                )

                # Add step_config fields
                for field_name, field_desc in sorted(config_fields.items()):
                    # Strip "config." prefix for readability
                    clean_name = field_name.replace("config.", "")
                    doc_parts.append(f'      - "{clean_name}" ({_infer_field_type(field_desc)}): {field_desc}')

                # Other type-specific fields
                for field_name, field_desc in sorted(other_fields.items()):
                    if field_name not in common_fields:
                        doc_parts.append(
                            f'    * "{field_name}" (REQUIRED for {step_type}, {_infer_field_type(field_desc)}): {field_desc}'
                        )

        doc_parts.append("""    
    NOTE: LLM-specific fields (aim, reasoning_questions, stage_action, example_reasoning, 
    step_context_queries) are NOT required for non-LLM step types (TOOL, TRANSFORM, etc.).""")

        return "\n".join(doc_parts)

    except ImportError:
        return _get_fallback_documentation()
    except Exception as e:
        import logging

        logger = logging.getLogger(__name__)
        logger.warning(f"Failed to load CARL types documentation: {e}, using fallback")
        return _get_fallback_documentation()


def _infer_field_type(description: str) -> str:
    """Infer a field type hint from its description text."""
    desc_lower = description.lower()
    if "list" in desc_lower or "array" in desc_lower:
        return "list"
    elif "dict" in desc_lower or "object" in desc_lower or "mapping" in desc_lower:
        return "dict"
    elif "int" in desc_lower or "integer" in desc_lower:
        return "int"
    elif "float" in desc_lower or "number" in desc_lower:
        return "float"
    elif "bool" in desc_lower or "boolean" in desc_lower:
        return "bool"
    else:
        return "str"


def get_carl_step_type_documentation(step_type: str) -> str:
    """Return formatted documentation for a specific CARL step type.

    Args:
        step_type: Step type name (LLM, TOOL, TRANSFORM, MEMORY, MCP, CONDITIONAL).

    Returns:
        Formatted documentation string for the requested step type.
    """
    try:
        get_schemas_func = _try_import_carl_schemas()
        if not get_schemas_func:
            return _get_fallback_step_type_documentation(step_type)

        schemas = get_schemas_func()
        if not schemas:
            return _get_fallback_step_type_documentation(step_type)

        step_type_upper = step_type.upper()
        if step_type_upper not in schemas:
            return _get_fallback_step_type_documentation(step_type)

        schema = schemas[step_type_upper]
        title = schema.get("title", step_type)
        description = schema.get("description", "")
        fields = schema.get("fields", {})

        doc_parts = [f"REQUIRED FIELDS FOR {step_type_upper} STEP:"]
        doc_parts.append("")

        # Common fields
        doc_parts.append('  * "number" (REQUIRED, int): Step number (cannot be changed during evolution)')
        doc_parts.append('  * "title" (REQUIRED, str): Short descriptive name for the step')
        doc_parts.append(
            '  * "dependencies" (REQUIRED, list): List of step numbers that must be completed before this step (must match base configuration)'
        )

        # Type-specific fields
        if step_type_upper == "LLM":
            llm_fields = ["aim", "reasoning_questions", "step_context_queries", "stage_action", "example_reasoning"]
            for field_name in llm_fields:
                if field_name in fields:
                    field_desc = fields[field_name]
                    doc_parts.append(f'  * "{field_name}" (REQUIRED, str): {field_desc}')
        else:
            # Non-LLM steps
            doc_parts.append(f'  * "step_type" (REQUIRED, str): Must be "{step_type_upper}"')
            doc_parts.append(
                f'  * "step_config" (REQUIRED, dict): Configuration for the {title.lower()}. Must contain:'
            )

            # step_config fields
            config_fields = {k: v for k, v in fields.items() if k.startswith("config.")}
            for field_name, field_desc in sorted(config_fields.items()):
                clean_name = field_name.replace("config.", "")
                doc_parts.append(f'    - "{clean_name}": {field_desc}')

        return "\n".join(doc_parts)

    except Exception as e:
        import logging

        logger = logging.getLogger(__name__)
        logger.warning(f"Failed to load CARL step type documentation for {step_type}: {e}, using fallback")
        return _get_fallback_step_type_documentation(step_type)


def _get_fallback_step_type_documentation(step_type: str) -> str:
    """Return static documentation for a step type when mmar_carl is not installed."""
    step_type_upper = step_type.upper()

    if step_type_upper == "LLM":
        return """REQUIRED FIELDS FOR LLM STEP:
  
  * "number" (REQUIRED, int): Step number (cannot be changed during evolution)
  
  * "title" (REQUIRED, str): Short descriptive name for the step. Can be improved.
  
  * "aim" (REQUIRED, str): Clear statement of what this step should accomplish. Can be refined.
  
  * "reasoning_questions" (REQUIRED, str): Questions that guide the LLM's reasoning process. Can be improved.
  
  * "dependencies" (REQUIRED, list): List of step numbers that must be completed before this step. MUST match the base configuration.
  
  * "step_context_queries" (REQUIRED, list): List of strings specifying what context information should be retrieved. Can be optimized.
  
  * "stage_action" (REQUIRED, str): Detailed description of the action to be performed. Can be enhanced.
  
  * "example_reasoning" (REQUIRED, str): Example or template of how reasoning should proceed. Can be improved."""
    else:
        return f"""REQUIRED FIELDS FOR {step_type_upper} STEP:
  
  * "number" (REQUIRED, int): Step number (cannot be changed during evolution)
  
  * "title" (REQUIRED, str): Short descriptive name for the step
  
  * "step_type" (REQUIRED, str): Must be "{step_type_upper}"
  
  * "dependencies" (REQUIRED, list): List of step numbers that must be completed before this step (must match base configuration)
  
  * "step_config" (REQUIRED, dict): Configuration for the {step_type_upper.lower()} step. See CARL documentation for specific requirements."""


def _detect_fitness_mode(chain_config: Dict[str, Any]) -> str:
    """Auto-detect fitness mode from chain configuration.

    Analyzes chain steps to determine the best fitness computation strategy:
    - 'iou': if TOOL steps reference IoU-related tools (e.g., segmentation evaluators)
    - 'accuracy': for pure LLM reasoning chains (text accuracy)
    - 'auto': when mixed or unable to determine

    Returns:
        Fitness mode string: 'iou', 'accuracy', 'tool_numeric', or 'auto'
    """
    steps = chain_config.get("steps", [])
    if not steps:
        return "auto"

    tool_names: list[str] = []
    step_types: set[str] = set()

    for step in steps:
        if not isinstance(step, dict):
            continue
        stype = str(step.get("step_type", "LLM")).upper()
        step_types.add(stype)

        cfg = step.get("step_config", {}) or {}
        if stype == "TOOL" and cfg.get("tool_name"):
            tool_names.append(str(cfg["tool_name"]).lower())

    # IoU-related tools
    iou_indicators = {"eval_sam", "segmentation", "iou"}
    if any(any(ind in tn for ind in iou_indicators) for tn in tool_names):
        return "iou"

    # If there are TOOL steps but we don't know the metric type
    if "TOOL" in step_types and tool_names:
        return "auto"  # auto will try tool_numeric extraction

    # Pure LLM chains
    if step_types <= {"LLM", "TRANSFORM", "MEMORY", "CONDITIONAL"}:
        return "accuracy"

    return "auto"


def _generate_available_tools_documentation(chain_config: Dict[str, Any]) -> str:
    """Generate documentation about available tools used in the chain.

    Scans TOOL steps in the chain config and produces a description of each tool,
    its parameters, and how it's connected in the chain.

    Returns:
        Formatted documentation string, or empty string if no TOOL steps exist.
    """
    steps = chain_config.get("steps", [])
    if not steps:
        return ""

    tool_docs: list[str] = []

    for step in steps:
        if not isinstance(step, dict):
            continue
        if str(step.get("step_type", "")).upper() != "TOOL":
            continue

        cfg = step.get("step_config", {}) or {}
        tool_name = cfg.get("tool_name", "")
        tool_desc = cfg.get("tool_description", "")
        input_mapping = cfg.get("input_mapping", {})
        timeout = cfg.get("timeout", 120)
        step_num = step.get("number", "?")

        doc = f'  - "{tool_name}" (used in step {step_num})'
        if tool_desc:
            doc += f": {tool_desc}"
        if input_mapping:
            params = ", ".join(f"{k}={v}" for k, v in input_mapping.items())
            doc += f"\n    Input parameters: {params}"
        doc += f"\n    Timeout: {timeout}s"
        tool_docs.append(doc)

    if not tool_docs:
        return ""

    return "AVAILABLE TOOLS IN THIS CHAIN:\n" + "\n".join(tool_docs) + "\n"


def _generate_task_specific_considerations(
    chain_config: Dict[str, Any],
    fitness_mode: str,
    description: str = "",
) -> str:
    """Generate task-specific considerations based on chain configuration.

    Dynamically produces context-appropriate guidance for the evolution LLM,
    based on the step types, tools, and fitness mode in the chain.

    Returns:
        Formatted considerations string (may be empty for generic tasks).
    """
    steps = chain_config.get("steps", [])
    if not steps:
        return ""

    parts: list[str] = ["TASK-SPECIFIC CONSIDERATIONS:"]

    # Collect step types and tools
    step_types: dict[str, list[int]] = {}  # type -> list of step numbers
    tool_configs: list[Dict[str, Any]] = []

    for step in steps:
        if not isinstance(step, dict):
            continue
        stype = str(step.get("step_type", "LLM")).upper()
        num = step.get("number", 0)
        step_types.setdefault(stype, []).append(num)

        if stype == "TOOL":
            cfg = step.get("step_config", {}) or {}
            tool_configs.append(
                {
                    "number": num,
                    "tool_name": cfg.get("tool_name", ""),
                    "frozen": step.get("frozen", False),
                }
            )

    # IoU / segmentation tasks
    if fitness_mode == "iou":
        iou_tools = [
            tc
            for tc in tool_configs
            if any(kw in str(tc.get("tool_name", "")).lower() for kw in ["sam", "segment", "iou", "eval"])
        ]
        if iou_tools:
            tool_info = iou_tools[0]
            parts.append(
                f"- This is a segmentation task. Fitness is computed from IoU scores "
                f"returned by the '{tool_info['tool_name']}' tool (step {tool_info['number']})."
            )
            parts.append("- Higher IoU means better segmentation quality → higher fitness.")
        parts.append(
            '- The TOOL step returns a JSON object with "iou" (float 0.0-1.0) and possibly '
            '"pred_mask_path" (path to predicted mask).'
        )

    # Multi-step LLM reasoning chains
    llm_steps = step_types.get("LLM", [])
    if len(llm_steps) > 1:
        parts.append(
            f"- This chain uses {len(llm_steps)} LLM reasoning steps. "
            "Consider how they build on each other for optimal results."
        )

    # Frozen step guidance
    frozen_nums = [s.get("number") for s in steps if isinstance(s, dict) and s.get("frozen")]
    non_frozen_nums = [s.get("number") for s in steps if isinstance(s, dict) and not s.get("frozen")]
    if frozen_nums:
        parts.append(
            f"- Steps {frozen_nums} are frozen and cannot be modified. "
            f"Focus your evolution efforts on steps {non_frozen_nums}."
        )

    # TRANSFORM step guidance
    if "TRANSFORM" in step_types:
        parts.append(
            "- TRANSFORM steps perform data transformations without LLM calls "
            "(e.g., setting thresholds, formatting data). They are typically frozen "
            "but can be evolved if not frozen."
        )

    # STRUCTURED_OUTPUT step guidance
    if "STRUCTURED_OUTPUT" in step_types:
        parts.append(
            "- STRUCTURED_OUTPUT steps generate schema-constrained JSON output from the LLM. "
            "The output_schema field defines the JSON Schema that the model output must match."
        )

    # MCP step guidance
    if "MCP" in step_types:
        parts.append(
            "- MCP steps call tools on external MCP (Model Context Protocol) servers. "
            "They enable integration with external services through a standardized protocol."
        )

    # MEMORY step guidance
    if "MEMORY" in step_types:
        parts.append(
            "- MEMORY steps store/retrieve information from shared memory across the chain. "
            "They enable steps to share state and persist intermediate results."
        )

    # CONDITIONAL step guidance
    if "CONDITIONAL" in step_types:
        parts.append(
            "- CONDITIONAL steps enable branching logic — different paths are taken "
            "based on conditions evaluated against previous step results."
        )

    # If description contains relevant keywords, add them as context
    if description:
        parts.append(f"- Task description: {description}")

    if len(parts) <= 1:
        return ""  # Only header, no specific considerations

    return "\n".join(parts) + "\n"


def _get_fitness_description(fitness_mode: str, validation_type: str) -> str:
    """Get human-readable fitness description for the task_description template."""
    if fitness_mode == "iou":
        return "IoU (Intersection over Union) score from TOOL step evaluation"
    elif fitness_mode == "accuracy":
        if validation_type == "Binary (0/1)":
            return "Accuracy of predicted answers on the evaluation dataset"
        else:
            return "Continuous metric score on the evaluation dataset"
    elif fitness_mode == "tool_numeric":
        return "Numeric score extracted from TOOL step results"
    else:
        return "Automatically detected from chain output (IoU, accuracy, or tool metric)"


def _get_fitness_goal(fitness_mode: str) -> str:
    """Get the fitness goal description."""
    if fitness_mode == "iou":
        return "Maximize mean IoU score (0.0 to 1.0)"
    elif fitness_mode == "accuracy":
        return "Maximize the measured Accuracy"
    elif fitness_mode == "tool_numeric":
        return "Maximize the numeric fitness score from TOOL evaluation"
    else:
        return "Maximize the fitness metric"


def _get_fitness_evaluation_description(fitness_mode: str) -> str:
    """Get fitness evaluation framework description."""
    if fitness_mode == "iou":
        return (
            "Fitness = mean IoU on the train split; val_fitness = mean IoU on the validation split. "
            'IoU is extracted from TOOL step results (JSON field "iou").'
        )
    elif fitness_mode == "accuracy":
        return "Fitness = Accuracy on the train split; val_fitness = Accuracy on the validation split."
    elif fitness_mode == "tool_numeric":
        return (
            "Fitness = mean numeric score from TOOL results on train split; "
            "val_fitness = same metric on validation split."
        )
    else:
        return (
            "Fitness is computed automatically: IoU from TOOL results if present, "
            "otherwise text accuracy on train/validation splits."
        )


def _get_fallback_documentation() -> str:
    """Return static documentation when mmar_carl is not installed."""
    return """  REQUIRED FIELDS:
  
  - "steps" (REQUIRED): List of step objects defining the reasoning chain. Each step object MUST contain:
    
    * "number" (REQUIRED, int): Sequential step number starting from 1. Defines the execution order 
      of steps in the reasoning chain. Steps are executed in ascending order of their numbers.
    
    * "step_type" (OPTIONAL, str): Type of step. If omitted, defaults to "LLM". Valid values:
      - "LLM": Uses a Large Language Model for reasoning (default)
      - "TOOL": Executes an external tool/function (e.g., API calls, computations)
      - "TRANSFORM": Performs data transformations without LLM calls
      - "MEMORY": Stores/retrieves information from memory
      - "MCP": Model Context Protocol step for structured data exchange
      - "CONDITIONAL": Conditional branching based on previous step results
    
    * "title" (REQUIRED, str): Short descriptive name for the step (e.g., "Problem Understanding", 
      "Solution Computation", "Evaluate Result"). Used for identification and logging purposes.
    
    * "dependencies" (REQUIRED, list): List of step numbers (integers) that must be completed 
      before this step can execute. Empty list [] means the step has no dependencies and can 
      run immediately. Used to define the execution graph and ensure proper ordering.
    
    For LLM steps (step_type="LLM" or omitted), the following fields are REQUIRED:
    
    * "aim" (REQUIRED for LLM, str): Clear statement of what this step should accomplish. Describes the 
      goal or objective of the step in the context of solving the problem. Guides the LLM's 
      reasoning direction.
    
    * "reasoning_questions" (REQUIRED for LLM, str): Questions that guide the LLM's reasoning process 
      at this step. Should help break down the problem and focus on relevant aspects. Can be 
      a single question or multiple questions separated by newlines or semicolons.
    
    * "step_context_queries" (REQUIRED for LLM, list): List of strings specifying what context information 
      should be retrieved for this step. Defines what information is available to the LLM during 
      step execution. Common values: ["problem"], ["@1.output"], ["@context.field_name"].
    
    * "stage_action" (REQUIRED for LLM, str): Detailed description of the action to be performed at this 
      step. Provides specific instructions to the LLM about what operations, calculations, or 
      reasoning processes should be carried out. More detailed than "aim" and guides the 
      actual execution.
    
    * "example_reasoning" (REQUIRED for LLM, str): Example or template of how reasoning should proceed 
      at this step. Serves as a demonstration or pattern for the LLM to follow. Helps establish 
      the expected reasoning style and format.
    
    For TOOL steps (step_type="TOOL"), the following fields are REQUIRED:
    
    * "step_config" (REQUIRED for TOOL, dict): Configuration for the tool execution. Must contain:
      - "tool_name" (str): Name of the registered tool (e.g., "my_evaluator")
      - "tool_description" (str): Brief description of what the tool does
      - "input_mapping" (dict): Maps tool arguments to context variables. Keys are tool parameter names,
        values are references like "@1.output" (output from step 1), "@context.field_name" (from context),
        or literal values. Example: {"prompt_text": "@1.output", "threshold": "@2.output", 
        "image_path": "@context.image_path"}
      - "timeout" (int, optional): Maximum execution time in seconds (default: 120)
    
    For TRANSFORM steps (step_type="TRANSFORM"), the following fields are REQUIRED:
    
    * "step_config" (REQUIRED for TRANSFORM, dict): Configuration for the transformation. Must contain:
      - "transform_type" (str): Type of transformation (e.g., "python_expr", "extract", "format", 
        "aggregate", "filter", "map")
      - "expression" (str): Expression or code to execute (for "python_expr")
      - "output_key" (str): Key name for the output value
      Example: {"transform_type": "python_expr", "expression": "0.3", "output_key": "threshold"}
    
    For MEMORY, MCP, and CONDITIONAL steps, see CARL documentation for step_config requirements.
    
    NOTE: LLM-specific fields (aim, reasoning_questions, stage_action, example_reasoning, 
    step_context_queries) are NOT required for non-LLM step types (TOOL, TRANSFORM, etc.)."""


def build_chain_experiment(
    *,
    spec: Dict[str, Any],
    output_root: str | Path,
    template_base: str | Path,
    dataset_path: str | Path,
    experiment_id: Optional[str] = None,
) -> Path:
    output_root = Path(output_root)
    template_base = Path(template_base)
    template_dir = template_base / "chain"
    if not template_dir.exists():
        if template_base.exists():
            template_dir = template_base
        else:
            raise FileNotFoundError(f"Chain template directory not found: {template_dir}")

    exp_suffix = "chain"
    exp_uuid = experiment_id or f"chain_exp_{uuid.uuid4().hex}"
    exp_dir = output_root / f"{exp_uuid}_{exp_suffix}"
    _ensure_dir(exp_dir)

    dataset_dir = exp_dir / "dataset"
    _ensure_dir(dataset_dir)

    dataset_target = dataset_dir / "data.csv"
    shutil.copy(Path(dataset_path), dataset_target)

    target_column: str = spec.get("target_column") or ""
    name: str = spec.get("name") or exp_uuid
    description: str = spec.get("description") or ""
    base_chain_config: str = spec.get("base_chain_config") or "{}"
    llm_model: str = spec.get("llm_model") or "local-inference"
    max_iterations: int = int(spec.get("max_iterations") or 100)
    validation_criteria: Dict[str, Any] = spec.get("validation_criteria") or {}
    evolution_mode: str = spec.get("evolution_mode", "full_chain") or "full_chain"
    step_number: Optional[int] = spec.get("step_number")
    # Normalize frozen steps (used both in chain_config.json and baseline enforcement)
    raw_frozen_steps = spec.get("frozen_steps") or []
    frozen_steps: list[int] = []
    for n in raw_frozen_steps:
        try:
            frozen_steps.append(int(n))
        except (TypeError, ValueError):
            continue

    def _clean_step_for_frozen(step: Dict[str, Any], is_frozen: bool) -> Dict[str, Any]:
        """Clean step: remove empty LLM fields for non-LLM steps, add frozen flag."""
        step_type = step.get("step_type", "LLM")
        cleaned = step.copy()

        # Remove empty LLM-specific fields for non-LLM steps to shorten the chain
        if step_type != "LLM":
            for field in ["aim", "reasoning_questions", "stage_action", "example_reasoning", "step_context_queries"]:
                if field in cleaned and (not cleaned[field] or cleaned[field] == "" or cleaned[field] == []):
                    del cleaned[field]

        # Add frozen flag if step is frozen
        if is_frozen:
            cleaned["frozen"] = True

        return cleaned

    try:
        chain_config = json.loads(base_chain_config)
        steps = chain_config.get("steps", [])

        # Clean steps: add frozen flag and remove empty fields
        frozen_steps_set = set(frozen_steps)
        cleaned_steps = [_clean_step_for_frozen(step, step.get("number") in frozen_steps_set) for step in steps]
        chain_config["steps"] = cleaned_steps
        base_chain_config = json.dumps(chain_config, indent=2, ensure_ascii=False)

        # If chain uses local retrieval tool, generate retrieval corpus now (like core).
        # The BM25 index will be built inside runner at experiment start (context.py),
        # using the corpus in exp_dir/retrieval/corpus.jsonl.
        if _chain_uses_tool(chain_config, "retrieve"):
            _prepare_local_retrieval_assets(
                exp_dir=exp_dir,
                dataset_csv=dataset_target,
                target_column=target_column,
            )

        num_steps = len(steps)
        step_titles = [step.get("title", f"Step {i + 1}") for i, step in enumerate(steps)]
        step_titles_text = (
            "\n".join([f"  - {title}" for title in step_titles]) if step_titles else "  - (no steps defined)"
        )

        target_step = None
        target_step_info = ""
        if evolution_mode == "single_step" and step_number is not None:
            step_idx = int(step_number) - 1
            if 0 <= step_idx < len(steps):
                target_step = steps[step_idx]
                target_step_info = f"Step {step_number}: {target_step.get('title', 'Unknown')}"
    except Exception:
        num_steps = 0
        step_titles_text = "  - (invalid chain config)"
        target_step = None
        target_step_info = ""

    validation_type = validation_criteria.get("validation_type") or "Binary (0/1)"
    binary_method = validation_criteria.get("binary_method") or "equality"
    continuous_metric = validation_criteria.get("continuous_metric") or "ROUGE-L"
    regexp_pattern = validation_criteria.get("regexp_pattern") or ""

    if not regexp_pattern:
        regexp_pattern = r"Answer:\s*(.+?)$"

    if validation_type == "Binary (0/1)":
        metric: str = "exact_match" if binary_method == "equality" else binary_method
    else:
        metric = continuous_metric

    carl_types_documentation = get_carl_types_documentation()

    step_type_documentation = ""
    if evolution_mode == "single_step" and target_step:
        step_type = target_step.get("step_type", "LLM") or "LLM"
        step_type_documentation = get_carl_step_type_documentation(step_type)

    # --- Dynamic placeholders based on chain content ---
    try:
        parsed_chain = json.loads(base_chain_config)
    except Exception:
        parsed_chain = {"steps": []}

    fitness_mode = _detect_fitness_mode(parsed_chain)
    available_tools_documentation = _generate_available_tools_documentation(parsed_chain)
    task_specific_considerations = _generate_task_specific_considerations(
        parsed_chain,
        fitness_mode,
        description,
    )
    fitness_description = _get_fitness_description(fitness_mode, validation_type)
    fitness_goal = _get_fitness_goal(fitness_mode)
    fitness_evaluation_description = _get_fitness_evaluation_description(fitness_mode)

    placeholders: Dict[str, Any] = {
        "experiment_id": exp_uuid,
        "task_name": exp_uuid,
        "name": name,
        "description": description,
        "target_field": target_column,
        "dataset_abs_path": str(dataset_target.resolve()),
        "base_chain_config": base_chain_config,
        "llm_model": llm_model,
        "max_iterations": str(max_iterations),
        "num_steps": str(num_steps),
        "step_titles": step_titles_text,
        "validation_type": validation_type,
        "metric": metric,
        "regexp_pattern": regexp_pattern,
        "evolution_mode": evolution_mode,
        "step_number": str(step_number) if step_number else "",
        "target_step_info": target_step_info,
        "target_step_json": json.dumps(target_step, indent=2) if target_step else "{}",
        "carl_types_documentation": carl_types_documentation,
        "step_type_documentation": step_type_documentation,
        # New dynamic placeholders
        "fitness_mode": fitness_mode,
        "fitness_description": fitness_description,
        "fitness_goal": fitness_goal,
        "fitness_evaluation_description": fitness_evaluation_description,
        "available_tools_documentation": available_tools_documentation,
        "task_specific_considerations": task_specific_considerations,
    }

    if evolution_mode == "single_step" and step_number:
        task_description_file = "task_description_step.txt"
    else:
        task_description_file = "task_description.txt"

    required_template_files = [
        task_description_file,
        "validate.py",
        "context.py",
        "helper.py",
        "chain_types.py",
        "chain_client.py",
        "chain_runner.py",
        "chain_validation.py",
        "metrics.yaml",
    ]

    # Optional template files that are copied verbatim (no placeholder substitution)
    optional_template_files = [
        "feedback_integration.py",
    ]

    for filename in required_template_files:
        src = template_dir / filename
        if not src.exists():
            raise FileNotFoundError(f"Template not found: {src}")
        content = src.read_text()
        rendered = Template(content).safe_substitute(placeholders)
        output_filename = "task_description.txt" if filename == task_description_file else filename
        (exp_dir / output_filename).write_text(rendered)

    for filename in optional_template_files:
        src = template_dir / filename
        if src.exists():
            shutil.copy(src, exp_dir / filename)

    # Copy chain_feedback_service.py so feedback_integration.py can import it
    _services_dir = Path(__file__).resolve().parent.parent / "services"
    _cfs = _services_dir / "chain_feedback_service.py"
    if _cfs.exists():
        shutil.copy(_cfs, exp_dir / "chain_feedback_service.py")

    (exp_dir / "base_chain_config.json").write_text(base_chain_config)
    chain_llm_model: Optional[str] = spec.get("chain_llm_model") or None
    chain_config_dict = {
        "experiment_id": exp_uuid,
        "name": name,
        "description": description,
        "target_column": target_column,
        "dataset_path": "dataset/data.csv",
        "llm_model": llm_model,
        "max_iterations": max_iterations,
    }
    if chain_llm_model:
        chain_config_dict["chain_llm_model"] = chain_llm_model
    # Optional evolution constraints
    if frozen_steps:
        chain_config_dict["frozen_steps"] = frozen_steps
    chain_size_limit = spec.get("chain_size_limit")
    if chain_size_limit:
        chain_config_dict["chain_size_limit"] = int(chain_size_limit)
    # Enable feedback by default for CARL chains so the mutation LLM receives
    # performance context through the DAG pipeline (FetchArtifact → FormatterStage → MutationContextStage)
    chain_config_dict["enable_feedback"] = spec.get("enable_feedback", True)
    chain_config_dict["feedback_template"] = spec.get("feedback_template", "detailed")
    (exp_dir / "chain_config.json").write_text(json.dumps(chain_config_dict, indent=2, ensure_ascii=False))

    # Write chain_spec.json for feedback_integration.py to read at validation time
    chain_spec = {
        "enable_feedback": chain_config_dict["enable_feedback"],
        "feedback_template": chain_config_dict["feedback_template"],
        "target_column": target_column,
        "evolution_mode": evolution_mode,
    }
    (exp_dir / "chain_spec.json").write_text(json.dumps(chain_spec, indent=2, ensure_ascii=False))

    initial_programs_dir = exp_dir / "initial_programs"
    _ensure_dir(initial_programs_dir)

    # NOTE: base_chain_config is JSON text. When embedding into a Python triple-quoted string,
    # we must escape backslashes so sequences like "\n" remain valid JSON escapes.
    # Otherwise Python turns them into literal newlines, which breaks json.loads().
    escaped_config = base_chain_config.replace("\\", "\\\\").replace('"""', '\\"\\"\\"').replace("'''", "\\'\\'\\'")

    # -- Robust JSON parsing helper (lives OUTSIDE the evolve block) --
    # Defined as a constant string to avoid f-string escape issues with regex.
    # Uses chr(92) for backslash to avoid multi-level escaping problems.
    _SAFE_LOADS_PREAMBLE = (
        "import json\n"
        "\n"
        "def _safe_json_loads(raw: str):\n"
        '    """Parse JSON string, auto-fixing invalid backslash escapes and control characters from LLM mutations."""\n'
        "    import json as _json\n"
        "    try:\n"
        "        return _json.loads(raw)\n"
        "    except _json.JSONDecodeError:\n"
        "        # Fix 1: Replace literal newlines and other control characters inside JSON strings\n"
        '        # This handles cases where LLM writes: "text\\nwith\\nliteral\\nnewlines"\n'
        '        # We need to replace them with escaped versions: "text\\\\nwith\\\\nliteral\\\\nnewlines"\n'
        "        def _fix_control_chars(json_str: str) -> str:\n"
        "            result = []\n"
        "            i = 0\n"
        "            in_string = False\n"
        "            string_char = None\n"
        "            escape = False\n"
        "            \n"
        "            while i < len(json_str):\n"
        "                char = json_str[i]\n"
        "                \n"
        "                if not in_string:\n"
        "                    if char in ('\"', \"'\"):\n"
        "                        in_string = True\n"
        "                        string_char = char\n"
        "                        result.append(char)\n"
        "                    else:\n"
        "                        result.append(char)\n"
        "                else:\n"
        "                    if escape:\n"
        "                        result.append(char)\n"
        "                        escape = False\n"
        "                    elif char == '\\\\':\n"
        "                        result.append(char)\n"
        "                        escape = True\n"
        "                    elif char == string_char:\n"
        "                        result.append(char)\n"
        "                        in_string = False\n"
        "                        string_char = None\n"
        "                    elif ord(char) < 32:\n"
        "                        # Control character inside JSON string\n"
        "                        if char == '\\n':\n"
        "                            result.append('\\\\\\\\n')\n"
        "                        elif char == '\\r':\n"
        "                            result.append('\\\\\\\\r')\n"
        "                        elif char == '\\t':\n"
        "                            result.append('\\\\\\\\t')\n"
        "                        # Skip other control characters\n"
        "                    else:\n"
        "                        result.append(char)\n"
        "                \n"
        "                i += 1\n"
        "            \n"
        "            return ''.join(result)\n"
        "        \n"
        "        # Try fixing control characters first\n"
        "        try:\n"
        "            fixed = _fix_control_chars(raw)\n"
        "            return _json.loads(fixed)\n"
        "        except _json.JSONDecodeError:\n"
        "            pass\n"
        "        \n"
        "        # Fix 2: Handle invalid backslash escapes\n"
        "        _BS = chr(92)\n"
        "        _VALID_AFTER = set('\"\\\\/bfnrtu')\n"
        "        out = []\n"
        "        i = 0\n"
        "        while i < len(raw):\n"
        "            if raw[i] == _BS and i + 1 < len(raw) and raw[i + 1] not in _VALID_AFTER:\n"
        "                out.append(_BS)\n"
        "                out.append(_BS)\n"
        "            else:\n"
        "                out.append(raw[i])\n"
        "            i += 1\n"
        "        try:\n"
        "            fixed = ''.join(out)\n"
        "            fixed = _fix_control_chars(fixed)\n"
        "            return _json.loads(fixed)\n"
        "        except _json.JSONDecodeError:\n"
        "            # Fix 3: Last resort - remove invalid backslash escapes entirely\n"
        "            out2 = []\n"
        "            i = 0\n"
        "            while i < len(raw):\n"
        "                if raw[i] == _BS and i + 1 < len(raw) and raw[i + 1] not in _VALID_AFTER:\n"
        "                    i += 1\n"
        "                    continue\n"
        "                out2.append(raw[i])\n"
        "                i += 1\n"
        "            fixed = ''.join(out2)\n"
        "            fixed = _fix_control_chars(fixed)\n"
        "            return _json.loads(fixed)\n"
        "\n"
    )

    if evolution_mode == "single_step" and step_number and target_step:
        # Check if the step is frozen - if so, do not evolve it
        is_step_frozen = step_number in frozen_steps or target_step.get("frozen", False)

        if is_step_frozen:
            # Step is frozen - return base config unchanged (no evolution)
            baseline_content = (
                _SAFE_LOADS_PREAMBLE
                + f'''
BASE_CHAIN_CONFIG: str = """{escaped_config}""".strip()

# EVOLVE-BLOCK-START

# IMPORTANT: Step {step_number} is FROZEN and MUST NOT be evolved.
# Return the base chain configuration unchanged.
import json as _json
CHAIN_CONFIG_JSON: str = BASE_CHAIN_CONFIG

# EVOLVE-BLOCK-END

def entrypoint(context=None):
    return {{"chain_config_json": CHAIN_CONFIG_JSON}}
'''
            )
        else:
            # Step is not frozen - proceed with evolution
            target_step_escaped = (
                json.dumps(target_step, indent=2).replace('"""', '\\"\\"\\"').replace("'''", "\\'\\'\\'")
            )
            baseline_content = (
                _SAFE_LOADS_PREAMBLE
                + f'''
BASE_CHAIN_CONFIG: str = """{escaped_config}""".strip()

# EVOLVE-BLOCK-START

# You must evolve ONLY step {step_number} while keeping all other steps unchanged.
# Parse the base config, modify only step {step_number}, and return the complete chain.
import json as _json
_base = _safe_json_loads(BASE_CHAIN_CONFIG)
_steps = _base.get("steps", [])
# Keep all steps except step {step_number}
_other_steps = [_s for _s in _steps if _s.get("number") != {step_number}]
# Evolve step {step_number} here - modify the step configuration to improve performance
_target_step = {target_step_escaped}
# TODO: Evolve _target_step fields (title, aim, reasoning_questions, stage_action, example_reasoning, step_context_queries)
# but keep number={step_number} and dependencies unchanged
_evolved_step = _target_step.copy()
# Reconstruct chain with evolved step
_evolved_steps = sorted(_other_steps + [_evolved_step], key=lambda s: s.get("number", 0))
_evolved_chain = {{"steps": _evolved_steps}}
if "search_config" in _base:
    _evolved_chain["search_config"] = _base["search_config"]
if "max_workers" in _base:
    _evolved_chain["max_workers"] = _base["max_workers"]
if "enable_progress" in _base:
    _evolved_chain["enable_progress"] = _base["enable_progress"]

CHAIN_CONFIG_JSON: str = _json.dumps(_evolved_chain, indent=2)

# EVOLVE-BLOCK-END

def entrypoint(context=None):
    return {{"chain_config_json": CHAIN_CONFIG_JSON}}
'''
            )
    else:
        # Full-chain evolution. If frozen steps are provided, enforce them after evolution
        if frozen_steps:
            frozen_steps_str = ", ".join(str(n) for n in sorted(set(frozen_steps)))
            baseline_content = (
                _SAFE_LOADS_PREAMBLE
                + f'''
BASE_CHAIN_CONFIG: str = """{escaped_config}""".strip()

# EVOLVE-BLOCK-START

# You may evolve the full CARL chain configuration in CHAIN_CONFIG_JSON.
# IMPORTANT: Steps with "frozen": true (numbers in FROZEN_STEPS) MUST NOT be modified.
# They will be FORCE-RESTORED from BASE_CHAIN_CONFIG after evolution.
# Do NOT renumber frozen steps — their number is their identity.
# Add new steps with numbers that do NOT collide with frozen step numbers.
# Only evolve steps without "frozen": true.
import json as _json
CHAIN_CONFIG_JSON: str = _json.dumps(_safe_json_loads(BASE_CHAIN_CONFIG), indent=2, ensure_ascii=False)

# EVOLVE-BLOCK-END

FROZEN_STEPS = [{frozen_steps_str}]

def _apply_frozen_steps(chain_json: str) -> str:
    """Strictly enforce frozen steps from BASE_CHAIN_CONFIG.

    Frozen steps are matched by their ORIGINAL step number from the base config.
    If a mutation renumbers, modifies, or removes a frozen step, the original
    frozen step is restored at its canonical position.  Any evolved step whose
    number collides with a frozen step number is dropped.
    """
    try:
        base = _safe_json_loads(BASE_CHAIN_CONFIG)
        chain = _safe_json_loads(chain_json)
    except Exception:
        return BASE_CHAIN_CONFIG

    base_steps_by_num = {{s.get("number"): s for s in base.get("steps", []) if isinstance(s, dict)}}

    frozen_originals = {{}}
    for num in FROZEN_STEPS:
        if num in base_steps_by_num:
            step_copy = base_steps_by_num[num].copy()
            step_copy.pop("frozen", None)
            frozen_originals[num] = step_copy

    evolved_steps = []
    seen_frozen = set()
    for s in chain.get("steps", []):
        num = s.get("number")
        if num in frozen_originals:
            evolved_steps.append(frozen_originals[num])
            seen_frozen.add(num)
        else:
            evolved_step = s.copy()
            evolved_step.pop("frozen", None)
            evolved_steps.append(evolved_step)

    for num, orig in frozen_originals.items():
        if num not in seen_frozen:
            evolved_steps.append(orig)

    evolved_steps.sort(key=lambda x: x.get("number", 9999))

    for s in evolved_steps:
        num = s.get("number")
        if num in frozen_originals:
            deps = frozen_originals[num].get("dependencies", [])
            s["dependencies"] = deps
            if "step_context_queries" in frozen_originals[num]:
                s["step_context_queries"] = frozen_originals[num]["step_context_queries"]

    chain["steps"] = evolved_steps
    return _json.dumps(chain, indent=2, ensure_ascii=False)

CHAIN_CONFIG_JSON = _apply_frozen_steps(CHAIN_CONFIG_JSON)

def entrypoint(context=None):
    return {{"chain_config_json": CHAIN_CONFIG_JSON}}
'''
            )
        else:
            baseline_content = (
                _SAFE_LOADS_PREAMBLE
                + f'''
BASE_CHAIN_CONFIG: str = """{escaped_config}""".strip()

# EVOLVE-BLOCK-START

import json as _json
CHAIN_CONFIG_JSON: str = _json.dumps(_safe_json_loads(BASE_CHAIN_CONFIG), indent=2, ensure_ascii=False)

# EVOLVE-BLOCK-END

def entrypoint(context=None):
    return {{"chain_config_json": CHAIN_CONFIG_JSON}}
'''
            )
    (initial_programs_dir / "baseline.py").write_text(baseline_content)

    return exp_dir
