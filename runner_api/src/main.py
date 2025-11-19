#!/usr/bin/env python3

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.api.routes import experiments, tasks, workers
from src.config import load_config
from src.services.experiment_service import ExperimentService
from src.services.gigavolve_service import GigaEvolveService
from src.workers.task_worker import TaskWorker

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Global services
gigavolve_service = None
_background_worker_task = None
_background_worker: TaskWorker | None = None
_results_collection_task = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifecycle"""
    global gigavolve_service, _background_worker_task, _background_worker, _results_collection_task
    # Startup
    logger.info("Starting GigaEvo Platform Runner API...")

    # Initialize GigaEvolve service
    gigavolve_service = GigaEvolveService()

    cfg = load_config()

    # Clone the repository on startup
    logger.info("Cloning GigaEvolve repository...")
    clone_success = await gigavolve_service.clone_repository(force_refresh=cfg.gigavolve.repo_force_refresh)

    if clone_success:
        repo_info = await gigavolve_service.get_repository_info()
        logger.info(f"GigaEvolve repository ready: {repo_info}")
    else:
        logger.error("Failed to clone GigaEvolve repository")

    # Initialize ExperimentService (singleton) and register in routes
    logger.info("Initializing ExperimentService (singleton)")
    experiment_service = ExperimentService()
    try:
        # Register the singleton for dependency in experiment routes
        experiments.set_experiment_service(experiment_service)
        logger.info("ExperimentService registered")
    except Exception as e:
        logger.error(f"Failed to register ExperimentService: {e}")

    logger.info("GigaEvo Platform Runner API startup complete")

    # Optionally start a default background worker
    try:
        if cfg.worker.autostart:
            _background_worker = TaskWorker(
                worker_id=cfg.worker.autostart_worker_id,
                name=cfg.worker.autostart_worker_name,
            )
            _background_worker_task = asyncio.create_task(_background_worker.start())
            logger.info(
                f"Started background TaskWorker id={cfg.worker.autostart_worker_id} name={cfg.worker.autostart_worker_name}"
            )
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
                            continue

                        png_path_str = result.get("output_png_file")
                        png_path = Path(png_path_str) if png_path_str else None

                        # Save evolution report
                        result = await gigavolve_service.generate_evolution_report(exp_id, out_subfolder)
                        if not result.get("success"):
                            logger.debug(
                                f"Report generation failed for {exp_id}: {result.get('stderr') or result.get('error')}"
                            )
                            continue

                        report_path_str = result.get("output_json_file")
                        report_path = Path(report_path_str) if report_path_str else None

                        # Handle artifacts
                        artifacts = []
                        if png_path and png_path.exists():
                            artifacts.append({"src": png_path, "dst": "metrics_plot.png"})
                        if report_path and report_path.exists():
                            artifacts.append({"src": report_path, "dst": "evolution_report.json"})
                        if not artifacts:
                            continue
                        await experiment_service.save_artifacts_to_workspace(exp_id, artifacts)
                        await experiment_service.upload_artifacts_to_storage(exp_id, artifacts)
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
        await experiment_service.close()
        experiments.set_experiment_service(None)
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


app = FastAPI(
    title="GigaEvo Platform Runner API",
    description="Runner API for executing experiments and managing workers",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(experiments.router, prefix="/api/v1/experiments", tags=["experiments"])
app.include_router(workers.router, prefix="/api/v1/workers", tags=["workers"])
app.include_router(tasks.router, prefix="/api/v1/tasks", tags=["tasks"])


@app.get("/health")
async def health_check():
    """Health check endpoint"""
    global gigavolve_service

    health_status = {"status": "healthy", "service": "GigaEvo Platform Runner API", "version": "0.1.0"}

    # Check GigaEvolve repository status
    if gigavolve_service:
        repo_ready = gigavolve_service.is_repository_ready()
        health_status["gigavolve_repository"] = {"ready": repo_ready, "path": gigavolve_service.config.clone_path}

        if repo_ready:
            repo_info = await gigavolve_service.get_repository_info()
            health_status["gigavolve_repository"].update(repo_info)
    else:
        health_status["gigavolve_repository"] = {"ready": False, "error": "Service not initialized"}

    return health_status


@app.get("/api/v1/repository/status")
async def get_repository_status():
    """Get detailed GigaEvolve repository status"""
    global gigavolve_service

    if not gigavolve_service:
        return {"error": "GigaEvolve service not initialized"}

    repo_info = await gigavolve_service.get_repository_info()
    return repo_info


@app.post("/api/v1/repository/refresh")
async def refresh_repository():
    """Force refresh the GigaEvolve repository"""
    global gigavolve_service

    if not gigavolve_service:
        return {"error": "GigaEvolve service not initialized"}

    logger.info("Force refreshing GigaEvolve repository...")
    clone_success = await gigavolve_service.clone_repository(force_refresh=True)

    if clone_success:
        repo_info = await gigavolve_service.get_repository_info()
        logger.info(f"Repository refreshed successfully: {repo_info}")
        return {"success": True, "repository": repo_info}
    else:
        logger.error("Failed to refresh repository")
        return {"success": False, "error": "Failed to clone repository"}


if __name__ == "__main__":
    config = load_config()
    uvicorn.run(app, host="0.0.0.0", port=8001)
