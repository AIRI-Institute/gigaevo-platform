#!/usr/bin/env python3

import json
import logging
import os
import re
import tempfile
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse, Response

from ...services.database_service import DatabaseService
from ...services.service_manager import ServiceManager
from ...services.visualization_service import VisualizationService

logger = logging.getLogger(__name__)

router = APIRouter()

_service_manager: ServiceManager | None = None


def set_service_manager(service_manager: ServiceManager):
    global _service_manager
    _service_manager = service_manager


def _feedback_md_key_iteration(object_key: str) -> Optional[int]:
    m = re.search(r"/iteration_(\d+)/chain_feedback\.md$", object_key.replace("\\", "/"))
    return int(m.group(1)) if m else None


async def _discover_chain_feedback_keys(storage, experiment_id: str) -> Tuple[List[int], Dict[int, str]]:
    prefix = f"experiments_results/{experiment_id}/"
    keys = await storage.list_objects(prefix=prefix, recursive=True)
    it_to_key: Dict[int, str] = {}
    for k in keys:
        it = _feedback_md_key_iteration(k)
        if it is not None:
            it_to_key[it] = k
    return sorted(it_to_key.keys()), it_to_key


def _validation_debug_key_iteration(object_key: str) -> Optional[int]:
    m = re.search(r"/iteration_(\d+)/validation_debug\.log$", object_key.replace("\\", "/"))
    return int(m.group(1)) if m else None


async def _discover_validation_debug_keys(storage, experiment_id: str) -> Tuple[List[int], Dict[int, str]]:
    prefix = f"experiments_results/{experiment_id}/"
    keys = await storage.list_objects(prefix=prefix, recursive=True)
    it_to_key: Dict[int, str] = {}
    for k in keys:
        it = _validation_debug_key_iteration(k)
        if it is not None:
            it_to_key[it] = k
    return sorted(it_to_key.keys()), it_to_key


def _extract_chain_config_from_program_code(program_code: str) -> Optional[Dict[str, Any]]:
    """Parse CHAIN_CONFIG_JSON or BASE_CHAIN_CONFIG triple-quoted string from evolved program."""
    if not program_code or not program_code.strip():
        return None
    for var in ("CHAIN_CONFIG_JSON", "BASE_CHAIN_CONFIG"):
        pattern = re.compile(
            rf"{re.escape(var)}\s*(?::\s*str\s*)?=\s*(?P<q>\"\"\"|'''|\"|')(?P<body>.*?)(?P=q)",
            re.DOTALL,
        )
        m = pattern.search(program_code)
        if not m:
            continue
        body = m.group("body")
        body = body.replace('\\"\\"\\"', '"""').replace("\\'\\'\\'", "'''")
        try:
            parsed = json.loads(body.strip())
            if isinstance(parsed, dict) and isinstance(parsed.get("steps"), list):
                return parsed
        except json.JSONDecodeError:
            continue
    return None


def _empty_token_usage() -> Dict[str, Any]:
    return {
        "totals": {
            "prompt_tokens": None,
            "completion_tokens": None,
            "total_tokens": None,
        },
        "models": [],
    }


def _coerce_non_negative_int(value: Any) -> Optional[int]:
    try:
        number = int(float(value))
        return number if number >= 0 else None
    except (TypeError, ValueError):
        return None


def _pick_metric(entry: Dict[str, int], keys: list[str]) -> Optional[int]:
    for key in keys:
        value = entry.get(key)
        if value is not None:
            return value
    return None


