# import json
# import os
# from typing import Optional, Dict, Any, List

# import gradio as gr
# from loguru import logger

# from .base import BaseComponent
# from common.llm_registry import get_default_llm_model_id, get_llm_model_choices


# CARL_STEP_TYPES = [
#     "LLM",
#     "TOOL",
#     "MCP",
#     "MEMORY",
#     "TRANSFORM",
#     "CONDITIONAL",
#     "STRUCTURED_OUTPUT",
# ]

# # Fallback step type hints (used when CARL library is unavailable)
# STEP_TYPE_HINTS = {
#     "LLM": {
#         "title": "LLM Step",
#         "description": "Generates text, prompts, or responses using a language model.",
#         "fields": {
#             "aim": "Clear statement of what this step should accomplish.",
#             "reasoning_questions": "Questions that guide the LLM's reasoning process.",
#             "stage_action": "Detailed description of the action to be performed.",
#             "example_reasoning": "Example or template of how reasoning should proceed.",
#         },
#     },
#     "TOOL": {
#         "title": "TOOL Step",
#         "description": "Calls an external tool or function registered in the ReasoningContext.",
#         "fields": {
#             "tool_name": "Name of the tool to call.",
#             "tool_description": "Description of what the tool does.",
#             "input_mapping": 'JSON object mapping input parameters. Use @N.output or @context.field.',
#             "timeout": "Timeout in seconds for tool execution. Default is 120.",
#         },
#     },
#     "TRANSFORM": {
#         "title": "TRANSFORM Step",
#         "description": "Transforms output data from previous steps using expressions.",
#         "fields": {
#             "transform_type": "Type of transformation: python_expr, jmespath, extract, etc.",
#             "expression": "Expression to evaluate.",
#             "output_key": "Key name to store the result.",
#         },
#     },
#     "MEMORY": {
#         "title": "MEMORY Step",
#         "description": "Reads from or writes to shared memory for use across steps.",
#         "fields": {
#             "operation": "Operation type: read or write.",
#             "namespace": "Namespace for organizing memory data.",
#             "key": "Key identifier for the memory entry.",
#             "value_expr": "Expression for the value to write.",
#         },
#     },
#     "MCP": {
#         "title": "MCP Step",
#         "description": "Calls a procedure via Model Context Protocol (MCP).",
#         "fields": {
#             "server": "Name of the MCP server.",
#             "procedure": "Name of the procedure to call.",
#             "params": "JSON object with parameters.",
#             "timeout": "Timeout in seconds.",
#         },
#     },
#     "CONDITIONAL": {
#         "title": "CONDITIONAL Step",
#         "description": "Conditionally branches execution based on conditions.",
#         "fields": {
#             "condition": "Condition expression to evaluate.",
#             "then_step": "Step number if condition is true.",
#             "else_step": "Step number if condition is false.",
#         },
#     },
#     "STRUCTURED_OUTPUT": {
#         "title": "STRUCTURED_OUTPUT Step",
#         "description": "Generates JSON output constrained by a JSON Schema.",
#         "fields": {
#             "output_schema": "JSON Schema defining the expected output structure.",
#             "prompt_template": "Template for the prompt sent to the LLM.",
#             "model": "Optional LLM model override.",
#             "temperature": "Temperature for generation (0.0-2.0).",
#         },
#     },
# }

# # Icons for step types
# STEP_TYPE_ICONS = {
#     "LLM": "🧠",
#     "TOOL": "🔧",
#     "MCP": "🔌",
#     "MEMORY": "💾",
#     "TRANSFORM": "🔄",
#     "CONDITIONAL": "🔀",
#     "STRUCTURED_OUTPUT": "📋",
# }

# # Quick chain templates
# CHAIN_TEMPLATES = {
#     "Simple LLM": {
#         "description": "Single LLM reasoning step — minimal baseline",
#         "steps": [
#             {"number": 1, "step_type": "LLM", "title": "Answer", "aim": "Respond to the problem.", "reasoning_questions": "What is being asked?", "dependencies": [], "step_context_queries": ["problem"], "stage_action": "Give an answer to the problem.", "example_reasoning": "", "frozen": False},
#         ],
#     },
#     "Two-Step Reasoning": {
#         "description": "Analysis → Solution (2 LLM steps) — weak baseline",
#         "steps": [
#             {"number": 1, "step_type": "LLM", "title": "Look at the problem", "aim": "Read the problem.", "reasoning_questions": "What is given?", "dependencies": [], "step_context_queries": ["problem"], "stage_action": "Read what is provided.", "example_reasoning": "", "frozen": False},
#             {"number": 2, "step_type": "LLM", "title": "Give answer", "aim": "Produce an answer.", "reasoning_questions": "What could the answer be?", "dependencies": [1], "step_context_queries": ["problem"], "stage_action": "Try to answer.", "example_reasoning": "", "frozen": False},
#         ],
#     },
#     "LLM + Tool Eval": {
#         "description": "LLM generates → Tool evaluates (frozen)",
#         "steps": [
#             {"number": 1, "step_type": "LLM", "title": "Generate Output", "aim": "Produce some output.", "reasoning_questions": "What is expected?", "dependencies": [], "step_context_queries": ["problem"], "stage_action": "Generate output for the tool.", "example_reasoning": "", "frozen": False},
#             {"number": 2, "step_type": "TOOL", "title": "Evaluate", "aim": "", "reasoning_questions": "", "dependencies": [1], "step_context_queries": [], "stage_action": "", "example_reasoning": "", "frozen": True, "step_config": {"tool_name": "", "tool_description": "Evaluation tool", "input_mapping": {"input": "@1.output"}, "timeout": 120}},
#         ],
#     },
#     "Three-Step CoT": {
#         "description": "Understand → Plan → Execute (Chain-of-Thought) — weak baseline",
#         "steps": [
#             {"number": 1, "step_type": "LLM", "title": "Read", "aim": "Look at the problem.", "reasoning_questions": "What is this about?", "dependencies": [], "step_context_queries": ["problem"], "stage_action": "Read the problem text.", "example_reasoning": "", "frozen": False},
#             {"number": 2, "step_type": "LLM", "title": "Think", "aim": "Think about it.", "reasoning_questions": "How could this be solved?", "dependencies": [1], "step_context_queries": ["problem"], "stage_action": "Consider possible approaches.", "example_reasoning": "", "frozen": False},
#             {"number": 3, "step_type": "LLM", "title": "Answer", "aim": "Give the answer.", "reasoning_questions": "What is the answer?", "dependencies": [1, 2], "step_context_queries": ["problem"], "stage_action": "Provide your answer.", "example_reasoning": "", "frozen": False},
#         ],
#     },
#     "HotpotQA (LLM-only QA)": {
#         "description": "LLM-only multi-step QA chain — weak baseline. No external tools required.",
#         "steps": [
#             {"number": 1, "step_type": "LLM", "title": "Read question", "aim": "Read the question.", "reasoning_questions": "What is the question about?", "dependencies": [], "step_context_queries": ["problem"], "stage_action": "Read what is being asked.", "example_reasoning": "", "frozen": False},
#             {"number": 2, "step_type": "LLM", "title": "Think", "aim": "Think about the answer.", "reasoning_questions": "What might the answer be?", "dependencies": [1], "step_context_queries": ["problem"], "stage_action": "Consider what you know and try to work toward an answer.", "example_reasoning": "", "frozen": False},
#             {"number": 3, "step_type": "LLM", "title": "Answer", "aim": "Produce an answer.", "reasoning_questions": "What is the answer?", "dependencies": [1, 2], "step_context_queries": ["problem"], "stage_action": "Write your answer. Format: Answer: <your answer>", "example_reasoning": "", "frozen": False},
#         ],
#     },
#     "HotpotQA (2-Hop Retrieval, static topology)": {
#         "description": "HotpotQA-style chain with retrieve tool — weak baseline. Tool steps are frozen. Requires CARL_TOOL__RETRIEVE_URL.",
#         "steps": [
#             {"number": 1, "step_type": "TOOL", "title": "Retrieve first-hop passages.", "aim": "", "reasoning_questions": "", "dependencies": [], "step_context_queries": [], "stage_action": "", "example_reasoning": "", "frozen": True, "step_config": {"tool_name": "retrieve", "tool_description": "BM25 retrieval tool (must be registered via CARL_TOOL__RETRIEVE_URL).", "input_mapping": {"query": "$outer_context"}, "timeout": 120}},
#             {"number": 2, "step_type": "LLM", "title": "Read passages", "aim": "Look at retrieved text.", "reasoning_questions": "What is in the passages?", "dependencies": [1], "step_context_queries": ["problem"], "stage_action": "Skim the passages.", "example_reasoning": "", "frozen": False},
#             {"number": 3, "step_type": "LLM", "title": "Second query", "aim": "Write a follow-up search query.", "reasoning_questions": "What else could help?", "dependencies": [2], "step_context_queries": ["problem"], "stage_action": "Write a search query.\nProvide ONLY the search query, no additional text.", "example_reasoning": "", "frozen": False},
#             {"number": 4, "step_type": "TOOL", "title": "Retrieve second-hop passages.", "aim": "", "reasoning_questions": "", "dependencies": [3], "step_context_queries": [], "stage_action": "", "example_reasoning": "", "frozen": True, "step_config": {"tool_name": "retrieve", "tool_description": "BM25 retrieval tool (must be registered via CARL_TOOL__RETRIEVE_URL).", "input_mapping": {"query": "$history[-1]"}, "timeout": 120}},
#             {"number": 5, "step_type": "LLM", "title": "Combine", "aim": "Put information together.", "reasoning_questions": "What do I have now?", "dependencies": [2, 4], "step_context_queries": ["problem"], "stage_action": "Combine what you found.", "example_reasoning": "", "frozen": False},
#             {"number": 6, "step_type": "LLM", "title": "Answer", "aim": "Give an answer.", "reasoning_questions": "What is the answer?", "dependencies": [2, 5], "step_context_queries": ["problem"], "stage_action": "Answer the question. Format: Answer: <your answer>", "example_reasoning": "", "frozen": False},
#         ],
#     },
#     "HotpotQA (2-Hop Retrieval, full chain)": {
#         "description": "Same as the retrieval baseline, but all steps are evolvable. Requires CARL_TOOL__RETRIEVE_URL.",
#         "steps": [
#             {"number": 1, "step_type": "TOOL", "title": "Retrieve first-hop passages.", "aim": "", "reasoning_questions": "", "dependencies": [], "step_context_queries": [], "stage_action": "", "example_reasoning": "", "frozen": False, "step_config": {"tool_name": "retrieve", "tool_description": "BM25 retrieval tool (must be registered via CARL_TOOL__RETRIEVE_URL).", "input_mapping": {"query": "$outer_context"}, "timeout": 120}},
#             {"number": 2, "step_type": "LLM", "title": "Read passages", "aim": "Look at retrieved text.", "reasoning_questions": "What is in the passages?", "dependencies": [1], "step_context_queries": ["problem"], "stage_action": "Skim the passages.", "example_reasoning": "", "frozen": False},
#             {"number": 3, "step_type": "LLM", "title": "Second query", "aim": "Write a follow-up search query.", "reasoning_questions": "What else could help?", "dependencies": [2], "step_context_queries": ["problem"], "stage_action": "Write a search query.\nProvide ONLY the search query, no additional text.", "example_reasoning": "", "frozen": False},
#             {"number": 4, "step_type": "TOOL", "title": "Retrieve second-hop passages.", "aim": "", "reasoning_questions": "", "dependencies": [3], "step_context_queries": [], "stage_action": "", "example_reasoning": "", "frozen": False, "step_config": {"tool_name": "retrieve", "tool_description": "BM25 retrieval tool (must be registered via CARL_TOOL__RETRIEVE_URL).", "input_mapping": {"query": "$history[-1]"}, "timeout": 120}},
#             {"number": 5, "step_type": "LLM", "title": "Combine", "aim": "Put information together.", "reasoning_questions": "What do I have now?", "dependencies": [2, 4], "step_context_queries": ["problem"], "stage_action": "Combine what you found.", "example_reasoning": "", "frozen": False},
#             {"number": 6, "step_type": "LLM", "title": "Answer", "aim": "Give an answer.", "reasoning_questions": "What is the answer?", "dependencies": [2, 5], "step_context_queries": ["problem"], "stage_action": "Answer the question. Format: Answer: <your answer>", "example_reasoning": "", "frozen": False},
#         ],
#     },
#     # ── Presets for testing TOOL / TRANSFORM / CONDITIONAL step types ──
#     "QA + Retrieve TOOL": {
#         "description": "LLM + TOOL chain — weak baseline. TOOL step is frozen; LLM step evolves.",
#         "steps": [
#             {"number": 1, "step_type": "TOOL", "title": "Retrieve relevant passages", "aim": "", "reasoning_questions": "", "dependencies": [], "step_context_queries": [], "stage_action": "", "example_reasoning": "", "frozen": True, "step_config": {"tool_name": "retrieve", "tool_description": "Built-in TF-IDF retrieval — searches the dataset for passages relevant to the query. Returns top matching passages.", "input_mapping": {"query": "$outer_context"}, "timeout": 120}},
#             {"number": 2, "step_type": "LLM", "title": "Answer from passages", "aim": "Try to answer using passages.", "reasoning_questions": "Is anything relevant here?", "dependencies": [1], "step_context_queries": ["problem"], "stage_action": "Look at the passages and try to answer. Format: Answer: <your answer>", "example_reasoning": "", "frozen": False},
#         ],
#     },
#     "QA + Transform (extract)": {
#         "description": "LLM reasoning + TRANSFORM step to extract answer — weak baseline.",
#         "steps": [
#             {"number": 1, "step_type": "LLM", "title": "Think and answer", "aim": "Try to answer the question.", "reasoning_questions": "What is being asked?", "dependencies": [], "step_context_queries": ["problem"], "stage_action": "Think about the question and write something. End with: Answer: <your answer>", "example_reasoning": "", "frozen": False},
#             {"number": 2, "step_type": "TRANSFORM", "title": "Extract final answer", "aim": "", "reasoning_questions": "", "dependencies": [1], "step_context_queries": [], "stage_action": "", "example_reasoning": "", "frozen": False, "step_config": {"transform_type": "extract", "input_key": "$history[-1]", "expression": "Answer:\\s*(.+)"}},
#         ],
#     },
#     "QA + Conditional routing": {
#         "description": "LLM classifies → CONDITIONAL routes → LLM answers — weak baseline.",
#         "steps": [
#             {"number": 1, "step_type": "LLM", "title": "Classify", "aim": "Decide question type.", "reasoning_questions": "Simple or complex?", "dependencies": [], "step_context_queries": ["problem"], "stage_action": "Output the single word 'simple' or 'complex', nothing else.", "example_reasoning": "", "frozen": False},
#             {"number": 2, "step_type": "CONDITIONAL", "title": "Route by complexity", "aim": "", "reasoning_questions": "", "dependencies": [1], "step_context_queries": [], "stage_action": "", "example_reasoning": "", "frozen": True, "step_config": {"branches": [{"condition": "simple", "target_step": 3}, {"condition": "complex", "target_step": 4}], "default_step": 3, "condition_context_key": "$history[-1]"}},
#             {"number": 3, "step_type": "LLM", "title": "Quick answer", "aim": "Answer briefly.", "reasoning_questions": "What is the answer?", "dependencies": [1], "step_context_queries": ["problem"], "stage_action": "Give a short answer. Format: Answer: <your answer>", "example_reasoning": "", "frozen": False},
#             {"number": 4, "step_type": "LLM", "title": "Detailed answer", "aim": "Answer with more thought.", "reasoning_questions": "What could the answer be?", "dependencies": [1], "step_context_queries": ["problem"], "stage_action": "Think a bit and answer. Format: Answer: <your answer>", "example_reasoning": "", "frozen": False},
#         ],
#     },
#     "LLM + TOOL + Transform pipeline": {
#         "description": "TOOL retrieves → LLM answers → TRANSFORM extracts — weak baseline.",
#         "steps": [
#             {"number": 1, "step_type": "TOOL", "title": "Retrieve context", "aim": "", "reasoning_questions": "", "dependencies": [], "step_context_queries": [], "stage_action": "", "example_reasoning": "", "frozen": True, "step_config": {"tool_name": "retrieve", "tool_description": "Built-in TF-IDF retrieval — searches for passages relevant to the query.", "input_mapping": {"query": "$outer_context"}, "timeout": 120}},
#             {"number": 2, "step_type": "LLM", "title": "Answer", "aim": "Try to answer.", "reasoning_questions": "What do the passages say?", "dependencies": [1], "step_context_queries": ["problem"], "stage_action": "Look at the passages and answer. End with: Answer: <your answer>", "example_reasoning": "", "frozen": False},
#             {"number": 3, "step_type": "TRANSFORM", "title": "Extract clean answer", "aim": "", "reasoning_questions": "", "dependencies": [2], "step_context_queries": [], "stage_action": "", "example_reasoning": "", "frozen": False, "step_config": {"transform_type": "extract", "input_key": "$history[-1]", "expression": "Answer:\\s*(.+)"}},
#         ],
#     },
# }


# def _try_import_carl_schemas():
#     """Try to import get_all_step_type_schemas from the installed mmar_carl package.

#     The library is installed via pip/uv from GitHub (see pyproject.toml).
#     Returns the function or None if the package is not available.
#     """
#     try:
#         from mmar_carl import get_all_step_type_schemas
#         logger.debug("Successfully imported CARL schemas from installed library")
#         return get_all_step_type_schemas
#     except ImportError:
#         logger.warning("mmar_carl package is not installed; using fallback hints")
#         return None


# def load_carl_step_schemas() -> Dict[str, Any]:
#     """Load step type schemas from CARL library, fallback to hardcoded hints."""
#     try:
#         get_schemas_func = _try_import_carl_schemas()
#         if not get_schemas_func:
#             return STEP_TYPE_HINTS

#         schemas = get_schemas_func()
#         if not schemas:
#             return STEP_TYPE_HINTS

#         result = {}
#         for step_type, schema in schemas.items():
#             result[step_type] = {
#                 "title": schema.get("title", step_type),
#                 "description": schema.get("description", ""),
#                 "fields": schema.get("fields", {}),
#             }
#         return result
#     except Exception as e:
#         logger.warning(f"Failed to load CARL schemas: {e}, using hardcoded hints")

#     return STEP_TYPE_HINTS


# # ---------------------------------------------------------------------------
# #  UI helpers
# # ---------------------------------------------------------------------------

# def _step_type_label(step_type: str, hints: Dict[str, Any]) -> str:
#     """Build a readable label for a step type."""
#     icon = STEP_TYPE_ICONS.get(step_type, "⚙️")
#     desc = hints.get(step_type, {}).get("description", "")
#     short = desc.split(".")[0].strip() if desc else step_type
#     return f"{icon} {step_type} — {short}"


# def _format_chain_preview_html(steps: List[dict]) -> str:
#     """Render the chain as rich HTML cards."""
#     if not steps:
#         return (
#             '<div style="text-align:center;padding:40px 20px;color:#888;'
#             'border:2px dashed #ddd;border-radius:12px;margin:12px 0;">'
#             '<div style="font-size:32px;margin-bottom:8px;">🔗</div>'
#             "<b>Chain is empty</b><br/>"
#             "Add steps using the palette below or start from a template."
#             "</div>"
#         )

#     cards: list[str] = []
#     for i, s in enumerate(steps):
#         num = s.get("number", i + 1)
#         stype = s.get("step_type", "LLM")
#         title = s.get("title", "") or f"Step {num}"
#         frozen = s.get("frozen", False)
#         deps = s.get("dependencies", [])
#         icon = STEP_TYPE_ICONS.get(stype, "⚙️")
#         aim = s.get("aim", "")

#         # Badge colours
#         type_color = {
#             "LLM": "#4A90D9",
#             "TOOL": "#E67E22",
#             "MCP": "#8E44AD",
#             "MEMORY": "#27AE60",
#             "TRANSFORM": "#F39C12",
#             "CONDITIONAL": "#E74C3C",
#             "STRUCTURED_OUTPUT": "#1ABC9C",
#         }.get(stype, "#95A5A6")

#         frozen_badge = (
#             ' <span style="background:#3498DB;color:#fff;padding:1px 7px;'
#             'border-radius:8px;font-size:11px;">❄ frozen</span>'
#             if frozen
#             else ""
#         )
#         deps_text = f" ← steps {', '.join(str(d) for d in deps)}" if deps else ""

#         aim_html = ""
#         if aim:
#             short_aim = (aim[:90] + "…") if len(aim) > 90 else aim
#             aim_html = f'<div style="color:#666;font-size:12px;margin-top:4px;">{short_aim}</div>'

#         cfg = s.get("step_config", {}) or {}
#         config_html = ""
#         if stype != "LLM" and cfg:
#             cfg_items = list(cfg.items())[:3]
#             parts = []
#             for k, v in cfg_items:
#                 vs = str(v)
#                 if len(vs) > 40:
#                     vs = vs[:37] + "…"
#                 parts.append(f"<b>{k}</b>: {vs}")
#             config_html = f'<div style="color:#888;font-size:11px;margin-top:2px;">{"  •  ".join(parts)}</div>'

#         card = (
#             f'<div style="background:#fff;border:1px solid #e0e0e0;border-left:4px solid {type_color};'
#             f'border-radius:8px;padding:10px 14px;margin-bottom:2px;">'
#             f'<div style="display:flex;align-items:center;gap:8px;">'
#             f'<span style="font-size:20px;">{icon}</span>'
#             f'<span style="background:{type_color};color:#fff;padding:2px 8px;border-radius:4px;'
#             f'font-size:12px;font-weight:600;">{stype}</span>'
#             f'<b style="font-size:14px;">#{num} {title}</b>'
#             f"{frozen_badge}"
#             f'<span style="color:#aaa;font-size:11px;margin-left:auto;">{deps_text}</span>'
#             f"</div>"
#             f"{aim_html}{config_html}"
#             f"</div>"
#         )

#         # Arrow between cards
#         if i < len(steps) - 1:
#             card += '<div style="text-align:center;color:#bbb;font-size:16px;line-height:20px;">↓</div>'

#         cards.append(card)

#     # Chain summary bar
#     n_frozen = sum(1 for s in steps if s.get("frozen", False))
#     n_evolvable = len(steps) - n_frozen
#     types_used = sorted(set(s.get("step_type", "LLM") for s in steps))
#     summary = (
#         f'<div style="display:flex;gap:16px;padding:6px 0;color:#666;font-size:12px;margin-bottom:6px;">'
#         f"<span>📊 <b>{len(steps)}</b> steps</span>"
#         f"<span>🧬 <b>{n_evolvable}</b> evolvable</span>"
#         f"<span>❄ <b>{n_frozen}</b> frozen</span>"
#         f'<span>Types: {", ".join(types_used)}</span>'
#         f"</div>"
#     )

#     return summary + "".join(cards)


# def _default_step(number: int, step_type: str = "LLM") -> dict:
#     """Create a default step dict."""
#     return {
#         "number": int(number),
#         "step_type": step_type.upper(),
#         "title": "",
#         "aim": "",
#         "reasoning_questions": "",
#         "dependencies": [] if number == 1 else [number - 1],
#         "step_context_queries": ["problem"],
#         "stage_action": "",
#         "example_reasoning": "",
#         "frozen": False,
#     }


# def _steps_to_chain_json(steps: list[dict]) -> str:
#     """Convert internal steps list to CARL chain config JSON."""
#     out_steps = []
#     for s in steps:
#         s_clean: dict = {
#             "number": int(s.get("number", 0)),
#             "title": s.get("title", ""),
#             "aim": s.get("aim", ""),
#             "reasoning_questions": s.get("reasoning_questions", ""),
#             "dependencies": s.get("dependencies", []) or [],
#             "step_context_queries": s.get("step_context_queries", []) or [],
#             "stage_action": s.get("stage_action", ""),
#             "example_reasoning": s.get("example_reasoning", ""),
#         }
#         stype_ui = str(s.get("step_type", "LLM")).upper()
#         if stype_ui != "LLM":
#             s_clean["step_type"] = stype_ui.lower()
#             step_cfg = dict(s.get("step_config", {}) or {})
#             s_clean["step_config"] = step_cfg
#         if s.get("frozen", False):
#             s_clean["frozen"] = True
#         out_steps.append(s_clean)
#     return json.dumps({"steps": out_steps}, indent=2, ensure_ascii=False)


# def _chain_json_to_steps(json_str: str) -> list[dict]:
#     """Parse CARL chain config JSON back to internal steps list."""
#     try:
#         cfg = json.loads(json_str)
#     except Exception:
#         return []
#     raw_steps = cfg.get("steps", [])
#     if not isinstance(raw_steps, list):
#         return []
#     steps = []
#     for s in raw_steps:
#         if not isinstance(s, dict):
#             continue
#         stype = str(s.get("step_type", "LLM")).upper()
#         if stype == "":
#             stype = "LLM"
#         step: dict = {
#             "number": int(s.get("number", len(steps) + 1)),
#             "step_type": stype,
#             "title": s.get("title", ""),
#             "aim": s.get("aim", ""),
#             "reasoning_questions": s.get("reasoning_questions", ""),
#             "dependencies": s.get("dependencies", []) or [],
#             "step_context_queries": s.get("step_context_queries", []) or [],
#             "stage_action": s.get("stage_action", ""),
#             "example_reasoning": s.get("example_reasoning", ""),
#             "frozen": bool(s.get("frozen", False)),
#         }
#         if s.get("step_config"):
#             step["step_config"] = dict(s["step_config"])
#         steps.append(step)
#     return steps


# _PYTHON_TEMPLATE = '''"""Custom Python code for CARL chain experiment.

# You can define custom tool functions, validators, or preprocessing
# logic here. This code will be available during chain execution.

# Example: custom tool function that can be registered in ReasoningContext:
# """

# import json
# from typing import Any, Dict


# # ── Custom Tool Functions ──────────────────────────────────────────────────
# # These functions can be registered as TOOL steps in your chain.
# # They receive keyword arguments from the step's input_mapping
# # and should return a dict with results.

# def my_custom_tool(**kwargs) -> Dict[str, Any]:
#     """Example custom tool.

#     Register this tool in the chain by setting step_type="TOOL"
#     and tool_name="my_custom_tool".
#     """
#     query = kwargs.get("query", "")
#     # Your custom logic here
#     result = f"Processed: {query}"
#     return {"result": result}


# # ── Custom Validation / Preprocessing ──────────────────────────────────────

# def preprocess_input(text: str) -> str:
#     """Optional: preprocess input before sending to chain."""
#     return text.strip()


# def postprocess_output(output: str) -> str:
#     """Optional: postprocess chain output before evaluation."""
#     return output.strip()
# '''


# # ---------------------------------------------------------------------------
# #  Component
# # ---------------------------------------------------------------------------

