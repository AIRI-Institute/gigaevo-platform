#!/usr/bin/env python3

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from common.version import __version__
from src.api.routes import experiments, tasks, workers
from src.config import load_config
from src.security import get_cors_allowed_origins, require_api_key
from src.services.experiment_service import ExperimentService
from src.services.gigavolve_service import GigaEvolveService
from src.services.redis_client import close_redis
from src.services.task_repository import TaskRepository
from src.services.task_service import TaskService
from src.services.worker_service import WorkerService
from src.workers.task_worker import TaskWorker

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


# Global services
gigavolve_service = None
_background_worker_task = None
_background_worker: TaskWorker | None = None
_results_collection_task = None
_repo_clone_task = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifecycle"""
    global gigavolve_service, task_repository, task_service, worker_service
    global _background_worker_task, _background_worker, _results_collection_task
    # Startup
    logger.info("Starting GigaEvo Platform Runner API...")

    # Initialize GigaEvolve service
    gigavolve_service = GigaEvolveService()

    cfg = load_config()

    # Initialize ExperimentService (singleton) and register in routes
    logger.info("Initializing ExperimentService (singleton)")
    task_repository = TaskRepository()
    task_service = TaskService(task_repository)
    worker_service = WorkerService()
    experiment_service = ExperimentService(task_repository)
    try:
        # Register the singleton for dependency in experiment routes
        experiments.set_experiment_service(experiment_service)
        tasks.set_task_service(task_service)
        workers.set_worker_service(worker_service)
        logger.info("ExperimentService registered")
    except Exception as e:
        logger.error(f"Failed to register ExperimentService: {e}")

    logger.info("GigaEvo Platform Runner API startup complete")

    # Start background worker
    try:
        _background_worker = TaskWorker(
            worker_id=cfg.worker.worker_id,
            name="background-worker",
            gigavolve_service=gigavolve_service,
            task_repository=task_repository,
            experiment_service=experiment_service,
        )
        _background_worker_task = asyncio.create_task(_background_worker.start())
        logger.info(f"Started background TaskWorker id={cfg.worker.worker_id}")
    except Exception as e:
        logger.warning(f"Failed to start background worker: {e}")

    # Start results collection loop for running experiments
    async def _results_collection_loop():
        nonlocal experiment_service
        global gigavolve_service
        while True:
            try:
                running_ids = await experiment_service.list_running_experiment_ids()
                if running_ids:
                    logger.debug(f"Results collection loop: running experiments: {running_ids}")
                for exp_id in running_ids:
                    try:
                        # Save evolution plot
                        out_subfolder = f"outputs/{exp_id}"
                        result = await gigavolve_service.generate_evolution_plot(
                            exp_id, out_subfolder, iteration_col="generation"
                        )
                        if not result.get("success"):
                            logger.debug(
                                f"Plot generation failed for {exp_id}: {result.get('stderr') or result.get('error')}"
                            )
                        else:
                            png_path_str = result.get("output_png_file")
                            png_path = Path(png_path_str) if png_path_str else None

                            result = await gigavolve_service.generate_evolution_report(exp_id, out_subfolder)
                            if not result.get("success"):
                                logger.debug(
                                    f"Report generation failed for {exp_id}: {result.get('stderr') or result.get('error')}"
                                )
                            else:
                                report_path_str = result.get("output_json_file")
                                report_path = Path(report_path_str) if report_path_str else None

                                artifacts = []
                                if png_path and png_path.exists():
                                    artifacts.append({"src": png_path, "dst": "metrics_plot.png"})
                                if report_path and report_path.exists():
                                    artifacts.append({"src": report_path, "dst": "evolution_report.json"})
                                if artifacts:
                                    await experiment_service.save_artifacts_to_workspace(exp_id, artifacts)
                                    await experiment_service.upload_artifacts_to_storage(exp_id, artifacts)

                        # Always try chain feedback upload (does not depend on plot/report success)
                        try:
                            uploaded = await experiment_service.collect_and_upload_chain_feedback(exp_id)
                            if uploaded:
                                logger.debug(f"Uploaded {uploaded} chain feedback file(s) for {exp_id}")
                        except Exception as feedback_e:
                            logger.warning(f"Failed to upload chain feedback for {exp_id}: {feedback_e}")

                    except Exception as inner_e:
                        logger.warning(f"Results collection loop error for {exp_id}: {inner_e}")
            except Exception as loop_e:
                logger.warning(f"Results collection loop tick error: {loop_e}")
            await asyncio.sleep(cfg.gigavolve.results_collection_interval)

    try:
        _results_collection_task = asyncio.create_task(_results_collection_loop())
        logger.info("Started results collection loop (10s)")
    except Exception as e:
        logger.warning(f"Failed to start results collection loop: {e}")

    yield

    # Shutdown
    logger.info("Shutting down GigaEvo Platform Runner API...")

    # Close Experiment service
    try:
        experiments.set_experiment_service(None)
        tasks.set_task_service(None)
        workers.set_worker_service(None)
        logger.info("ExperimentService closed")
    except Exception as e:
        logger.warning(f"Failed to close ExperimentService: {e}")

    # Stop background worker if running
    try:
        if _background_worker:
            await _background_worker.stop()
        if _background_worker_task:
            try:
                # Give the worker task a timeout to finish gracefully
                await asyncio.wait_for(_background_worker_task, timeout=2)
            except asyncio.TimeoutError:
                _background_worker_task.cancel()
                try:
                    await _background_worker_task
                except asyncio.CancelledError:
                    pass
        if _results_collection_task:
            _results_collection_task.cancel()
            try:
                await _results_collection_task
            except asyncio.CancelledError:
                pass
    except Exception as e:
        logger.warning(f"Failed to stop background worker: {e}")
    await close_redis()


app = FastAPI(
    title="GigaEvo Platform Runner API",
    description="Runner API for executing experiments and managing workers",
    version=__version__,
    lifespan=lifespan,
)

_cors_allowed = get_cors_allowed_origins()
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_allowed,
    allow_credentials="*" not in _cors_allowed,
    allow_methods=["*"],
    allow_headers=["*"],
)

_auth = [Depends(require_api_key)]
app.include_router(experiments.router, prefix="/api/v1/experiments", tags=["experiments"], dependencies=_auth)
app.include_router(workers.router, prefix="/api/v1/workers", tags=["workers"], dependencies=_auth)
app.include_router(tasks.router, prefix="/api/v1/tasks", tags=["tasks"], dependencies=_auth)


@app.get("/health")
async def health_check():
    """Health check endpoint"""
    global gigavolve_service

    health_status = {"status": "healthy", "service": "GigaEvo Platform Runner API", "version": "unknown"}

    # Check GigaEvolve repository status
    if gigavolve_service:
        repo_ready = gigavolve_service.is_repository_ready()
        health_status["dependencies"] = {"installed": repo_ready}

        if repo_ready:
            repo_info = await gigavolve_service.get_repository_info()
            health_status["version"] = repo_info.get("tag") or repo_info.get("commit_hash", "unknown")
    else:
        health_status["dependencies"] = {"installed": False, "error": "Service not initialized"}

    return health_status


@app.get("/api/v1/repository/status")
async def get_repository_status():
    """Get detailed GigaEvolve repository status"""
    global gigavolve_service

    if not gigavolve_service:
        return {"error": "GigaEvolve service not initialized"}

    repo_info = await gigavolve_service.get_repository_info()
    return repo_info


if __name__ == "__main__":
    config = load_config()
    uvicorn.run(app, host="0.0.0.0", port=8001)