def _extract_token_usage(parsed_report: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    # Source keys are written by runner as "<model>__<metric_norm>" in evolution_report.json,
    # where metric_norm is Redis metric_part with "/" replaced by "_".
    model_metrics: Dict[str, Dict[str, int]] = {}
    for key, raw_value in parsed_report.items():
        if not isinstance(key, str) or "__" not in key:
            continue

        model_name, metric_name = key.split("__", 1)
        model_name = model_name.strip()
        metric_name = metric_name.strip().lower()
        if not model_name or not metric_name:
            continue

        value = _coerce_non_negative_int(raw_value)
        if value is None:
            continue

        bucket = model_metrics.setdefault(model_name, {})
        metric_norm = metric_name.replace("-", "_")
        bucket[metric_norm] = value

    if not model_metrics:
        return None

    models = []
    totals_prompt = 0
    totals_completion = 0
    totals_total = 0
    has_prompt = False
    has_completion = False
    has_total = False

    for model_name in sorted(model_metrics):
        entry = model_metrics[model_name]
        # Prefer cumulative counters when both snapshot and cumulative values are present.
        prompt_tokens = _pick_metric(
            entry,
            [
                "cumulative_prompt_tokens",
                "cumulative_input_tokens",
                "cumulative_context_tokens",
                "prompt_tokens",
                "input_tokens",
                "context_tokens",
            ],
        )
        completion_tokens = _pick_metric(
            entry,
            [
                "cumulative_completion_tokens",
                "cumulative_output_tokens",
                "cumulative_response_tokens",
                "cumulative_generated_tokens",
                "completion_tokens",
                "output_tokens",
                "response_tokens",
                "generated_tokens",
            ],
        )
        total_tokens = _pick_metric(
            entry,
            [
                "cumulative_total_tokens",
                "total_tokens",
            ],
        )

        if total_tokens is None and (prompt_tokens is not None or completion_tokens is not None):
            total_tokens = (prompt_tokens or 0) + (completion_tokens or 0)

        if prompt_tokens is not None:
            totals_prompt += prompt_tokens
            has_prompt = True
        if completion_tokens is not None:
            totals_completion += completion_tokens
            has_completion = True
        if total_tokens is not None:
            totals_total += total_tokens
            has_total = True

        models.append(
            {
                "name": model_name,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
            }
        )

    if not has_total and (has_prompt or has_completion):
        totals_total = totals_prompt + totals_completion
        has_total = True

    return {
        "totals": {
            "prompt_tokens": totals_prompt if has_prompt else None,
            "completion_tokens": totals_completion if has_completion else None,
            "total_tokens": totals_total if has_total else None,
        },
        "models": models,
    }


@router.get("/results/{experiment_id}/visual_results.jpeg")
async def get_visual_result_image(experiment_id: str):
    """Serve visualization image for the experiment, generating if missing.

    Saves locally and uploads to S3 via StorageService.
    """
    # Use system temporary directory instead of /app
    base_dir = tempfile.gettempdir()
    exp_dir = os.path.join(base_dir, f"gigaevo_experiments_{experiment_id}")
    os.makedirs(exp_dir, exist_ok=True)
    image_path = os.path.join(exp_dir, "visual_results.jpeg")

    if not os.path.exists(image_path):
        if not _service_manager:
            # Fallback: just return empty file response
            return FileResponse(image_path, media_type="image/jpeg")
        vis = VisualizationService(_service_manager.get_storage_service())
        # Compute seconds since start if available
        db = _service_manager.get_db_service()
        exp_model = await db.get_experiment(experiment_id)
        import time as _t

        seconds = 0
        if exp_model and exp_model.started_at:
            seconds = int(max(0, _t.time() - exp_model.started_at.timestamp()))
        # Minimal metrics for initial generation
        await vis.generate_and_store(experiment_id, metrics={"metric": 1.0}, seconds_since_start=seconds)

    return FileResponse(image_path, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@router.get("/results/{experiment_id}/metrics_plot.png")
async def get_metrics_plot_image(experiment_id: str):
    """Serve metrics plot PNG for the experiment, generating/updating with real metrics."""
    # Use system temporary directory instead of /app
    base_dir = tempfile.gettempdir()
    exp_dir = os.path.join(base_dir, f"gigaevo_experiments_{experiment_id}")
    os.makedirs(exp_dir, exist_ok=True)
    image_path = os.path.join(exp_dir, "metrics_plot.png")

    if not _service_manager:
        return FileResponse(image_path, media_type="image/png")

    vis = VisualizationService(_service_manager.get_storage_service())
    db = _service_manager.get_db_service()
    exp_model = await db.get_experiment(experiment_id)
    import time as _t

    seconds = 0
    if exp_model and exp_model.started_at:
        seconds = int(max(0, _t.time() - exp_model.started_at.timestamp()))

    # Get real metrics from experiment model
    metrics = {}
    if exp_model and exp_model.metrics:
        # Convert metrics dict to float values for visualization
        metrics = {k: float(v) for k, v in exp_model.metrics.items() if isinstance(v, (int, float))}

    # If no metrics available, use default
    if not metrics:
        metrics = {"metric": 1.0}

    # Always regenerate to keep plot up-to-date (or check file age for optimization)
    await vis.generate_and_store(experiment_id, metrics=metrics, seconds_since_start=seconds)

    return FileResponse(image_path, media_type="image/png", headers={"Cache-Control": "no-store"})


@router.get("/results/{experiment_id}/best_program.py")
async def get_best_program(experiment_id: str):
    """Serve best program .py from storage; if missing, derive from evolution_report.json and persist."""
    # Use system temporary directory instead of /app
    base_dir = tempfile.gettempdir()
    exp_dir = os.path.join(base_dir, f"gigaevo_experiments_{experiment_id}")
    os.makedirs(exp_dir, exist_ok=True)
    program_path = os.path.join(exp_dir, "best_program.py")

    if not _service_manager:
        # Fallback empty file path
        return FileResponse(program_path, media_type="text/x-python")

    storage = _service_manager.get_storage_service()
    db: DatabaseService = _service_manager.get_db_service()
    s3_program_key = f"experiments_results/{experiment_id}/best_program.py"

    # If local file already exists, serve it; else try to fetch from S3 or derive
    if not os.path.exists(program_path):
        # Try to fetch from storage
        try:
            if await storage.object_exists(s3_program_key):
                data = await storage.download_bytes(s3_program_key)
                if data:
                    with open(program_path, "wb") as f:
                        f.write(data)
            else:
                # Derive from evolution report
                report_key = f"experiments_results/{experiment_id}/evolution_report.json"
                report_bytes = await storage.download_bytes(report_key)
                if report_bytes:
                    payload = json.loads(report_bytes.decode("utf-8"))
                    code = payload.get("best_program")
                    if code:
                        # Persist to local and S3
                        code_bytes = code.encode("utf-8")
                        with open(program_path, "wb") as f:
                            f.write(code_bytes)
                        await storage.upload_bytes(
                            code_bytes,
                            s3_program_key,
                            metadata={"type": "best_program", "experiment_id": experiment_id},
                        )
                        # Update DB best_result
                        try:
                            exp = await db.get_experiment(experiment_id)
                            best_result = (exp.best_result or {}) if exp else {}
                            best_result.update(
                                {
                                    "program_code": code,
                                    "artifacts": {
                                        **(best_result.get("artifacts") or {}),
                                        "best_program": s3_program_key,
                                    },
                                }
                            )
                            await db.update_experiment(experiment_id, best_result=best_result)
                        except Exception:
                            pass
        except Exception:
            # Ignore failures; return empty or partially written file
            pass

    return FileResponse(program_path, media_type="text/x-python", headers={"Cache-Control": "no-store"})


@router.get("/results/{experiment_id}/evolution_report.json")
async def get_evolution_report_json(experiment_id: str):
    """Proxy evolution_report.json from storage to avoid browser CORS and forced download."""
    if not _service_manager:
        return JSONResponse({"error": "service_manager_not_initialized"}, status_code=503)
    storage = _service_manager.get_storage_service()
    try:
        object_name = f"experiments_results/{experiment_id}/evolution_report.json"
        data = await storage.download_bytes(object_name)
        if data is None:
            return JSONResponse({"error": "not_found"}, status_code=404)
        return Response(content=data, media_type="application/json", headers={"Cache-Control": "no-store"})
    except Exception:
        return JSONResponse({"error": "failed_to_fetch_report"}, status_code=500)


@router.get("/results/{experiment_id}/summary")
async def get_evolution_summary(experiment_id: str):
    """Return a robust summary JSON based on evolution_report.json if available.

    Fields: total_iterations, total_programs, total_programs_complete, best_fitness, best_generations
    """
    if not _service_manager:
        return JSONResponse(
            {
                "total_iterations": None,
                "total_programs": None,
                "total_programs_complete": None,
                "best_fitness": None,
                "best_generations": None,
                "token_usage": _empty_token_usage(),
            }
        )
    storage = _service_manager.get_storage_service()
    db: DatabaseService = _service_manager.get_db_service()
    summary = {
        "total_iterations": None,
        "total_programs": None,
        "total_programs_complete": None,
        "best_fitness": None,
        "best_generations": None,
        "token_usage": _empty_token_usage(),
    }
    # Try to fetch and parse report
    try:
        object_name = f"experiments_results/{experiment_id}/evolution_report.json"
        data = await storage.download_bytes(object_name)
        if data:
            import json as _json

            parsed = _json.loads(data.decode("utf-8"))
            # If parsed is a dict, extract fields
            if isinstance(parsed, dict):
                summary["total_iterations"] = parsed.get("total_iterations")
                summary["total_programs"] = parsed.get("total_programs")
                summary["total_programs_complete"] = parsed.get("total_programs_complete")
                bf = parsed.get("best_fitness")
                summary["best_fitness"] = float(bf) if bf is not None else None
                # Map singular to plural per UI contract
                summary["best_generations"] = parsed.get("best_generation")
                summary["token_usage"] = _extract_token_usage(parsed) or _empty_token_usage()
    except Exception:
        pass
    # Fallback: fill best_fitness from DB metrics if still empty
    try:
        if summary["best_fitness"] is None:
            exp = await db.get_experiment(experiment_id)
            if exp and exp.metrics:
                bf = (exp.metrics or {}).get("fitness")
                if bf is not None:
                    summary["best_fitness"] = float(bf)
    except Exception:
        pass
    return JSONResponse(summary, headers={"Cache-Control": "no-store"})


@router.get("/results/{experiment_id}/best_chain_config.json")
async def get_best_chain_config(experiment_id: str):
    """Return the best (evolved) chain configuration JSON for chain experiments."""
    if not _service_manager:
        return JSONResponse({"error": "service_manager_not_initialized"}, status_code=503)
    storage = _service_manager.get_storage_service()
    try:
        object_name = f"experiments_results/{experiment_id}/best_chain_config.json"
        data = await storage.download_bytes(object_name)
        if data is None:
            # Fallback: evolution_report.json may embed best_chain_config or only best_program
            report_key = f"experiments_results/{experiment_id}/evolution_report.json"
            report_bytes = await storage.download_bytes(report_key)
            if report_bytes:
                payload = json.loads(report_bytes.decode("utf-8"))
                best_chain = payload.get("best_chain_config")
                if isinstance(best_chain, dict) and best_chain.get("steps"):
                    return JSONResponse(best_chain, headers={"Cache-Control": "no-store"})
                best_program = payload.get("best_program")
                if isinstance(best_program, str):
                    extracted = _extract_chain_config_from_program_code(best_program)
                    if extracted:
                        return JSONResponse(extracted, headers={"Cache-Control": "no-store"})
            return JSONResponse({"error": "not_found"}, status_code=404, headers={"Cache-Control": "no-store"})
        return Response(content=data, media_type="application/json", headers={"Cache-Control": "no-store"})
    except Exception:
        return JSONResponse({"error": "failed_to_fetch_chain_config"}, status_code=500)


@router.get("/results/{experiment_id}/initial_chain_config.json")
async def get_initial_chain_config(experiment_id: str):
    """Return the initial (pre-evolution) chain configuration JSON for chain experiments."""
    if not _service_manager:
        return JSONResponse({"error": "service_manager_not_initialized"}, status_code=503)
    storage = _service_manager.get_storage_service()
    try:
        # Try results folder first
        object_name = f"experiments_results/{experiment_id}/initial_chain_config.json"
        data = await storage.download_bytes(object_name)
        if data is None:
            # Fallback: try to get from experiment files (base_chain_config.json)
            db: DatabaseService = _service_manager.get_db_service()
            exp_model = await db.get_experiment(experiment_id)
            if exp_model:
                base_prefix = (exp_model.config or {}).get("experiment_files_path") or ""
                if base_prefix:
                    initial_src_key = base_prefix.rstrip("/") + "/base_chain_config.json"
                    data = await storage.download_bytes(initial_src_key)
            # Also check evolution_report.json
            if data is None:
                report_key = f"experiments_results/{experiment_id}/evolution_report.json"
                report_bytes = await storage.download_bytes(report_key)
                if report_bytes:
                    payload = json.loads(report_bytes.decode("utf-8"))
                    initial_chain = payload.get("initial_chain_config")
                    if initial_chain:
                        return JSONResponse(initial_chain, headers={"Cache-Control": "no-store"})
        if data is None:
            return JSONResponse({"error": "not_found"}, status_code=404, headers={"Cache-Control": "no-store"})
        return Response(content=data, media_type="application/json", headers={"Cache-Control": "no-store"})
    except Exception:
        return JSONResponse({"error": "failed_to_fetch_initial_config"}, status_code=500)


@router.get("/results/{experiment_id}/download.zip")
async def download_results_archive(experiment_id: str):
    """Return the prebuilt ZIP with best_program.py and validate.py (updated by background job every ~10s)."""
    if not _service_manager:
        return JSONResponse({"error": "service_manager_not_initialized"}, status_code=503)

    storage = _service_manager.get_storage_service()

    try:
        # Stream prebuilt archive from storage
        results_zip_key = f"experiments_results/{experiment_id}/results.zip"
        data = await storage.download_bytes(results_zip_key)
        if not data:
            return JSONResponse({"error": "archive_not_ready"}, status_code=404, headers={"Cache-Control": "no-store"})
        return Response(
            content=data,
            media_type="application/zip",
            headers={"Content-Disposition": 'attachment; filename="results.zip"', "Cache-Control": "no-store"},
        )
    except Exception:
        return JSONResponse({"error": "failed_to_prepare_archive"}, status_code=500)


@router.get("/results/{experiment_id}/feedback")
async def get_chain_feedback(
    experiment_id: str,
    iteration: int | None = None,
    feedback_template: str | None = None,
):
    """Get chain feedback for an experiment.

    Args:
        experiment_id: The experiment ID
        iteration: Optional iteration number (if None, returns the latest feedback)
        feedback_template: Optional requested template variant (informational)

    Returns:
        JSON with feedback content and metadata
    """
    if not _service_manager:
        return JSONResponse({"error": "service_manager_not_initialized"}, status_code=503)

    storage = _service_manager.get_storage_service()

    async def _try_fetch_json_meta(iter_num: int) -> dict:
        """Try to fetch chain_feedback.json for additional metadata."""
        try:
            json_key = f"experiments_results/{experiment_id}/iteration_{iter_num}/chain_feedback.json"
            jdata = await storage.download_bytes(json_key)
            if jdata:
                import json as _json

                return _json.loads(jdata.decode("utf-8"))
        except Exception:
            pass
        return {}

    def _response(
        iter_num: int,
        feedback_text: str,
        meta: dict,
        available: List[int],
    ) -> JSONResponse:
        return JSONResponse(
            {
                "experiment_id": experiment_id,
                "iteration": iter_num,
                "feedback": feedback_text,
                "feedback_length": len(feedback_text),
                "has_feedback": len(feedback_text.strip()) > 0,
                "feedback_template": meta.get("feedback_template", feedback_template or "detailed"),
                "results_count": meta.get("results_count"),
                "target_column": meta.get("target_column"),
                "available_iterations": available,
            }
        )

    try:
        available_list, it_to_key = await _discover_chain_feedback_keys(storage, experiment_id)

        if iteration is None:
            if not available_list:
                return JSONResponse(
                    {
                        "experiment_id": experiment_id,
                        "iteration": None,
                        "feedback": "",
                        "feedback_length": 0,
                        "has_feedback": False,
                        "message": "No feedback found for this experiment",
                        "available_iterations": [],
                    }
                )
            latest_i = max(available_list)
            key = it_to_key[latest_i]
            data = await storage.download_bytes(key)
            if data:
                feedback_text = data.decode("utf-8")
                meta = await _try_fetch_json_meta(latest_i)
                return _response(latest_i, feedback_text, meta, available_list)
        else:
            if iteration in it_to_key:
                key = it_to_key[iteration]
                data = await storage.download_bytes(key)
                if data:
                    feedback_text = data.decode("utf-8")
                    meta = await _try_fetch_json_meta(iteration)
                    return _response(iteration, feedback_text, meta, available_list)
            # Legacy direct path (in case listing missed a key)
            feedback_key = f"experiments_results/{experiment_id}/iteration_{iteration}/chain_feedback.md"
            data = await storage.download_bytes(feedback_key)
            if data:
                feedback_text = data.decode("utf-8")
                meta = await _try_fetch_json_meta(iteration)
                if iteration not in available_list:
                    available_list = sorted(set(available_list + [iteration]))
                return _response(iteration, feedback_text, meta, available_list)

        return JSONResponse(
            {
                "experiment_id": experiment_id,
                "iteration": iteration,
                "feedback": "",
                "feedback_length": 0,
                "has_feedback": False,
                "message": "No feedback found for this experiment",
                "available_iterations": available_list,
            }
        )

    except Exception as e:
        return JSONResponse(
            {
                "error": f"failed_to_fetch_feedback: {str(e)}",
                "experiment_id": experiment_id,
                "iteration": iteration,
            },
            status_code=500,
        )


@router.get("/results/{experiment_id}/validation_debug")
async def get_validation_debug(experiment_id: str, iteration: int | None = None):
    """Get validation debug log for an experiment.

    Args:
        experiment_id: The experiment ID
        iteration: Optional iteration number (if None, returns the latest debug log)

    Returns:
        JSON with debug log content and metadata
    """
    if not _service_manager:
        return JSONResponse({"error": "service_manager_not_initialized"}, status_code=503)

    storage = _service_manager.get_storage_service()

    try:
        available_list, it_to_key = await _discover_validation_debug_keys(storage, experiment_id)

        if iteration is None:
            if not available_list:
                return JSONResponse(
                    {
                        "experiment_id": experiment_id,
                        "iteration": None,
                        "debug_log": "",
                        "debug_log_length": 0,
                        "message": "No debug log found for this experiment",
                        "available_iterations": [],
                    }
                )
            latest_i = max(available_list)
            data = await storage.download_bytes(it_to_key[latest_i])
            if data:
                debug_text = data.decode("utf-8")
                return JSONResponse(
                    {
                        "experiment_id": experiment_id,
                        "iteration": latest_i,
                        "debug_log": debug_text,
                        "debug_log_length": len(debug_text),
                        "available_iterations": available_list,
                    }
                )
        else:
            if iteration in it_to_key:
                data = await storage.download_bytes(it_to_key[iteration])
            else:
                debug_key = f"experiments_results/{experiment_id}/iteration_{iteration}/validation_debug.log"
                data = await storage.download_bytes(debug_key)
            if data:
                debug_text = data.decode("utf-8")
                avail = available_list if iteration in it_to_key else sorted(set(available_list + [iteration]))
                return JSONResponse(
                    {
                        "experiment_id": experiment_id,
                        "iteration": iteration,
                        "debug_log": debug_text,
                        "debug_log_length": len(debug_text),
                        "available_iterations": avail,
                    }
                )

        return JSONResponse(
            {
                "experiment_id": experiment_id,
                "iteration": iteration,
                "debug_log": "",
                "debug_log_length": 0,
                "message": "No debug log found for this experiment",
                "available_iterations": available_list,
            }
        )

    except Exception as e:
        return JSONResponse(
            {"error": f"failed_to_fetch_debug_log: {str(e)}", "experiment_id": experiment_id, "iteration": iteration},
            status_code=500,
        )


@router.post("/results/{experiment_id}/upload_to_memory")
async def upload_to_memory(experiment_id: str):
    """Trigger Ideas Tracker to analyze experiment results and write
    MemoryCards to the memory platform.
    """
    instance_service = None
    assigned_runner_id = ""
    allocated_runner = False
    try:
        if not _service_manager:
            return JSONResponse({"error": "service_manager_not_initialized"}, status_code=503)

        db: DatabaseService = _service_manager.get_db_service()
        exp = await db.get_experiment(experiment_id)
        if not exp:
            return JSONResponse({"error": "experiment_not_found"}, status_code=404)

        status = (exp.status or "").lower()
        if status not in ("completed", "terminated", "cancelled"):
            return JSONResponse(
                {"error": f"Experiment must be completed, terminated, or cancelled (current: {status})"},
                status_code=400,
            )

        instance_service = _service_manager.instance_service
        if not instance_service:
            return JSONResponse({"error": "runner_service_not_available"}, status_code=503)

        exp_config = dict(exp.config or {})
        parameters = dict(exp_config.get("parameters") or {})
        namespace = str(parameters.get("memory_namespace") or "").strip() or experiment_id
        assigned_runner_id = str(exp_config.get("assigned_runner_id") or "").strip()
        if not assigned_runner_id:
            return JSONResponse(
                {"error": "Experiment has no assigned runner; upload_to_memory requires the original runner."},
                status_code=409,
            )

        instance = await instance_service.allocate_specific_runner(assigned_runner_id, experiment_id)
        if not instance:
            runner = await instance_service.get_instance(assigned_runner_id)
            runner_status = getattr(runner, "status", None)
            detail = (
                f"Original runner '{assigned_runner_id}' is not available for upload_to_memory"
                f"{f' (status={runner_status})' if runner_status else ''}. Try again later."
            )
            return JSONResponse({"error": detail}, status_code=503)
        allocated_runner = True

        runner_url = instance.endpoint_url.rstrip("/")
        upload_url = f"{runner_url}/api/v1/experiments/{experiment_id}/upload-to-memory"
        config_payload = {"memory_namespace": namespace}

        async with httpx.AsyncClient(timeout=660.0) as client:
            resp = await client.post(upload_url, json=config_payload)

        if resp.status_code == 200:
            payload = resp.json()
            if isinstance(payload, dict) and "namespace" not in payload:
                payload["namespace"] = namespace
            return JSONResponse(payload)

        try:
            error_payload = resp.json()
        except Exception:
            error_payload = {"error": resp.text[:500] if resp.text else "Runner returned error"}
        detail = error_payload.get("detail") or error_payload.get("error") or f"HTTP {resp.status_code}"
        return JSONResponse({"error": detail}, status_code=resp.status_code)

    except Exception as e:
        logger.exception(f"Memory upload error for {experiment_id}")
        return JSONResponse({"error": f"Upload failed: {str(e)}"}, status_code=500)
    finally:
        if allocated_runner and instance_service and assigned_runner_id:
            try:
                await instance_service.release_runner_by_id_if_experiment(assigned_runner_id, experiment_id)
            except Exception:
                logger.warning(
                    "Failed to release runner %s after upload_to_memory for %s",
                    assigned_runner_id,
                    experiment_id,
                )