# class CreateCARLExperimentComponent(BaseComponent):
#     def __init__(self, *args, **kwargs):
#         super().__init__(*args, **kwargs)
#         self.step_type_hints = load_carl_step_schemas()

#     def build(self) -> gr.Column:
#         hints = self.step_type_hints

#         with gr.Column() as component:
#             gr.Markdown("## 🔗 Create CARL Chain Experiment")

#             gr.Markdown("#### Start with a Preset")
#             with gr.Row():
#                 btn_hotpotqa_one_click = gr.Button("🧠 HotpotQA — LLM-only", size="sm")
#                 btn_tool_preset = gr.Button("🔧 QA + Retrieve TOOL", size="sm")
#                 btn_transform_preset = gr.Button("🔄 QA + Transform", size="sm")
#             with gr.Row():
#                 btn_conditional_preset = gr.Button("🔀 QA + Conditional", size="sm")
#                 btn_mixed_preset = gr.Button("⚙️ TOOL + LLM + Transform", size="sm")
#             gr.Markdown(
#                 "**Note:** Each preset auto-loads a small demo dataset (HotpotQA). "
#                 "You can replace it by uploading your own dataset file."
#             )

#             # ── SECTION 1: Experiment settings ────────────────────────
#             with gr.Accordion("⚙️ Experiment Settings", open=True):
#                 with gr.Row():
#                     with gr.Column(scale=2):
#                         name_input = gr.Textbox(
#                             label="Experiment Name",
#                             placeholder="e.g., Math Reasoning Chain",
#                         )
#                         description_input = gr.Textbox(
#                             label="Description",
#                             placeholder="Short description of the goal and setup…",
#                             lines=2,
#                         )
#                     with gr.Column(scale=1):
#                         data_file_input = gr.File(
#                             label="Dataset",
#                             file_types=[".csv", ".json", ".txt", ".zip"],
#                             height=80,
#                             elem_classes="short-upload",
#                         )
#                         dataset_info = gr.Textbox(
#                             label="Dataset Source",
#                             value="No file selected",
#                             interactive=False,
#                         )

#                 with gr.Row():
#                     target_field_input = gr.Textbox(
#                         label="Target Column",
#                         placeholder="target",
#                         scale=1,
#                     )
#                     llm_model_input = gr.Dropdown(
#                         choices=get_llm_model_choices(),
#                         value=get_default_llm_model_id(),
#                         label="🧬 Evolution Model",
#                         info="LLM that drives the evolution process (mutates chain steps)",
#                         scale=1,
#                     )
#                     chain_llm_model_input = gr.Dropdown(
#                         choices=[("Same as Evolution Model", "")] + get_llm_model_choices(),
#                         value="",
#                         label="🔗 Chain Execution Model",
#                         info="LLM used by chain steps for reasoning (if different from evolution model)",
#                         scale=1,
#                     )
#                     max_iterations_input = gr.Slider(
#                         minimum=1,
#                         maximum=500,
#                         value=100,
#                         step=1,
#                         label="Max Iterations",
#                         scale=1,
#                     )

#                 with gr.Row():
#                     chain_size_limit_input = gr.Number(
#                         value=None,
#                         label="Chain Size Limit",
#                         info="Maximum number of steps allowed (empty = no limit)",
#                         precision=0,
#                         minimum=1,
#                         scale=1,
#                     )
#                     dataset_size_input = gr.Number(
#                         value=None,
#                         label="Dataset Size (rows)",
#                         info="Optional subsample limit (empty = all rows)",
#                         precision=0,
#                         scale=1,
#                     )
#                     test_size_input = gr.Slider(
#                         minimum=0.0,
#                         maximum=0.9,
#                         value=0.2,
#                         step=0.05,
#                         label="Test Split Ratio",
#                         scale=1,
#                     )

#                 with gr.Row():
#                     evolution_mode_input = gr.Dropdown(
#                         choices=["full_chain", "single_step"],
#                         value="full_chain",
#                         label="Evolution Mode",
#                         info="full_chain: evolve all non-frozen steps; single_step: evolve a single step",
#                         scale=1,
#                     )
#                     evolution_step_number_input = gr.Number(
#                         value=None,
#                         label="Step Number to Evolve",
#                         info="Only used for single_step mode",
#                         precision=0,
#                         visible=False,
#                         scale=1,
#                     )

#             # ── SECTION 2: Chain Builder (3 tabs) ──────────────────────
#             gr.Markdown("### 🏗️ Chain Builder")

#             # Chain size indicator
#             chain_size_indicator = gr.HTML(value="")

#             with gr.Tabs() as chain_tabs:
#                 # ─── TAB 1: Visual Builder ─────────────────────────────
#                 with gr.Tab("🏗️ Visual Builder"):
#                     # Current chain preview (rich HTML)
#                     chain_preview = gr.HTML(
#                         value=_format_chain_preview_html([]),
#                         label="Current Chain",
#                     )

#                     # Quick templates
#                     with gr.Accordion("⚡ Quick Templates", open=False):
#                         gr.Markdown(
#                             "Click to create a chain from a template. "
#                             "Existing steps will be **replaced**."
#                         )
#                         with gr.Row():
#                             template_btns = {}
#                             for tpl_name, tpl_data in CHAIN_TEMPLATES.items():
#                                 n_steps = len(tpl_data["steps"])
#                                 types = ", ".join(s["step_type"] for s in tpl_data["steps"])
#                                 template_btns[tpl_name] = gr.Button(
#                                     f"📝 {tpl_name}\n({n_steps} steps: {types})",
#                                     size="sm",
#                                 )

#                     # Step palette — add new steps
#                     with gr.Accordion("➕ Add Step", open=True):
#                         # Build step type choices with descriptions
#                         type_choices = [_step_type_label(t, hints) for t in CARL_STEP_TYPES]
#                         step_type_radio = gr.Radio(
#                             choices=type_choices,
#                             value=type_choices[0],
#                             label="Step Type",
#                             info="Pick a type and click \"Add step\"",
#                         )
#                         add_step_btn = gr.Button("➕ Add Step to Chain", variant="primary")

#                     # Step selector + actions
#                     with gr.Row():
#                         selected_step_dropdown = gr.Dropdown(
#                             choices=[], value=None,
#                             label="Select a step to edit",
#                             interactive=True, scale=3,
#                         )
#                         edit_step_btn = gr.Button("✏️ Edit", size="sm", scale=1)
#                         toggle_freeze_btn = gr.Button("❄️ Freeze / Unfreeze", size="sm", scale=1)
#                         remove_step_btn = gr.Button("🗑️ Delete Step", variant="stop", size="sm", scale=1)

#                     # Move up / down buttons
#                     with gr.Row():
#                         move_up_btn = gr.Button("⬆️ Move Up", size="sm", scale=1)
#                         move_down_btn = gr.Button("⬇️ Move Down", size="sm", scale=1)
#                         duplicate_step_btn = gr.Button("📋 Duplicate Step", size="sm", scale=1)

#                     # ── SECTION 3: Step Editor (hidden by default) ────────────
#                     with gr.Group(visible=False) as step_form_group:
#                         step_editor_title = gr.Markdown("### ✏️ Edit Step")

#                         with gr.Row():
#                             with gr.Column(scale=2):
#                                 title_input = gr.Textbox(
#                                     label="Step Title *",
#                                     placeholder="e.g., Analyze Problem",
#                                 )
#                             with gr.Column(scale=1):
#                                 step_type_display = gr.Textbox(
#                                     label="Type", interactive=False,
#                                 )
#                                 frozen_checkbox = gr.Checkbox(
#                                     label="❄ Frozen (do not evolve)",
#                                     value=False,
#                                 )

#                         # Dependencies as multi-select
#                         dependencies_checkboxgroup = gr.CheckboxGroup(
#                             choices=[], value=[],
#                             label="Dependencies (steps that must run before this step)",
#                         )

#                         # LLM-specific fields
#                         with gr.Accordion("🧠 LLM Fields", open=True, visible=False) as llm_fields_group:
#                             aim_input = gr.Textbox(
#                                 label="Aim *", lines=2,
#                                 placeholder="What should this step achieve?",
#                                 info=hints.get("LLM", {}).get("fields", {}).get("aim", ""),
#                             )
#                             reasoning_questions_input = gr.Textbox(
#                                 label="Reasoning Questions *", lines=2,
#                                 placeholder="Questions that guide the LLM reasoning for this step",
#                                 info=hints.get("LLM", {}).get("fields", {}).get("reasoning_questions", ""),
#                             )
#                             step_context_queries_input = gr.Textbox(
#                                 label="Context Queries (JSON array)",
#                                 placeholder='["problem"]',
#                                 info='Context selectors for this step. Example: ["problem"], ["@1.output"]',
#                             )
#                             stage_action_input = gr.Textbox(
#                                 label="Stage Action *", lines=2,
#                                 placeholder="Detailed instructions for what to do in this step",
#                                 info=hints.get("LLM", {}).get("fields", {}).get("stage_action", ""),
#                             )
#                             example_reasoning_input = gr.Textbox(
#                                 label="Example Reasoning", lines=3,
#                                 placeholder="Optional example of the desired reasoning style",
#                                 info=hints.get("LLM", {}).get("fields", {}).get("example_reasoning", ""),
#                             )

#                         # TOOL config
#                         with gr.Accordion("🔧 TOOL Configuration", open=True, visible=False) as tool_config_group:
#                             gr.Markdown(f"*{hints.get('TOOL', {}).get('description', '')}*")
#                             tool_name_input = gr.Textbox(label="Tool Name *", placeholder="my_evaluator")
#                             tool_description_input = gr.Textbox(label="Tool Description", lines=2, placeholder="What does this tool do?")
#                             tool_input_mapping_input = gr.Textbox(
#                                 label="Input Mapping (JSON) *",
#                                 placeholder='{"input": "@1.output", "data": "@context.data"}',
#                                 lines=3,
#                             )
#                             tool_timeout_input = gr.Number(label="Timeout (sec)", value=120, precision=0, minimum=1, maximum=3600)

#                         # TRANSFORM config
#                         with gr.Accordion("🔄 TRANSFORM Configuration", open=True, visible=False) as transform_config_group:
#                             gr.Markdown(f"*{hints.get('TRANSFORM', {}).get('description', '')}*")
#                             transform_type_input = gr.Dropdown(
#                                 choices=["extract", "format", "aggregate", "filter", "map", "python_expr", "jmespath"],
#                                 value="extract", label="Transform Type *",
#                             )
#                             transform_input_key_input = gr.Textbox(label="Input Key", placeholder="$history[-1]", value="$history[-1]")
#                             transform_expression_input = gr.Textbox(label="Expression", placeholder="Expression to evaluate", lines=2)
#                             transform_output_format_input = gr.Textbox(label="Output Format", placeholder="Output format template", lines=2)
#                             transform_map_template_input = gr.Textbox(label="Map Template", placeholder="Template for map operations", lines=2)

#                         # MEMORY config
#                         with gr.Accordion("💾 MEMORY Configuration", open=True, visible=False) as memory_config_group:
#                             gr.Markdown(f"*{hints.get('MEMORY', {}).get('description', '')}*")
#                             memory_operation_input = gr.Dropdown(choices=["read", "write", "append", "delete", "list"], value="read", label="Operation *")
#                             memory_namespace_input = gr.Textbox(label="Namespace", placeholder="default", value="default")
#                             memory_key_input = gr.Textbox(label="Memory Key *", placeholder="my_key")
#                             memory_value_source_input = gr.Textbox(label="Value Source (for write)", placeholder="$history[-1]")
#                             memory_default_value_input = gr.Textbox(label="Default Value", placeholder="Default value if missing")

#                         # MCP config
#                         with gr.Accordion("🔌 MCP Configuration", open=True, visible=False) as mcp_config_group:
#                             gr.Markdown(f"*{hints.get('MCP', {}).get('description', '')}*")
#                             mcp_server_name_input = gr.Textbox(label="Server Name *", placeholder="mcp_server")
#                             mcp_tool_name_input = gr.Textbox(label="Tool Name *", placeholder="tool_name")
#                             mcp_arguments_input = gr.Textbox(label="Arguments (JSON)", placeholder='{"key": "value"}', lines=2)
#                             mcp_argument_mapping_input = gr.Textbox(label="Argument Mapping (JSON)", placeholder='{"arg": "@1.output"}', lines=2)
#                             mcp_timeout_input = gr.Number(label="Timeout (sec)", value=30, precision=0, minimum=1, maximum=3600)

#                         # CONDITIONAL config
#                         with gr.Accordion("🔀 CONDITIONAL Configuration", open=True, visible=False) as conditional_config_group:
#                             gr.Markdown(f"*{hints.get('CONDITIONAL', {}).get('description', '')}*")
#                             conditional_branches_input = gr.Textbox(
#                                 label="Branches (JSON) *",
#                                 placeholder='[{"condition": "@1.output == \\"yes\\"", "next_step": 2}]',
#                                 lines=4,
#                             )
#                             conditional_default_step_input = gr.Number(label="Default Step", precision=0, minimum=1)
#                             conditional_condition_context_key_input = gr.Textbox(label="Condition Context Key", value="$history[-1]")

#                         # STRUCTURED_OUTPUT config
#                         with gr.Accordion("📋 STRUCTURED_OUTPUT Configuration", open=True, visible=False) as structured_output_config_group:
#                             gr.Markdown(f"*{hints.get('STRUCTURED_OUTPUT', {}).get('description', '')}*")
#                             structured_output_schema_input = gr.Textbox(
#                                 label="Output Schema (JSON Schema) *",
#                                 placeholder='{"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}',
#                                 lines=5,
#                             )
#                             structured_output_prompt_input = gr.Textbox(label="Prompt Template", placeholder="Based on @context.problem, …", lines=3)
#                             structured_output_model_input = gr.Textbox(label="Model Override", placeholder="Leave empty to use the chain default model")
#                             structured_output_temperature_input = gr.Slider(minimum=0.0, maximum=2.0, value=0.0, step=0.1, label="Temperature")

#                         with gr.Row():
#                             save_step_btn = gr.Button("💾 Save Step", variant="primary", size="lg")
#                             cancel_step_btn = gr.Button("❌ Cancel", variant="secondary", size="lg")

#                 # ─── TAB 2: JSON Editor ────────────────────────────────
#                 with gr.Tab("📝 JSON Editor"):
#                     gr.Markdown(
#                         "Edit the chain configuration as raw JSON. "
#                         "Changes here **sync with** the Visual Builder. "
#                         "Click **Apply JSON** to update the visual view."
#                     )
#                     chain_json_editor = gr.Code(
#                         value='{"steps": []}',
#                         language="json",
#                         label="Chain Configuration (JSON)",
#                         lines=25,
#                     )
#                     with gr.Row():
#                         apply_json_btn = gr.Button("✅ Apply JSON → Visual Builder", variant="primary")
#                         export_json_btn = gr.Button("📤 Visual Builder → JSON", variant="secondary")
#                     json_status = gr.HTML(value="")

#                 # ─── TAB 3: Python Code ────────────────────────────────
#                 with gr.Tab("🐍 Python Code"):
#                     gr.Markdown(
#                         "Write custom Python code for your CARL experiment. "
#                         "Define **tool functions**, **validators**, or **pre/post-processors** "
#                         "that can be used by TOOL steps in your chain."
#                     )
#                     python_code_editor = gr.Code(
#                         value=_PYTHON_TEMPLATE,
#                         language="python",
#                         label="Custom Python Code",
#                         lines=30,
#                     )
#                     gr.Markdown(
#                         "<details><summary>💡 <b>How to use custom code with CARL</b></summary>\n\n"
#                         "1. Define your Python functions above\n"
#                         "2. In the Visual Builder, add a TOOL step\n"
#                         "3. Set `tool_name` to the function name (e.g. `my_custom_tool`)\n"
#                         "4. The function will receive keyword arguments from `input_mapping`\n"
#                         "5. Return a `dict` with results\n\n"
#                         "Environment variables like `CARL_TOOL__<NAME>_URL` can also register "
#                         "HTTP tools automatically.\n"
#                         "</details>"
#                     )

#             # ── SECTION 4: Create / Clean ─────────────────────────────
#             with gr.Row():
#                 create_btn = gr.Button("🚀 Create Experiment", variant="primary", size="lg")
#                 clean_btn = gr.Button("🧹 Clear Form", variant="secondary", size="lg")
#             create_output = gr.Textbox(label="Status", interactive=False, lines=3)

#             # Internal state
#             steps_state = gr.State(value=[])
#             dataset_path_state = gr.State(value="")
#             current_editing_step = gr.State(value=None)

#             # ══════════════════════════════════════════════════════════
#             #  Callbacks
#             # ══════════════════════════════════════════════════════════

#             def _parse_step_type_from_radio(radio_val: str) -> str:
#                 """Extract step type from radio label like '🧠 LLM — description'."""
#                 if not radio_val:
#                     return "LLM"
#                 parts = radio_val.split("—")[0].strip()
#                 for t in CARL_STEP_TYPES:
#                     if t in parts:
#                         return t
#                 return "LLM"

#             def _build_step_choices(steps: list[dict]) -> list[str]:
#                 """Build dropdown choices for step selector."""
#                 choices = []
#                 for s in steps:
#                     num = s.get("number", 0)
#                     stype = s.get("step_type", "LLM")
#                     title = s.get("title", "") or f"Step {num}"
#                     icon = STEP_TYPE_ICONS.get(stype, "⚙️")
#                     frozen = " ❄" if s.get("frozen") else ""
#                     choices.append(f"{num}: {icon} [{stype}] {title}{frozen}")
#                 return choices

#             def _step_number_from_choice(choice: str) -> Optional[int]:
#                 """Parse step number from choice string like '1: 🧠 [LLM] Title'."""
#                 if not choice:
#                     return None
#                 try:
#                     return int(choice.split(":")[0].strip())
#                 except (ValueError, IndexError):
#                     return None

#             def _build_deps_choices(steps: list[dict], current_num: int) -> list[str]:
#                 """Build checkbox choices for dependencies."""
#                 choices = []
#                 for s in steps:
#                     n = s.get("number", 0)
#                     if n == current_num:
#                         continue
#                     title = s.get("title", "") or f"Step {n}"
#                     stype = s.get("step_type", "LLM")
#                     icon = STEP_TYPE_ICONS.get(stype, "⚙️")
#                     choices.append(f"{n}: {icon} {title}")
#                 return choices

#             def _deps_values_from_list(deps: list[int], steps: list[dict], current_num: int) -> list[str]:
#                 """Convert dependency numbers to checkbox values."""
#                 choices_map = {}
#                 for s in steps:
#                     n = s.get("number", 0)
#                     if n == current_num:
#                         continue
#                     title = s.get("title", "") or f"Step {n}"
#                     stype = s.get("step_type", "LLM")
#                     icon = STEP_TYPE_ICONS.get(stype, "⚙️")
#                     choices_map[n] = f"{n}: {icon} {title}"
#                 return [choices_map[d] for d in deps if d in choices_map]

#             def _deps_from_values(values: list[str]) -> list[int]:
#                 """Convert checkbox values back to dependency numbers."""
#                 result = []
#                 for v in values:
#                     try:
#                         result.append(int(v.split(":")[0].strip()))
#                     except (ValueError, IndexError):
#                         pass
#                 return sorted(result)

#             def _update_chain_size_indicator(steps: list[dict], limit) -> str:
#                 n = len(steps)
#                 try:
#                     max_n = int(limit) if limit not in (None, "", 0) else 0
#                 except (ValueError, TypeError):
#                     max_n = 0

#                 if max_n > 0:
#                     pct = min(100, int(n / max_n * 100))
#                     color = "#27AE60" if pct < 80 else ("#F39C12" if pct < 100 else "#E74C3C")
#                     return (
#                         f'<div style="display:flex;align-items:center;gap:10px;padding:6px 0;">'
#                         f'<span style="font-size:13px;color:#555;">Steps: <b>{n}</b> / {max_n}</span>'
#                         f'<div style="flex:1;background:#eee;border-radius:4px;height:8px;">'
#                         f'<div style="width:{pct}%;background:{color};border-radius:4px;height:8px;'
#                         f'transition:width 0.3s;"></div></div></div>'
#                     )
#                 else:
#                     return f'<div style="padding:4px 0;font-size:13px;color:#555;">Steps: <b>{n}</b> (no limit)</div>'

#             # -- Show / hide form --
#             def _show_form_for_type(stype: str, step_num: Optional[int]):
#                 """Return visibility updates for step form groups (10 values, no deps)."""
#                 stype = stype.upper()
#                 num = int(step_num) if step_num else 0
#                 title_text = f"### ✏️ Step #{num}" if num else "### ✏️ New Step"
#                 return (
#                     gr.update(visible=True),                                    # step_form_group
#                     gr.update(value=title_text),                                # step_editor_title
#                     gr.update(value=f"{STEP_TYPE_ICONS.get(stype, '⚙️')} {stype}"),  # step_type_display
#                     gr.update(visible=stype == "LLM"),                          # llm_fields_group
#                     gr.update(visible=stype == "TOOL"),                          # tool_config_group
#                     gr.update(visible=stype == "TRANSFORM"),                     # transform_config_group
#                     gr.update(visible=stype == "MEMORY"),                        # memory_config_group
#                     gr.update(visible=stype == "MCP"),                           # mcp_config_group
#                     gr.update(visible=stype == "CONDITIONAL"),                   # conditional_config_group
#                     gr.update(visible=stype == "STRUCTURED_OUTPUT"),             # structured_output_config_group
#                 )

#             # -- Add step --
#             def _add_step(steps, radio_val, size_limit):
#                 steps = list(steps or [])
#                 stype = _parse_step_type_from_radio(radio_val)
#                 next_num = len(steps) + 1

#                 # Size limit check
#                 try:
#                     if size_limit not in (None, "", 0) and int(size_limit) > 0 and next_num > int(size_limit):
#                         return (
#                             gr.update(value=steps),
#                             gr.update(value=_format_chain_preview_html(steps)),
#                             gr.update(value=_update_chain_size_indicator(steps, size_limit)),
#                             gr.update(choices=_build_step_choices(steps)),
#                             None,
#                         ) + _show_form_for_type(stype, None) + (
#                             # field defaults
#                             gr.update(value=""), gr.update(value=False),
#                             gr.update(choices=[], value=[]),  # deps
#                             gr.update(value=""), gr.update(value=""), gr.update(value='["problem"]'),
#                             gr.update(value=""), gr.update(value=""),
#                         )
#                 except Exception:
#                     pass

#                 new_step = _default_step(next_num, stype)
#                 steps.append(new_step)

#                 form_updates = _show_form_for_type(stype, next_num)
#                 dep_choices = _build_deps_choices(steps, next_num)
#                 dep_values = _deps_values_from_list(new_step["dependencies"], steps, next_num)

#                 return (
#                     gr.update(value=steps),
#                     gr.update(value=_format_chain_preview_html(steps)),
#                     gr.update(value=_update_chain_size_indicator(steps, size_limit)),
#                     gr.update(choices=_build_step_choices(steps), value=_build_step_choices(steps)[-1]),
#                     len(steps) - 1,  # current_editing_step
#                 ) + form_updates + (
#                     gr.update(value=new_step["title"]),
#                     gr.update(value=new_step.get("frozen", False)),
#                     gr.update(choices=dep_choices, value=dep_values),  # deps (choices + value combined)
#                     gr.update(value=""), gr.update(value=""), gr.update(value='["problem"]'),
#                     gr.update(value=""), gr.update(value=""),
#                 )

#             add_step_btn.click(
#                 _add_step,
#                 inputs=[steps_state, step_type_radio, chain_size_limit_input],
#                 outputs=[
#                     steps_state, chain_preview, chain_size_indicator, selected_step_dropdown,
#                     current_editing_step,
#                     step_form_group, step_editor_title, step_type_display,
#                     llm_fields_group, tool_config_group, transform_config_group,
#                     memory_config_group, mcp_config_group, conditional_config_group,
#                     structured_output_config_group,
#                     # field defaults
#                     title_input, frozen_checkbox, dependencies_checkboxgroup,
#                     aim_input, reasoning_questions_input, step_context_queries_input,
#                     stage_action_input, example_reasoning_input,
#                 ],
#             )

#             # -- Edit step --
#             # Total output count: 1 (editing_idx) + 10 (form) + 3 (common) + 5 (llm) + 4 (tool)
#             #   + 5 (transform) + 5 (memory) + 5 (mcp) + 3 (conditional) + 4 (structured_output) = 45
#             _N_EDIT_OUTPUTS = 45

#             def _edit_step(steps, choice):
#                 step_num = _step_number_from_choice(choice)
#                 if not steps or step_num is None:
#                     return (None, gr.update(visible=False)) + tuple(gr.update() for _ in range(_N_EDIT_OUTPUTS - 2))

#                 idx = step_num - 1
#                 if idx < 0 or idx >= len(steps):
#                     return (None, gr.update(visible=False)) + tuple(gr.update() for _ in range(_N_EDIT_OUTPUTS - 2))

#                 s = steps[idx]
#                 stype = s.get("step_type", "LLM")
#                 cfg = s.get("step_config", {}) or {}
#                 form_updates = _show_form_for_type(stype, step_num)
#                 dep_choices = _build_deps_choices(steps, step_num)
#                 dep_values = _deps_values_from_list(s.get("dependencies", []), steps, step_num)

