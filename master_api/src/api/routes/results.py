#!/usr/bin/env python3

import io
import json
import os
import tempfile
import zipfile

from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse, Response

from ...services.database_service import DatabaseService
from ...services.service_manager import ServiceManager
from ...services.visualization_service import VisualizationService

router = APIRouter()

_service_manager: ServiceManager | None = None


def set_service_manager(service_manager: ServiceManager):
    global _service_manager
    _service_manager = service_manager


@router.get("/results/{experiment_id}/visual_results.jpeg")
async def get_visual_result_image(experiment_id: str):
    """Serve visualization image for the experiment, generating if missing.

    Saves locally and uploads to S3 via StorageService.
    """
    # Use system temporary directory instead of /app
    base_dir = tempfile.gettempdir()
    exp_dir = os.path.join(base_dir, f"geml_experiments_{experiment_id}")
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
    """Serve metrics plot PNG for the experiment, generating if missing."""
    # Use system temporary directory instead of /app
    base_dir = tempfile.gettempdir()
    exp_dir = os.path.join(base_dir, f"geml_experiments_{experiment_id}")
    os.makedirs(exp_dir, exist_ok=True)
    image_path = os.path.join(exp_dir, "metrics_plot.png")

    if not os.path.exists(image_path):
        if not _service_manager:
            return FileResponse(image_path, media_type="image/png")
        vis = VisualizationService(_service_manager.get_storage_service())
        db = _service_manager.get_db_service()
        exp_model = await db.get_experiment(experiment_id)
        import time as _t

        seconds = 0
        if exp_model and exp_model.started_at:
            seconds = int(max(0, _t.time() - exp_model.started_at.timestamp()))
        await vis.generate_and_store(experiment_id, metrics={"metric": 1.0}, seconds_since_start=seconds)

    return FileResponse(image_path, media_type="image/png", headers={"Cache-Control": "no-store"})


@router.get("/results/{experiment_id}/best_program.py")
async def get_best_program(experiment_id: str):
    """Serve best program .py from storage; if missing, derive from evolution_report.json and persist."""
    # Use system temporary directory instead of /app
    base_dir = tempfile.gettempdir()
    exp_dir = os.path.join(base_dir, f"geml_experiments_{experiment_id}")
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


@router.get("/results/{experiment_id}/download.zip")
async def download_results_archive(experiment_id: str):
    """Return the prebuilt ZIP with best_program.py and validate.py (updated by background job every ~10s)."""
    if not _service_manager:
        return JSONResponse({"error": "service_manager_not_initialized"}, status_code=503)

    storage = _service_manager.get_storage_service()
    db: DatabaseService = _service_manager.get_db_service()

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
