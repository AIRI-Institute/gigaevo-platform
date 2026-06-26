#!/usr/bin/env python3

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional
from urllib.parse import urlsplit, urlunsplit

from common.llm_registry import (
    get_default_llm_model_id,
    get_default_prompt_llm_model_id,
    get_llm_runtime_required,
    load_llm_registry,
)
from redis.asyncio import Redis

from ..config import load_config

logger = logging.getLogger(__name__)

_DONE_PROGRAM_STATE = "done"


def _extract_prompt_template(program_code: str) -> Optional[str]:
    """Extract PROMPT_TEMPLATE content from program code.

    Returns the extracted prompt template string, or None if extraction fails
    (indicating this is not a prompt experiment).
    """
    if not program_code:
        return None

    # Match PROMPT_TEMPLATE: str = """...""" or PROMPT_TEMPLATE = """..."""
    # Supports with/without type hint, triple/single quotes
    pattern = re.compile(
        r"PROMPT_TEMPLATE\s*(?::\s*str\s*)?=\s*(?P<q>\"\"\"|'''|\"|')(?P<body>.*?)(?P=q)",
        re.DOTALL,
    )
    m = pattern.search(program_code)
    if m:
        # Unescape escaped triple quotes from baseline creation
        body = m.group("body")
        body = body.replace('\\"\\"\\"', '"""').replace("\\'\\'\\'", "'''")
        return body
    return None


def _build_evolution_summary(df: Any, experiment_id: str, higher_is_better: bool) -> Dict[str, Any]:
    """Build report summary using current gigaevo-core completion semantics."""
    import pandas as pd  # type: ignore

    working_df = df.copy()

    if "metric_is_valid" in working_df.columns:
        working_df["metric_is_valid"] = pd.to_numeric(working_df["metric_is_valid"], errors="coerce").fillna(0.0)
    else:
        working_df["metric_is_valid"] = 0.0

    if "metric_fitness" in working_df.columns:
        working_df["metric_fitness"] = pd.to_numeric(working_df["metric_fitness"], errors="coerce")
    else:
        working_df["metric_fitness"] = float("nan")

    if "generation" in working_df.columns:
        working_df["generation"] = pd.to_numeric(working_df["generation"], errors="coerce").fillna(0).astype(int)
    else:
        working_df["generation"] = 0

    if "created_at" in working_df.columns:
        working_df["created_at"] = pd.to_datetime(working_df["created_at"], errors="coerce")
    else:
        working_df["created_at"] = pd.NaT

    # Use current gigaevo-core lifecycle semantics: successful completions are DONE programs only.
    if "state" in working_df.columns:
        done_mask = working_df["state"].astype(str).str.lower().eq(_DONE_PROGRAM_STATE)
    else:
        done_mask = pd.Series(False, index=working_df.index)

    valid_completed = working_df[done_mask & working_df["metric_is_valid"].ge(1)]

    best_row = None
    if not valid_completed.empty:
        ranked = valid_completed.sort_values(
            ["metric_fitness", "generation", "created_at"],
            ascending=[not higher_is_better, False, False],
            na_position="last",
        )
        best_row = ranked.head(1).iloc[0].to_dict()

    total_iterations = 0
    if "metadata_iteration" in working_df.columns:
        iter_series = pd.to_numeric(working_df["metadata_iteration"], errors="coerce")
        if not iter_series.empty and iter_series.notna().any():
            try:
                total_iterations = int(iter_series.max()) + 1
            except Exception:
                total_iterations = 0

    total_programs = int(working_df["program_id"].nunique()) if "program_id" in working_df.columns else 0

    if "program_id" in working_df.columns:
        try:
            total_programs_complete = int(working_df.loc[done_mask, "program_id"].nunique())
        except Exception:
            total_programs_complete = 0
    else:
        total_programs_complete = 0

    best_code_raw = best_row.get("code") if best_row else None
    best_code = best_code_raw if isinstance(best_code_raw, str) else None

    return {
        "experiment_id": experiment_id,
        "total_iterations": total_iterations,
        "total_programs": total_programs,
        "total_programs_complete": total_programs_complete,
        "best_program_id": (best_row.get("program_id") if best_row else None),
        "best_fitness": (
            float(best_row.get("metric_fitness")) if best_row and best_row.get("metric_fitness") is not None else None
        ),
        "best_generation": int(best_row.get("generation"))
        if best_row and best_row.get("generation") is not None
        else None,
        "best_created_at": str(best_row.get("created_at"))
        if best_row and best_row.get("created_at") is not None
        else None,
        "best_program": (_extract_prompt_template(best_code) or best_code) if best_row else None,
    }