#                 return (
#                     idx,  # current_editing_step
#                 ) + form_updates + (
#                     # Common fields
#                     gr.update(value=s.get("title", "")),
#                     gr.update(value=bool(s.get("frozen", False))),
#                     gr.update(choices=dep_choices, value=dep_values),  # deps (single update)
#                     # LLM fields
#                     gr.update(value=s.get("aim", "")),
#                     gr.update(value=s.get("reasoning_questions", "")),
#                     gr.update(value=json.dumps(s.get("step_context_queries", []))),
#                     gr.update(value=s.get("stage_action", "")),
#                     gr.update(value=s.get("example_reasoning", "")),
#                     # TOOL
#                     gr.update(value=cfg.get("tool_name", "")),
#                     gr.update(value=cfg.get("tool_description", "")),
#                     gr.update(value=json.dumps(cfg.get("input_mapping", {})) if isinstance(cfg.get("input_mapping"), dict) else str(cfg.get("input_mapping", "{}"))),
#                     gr.update(value=cfg.get("timeout", 120)),
#                     # TRANSFORM
#                     gr.update(value=cfg.get("transform_type", "extract")),
#                     gr.update(value=cfg.get("input_key", "$history[-1]")),
#                     gr.update(value=cfg.get("expression", "")),
#                     gr.update(value=cfg.get("output_format", "")),
#                     gr.update(value=cfg.get("map_template", "")),
#                     # MEMORY
#                     gr.update(value=cfg.get("operation", "read")),
#                     gr.update(value=cfg.get("namespace", "default")),
#                     gr.update(value=cfg.get("memory_key", cfg.get("key", ""))),
#                     gr.update(value=cfg.get("value_source", "")),
#                     gr.update(value=str(cfg.get("default_value", "")) if cfg.get("default_value") is not None else ""),
#                     # MCP
#                     gr.update(value=cfg.get("server", {}).get("server_name", "") if isinstance(cfg.get("server"), dict) else str(cfg.get("server", ""))),
#                     gr.update(value=cfg.get("tool_name", "")),
#                     gr.update(value=json.dumps(cfg.get("arguments", {})) if isinstance(cfg.get("arguments"), dict) else "{}"),
#                     gr.update(value=json.dumps(cfg.get("argument_mapping", {})) if isinstance(cfg.get("argument_mapping"), dict) else "{}"),
#                     gr.update(value=cfg.get("timeout", 30)),
#                     # CONDITIONAL
#                     gr.update(value=json.dumps(cfg.get("branches", [])) if isinstance(cfg.get("branches"), list) else "[]"),
#                     gr.update(value=cfg.get("default_step")),
#                     gr.update(value=cfg.get("condition_context_key", "$history[-1]")),
#                     # STRUCTURED_OUTPUT
#                     gr.update(value=json.dumps(cfg.get("output_schema", {})) if isinstance(cfg.get("output_schema"), dict) else str(cfg.get("output_schema", "{}"))),
#                     gr.update(value=cfg.get("prompt_template", "")),
#                     gr.update(value=cfg.get("model", "")),
#                     gr.update(value=cfg.get("temperature", 0.0)),
#                 )

#             _edit_outputs = [
#                 current_editing_step,
#                 step_form_group, step_editor_title, step_type_display,
#                 llm_fields_group, tool_config_group, transform_config_group,
#                 memory_config_group, mcp_config_group, conditional_config_group,
#                 structured_output_config_group,
#                 # common fields
#                 title_input, frozen_checkbox, dependencies_checkboxgroup,
#                 # LLM
#                 aim_input, reasoning_questions_input, step_context_queries_input,
#                 stage_action_input, example_reasoning_input,
#                 # TOOL
#                 tool_name_input, tool_description_input, tool_input_mapping_input, tool_timeout_input,
#                 # TRANSFORM
#                 transform_type_input, transform_input_key_input, transform_expression_input,
#                 transform_output_format_input, transform_map_template_input,
#                 # MEMORY
#                 memory_operation_input, memory_namespace_input, memory_key_input,
#                 memory_value_source_input, memory_default_value_input,
#                 # MCP
#                 mcp_server_name_input, mcp_tool_name_input, mcp_arguments_input,
#                 mcp_argument_mapping_input, mcp_timeout_input,
#                 # CONDITIONAL
#                 conditional_branches_input, conditional_default_step_input,
#                 conditional_condition_context_key_input,
#                 # STRUCTURED_OUTPUT
#                 structured_output_schema_input, structured_output_prompt_input,
#                 structured_output_model_input, structured_output_temperature_input,
#             ]

#             edit_step_btn.click(
#                 _edit_step,
#                 inputs=[steps_state, selected_step_dropdown],
#                 outputs=_edit_outputs,
#             )
#             selected_step_dropdown.change(
#                 _edit_step,
#                 inputs=[steps_state, selected_step_dropdown],
#                 outputs=_edit_outputs,
#             )

#             # -- Toggle freeze --
#             def _toggle_freeze(steps, choice, size_limit):
#                 steps = list(steps or [])
#                 step_num = _step_number_from_choice(choice)
#                 if step_num is None or step_num < 1 or step_num > len(steps):
#                     return gr.update(), gr.update(), gr.update(), gr.update()
#                 idx = step_num - 1
#                 steps[idx]["frozen"] = not steps[idx].get("frozen", False)
#                 return (
#                     gr.update(value=steps),
#                     gr.update(value=_format_chain_preview_html(steps)),
#                     gr.update(value=_update_chain_size_indicator(steps, size_limit)),
#                     gr.update(choices=_build_step_choices(steps), value=_build_step_choices(steps)[idx] if steps else None),
#                 )

#             toggle_freeze_btn.click(
#                 _toggle_freeze,
#                 inputs=[steps_state, selected_step_dropdown, chain_size_limit_input],
#                 outputs=[steps_state, chain_preview, chain_size_indicator, selected_step_dropdown],
#             )

#             # -- Remove step --
#             def _remove_step(steps, choice, size_limit):
#                 steps = list(steps or [])
#                 step_num = _step_number_from_choice(choice)
#                 if step_num is None or step_num < 1 or step_num > len(steps):
#                     return gr.update(), gr.update(), gr.update(), gr.update(), gr.update(visible=False), None
#                 idx = step_num - 1
#                 del steps[idx]
#                 # Renumber
#                 for i, s in enumerate(steps, start=1):
#                     old_num = s["number"]
#                     s["number"] = i
#                     deps = s.get("dependencies", [])
#                     s["dependencies"] = sorted(
#                         [d - 1 if d > step_num else d for d in deps if d != step_num]
#                     )
#                 choices = _build_step_choices(steps)
#                 return (
#                     gr.update(value=steps),
#                     gr.update(value=_format_chain_preview_html(steps)),
#                     gr.update(value=_update_chain_size_indicator(steps, size_limit)),
#                     gr.update(choices=choices, value=choices[-1] if choices else None),
#                     gr.update(visible=False),  # hide form
#                     None,
#                 )

#             remove_step_btn.click(
#                 _remove_step,
#                 inputs=[steps_state, selected_step_dropdown, chain_size_limit_input],
#                 outputs=[steps_state, chain_preview, chain_size_indicator, selected_step_dropdown, step_form_group, current_editing_step],
#             )

#             # -- Move step up --
#             def _move_step_up(steps, choice, size_limit):
#                 steps = list(steps or [])
#                 step_num = _step_number_from_choice(choice)
#                 if step_num is None or step_num <= 1 or step_num > len(steps):
#                     return gr.update(), gr.update(), gr.update(), gr.update()
#                 idx = step_num - 1
#                 # Swap with previous step
#                 steps[idx], steps[idx - 1] = steps[idx - 1], steps[idx]
#                 # Renumber
#                 for i, s in enumerate(steps, start=1):
#                     s["number"] = i
#                 new_idx = idx - 1
#                 choices = _build_step_choices(steps)
#                 return (
#                     gr.update(value=steps),
#                     gr.update(value=_format_chain_preview_html(steps)),
#                     gr.update(value=_update_chain_size_indicator(steps, size_limit)),
#                     gr.update(choices=choices, value=choices[new_idx] if new_idx < len(choices) else None),
#                 )

#             move_up_btn.click(
#                 _move_step_up,
#                 inputs=[steps_state, selected_step_dropdown, chain_size_limit_input],
#                 outputs=[steps_state, chain_preview, chain_size_indicator, selected_step_dropdown],
#             )

#             # -- Move step down --
#             def _move_step_down(steps, choice, size_limit):
#                 steps = list(steps or [])
#                 step_num = _step_number_from_choice(choice)
#                 if step_num is None or step_num < 1 or step_num >= len(steps):
#                     return gr.update(), gr.update(), gr.update(), gr.update()
#                 idx = step_num - 1
#                 # Swap with next step
#                 steps[idx], steps[idx + 1] = steps[idx + 1], steps[idx]
#                 # Renumber
#                 for i, s in enumerate(steps, start=1):
#                     s["number"] = i
#                 new_idx = idx + 1
#                 choices = _build_step_choices(steps)
#                 return (
#                     gr.update(value=steps),
#                     gr.update(value=_format_chain_preview_html(steps)),
#                     gr.update(value=_update_chain_size_indicator(steps, size_limit)),
#                     gr.update(choices=choices, value=choices[new_idx] if new_idx < len(choices) else None),
#                 )

#             move_down_btn.click(
#                 _move_step_down,
#                 inputs=[steps_state, selected_step_dropdown, chain_size_limit_input],
#                 outputs=[steps_state, chain_preview, chain_size_indicator, selected_step_dropdown],
#             )

#             # -- Duplicate step --
#             def _duplicate_step(steps, choice, size_limit):
#                 steps = list(steps or [])
#                 step_num = _step_number_from_choice(choice)
#                 if step_num is None or step_num < 1 or step_num > len(steps):
#                     return gr.update(), gr.update(), gr.update(), gr.update()
#                 # Size limit check
#                 try:
#                     if size_limit not in (None, "", 0) and int(size_limit) > 0 and len(steps) >= int(size_limit):
#                         return gr.update(), gr.update(), gr.update(), gr.update()
#                 except Exception:
#                     pass
#                 idx = step_num - 1
#                 new_step = dict(steps[idx])
#                 new_step["title"] = (new_step.get("title", "") or "") + " (copy)"
#                 new_step["frozen"] = False
#                 steps.insert(idx + 1, new_step)
#                 # Renumber
#                 for i, s in enumerate(steps, start=1):
#                     s["number"] = i
#                 choices = _build_step_choices(steps)
#                 return (
#                     gr.update(value=steps),
#                     gr.update(value=_format_chain_preview_html(steps)),
#                     gr.update(value=_update_chain_size_indicator(steps, size_limit)),
#                     gr.update(choices=choices, value=choices[idx + 1] if idx + 1 < len(choices) else None),
#                 )

#             duplicate_step_btn.click(
#                 _duplicate_step,
#                 inputs=[steps_state, selected_step_dropdown, chain_size_limit_input],
#                 outputs=[steps_state, chain_preview, chain_size_indicator, selected_step_dropdown],
#             )

#             # -- Save step --
#             def _save_step(
#                 steps, editing_idx,
#                 title, frozen_flag, deps_values,
#                 aim, reasoning_questions, ctx_raw, stage_action, example_reasoning,
#                 tool_name, tool_desc, tool_mapping_raw, tool_timeout,
#                 tr_type, tr_input_key, tr_expr, tr_output_format, tr_map_template,
#                 mem_op, mem_ns, mem_key, mem_val_src, mem_default,
#                 mcp_srv, mcp_tool, mcp_args_raw, mcp_argmap_raw, mcp_timeout,
#                 cond_branches_raw, cond_default, cond_ctx_key,
#                 so_schema_raw, so_prompt, so_model, so_temp,
#                 size_limit,
#             ):
#                 steps = list(steps or [])
#                 if editing_idx is None or editing_idx < 0 or editing_idx >= len(steps):
#                     return (
#                         gr.update(value=steps),
#                         gr.update(value=_format_chain_preview_html(steps)),
#                         gr.update(value=_update_chain_size_indicator(steps, size_limit)),
#                         gr.update(choices=_build_step_choices(steps)),
#                         gr.update(visible=False),
#                         None,
#                     )

#                 s = steps[editing_idx]
#                 stype = str(s.get("step_type", "LLM")).upper()

#                 s["title"] = str(title or "").strip()
#                 s["frozen"] = bool(frozen_flag)
#                 s["dependencies"] = _deps_from_values(deps_values or [])

#                 # LLM fields
#                 s["aim"] = str(aim or "").strip()
#                 s["reasoning_questions"] = str(reasoning_questions or "").strip()
#                 s["stage_action"] = str(stage_action or "").strip()
#                 s["example_reasoning"] = str(example_reasoning or "").strip()
#                 try:
#                     ctx = json.loads(ctx_raw) if ctx_raw else []
#                     if not isinstance(ctx, list):
#                         ctx = []
#                 except Exception:
#                     ctx = []
#                 s["step_context_queries"] = ctx

#                 # Type-specific config
#                 step_config = {}
#                 if stype == "TOOL":
#                     try:
#                         im = json.loads(tool_mapping_raw) if tool_mapping_raw else {}
#                         if not isinstance(im, dict):
#                             im = {}
#                     except Exception:
#                         im = {}
#                     step_config = {
#                         "tool_name": str(tool_name or ""),
#                         "tool_description": str(tool_desc or ""),
#                         "input_mapping": im,
#                         "timeout": int(tool_timeout) if tool_timeout not in (None, "") else 120,
#                     }
#                 elif stype == "TRANSFORM":
#                     step_config = {"transform_type": str(tr_type or "extract"), "input_key": str(tr_input_key or "$history[-1]")}
#                     if tr_expr:
#                         step_config["expression"] = str(tr_expr)
#                     if tr_output_format:
#                         step_config["output_format"] = str(tr_output_format)
#                     if tr_map_template:
#                         step_config["map_template"] = str(tr_map_template)
#                 elif stype == "MEMORY":
#                     step_config = {"operation": str(mem_op or "read"), "namespace": str(mem_ns or "default"), "memory_key": str(mem_key or "")}
#                     if mem_op in ("write", "append") and mem_val_src:
#                         step_config["value_source"] = str(mem_val_src)
#                     if mem_default:
#                         try:
#                             step_config["default_value"] = json.loads(mem_default)
#                         except Exception:
#                             step_config["default_value"] = str(mem_default)
#                 elif stype == "MCP":
#                     try:
#                         args = json.loads(mcp_args_raw) if mcp_args_raw else {}
#                     except Exception:
#                         args = {}
#                     try:
#                         argmap = json.loads(mcp_argmap_raw) if mcp_argmap_raw else {}
#                     except Exception:
#                         argmap = {}
#                     step_config = {
#                         "server": {"server_name": str(mcp_srv or ""), "transport": "stdio"},
#                         "tool_name": str(mcp_tool or ""),
#                         "arguments": args, "argument_mapping": argmap,
#                         "timeout": float(mcp_timeout) if mcp_timeout not in (None, "") else 60.0,
#                     }
#                 elif stype == "CONDITIONAL":
#                     try:
#                         branches = json.loads(cond_branches_raw) if cond_branches_raw else []
#                     except Exception:
#                         branches = []
#                     step_config = {
#                         "branches": branches,
#                         "default_step": int(cond_default) if cond_default not in (None, "") else None,
#                         "condition_context_key": str(cond_ctx_key or "$history[-1]"),
#                     }
#                 elif stype == "STRUCTURED_OUTPUT":
#                     try:
#                         schema = json.loads(so_schema_raw) if so_schema_raw else {}
#                     except Exception:
#                         schema = {}
#                     step_config = {"output_schema": schema}
#                     if so_prompt:
#                         step_config["prompt_template"] = str(so_prompt)
#                     if so_model:
#                         step_config["model"] = str(so_model)
#                     if so_temp is not None and so_temp != "":
#                         try:
#                             step_config["temperature"] = float(so_temp)
#                         except Exception:
#                             pass

#                 if step_config:
#                     s["step_config"] = step_config
#                 elif "step_config" in s:
#                     del s["step_config"]

#                 choices = _build_step_choices(steps)
#                 return (
#                     gr.update(value=steps),
#                     gr.update(value=_format_chain_preview_html(steps)),
#                     gr.update(value=_update_chain_size_indicator(steps, size_limit)),
#                     gr.update(choices=choices, value=choices[editing_idx] if editing_idx < len(choices) else None),
#                     gr.update(visible=False),
#                     None,
#                 )

#             save_step_btn.click(
#                 _save_step,
#                 inputs=[
#                     steps_state, current_editing_step,
#                     title_input, frozen_checkbox, dependencies_checkboxgroup,
#                     aim_input, reasoning_questions_input, step_context_queries_input,
#                     stage_action_input, example_reasoning_input,
#                     tool_name_input, tool_description_input, tool_input_mapping_input, tool_timeout_input,
#                     transform_type_input, transform_input_key_input, transform_expression_input,
#                     transform_output_format_input, transform_map_template_input,
#                     memory_operation_input, memory_namespace_input, memory_key_input,
#                     memory_value_source_input, memory_default_value_input,
#                     mcp_server_name_input, mcp_tool_name_input, mcp_arguments_input,
#                     mcp_argument_mapping_input, mcp_timeout_input,
#                     conditional_branches_input, conditional_default_step_input,
#                     conditional_condition_context_key_input,
#                     structured_output_schema_input, structured_output_prompt_input,
#                     structured_output_model_input, structured_output_temperature_input,
#                     chain_size_limit_input,
#                 ],
#                 outputs=[
#                     steps_state, chain_preview, chain_size_indicator,
#                     selected_step_dropdown, step_form_group, current_editing_step,
#                 ],
#             )

#             # -- Cancel editing --
#             cancel_step_btn.click(
#                 lambda: (gr.update(visible=False), None),
#                 outputs=[step_form_group, current_editing_step],
#             )

#             # -- Evolution mode toggle --
#             evolution_mode_input.change(
#                 lambda mode: gr.update(visible=(mode == "single_step")),
#                 inputs=[evolution_mode_input],
#                 outputs=[evolution_step_number_input],
#             )

#             # -- Chain size limit change --
#             chain_size_limit_input.change(
#                 lambda steps, lim: gr.update(value=_update_chain_size_indicator(steps, lim)),
#                 inputs=[steps_state, chain_size_limit_input],
#                 outputs=[chain_size_indicator],
#             )

#             # -- Quick templates --
#             def _apply_template(tpl_name: str, size_limit):
#                 tpl = CHAIN_TEMPLATES.get(tpl_name, {})
#                 steps = [dict(s) for s in tpl.get("steps", [])]  # deep copy
#                 choices = _build_step_choices(steps)
#                 return (
#                     gr.update(value=steps),
#                     gr.update(value=_format_chain_preview_html(steps)),
#                     gr.update(value=_update_chain_size_indicator(steps, size_limit)),
#                     gr.update(choices=choices, value=choices[0] if choices else None),
#                     gr.update(visible=False),
#                     None,
#                 )

#             for tpl_name, btn in template_btns.items():
#                 # Capture tpl_name in closure
#                 btn.click(
#                     lambda sl, _tn=tpl_name: _apply_template(_tn, sl),
#                     inputs=[chain_size_limit_input],
#                     outputs=[
#                         steps_state, chain_preview, chain_size_indicator,
#                         selected_step_dropdown, step_form_group, current_editing_step,
#                     ],
#                 )

#             # ── JSON Editor ↔ Visual Builder sync ─────────────────────

#             def _export_steps_to_json(steps):
#                 """Visual Builder → JSON Editor."""
#                 steps = steps or []
#                 json_text = _steps_to_chain_json(steps)
#                 return (
#                     gr.update(value=json_text),
#                     '<div style="color:#27AE60;font-size:12px;">✅ Exported from Visual Builder</div>',
#                 )

#             export_json_btn.click(
#                 _export_steps_to_json,
#                 inputs=[steps_state],
#                 outputs=[chain_json_editor, json_status],
#             )

#             def _apply_json_to_visual(json_text, size_limit):
#                 """JSON Editor → Visual Builder."""
#                 try:
#                     steps = _chain_json_to_steps(json_text)
#                     if not steps:
#                         return (
#                             gr.update(), gr.update(), gr.update(), gr.update(),
#                             gr.update(visible=False), None,
#                             '<div style="color:#E74C3C;font-size:12px;">❌ No valid steps found in JSON</div>',
#                         )
#                     choices = _build_step_choices(steps)
#                     return (
#                         gr.update(value=steps),
#                         gr.update(value=_format_chain_preview_html(steps)),
#                         gr.update(value=_update_chain_size_indicator(steps, size_limit)),
#                         gr.update(choices=choices, value=choices[0] if choices else None),
#                         gr.update(visible=False),
#                         None,
#                         '<div style="color:#27AE60;font-size:12px;">✅ Applied to Visual Builder</div>',
#                     )
#                 except Exception as e:
#                     return (
#                         gr.update(), gr.update(), gr.update(), gr.update(),
#                         gr.update(), None,
#                         f'<div style="color:#E74C3C;font-size:12px;">❌ JSON error: {e}</div>',
#                     )

#             apply_json_btn.click(
#                 _apply_json_to_visual,
#                 inputs=[chain_json_editor, chain_size_limit_input],
#                 outputs=[
#                     steps_state, chain_preview, chain_size_indicator,
#                     selected_step_dropdown, step_form_group, current_editing_step,
#                     json_status,
#                 ],
#             )

#             # ── Generic preset helper ────────────────────────────────
#             _preset_outputs = [
#                 name_input,
#                 description_input,
#                 data_file_input,
#                 dataset_info,
#                 target_field_input,
#                 max_iterations_input,
#                 chain_size_limit_input,
#                 dataset_size_input,
#                 test_size_input,
#                 evolution_mode_input,
#                 evolution_step_number_input,
#                 dataset_path_state,
#                 create_output,
#                 steps_state,
#                 chain_preview,
#                 chain_size_indicator,
#                 selected_step_dropdown,
#                 step_form_group,
#                 current_editing_step,
#             ]

#             def _make_preset(
#                 preset_name: str,
#                 preset_description: str,
#                 template_key: str,
#                 chain_limit: int,
#                 target_field: str = "answer",
#                 max_iters: int = 100,
#                 test_sz: float = 0.2,
#                 evo_mode: str = "full_chain",
#                 demo_dataset: str = "hotpotqa_demo",
#             ):
#                 """Generic factory that builds a one-click preset callback."""
#                 def _preset_fn():
#                     (
#                         steps_update,
#                         preview_update,
#                         indicator_update,
#                         dropdown_update,
#                         form_hide_update,
#                         editing_update,
#                     ) = _apply_template(template_key, chain_limit)

#                     # Auto-upload demo dataset
#                     dataset_path = ""
#                     dataset_info_update = gr.update(value="Loading demo dataset…")
#                     create_output_update = gr.update(value="")
#                     try:
#                         up = self.exp_manager.upload_example_dataset(demo_dataset)
#                         if isinstance(up, dict) and up.get("data_path") and "error" not in up:
#                             dataset_path = str(up.get("data_path") or "")
#                             fname = str(up.get("filename") or f"{demo_dataset}.csv")
#                             dataset_info_update = gr.update(value=f"📁 Using demo dataset: {fname}")
#                         else:
#                             err = up.get("error") if isinstance(up, dict) else "unknown_error"
#                             dataset_info_update = gr.update(value="⚠️ Failed to auto-load demo dataset")
#                             create_output_update = gr.update(value=f"❌ Failed to load demo dataset: {err}")
#                     except Exception as e:
#                         dataset_info_update = gr.update(value="⚠️ Failed to auto-load demo dataset")
#                         create_output_update = gr.update(value=f"❌ Failed to load demo dataset: {e}")

#                     return (
#                         gr.update(value=preset_name),           # name_input (Textbox)
#                         gr.update(value=preset_description),    # description_input (Textbox)
#                         gr.update(value=None),                  # data_file_input (File)
#                         dataset_info_update,                    # dataset_info (Textbox)
#                         gr.update(value=target_field),          # target_field_input (Textbox)
#                         gr.update(value=max_iters),             # max_iterations_input (Slider)
#                         gr.update(value=chain_limit),           # chain_size_limit_input (Number)
#                         gr.update(value=None),                  # dataset_size_input (Number)
#                         gr.update(value=test_sz),               # test_size_input (Number)
#                         gr.update(value=evo_mode),              # evolution_mode_input (Radio)
#                         gr.update(value=None, visible=False),   # evolution_step_number_input
#                         dataset_path,                           # dataset_path_state (gr.State — raw value!)
#                         create_output_update,                   # create_output (Textbox)
#                         steps_update,                           # steps_state (gr.State — raw value!)
#                         preview_update,                         # chain_preview (HTML)
#                         indicator_update,                       # chain_size_indicator (HTML)
#                         dropdown_update,                        # selected_step_dropdown (Dropdown)
#                         form_hide_update,                       # step_form_group (Group)
#                         editing_update,                         # current_editing_step (gr.State — raw value!)
#                     )
#                 return _preset_fn

#             btn_hotpotqa_one_click.click(
#                 _make_preset(
#                     "HotpotQA CARL Evolution (LLM-only QA)",
#                     "Multi-step QA: analyse → reason → answer. LLM-only chain — no external tools required.",
#                     "HotpotQA (LLM-only QA)", chain_limit=6,
#                 ),
#                 outputs=_preset_outputs,
#             )

#             btn_tool_preset.click(
#                 _make_preset(
#                     "QA + Retrieve TOOL",
#                     "TOOL step (built-in TF-IDF retrieve, frozen) + LLM reasoning. "
#                     "Tests TOOL step execution during evolution — no external services required.",
#                     "QA + Retrieve TOOL", chain_limit=4,
#                 ),
#                 outputs=_preset_outputs,
#             )

#             btn_transform_preset.click(
#                 _make_preset(
#                     "QA + Transform (extract)",
#                     "LLM produces reasoning → TRANSFORM step extracts clean answer. "
#                     "Tests TRANSFORM step handling and evolution.",
#                     "QA + Transform (extract)", chain_limit=4,
#                 ),
#                 outputs=_preset_outputs,
#             )

#             btn_conditional_preset.click(
#                 _make_preset(
#                     "QA + Conditional Routing",
#                     "LLM classifies question → CONDITIONAL routes to quick vs deep path → LLM answers. "
#                     "Tests CONDITIONAL branching during evolution.",
#                     "QA + Conditional routing", chain_limit=6,
#                 ),
#                 outputs=_preset_outputs,
#             )

#             btn_mixed_preset.click(
#                 _make_preset(
#                     "Mixed Pipeline: TOOL + LLM + Transform",
#                     "Full pipeline: TOOL retrieves context → LLM reasons → TRANSFORM extracts answer. "
#                     "Tests all three non-LLM step types in one chain.",
#                     "LLM + TOOL + Transform pipeline", chain_limit=5,
#                 ),
#                 outputs=_preset_outputs,
#             )

#             # -- Create experiment --
#             def _create(name, description, data_file, max_iterations, llm_model,
#                         chain_llm_model,
#                         target_field, steps, chain_size_limit, dataset_size, test_size,
#                         dataset_path, evolution_mode, evolution_step_number):
#                 try:
#                     if not name:
#                         return "❌ Please enter an experiment name"
#                     if not target_field:
#                         return "❌ Please specify the target column"
#                     steps = steps or []
#                     if not steps:
#                         return "❌ Please add at least one step to the chain"

#                     evo_mode = str(evolution_mode or "full_chain").strip()
#                     if evo_mode == "single_step":
#                         if evolution_step_number in (None, "", 0):
#                             return "❌ Please provide a step number for single_step mode"
#                         evo_step = int(evolution_step_number)
#                         if evo_step < 1 or evo_step > len(steps):
#                             return f"❌ Step number {evo_step} is out of range (1-{len(steps)})"

