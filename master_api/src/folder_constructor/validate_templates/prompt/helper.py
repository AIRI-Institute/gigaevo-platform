"""Helper module for ${task_name} prompt evolution task (self-contained)."""

import asyncio
import os
import re
from collections import defaultdict
from dataclasses import dataclass
from functools import partial
from typing import Any, Dict, List, Optional, Set, Tuple, TypedDict

import httpx
import openai
import yaml
import logging

# --- Prompt utilities (inline) ---
def get_dataset_columns(dataset, exclude_target: Optional[str] = None) -> Set[str]:
    columns = set(dataset.columns)
    if exclude_target:
        columns.discard(exclude_target)
    return columns


def extract_placeholders(prompt_template: str) -> Set[str]:
    placeholders: Set[str] = set()
    # Simple parser for {name} fields
    for match in re.finditer(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", prompt_template):
        placeholders.add(match.group(1))
    return placeholders


def validate_prompt_template(
    prompt_template: str,
    required_placeholders: Set[str],
    available_placeholders: Set[str],
) -> None:
    found_placeholders = extract_placeholders(prompt_template)

    missing_required = required_placeholders - found_placeholders
    if missing_required:
        raise ValueError(
            f"Missing required placeholder(s): {', '.join(sorted(missing_required))}. "
            f"Found: {', '.join(sorted(found_placeholders)) if found_placeholders else 'none'}"
        )

    invalid_placeholders = found_placeholders - available_placeholders
    if invalid_placeholders:
        raise ValueError(
            f"Invalid placeholder(s): {', '.join(sorted(invalid_placeholders))}. "
            f"Available: {', '.join(sorted(available_placeholders))}"
        )


def create_validator(
    required_placeholders: Set[str],
    available_placeholders: Set[str],
):
    def validator(prompt_template: str) -> None:
        validate_prompt_template(prompt_template, required_placeholders, available_placeholders)

    return validator


# --- LLM client wrapper (inline) ---
@dataclass
class CallLog:
    prompt_cost_utilization: float
    response_cost_utilization: float
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    model_name: str = ""


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def _extract_reasoning_tokens(usage: Any) -> int:
    details = getattr(usage, "completion_tokens_details", None)
    if details is None:
        return 0
    if isinstance(details, dict):
        return _safe_int(details.get("reasoning_tokens", 0))
    return _safe_int(getattr(details, "reasoning_tokens", 0))


def _get_async_client() -> openai.AsyncOpenAI:
    base_url_env = os.getenv("PROMPT_BASE_URL")
    if base_url_env:
        api_key_env = os.getenv("PROMPT_API_KEY", None)
        ssl_no_verify = (os.getenv("PROMPT_SSL_NO_VERIFY", "0") or "").strip().lower() in {"1", "true", "yes"}
        http_client = httpx.AsyncClient(verify=False) if ssl_no_verify else None
        os.environ["OPENAI_LOG"] = "debug" 
        logging.getLogger("openai").setLevel(logging.DEBUG)
        logging.getLogger("httpx").setLevel(logging.DEBUG)
        return openai.AsyncOpenAI(base_url=base_url_env, api_key=api_key_env, http_client=http_client)

    openrouter_key = os.getenv("PROMPT_OPENROUTER_API_KEY")
    if openrouter_key and openrouter_key != "None":
        base_url = "https://openrouter.ai/api/v1"
        ssl_no_verify = (os.getenv("PROMPT_SSL_NO_VERIFY", "0") or "").strip().lower() in {"1", "true", "yes"}
        http_client = httpx.AsyncClient(verify=False) if ssl_no_verify else None
        return openai.AsyncOpenAI(base_url=base_url, api_key=openrouter_key, http_client=http_client)

    vllm_config_path = "vllm/classifier/vllm_config.yaml"
    if os.path.exists(vllm_config_path):
        with open(vllm_config_path, "r") as f:
            config = yaml.safe_load(f)
        base_url = f"http://localhost:{config['port']}/v1"
        return openai.AsyncOpenAI(base_url=base_url, api_key="None")

    raise RuntimeError(
        "No LLM API configured.\n"
        "In this repo, Runner API is expected to provide PROMPT_BASE_URL / PROMPT_API_KEY "
        "based on repo-level llm_models.yml.\n"
        "Also supported: local vLLM via vllm/classifier/vllm_config.yaml."
    )


class ClientWrapper:
    PRICING = {
        "gemma-3-12b-it": {"prompt": 0.05, "completion": 0.10},
        "gemma-3-27b-it": {"prompt": 0.09, "completion": 0.16},
        "Qwen2.5-14B-Instruct": {"prompt": 0.06, "completion": 0.24},
        "Qwen2.5-32B-Instruct": {"prompt": 0.10, "completion": 0.28},
    }
    DEFAULT_PRICING = {"prompt": 0.05, "completion": 0.10}

    def __init__(self, model_name: str, max_cost: float = 10.0):
        self.client = _get_async_client()
        self.model_name = model_name
        self.max_cost = max_cost
        self._call_logs: List[CallLog] = []
        self._set_gen_kwargs()
        self._set_pricing()

    def _set_gen_kwargs(self) -> None:
        MODEL_CONFIGS = {
            "gemma": {"top_p": 0.95, "extra_body": {"top_k": 64}},
            "qwen": {"top_p": 0.8, "extra_body": {"top_k": 20}},
        }
        if "gemma" in self.model_name.lower():
            self.gen_kwargs = MODEL_CONFIGS["gemma"]
        elif "qwen" in self.model_name.lower():
            self.gen_kwargs = MODEL_CONFIGS["qwen"]
        else:
            self.gen_kwargs = {}

    def _set_pricing(self) -> None:
        model_pricing = self.DEFAULT_PRICING
        for name in self.PRICING.keys():
            if name.lower() in self.model_name.lower():
                model_pricing = self.PRICING[name]
                break
        self.model_pricing = model_pricing

    @property
    def call_logs(self) -> List[CallLog]:
        return self._call_logs

    async def __call__(self, prompt: str, temperature: float) -> str:
        response = await self.client.chat.completions.create(
            model=self.model_name,
            messages=[{"role": "user", "content": prompt}],
            temperature=temperature,
            **self.gen_kwargs,
        )
        content = response.choices[0].message.content or ""

        usage = getattr(response, "usage", None)
        prompt_tokens = _safe_int(getattr(usage, "prompt_tokens", 0)) if usage else 0
        completion_tokens = _safe_int(getattr(usage, "completion_tokens", 0)) if usage else 0
        total_tokens = _safe_int(getattr(usage, "total_tokens", 0)) if usage else 0
        if total_tokens <= 0 and (prompt_tokens > 0 or completion_tokens > 0):
            total_tokens = prompt_tokens + completion_tokens
        reasoning_tokens = _extract_reasoning_tokens(usage) if usage else 0

        M = 1_000_000
        prompt_cost = (prompt_tokens / M) * self.model_pricing["prompt"]
        response_cost = (completion_tokens / M) * self.model_pricing["completion"]
        call_log = CallLog(
            prompt_cost_utilization=prompt_cost / self.max_cost,
            response_cost_utilization=response_cost / self.max_cost,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            reasoning_tokens=reasoning_tokens,
            total_tokens=total_tokens,
            model_name=self.model_name,
        )
        self._call_logs.append(call_log)
        return content

    async def close(self):
        await self.client.close()


# --- Agent base and implementation (inline) ---
class OutputDict(TypedDict):
    predictions: List[Any]
    call_logs: List[List[CallLog]]


# Configuration from template placeholders
EXTRACTION_PATTERN = r"${regexp_pattern}"
FAILURE_VALUE = ${failure_value}


class PromptAgent:
    """Unified prompt agent with configurable extraction pattern."""

    def __init__(self, client: ClientWrapper, prompt_template: str):
        self.client = client
        self.prompt_template = prompt_template
        self.temperature = 0.0

    async def __call__(self, sample: dict[str, str]) -> Any:
        response = await self.client(
            prompt=self.prompt_template.format(**sample),
            temperature=self.temperature,
        )
        return self._extract_answer(response)

    def _extract_answer(self, response: str) -> Any:
        """Extract answer from LLM response using configured pattern."""
        if not EXTRACTION_PATTERN:
            # No pattern - return full response
            return response.strip()

        match = re.search(EXTRACTION_PATTERN, response, re.IGNORECASE | re.DOTALL)
        if match:
            return match.group(1).strip()
        return FAILURE_VALUE


async def _process_sample(
    agent_class: type[PromptAgent],
    model_name: str,
    max_cost: float,
    index: int,
    sample: dict,
    semaphore: asyncio.Semaphore,
) -> Tuple[int, Any, List[CallLog]]:
    async with semaphore:
        wrapper = ClientWrapper(model_name, max_cost=max_cost)
        agent = agent_class(wrapper)
        prediction = await agent(sample)
        call_logs = wrapper.call_logs.copy()
        await wrapper.close()
        if call_logs:
            total_cost_util = sum(
                cl.prompt_cost_utilization + cl.response_cost_utilization for cl in call_logs
            )
            if total_cost_util > 1.0:
                raise ValueError(
                    f"Cost budget exceeded: {total_cost_util:.2%} of max cost (${{max_cost}}) used"
                )
        return index, prediction, call_logs


async def _run_agent_async(
    agent_class: type[PromptAgent],
    context: dict,
    dataset_key: str = "train_dataset",
) -> OutputDict:
    model_name = context["model_name"]
    dataset = context.get(dataset_key)
    max_cost = context.get("max_cost", 1.0)
    if dataset is None:
        return OutputDict(predictions=[], call_logs=[])
    max_concurrent = 8
    semaphore = asyncio.Semaphore(max_concurrent)
    tasks = []
    for index, (_, row) in enumerate(dataset.iterrows()):
        sample = row.to_dict()
        task = asyncio.create_task(
            _process_sample(agent_class, model_name, max_cost, index, sample, semaphore)
        )
        tasks.append(task)
    results = await asyncio.gather(*tasks)
    results.sort(key=lambda x: x[0])
    predictions: List[Any] = []
    all_call_logs: List[List[CallLog]] = []
    for _, prediction, call_logs in results:
        predictions.append(prediction)
        all_call_logs.append(call_logs)
    return OutputDict(predictions=predictions, call_logs=all_call_logs)


def _sanitize_model_name(model_name: str) -> str:
    return str(model_name).replace("/", "_").replace(":", "_").strip()


def _log_get(call_log: Any, key: str, default: Any = 0) -> Any:
    try:
        return getattr(call_log, key)
    except Exception:
        pass
    try:
        return call_log.get(key, default)
    except Exception:
        return default


def _aggregate_token_usage(outputs: List[OutputDict], fallback_model_name: str) -> Dict[str, Dict[str, int]]:
    totals: Dict[str, Dict[str, int]] = defaultdict(
        lambda: {
            "context_tokens": 0,
            "generated_tokens": 0,
            "reasoning_tokens": 0,
            "total_tokens": 0,
        }
    )

    for output in outputs:
        for call_logs in output.get("call_logs", []):
            for call_log in call_logs:
                raw_model_name = _log_get(call_log, "model_name", fallback_model_name) or fallback_model_name
                model_name = _sanitize_model_name(raw_model_name)
                if not model_name:
                    continue

                prompt_tokens = _safe_int(_log_get(call_log, "prompt_tokens", 0))
                completion_tokens = _safe_int(_log_get(call_log, "completion_tokens", 0))
                reasoning_tokens = _safe_int(_log_get(call_log, "reasoning_tokens", 0))
                total_tokens = _safe_int(_log_get(call_log, "total_tokens", 0))
                if total_tokens <= 0 and (prompt_tokens > 0 or completion_tokens > 0):
                    total_tokens = prompt_tokens + completion_tokens

                bucket = totals[model_name]
                bucket["context_tokens"] += prompt_tokens
                bucket["generated_tokens"] += completion_tokens
                bucket["reasoning_tokens"] += reasoning_tokens
                bucket["total_tokens"] += total_tokens

    return dict(totals)


def _publish_token_usage_to_redis(token_totals: Dict[str, Dict[str, int]]) -> None:
    if not token_totals:
        return

    redis_host = (os.getenv("GIGAEVO_REDIS_HOST") or "").strip()
    redis_port_raw = (os.getenv("GIGAEVO_REDIS_PORT") or "").strip()
    redis_db_raw = (os.getenv("GIGAEVO_REDIS_DB") or "").strip()
    metrics_prefix = (os.getenv("GIGAEVO_METRICS_PREFIX") or "").strip()
    if not redis_host or not metrics_prefix:
        return

    try:
        redis_port = int(redis_port_raw or "6379")
        redis_db = int(redis_db_raw or "0")
    except ValueError:
        return

    client = None
    try:
        import redis  # type: ignore

        client = redis.Redis(
            host=redis_host,
            port=redis_port,
            db=redis_db,
            decode_responses=True,
        )
        hash_key = f"{metrics_prefix}:metrics:latest"
        pipe = client.pipeline(transaction=False)

        for model_name, usage in token_totals.items():
            metric_prefix = f"llm/tokens/default/{model_name}/"
            context_tokens = float(usage.get("context_tokens", 0))
            generated_tokens = float(usage.get("generated_tokens", 0))
            reasoning_tokens = float(usage.get("reasoning_tokens", 0))
            total_tokens = float(usage.get("total_tokens", 0))

            # Last snapshot fields (for parity with core tracker schema).
            pipe.hset(hash_key, metric_prefix + "context_tokens", context_tokens)
            pipe.hset(hash_key, metric_prefix + "generated_tokens", generated_tokens)
            pipe.hset(hash_key, metric_prefix + "reasoning_tokens", reasoning_tokens)
            pipe.hset(hash_key, metric_prefix + "total_tokens", total_tokens)

            # Cumulative fields consumed by summary/report code.
            pipe.hincrbyfloat(hash_key, metric_prefix + "cumulative_context_tokens", context_tokens)
            pipe.hincrbyfloat(hash_key, metric_prefix + "cumulative_generated_tokens", generated_tokens)
            pipe.hincrbyfloat(hash_key, metric_prefix + "cumulative_reasoning_tokens", reasoning_tokens)
            pipe.hincrbyfloat(hash_key, metric_prefix + "cumulative_total_tokens", total_tokens)

        pipe.execute()
    except Exception:
        # Token metrics publication should never break experiment execution.
        return
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass


def run_agent(prompt_template: str, context: dict):
    # Infer placeholders
    train_dataset = context["train_dataset"]
    model_name = str(context.get("model_name") or "")
    available_placeholders = get_dataset_columns(train_dataset, exclude_target="${target_field}")
    required_placeholders = {${required_fields_set}}
    validator = create_validator(required_placeholders, available_placeholders)
    validator(prompt_template)
    agent_cls_partial = partial(PromptAgent, prompt_template=prompt_template)
    train_output = asyncio.run(_run_agent_async(agent_cls_partial, context, "train_dataset"), debug=True)
    val_output = asyncio.run(_run_agent_async(agent_cls_partial, context, "val_dataset"), debug=True)
    token_totals = _aggregate_token_usage([train_output, val_output], fallback_model_name=model_name)
    _publish_token_usage_to_redis(token_totals)
    return {"train": train_output, "val": val_output}
