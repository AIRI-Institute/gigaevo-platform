from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, List

import pandas as pd

import os
from openai import OpenAI
import httpx

from mmar_carl import (
    ReasoningChain,
    ReasoningContext,
    StepDescription,
    ContextSearchConfig,
    Language,
)

def _get_llm_api() -> Any:

    base_url = os.environ.get("LLM__BASE_URL")
    api_key = os.environ.get("LLM__API_KEY")
    model_name = os.environ.get("LLM__MODEL")
    endpoint_key = os.environ.get("LLM__ENDPOINT_KEY", "my_model")

    if not base_url or not api_key:
        raise RuntimeError("LLM__BASE_URL and/or LLM__API_KEY not set in environment")
    
    if not model_name:
        raise RuntimeError("LLM__MODEL not set in environment")

    verify_ssl_str = os.environ.get("LLM__VERIFY_SSL", "false").lower()
    verify_ssl = verify_ssl_str in {"1", "true", "yes"}

    http_client = httpx.Client(verify=verify_ssl) if not verify_ssl else None

    client = OpenAI(
        base_url=base_url,
        api_key=api_key,
        http_client=http_client,
    )

    class OpenAIWrapper:
        def __init__(self, client, model_name: str):
            self.client = client
            self.model_name = model_name

        def get_response_with_retries(self, prompt: str, retries: int = 3):
            resp = self.client.chat.completions.create(
                model=self.model_name,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.choices[0].message.content

    class SingleEndpointDict:
        def __init__(self, endpoint_key: str, wrapper: OpenAIWrapper):
            self._endpoint_key = endpoint_key
            self._endpoints = {endpoint_key: wrapper}

        def __getitem__(self, key: str):
            return self._endpoints.get(key, self._endpoints[self._endpoint_key])

    wrapper = OpenAIWrapper(client, model_name)
    return SingleEndpointDict(endpoint_key, wrapper)


def _format_row_as_context(row: pd.Series, target_column: str) -> str:
    context_parts = []
    for col in row.index:
        if col != target_column:
            context_parts.append(f"{col}:\n{row[col]}")
    
    context_text = "\n\n".join(context_parts)
    return f"{context_text}\n\nSolve the problem and compute the correct answer."


def _get_system_prompt() -> str:
    return """You are an expert problem solver with extensive experience in reasoning tasks.

Your approach should:
- Read the problem carefully and identify what is being asked
- Break down complex problems into smaller, manageable steps
- Show your reasoning clearly at each step
- Perform operations accurately
- Verify your answer makes sense in the context of the problem
- Provide the final answer clearly

Focus on accuracy and step-by-step reasoning."""


def _extract_numeric_answer(text: str) -> str:
    import re
    
    if not text:
        return ""
    
    matches = re.findall(r"[-+]?\d+\.?\d*", text)
    if matches:
        return matches[-1]
    
    matches = re.findall(r"\d+", text)
    return matches[-1] if matches else ""


def _create_chain_from_config(chain_config: Dict[str, Any]) -> ReasoningChain:
    steps_data = chain_config.get("steps", [])
    steps = []
    
    for step_data in steps_data:
        step = StepDescription.model_validate(step_data)
        steps.append(step)
    
    search_config = None
    if "search_config" in chain_config:
        search_cfg_data = chain_config["search_config"]
        if search_cfg_data.get("strategy") == "substring":
            search_config = ContextSearchConfig(
                strategy="substring",
                substring_config=search_cfg_data.get("substring_config", {}),
            )
        elif search_cfg_data.get("strategy") == "vector":
            search_config = ContextSearchConfig(
                strategy="vector",
                vector_config=search_cfg_data.get("vector_config", {}),
            )
    
    chain = ReasoningChain(
        steps=steps,
        max_workers=chain_config.get("max_workers", 2),
        enable_progress=chain_config.get("enable_progress", False),
        search_config=search_config,
    )
    
    return chain


def _run_split(chain_config: Dict[str, Any], df: pd.DataFrame, target_column: str) -> List[Dict[str, Any]]:
    if df is None or len(df) == 0:
        return []

    try:
        chain = _create_chain_from_config(chain_config)
        api = _get_llm_api()
    except Exception as e:
        import sys
        print(f"Error creating chain or API: {e}", file=sys.stderr)
        return []

    results: List[Dict[str, Any]] = []
    
    CHAIN_TIMEOUT = int(os.environ.get("CARL__CHAIN_TIMEOUT", "30"))

    for idx, (_, row) in enumerate(df.iterrows()):
        try:
            outer_context = _format_row_as_context(row, target_column)

            endpoint_key = os.environ.get("LLM__ENDPOINT_KEY", "my_model")
            
            ctx = ReasoningContext(
                outer_context=outer_context,
                api=api,
                endpoint_key=endpoint_key,
                language=Language.ENGLISH,
                retry_max=2,
                system_prompt=_get_system_prompt(),
            )

            start = time.time()
            
            try:
                exec_result = chain.execute(ctx)
                elapsed = time.time() - start
                
                if elapsed > CHAIN_TIMEOUT:
                    exec_result.success = False
            except Exception as exec_error:
                elapsed = time.time() - start
                import sys
                print(f"Error executing chain for row {idx}: {exec_error}", file=sys.stderr)
                class FailedResult:
                    success = False
                    def get_final_output(self):
                        return ""
                exec_result = FailedResult()

            output_text = exec_result.get_final_output() if hasattr(exec_result, 'get_final_output') else ""
            prediction = _extract_numeric_answer(output_text)
            ground_truth = str(row[target_column])
            
            step_results = []
            chain_history = []
            full_output = ""
            
            if hasattr(exec_result, 'step_results'):
                for step_result in exec_result.step_results:
                    step_results.append({
                        "step_number": step_result.step_number,
                        "step_title": step_result.step_title,
                        "result": step_result.result,
                        "success": step_result.success,
                        "execution_time": step_result.execution_time,
                        "error_message": step_result.error_message,
                    })
            
            if hasattr(exec_result, 'history'):
                chain_history = exec_result.history
            
            if hasattr(exec_result, 'get_full_output'):
                full_output = exec_result.get_full_output()

            results.append(
                {
                    "prediction": prediction,
                    "ground_truth": ground_truth,
                    "success": bool(exec_result.success),
                    "time": float(elapsed),
                    "step_results": step_results,
                    "chain_history": chain_history,
                    "full_output": full_output,
                }
            )
        except TimeoutError as e:
            import sys
            print(f"Timeout processing row {idx}: {e}", file=sys.stderr)
            results.append(
                {
                    "prediction": "",
                    "ground_truth": str(row.get(target_column, "")),
                    "success": False,
                    "time": float(CHAIN_TIMEOUT),
                }
            )
        except Exception as e:
            import sys
            print(f"Error processing row {idx}: {e}", file=sys.stderr)
            results.append(
                {
                    "prediction": "",
                    "ground_truth": str(row.get(target_column, "")),
                    "success": False,
                    "time": 0.0,
                }
            )

    return results


def _fix_json_common_errors(json_str: str) -> str:
    json_str = json_str.strip()
    json_str = re.sub(r'}\s*{', '},{', json_str)  # } { -> },
    json_str = re.sub(r']\s*\[', '],[', json_str)  # ] [ -> ],
    json_str = re.sub(r'"\s*"', '","', json_str)   # " " -> ","
    json_str = re.sub(r'(\d)\s*"', r'\1,"', json_str)  # число " -> число,"
    json_str = re.sub(r'"\s*(\d)', r'",\1', json_str)  # " число -> ",число    
    return json_str


def parse_chain_config_json(chain_config_json: str) -> Dict[str, Any]:
    cfg = None
    json_str = chain_config_json.strip()
    original_error = None
    
    try:
        cfg = json.loads(json_str)
    except json.JSONDecodeError as e:
        original_error = e
        try:
            fixed_json = _fix_json_common_errors(json_str)
            cfg = json.loads(fixed_json)
        except (json.JSONDecodeError, Exception) as e2:
      
            start_idx = json_str.find('{')
            end_idx = json_str.rfind('}')
            if start_idx >= 0 and end_idx > start_idx:
                try:
                    extracted = json_str[start_idx:end_idx + 1]
                    fixed_extracted = _fix_json_common_errors(extracted)
                    cfg = json.loads(fixed_extracted)
                except json.JSONDecodeError:
                    try:
                        fixed_chars = ""
                        for char in extracted:
                            code = ord(char)
                            if 0 <= code <= 0x1F:
                                if char == '\n':
                                    fixed_chars += '\\n'
                                elif char == '\r':
                                    fixed_chars += '\\r'
                                elif char == '\t':
                                    fixed_chars += '\\t'
                                elif char == '\b':
                                    fixed_chars += '\\b'
                                elif char == '\f':
                                    fixed_chars += '\\f'
                                else:
                                    fixed_chars += f'\\u{code:04x}'
                            else:
                                fixed_chars += char
                        cfg = json.loads(fixed_chars)
                    except (json.JSONDecodeError, Exception):
                        try:
                            import json5
                            cfg = json5.loads(json_str)
                        except (ImportError, Exception):
                            import sys
                            error_msg = f"Invalid JSON in CHAIN_CONFIG_JSON: {original_error}"
                            print(f"ERROR: {error_msg}", file=sys.stderr)
                            print(f"JSON (first 500 chars): {json_str[:500]}", file=sys.stderr)
                            raise ValueError(error_msg) from original_error
            else:
                import sys
                error_msg = f"Invalid JSON in CHAIN_CONFIG_JSON: {original_error}"
                print(f"ERROR: {error_msg}", file=sys.stderr)
                print(f"JSON (first 500 chars): {json_str[:500]}", file=sys.stderr)
                raise ValueError(error_msg) from original_error
    
    if cfg is None:
        raise ValueError("Failed to parse CHAIN_CONFIG_JSON")
    
    return cfg


def execute_chain_on_dataset(chain_config: Dict[str, Any], df: pd.DataFrame, target_column: str) -> List[Dict[str, Any]]:
    return _run_split(chain_config, df, target_column)