#                     steps_for_json = []
#                     frozen_steps = []
#                     for s in steps:
#                         s_clean = {
#                             "number": int(s.get("number", 0)),
#                             "title": s.get("title", ""),
#                             "aim": s.get("aim", ""),
#                             "reasoning_questions": s.get("reasoning_questions", ""),
#                             "dependencies": s.get("dependencies", []) or [],
#                             "step_context_queries": s.get("step_context_queries", []) or [],
#                             "stage_action": s.get("stage_action", ""),
#                             "example_reasoning": s.get("example_reasoning", ""),
#                         }
#                         # IMPORTANT: CARL (mmar_carl) step_type values are lowercase ("tool", "mcp", ...).
#                         # The UI uses uppercase labels; normalize here before sending to Master API/Runner.
#                         stype_ui = str(s.get("step_type", "LLM")).upper()
#                         if stype_ui != "LLM":
#                             s_clean["step_type"] = stype_ui.lower()
#                             step_cfg = dict(s.get("step_config", {}) or {})
#                             # Normalize tool_name casing for env-registered tools (registered as lowercase).
#                             if s_clean["step_type"] == "tool" and "tool_name" in step_cfg and isinstance(step_cfg["tool_name"], str):
#                                 step_cfg["tool_name"] = step_cfg["tool_name"].strip().lower()
#                             s_clean["step_config"] = step_cfg
#                         steps_for_json.append(s_clean)
#                         if s.get("frozen", False) and s_clean["number"]:
#                             frozen_steps.append(int(s_clean["number"]))

#                     chain_cfg = {"steps": steps_for_json}
#                     payload = {
#                         "name": name,
#                         "description": description,
#                         "target_column": target_field,
#                         "base_chain_config": json.dumps(chain_cfg, indent=2),
#                         "llm_model": llm_model,
#                         "max_iterations": int(max_iterations),
#                         "frozen_steps": sorted(set(frozen_steps)) if frozen_steps else None,
#                         "chain_size_limit": int(chain_size_limit) if chain_size_limit not in (None, "", 0) else None,
#                         "evolution_mode": evo_mode,
#                     }

#                     # Chain execution model (optional, different from evolution model)
#                     if chain_llm_model and str(chain_llm_model).strip():
#                         payload["chain_llm_model"] = str(chain_llm_model).strip()

#                     if evo_mode == "single_step" and evolution_step_number not in (None, "", 0):
#                         payload["step_number"] = int(evolution_step_number)

#                     if (data_file and hasattr(data_file, "name")) and not dataset_path:
#                         try:
#                             fname = os.path.basename(getattr(data_file, "name", "dataset.csv"))
#                             res = self.exp_manager.upload_data_file(getattr(data_file, "name"), fname)
#                             dataset_path = res.get("data_path") or ""
#                         except Exception as ue:
#                             logger.error(f"Failed to upload data file: {ue}")
#                             dataset_path = ""
#                     if dataset_path:
#                         payload["data_path"] = dataset_path
#                     else:
#                         return "❌ Please upload a dataset file"
#                     if dataset_size not in (None, "", 0):
#                         try:
#                             payload["dataset_size"] = int(dataset_size)
#                         except Exception:
#                             pass
#                     if test_size not in (None, ""):
#                         try:
#                             payload["test_size"] = float(test_size)
#                         except Exception:
#                             pass
#                     result = self.exp_manager.create_carl_chain_experiment(payload)
#                     if "error" in result:
#                         return f"❌ {result['error']}"
#                     return f"✅ Experiment '{name}' created. ID: {result.get('id', 'n/a')}"
#                 except Exception as e:
#                     logger.error(f"Failed to create CARL experiment: {e}", exc_info=True)
#                     return f"❌ {str(e)}"

#             create_btn.click(
#                 _create,
#                 inputs=[
#                     name_input, description_input, data_file_input,
#                     max_iterations_input, llm_model_input, chain_llm_model_input,
#                     target_field_input,
#                     steps_state, chain_size_limit_input, dataset_size_input,
#                     test_size_input, dataset_path_state, evolution_mode_input,
#                     evolution_step_number_input,
#                 ],
#                 outputs=[create_output],
#             )

#             # -- Clean form --
#             def _clean():
#                 return (
#                     gr.update(value=""), gr.update(value=""), gr.update(value=None),
#                     gr.update(value="No file selected"), gr.update(value=""),
#                     gr.update(value=[]),
#                     gr.update(value=_format_chain_preview_html([])),
#                     gr.update(value=_update_chain_size_indicator([], None)),
#                     gr.update(choices=[], value=None),
#                     gr.update(value=""), gr.update(value=None), gr.update(value=0.2),
#                     gr.update(value=""), gr.update(visible=False), None,
#                     gr.update(value='{"steps": []}'),  # chain_json_editor
#                     gr.update(value=_PYTHON_TEMPLATE),  # python_code_editor
#                 )

#             clean_btn.click(
#                 _clean,
#                 outputs=[
#                     name_input, description_input, data_file_input,
#                     dataset_info, target_field_input,
#                     steps_state, chain_preview, chain_size_indicator,
#                     selected_step_dropdown, dataset_path_state,
#                     dataset_size_input, test_size_input,
#                     create_output, step_form_group, current_editing_step,
#                     chain_json_editor, python_code_editor,
#                 ],
#             )

#         return component

import json
import os
from typing import Optional, Dict, Any, List

import gradio as gr
from loguru import logger

from .base import BaseComponent
from common.llm_registry import get_default_llm_model_id, get_llm_model_choices
from config.settings import STORAGE_BUCKET_NAME
from utils.file_handlers import count_csv_rows, download_preset_dataset, read_csv_columns
from utils.validators import get_default_target_choices


# Dynamic step type loading - will be populated from CARL library
CARL_STEP_TYPES: list[str] = []

# Fallback step type hints (used when CARL library is unavailable)
FALLBACK_STEP_TYPE_HINTS = {
    "LLM": {
        "title": "LLM Step",
        "description": "Generates text, prompts, or responses using a language model.",
        "fields": {
            "aim": "Clear statement of what this step should accomplish.",
            "reasoning_questions": "Questions that guide the LLM's reasoning process.",
            "stage_action": "Detailed description of the action to be performed.",
            "example_reasoning": "Example or template of how reasoning should proceed.",
        },
    },
    "TOOL": {
        "title": "TOOL Step",
        "description": "Calls an external tool or function registered in the ReasoningContext.",
        "fields": {
            "tool_name": "Name of the tool to call.",
            "tool_description": "Description of what the tool does.",
            "input_mapping": "JSON object mapping input parameters. Use @N.output or @context.field.",
            "timeout": "Timeout in seconds for tool execution. Default is 120.",
        },
    },
    "TRANSFORM": {
        "title": "TRANSFORM Step",
        "description": "Transforms output data from previous steps using expressions.",
        "fields": {
            "transform_type": "Type of transformation: python_expr, jmespath, extract, etc.",
            "expression": "Expression to evaluate.",
            "output_key": "Key name to store the result.",
        },
    },
    "MEMORY": {
        "title": "MEMORY Step",
        "description": "Reads from or writes to shared memory for use across steps.",
        "fields": {
            "operation": "Operation type: read or write.",
            "namespace": "Namespace for organizing memory data.",
            "key": "Key identifier for the memory entry.",
            "value_expr": "Expression for the value to write.",
        },
    },
    "MCP": {
        "title": "MCP Step",
        "description": "Calls a procedure via Model Context Protocol (MCP).",
        "fields": {
            "server": "Name of the MCP server.",
            "procedure": "Name of the procedure to call.",
            "params": "JSON object with parameters.",
            "timeout": "Timeout in seconds.",
        },
    },
    "CONDITIONAL": {
        "title": "CONDITIONAL Step",
        "description": "Conditionally branches execution based on conditions.",
        "fields": {
            "condition": "Condition expression to evaluate.",
            "then_step": "Step number if condition is true.",
            "else_step": "Step number if condition is false.",
        },
    },
    "STRUCTURED_OUTPUT": {
        "title": "STRUCTURED_OUTPUT Step",
        "description": "Generates JSON output constrained by a JSON Schema.",
        "fields": {
            "output_schema": "JSON Schema defining the expected output structure.",
            "prompt_template": "Template for the prompt sent to the LLM.",
            "model": "Optional LLM model override.",
            "temperature": "Temperature for generation (0.0-2.0).",
        },
    },
}

# Icons for step types
STEP_TYPE_ICONS = {
    "LLM": "🧠",
    "TOOL": "🔧",
    "MCP": "🔌",
    "MEMORY": "💾",
    "TRANSFORM": "🔄",
    "CONDITIONAL": "🔀",
    "STRUCTURED_OUTPUT": "📋",
}

# Quick chain templates
CHAIN_TEMPLATES = {
    "Two-Step Reasoning": {
        "description": "Analysis → Solution (2 LLM steps) — weak baseline",
        "steps": [
            {
                "number": 1,
                "step_type": "LLM",
                "title": "Look at the problem",
                "aim": "Read the problem.",
                "reasoning_questions": "What is given?",
                "dependencies": [],
                "step_context_queries": ["problem"],
                "stage_action": "Read what is provided.",
                "example_reasoning": "",
                "frozen": False,
            },
            {
                "number": 2,
                "step_type": "LLM",
                "title": "Give answer",
                "aim": "Produce an answer.",
                "reasoning_questions": "What could the answer be?",
                "dependencies": [1],
                "step_context_queries": ["problem"],
                "stage_action": "Try to answer.",
                "example_reasoning": "",
                "frozen": False,
            },
        ],
    },
    "LLM + Tool Eval": {
        "description": "LLM generates → Tool evaluates (frozen)",
        "steps": [
            {
                "number": 1,
                "step_type": "LLM",
                "title": "Generate Output",
                "aim": "Produce some output.",
                "reasoning_questions": "What is expected?",
                "dependencies": [],
                "step_context_queries": ["problem"],
                "stage_action": "Generate output for the tool.",
                "example_reasoning": "",
                "frozen": False,
            },
            {
                "number": 2,
                "step_type": "TOOL",
                "title": "Evaluate",
                "aim": "",
                "reasoning_questions": "",
                "dependencies": [1],
                "step_context_queries": [],
                "stage_action": "",
                "example_reasoning": "",
                "frozen": True,
                "step_config": {
                    "tool_name": "",
                    "tool_description": "Evaluation tool",
                    "input_mapping": {"input": "@1.output"},
                    "timeout": 120,
                },
            },
        ],
    },
    "Three-Step CoT": {
        "description": "Understand → Plan → Execute (Chain-of-Thought) — weak baseline",
        "steps": [
            {
                "number": 1,
                "step_type": "LLM",
                "title": "Read",
                "aim": "Look at the problem.",
                "reasoning_questions": "What is this about?",
                "dependencies": [],
                "step_context_queries": [],
                "stage_action": "Read the problem text.",
                "example_reasoning": "",
                "frozen": False,
            },
            {
                "number": 2,
                "step_type": "LLM",
                "title": "Think",
                "aim": "Think about it.",
                "reasoning_questions": "How could this be solved?",
                "dependencies": [1],
                "step_context_queries": ["step_1_output"],
                "stage_action": "Consider possible approaches.",
                "example_reasoning": "",
                "frozen": False,
            },
            {
                "number": 3,
                "step_type": "LLM",
                "title": "Answer",
                "aim": "Give the answer.",
                "reasoning_questions": "What is the answer?",
                "dependencies": [1, 2],
                "step_context_queries": ["step_1_output", "step_2_output"],
                "stage_action": "Provide your answer.",
                "example_reasoning": "",
                "frozen": False,
            },
        ],
    },
    "HotpotQA (LLM-only QA)": {
        "description": "LLM-only multi-step QA chain — weak baseline. No external tools required.",
        "steps": [
            {
                "number": 1,
                "step_type": "LLM",
                "title": "Read question",
                "aim": "Read the question.",
                "reasoning_questions": "What is the question about?",
                "dependencies": [],
                "step_context_queries": ["problem"],
                "stage_action": "Read what is being asked.",
                "example_reasoning": "",
                "frozen": False,
            },
            {
                "number": 2,
                "step_type": "LLM",
                "title": "Think",
                "aim": "Think about the answer.",
                "reasoning_questions": "What might the answer be?",
                "dependencies": [1],
                "step_context_queries": ["problem"],
                "stage_action": "Consider what you know and try to work toward an answer.",
                "example_reasoning": "",
                "frozen": False,
            },
            {
                "number": 3,
                "step_type": "LLM",
                "title": "Answer",
                "aim": "Produce an answer.",
                "reasoning_questions": "What is the answer?",
                "dependencies": [1, 2],
                "step_context_queries": ["problem"],
                "stage_action": "Write your answer. Format: Answer: <your answer>",
                "example_reasoning": "",
                "frozen": False,
            },
        ],
    },
    "HotpotQA (2-Hop Retrieval, static topology)": {
        "description": "HotpotQA-style chain with retrieve tool — weak baseline. Tool steps are frozen. Requires CARL_TOOL__RETRIEVE_URL.",
        "steps": [
            {
                "number": 1,
                "step_type": "TOOL",
                "title": "Retrieve first-hop passages.",
                "aim": "",
                "reasoning_questions": "",
                "dependencies": [],
                "step_context_queries": [],
                "stage_action": "",
                "example_reasoning": "",
                "frozen": True,
                "step_config": {
                    "tool_name": "retrieve",
                    "tool_description": "BM25 retrieval tool (must be registered via CARL_TOOL__RETRIEVE_URL).",
                    "input_mapping": {"query": "$outer_context"},
                    "timeout": 120,
                },
            },
            {
                "number": 2,
                "step_type": "LLM",
                "title": "Read passages",
                "aim": "Look at retrieved text.",
                "reasoning_questions": "What is in the passages?",
                "dependencies": [1],
                "step_context_queries": ["problem"],
                "stage_action": "Skim the passages.",
                "example_reasoning": "",
                "frozen": False,
            },
            {
                "number": 3,
                "step_type": "LLM",
                "title": "Second query",
                "aim": "Write a follow-up search query.",
                "reasoning_questions": "What else could help?",
                "dependencies": [2],
                "step_context_queries": ["problem"],
                "stage_action": "Write a search query.\nProvide ONLY the search query, no additional text.",
                "example_reasoning": "",
                "frozen": False,
            },
            {
                "number": 4,
                "step_type": "TOOL",
                "title": "Retrieve second-hop passages.",
                "aim": "",
                "reasoning_questions": "",
                "dependencies": [3],
                "step_context_queries": [],
                "stage_action": "",
                "example_reasoning": "",
                "frozen": True,
                "step_config": {
                    "tool_name": "retrieve",
                    "tool_description": "BM25 retrieval tool (must be registered via CARL_TOOL__RETRIEVE_URL).",
                    "input_mapping": {"query": "$history[-1]"},
                    "timeout": 120,
                },
            },
            {
                "number": 5,
                "step_type": "LLM",
                "title": "Combine",
                "aim": "Put information together.",
                "reasoning_questions": "What do I have now?",
                "dependencies": [2, 4],
                "step_context_queries": ["problem"],
                "stage_action": "Combine what you found.",
                "example_reasoning": "",
                "frozen": False,
            },
            {
                "number": 6,
                "step_type": "LLM",
                "title": "Answer",
                "aim": "Give an answer.",
                "reasoning_questions": "What is the answer?",
                "dependencies": [2, 5],
                "step_context_queries": ["problem"],
                "stage_action": "Answer the question. Format: Answer: <your answer>",
                "example_reasoning": "",
                "frozen": False,
            },
        ],
    },
    "HotpotQA (2-Hop Retrieval, full chain)": {
        "description": "Same as the retrieval baseline, but all steps are evolvable. Requires CARL_TOOL__RETRIEVE_URL.",
        "steps": [
            {
                "number": 1,
                "step_type": "TOOL",
                "title": "Retrieve first-hop passages.",
                "aim": "",
                "reasoning_questions": "",
                "dependencies": [],
                "step_context_queries": [],
                "stage_action": "",
                "example_reasoning": "",
                "frozen": False,
                "step_config": {
                    "tool_name": "retrieve",
                    "tool_description": "BM25 retrieval tool (must be registered via CARL_TOOL__RETRIEVE_URL).",
                    "input_mapping": {"query": "$outer_context"},
                    "timeout": 120,
                },
            },
            {
                "number": 2,
                "step_type": "LLM",
                "title": "Read passages",
                "aim": "Look at retrieved text.",
                "reasoning_questions": "What is in the passages?",
                "dependencies": [1],
                "step_context_queries": ["problem"],
                "stage_action": "Skim the passages.",
                "example_reasoning": "",
                "frozen": False,
            },
            {
                "number": 3,
                "step_type": "LLM",
                "title": "Second query",
                "aim": "Write a follow-up search query.",
                "reasoning_questions": "What else could help?",
                "dependencies": [2],
                "step_context_queries": ["problem"],
                "stage_action": "Write a search query.\nProvide ONLY the search query, no additional text.",
                "example_reasoning": "",
                "frozen": False,
            },
            {
                "number": 4,
                "step_type": "TOOL",
                "title": "Retrieve second-hop passages.",
                "aim": "",
                "reasoning_questions": "",
                "dependencies": [3],
                "step_context_queries": [],
                "stage_action": "",
                "example_reasoning": "",
                "frozen": False,
                "step_config": {
                    "tool_name": "retrieve",
                    "tool_description": "BM25 retrieval tool (must be registered via CARL_TOOL__RETRIEVE_URL).",
                    "input_mapping": {"query": "$history[-1]"},
                    "timeout": 120,
                },
            },
            {
                "number": 5,
                "step_type": "LLM",
                "title": "Combine",
                "aim": "Put information together.",
                "reasoning_questions": "What do I have now?",
                "dependencies": [2, 4],
                "step_context_queries": ["problem"],
                "stage_action": "Combine what you found.",
                "example_reasoning": "",
                "frozen": False,
            },
            {
                "number": 6,
                "step_type": "LLM",
                "title": "Answer",
                "aim": "Give an answer.",
                "reasoning_questions": "What is the answer?",
                "dependencies": [2, 5],
                "step_context_queries": ["problem"],
                "stage_action": "Answer the question. Format: Answer: <your answer>",
                "example_reasoning": "",
                "frozen": False,
            },
        ],
    },
    # ── Sentiment Classification template ──
    "Sentiment Classification": {
        "description": "Sentiment analysis: analyze text → classify as positive/negative/neutral.",
        "steps": [
            {
                "number": 1,
                "step_type": "LLM",
                "title": "Analyze text sentiment indicators",
                "aim": "Identify sentiment-bearing words, phrases, and overall tone in the given text",
                "reasoning_questions": "What emotional words are present? Are there negations or intensifiers? What is the dominant emotion? Is the tone sarcastic or literal?",
                "dependencies": [],
                "step_context_queries": [],
                "stage_action": "Read the text carefully. List all sentiment-bearing words and phrases. Identify the dominant emotional tone. Note any negations, sarcasm, or mixed signals. Classify the overall sentiment as positive, negative, or neutral.",
                "example_reasoning": "The text contains words like 'excellent', 'love', and 'highly recommend' which are strongly positive. The overall sentiment is positive.",
                "frozen": False,
            },
            {
                "number": 2,
                "step_type": "LLM",
                "title": "Verify and output final sentiment",
                "aim": "Double-check the sentiment classification and output a single word: positive, negative, or neutral",
                "reasoning_questions": "Does the initial analysis correctly capture the overall tone? Are there any edge cases?",
                "dependencies": [1],
                "step_context_queries": ["step_1_output"],
                "stage_action": "Review the sentiment analysis from step 1. Verify it against the original text. Output ONLY a single word: 'positive', 'negative', or 'neutral'. Do not include any other text.",
                "example_reasoning": "Step 1 identified positive sentiment indicators. Confirmed. The sentiment is: positive",
                "frozen": False,
            },
        ],
    },
    # ── TOOL + LLM QA (lightweight) ──
    "TOOL + LLM QA": {
        "description": "Retrieve with TOOL → LLM answers from context. Lightweight 2-step chain.",
        "steps": [
            {
                "number": 1,
                "step_type": "TOOL",
                "title": "Retrieve relevant information",
                "aim": "",
                "reasoning_questions": "",
                "dependencies": [],
                "step_context_queries": [],
                "stage_action": "",
                "example_reasoning": "",
                "frozen": False,
                "step_config": {
                    "tool_name": "retrieve",
                    "tool_description": "Built-in retrieval tool that searches the dataset for passages relevant to the query.",
                    "input_mapping": {"query": "$outer_context"},
                    "timeout": 120,
                },
            },
            {
                "number": 2,
                "step_type": "LLM",
                "title": "Answer using retrieved context",
                "aim": "Read the question and retrieved passages, then produce a concise, accurate answer.",
                "reasoning_questions": "What is the question asking? Does the retrieved information contain the answer? What is the most concise correct answer?",
                "dependencies": [1],
                "step_context_queries": ["step_1_output"],
                "stage_action": "You are given a question and retrieved passages. Read the question carefully. Look through the retrieved passages to find the answer. Output ONLY the answer itself — a short phrase or name, not a full sentence. For example, if the question is 'What is the capital of France?', output 'Paris' not 'The capital of France is Paris.'",
                "example_reasoning": "Question: What is the capital of France? Retrieved passages mention Paris as the capital. Answer: Paris",
                "frozen": False,
            },
        ],
    },
    # ── Presets for testing TOOL / TRANSFORM / CONDITIONAL step types ──
    "QA + Retrieve TOOL": {
        "description": "LLM + TOOL chain — weak baseline. TOOL step is frozen; LLM step evolves.",
        "steps": [
            {
                "number": 1,
                "step_type": "TOOL",
                "title": "Retrieve relevant passages",
                "aim": "",
                "reasoning_questions": "",
                "dependencies": [],
                "step_context_queries": [],
                "stage_action": "",
                "example_reasoning": "",
                "frozen": True,
                "step_config": {
                    "tool_name": "retrieve",
                    "tool_description": "Built-in retrieval tool that searches the dataset for passages relevant to the query. Returns top matching passages.",
                    "input_mapping": {"query": "$outer_context"},
                    "timeout": 120,
                },
            },
            {
                "number": 2,
                "step_type": "LLM",
                "title": "Answer from passages",
                "aim": "Try to answer using passages.",
                "reasoning_questions": "Is anything relevant here?",
                "dependencies": [1],
                "step_context_queries": ["step_1_output"],
                "stage_action": "Look at the passages and try to answer. Format: Answer: <your answer>",
                "example_reasoning": "",
                "frozen": False,
            },
        ],
    },
    "QA + Transform (extract)": {
        "description": "LLM reasoning + TRANSFORM step to extract answer — weak baseline.",
        "steps": [
            {
                "number": 1,
                "step_type": "LLM",
                "title": "Think and answer",
                "aim": "Try to answer the question.",
                "reasoning_questions": "What is being asked?",
                "dependencies": [],
                "step_context_queries": [],
                "stage_action": "Think about the question and write something. End with: Answer: <your answer>",
                "example_reasoning": "",
                "frozen": False,
            },
            {
                "number": 2,
                "step_type": "TRANSFORM",
                "title": "Extract final answer",
                "aim": "",
                "reasoning_questions": "",
                "dependencies": [1],
                "step_context_queries": ["step_1_output"],
                "stage_action": "",
                "example_reasoning": "",
                "frozen": False,
                "step_config": {"transform_type": "extract", "pattern": "Answer:\\s*(.+?)(?:\\n|$)"},
            },
        ],
    },
    "QA + Conditional routing": {
        "description": "LLM classifies → CONDITIONAL routes → LLM answers — weak baseline.",
        "steps": [
            {
                "number": 1,
                "step_type": "LLM",
                "title": "Classify",
                "aim": "Decide question type.",
                "reasoning_questions": "Simple or complex?",
                "dependencies": [],
                "step_context_queries": [],
                "stage_action": "Output the single word 'simple' or 'complex', nothing else.",
                "example_reasoning": "",
                "frozen": False,
            },
            {
                "number": 2,
                "step_type": "CONDITIONAL",
                "title": "Route by complexity",
                "aim": "",
                "reasoning_questions": "",
                "dependencies": [1],
                "step_context_queries": ["step_1_output"],
                "stage_action": "",
                "example_reasoning": "",
                "frozen": True,
                "step_config": {
                    "condition_type": "contains",
                    "condition_value": "complex",
                    "true_branch_step": 4,
                    "false_branch_step": 3,
                },
            },
            {
                "number": 3,
                "step_type": "LLM",
                "title": "Quick answer",
                "aim": "Answer briefly.",
                "reasoning_questions": "What is the answer?",
                "dependencies": [1],
                "step_context_queries": ["step_1_output"],
                "stage_action": "Give a short answer. Format: Answer: <your answer>",
                "example_reasoning": "",
                "frozen": False,
            },
            {
                "number": 4,
                "step_type": "LLM",
                "title": "Detailed answer",
                "aim": "Answer with more thought.",
                "reasoning_questions": "What could the answer be?",
                "dependencies": [1],
                "step_context_queries": ["step_1_output"],
                "stage_action": "Think a bit and answer. Format: Answer: <your answer>",
                "example_reasoning": "",
                "frozen": False,
            },
        ],
    },
    "LLM + TOOL + Transform pipeline": {
        "description": "TOOL retrieves → LLM answers → TRANSFORM extracts — weak baseline.",
        "steps": [
            {
                "number": 1,
                "step_type": "TOOL",
                "title": "Retrieve context",
                "aim": "",
                "reasoning_questions": "",
                "dependencies": [],
                "step_context_queries": [],
                "stage_action": "",
                "example_reasoning": "",
                "frozen": True,
                "step_config": {
                    "tool_name": "retrieve",
                    "tool_description": "Built-in retrieval tool that searches the dataset for passages relevant to the query.",
                    "input_mapping": {"query": "$outer_context"},
                    "timeout": 120,
                },
            },
            {
                "number": 2,
                "step_type": "LLM",
                "title": "Answer",
                "aim": "Try to answer.",
                "reasoning_questions": "What do the passages say?",
                "dependencies": [1],
                "step_context_queries": ["step_1_output"],
                "stage_action": "Look at the passages and answer. End with: Answer: <your answer>",
                "example_reasoning": "",
                "frozen": False,
            },
            {
                "number": 3,
                "step_type": "TRANSFORM",
                "title": "Extract clean answer",
                "aim": "",
                "reasoning_questions": "",
                "dependencies": [2],
                "step_context_queries": ["step_2_output"],
                "stage_action": "",
                "example_reasoning": "",
                "frozen": False,
                "step_config": {"transform_type": "extract", "pattern": "Answer:\\s*(.+?)(?:\\n|$)"},
            },
        ],
    },
}