class GigaEvolveService:
    def __init__(self):
        cfg = load_config()
        self.config = cfg.gigavolve
        self._build_info_name = ".buildinfo.json"

    def _repo_root(self) -> Path:
        return Path(self.config.clone_path).resolve()

    def _build_info_path(self) -> Path:
        return self._repo_root() / self._build_info_name

    def _read_build_info(self) -> Dict[str, Any]:
        path = self._build_info_path()
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _resolve_python_path(self, clone_path: Path) -> str:
        """Prefer repo-local venv python if present; otherwise fall back to configured python_path."""
        venv_python = clone_path / ".venv" / "bin" / "python"
        return str(venv_python) if venv_python.exists() else self.config.python_path

    @staticmethod
    def _normalize_problem_name(experiment_id: str) -> str:
        """Normalize experiment id into problem.name (used for Redis keys)."""
        return experiment_id.replace("exp_", "") if experiment_id.startswith("exp_") else experiment_id

    @staticmethod
    def _env_with_problem_dir(problem_dir: Path, *, base_env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        """
        Return env with PROBLEM_DIR set and PYTHONPATH prepended with problem dir (for helper imports).
        """
        env: Dict[str, str] = dict(base_env) if base_env is not None else dict(os.environ)
        problem_dir_str = str(problem_dir.resolve())

        # Provide problem directory for exec runner to resolve __file__ when executing user code,
        # and for tools that need to import helper modules from the problem directory.
        env["PROBLEM_DIR"] = problem_dir_str

        # Only prepend PYTHONPATH if the directory exists (preserve prior behavior).
        if problem_dir.exists():
            existing_pythonpath = env.get("PYTHONPATH", "")
            if existing_pythonpath:
                env["PYTHONPATH"] = f"{problem_dir_str}{os.pathsep}{existing_pythonpath}"
            else:
                env["PYTHONPATH"] = problem_dir_str
        return env

    def _redis_host_port_db(self) -> tuple[str, int, int]:
        """Resolve redis host/port/db from config (supports redis_url or legacy config)."""
        cfg = load_config()
        candidate_url = (cfg.gigavolve.redis_url or cfg.redis.url or "").strip()
        return self._parse_redis_url(candidate_url)

    @staticmethod
    def _resolve_memory_namespace(experiment_id: str, config: Dict[str, Any]) -> str:
        namespace = str(config.get("memory_namespace") or "").strip()
        return namespace or str(experiment_id)

    def _memory_checkpoint_dir(self, experiment_id: str) -> Path:
        return (self._repo_root() / "outputs" / str(experiment_id) / "memory").resolve()

    @staticmethod
    def _normalize_base_url(value: Any) -> str:
        return str(value or "").strip().rstrip("/").lower()

    def _ideas_tracker_analyzer_base_url(self, clone_path: Path) -> Optional[str]:
        memory_config_path = clone_path / "config" / "memory.yaml"
        if not memory_config_path.exists():
            return None

        try:
            import yaml  # type: ignore

            payload = yaml.safe_load(memory_config_path.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            logger.warning("Failed to read memory config for Ideas Tracker LLM env: %s", exc)
            return None

        if not isinstance(payload, dict):
            return None
        ideas_tracker_cfg = payload.get("ideas_tracker") or {}
        if not isinstance(ideas_tracker_cfg, dict):
            return None
        analyzer_cfg = ideas_tracker_cfg.get("analyzer") or {}
        if not isinstance(analyzer_cfg, dict):
            return None

        base_url = str(analyzer_cfg.get("base_url") or "").strip()
        return base_url or None

    def _ideas_tracker_llm_runtime(self, clone_path: Path) -> Dict[str, Any]:
        desired_base_url = self._normalize_base_url(self._ideas_tracker_analyzer_base_url(clone_path))
        registry = load_llm_registry()
        models = registry.get("models") if isinstance(registry, dict) else []
        if isinstance(models, list) and desired_base_url:
            for entry in models:
                if not isinstance(entry, dict):
                    continue
                runtime = entry.get("runtime") or {}
                if not isinstance(runtime, dict):
                    continue
                api_key = str(runtime.get("api_key") or "").strip()
                if not api_key:
                    continue
                if self._normalize_base_url(runtime.get("base_url")) == desired_base_url:
                    return runtime

        fallback_model_id = get_default_llm_model_id()
        return get_llm_runtime_required(fallback_model_id, required_keys={"api_key"})

    def _configure_ideas_tracker_llm_env(self, env: Dict[str, str], clone_path: Path) -> None:
        runtime = self._ideas_tracker_llm_runtime(clone_path)
        api_key = str(runtime.get("api_key") or "").strip()
        if not api_key:
            return

        env["OPENAI_API_KEY"] = api_key
        base_url = str(runtime.get("base_url") or "").strip()
        if base_url:
            env["OPENAI_BASE_URL"] = base_url

    def _memory_hydra_override(self) -> str:
        """
        Select the Hydra override syntax accepted by the baked gigaevo-core config.

        Some core branches include `memory: none` in config defaults, which expects
        `memory=api`. The current core-internal branch used here omits the memory
        group from defaults entirely, which requires `+memory=api`.
        """
        config_path = self._repo_root() / "config" / "config.yaml"
        try:
            payload = config_path.read_text(encoding="utf-8")
        except Exception:
            return "+memory=api"

        if re.search(r"^\s*-\s*(?:/)?memory\s*:\s*\S+", payload, flags=re.MULTILINE):
            return "memory=api"
        return "+memory=api"

    @staticmethod
    def _require_memory_api_url() -> str:
        memory_api_url = str(os.getenv("MEMORY_API_URL") or "").strip()
        if not memory_api_url:
            raise ValueError("MEMORY_API_URL is required when memory retrieval or upload is requested")
        return memory_api_url

    def _configure_memory_env(self, env: Dict[str, str], namespace: str) -> str:
        memory_api_url = self._require_memory_api_url()
        env.update(
            {
                "MEMORY_API_URL": memory_api_url,
                "MEMORY_NAMESPACE": namespace,
                "MEMORY_USE_API": "true",
            }
        )
        return memory_api_url

    def is_repository_ready(self) -> bool:
        """Check if baked repository files are ready for execution."""
        try:
            repo_root = self._repo_root()
            if not repo_root.exists() or not repo_root.is_dir():
                return False

            build_info = self._read_build_info()
            if not build_info:
                return False

            run_script = repo_root / "run.py"
            venv_python = repo_root / ".venv" / "bin" / "python"
            if not run_script.exists():
                return False
            if not venv_python.exists() and self.config.python_path == "python3":
                return False
            return True
        except Exception as e:
            logger.error(f"Error checking repository status: {e}")
            return False

    async def get_repository_info(self) -> Dict[str, Any]:
        """Get information about the baked repository."""
        try:
            if not self.is_repository_ready():
                return {"error": "Repository not ready"}

            clone_path = self._repo_root()
            build_info = self._read_build_info()
            tag = str(build_info.get("core_ref") or "") or None
            commit_hash = str(build_info.get("core_commit") or "") or None
            remote_url = str(build_info.get("core_repo_url") or "") or None
            branch = str(build_info.get("core_branch") or "") or None

            return {
                "path": str(clone_path),
                "tag": tag,
                "commit_hash": commit_hash,
                "remote_url": remote_url,
                "branch": branch,
                "is_ready": True,
                "is_mock": False,
                "build_info": build_info,
            }

        except Exception as e:
            logger.error(f"Error getting repository info: {e}")
            return {"error": str(e)}

    def _write_llm_config_for_model(self, clone_path: Path, model_id: str) -> None:
        """Write Hydra LLM config with runtime values from llm_models.yml for the selected model."""
        llm_dir = clone_path / "config" / "llm"
        llm_dir.mkdir(parents=True, exist_ok=True)
        cfg_path = llm_dir / "custom.yaml"

        runtime = get_llm_runtime_required(
            model_id,
            required_keys={
                "model",
                "api_key",
                "base_url",
                "temperature",
                "max_tokens",
                "top_p",
                "max_retries",
                "timeout",
            },
        )

        payload = {
            "llm": {
                "_target_": "gigaevo.llm.models.MultiModelRouter",
                "_convert_": "all",
                "models": [
                    {
                        "_target_": "langchain_openai.ChatOpenAI",
                        "model": runtime.get("model"),
                        "api_key": runtime.get("api_key"),
                        "temperature": float(runtime.get("temperature", 0.7)),
                        "max_tokens": int(runtime.get("max_tokens", 2048)),
                        "top_p": float(runtime.get("top_p", 1.0)),
                        "base_url": runtime.get("base_url"),
                        "request_timeout": int(runtime.get("request_timeout", 600)),
                        "max_retries": int(runtime.get("max_retries", 3)),
                        "timeout": int(runtime.get("timeout", 600)),
                    }
                ],
                "probabilities": [1.0],
                "writer": "${writer}",
            }
        }

        import yaml  # type: ignore

        text = "# @package _global_\n\n" + yaml.safe_dump(payload, sort_keys=False)
        cfg_path.write_text(text, encoding="utf-8")

    async def run_experiment(
        self,
        experiment_id: str,
        config: Dict[str, Any],
        cancel_check: Optional[Callable[[], Awaitable[bool]]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Run a GigaEvolve experiment with timeout and optional cancel-aware execution."""
        clone_path = Path(self.config.clone_path).resolve()
        try:
            run_script = clone_path / "run.py"
            if not run_script.exists():
                raise FileNotFoundError("Run script not found")

            problem_dir = clone_path / "problems" / str(experiment_id)
            python_path = self._resolve_python_path(clone_path)

            logger.info(f"Using Python path: {python_path}")

            cfg = load_config()
            problem_name = self._normalize_problem_name(experiment_id)

            cmd = [
                python_path,
                str(run_script),
                f"problem.name={problem_name}",  # Used by run.py for Redis keys
                f"problem.dir={problem_dir}",  # Full path with exp_ prefix for file system
            ]

            redis_host, redis_port, redis_db = self._redis_host_port_db()
            cmd.extend(
                [
                    f"redis.host={redis_host}",
                    f"redis.port={redis_port}",
                    f"redis.db={redis_db}",
                ]
            )

            # Add max generations if specified
            max_iterations = config.get("max_iterations")
            if max_iterations:
                cmd.append(f"max_generations={max_iterations}")

            # Use custom.yaml so LLM__* environment variables are picked up
            cmd.append("llm=custom")

            env = self._env_with_problem_dir(problem_dir)
            env.update(
                {
                    # Exposed for prompt helper runtime so it can publish eval-model
                    # token usage into the same Redis metrics hash.
                    "GIGAEVO_REDIS_HOST": str(redis_host),
                    "GIGAEVO_REDIS_PORT": str(redis_port),
                    "GIGAEVO_REDIS_DB": str(redis_db),
                    "GIGAEVO_METRICS_PREFIX": str(problem_name),
                }
            )

            # Add dataset configuration to environment variables
            dataset_size = config.get("dataset_size")
            if dataset_size is not None:
                env["DATASET_SIZE"] = str(int(dataset_size))
            test_size = config.get("test_size")
            if test_size is not None:
                env["TEST_SIZE"] = str(float(test_size))

            if config.get("enable_memory"):
                namespace = self._resolve_memory_namespace(experiment_id, config)
                checkpoint_dir = self._memory_checkpoint_dir(experiment_id)
                checkpoint_dir.mkdir(parents=True, exist_ok=True)
                self._configure_memory_env(env, namespace)
                cmd.extend(
                    [
                        self._memory_hydra_override(),
                        f"namespace={namespace}",
                        f"checkpoint_dir={checkpoint_dir}",
                    ]
                )
                logger.info(
                    "Memory enabled for %s: namespace=%s checkpoint_dir=%s",
                    experiment_id,
                    namespace,
                    checkpoint_dir,
                )

            # Limit validation samples for chain experiments to speed up validation
            # Use max 2 samples for train and 1 for val to keep validation very fast
            # This significantly reduces validation time from hours to under 2 minutes
            env["CARL__MAX_TRAIN_SAMPLES"] = "2"
            env["CARL__MAX_VAL_SAMPLES"] = "1"
            # Reduce chain timeout per example to fail faster on problematic cases
            env["CARL__CHAIN_TIMEOUT"] = "15"  # 15 seconds per chain execution

            # Resolve selected model id from experiment config and load runtime values from llm_models.yml
            selected_model_id = str(config.get("llm_model") or "").strip() or get_default_llm_model_id()
            runtime = get_llm_runtime_required(
                selected_model_id,
                required_keys={"model", "api_key", "base_url"},
            )
            llm_base_url = str(runtime.get("base_url") or "")
            llm_api_key = str(runtime.get("api_key") or "")
            llm_model = str(runtime.get("model") or selected_model_id)

            # PROMPT_* variables use a separate, independently configurable model id
            prompt_model_id = str(config.get("prompt_llm_model") or "").strip() or get_default_prompt_llm_model_id()
            prompt_runtime = get_llm_runtime_required(
                prompt_model_id,
                required_keys={"model", "api_key", "base_url"},
            )
            prompt_model = str(prompt_runtime.get("model") or prompt_model_id)

            # Ensure Hydra LLM config exists before running
            try:
                self._write_llm_config_for_model(clone_path, selected_model_id)
                cfg_path = clone_path / "config" / "llm" / "custom.yaml"
                if cfg_path.exists():
                    logger.info(f"LLM config file ready at {cfg_path}")
                else:
                    logger.error(f"LLM config file was not created at {cfg_path}")
            except Exception as cfg_err:
                logger.error(f"Failed to create LLM config: {cfg_err}", exc_info=True)
                raise RuntimeError(f"Cannot proceed without LLM config: {cfg_err}") from cfg_err

            # LLM env vars (values sourced from llm_models.yml)
            env.update(
                {
                    "LLM__BASE_URL": llm_base_url,
                    "LLM__API_KEY": llm_api_key,
                    "LLM__MODEL": llm_model,
                    "LLM__MAX_TOKENS": str(runtime.get("max_tokens", 2048)),
                    "LLM__REQUEST_TIMEOUT": str(runtime.get("request_timeout", 600.0)),
                    "LLM__MAX_RETRIES": str(runtime.get("max_retries", 3)),
                    "LLM__TIMEOUT": str(runtime.get("timeout", 600.0)),
                    "LLM__TEMPERATURE": str(runtime.get("temperature", 0.7)),
                    "LLM__TOP_P": str(runtime.get("top_p", 1.0)),
                    "OPENAI_API_KEY": llm_api_key,
                }
            )
            os.environ["OPENAI_LOG"] = "debug"

            # SSL bypass
            if runtime.get("ssl_no_verify") or prompt_runtime.get("ssl_no_verify") or cfg.gigavolve.ssl_bypass_enabled:
                env["LLM_SSL_NO_VERIFY"] = "1"
                env["PROMPT_SSL_NO_VERIFY"] = "1"
                logger.info("SSL verification bypass enabled for LLM requests")

            # Prompt-model env vars
            env.update(
                {
                    "PROMPT_MODEL_NAME": prompt_model,
                    "PROMPT_MODEL": prompt_model,
                    "PROMPT_BASE_URL": str(prompt_runtime.get("base_url") or ""),
                    "PROMPT_API_KEY": str(prompt_runtime.get("api_key") or ""),
                }
            )
            openrouter_key = str(prompt_runtime.get("openrouter_api_key") or "").strip()
            if openrouter_key:
                env["PROMPT_OPENROUTER_API_KEY"] = openrouter_key

            logger.info(
                f"LLM env: BASE_URL={llm_base_url[:50]}..., MODEL={llm_model}, "
                f"API_KEY={'SET' if llm_api_key else 'NOT SET'}"
            )

            # Run subprocess asynchronously to avoid blocking the event loop
            logger.info(f"Starting run.py for experiment {experiment_id} with command: {' '.join(cmd)}")
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(clone_path),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            # Use communicate() to consume stdout/stderr while polling for
            # cancellation.  This avoids the deadlock that occurs when the OS
            # pipe buffer fills up and the child blocks on write.
            # Experiment stops only when gigaevo-core finishes max_iterations.
            comm_task = asyncio.ensure_future(proc.communicate())
            cancelled = False

            while not comm_task.done():
                await asyncio.wait({comm_task}, timeout=2.0)
                if comm_task.done():
                    break

                # Consult the optional cancel callback
                if cancel_check is not None:
                    try:
                        if await cancel_check():
                            cancelled = True
                            break
                    except Exception as exc:
                        logger.debug(f"Cancel check failed for {experiment_id}: {exc}")

            # If the subprocess is still running (cancelled), kill it
            if not comm_task.done():
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                await proc.wait()
                comm_task.cancel()
                try:
                    await comm_task
                except asyncio.CancelledError:
                    pass

                if cancelled:
                    return {
                        "output": None,
                        "error": "Experiment cancelled",
                        "success": False,
                        "timed_out": False,
                    }

            stdout_b, stderr_b = comm_task.result()
            stdout = stdout_b.decode("utf-8", errors="replace") if stdout_b else ""
            stderr = stderr_b.decode("utf-8", errors="replace") if stderr_b else ""

            if proc.returncode == 0:
                return {"output": stdout, "error": None, "success": True, "timed_out": False}
            else:
                return {"output": None, "error": stderr or stdout, "success": False, "timed_out": False}
        except Exception as e:
            return {"output": None, "error": str(e), "success": False, "timed_out": False}

    async def run_ideas_tracker(
        self,
        experiment_id: str,
        config: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Run Ideas Tracker standalone to extract ideas and write MemoryCards."""
        clone_path = self._repo_root()
        namespace = self._resolve_memory_namespace(experiment_id, config)
        checkpoint_dir = self._memory_checkpoint_dir(experiment_id)
        checkpoint_dir.mkdir(parents=True, exist_ok=True)

        try:
            python_path = self._resolve_python_path(clone_path)
            redis_host, redis_port, redis_db = self._redis_host_port_db()
            problem_name = self._normalize_problem_name(experiment_id)
            logs_dir = Path(tempfile.gettempdir()) / "gigaevo_ideas_tracker_logs" / str(experiment_id)

            # Absolute path avoids namespace collision when problem dir has its own src/ package
            tracker_script = Path("/app/src/tools/run_ideas_tracker_from_redis.py")
            cmd = [
                python_path,
                str(tracker_script),
                "--config-path",
                str(clone_path / "config" / "memory.yaml"),
                "--redis-host",
                str(redis_host),
                "--redis-port",
                str(redis_port),
                "--redis-db",
                str(redis_db),
                "--redis-prefix",
                str(problem_name),
                "--checkpoint-dir",
                str(checkpoint_dir),
                "--logs-dir",
                str(logs_dir),
                "--memory-write",
            ]

            problem_dir = clone_path / "problems" / str(experiment_id)
            env = self._env_with_problem_dir(problem_dir)
            self._configure_memory_env(env, namespace)
            self._configure_ideas_tracker_llm_env(env, clone_path)

            logger.info(
                "Running Ideas Tracker for %s: namespace=%s checkpoint_dir=%s",
                experiment_id,
                namespace,
                checkpoint_dir,
            )
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(clone_path),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=600)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                await proc.wait()
                return {"success": False, "error": "Ideas Tracker timed out (600s)", "namespace": namespace}

            stdout = (stdout_b or b"").decode("utf-8", errors="replace")
            stderr = (stderr_b or b"").decode("utf-8", errors="replace")

            if proc.returncode == 0:
                logger.info("Ideas Tracker completed for %s", experiment_id)
                return {"success": True, "output": stdout, "namespace": namespace}

            logger.error("Ideas Tracker failed for %s: %s", experiment_id, stderr or stdout)
            return {"success": False, "error": stderr or stdout, "namespace": namespace}
        except ValueError:
            raise
        except Exception as e:
            logger.error(f"Ideas Tracker error for {experiment_id}: {e}")
            return {"success": False, "error": str(e), "namespace": namespace}

    async def generate_code_from_llm(self, prompt: str) -> Optional[str]:
        """Generate code using LLM (placeholder)"""
        # TODO: Implement LLM integration for code generation
        # This would connect to an LLM service to generate experiment code
        return None

    @staticmethod
    def _parse_redis_url(url_str: str) -> tuple[str, int, int]:
        """Parse a redis:// URL into (host, port, db). Raises ValueError if invalid."""
        if not url_str:
            raise ValueError("Empty Redis URL")
        parts = urlsplit(url_str)
        host = parts.hostname
        port = parts.port
        path_num = (parts.path or "").lstrip("/")
        if not path_num.isdigit():
            raise ValueError(f"Invalid Redis URL (db index missing): '{url_str}'")
        db = int(path_num)
        if not host or port is None:
            raise ValueError(f"Invalid Redis URL (host/port missing): '{url_str}'")
        return host, port, db

    @staticmethod
    def _redis_value_to_float(value: Any, field_name: str = "value") -> Optional[float]:
        """Convert Redis value to float. Returns None for empty/invalid values."""
        if value is None:
            return None

        # Convert to string and strip whitespace
        value_str = str(value).strip()
        if not value_str:
            return None

        try:
            # Convert to float to preserve precision and handle both integers and decimals
            return float(value_str)
        except (ValueError, TypeError, OverflowError) as e:
            logger.warning(f"Failed to convert Redis {field_name} '{value!r}': {e}")
            return None

    async def cleanup_experiment(self, experiment_id: str) -> bool:
        """Clean up experiment artifacts"""
        try:
            # TODO: Implement cleanup logic
            return True
        except Exception as e:
            print(f"Failed to cleanup experiment {experiment_id}: {e}")
            return False

    async def generate_evolution_plot(
        self,
        experiment_id: str,
        output_subfolder: str,
        *,
        iteration_col: str = "generation",
    ) -> dict:
        """
        Run tools.comparison to generate evolution plot for a single running experiment.
        Saves plot to <repo>/{output_subfolder}/evolution_runs_comparison.(png|pdf)
        """
        clone_path = Path(self.config.clone_path).resolve()
        try:
            python_path = self._resolve_python_path(clone_path)

            host, port, db = self._redis_host_port_db()

            # Extract problem name from experiment_id (remove 'exp_' prefix if present)
            # CRITICAL: Must match the problem.name used by run.py (without exp_ prefix)
            # run.py stores data in Redis using problem.name, so we must use the same name here
            problem_name = self._normalize_problem_name(experiment_id)

            # tools.comparison expects run spec <prefix>@<db>[:label]
            # The prefix must match what run.py used when storing data (problem.name without exp_)
            run_spec = f"{problem_name}@{db}:Run"

            # Ensure output folder exists
            out_dir = clone_path / output_subfolder
            out_dir.mkdir(parents=True, exist_ok=True)

            problem_dir = clone_path / "problems" / experiment_id

            # Build the command to run module tools.comparison
            cmd = [
                python_path,
                "-m",
                "tools.comparison",
                "--redis-host",
                host,
                "--redis-port",
                str(port),
                "--run",
                run_spec,
                "--iteration-col",
                iteration_col,
                "--output-folder",
                str(out_dir),
            ]

            env = self._env_with_problem_dir(problem_dir)
            logger.info(f"Setting PYTHONPATH to include problem directory: {env.get('PROBLEM_DIR')}")

            proc = await asyncio.create_subprocess_exec(*cmd, cwd=str(clone_path), env=env)
            # Enforce a timeout using experiment_timeout to avoid hanging results collection
            try:
                stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=self.config.experiment_timeout)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                return {"success": False, "error": "Results collection timed out"}
            stdout = (stdout_b or b"").decode("utf-8", errors="replace")
            stderr = (stderr_b or b"").decode("utf-8", errors="replace")

            success = proc.returncode == 0
            return {
                "success": success,
                "output_dir": str(out_dir),
                "stdout": stdout,
                "stderr": stderr,
                "output_png_file": str(out_dir / "evolution_runs_comparison.png"),
                "output_pdf_file": str(out_dir / "evolution_runs_comparison.pdf"),
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def generate_evolution_report(
        self,
        experiment_id: str,
        output_subfolder: str,
    ) -> dict:
        """
        Export comprehensive evolution data to CSV using tools.redis2pd, then build a JSON
        summary with main stats and the best_program code.
        Writes files under <repo>/{output_subfolder}/:
          - evolution_report.csv (intermediate)
          - evolution_report.json (final summary with best_program)
        """
        clone_path = Path(self.config.clone_path).resolve()
        try:
            python_path = self._resolve_python_path(clone_path)

            host, port, db = self._redis_host_port_db()

            # Extract problem name from experiment_id (remove 'exp_' prefix if present)
            # CRITICAL: Must match the problem.name used by run.py (without exp_ prefix)
            # run.py stores data in Redis using problem.name, so we must use the same name here
            problem_name = self._normalize_problem_name(experiment_id)

            # Ensure output folder exists
            out_dir = clone_path / output_subfolder
            out_dir.mkdir(parents=True, exist_ok=True)
            out_csv = out_dir / "evolution_report.csv"
            out_json = out_dir / "evolution_report.json"

            problem_dir = clone_path / "problems" / experiment_id

            # Build the command to run module tools.redis2pd
            # The redis-prefix must match what run.py used when storing data (problem.name without exp_)
            cmd = [
                python_path,
                "-m",
                "tools.redis2pd",
                "--redis-host",
                host,
                "--redis-port",
                str(port),
                "--redis-db",
                str(db),
                "--redis-prefix",
                f"{problem_name}",  # Must match problem.name from run.py (without exp_ prefix)
                "--output-file",
                str(out_csv),
            ]

            env = self._env_with_problem_dir(problem_dir)
            logger.info(f"Setting PYTHONPATH to include problem directory: {env.get('PROBLEM_DIR')}")

            proc = await asyncio.create_subprocess_exec(*cmd, cwd=str(clone_path), env=env)
            # Enforce a timeout using experiment_timeout
            try:
                stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=self.config.experiment_timeout)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                return {"success": False, "error": "Stats export timed out"}
            stdout = (stdout_b or b"").decode("utf-8", errors="replace")
            stderr = (stderr_b or b"").decode("utf-8", errors="replace")

            success = proc.returncode == 0
            if not success:
                return {"success": False, "stdout": stdout, "stderr": stderr}

            # Build JSON summary with pandas (required)
            try:
                import pandas as pd  # type: ignore
            except Exception as _e:
                return {"success": False, "error": f"pandas is required to build evolution_report.json: {_e}"}

            df = pd.read_csv(out_csv)

            # Determine fitness direction from workspace metrics.yml (higher_is_better)
            higher_is_better = True
            try:
                problem_dir = clone_path / "problems" / str(experiment_id)
                metrics_yaml = problem_dir / "metrics.yml"
                if metrics_yaml.exists():
                    try:
                        import yaml  # type: ignore

                        _cfg = yaml.safe_load(metrics_yaml.read_text(encoding="utf-8")) or {}
                        _specs = _cfg.get("specs", {}) or {}
                        _fitness = _specs.get("fitness", {}) or {}
                        hib = _fitness.get("higher_is_better", True)
                        if isinstance(hib, bool):
                            higher_is_better = hib
                    except Exception:
                        pass
            except Exception:
                pass
            summary = _build_evolution_summary(df, experiment_id, higher_is_better)

            # Add token counters
            try:
                try:
                    redis_host, redis_port, redis_db = self._redis_host_port_db()
                    redis_url = f"redis://{redis_host}:{redis_port}/{redis_db}"
                    problem_name = self._normalize_problem_name(experiment_id)
                    async with Redis.from_url(redis_url, decode_responses=True) as r:
                        hash_key = f"{problem_name}:metrics:latest"
                        # Redis can contain token metrics for multiple models.
                        # Collect all token-related metrics and write them into the report JSON
                        # with keys prefixed by model name.
                        token_root = "llm/tokens/"

                        all_fields = await r.hgetall(hash_key)
                        for field, raw_value in (all_fields or {}).items():
                            if not isinstance(field, str) or not field.startswith(token_root):
                                continue

                            rest = field[len(token_root) :]
                            parts = rest.split("/", 2)
                            if len(parts) != 3:
                                continue

                            tracker_part, model_part, metric_part = parts
                            tracker_name = str(tracker_part).strip()
                            model_name_raw = str(model_part).strip()
                            if not model_name_raw:
                                continue
                            # Preserve backward compatibility for default tracker naming.
                            model_name = (
                                model_name_raw
                                if tracker_name in {"", "default"}
                                else f"{tracker_name}:{model_name_raw}"
                            )

                            metric_norm = str(metric_part).replace("/", "_").strip()
                            value = self._redis_value_to_float(raw_value, metric_part)
                            if value is None:
                                continue

                            summary[f"{model_name}__{metric_norm}"] = value
                except Exception as redis_err:
                    logger.warning(f"Failed to fetch token metrics for {experiment_id}: {redis_err}")
            except Exception as e:
                logger.debug(f"Token metrics fetch failed for {experiment_id}: {e}")

            # Write JSON summary
            try:
                out_json.write_text(json.dumps(summary, indent=2), encoding="utf-8")
            except Exception as _e:
                return {"success": False, "error": f"Failed to write JSON summary: {_e}"}

            return {
                "success": True,
                "output_dir": str(out_dir),
                "output_json_file": str(out_json),
                "stdout": stdout,
                "stderr": stderr,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}
