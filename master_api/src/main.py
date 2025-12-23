#!/usr/bin/env python3


import asyncio
import json
import os

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

from src.api.routes import examples, experiments, instances, results, status
from src.config import load_config
from src.services.service_manager import ServiceManager

# Initialize global service manager
service_manager: ServiceManager = None
_results_ingest_task = None

app = FastAPI(
    title="GigaEvo Platform Master API",
    description="Master API for managing experiments and coordinating runners",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(experiments.router, prefix="/api/v1/experiments", tags=["experiments"])
app.include_router(instances.router, prefix="/api/v1/instances", tags=["instances"])
app.include_router(status.router, prefix="/api/v1/status", tags=["status"])
app.include_router(results.router)
app.include_router(examples.router, tags=["examples"])


@app.get("/debug/consumer")
async def debug_consumer():
    """Debug endpoint to check consumer status"""
    global service_manager
    debug_info = {
        "service_manager_initialized": service_manager is not None,
        "kafka_enabled": False,
        "kafka_service_available": False,
        "workflow_consumer_available": False,
        "deployment_consumer_available": False,
        "consumers_running": [],
    }

    if service_manager:
        config = service_manager.config
        debug_info["kafka_enabled"] = config.kafka.enabled
        debug_info["kafka_service_available"] = service_manager.kafka_service is not None
        debug_info["workflow_consumer_available"] = service_manager.workflow_consumer is not None
        debug_info["deployment_consumer_available"] = service_manager.deployment_consumer is not None

        if service_manager.kafka_service:
            debug_info["consumers_running"] = list(service_manager.kafka_service.consumer_tasks.keys())

    return debug_info


@app.post("/debug/test-consumer")
async def test_consumer():
    """Test consumer by sending a test message"""
    global service_manager
    if not service_manager or not service_manager.kafka_service:
        return {"error": "Kafka service not available"}

    try:
        # Send test message to experiment-config topic
        test_message = {
            "experiment_id": "test-exp-001",
            "config": {"task_type": "classification", "target_field": "target", "test": True},
        }

        await service_manager.kafka_service.send_message(
            service_manager.config.kafka.topics["experiment_config"], test_message, key="test-exp-001"
        )

        return {"success": True, "message": "Test message sent"}
    except Exception as e:
        return {"error": str(e)}


@app.get("/health")
async def health_check():
    """Health check endpoint with detailed status"""
    global service_manager

    health_status = {
        "status": "healthy",
        "service": "master-api",
        "timestamp": "2024-01-01T00:00:00Z",
        "components": {},
    }

    if service_manager and service_manager.is_healthy():
        service_health = service_manager.health_check()
        health_status["components"] = service_health["services"]
        health_status["service_manager"] = service_health["service_manager"]
    else:
        health_status["status"] = "unhealthy"
        health_status["service_manager"] = "not_initialized"
        health_status["components"]["database"] = "not_initialized"
        health_status["components"]["storage"] = "not_initialized"

    # Check Kafka configuration
    config = load_config()
    if config.kafka.enabled:
        health_status["components"]["kafka"] = "enabled"
    else:
        health_status["components"]["kafka"] = "disabled"

    return health_status


@app.on_event("startup")
async def startup_event():
    global service_manager, _results_ingest_task

    logger.info("Starting GigaEvo Platform Master API...")

    # Wait a moment for services to be ready, especially in development
    await asyncio.sleep(2)

    try:
        # Initialize service manager
        service_manager = ServiceManager()
        await service_manager.initialize()
        logger.info("Service Manager initialized successfully")

        # Make services available to routes
        experiments.set_service_manager(service_manager)
        instances.set_service_manager(service_manager)
        status.set_service_manager(service_manager)
        results.set_service_manager(service_manager)
        examples.set_service_manager(service_manager)

        # Placeholder: real-time visuals are now produced by runner-api.
        # Master will no longer generate placeholder images.

        # Preload example datasets to storage so Web UI presets work out of the box
        try:
            storage = service_manager.get_storage_service()
            # Resolve examples directory: repo_root/master_api/data_examples
            here = os.path.dirname(__file__)
            examples_dir = os.path.normpath(os.path.join(here, "..", "data_examples"))
            if os.path.isdir(examples_dir):
                # Build set of existing objects under data/
                try:
                    existing = set(await storage.list_objects(prefix="data/"))
                except Exception:
                    existing = set()
                num_uploaded = 0
                for fname in os.listdir(examples_dir):
                    if not fname.endswith("_spec.json"):
                        continue
                    spec_path = os.path.join(examples_dir, fname)
                    try:
                        with open(spec_path, "r", encoding="utf-8") as f:
                            spec = json.load(f)
                    except Exception:
                        continue
                    dataset_rel = spec.get("dataset_path", "")
                    dataset_basename = os.path.basename(dataset_rel) if dataset_rel else ""
                    if not dataset_basename:
                        continue
                    local_csv = os.path.join(examples_dir, dataset_basename)
                    if not os.path.exists(local_csv):
                        logger.warning(f"Example dataset missing: {local_csv}")
                        continue
                    object_name = f"data/{dataset_basename}"
                    if object_name in existing:
                        continue
                    ok = await storage.upload_file(
                        local_csv, object_name, metadata={"source": "example", "example_name": fname[:-10]}
                    )
                    if ok:
                        num_uploaded += 1
                if num_uploaded:
                    logger.info(f"Preloaded {num_uploaded} example dataset(s) into storage")
        except Exception as e:
            logger.warning(f"Failed to preload example datasets: {e}")

        # Preload local prompt preset datasets to storage under prompt_data/
        try:
            storage = service_manager.get_storage_service()
            here = os.path.dirname(__file__)
            presets_dir = os.path.normpath(os.path.join(here, "..", "prompt_examples"))
            if os.path.isdir(presets_dir):
                uploaded = 0
                for name in sorted(os.listdir(presets_dir)):
                    pdir = os.path.join(presets_dir, name)
                    if not os.path.isdir(pdir):
                        continue
                    train_csv = os.path.join(pdir, "dataset", "train.csv")
                    if not os.path.exists(train_csv):
                        continue
                    object_name = f"prompt_data/{name}/train.csv"
                    try:
                        exists = await storage.object_exists(object_name)
                    except Exception:
                        exists = False
                    if exists:
                        continue
                    ok = await storage.upload_file(
                        train_csv, object_name, metadata={"source": "local_prompt_preset", "example_name": name}
                    )
                    if ok:
                        uploaded += 1
                if uploaded:
                    logger.info(f"Preloaded {uploaded} local prompt preset dataset(s) into storage")
        except Exception as e:
            logger.warning(f"Failed to preload local prompt preset datasets: {e}")

        logger.info("GigaEvo Platform Master API startup completed")

        # Start background results ingestion loop: pulls reports from MinIO and updates DB every 10s
        async def _results_ingest_loop():
            global service_manager
            while True:
                try:
                    if service_manager and service_manager.is_healthy():
                        db = service_manager.get_db_service()
                        storage = service_manager.get_storage_service()
                        # Fetch a batch of experiments
                        experiments_list = await db.list_experiments(limit=1000, offset=0)
                        for exp in experiments_list:
                            exp_id = exp.id if hasattr(exp, "id") else exp.get("id")
                            if not exp_id:
                                continue
                            # Download evolution_report.json from storage
                            object_name = f"experiments_results/{exp_id}/evolution_report.json"
                            data = await storage.download_bytes(object_name)
                            if not data:
                                continue
                            try:
                                payload = json.loads(data.decode("utf-8"))
                            except Exception:
                                continue

                            best_fitness = payload.get("best_fitness")
                            best_program_code = payload.get("best_program")

                            # Derive progress percentage from reported iterations vs configured max_iterations
                            total_iterations = payload.get("total_iterations")
                            exp_config = getattr(exp, "config", {}) or {}
                            max_iterations = exp_config.get("max_iterations") if isinstance(exp_config, dict) else None

                            progress_pct = None
                            if (
                                isinstance(total_iterations, (int, float))
                                and isinstance(max_iterations, (int, float))
                                and max_iterations > 0
                            ):
                                # Clamp to [0, 100]
                                progress_pct = max(
                                    0.0, min(100.0, float(total_iterations) / float(max_iterations) * 100.0)
                                )

                            # Merge metrics instead of overwriting so we can keep multiple values
                            existing_metrics = getattr(exp, "metrics", {}) or {}
                            current_metrics = dict(existing_metrics) if isinstance(existing_metrics, dict) else {}

                            if best_fitness is not None:
                                current_metrics["fitness"] = float(best_fitness)
                            if progress_pct is not None:
                                current_metrics["progress"] = float(progress_pct)

                            update_kwargs = {}
                            if current_metrics:
                                update_kwargs["metrics"] = current_metrics
                            # Persist best program and validation artifacts into S3 and DB for quick download
                            try:
                                # Keys
                                base_results_prefix = f"experiments_results/{exp_id}/"
                                best_program_key = f"{base_results_prefix}best_program.py"
                                validation_dst_key = f"{base_results_prefix}validate.py"
                                results_zip_key = f"{base_results_prefix}results.zip"

                                # Upload latest best_program.py every tick (overwrite allowed)
                                if best_program_code:
                                    await storage.upload_bytes(
                                        best_program_code.encode("utf-8"),
                                        best_program_key,
                                        metadata={"type": "best_program", "experiment_id": exp_id},
                                    )

                                # Upload/copy latest validation under results (overwrite allowed)
                                exp_model = await db.get_experiment(exp_id)
                                if exp_model:
                                    base_prefix = (exp_model.config or {}).get("experiment_files_path") or (
                                        exp_model.data_path or ""
                                    )
                                    if base_prefix:
                                        validation_src_key = base_prefix.rstrip("/") + "/validate.py"
                                        if await storage.object_exists(validation_src_key):
                                            # Prefer server-side copy for efficiency, but fall back to re-upload
                                            copied = await storage.copy_object(
                                                validation_src_key, validation_dst_key, metadata={"type": "validation"}
                                            )
                                            if not copied:
                                                val_bytes = await storage.download_bytes(validation_src_key)
                                                if val_bytes:
                                                    await storage.upload_bytes(
                                                        val_bytes, validation_dst_key, metadata={"type": "validation"}
                                                    )

                                # Rebuild results.zip every tick if best_program available
                                if best_program_code:
                                    import io as _io
                                    import zipfile as _zipfile

                                    mem = _io.BytesIO()
                                    with _zipfile.ZipFile(mem, "w", _zipfile.ZIP_DEFLATED) as zf:
                                        zf.writestr("best_program.py", best_program_code.encode("utf-8"))
                                        # Use current validation from results if any
                                        val_b = await storage.download_bytes(validation_dst_key)
                                        if val_b:
                                            zf.writestr("validate.py", val_b)
                                    mem.seek(0)
                                    await storage.upload_bytes(
                                        mem.getvalue(),
                                        results_zip_key,
                                        metadata={"type": "results_archive", "experiment_id": exp_id},
                                    )
                                # Update DB best_result payload
                                if best_program_code:
                                    current = (exp_model.best_result or {}) if exp_model else {}
                                    artifacts = current.get("artifacts") or {}
                                    artifacts.update(
                                        {
                                            "best_program": best_program_key,
                                            "validation": validation_dst_key,
                                            "archive": results_zip_key,
                                        }
                                    )
                                    current.update({"program_code": best_program_code, "artifacts": artifacts})
                                    update_kwargs["best_result"] = current
                            except Exception:
                                # Non-fatal
                                pass
                            try:
                                await db.update_experiment(exp_id, **update_kwargs)
                            except Exception:
                                # Best-effort; continue with others
                                pass
                except Exception:
                    # Suppress loop-wide errors; try again on next tick
                    pass
                await asyncio.sleep(10)

        try:
            _results_ingest_task = asyncio.create_task(_results_ingest_loop())
            logger.info("Started results ingestion loop (10s)")
        except Exception as e:
            logger.warning(f"Failed to start results ingestion loop: {e}")

    except Exception as e:
        logger.error(f"Failed to start GigaEvo Platform Master API: {e}")
        logger.error(f"Service manager will be unavailable. Error details: {str(e)}")
        # Continue anyway so health endpoint can report the error
        # But ensure service_manager remains None to indicate failure


@app.on_event("shutdown")
async def shutdown_event():
    global service_manager, _results_ingest_task

    logger.info("Shutting down GigaEvo Platform Master API...")

    # Cancel results ingestion loop if running
    if _results_ingest_task:
        try:
            _results_ingest_task.cancel()
            await _results_ingest_task
        except Exception:
            pass

    # No background visuals task to cancel

    if service_manager:
        try:
            await service_manager.cleanup()
            logger.info("Service Manager cleaned up successfully")
        except Exception as e:
            logger.error(f"Error during cleanup: {e}")


def get_service_manager() -> ServiceManager:
    """Get the global service manager instance"""
    global service_manager
    if not service_manager:
        raise RuntimeError("Service manager not initialized")
    return service_manager


if __name__ == "__main__":
    config = load_config()
    uvicorn.run(app, host=config.host, port=config.port)