# Evolution presets - chains designed to start with moderate quality for auto-evolution
# These provide baseline performance while leaving room for significant improvement
EVOLUTION_PRESETS = {
    "🧬 Evolution: Basic 2-Step Chain": {
        "description": "Simple two-step chain with basic prompts - good starting point for most tasks (20-40% accuracy expected)",
        "evolution_mode": "full_chain",
        "steps": [
            {
                "number": 1,
                "step_type": "LLM",
                "title": "Analyze Problem",
                "aim": "Read and understand the problem. Identify what is being asked and what information is given.",
                "reasoning_questions": "What is the question asking for? What information do I have? What type of problem is this?",
                "dependencies": [],
                "step_context_queries": ["problem"],
                "stage_action": "Analyze the problem structure and identify the key components.",
                "example_reasoning": "The problem asks for a numerical answer. I can see numbers and operations in the question.",
                "frozen": False,
            },
            {
                "number": 2,
                "step_type": "LLM",
                "title": "Solve and Answer",
                "aim": "Solve the problem and provide the final answer in the requested format.",
                "reasoning_questions": "What method should I use? What steps are needed? What is the final answer?",
                "dependencies": [1],
                "step_context_queries": ["problem"],
                "stage_action": "Apply solution method and output the final answer clearly.",
                "example_reasoning": "I need to perform the calculation step by step and give the result.",
                "frozen": False,
            },
        ],
    },
    "🧬 Evolution: Improved CoT (3-Step)": {
        "description": "Chain-of-Thought with structured reasoning - moderate baseline with clear improvement paths (30-50% accuracy expected)",
        "evolution_mode": "full_chain",
        "steps": [
            {
                "number": 1,
                "step_type": "LLM",
                "title": "Understand Problem",
                "aim": "Carefully read and understand what the problem is asking. Identify the type of task and key information.",
                "reasoning_questions": "What type of problem is this? What are the key elements? What is the goal?",
                "dependencies": [],
                "step_context_queries": ["problem"],
                "stage_action": "Break down the problem and identify its components.",
                "example_reasoning": "This is a math problem requiring calculation. I need to identify the numbers and operations.",
                "frozen": False,
            },
            {
                "number": 2,
                "step_type": "LLM",
                "title": "Plan Solution",
                "aim": "Think through how to solve the problem step by step. Consider different approaches.",
                "reasoning_questions": "What steps should I take? What approach works best? Are there multiple ways to solve this?",
                "dependencies": [1],
                "step_context_queries": ["problem"],
                "stage_action": "Outline a solution approach with clear reasoning steps.",
                "example_reasoning": "First I'll extract the numbers, then apply the operation, finally verify the result.",
                "frozen": False,
            },
            {
                "number": 3,
                "step_type": "LLM",
                "title": "Execute and Answer",
                "aim": "Execute the solution plan and provide the final answer with proper formatting.",
                "reasoning_questions": "How do I execute each step? What is the final result? How should I format the answer?",
                "dependencies": [1, 2],
                "step_context_queries": ["problem"],
                "stage_action": "Follow the plan and output the final answer clearly.",
                "example_reasoning": "Following the plan: 5 + 3 = 8, so the answer is 8.",
                "frozen": False,
            },
        ],
    },
    "🧬 Evolution: QA with Retrieval": {
        "description": "Question-answering with retrieval tool - tests context usage and answer synthesis (25-45% accuracy expected)",
        "evolution_mode": "full_chain",
        "steps": [
            {
                "number": 1,
                "step_type": "TOOL",
                "title": "Retrieve Context",
                "aim": "",
                "reasoning_questions": "",
                "dependencies": [],
                "step_context_queries": [],
                "stage_action": "",
                "example_reasoning": "",
                "frozen": False,
                "step_config": {
                    "tool_name": "retrieve",
                    "tool_description": "Retrieve relevant passages from the knowledge base",
                    "input_mapping": {"query": "$outer_context"},
                    "timeout": 120,
                },
            },
            {
                "number": 2,
                "step_type": "LLM",
                "title": "Find Answer in Context",
                "aim": "Read the retrieved context and find the answer to the question.",
                "reasoning_questions": "What information is relevant? Where is the answer in the context? How does it relate to the question?",
                "dependencies": [1],
                "step_context_queries": ["problem"],
                "stage_action": "Analyze the retrieved information and extract the answer.",
                "example_reasoning": "The context contains relevant information. I need to find the specific answer.",
                "frozen": False,
            },
        ],
    },
    "🧬 Evolution: Multi-Strategy (4-Step)": {
        "description": "Four-step chain exploring multiple solution strategies - excellent for testing optimization (35-55% accuracy expected)",
        "evolution_mode": "full_chain",
        "steps": [
            {
                "number": 1,
                "step_type": "LLM",
                "title": "Strategy A - Direct",
                "aim": "Try to solve the problem using a direct, straightforward approach.",
                "reasoning_questions": "What is the most direct way to solve this? Can I answer immediately?",
                "dependencies": [],
                "step_context_queries": ["problem"],
                "stage_action": "Apply direct solution method.",
                "example_reasoning": "I'll solve this directly by performing the obvious calculation.",
                "frozen": False,
            },
            {
                "number": 2,
                "step_type": "LLM",
                "title": "Strategy B - Stepwise",
                "aim": "Try to solve the problem by breaking it into smaller steps.",
                "reasoning_questions": "How can I break this down? What are the intermediate steps?",
                "dependencies": [],
                "step_context_queries": ["problem"],
                "stage_action": "Apply stepwise decomposition method.",
                "example_reasoning": "I'll break this into smaller parts and solve each part separately.",
                "frozen": False,
            },
            {
                "number": 3,
                "step_type": "LLM",
                "title": "Compare and Select",
                "aim": "Compare the results from both strategies and select the better approach.",
                "reasoning_questions": "Which approach seems more reliable? What are the trade-offs? Which answer makes more sense?",
                "dependencies": [1, 2],
                "step_context_queries": ["problem"],
                "stage_action": "Evaluate both strategies and choose the best result.",
                "example_reasoning": "Comparing both approaches, the stepwise method seems more thorough.",
                "frozen": False,
            },
            {
                "number": 4,
                "step_type": "LLM",
                "title": "Final Answer",
                "aim": "Provide the final answer based on the best strategy, with clear reasoning.",
                "reasoning_questions": "What is the final answer? How should I present it? Is it complete and correct?",
                "dependencies": [3],
                "step_context_queries": ["problem"],
                "stage_action": "Output the final answer with justification.",
                "example_reasoning": "Based on the analysis, the answer is 42 using the stepwise approach.",
                "frozen": False,
            },
        ],
    },
    "🧬 Evolution: Single-Step Focus": {
        "description": "Evolves only the reasoning step - tests single-step evolution mode (40-60% accuracy expected)",
        "evolution_mode": "single_step",
        "step_number": 2,
        "steps": [
            {
                "number": 1,
                "step_type": "LLM",
                "title": "Problem Analysis",
                "aim": "Carefully analyze the problem and identify what's being asked. Extract key information and constraints.",
                "reasoning_questions": "What is given? What is being asked? What are the constraints?",
                "dependencies": [],
                "step_context_queries": ["problem"],
                "stage_action": "Provide a structured analysis of the problem.",
                "example_reasoning": "The problem provides numerical values and asks for a calculation result.",
                "frozen": True,
            },
            {
                "number": 2,
                "step_type": "LLM",
                "title": "Core Reasoning (EVOLVE THIS)",
                "aim": "Think through the solution approach and develop a step-by-step reasoning strategy.",
                "reasoning_questions": "What's the best approach? What steps are needed? How do I verify the answer?",
                "dependencies": [1],
                "step_context_queries": ["problem"],
                "stage_action": "Develop and execute a clear reasoning strategy.",
                "example_reasoning": "I need to apply the correct operations in the right order and verify the result.",
                "frozen": False,
            },
            {
                "number": 3,
                "step_type": "LLM",
                "title": "Final Answer",
                "aim": "Take the reasoning and produce a clear, correctly formatted final answer.",
                "reasoning_questions": "What is the final answer? How should it be formatted? Is it complete?",
                "dependencies": [2],
                "step_context_queries": ["problem"],
                "stage_action": "Format and present the final answer.",
                "example_reasoning": "The calculation gives 42, so the answer is 42.",
                "frozen": True,
            },
        ],
    },
    "🧬 Evolution: Math Specialist": {
        "description": "Specialized for mathematical problems - focuses on numerical reasoning (35-55% accuracy expected)",
        "evolution_mode": "full_chain",
        "steps": [
            {
                "number": 1,
                "step_type": "LLM",
                "title": "Extract Numbers",
                "aim": "Identify all numerical values and mathematical operations mentioned in the problem.",
                "reasoning_questions": "What numbers are given? What operations are needed? What is the goal?",
                "dependencies": [],
                "step_context_queries": ["problem"],
                "stage_action": "List the numbers and operations needed for solving.",
                "example_reasoning": "I see numbers 5 and 3, and I need to add them.",
                "frozen": False,
            },
            {
                "number": 2,
                "step_type": "LLM",
                "title": "Perform Calculation",
                "aim": "Perform the necessary mathematical operations step by step.",
                "reasoning_questions": "What's the first operation? What's next? How do I handle order of operations?",
                "dependencies": [1],
                "step_context_queries": ["problem"],
                "stage_action": "Execute calculations and show intermediate steps.",
                "example_reasoning": "First I add 5 + 3 = 8, then I check if there are more operations.",
                "frozen": False,
            },
            {
                "number": 3,
                "step_type": "LLM",
                "title": "Verify and Answer",
                "aim": "Verify the calculation makes sense and provide the final numerical answer.",
                "reasoning_questions": "Does the answer make sense? Did I miss anything? What's the final result?",
                "dependencies": [2],
                "step_context_queries": ["problem"],
                "stage_action": "Check the result and output the final answer.",
                "example_reasoning": "The calculation is correct. 5 + 3 = 8, so the answer is 8.",
                "frozen": False,
            },
        ],
    },
    "🧬 Evolution: Text Comprehension": {
        "description": "Optimized for text-based question answering - focuses on understanding and extraction (40-60% accuracy expected)",
        "evolution_mode": "full_chain",
        "steps": [
            {
                "number": 1,
                "step_type": "LLM",
                "title": "Comprehend Text",
                "aim": "Read and understand the given text. Identify the main topic and key information.",
                "reasoning_questions": "What is the text about? What are the key points? What information is relevant?",
                "dependencies": [],
                "step_context_queries": ["problem"],
                "stage_action": "Summarize the text and identify relevant information.",
                "example_reasoning": "The text discusses a topic and provides specific details that may answer the question.",
                "frozen": False,
            },
            {
                "number": 2,
                "step_type": "LLM",
                "title": "Extract Answer",
                "aim": "Find the answer to the question based on the text comprehension.",
                "reasoning_questions": "What is the question asking? Where is the answer in the text? How do I formulate the answer?",
                "dependencies": [1],
                "step_context_queries": ["problem"],
                "stage_action": "Locate and extract the specific answer from the text.",
                "example_reasoning": "Based on my understanding, the answer is found in the text.",
                "frozen": False,
            },
        ],
    },
    "🧬 Evolution: Memory-Enhanced Reasoning": {
        "description": "Tests memory operations with multi-step reasoning - complex baseline (30-50% accuracy expected)",
        "evolution_mode": "full_chain",
        "steps": [
            {
                "number": 1,
                "step_type": "LLM",
                "title": "Initial Analysis",
                "aim": "Analyze the problem and identify key information that needs to be remembered.",
                "reasoning_questions": "What's important to remember? What are the key components? How do I structure this?",
                "dependencies": [],
                "step_context_queries": ["problem"],
                "stage_action": "Extract and structure key information from the problem.",
                "example_reasoning": "I need to remember the key numbers and their relationships for later use.",
                "frozen": False,
            },
            {
                "number": 2,
                "step_type": "MEMORY",
                "title": "Store Key Information",
                "aim": "",
                "reasoning_questions": "",
                "dependencies": [1],
                "step_context_queries": [],
                "stage_action": "",
                "example_reasoning": "",
                "frozen": False,
                "step_config": {
                    "operation": "write",
                    "key": "problem_context",
                    "namespace": "default",
                    "value_source": "$history[-1]",
                },
            },
            {
                "number": 3,
                "step_type": "LLM",
                "title": "Apply Memory",
                "aim": "Use the stored information to reason about the solution.",
                "reasoning_questions": "What did I store? How does it help now? What's the next step?",
                "dependencies": [2],
                "step_context_queries": ["problem"],
                "stage_action": "Retrieve and use stored information for solution.",
                "example_reasoning": "Using the stored information, I can now work through the solution.",
                "frozen": False,
            },
        ],
    },
    "🧬 Evolution: Verification Chain": {
        "description": "Focuses on answer verification and self-correction - tests meta-cognitive abilities (40-60% accuracy expected)",
        "evolution_mode": "full_chain",
        "steps": [
            {
                "number": 1,
                "step_type": "LLM",
                "title": "Initial Solution",
                "aim": "Solve the problem and provide an initial answer.",
                "reasoning_questions": "What's the approach? What's the answer? Am I confident?",
                "dependencies": [],
                "step_context_queries": ["problem"],
                "stage_action": "Provide an initial solution with reasoning.",
                "example_reasoning": "I think the answer is 42 based on my calculation.",
                "frozen": False,
            },
            {
                "number": 2,
                "step_type": "LLM",
                "title": "Verify Answer",
                "aim": "Check the initial answer for correctness. Look for potential mistakes.",
                "reasoning_questions": "Is my answer correct? Did I make any errors? How can I verify?",
                "dependencies": [1],
                "step_context_queries": ["problem"],
                "stage_action": "Review and verify the initial solution.",
                "example_reasoning": "Let me double-check: does 42 make sense given the problem?",
                "frozen": False,
            },
            {
                "number": 3,
                "step_type": "LLM",
                "title": "Final Answer",
                "aim": "Provide the final answer, incorporating any corrections from verification.",
                "reasoning_questions": "What's the corrected answer? How should I present it?",
                "dependencies": [1, 2],
                "step_context_queries": ["problem"],
                "stage_action": "Output the verified final answer.",
                "example_reasoning": "After verification, I confirm the answer is 42.",
                "frozen": False,
            },
        ],
    },
}

# Storage-backed dataset presets: LLM steps aligned with master_api/carl_examples/*.json —
# use ``$outer_context`` (full row minus target, as built by the runner) and ``step_N_output`` for CARL context resolution.
DATASET_CARL_LL_PRESETS: Dict[str, List[dict]] = {
    "gsm8k": [
        {
            "number": 1,
            "step_type": "LLM",
            "title": "Decompose and solve the math problem step by step",
            "aim": "Break down the word problem into clear mathematical steps and solve each one",
            "reasoning_questions": "What quantities are given? What is being asked? What mathematical operations are needed? What intermediate values should be calculated?",
            "dependencies": [],
            "step_context_queries": ["$outer_context"],
            "stage_action": "Read the problem carefully. Identify all given numbers and their meanings. List the mathematical operations needed in order. Solve each step showing your work. Calculate the final answer as a single number.",
            "example_reasoning": "Given: 5 apples at $2 each and 3 oranges at $3 each. Step 1: 5 * $2 = $10 for apples. Step 2: 3 * $3 = $9 for oranges. Step 3: $10 + $9 = $19 total. Answer: 19",
            "frozen": False,
        },
        {
            "number": 2,
            "step_type": "LLM",
            "title": "Extract and format the final numeric answer",
            "aim": "Extract only the final numeric answer from the reasoning and output it as a clean number",
            "reasoning_questions": "What is the final numeric result from the calculation? Is the answer a whole number or decimal?",
            "dependencies": [1],
            "step_context_queries": ["step_1_output"],
            "stage_action": "Look at the solution from step 1. Find the final computed answer. Output ONLY the numeric answer as a plain number (no units, no text, no explanation). For example, output '42' not 'The answer is 42 dollars'.",
            "example_reasoning": "The calculation in step 1 resulted in $19 total. The numeric answer is: 19",
            "frozen": False,
        },
    ],
    "commonsense": [
        {
            "number": 1,
            "step_type": "LLM",
            "title": "Reason over the question and options",
            "aim": "Select the correct multiple-choice letter (A–E) using commonsense reasoning",
            "reasoning_questions": "What is the question asking? What does each option mean? Which options are implausible?",
            "dependencies": [],
            "step_context_queries": ["$outer_context"],
            "stage_action": "Use the question and options from the context. Think step by step. End with a single line exactly in the form: Answer: <letter> where <letter> is A, B, C, D, or E.",
            "example_reasoning": "Option B best fits because the others contradict common sense. Answer: B",
            "frozen": False,
        },
        {
            "number": 2,
            "step_type": "LLM",
            "title": "Verify and output final letter",
            "aim": "Confirm the chosen letter against the question and options",
            "reasoning_questions": "Does the letter from step 1 match the question? Is another option better?",
            "dependencies": [1],
            "step_context_queries": ["$outer_context", "step_1_output"],
            "stage_action": "Compare step 1's reasoning to the question and options again. Output ONLY one line: Answer: <letter> with a single capital letter A–E. No other text.",
            "example_reasoning": "Step 1 chose B; rechecking confirms B. Answer: B",
            "frozen": False,
        },
    ],
    "sentiment_analysis": [
        {
            "number": 1,
            "step_type": "LLM",
            "title": "Analyze review for binary sentiment (0/1)",
            "aim": "Decide whether the movie review is negative (0) or positive (1)",
            "reasoning_questions": "What sentiment cues appear? Are there negations or sarcasm? Overall, is the review closer to negative or positive?",
            "dependencies": [],
            "step_context_queries": ["$outer_context"],
            "stage_action": "Read the review text from the context. Argue whether it is negative (0) or positive (1). End with one line exactly: Answer: 0 or Answer: 1.",
            "example_reasoning": "The review praises the film strongly. Answer: 1",
            "frozen": False,
        },
        {
            "number": 2,
            "step_type": "LLM",
            "title": "Verify binary label",
            "aim": "Double-check and output only 0 or 1 to match the dataset target column",
            "reasoning_questions": "Does step 1's label match the tone of the original review?",
            "dependencies": [1],
            "step_context_queries": ["$outer_context", "step_1_output"],
            "stage_action": "Verify step 1 against the original review text. Output ONLY one line, exactly: Answer: 0 or Answer: 1. No words, no explanation.",
            "example_reasoning": "Step 1 said Answer: 1; the text is clearly positive. Answer: 1",
            "frozen": False,
        },
    ],
    "emotion": [
        {
            "number": 1,
            "step_type": "LLM",
            "title": "Reason over text and emotion options",
            "aim": "Infer the dominant emotion and map it to the correct option letter (A–F)",
            "reasoning_questions": "What emotion is expressed? Which labeled option (A–F) matches best?",
            "dependencies": [],
            "step_context_queries": ["$outer_context"],
            "stage_action": "Use the passage and the listed options from the context. Think step by step. End with one line exactly: Answer: <letter> where <letter> is A, B, C, D, E, or F.",
            "example_reasoning": "The tone is joyful; option B matches. Answer: B",
            "frozen": False,
        },
        {
            "number": 2,
            "step_type": "LLM",
            "title": "Verify and output final letter",
            "aim": "Confirm the emotion choice against the text and options",
            "reasoning_questions": "Is the letter from step 1 consistent with the options list?",
            "dependencies": [1],
            "step_context_queries": ["$outer_context", "step_1_output"],
            "stage_action": "Recheck step 1 against the question and options. Output ONLY one line: Answer: <letter> with a single capital letter A–F. No other text.",
            "example_reasoning": "Step 1 chose B; still the best fit. Answer: B",
            "frozen": False,
        },
    ],
}


def export_evolution_preset_to_json(preset_name: str) -> str:
    """Export an evolution preset to JSON format for use in experiments.

    Args:
        preset_name: Name of the evolution preset to export

    Returns:
        JSON string with chain configuration ready for use in CARL experiments
    """
    if preset_name not in EVOLUTION_PRESETS:
        raise ValueError(f"Evolution preset '{preset_name}' not found")

    preset = EVOLUTION_PRESETS[preset_name]
    chain_config = {
        "steps": preset["steps"],
        "max_workers": 2,
        "enable_progress": False,
    }

    # Add evolution metadata as comment
    metadata = {
        "_comment": f"Evolution preset: {preset_name}",
        "_description": preset["description"],
        "_evolution_mode": preset.get("evolution_mode", "full_chain"),
        "_preset_type": "evolution",
    }

    # Merge metadata with config (metadata will be stripped during evolution)
    result = {**metadata, **chain_config}

    return json.dumps(result, indent=2, ensure_ascii=False)


def get_all_evolution_presets() -> Dict[str, Any]:
    """Get all evolution presets with their metadata.

    Returns:
        Dictionary mapping preset names to their configurations
    """
    return EVOLUTION_PRESETS.copy()


def get_recommended_preset_for_task(task_type: str, dataset_size: int = 0) -> str:
    """Recommend an evolution preset based on task characteristics.

    Args:
        task_type: Type of task (qa, math, classification, etc.)
        dataset_size: Size of dataset (for complexity estimation)

    Returns:
        Name of recommended evolution preset
    """
    task_type_lower = task_type.lower()

    # Question answering tasks
    if "qa" in task_type_lower or "question" in task_type_lower:
        if "hotpot" in task_type_lower or "multi" in task_type_lower:
            return "🧬 Evolution: Weak 2-Step Chain"
        return "🧬 Evolution: Text QA Chain"

    # Math/reasoning tasks
    if "math" in task_type_lower or "calculation" in task_type_lower or "reasoning" in task_type_lower:
        return "🧬 Evolution: Math Problem Chain"

    # Tasks that benefit from retrieval
    if dataset_size > 100:
        return "🧬 Evolution: QA with Weak Retrieval"

    # Default: weak 2-step baseline
    return "🧬 Evolution: Weak 2-Step Chain"


def get_chain_complexity_score(steps: List[dict]) -> float:
    """Calculate complexity score for a CARL chain (0-1, higher = more complex).

    This score can be used to track evolution progress and diversity.
    Based on:
    - Number of steps
    - Step type diversity
    - Dependency complexity
    - Presence of advanced step types (TOOL, MEMORY, etc.)

    Args:
        steps: List of step configurations

    Returns:
        Complexity score between 0 and 1
    """
    if not steps:
        return 0.0

    n_steps = len(steps)

    # Step count factor (more steps = higher complexity)
    step_factor = min(n_steps / 10.0, 1.0)  # cap at 10 steps

    # Type diversity (unique step types)
    step_types = set(s.get("step_type", "LLM") for s in steps)
    type_diversity = len(step_types) / 7.0  # 7 possible types
    type_factor = min(type_diversity, 1.0)

    # Dependency complexity (average dependencies per step)
    total_deps = sum(len(s.get("dependencies", [])) for s in steps)
    avg_deps = total_deps / n_steps if n_steps > 0 else 0
    dep_factor = min(avg_deps / 3.0, 1.0)  # cap at 3 avg dependencies

    # Advanced step types bonus
    advanced_types = {"TOOL", "MEMORY", "MCP", "CONDITIONAL", "TRANSFORM"}
    has_advanced = any(st in advanced_types for st in step_types)
    advanced_factor = 0.2 if has_advanced else 0.0

    # Combine factors with weights
    complexity = 0.4 * step_factor + 0.2 * type_factor + 0.2 * dep_factor + advanced_factor

    return min(complexity, 1.0)


def validate_chain_for_evolution(steps: List[dict]) -> tuple[bool, list[str]]:
    """Validate that a chain configuration is suitable for evolution.

    Checks:
    - All required fields are present
    - Step numbers are sequential
    - Dependencies are valid
    - At least one non-frozen step exists

    Args:
        steps: List of step configurations

    Returns:
        Tuple of (is_valid, list_of_error_messages)
    """
    errors = []

    if not steps:
        errors.append("Chain must have at least one step")
        return False, errors

    # Check sequential numbering
    expected_numbers = set(range(1, len(steps) + 1))
    actual_numbers = set(s.get("number") for s in steps if isinstance(s, dict))
    if actual_numbers != expected_numbers:
        errors.append(f"Step numbers must be sequential from 1 to {len(steps)}")

    # Check dependencies
    for step in steps:
        if not isinstance(step, dict):
            continue

        step_num = step.get("number")
        deps = step.get("dependencies", [])

        # Check that dependencies reference valid steps
        for dep in deps:
            if dep not in actual_numbers:
                errors.append(f"Step {step_num} has invalid dependency {dep}")

        # Check for circular dependencies (simple check)
        if step_num in deps:
            errors.append(f"Step {step_num} cannot depend on itself")

    # Check that at least one non-frozen step exists
    non_frozen = [s for s in steps if isinstance(s, dict) and not s.get("frozen", False)]
    if not non_frozen:
        errors.append("At least one step must be non-frozen for evolution")

    # Check required fields for each step type
    # Note: step_type may be missing for LLM steps (default), or may be "LLM" (uppercase) in UI
    for step in steps:
        if not isinstance(step, dict):
            continue

        step_type = str(step.get("step_type", "LLM")).upper()
        step_num = step.get("number")

        # LLM is the default, so if step_type is missing or "LLM", treat as LLM step
        if step_type == "LLM" or not step.get("step_type"):
            required_fields = ["aim", "reasoning_questions", "stage_action"]
            for field in required_fields:
                if not step.get(field):
                    errors.append(f"LLM step {step_num} missing required field '{field}'")

        # Check non-LLM step types have required step_config
        elif step_type in ["TOOL", "TRANSFORM", "CONDITIONAL", "MEMORY", "MCP", "STRUCTURED_OUTPUT"]:
            step_config = step.get("step_config")
            if not step_config or not isinstance(step_config, dict):
                errors.append(f"{step_type} step {step_num} missing required 'step_config' field")
            else:
                # Validate step_config based on step type
                if step_type == "TOOL":
                    required_config_fields = ["tool_name", "tool_description", "input_mapping"]
                    for field in required_config_fields:
                        if field not in step_config:
                            errors.append(f"TOOL step {step_num} missing required step_config field '{field}'")

                elif step_type == "TRANSFORM":
                    required_config_fields = ["transform_type"]
                    for field in required_config_fields:
                        if field not in step_config:
                            errors.append(f"TRANSFORM step {step_num} missing required step_config field '{field}'")

                elif step_type == "CONDITIONAL":
                    if "branches" not in step_config and "condition" not in step_config:
                        errors.append(
                            f"CONDITIONAL step {step_num} missing required step_config: must have 'branches' or 'condition'"
                        )

                elif step_type == "MEMORY":
                    required_config_fields = ["operation"]
                    for field in required_config_fields:
                        if field not in step_config:
                            errors.append(f"MEMORY step {step_num} missing required step_config field '{field}'")

                elif step_type == "MCP":
                    required_config_fields = ["server", "procedure"]
                    for field in required_config_fields:
                        if field not in step_config:
                            errors.append(f"MCP step {step_num} missing required step_config field '{field}'")

                elif step_type == "STRUCTURED_OUTPUT":
                    if "output_schema" not in step_config:
                        errors.append(
                            f"STRUCTURED_OUTPUT step {step_num} missing required step_config field 'output_schema'"
                        )

    return len(errors) == 0, errors


def load_preset_from_file(preset_filename: str) -> Dict[str, Any]:
    """Load a CARL experiment preset from file.

    Args:
        preset_filename: Name of the preset file in master_api/data_examples/ or master_api/carl_examples/

    Returns:
        Dictionary with preset configuration including dataset_path, target_field,
        base_chain_config, and other metadata
    """
    from pathlib import Path

    from utils.master_api_paths import iter_master_api_roots

    possible_paths: list[Path] = []
    for root in iter_master_api_roots():
        possible_paths.append(root / "data_examples" / preset_filename)
        possible_paths.append(root / "carl_examples" / preset_filename)
    possible_paths.extend(
        [
            Path(__file__).parent.parent.parent / "master_api" / "data_examples" / preset_filename,
            Path(__file__).parent.parent.parent / "master_api" / "carl_examples" / preset_filename,
            Path("master_api/data_examples") / preset_filename,
            Path("master_api/carl_examples") / preset_filename,
            Path("carl_examples") / preset_filename,
        ]
    )

    preset_path = None
    for path in possible_paths:
        if path.exists():
            preset_path = path
            break

    if not preset_path:
        logger.error(f"Preset file not found: {preset_filename}")
        logger.debug(f"Searched in: {[str(p) for p in possible_paths]}")
        raise FileNotFoundError(f"Preset file not found: {preset_filename}")

    try:
        with open(preset_path, "r", encoding="utf-8") as f:
            preset = json.load(f)
        logger.info(f"Loaded preset from {preset_path}")
        return preset
    except Exception as e:
        logger.error(f"Failed to load preset from {preset_path}: {e}")
        raise


# Preset file mappings for ready-to-run experiments
CARL_EXPERIMENT_PRESETS = {
    "btn_sentiment_simple": "sentiment_simple.json",
    "btn_hotpotqa_tool": "hotpotqa_tool.json",
    "btn_transform_demo": "transform_demo.json",
    "btn_conditional_demo": "conditional_demo.json",
    "btn_mixed_pipeline": "mixed_pipeline.json",
}


def get_preset_info(preset_filename: str) -> tuple[str, str]:
    """Get display information for a preset.

    Returns:
        Tuple of (title, description)
    """
    try:
        preset = load_preset_from_file(preset_filename)
        title = preset.get("task_description", "Unknown Preset")
        desc = f"Dataset: {preset.get('dataset_path', 'N/A')}"
        return title, desc
    except Exception:
        return "Unknown Preset", "Could not load preset information"


def _try_import_carl_schemas():
    """Try to import get_all_step_type_schemas from the installed mmar_carl package.

    The library is installed via pip/uv from GitHub (see pyproject.toml).
    Returns the function or None if the package is not available.
    """
    try:
        from mmar_carl import get_all_step_type_schemas

        logger.debug("Successfully imported CARL schemas from installed library")
        return get_all_step_type_schemas
    except ImportError:
        logger.warning("mmar_carl package is not installed; using fallback hints")
        return None


def load_carl_step_schemas() -> Dict[str, Any]:
    """Load step type schemas from CARL library, fallback to hardcoded hints.

    This function dynamically updates the global CARL_STEP_TYPES list
    to match the available step types in the installed CARL library.
    """
    global CARL_STEP_TYPES

    try:
        get_schemas_func = _try_import_carl_schemas()
        if not get_schemas_func:
            logger.info("CARL library not available, using fallback step types")
            CARL_STEP_TYPES = list(FALLBACK_STEP_TYPE_HINTS.keys())
            return FALLBACK_STEP_TYPE_HINTS

        schemas = get_schemas_func()
        if not schemas:
            logger.warning("CARL library returned empty schemas, using fallback")
            CARL_STEP_TYPES = list(FALLBACK_STEP_TYPE_HINTS.keys())
            return FALLBACK_STEP_TYPE_HINTS

        # Dynamically populate CARL_STEP_TYPES from the library
        CARL_STEP_TYPES = sorted(schemas.keys())
        logger.info(f"Dynamically loaded {len(CARL_STEP_TYPES)} step types from CARL library: {CARL_STEP_TYPES}")

        result = {}
        for step_type, schema in schemas.items():
            result[step_type] = {
                "title": schema.get("title", step_type),
                "description": schema.get("description", ""),
                "fields": schema.get("fields", {}),
            }
        return result
    except Exception as e:
        logger.warning(f"Failed to load CARL schemas: {e}, using fallback hints")
        CARL_STEP_TYPES = list(FALLBACK_STEP_TYPE_HINTS.keys())

    return FALLBACK_STEP_TYPE_HINTS


# ---------------------------------------------------------------------------
#  UI helpers
# ---------------------------------------------------------------------------


def _step_type_label(step_type: str, hints: Dict[str, Any]) -> str:
    """Build a readable label for a step type."""
    icon = STEP_TYPE_ICONS.get(step_type, "⚙️")
    desc = hints.get(step_type, {}).get("description", "")
    short = desc.split(".")[0].strip() if desc else step_type
    return f"{icon} {step_type} — {short}"


def _format_chain_preview_html(steps: List[dict]) -> str:
    """Render the chain as rich HTML cards."""
    if not steps:
        return (
            '<div style="text-align:center;padding:40px 20px;color:#888;'
            'border:2px dashed #ddd;border-radius:12px;margin:12px 0;">'
            '<div style="font-size:32px;margin-bottom:8px;">🔗</div>'
            "<b>Chain is empty</b><br/>"
            "Add steps using the palette below or start from a template."
            "</div>"
        )

    cards: list[str] = []
    for i, s in enumerate(steps):
        num = s.get("number", i + 1)
        stype = s.get("step_type", "LLM")
        title = s.get("title", "") or f"Step {num}"
        frozen = s.get("frozen", False)
        deps = s.get("dependencies", [])
        icon = STEP_TYPE_ICONS.get(stype, "⚙️")
        aim = s.get("aim", "")

        # Badge colours
        type_color = {
            "LLM": "#4A90D9",
            "TOOL": "#E67E22",
            "MCP": "#8E44AD",
            "MEMORY": "#27AE60",
            "TRANSFORM": "#F39C12",
            "CONDITIONAL": "#E74C3C",
            "STRUCTURED_OUTPUT": "#1ABC9C",
        }.get(stype, "#95A5A6")

        frozen_badge = (
            ' <span style="background:#3498DB;color:#fff;padding:1px 7px;'
            'border-radius:8px;font-size:11px;">❄ frozen</span>'
            if frozen
            else ""
        )
        deps_text = f" ← steps {', '.join(str(d) for d in deps)}" if deps else ""

        aim_html = ""
        if aim:
            short_aim = (aim[:90] + "…") if len(aim) > 90 else aim
            aim_html = f'<div style="color:#666;font-size:12px;margin-top:4px;">{short_aim}</div>'

        cfg = s.get("step_config", {}) or {}
        config_html = ""
        if stype != "LLM" and cfg:
            cfg_items = list(cfg.items())[:3]
            parts = []
            for k, v in cfg_items:
                vs = str(v)
                if len(vs) > 40:
                    vs = vs[:37] + "…"
                parts.append(f"<b>{k}</b>: {vs}")
            config_html = f'<div style="color:#888;font-size:11px;margin-top:2px;">{"  •  ".join(parts)}</div>'

        card = (
            f'<div style="background:#fff;border:1px solid #e0e0e0;border-left:4px solid {type_color};'
            f'border-radius:8px;padding:10px 14px;margin-bottom:2px;">'
            f'<div style="display:flex;align-items:center;gap:8px;">'
            f'<span style="font-size:20px;">{icon}</span>'
            f'<span style="background:{type_color};color:#fff;padding:2px 8px;border-radius:4px;'
            f'font-size:12px;font-weight:600;">{stype}</span>'
            f'<b style="font-size:14px;">#{num} {title}</b>'
            f"{frozen_badge}"
            f'<span style="color:#aaa;font-size:11px;margin-left:auto;">{deps_text}</span>'
            f"</div>"
            f"{aim_html}{config_html}"
            f"</div>"
        )

        # Arrow between cards
        if i < len(steps) - 1:
            card += '<div style="text-align:center;color:#bbb;font-size:16px;line-height:20px;">↓</div>'

        cards.append(card)

    # Chain summary bar
    n_frozen = sum(1 for s in steps if s.get("frozen", False))
    n_evolvable = len(steps) - n_frozen
    types_used = sorted(set(s.get("step_type", "LLM") for s in steps))
    summary = (
        f'<div style="display:flex;gap:16px;padding:6px 0;color:#666;font-size:12px;margin-bottom:6px;">'
        f"<span>📊 <b>{len(steps)}</b> steps</span>"
        f"<span>🧬 <b>{n_evolvable}</b> evolvable</span>"
        f"<span>❄ <b>{n_frozen}</b> frozen</span>"
        f"<span>Types: {', '.join(types_used)}</span>"
        f"</div>"
    )

    return summary + "".join(cards)


def _default_step(number: int, step_type: str = "LLM") -> dict:
    """Create a default step dict."""
    return {
        "number": int(number),
        "step_type": step_type.upper(),
        "title": "",
        "aim": "",
        "reasoning_questions": "",
        "dependencies": [] if number == 1 else [number - 1],
        "step_context_queries": ["problem"],
        "stage_action": "",
        "example_reasoning": "",
        "frozen": False,
    }


def _steps_to_chain_json(steps: list[dict]) -> str:
    """Convert internal steps list to CARL chain config JSON."""
    out_steps = []
    for s in steps:
        s_clean: dict = {
            "number": int(s.get("number", 0)),
            "title": s.get("title", ""),
            "aim": s.get("aim", ""),
            "reasoning_questions": s.get("reasoning_questions", ""),
            "dependencies": s.get("dependencies", []) or [],
            "step_context_queries": s.get("step_context_queries", []) or [],
            "stage_action": s.get("stage_action", ""),
            "example_reasoning": s.get("example_reasoning", ""),
        }
        stype_ui = str(s.get("step_type", "LLM")).upper()
        if stype_ui != "LLM":
            s_clean["step_type"] = stype_ui.lower()
            step_cfg = dict(s.get("step_config", {}) or {})
            s_clean["step_config"] = step_cfg
        if s.get("frozen", False):
            s_clean["frozen"] = True
        out_steps.append(s_clean)
    return json.dumps({"steps": out_steps}, indent=2, ensure_ascii=False)


def _chain_json_to_steps(json_str: str) -> list[dict]:
    """Parse CARL chain config JSON back to internal steps list."""
    try:
        cfg = json.loads(json_str)
    except Exception:
        return []
    raw_steps = cfg.get("steps", [])
    if not isinstance(raw_steps, list):
        return []
    steps = []
    for s in raw_steps:
        if not isinstance(s, dict):
            continue
        stype = str(s.get("step_type", "LLM")).upper()
        if stype == "":
            stype = "LLM"
        step: dict = {
            "number": int(s.get("number", len(steps) + 1)),
            "step_type": stype,
            "title": s.get("title", ""),
            "aim": s.get("aim", ""),
            "reasoning_questions": s.get("reasoning_questions", ""),
            "dependencies": s.get("dependencies", []) or [],
            "step_context_queries": s.get("step_context_queries", []) or [],
            "stage_action": s.get("stage_action", ""),
            "example_reasoning": s.get("example_reasoning", ""),
            "frozen": bool(s.get("frozen", False)),
        }
        if s.get("step_config"):
            step["step_config"] = dict(s["step_config"])
        steps.append(step)
    return steps


_PYTHON_TEMPLATE = '''"""Custom Python code for CARL chain experiment.

You can define custom tool functions, validators, or preprocessing
logic here. This code will be available during chain execution.

Example: custom tool function that can be registered in ReasoningContext:
"""

import json
from typing import Any, Dict


# ── Custom Tool Functions ──────────────────────────────────────────────────
# These functions can be registered as TOOL steps in your chain.
# They receive keyword arguments from the step's input_mapping
# and should return a dict with results.

def my_custom_tool(**kwargs) -> Dict[str, Any]:
    """Example custom tool.
    
    Register this tool in the chain by setting step_type="TOOL"
    and tool_name="my_custom_tool".
    """
    query = kwargs.get("query", "")
    # Your custom logic here
    result = f"Processed: {query}"
    return {"result": result}


# ── Custom Validation / Preprocessing ──────────────────────────────────────

def preprocess_input(text: str) -> str:
    """Optional: preprocess input before sending to chain."""
    return text.strip()


def postprocess_output(output: str) -> str:
    """Optional: postprocess chain output before evaluation."""
    return output.strip()
'''


# ---------------------------------------------------------------------------
#  Component
# ---------------------------------------------------------------------------


class CreateCARLExperimentComponent(BaseComponent):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.step_type_hints = load_carl_step_schemas()
        self.bucket_name: Optional[str] = None

    def build(self) -> gr.Column:
        hints = self.step_type_hints

        with gr.Column() as component:
            gr.Markdown("## 🔗 Create CARL Chain Experiment")

            gr.Markdown("#### Start with a Preset Example")

            with gr.Row():
                btn_sentiment_simple = gr.Button("💬 Sentiment", size="sm")
                btn_hotpotqa_tool = gr.Button("🔧 HotpotQA + TOOL", size="sm")
                btn_transform_demo = gr.Button("🔄 TRANSFORM Demo", size="sm")
                btn_conditional_demo = gr.Button("🔀 CONDITIONAL Demo", size="sm")
                btn_mixed_pipeline = gr.Button("⚙️ Mixed Pipeline", size="sm")
            with gr.Row():
                btn_ds_gsm8k = gr.Button("GSM8K Chain", size="sm")
                btn_ds_commonsense = gr.Button("Commonsense QA", size="sm")
                btn_ds_emotion = gr.Button("Emotion Classification", size="sm")

            # ── SECTION 1: Experiment settings ────────────────────────
            with gr.Accordion("⚙️ Experiment Settings", open=True):
                with gr.Row():
                    with gr.Column(scale=2):
                        name_input = gr.Textbox(
                            label="Experiment Name",
                            placeholder="e.g., Math Reasoning Chain",
                        )
                        description_input = gr.Textbox(
                            label="Description",
                            placeholder="Short description of the goal and setup…",
                            lines=2,
                        )
                    with gr.Column(scale=1):
                        data_file_input = gr.File(
                            label="Dataset",
                            file_types=[".csv", ".json", ".txt", ".zip"],
                            height=80,
                            elem_classes="short-upload",
                        )
                        dataset_info = gr.Textbox(
                            label="Dataset Source",
                            value="No file selected",
                            interactive=False,
                        )

                with gr.Row():
                    target_field_input = gr.Textbox(
                        label="Target Column",
                        placeholder="target",
                        scale=1,
                    )
                    llm_model_input = gr.Dropdown(
                        choices=get_llm_model_choices(),
                        value=get_default_llm_model_id(),
                        label="🧬 Evolution Model",
                        info="LLM that drives the evolution process (mutates chain steps)",
                        scale=1,
                    )
                    chain_llm_model_input = gr.Dropdown(
                        choices=[("Same as Evolution Model", "")] + get_llm_model_choices(),
                        value="",
                        label="🔗 Chain Execution Model",
                        info="LLM used by chain steps for reasoning (if different from evolution model)",
                        scale=1,
                    )
                    max_iterations_input = gr.Slider(
                        minimum=1,
                        maximum=500,
                        value=100,
                        step=1,
                        label="Max Iterations",
                        scale=1,
                    )

                with gr.Row():
                    chain_size_limit_input = gr.Number(
                        value=None,
                        label="Chain Size Limit",
                        info="Maximum number of steps allowed (empty = no limit)",
                        precision=0,
                        minimum=1,
                        scale=1,
                    )
                    dataset_size_input = gr.Number(
                        value=None,
                        label="Dataset Size (rows)",
                        info="Optional subsample limit (empty = all rows)",
                        precision=0,
                        scale=1,
                    )
                    test_size_input = gr.Slider(
                        minimum=0.0,
                        maximum=0.9,
                        value=0.2,
                        step=0.05,
                        label="Test Split Ratio",
                        scale=1,
                    )

                with gr.Row():
                    evolution_mode_input = gr.Dropdown(
                        choices=["full_chain", "single_step"],
                        value="full_chain",
                        label="Evolution Mode",
                        info="full_chain: evolve all non-frozen steps; single_step: evolve a single step",
                        scale=1,
                    )
                    evolution_step_number_input = gr.Number(
                        value=None,
                        label="Step Number to Evolve",
                        info="Only used for single_step mode",
                        precision=0,
                        visible=False,
                        scale=1,
                    )

                # Chain Feedback Settings
                gr.Markdown("### 🔄 Chain Feedback")
                gr.Markdown(
                    "Configure **chain execution feedback** for additional reflection during evolution. "
                    "Feedback helps the LLM understand what went wrong and how to improve."
                )

                with gr.Row():
                    enable_feedback_checkbox = gr.Checkbox(
                        label="Enable Chain Feedback",
                        value=False,
                        info="Generate feedback from chain execution results for mutation prompts",
                        scale=1,
                    )

                feedback_template_radio = gr.Radio(
                    choices=[
                        ("📊 Detailed - Full performance analysis with examples", "detailed"),
                        ("📈 Summary - Brief performance overview", "summary"),
                        ("❌ Errors Only - Focus on failures and mistakes", "errors_only"),
                    ],
                    value="detailed",
                    label="Feedback Template",
                    info="How detailed should the feedback be?",
                    interactive=True,
                    scale=2,
                )

                gr.Markdown(
                    "<details><summary>💡 <b>How feedback works</b></summary>\n\n"
                    "**🔄 What is Feedback?**\n"
                    "After each chain execution, the system analyzes results and generates "
                    "structured feedback that is included in mutation prompts.\n\n"
                    "**📊 Detailed Template:**\n"
                    "- Performance summary (accuracy, correct/incorrect counts)\n"
                    "- Failed examples with predictions vs ground truth\n"
                    "- Execution time statistics\n"
                    "- Improvement suggestions\n\n"
                    "**📈 Summary Template:**\n"
                    "- Brief performance overview only\n\n"
                    "**❌ Errors Only Template:**\n"
                    "- Focus on failed examples\n"
                    "- Shows what went wrong\n\n"
                    "**⚙️ When to Use:**\n"
                    "- Enable for difficult tasks where the LLM needs guidance\n"
                    "- Disable for simple tasks to reduce token usage\n"
                    "- Use 'errors_only' for fast iteration on problematic cases\n"
                    "</details>"
                )

                # Validation Configuration
                gr.Markdown("### 🔧 Validation Criteria")
                with gr.Group():
                    validation_type_input = gr.Dropdown(
                        choices=["Binary (0/1)", "Continuous (0..1)"],
                        label="Validation Type",
                        info="Select how chain outputs should be validated",
                        interactive=True,
                    )

                    with gr.Group() as binary_validation_group:
                        binary_validation_method_input = gr.Dropdown(
                            choices=["equality", "substring", "regexp"],
                            label="Binary Validation Method",
                            info="Method for binary validation",
                            visible=True,
                            interactive=True,
                        )
                        regexp_pattern_input = gr.Textbox(
                            label="RegExp Pattern",
                            placeholder=r"Answer:\s*(.+?)$",
                            visible=False,
                            info="Regular expression with capture group to extract and compare with ground truth",
                        )

                    with gr.Group() as continuous_validation_group:
                        continuous_metric_input = gr.Dropdown(
                            choices=[
                                "ROUGE-1",
                                "ROUGE-2",
                                "ROUGE-L",
                                "BERTScore",
                                "BLEU",
                            ],
                            label="Continuous Validation Metric",
                            info="Metric for continuous validation",
                            visible=False,
                            interactive=True,
                        )

                # Memory Configuration
                gr.Markdown("### 🧠 Memory Configuration")
                gr.Markdown(
                    "Configure **memory retrieval** to mix relevant ideas from a memory bank "
                    "into the evolution process during mutations."
                )

                with gr.Row():
                    enable_memory_checkbox = gr.Checkbox(
                        label="Enable Memory Retrieval",
                        value=False,
                        info="Retrieve relevant ideas from memory bank during evolution mutations",
                        scale=1,
                    )
                with gr.Row():
                    experiment_memory_namespace_input = gr.Textbox(
                        label="Memory Namespace",
                        placeholder="Optional; defaults to experiment ID",
                        info="Optional. Used only when memory retrieval is enabled; leave empty to use the experiment ID.",
                    )

            # ── SECTION 2: Chain Builder (3 tabs) ──────────────────────
            gr.Markdown("### 🏗️ Chain Builder")

            # Chain size indicator
            chain_size_indicator = gr.HTML(value="")

            with gr.Tabs() as chain_tabs:
                # ─── TAB 1: Visual Builder ─────────────────────────────
                with gr.Tab("🏗️ Visual Builder"):
                    # Current chain preview (rich HTML)
                    chain_preview = gr.HTML(
                        value=_format_chain_preview_html([]),
                        label="Current Chain",
                    )

                    # Step palette — add new steps
                    with gr.Accordion("➕ Add Step", open=True):
                        # Build step type choices with descriptions
                        type_choices = [_step_type_label(t, hints) for t in CARL_STEP_TYPES]
                        step_type_radio = gr.Radio(
                            choices=type_choices,
                            value=type_choices[0],
                            label="Step Type",
                            info='Pick a type and click "Add step"',
                        )
                        add_step_btn = gr.Button("➕ Add Step to Chain", variant="primary")

                    # Step selector + actions
                    with gr.Row():
                        selected_step_dropdown = gr.Dropdown(
                            choices=[],
                            value=None,
                            label="Select a step to edit",
                            interactive=True,
                            scale=3,
                        )
                        edit_step_btn = gr.Button("✏️ Edit", size="sm", scale=1)
                        toggle_freeze_btn = gr.Button("❄️ Freeze / Unfreeze", size="sm", scale=1)
                        remove_step_btn = gr.Button("🗑️ Delete Step", variant="stop", size="sm", scale=1)

                    # Move up / down buttons
                    with gr.Row():
                        move_up_btn = gr.Button("⬆️ Move Up", size="sm", scale=1)
                        move_down_btn = gr.Button("⬇️ Move Down", size="sm", scale=1)
                        duplicate_step_btn = gr.Button("📋 Duplicate Step", size="sm", scale=1)

                    # ── SECTION 3: Step Editor (hidden by default) ────────────
                    with gr.Group(visible=False) as step_form_group:
                        step_editor_title = gr.Markdown("### ✏️ Edit Step")

                        with gr.Row():
                            with gr.Column(scale=2):
                                title_input = gr.Textbox(
                                    label="Step Title *",
                                    placeholder="e.g., Analyze Problem",
                                )
                            with gr.Column(scale=1):
                                step_type_display = gr.Textbox(
                                    label="Type",
                                    interactive=False,
                                )
                                frozen_checkbox = gr.Checkbox(
                                    label="❄ Frozen (do not evolve)",
                                    value=False,
                                )

                        # Dependencies as multi-select
                        dependencies_checkboxgroup = gr.CheckboxGroup(
                            choices=[],
                            value=[],
                            label="Dependencies (steps that must run before this step)",
                        )

                        # LLM-specific fields
                        with gr.Accordion("🧠 LLM Fields", open=True, visible=False) as llm_fields_group:
                            aim_input = gr.Textbox(
                                label="Aim *",
                                lines=2,
                                placeholder="What should this step achieve?",
                                info=hints.get("LLM", {}).get("fields", {}).get("aim", ""),
                            )
                            reasoning_questions_input = gr.Textbox(
                                label="Reasoning Questions *",
                                lines=2,
                                placeholder="Questions that guide the LLM reasoning for this step",
                                info=hints.get("LLM", {}).get("fields", {}).get("reasoning_questions", ""),
                            )
                            step_context_queries_input = gr.Textbox(
                                label="Context Queries (JSON array)",
                                placeholder='["problem"]',
                                info='Context selectors for this step. Example: ["problem"], ["@1.output"]',
                            )
                            stage_action_input = gr.Textbox(
                                label="Stage Action *",
                                lines=2,
                                placeholder="Detailed instructions for what to do in this step",
                                info=hints.get("LLM", {}).get("fields", {}).get("stage_action", ""),
                            )
                            example_reasoning_input = gr.Textbox(
                                label="Example Reasoning",
                                lines=3,
                                placeholder="Optional example of the desired reasoning style",
                                info=hints.get("LLM", {}).get("fields", {}).get("example_reasoning", ""),
                            )

                        # TOOL config
                        with gr.Accordion("🔧 TOOL Configuration", open=True, visible=False) as tool_config_group:
                            gr.Markdown(f"*{hints.get('TOOL', {}).get('description', '')}*")
                            tool_name_input = gr.Textbox(label="Tool Name *", placeholder="my_evaluator")
                            tool_description_input = gr.Textbox(
                                label="Tool Description", lines=2, placeholder="What does this tool do?"
                            )
                            tool_input_mapping_input = gr.Textbox(
                                label="Input Mapping (JSON) *",
                                placeholder='{"input": "@1.output", "data": "@context.data"}',
                                lines=3,
                            )
                            tool_timeout_input = gr.Number(
                                label="Timeout (sec)", value=120, precision=0, minimum=1, maximum=3600
                            )

                        # TRANSFORM config
                        with gr.Accordion(
                            "🔄 TRANSFORM Configuration", open=True, visible=False
                        ) as transform_config_group:
                            gr.Markdown(f"*{hints.get('TRANSFORM', {}).get('description', '')}*")
                            transform_type_input = gr.Dropdown(
                                choices=["extract", "format", "aggregate", "filter", "map", "python_expr", "jmespath"],
                                value="extract",
                                label="Transform Type *",
                            )
                            transform_input_key_input = gr.Textbox(
                                label="Input Key", placeholder="$history[-1]", value="$history[-1]"
                            )
                            transform_expression_input = gr.Textbox(
                                label="Expression", placeholder="Expression to evaluate", lines=2
                            )
                            transform_output_format_input = gr.Textbox(
                                label="Output Format", placeholder="Output format template", lines=2
                            )
                            transform_map_template_input = gr.Textbox(
                                label="Map Template", placeholder="Template for map operations", lines=2
                            )

                        # MEMORY config
                        with gr.Accordion("💾 MEMORY Configuration", open=True, visible=False) as memory_config_group:
                            gr.Markdown(f"*{hints.get('MEMORY', {}).get('description', '')}*")
                            memory_operation_input = gr.Dropdown(
                                choices=["read", "write", "append", "delete", "list"], value="read", label="Operation *"
                            )
                            memory_namespace_input = gr.Textbox(
                                label="Namespace", placeholder="default", value="default"
                            )
                            memory_key_input = gr.Textbox(label="Memory Key *", placeholder="my_key")
                            memory_value_source_input = gr.Textbox(
                                label="Value Source (for write)", placeholder="$history[-1]"
                            )
                            memory_default_value_input = gr.Textbox(
                                label="Default Value", placeholder="Default value if missing"
                            )

                        # MCP config
                        with gr.Accordion("🔌 MCP Configuration", open=True, visible=False) as mcp_config_group:
                            gr.Markdown(f"*{hints.get('MCP', {}).get('description', '')}*")
                            mcp_server_name_input = gr.Textbox(label="Server Name *", placeholder="mcp_server")
                            mcp_tool_name_input = gr.Textbox(label="Tool Name *", placeholder="tool_name")
                            mcp_arguments_input = gr.Textbox(
                                label="Arguments (JSON)", placeholder='{"key": "value"}', lines=2
                            )
                            mcp_argument_mapping_input = gr.Textbox(
                                label="Argument Mapping (JSON)", placeholder='{"arg": "@1.output"}', lines=2
                            )
                            mcp_timeout_input = gr.Number(
                                label="Timeout (sec)", value=30, precision=0, minimum=1, maximum=3600
                            )

                        # CONDITIONAL config
                        with gr.Accordion(
                            "🔀 CONDITIONAL Configuration", open=True, visible=False
                        ) as conditional_config_group:
                            gr.Markdown(f"*{hints.get('CONDITIONAL', {}).get('description', '')}*")
                            conditional_branches_input = gr.Textbox(
                                label="Branches (JSON) *",
                                placeholder='[{"condition": "@1.output == \\"yes\\"", "next_step": 2}]',
                                lines=4,
                            )
                            conditional_default_step_input = gr.Number(label="Default Step", precision=0, minimum=1)
                            conditional_condition_context_key_input = gr.Textbox(
                                label="Condition Context Key", value="$history[-1]"
                            )

                        # STRUCTURED_OUTPUT config
                        with gr.Accordion(
                            "📋 STRUCTURED_OUTPUT Configuration", open=True, visible=False
                        ) as structured_output_config_group:
                            gr.Markdown(f"*{hints.get('STRUCTURED_OUTPUT', {}).get('description', '')}*")
                            structured_output_schema_input = gr.Textbox(
                                label="Output Schema (JSON Schema) *",
                                placeholder='{"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}',
                                lines=5,
                            )
                            structured_output_prompt_input = gr.Textbox(
                                label="Prompt Template", placeholder="Based on @context.problem, …", lines=3
                            )
                            structured_output_model_input = gr.Textbox(
                                label="Model Override", placeholder="Leave empty to use the chain default model"
                            )
                            structured_output_temperature_input = gr.Slider(
                                minimum=0.0, maximum=2.0, value=0.0, step=0.1, label="Temperature"
                            )

                        with gr.Row():
                            save_step_btn = gr.Button("💾 Save Step", variant="primary", size="lg")
                            cancel_step_btn = gr.Button("❌ Cancel", variant="secondary", size="lg")

                # ─── TAB 2: JSON Editor ────────────────────────────────
                with gr.Tab("📝 JSON Editor"):
                    gr.Markdown(
                        "Edit the chain configuration as raw JSON. "
                        "Changes here **sync with** the Visual Builder. "
                        "Click **Apply JSON** to update the visual view."
                    )
                    chain_json_editor = gr.Code(
                        value='{"steps": []}',
                        language="json",
                        label="Chain Configuration (JSON)",
                        lines=25,
                    )
                    with gr.Row():
                        apply_json_btn = gr.Button("✅ Apply JSON → Visual Builder", variant="primary")
                        export_json_btn = gr.Button("📤 Visual Builder → JSON", variant="secondary")
                    json_status = gr.HTML(value="")

                # ─── TAB 3: Python Code ────────────────────────────────
                with gr.Tab("🐍 Python Code"):
                    gr.Markdown(
                        "Write custom Python code for your CARL experiment. "
                        "Define **tool functions** that will be **automatically registered** "
                        "and can be used by TOOL steps in your chain."
                    )
                    python_code_editor = gr.Code(
                        value=_PYTHON_TEMPLATE,
                        language="python",
                        label="Custom Python Code",
                        lines=30,
                    )
                    gr.Markdown(
                        "<details><summary>💡 <b>How custom tools work</b></summary>\n\n"
                        "**🔧 Automatic Registration:**\n"
                        "All functions defined in this code are automatically registered as tools "
                        "when the experiment runs. You don't need to manually register them.\n\n"
                        "**📝 Using Custom Tools:**\n"
                        "1. Define your Python functions above (e.g., `def my_tool(**kwargs)`)\n"
                        "2. In the Visual Builder, add a TOOL step\n"
                        "3. Set `tool_name` to your function name (e.g., `my_tool`)\n"
                        "4. Configure `input_mapping` to pass data from previous steps\n"
                        "5. Your function receives keyword arguments and returns a `dict`\n\n"
                        "**🔌 Built-in Tools:**\n"
                        "Environment variables like `CARL_TOOL__<NAME>_URL` also register HTTP tools.\n"
                        "The `retrieve` tool is automatically available for TF-IDF/BM25 retrieval.\n"
                        "</details>"
                    )

            # ── SECTION 5: Create / Clean ─────────────────────────────
            with gr.Row():
                create_btn = gr.Button("🚀 Create Experiment", variant="primary", size="lg")
                clean_btn = gr.Button("🧹 Clear Form", variant="secondary", size="lg")
            create_output = gr.Textbox(label="Status", interactive=False, lines=3)

            # Internal state
            steps_state = gr.State(value=[])
            dataset_path_state = gr.State(value="")
            current_editing_step = gr.State(value=None)

            # ══════════════════════════════════════════════════════════
            #  Callbacks
            # ══════════════════════════════════════════════════════════

            def _parse_step_type_from_radio(radio_val: str) -> str:
                """Extract step type from radio label like '🧠 LLM — description'."""
                if not radio_val:
                    return "LLM"
                parts = radio_val.split("—")[0].strip()
                for t in CARL_STEP_TYPES:
                    if t in parts:
                        return t
                return "LLM"

            def _build_step_choices(steps: list[dict]) -> list[str]:
                """Build dropdown choices for step selector."""
                choices = []
                for s in steps:
                    num = s.get("number", 0)
                    stype = s.get("step_type", "LLM")
                    title = s.get("title", "") or f"Step {num}"
                    icon = STEP_TYPE_ICONS.get(stype, "⚙️")
                    frozen = " ❄" if s.get("frozen") else ""
                    choices.append(f"{num}: {icon} [{stype}] {title}{frozen}")
                return choices

            def _step_number_from_choice(choice: str) -> Optional[int]:
                """Parse step number from choice string like '1: 🧠 [LLM] Title'."""
                if not choice:
                    return None
                try:
                    return int(choice.split(":")[0].strip())
                except (ValueError, IndexError):
                    return None

            def _build_deps_choices(steps: list[dict], current_num: int) -> list[str]:
                """Build checkbox choices for dependencies."""
                choices = []
                for s in steps:
                    n = s.get("number", 0)
                    if n == current_num:
                        continue
                    title = s.get("title", "") or f"Step {n}"
                    stype = s.get("step_type", "LLM")
                    icon = STEP_TYPE_ICONS.get(stype, "⚙️")
                    choices.append(f"{n}: {icon} {title}")
                return choices

            def _deps_values_from_list(deps: list[int], steps: list[dict], current_num: int) -> list[str]:
                """Convert dependency numbers to checkbox values."""
                choices_map = {}
                for s in steps:
                    n = s.get("number", 0)
                    if n == current_num:
                        continue
                    title = s.get("title", "") or f"Step {n}"
                    stype = s.get("step_type", "LLM")
                    icon = STEP_TYPE_ICONS.get(stype, "⚙️")
                    choices_map[n] = f"{n}: {icon} {title}"
                return [choices_map[d] for d in deps if d in choices_map]

            def _deps_from_values(values: list[str]) -> list[int]:
                """Convert checkbox values back to dependency numbers."""
                result = []
                for v in values:
                    try:
                        result.append(int(v.split(":")[0].strip()))
                    except (ValueError, IndexError):
                        pass
                return sorted(result)

            def _update_chain_size_indicator(steps: list[dict], limit) -> str:
                n = len(steps)
                try:
                    max_n = int(limit) if limit not in (None, "", 0) else 0
                except (ValueError, TypeError):
                    max_n = 0

                if max_n > 0:
                    pct = min(100, int(n / max_n * 100))
                    color = "#27AE60" if pct < 80 else ("#F39C12" if pct < 100 else "#E74C3C")
                    return (
                        f'<div style="display:flex;align-items:center;gap:10px;padding:6px 0;">'
                        f'<span style="font-size:13px;color:#555;">Steps: <b>{n}</b> / {max_n}</span>'
                        f'<div style="flex:1;background:#eee;border-radius:4px;height:8px;">'
                        f'<div style="width:{pct}%;background:{color};border-radius:4px;height:8px;'
                        f'transition:width 0.3s;"></div></div></div>'
                    )
                else:
                    return f'<div style="padding:4px 0;font-size:13px;color:#555;">Steps: <b>{n}</b> (no limit)</div>'

            # -- Show / hide form --
            def _show_form_for_type(stype: str, step_num: Optional[int]):
                """Return visibility updates for step form groups (10 values, no deps)."""
                stype = stype.upper()
                num = int(step_num) if step_num else 0
                title_text = f"### ✏️ Step #{num}" if num else "### ✏️ New Step"
                return (
                    gr.update(visible=True),  # step_form_group
                    gr.update(value=title_text),  # step_editor_title
                    gr.update(value=f"{STEP_TYPE_ICONS.get(stype, '⚙️')} {stype}"),  # step_type_display
                    gr.update(visible=stype == "LLM"),  # llm_fields_group
                    gr.update(visible=stype == "TOOL"),  # tool_config_group
                    gr.update(visible=stype == "TRANSFORM"),  # transform_config_group
                    gr.update(visible=stype == "MEMORY"),  # memory_config_group
                    gr.update(visible=stype == "MCP"),  # mcp_config_group
                    gr.update(visible=stype == "CONDITIONAL"),  # conditional_config_group
                    gr.update(visible=stype == "STRUCTURED_OUTPUT"),  # structured_output_config_group
                )

            # -- Add step --
            def _add_step(steps, radio_val, size_limit):
                steps = list(steps or [])
                stype = _parse_step_type_from_radio(radio_val)
                next_num = len(steps) + 1

                # Size limit check
                try:
                    if size_limit not in (None, "", 0) and int(size_limit) > 0 and next_num > int(size_limit):
                        return (
                            (
                                gr.update(value=steps),
                                gr.update(value=_format_chain_preview_html(steps)),
                                gr.update(value=_update_chain_size_indicator(steps, size_limit)),
                                gr.update(choices=_build_step_choices(steps)),
                                None,
                            )
                            + _show_form_for_type(stype, None)
                            + (
                                # field defaults
                                gr.update(value=""),
                                gr.update(value=False),
                                gr.update(choices=[], value=[]),  # deps
                                gr.update(value=""),
                                gr.update(value=""),
                                gr.update(value='["problem"]'),
                                gr.update(value=""),
                                gr.update(value=""),
                            )
                        )
                except Exception:
                    pass

                new_step = _default_step(next_num, stype)
                steps.append(new_step)

                form_updates = _show_form_for_type(stype, next_num)
                dep_choices = _build_deps_choices(steps, next_num)
                dep_values = _deps_values_from_list(new_step["dependencies"], steps, next_num)

                return (
                    (
                        gr.update(value=steps),
                        gr.update(value=_format_chain_preview_html(steps)),
                        gr.update(value=_update_chain_size_indicator(steps, size_limit)),
                        gr.update(choices=_build_step_choices(steps), value=_build_step_choices(steps)[-1]),
                        len(steps) - 1,  # current_editing_step
                    )
                    + form_updates
                    + (
                        gr.update(value=new_step["title"]),
                        gr.update(value=new_step.get("frozen", False)),
                        gr.update(choices=dep_choices, value=dep_values),  # deps (choices + value combined)
                        gr.update(value=""),
                        gr.update(value=""),
                        gr.update(value='["problem"]'),
                        gr.update(value=""),
                        gr.update(value=""),
                    )
                )

            add_step_btn.click(
                _add_step,
                inputs=[steps_state, step_type_radio, chain_size_limit_input],
                outputs=[
                    steps_state,
                    chain_preview,
                    chain_size_indicator,
                    selected_step_dropdown,
                    current_editing_step,
                    step_form_group,
                    step_editor_title,
                    step_type_display,
                    llm_fields_group,
                    tool_config_group,
                    transform_config_group,
                    memory_config_group,
                    mcp_config_group,
                    conditional_config_group,
                    structured_output_config_group,
                    # field defaults
                    title_input,
                    frozen_checkbox,
                    dependencies_checkboxgroup,
                    aim_input,
                    reasoning_questions_input,
                    step_context_queries_input,
                    stage_action_input,
                    example_reasoning_input,
                ],
            )

            # -- Edit step --
            # Total output count: 1 (editing_idx) + 10 (form) + 3 (common) + 5 (llm) + 4 (tool)
            #   + 5 (transform) + 5 (memory) + 5 (mcp) + 3 (conditional) + 4 (structured_output) = 45
            _N_EDIT_OUTPUTS = 45

            def _edit_step(steps, choice):
                step_num = _step_number_from_choice(choice)
                if not steps or step_num is None:
                    return (None, gr.update(visible=False)) + tuple(gr.update() for _ in range(_N_EDIT_OUTPUTS - 2))

                idx = step_num - 1
                if idx < 0 or idx >= len(steps):
                    return (None, gr.update(visible=False)) + tuple(gr.update() for _ in range(_N_EDIT_OUTPUTS - 2))

                s = steps[idx]
                stype = s.get("step_type", "LLM")
                cfg = s.get("step_config", {}) or {}
                form_updates = _show_form_for_type(stype, step_num)
                dep_choices = _build_deps_choices(steps, step_num)
                dep_values = _deps_values_from_list(s.get("dependencies", []), steps, step_num)

                return (
                    (
                        idx,  # current_editing_step
                    )
                    + form_updates
                    + (
                        # Common fields
                        gr.update(value=s.get("title", "")),
                        gr.update(value=bool(s.get("frozen", False))),
                        gr.update(choices=dep_choices, value=dep_values),  # deps (single update)
                        # LLM fields
                        gr.update(value=s.get("aim", "")),
                        gr.update(value=s.get("reasoning_questions", "")),
                        gr.update(value=json.dumps(s.get("step_context_queries", []))),
                        gr.update(value=s.get("stage_action", "")),
                        gr.update(value=s.get("example_reasoning", "")),
                        # TOOL
                        gr.update(value=cfg.get("tool_name", "")),
                        gr.update(value=cfg.get("tool_description", "")),
                        gr.update(
                            value=json.dumps(cfg.get("input_mapping", {}))
                            if isinstance(cfg.get("input_mapping"), dict)
                            else str(cfg.get("input_mapping", "{}"))
                        ),
                        gr.update(value=cfg.get("timeout", 120)),
                        # TRANSFORM
                        gr.update(value=cfg.get("transform_type", "extract")),
                        gr.update(value=cfg.get("input_key", "$history[-1]")),
                        gr.update(value=cfg.get("expression", "")),
                        gr.update(value=cfg.get("output_format", "")),
                        gr.update(value=cfg.get("map_template", "")),
                        # MEMORY
                        gr.update(value=cfg.get("operation", "read")),
                        gr.update(value=cfg.get("namespace", "default")),
                        gr.update(value=cfg.get("memory_key", cfg.get("key", ""))),
                        gr.update(value=cfg.get("value_source", "")),
                        gr.update(
                            value=str(cfg.get("default_value", "")) if cfg.get("default_value") is not None else ""
                        ),
                        # MCP
                        gr.update(
                            value=cfg.get("server", {}).get("server_name", "")
                            if isinstance(cfg.get("server"), dict)
                            else str(cfg.get("server", ""))
                        ),
                        gr.update(value=cfg.get("tool_name", "")),
                        gr.update(
                            value=json.dumps(cfg.get("arguments", {}))
                            if isinstance(cfg.get("arguments"), dict)
                            else "{}"
                        ),
                        gr.update(
                            value=json.dumps(cfg.get("argument_mapping", {}))
                            if isinstance(cfg.get("argument_mapping"), dict)
                            else "{}"
                        ),
                        gr.update(value=cfg.get("timeout", 30)),
                        # CONDITIONAL
                        gr.update(
                            value=json.dumps(cfg.get("branches", [])) if isinstance(cfg.get("branches"), list) else "[]"
                        ),
                        gr.update(value=cfg.get("default_step")),
                        gr.update(value=cfg.get("condition_context_key", "$history[-1]")),
                        # STRUCTURED_OUTPUT
                        gr.update(
                            value=json.dumps(cfg.get("output_schema", {}))
                            if isinstance(cfg.get("output_schema"), dict)
                            else str(cfg.get("output_schema", "{}"))
                        ),
                        gr.update(value=cfg.get("prompt_template", "")),
                        gr.update(value=cfg.get("model", "")),
                        gr.update(value=cfg.get("temperature", 0.0)),
                    )
                )

            _edit_outputs = [
                current_editing_step,
                step_form_group,
                step_editor_title,
                step_type_display,
                llm_fields_group,
                tool_config_group,
                transform_config_group,
                memory_config_group,
                mcp_config_group,
                conditional_config_group,
                structured_output_config_group,
                # common fields
                title_input,
                frozen_checkbox,
                dependencies_checkboxgroup,
                # LLM
                aim_input,
                reasoning_questions_input,
                step_context_queries_input,
                stage_action_input,
                example_reasoning_input,
                # TOOL
                tool_name_input,
                tool_description_input,
                tool_input_mapping_input,
                tool_timeout_input,
                # TRANSFORM
                transform_type_input,
                transform_input_key_input,
                transform_expression_input,
                transform_output_format_input,
                transform_map_template_input,
                # MEMORY
                memory_operation_input,
                memory_namespace_input,
                memory_key_input,
                memory_value_source_input,
                memory_default_value_input,
                # MCP
                mcp_server_name_input,
                mcp_tool_name_input,
                mcp_arguments_input,
                mcp_argument_mapping_input,
                mcp_timeout_input,
                # CONDITIONAL
                conditional_branches_input,
                conditional_default_step_input,
                conditional_condition_context_key_input,
                # STRUCTURED_OUTPUT
                structured_output_schema_input,
                structured_output_prompt_input,
                structured_output_model_input,
                structured_output_temperature_input,
            ]

            edit_step_btn.click(
                _edit_step,
                inputs=[steps_state, selected_step_dropdown],
                outputs=_edit_outputs,
            )
            selected_step_dropdown.change(
                _edit_step,
                inputs=[steps_state, selected_step_dropdown],
                outputs=_edit_outputs,
            )

            # -- Toggle freeze --
            def _toggle_freeze(steps, choice, size_limit):
                steps = list(steps or [])
                step_num = _step_number_from_choice(choice)
                if step_num is None or step_num < 1 or step_num > len(steps):
                    return gr.update(), gr.update(), gr.update(), gr.update()
                idx = step_num - 1
                steps[idx]["frozen"] = not steps[idx].get("frozen", False)
                return (
                    gr.update(value=steps),
                    gr.update(value=_format_chain_preview_html(steps)),
                    gr.update(value=_update_chain_size_indicator(steps, size_limit)),
                    gr.update(
                        choices=_build_step_choices(steps), value=_build_step_choices(steps)[idx] if steps else None
                    ),
                )

            toggle_freeze_btn.click(
                _toggle_freeze,
                inputs=[steps_state, selected_step_dropdown, chain_size_limit_input],
                outputs=[steps_state, chain_preview, chain_size_indicator, selected_step_dropdown],
            )

            # -- Remove step --
            def _remove_step(steps, choice, size_limit):
                steps = list(steps or [])
                step_num = _step_number_from_choice(choice)
                if step_num is None or step_num < 1 or step_num > len(steps):
                    return gr.update(), gr.update(), gr.update(), gr.update(), gr.update(visible=False), None
                idx = step_num - 1
                del steps[idx]
                # Renumber
                for i, s in enumerate(steps, start=1):
                    old_num = s["number"]
                    s["number"] = i
                    deps = s.get("dependencies", [])
                    s["dependencies"] = sorted([d - 1 if d > step_num else d for d in deps if d != step_num])
                choices = _build_step_choices(steps)
                return (
                    gr.update(value=steps),
                    gr.update(value=_format_chain_preview_html(steps)),
                    gr.update(value=_update_chain_size_indicator(steps, size_limit)),
                    gr.update(choices=choices, value=choices[-1] if choices else None),
                    gr.update(visible=False),  # hide form
                    None,
                )

            remove_step_btn.click(
                _remove_step,
                inputs=[steps_state, selected_step_dropdown, chain_size_limit_input],
                outputs=[
                    steps_state,
                    chain_preview,
                    chain_size_indicator,
                    selected_step_dropdown,
                    step_form_group,
                    current_editing_step,
                ],
            )

            # -- Move step up --
            def _move_step_up(steps, choice, size_limit):
                steps = list(steps or [])
                step_num = _step_number_from_choice(choice)
                if step_num is None or step_num <= 1 or step_num > len(steps):
                    return gr.update(), gr.update(), gr.update(), gr.update()
                idx = step_num - 1
                # Swap with previous step
                steps[idx], steps[idx - 1] = steps[idx - 1], steps[idx]
                # Renumber
                for i, s in enumerate(steps, start=1):
                    s["number"] = i
                new_idx = idx - 1
                choices = _build_step_choices(steps)
                return (
                    gr.update(value=steps),
                    gr.update(value=_format_chain_preview_html(steps)),
                    gr.update(value=_update_chain_size_indicator(steps, size_limit)),
                    gr.update(choices=choices, value=choices[new_idx] if new_idx < len(choices) else None),
                )

            move_up_btn.click(
                _move_step_up,
                inputs=[steps_state, selected_step_dropdown, chain_size_limit_input],
                outputs=[steps_state, chain_preview, chain_size_indicator, selected_step_dropdown],
            )

            # -- Move step down --
            def _move_step_down(steps, choice, size_limit):
                steps = list(steps or [])
                step_num = _step_number_from_choice(choice)
                if step_num is None or step_num < 1 or step_num >= len(steps):
                    return gr.update(), gr.update(), gr.update(), gr.update()
                idx = step_num - 1
                # Swap with next step
                steps[idx], steps[idx + 1] = steps[idx + 1], steps[idx]
                # Renumber
                for i, s in enumerate(steps, start=1):
                    s["number"] = i
                new_idx = idx + 1
                choices = _build_step_choices(steps)
                return (
                    gr.update(value=steps),
                    gr.update(value=_format_chain_preview_html(steps)),
                    gr.update(value=_update_chain_size_indicator(steps, size_limit)),
                    gr.update(choices=choices, value=choices[new_idx] if new_idx < len(choices) else None),
                )

            move_down_btn.click(
                _move_step_down,
                inputs=[steps_state, selected_step_dropdown, chain_size_limit_input],
                outputs=[steps_state, chain_preview, chain_size_indicator, selected_step_dropdown],
            )

            # -- Duplicate step --
            def _duplicate_step(steps, choice, size_limit):
                steps = list(steps or [])
                step_num = _step_number_from_choice(choice)
                if step_num is None or step_num < 1 or step_num > len(steps):
                    return gr.update(), gr.update(), gr.update(), gr.update()
                # Size limit check
                try:
                    if size_limit not in (None, "", 0) and int(size_limit) > 0 and len(steps) >= int(size_limit):
                        return gr.update(), gr.update(), gr.update(), gr.update()
                except Exception:
                    pass
                idx = step_num - 1
                new_step = dict(steps[idx])
                new_step["title"] = (new_step.get("title", "") or "") + " (copy)"
                new_step["frozen"] = False
                steps.insert(idx + 1, new_step)
                # Renumber
                for i, s in enumerate(steps, start=1):
                    s["number"] = i
                choices = _build_step_choices(steps)
                return (
                    gr.update(value=steps),
                    gr.update(value=_format_chain_preview_html(steps)),
                    gr.update(value=_update_chain_size_indicator(steps, size_limit)),
                    gr.update(choices=choices, value=choices[idx + 1] if idx + 1 < len(choices) else None),
                )

            duplicate_step_btn.click(
                _duplicate_step,
                inputs=[steps_state, selected_step_dropdown, chain_size_limit_input],
                outputs=[steps_state, chain_preview, chain_size_indicator, selected_step_dropdown],
            )

            # -- Save step --
            def _save_step(
                steps,
                editing_idx,
                title,
                frozen_flag,
                deps_values,
                aim,
                reasoning_questions,
                ctx_raw,
                stage_action,
                example_reasoning,
                tool_name,
                tool_desc,
                tool_mapping_raw,
                tool_timeout,
                tr_type,
                tr_input_key,
                tr_expr,
                tr_output_format,
                tr_map_template,
                mem_op,
                mem_ns,
                mem_key,
                mem_val_src,
                mem_default,
                mcp_srv,
                mcp_tool,
                mcp_args_raw,
                mcp_argmap_raw,
                mcp_timeout,
                cond_branches_raw,
                cond_default,
                cond_ctx_key,
                so_schema_raw,
                so_prompt,
                so_model,
                so_temp,
                size_limit,
            ):
                steps = list(steps or [])
                if editing_idx is None or editing_idx < 0 or editing_idx >= len(steps):
                    return (
                        gr.update(value=steps),
                        gr.update(value=_format_chain_preview_html(steps)),
                        gr.update(value=_update_chain_size_indicator(steps, size_limit)),
                        gr.update(choices=_build_step_choices(steps)),
                        gr.update(visible=False),
                        None,
                    )

                s = steps[editing_idx]
                stype = str(s.get("step_type", "LLM")).upper()

                s["title"] = str(title or "").strip()
                s["frozen"] = bool(frozen_flag)
                s["dependencies"] = _deps_from_values(deps_values or [])

                # LLM fields
                s["aim"] = str(aim or "").strip()
                s["reasoning_questions"] = str(reasoning_questions or "").strip()
                s["stage_action"] = str(stage_action or "").strip()
                s["example_reasoning"] = str(example_reasoning or "").strip()
                try:
                    ctx = json.loads(ctx_raw) if ctx_raw else []
                    if not isinstance(ctx, list):
                        ctx = []
                except Exception:
                    ctx = []
                s["step_context_queries"] = ctx

                # Type-specific config
                step_config = {}
                if stype == "TOOL":
                    try:
                        im = json.loads(tool_mapping_raw) if tool_mapping_raw else {}
                        if not isinstance(im, dict):
                            im = {}
                    except Exception:
                        im = {}
                    step_config = {
                        "tool_name": str(tool_name or ""),
                        "tool_description": str(tool_desc or ""),
                        "input_mapping": im,
                        "timeout": int(tool_timeout) if tool_timeout not in (None, "") else 120,
                    }
                elif stype == "TRANSFORM":
                    step_config = {
                        "transform_type": str(tr_type or "extract"),
                        "input_key": str(tr_input_key or "$history[-1]"),
                    }
                    if tr_expr:
                        step_config["expression"] = str(tr_expr)
                    if tr_output_format:
                        step_config["output_format"] = str(tr_output_format)
                    if tr_map_template:
                        step_config["map_template"] = str(tr_map_template)
                elif stype == "MEMORY":
                    step_config = {
                        "operation": str(mem_op or "read"),
                        "namespace": str(mem_ns or "default"),
                        "memory_key": str(mem_key or ""),
                    }
                    if mem_op in ("write", "append") and mem_val_src:
                        step_config["value_source"] = str(mem_val_src)
                    if mem_default:
                        try:
                            step_config["default_value"] = json.loads(mem_default)
                        except Exception:
                            step_config["default_value"] = str(mem_default)
                elif stype == "MCP":
                    try:
                        args = json.loads(mcp_args_raw) if mcp_args_raw else {}
                    except Exception:
                        args = {}
                    try:
                        argmap = json.loads(mcp_argmap_raw) if mcp_argmap_raw else {}
                    except Exception:
                        argmap = {}
                    step_config = {
                        "server": {"server_name": str(mcp_srv or ""), "transport": "stdio"},
                        "tool_name": str(mcp_tool or ""),
                        "arguments": args,
                        "argument_mapping": argmap,
                        "timeout": float(mcp_timeout) if mcp_timeout not in (None, "") else 60.0,
                    }
                elif stype == "CONDITIONAL":
                    try:
                        branches = json.loads(cond_branches_raw) if cond_branches_raw else []
                    except Exception:
                        branches = []
                    step_config = {
                        "branches": branches,
                        "default_step": int(cond_default) if cond_default not in (None, "") else None,
                        "condition_context_key": str(cond_ctx_key or "$history[-1]"),
                    }
                elif stype == "STRUCTURED_OUTPUT":
                    try:
                        schema = json.loads(so_schema_raw) if so_schema_raw else {}
                    except Exception:
                        schema = {}
                    step_config = {"output_schema": schema}
                    if so_prompt:
                        step_config["prompt_template"] = str(so_prompt)
                    if so_model:
                        step_config["model"] = str(so_model)
                    if so_temp is not None and so_temp != "":
                        try:
                            step_config["temperature"] = float(so_temp)
                        except Exception:
                            pass

                if step_config:
                    s["step_config"] = step_config
                elif "step_config" in s:
                    del s["step_config"]

                choices = _build_step_choices(steps)
                return (
                    gr.update(value=steps),
                    gr.update(value=_format_chain_preview_html(steps)),
                    gr.update(value=_update_chain_size_indicator(steps, size_limit)),
                    gr.update(choices=choices, value=choices[editing_idx] if editing_idx < len(choices) else None),
                    gr.update(visible=False),
                    None,
                )

            save_step_btn.click(
                _save_step,
                inputs=[
                    steps_state,
                    current_editing_step,
                    title_input,
                    frozen_checkbox,
                    dependencies_checkboxgroup,
                    aim_input,
                    reasoning_questions_input,
                    step_context_queries_input,
                    stage_action_input,
                    example_reasoning_input,
                    tool_name_input,
                    tool_description_input,
                    tool_input_mapping_input,
                    tool_timeout_input,
                    transform_type_input,
                    transform_input_key_input,
                    transform_expression_input,
                    transform_output_format_input,
                    transform_map_template_input,
                    memory_operation_input,
                    memory_namespace_input,
                    memory_key_input,
                    memory_value_source_input,
                    memory_default_value_input,
                    mcp_server_name_input,
                    mcp_tool_name_input,
                    mcp_arguments_input,
                    mcp_argument_mapping_input,
                    mcp_timeout_input,
                    conditional_branches_input,
                    conditional_default_step_input,
                    conditional_condition_context_key_input,
                    structured_output_schema_input,
                    structured_output_prompt_input,
                    structured_output_model_input,
                    structured_output_temperature_input,
                    chain_size_limit_input,
                ],
                outputs=[
                    steps_state,
                    chain_preview,
                    chain_size_indicator,
                    selected_step_dropdown,
                    step_form_group,
                    current_editing_step,
                ],
            )

            # -- Cancel editing --
            cancel_step_btn.click(
                lambda: (gr.update(visible=False), None),
                outputs=[step_form_group, current_editing_step],
            )

            # -- Evolution mode toggle --
            evolution_mode_input.change(
                lambda mode: gr.update(visible=(mode == "single_step")),
                inputs=[evolution_mode_input],
                outputs=[evolution_step_number_input],
            )

            # -- Chain size limit change --
            chain_size_limit_input.change(
                lambda steps, lim: gr.update(value=_update_chain_size_indicator(steps, lim)),
                inputs=[steps_state, chain_size_limit_input],
                outputs=[chain_size_indicator],
            )

            # -- Quick templates --
            def _apply_steps_list(raw_steps: List[dict], size_limit):
                steps = [dict(s) for s in raw_steps]
                choices = _build_step_choices(steps)
                return (
                    gr.update(value=steps),
                    gr.update(value=_format_chain_preview_html(steps)),
                    gr.update(value=_update_chain_size_indicator(steps, size_limit)),
                    gr.update(choices=choices, value=choices[0] if choices else None),
                    gr.update(visible=False),
                    None,
                    gr.update(value=_steps_to_chain_json(steps)),
                )

            def _apply_template(tpl_name: str, size_limit):
                tpl = CHAIN_TEMPLATES.get(tpl_name, {})
                return _apply_steps_list(tpl.get("steps", []), size_limit)

            # ── JSON Editor ↔ Visual Builder sync ─────────────────────

            def _export_steps_to_json(steps):
                """Visual Builder → JSON Editor."""
                steps = steps or []
                json_text = _steps_to_chain_json(steps)
                return (
                    gr.update(value=json_text),
                    '<div style="color:#27AE60;font-size:12px;">✅ Exported from Visual Builder</div>',
                )

            export_json_btn.click(
                _export_steps_to_json,
                inputs=[steps_state],
                outputs=[chain_json_editor, json_status],
            )

            def _apply_json_to_visual(json_text, size_limit):
                """JSON Editor → Visual Builder."""
                try:
                    steps = _chain_json_to_steps(json_text)
                    if not steps:
                        return (
                            gr.update(),
                            gr.update(),
                            gr.update(),
                            gr.update(),
                            gr.update(visible=False),
                            None,
                            gr.update(),
                            '<div style="color:#E74C3C;font-size:12px;">❌ No valid steps found in JSON</div>',
                        )
                    choices = _build_step_choices(steps)
                    return (
                        gr.update(value=steps),
                        gr.update(value=_format_chain_preview_html(steps)),
                        gr.update(value=_update_chain_size_indicator(steps, size_limit)),
                        gr.update(choices=choices, value=choices[0] if choices else None),
                        gr.update(visible=False),
                        None,
                        gr.update(value=_steps_to_chain_json(steps)),
                        '<div style="color:#27AE60;font-size:12px;">✅ Applied to Visual Builder</div>',
                    )
                except Exception as e:
                    return (
                        gr.update(),
                        gr.update(),
                        gr.update(),
                        gr.update(),
                        gr.update(),
                        None,
                        gr.update(),
                        f'<div style="color:#E74C3C;font-size:12px;">❌ JSON error: {e}</div>',
                    )

            apply_json_btn.click(
                _apply_json_to_visual,
                inputs=[chain_json_editor, chain_size_limit_input],
                outputs=[
                    steps_state,
                    chain_preview,
                    chain_size_indicator,
                    selected_step_dropdown,
                    step_form_group,
                    current_editing_step,
                    chain_json_editor,
                    json_status,
                ],
            )

            # ── Generic preset helper ────────────────────────────────
            _preset_outputs = [
                name_input,
                description_input,
                data_file_input,
                dataset_info,
                target_field_input,
                max_iterations_input,
                chain_size_limit_input,
                dataset_size_input,
                test_size_input,
                evolution_mode_input,
                evolution_step_number_input,
                dataset_path_state,
                create_output,
                steps_state,
                chain_preview,
                chain_size_indicator,
                selected_step_dropdown,
                step_form_group,
                current_editing_step,
                chain_json_editor,
            ]

            def _make_preset(
                preset_name: str,
                preset_description: str,
                template_key: str,
                chain_limit: int,
                target_field: str = "answer",
                max_iters: int = 100,
                test_sz: float = 0.2,
                evo_mode: str = "full_chain",
                demo_dataset: str = "hotpotqa_demo",
            ):
                """Generic factory that builds a one-click preset callback."""

                def _preset_fn():
                    (
                        steps_update,
                        preview_update,
                        indicator_update,
                        dropdown_update,
                        form_hide_update,
                        editing_update,
                        chain_json_update,
                    ) = _apply_template(template_key, chain_limit)

                    # Auto-upload demo dataset
                    dataset_path = ""
                    dataset_info_update = gr.update(value="Loading demo dataset…")
                    create_output_update = gr.update(value="")
                    try:
                        up = self.exp_manager.upload_example_dataset(demo_dataset)
                        if isinstance(up, dict) and up.get("data_path") and "error" not in up:
                            dataset_path = str(up.get("data_path") or "")
                            fname = str(up.get("filename") or f"{demo_dataset}.csv")
                            dataset_info_update = gr.update(value=f"📁 Using demo dataset: {fname}")
                        else:
                            err = up.get("error") if isinstance(up, dict) else "unknown_error"
                            dataset_info_update = gr.update(value="⚠️ Failed to auto-load demo dataset")
                            create_output_update = gr.update(value=f"❌ Failed to load demo dataset: {err}")
                    except Exception as e:
                        dataset_info_update = gr.update(value="⚠️ Failed to auto-load demo dataset")
                        create_output_update = gr.update(value=f"❌ Failed to load demo dataset: {e}")

                    return (
                        gr.update(value=preset_name),  # name_input (Textbox)
                        gr.update(value=preset_description),  # description_input (Textbox)
                        gr.update(value=None),  # data_file_input (File)
                        dataset_info_update,  # dataset_info (Textbox)
                        gr.update(value=target_field),  # target_field_input (Textbox)
                        gr.update(value=max_iters),  # max_iterations_input (Slider)
                        gr.update(value=chain_limit),  # chain_size_limit_input (Number)
                        gr.update(value=None),  # dataset_size_input (Number)
                        gr.update(value=test_sz),  # test_size_input (Number)
                        gr.update(value=evo_mode),  # evolution_mode_input (Radio)
                        gr.update(value=None, visible=False),  # evolution_step_number_input
                        dataset_path,  # dataset_path_state (gr.State — raw value!)
                        create_output_update,  # create_output (Textbox)
                        steps_update,  # steps_state (gr.State — raw value!)
                        preview_update,  # chain_preview (HTML)
                        indicator_update,  # chain_size_indicator (HTML)
                        dropdown_update,  # selected_step_dropdown (Dropdown)
                        form_hide_update,  # step_form_group (Group)
                        editing_update,  # current_editing_step (gr.State — raw value!)
                        chain_json_update,
                    )

                return _preset_fn

            def _apply_dataset_preset(
                chain_limit,
                preset_name: str,
                display_name: str,
                task_description: str,
            ):
                """Load train.csv from storage + LLM chain using ``$outer_context`` / ``step_N_output`` (see ``master_api/carl_examples``)."""
                tpl_steps = DATASET_CARL_LL_PRESETS.get(preset_name, [])
                (
                    steps_update,
                    preview_update,
                    indicator_update,
                    dropdown_update,
                    form_hide_update,
                    editing_update,
                    json_u,
                ) = _apply_steps_list(tpl_steps, chain_limit)

                dataset_info_update = gr.update(value="No file selected")
                file_update = gr.update(value=None)
                dataset_size_update = gr.update(value=None)
                create_output_update = gr.update(value="")

                preset_target_field = "target"
                try:
                    preset_info = self.exp_manager.get_local_prompt_preset(preset_name)
                    if preset_info and preset_info.get("target_field"):
                        preset_target_field = str(preset_info.get("target_field"))
                except Exception as e:
                    logger.debug(f"Could not get preset configuration for {preset_name}: {e}")

                target_str = preset_target_field

                try:
                    if not self.bucket_name:
                        self.bucket_name = self.status_service.get_storage_status()

                    bucket_name = self.bucket_name or STORAGE_BUCKET_NAME

                    if bucket_name:
                        dataset_paths = [
                            f"prompt_data/{preset_name}/train.csv",
                            f"data/{preset_name}/train.csv",
                        ]
                        local_path = None

                        for ds_path in dataset_paths:
                            logger.debug(
                                f"Trying to download {preset_name} dataset from: {ds_path} (bucket: {bucket_name})"
                            )
                            local_path = download_preset_dataset(ds_path, bucket_name)
                            if local_path and os.path.exists(local_path):
                                logger.info(f"Successfully downloaded {preset_name} dataset to: {local_path}")
                                break

                        if local_path and os.path.exists(local_path):
                            file_update = gr.update(value=local_path)
                            dataset_info_update = gr.update(
                                value=f"📁 Using preset dataset: {os.path.basename(local_path)}"
                            )

                            try:
                                file_columns = read_csv_columns(local_path)
                                target_choices = get_default_target_choices("classification", file_columns)
                                if preset_target_field in target_choices:
                                    target_str = preset_target_field
                                elif "target" in target_choices:
                                    target_str = "target"
                                elif target_choices:
                                    target_str = target_choices[0]

                                row_count = count_csv_rows(local_path)
                                if row_count is not None and row_count > 0:
                                    dataset_size_update = gr.update(value=row_count)
                            except Exception as e:
                                logger.warning(f"Failed to process {preset_name} dataset {local_path}: {e}")
                        else:
                            logger.warning(
                                f"Failed to download {preset_name} dataset from any of the paths: {dataset_paths}"
                            )
                            dataset_info_update = gr.update(value="⚠️ Failed to load preset dataset")
                    else:
                        logger.error(f"No bucket name available for downloading {preset_name} dataset")
                        dataset_info_update = gr.update(value="⚠️ No storage bucket configured")
                except Exception as e:
                    logger.error(f"Error loading {preset_name} preset dataset: {e}", exc_info=True)
                    dataset_info_update = gr.update(value="⚠️ Error loading preset dataset")

                return (
                    gr.update(value=display_name),
                    gr.update(value=task_description),
                    file_update,
                    dataset_info_update,
                    gr.update(value=target_str),
                    gr.update(),
                    gr.update(value=chain_limit),
                    dataset_size_update,
                    gr.update(),
                    gr.update(value="full_chain"),
                    gr.update(value=None, visible=False),
                    "",
                    create_output_update,
                    steps_update,
                    preview_update,
                    indicator_update,
                    dropdown_update,
                    form_hide_update,
                    None,
                    json_u,
                )

            # Sentiment Simple preset
            btn_sentiment_simple.click(
                _make_preset(
                    "Sentiment Analysis (2-step)",
                    "Classify text sentiment (positive/negative/neutral). 2-step chain: analyze → verify.",
                    "Sentiment Classification",
                    chain_limit=4,
                    target_field="sentiment",
                    demo_dataset="sentiment_demo",
                    max_iters=100,
                ),
                outputs=_preset_outputs,
            )

            # Step Type Showcase presets
            btn_hotpotqa_tool.click(
                _make_preset(
                    "HotpotQA with TOOL (retrieve + answer)",
                    "Lightweight 2-step chain: TOOL retrieves passages → LLM answers from context. Tests TOOL + LLM evolution.",
                    "TOOL + LLM QA",
                    chain_limit=4,
                    target_field="answer",
                    demo_dataset="hotpotqa_demo",
                    max_iters=100,
                ),
                outputs=_preset_outputs,
            )

            btn_transform_demo.click(
                _make_preset(
                    "TRANSFORM Step Demonstration",
                    "Shows TRANSFORM step: LLM reasons → TRANSFORM extracts clean answer. Demonstrates data transformation.",
                    "QA + Transform (extract)",
                    chain_limit=4,
                    target_field="answer",
                    demo_dataset="hotpotqa_demo",
                    max_iters=100,
                ),
                outputs=_preset_outputs,
            )

            btn_conditional_demo.click(
                _make_preset(
                    "CONDITIONAL Step Demonstration",
                    "Shows CONDITIONAL routing: LLM classifies → CONDITIONAL routes → different answer paths. Demonstrates conditional logic.",
                    "QA + Conditional routing",
                    chain_limit=6,
                    target_field="answer",
                    demo_dataset="hotpotqa_demo",
                    max_iters=100,
                ),
                outputs=_preset_outputs,
            )

            btn_mixed_pipeline.click(
                _make_preset(
                    "Mixed Pipeline: TOOL + LLM + TRANSFORM",
                    "Full pipeline: TOOL retrieves → LLM answers → TRANSFORM extracts. Demonstrates combining multiple step types.",
                    "LLM + TOOL + Transform pipeline",
                    chain_limit=5,
                    target_field="answer",
                    demo_dataset="hotpotqa_demo",
                    max_iters=100,
                ),
                outputs=_preset_outputs,
            )

            btn_ds_gsm8k.click(
                lambda sl: _apply_dataset_preset(
                    sl,
                    "gsm8k",
                    "GSM8K Chain Evolution",
                    "Evolve reasoning chains for GSM8K math word problems",
                ),
                inputs=[chain_size_limit_input],
                outputs=_preset_outputs,
            )
            btn_ds_commonsense.click(
                lambda sl: _apply_dataset_preset(
                    sl,
                    "commonsense",
                    "Commonsense QA Chain Evolution",
                    "Evolve reasoning chains for commonsense question answering",
                ),
                inputs=[chain_size_limit_input],
                outputs=_preset_outputs,
            )
            btn_ds_emotion.click(
                lambda sl: _apply_dataset_preset(
                    sl,
                    "emotion",
                    "Emotion Classification Chain Evolution",
                    "Evolve reasoning chains for emotion classification",
                ),
                inputs=[chain_size_limit_input],
                outputs=_preset_outputs,
            )

            # -- Create experiment --
            def _create(
                name,
                description,
                data_file,
                max_iterations,
                llm_model,
                chain_llm_model,
                target_field,
                steps,
                chain_size_limit,
                dataset_size,
                test_size,
                dataset_path,
                evolution_mode,
                evolution_step_number,
                python_code,
                enable_feedback,
                feedback_template,
                enable_memory,
                experiment_memory_namespace,
                validation_type,
                binary_validation_method,
                regexp_pattern,
                continuous_metric,
            ):
                try:
                    if not name:
                        return "❌ Please enter an experiment name"
                    if not target_field:
                        return "❌ Please specify the target column"
                    steps = steps or []
                    if not steps:
                        return "❌ Please add at least one step to the chain"

                    evo_mode = str(evolution_mode or "full_chain").strip()
                    if evo_mode == "single_step":
                        if evolution_step_number in (None, "", 0):
                            return "❌ Please provide a step number for single_step mode"
                        evo_step = int(evolution_step_number)
                        if evo_step < 1 or evo_step > len(steps):
                            return f"❌ Step number {evo_step} is out of range (1-{len(steps)})"

                    steps_for_json = []
                    frozen_steps = []
                    for s in steps:
                        s_clean = {
                            "number": int(s.get("number", 0)),
                            "title": s.get("title", ""),
                            "aim": s.get("aim", ""),
                            "reasoning_questions": s.get("reasoning_questions", ""),
                            "dependencies": s.get("dependencies", []) or [],
                            "step_context_queries": s.get("step_context_queries", []) or [],
                            "stage_action": s.get("stage_action", ""),
                            "example_reasoning": s.get("example_reasoning", ""),
                        }
                        # IMPORTANT: CARL (mmar_carl) step_type values are lowercase ("tool", "mcp", ...).
                        # The UI uses uppercase labels; normalize here before sending to Master API/Runner.
                        stype_ui = str(s.get("step_type", "LLM")).upper()
                        if stype_ui != "LLM":
                            s_clean["step_type"] = stype_ui.lower()
                            step_cfg = dict(s.get("step_config", {}) or {})
                            # Normalize tool_name casing for env-registered tools (registered as lowercase).
                            if (
                                s_clean["step_type"] == "tool"
                                and "tool_name" in step_cfg
                                and isinstance(step_cfg["tool_name"], str)
                            ):
                                step_cfg["tool_name"] = step_cfg["tool_name"].strip().lower()
                            s_clean["step_config"] = step_cfg
                        steps_for_json.append(s_clean)
                        if s.get("frozen", False) and s_clean["number"]:
                            frozen_steps.append(int(s_clean["number"]))

                    # Check if trying to evolve a frozen step in single_step mode
                    if evo_mode == "single_step" and evolution_step_number not in (None, "", 0):
                        evo_step = int(evolution_step_number)
                        if evo_step in frozen_steps:
                            step_title = next(
                                (s.get("title", f"Step {evo_step}") for s in steps if s.get("number") == evo_step),
                                f"Step {evo_step}",
                            )
                            return f"⚠️ Step {evo_step} ({step_title}) is frozen and cannot be evolved. Please unfreeze it or choose a different step to evolve."

                    validation_criteria = {
                        "validation_type": validation_type or "Binary (0/1)",
                        "binary_method": binary_validation_method or "equality",
                        "regexp_pattern": regexp_pattern or "",
                        "continuous_metric": continuous_metric or "",
                    }

                    chain_cfg = {"steps": steps_for_json}
                    payload = {
                        "name": name,
                        "description": description,
                        "target_column": target_field,
                        "base_chain_config": json.dumps(chain_cfg, indent=2),
                        "llm_model": llm_model,
                        "max_iterations": int(max_iterations),
                        "frozen_steps": sorted(set(frozen_steps)) if frozen_steps else None,
                        "chain_size_limit": int(chain_size_limit) if chain_size_limit not in (None, "", 0) else None,
                        "evolution_mode": evo_mode,
                        "validation_criteria": validation_criteria,
                    }

                    # Chain execution model (optional, different from evolution model)
                    if chain_llm_model and str(chain_llm_model).strip():
                        payload["chain_llm_model"] = str(chain_llm_model).strip()

                    if evo_mode == "single_step" and evolution_step_number not in (None, "", 0):
                        payload["step_number"] = int(evolution_step_number)

                    if (data_file and hasattr(data_file, "name")) and not dataset_path:
                        try:
                            fname = os.path.basename(getattr(data_file, "name", "dataset.csv"))
                            res = self.exp_manager.upload_data_file(getattr(data_file, "name"), fname)
                            dataset_path = res.get("data_path") or ""
                        except Exception as ue:
                            logger.error(f"Failed to upload data file: {ue}")
                            dataset_path = ""
                    if dataset_path:
                        payload["data_path"] = dataset_path
                    else:
                        return "❌ Please upload a dataset file"
                    if dataset_size not in (None, "", 0):
                        try:
                            payload["dataset_size"] = int(dataset_size)
                        except Exception:
                            pass
                    if test_size not in (None, ""):
                        try:
                            payload["test_size"] = float(test_size)
                        except Exception:
                            pass
                    result = self.exp_manager.create_carl_chain_experiment(
                        payload,
                        python_code=python_code,
                        enable_feedback=enable_feedback,
                        feedback_template=feedback_template,
                        enable_memory=enable_memory,
                        memory_namespace=experiment_memory_namespace,
                    )
                    if "error" in result:
                        return f"❌ {result['error']}"
                    return f"✅ Experiment '{name}' created. ID: {result.get('id', 'n/a')}"
                except Exception as e:
                    logger.error(f"Failed to create CARL experiment: {e}", exc_info=True)
                    return f"❌ {str(e)}"

            create_btn.click(
                _create,
                inputs=[
                    name_input,
                    description_input,
                    data_file_input,
                    max_iterations_input,
                    llm_model_input,
                    chain_llm_model_input,
                    target_field_input,
                    steps_state,
                    chain_size_limit_input,
                    dataset_size_input,
                    test_size_input,
                    dataset_path_state,
                    evolution_mode_input,
                    evolution_step_number_input,
                    python_code_editor,
                    enable_feedback_checkbox,
                    feedback_template_radio,
                    enable_memory_checkbox,
                    experiment_memory_namespace_input,
                    validation_type_input,
                    binary_validation_method_input,
                    regexp_pattern_input,
                    continuous_metric_input,
                ],
                outputs=[create_output],
            )

            # -- Validation type change handlers --
            def _handle_validation_type_change(vt):
                if vt == "Binary (0/1)":
                    return (
                        gr.update(visible=True),
                        gr.update(value="equality", visible=True),
                        gr.update(visible=False),
                        gr.update(visible=False),
                        gr.update(value=None, visible=False),
                    )
                elif vt == "Continuous (0..1)":
                    return (
                        gr.update(visible=False),
                        gr.update(value=None, visible=False),
                        gr.update(visible=False),
                        gr.update(visible=True),
                        gr.update(value=None, visible=True),
                    )
                return (
                    gr.update(visible=True),
                    gr.update(value="equality", visible=True),
                    gr.update(visible=False),
                    gr.update(visible=False),
                    gr.update(value=None, visible=False),
                )

            validation_type_input.change(
                _handle_validation_type_change,
                inputs=[validation_type_input],
                outputs=[
                    binary_validation_group,
                    binary_validation_method_input,
                    regexp_pattern_input,
                    continuous_validation_group,
                    continuous_metric_input,
                ],
            )

            def _handle_binary_method_change(method):
                return gr.update(visible=(method == "regexp"))

            binary_validation_method_input.change(
                _handle_binary_method_change,
                inputs=[binary_validation_method_input],
                outputs=[regexp_pattern_input],
            )

            # -- Clean form --
            def _clean():
                return (
                    gr.update(value=""),
                    gr.update(value=""),
                    gr.update(value=None),
                    gr.update(value="No file selected"),
                    gr.update(value=""),
                    gr.update(value=[]),
                    gr.update(value=_format_chain_preview_html([])),
                    gr.update(value=_update_chain_size_indicator([], None)),
                    gr.update(choices=[], value=None),
                    gr.update(value=""),
                    gr.update(value=None),
                    gr.update(value=0.2),
                    gr.update(value=""),
                    gr.update(visible=False),
                    None,
                    gr.update(value='{"steps": []}'),  # chain_json_editor
                    gr.update(value=_PYTHON_TEMPLATE),  # python_code_editor
                )

            clean_btn.click(
                _clean,
                outputs=[
                    name_input,
                    description_input,
                    data_file_input,
                    dataset_info,
                    target_field_input,
                    steps_state,
                    chain_preview,
                    chain_size_indicator,
                    selected_step_dropdown,
                    dataset_path_state,
                    dataset_size_input,
                    test_size_input,
                    create_output,
                    step_form_group,
                    current_editing_step,
                    chain_json_editor,
                    python_code_editor,
                ],
            )

        return component
