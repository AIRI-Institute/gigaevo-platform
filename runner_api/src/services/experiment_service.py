#!/usr/bin/env python3

import asyncio
import base64
import json
import logging
import shutil
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from minio import Minio
from minio.error import S3Error
from redis.asyncio import Redis

from ..config import load_config
from ..models.task import Task, TaskCreate, TaskStatus, TaskType

logger = logging.getLogger(__name__)


class ExperimentStatus(str, Enum):
    INITIALIZED = "initialized"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ExperimentService:
    def __init__(self):
        self.config = load_config()
        self.redis: Optional[Redis] = None
        self.minio_client: Optional[Minio] = None
        self._experiment_workspaces = Path(self.config.gigavolve.clone_path) / "problems"
        self._experiment_workspaces.mkdir(exist_ok=True)

    async def _get_redis(self) -> Redis:
        """Get Redis connection"""
        if self.redis is None:
            self.redis = Redis.from_url(self.config.redis.url, decode_responses=True)
        return self.redis

    def _get_minio_client(self) -> Minio:
        """Get MinIO client"""
        if self.minio_client is None:
            # Derive endpoint host:port and secure flag from config and endpoint URL
            endpoint_url = self.config.storage.endpoint_url
            host_port = endpoint_url
            try:
                from urllib.parse import urlparse

                parsed = urlparse(endpoint_url)
                # Extract host:port (urlparse puts bare "host:port" into path)
                if parsed.netloc:
                    host_port = parsed.netloc
                else:
                    host_port = parsed.path.lstrip("/")
            except Exception:
                host_port = endpoint_url.replace("http://", "").replace("https://", "")
            self.minio_client = Minio(
                host_port,
                access_key=self.config.storage.access_key,
                secret_key=self.config.storage.secret_key,
                secure=endpoint_url.startswith("https://"),
            )
        return self.minio_client

    async def close(self):
        """Close connections and cleanup resources"""
        if self.redis:
            await self.redis.close()
            self.redis = None

    async def list_running_experiment_ids(self) -> List[str]:
        """Return list of experiment ids that are currently in RUNNING state (from Redis)."""
        try:
            redis = await self._get_redis()
            # Scan over status hashes
            cursor = 0
            running: List[str] = []
            pattern = "experiment:*:status"
            while True:
                cursor, keys = await redis.scan(cursor=cursor, match=pattern, count=100)
                if keys:
                    # Fetch all hashes in batch
                    pipe = redis.pipeline()
                    for k in keys:
                        pipe.hget(k, "status")
                    statuses = await pipe.execute()
                    for k, st in zip(keys, statuses):
                        if st == ExperimentStatus.RUNNING.value:
                            # extract experiment id from key experiment:{id}:status
                            try:
                                exp_id = str(k).split(":")[1]
                                running.append(exp_id)
                            except Exception:
                                continue
                if cursor == 0:
                    break
            return running
        except Exception as e:
            logger.error(f"Failed to list running experiments: {e}")
            return []

    async def save_artifacts_to_workspace(
        self, experiment_id: str, artifacts: List[Dict[str, Any]]
    ) -> Dict[str, Optional[Path]]:
        """Save each artifact to workspace/results/<dst>.

        Artifacts are dictionaries: {'src': Path|str, 'dst': str}
        """
        results: Dict[str, Optional[Path]] = {}
        try:
            workspace = self._experiment_workspaces / str(experiment_id)
            workspace.mkdir(parents=True, exist_ok=True)
            root = workspace / "results"
            root.mkdir(parents=True, exist_ok=True)
            for item in artifacts or []:
                try:
                    src = item.get("src")
                    dst_name = Path(str(item.get("dst") or "")).name
                    if not src or not dst_name:
                        results[dst_name or ""] = None
                        continue
                    if isinstance(src, str):
                        src = Path(src)
                    if not isinstance(src, Path) or not src.exists():
                        results[dst_name] = None
                        continue
                    dst = root / dst_name
                    await asyncio.to_thread(shutil.copyfile, str(src), str(dst))
                    results[dst_name] = dst
                except Exception:
                    key = Path(str(item.get("dst") or "")).name if isinstance(item, dict) else ""
                    results[key] = None
        except Exception as e:
            logger.warning(f"Failed to save local artifacts for {experiment_id}: {e}")
        return results

    async def upload_artifacts_to_storage(self, experiment_id: str, artifacts: List[Dict[str, Any]]) -> bool:
        """Upload each artifact to s3://<bucket>/experiments_results/<experiment_id>/<dst>.

        Artifacts are dictionaries: {'src': Path|str, 'dst': str}
        """
        uploaded_any = False
        try:
            minio = self._get_minio_client()
            bucket_name = self.config.storage.bucket_name
            prefix = f"experiments_results/{experiment_id}/"
            for item in artifacts or []:
                try:
                    src = item.get("src")
                    dst_name = Path(str(item.get("dst") or "")).name
                    if not src or not dst_name:
                        continue
                    if isinstance(src, str):
                        src = Path(src)
                    if not isinstance(src, Path) or not src.exists():
                        continue
                    storage_key = prefix + dst_name
                    await asyncio.to_thread(minio.fput_object, bucket_name, storage_key, str(src))
                    uploaded_any = True
                except Exception:
                    continue
            if uploaded_any:
                logger.info(f"Uploaded artifacts for {experiment_id} to s3://{bucket_name}/{prefix}")
        except Exception as e:
            logger.error(f"Failed to upload artifacts for {experiment_id}: {e}")
        return uploaded_any

    async def initialize_experiment(self, experiment_id: str, config: Dict[str, Any]) -> bool:
        """Initialize experiment in runner"""
        try:
            experiment_id_str = str(experiment_id)
            workspace = self._experiment_workspaces / experiment_id_str
            workspace.mkdir(exist_ok=True)

            # Store experiment config (async file write)
            config_path = workspace / "config.json"
            config_json = json.dumps(config, indent=4)
            await asyncio.to_thread(config_path.write_text, config_json)

            # Download data from storage
            data_path = config.get("dataset_path", "")
            if not data_path:
                logger.error(f"Experiment {experiment_id} initialization failed: no dataset path found in config")
                return False

            downloaded = await self._download_experiment_data(experiment_id, data_path, workspace)
            if not downloaded:
                logger.error(
                    f"Experiment {experiment_id} initialization failed: data download failed for path '{data_path}'"
                )
                return False

            # Create experiment status in Redis
            redis = await self._get_redis()
            status_key = f"experiment:{experiment_id_str}:status"
            await redis.hset(
                status_key,
                mapping={
                    "status": ExperimentStatus.INITIALIZED.value,
                    "progress": "0.0",
                    "workspace": str(workspace),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                },
            )

            logger.info(f"Experiment {experiment_id} initialized in workspace {workspace}")
            return True

        except Exception as e:
            logger.error(f"Failed to initialize experiment {experiment_id}: {e}")
            return False

    async def _download_experiment_data(self, experiment_id: str, data_path: str, workspace: Path) -> bool:
        """Download experiment data from MinIO storage. Returns True on success, False otherwise."""
        try:
            minio = self._get_minio_client()

            def _safe_local_path(dest_root: Path, rel_key: str) -> Optional[Path]:
                """Resolve a relative object key into a path under dest_root, rejecting traversal."""
                try:
                    # Normalize and strip any leading separators
                    rel = rel_key.lstrip("/\\")
                    # Quick traversal check on split components
                    if any(part in ("..", "") for part in rel.split("/")):
                        return None
                    candidate = (dest_root / rel).resolve()
                    dest_root_resolved = dest_root.resolve()
                    # Ensure candidate is within dest_root
                    _ = candidate.relative_to(dest_root_resolved)
                    return candidate
                except Exception:
                    return None

            # Parse S3-style path or use as bucket/key
            if data_path.startswith("s3://"):
                # s3://bucket/key format
                path_parts = data_path[5:].split("/", 1)
                bucket_name = path_parts[0]
                object_name = path_parts[1] if len(path_parts) > 1 and path_parts[1] else None
                if not object_name:
                    raise ValueError(f"Invalid S3 path: {data_path}")
            else:
                # relative path in the default bucket
                bucket_name = self.config.storage.bucket_name
                object_name = data_path.lstrip("/")
                if not object_name:
                    raise ValueError(f"Invalid data path: {data_path}")

            # If object_name is a "folder" prefix, download all objects under it
            is_prefix = object_name.endswith("/")
            prefix = object_name if is_prefix else f"{object_name}/"

            try:
                objects = await asyncio.to_thread(
                    lambda: list(minio.list_objects(bucket_name, prefix=prefix, recursive=True))
                )
            except Exception:
                objects = []

            if is_prefix:
                if len(objects) == 0:
                    logger.warning(f"No objects found under prefix '{object_name}' for experiment {experiment_id}")
                    return False
                # Treat as folder/prefix: download all objects under prefix directly into workspace, preserving structure
                dest_root = workspace
                for obj in objects:
                    rel_key = obj.object_name[len(prefix) :]
                    if not rel_key:
                        continue
                    local_path = _safe_local_path(dest_root, rel_key)
                    if local_path is None:
                        logger.warning(
                            f"Skipped unsafe object path '{obj.object_name}' while downloading experiment {experiment_id}"
                        )
                        continue
                    local_path.parent.mkdir(parents=True, exist_ok=True)
                    await asyncio.to_thread(minio.fget_object, bucket_name, obj.object_name, str(local_path))
                logger.info(
                    f"Downloaded prefix '{object_name}' for experiment {experiment_id} into workspace {dest_root}"
                )
                return True
            elif len(objects) > 0:
                # Treat as folder detected by listing without explicit trailing slash; download into workspace
                dest_root = workspace
                for obj in objects:
                    rel_key = obj.object_name[len(prefix) :]
                    if not rel_key:
                        continue
                    local_path = _safe_local_path(dest_root, rel_key)
                    if local_path is None:
                        logger.warning(
                            f"Skipped unsafe object path '{obj.object_name}' while downloading experiment {experiment_id}"
                        )
                        continue
                    local_path.parent.mkdir(parents=True, exist_ok=True)
                    await asyncio.to_thread(minio.fget_object, bucket_name, obj.object_name, str(local_path))
                logger.info(
                    f"Downloaded prefix '{object_name}' for experiment {experiment_id} into workspace {dest_root}"
                )
                return True
            else:
                # Download single file (async to avoid blocking event loop)
                local_file = workspace / Path(object_name).name
                local_file.parent.mkdir(parents=True, exist_ok=True)
                await asyncio.to_thread(minio.fget_object, bucket_name, object_name, str(local_file))
                logger.info(f"Downloaded data for experiment {experiment_id} to {local_file}")
                return True

        except S3Error as e:
            logger.warning(f"Failed to download data for experiment {experiment_id}: {e}")
            return False
        except Exception as e:
            logger.error(f"Error downloading data for experiment {experiment_id}: {e}")
            return False

    async def start_experiment(self, experiment_id: str) -> bool:
        """Start experiment execution"""
        try:
            experiment_id_str = str(experiment_id)
            redis = await self._get_redis()

            # Validate experiment state
            status_key = f"experiment:{experiment_id_str}:status"
            status_data = await redis.hgetall(status_key)

            if not status_data:
                logger.error(f"Experiment {experiment_id} not initialized")
                return False

            current_status = status_data.get("status", "")
            if current_status in [
                ExperimentStatus.RUNNING.value,
                ExperimentStatus.COMPLETED.value,
            ]:
                logger.warning(f"Experiment {experiment_id} is already {current_status}")
                return False

            # Create task plan for experiment execution
            task_plan = [
                TaskCreate(
                    experiment_id=experiment_id,
                    task_type=TaskType.RUN_EXPERIMENT,
                    parameters={
                        "config": await self._get_experiment_config(experiment_id),
                    },
                )
            ]

            # Use pipeline for atomic operations
            pipe = redis.pipeline()

            # Update status to running
            pipe.hset(status_key, "status", ExperimentStatus.RUNNING.value)
            pipe.hset(status_key, "started_at", datetime.now(timezone.utc).isoformat())

            # Add tasks to Redis queue
            task_ids = []
            queue_key = f"experiment:{experiment_id_str}:tasks"

            for item in task_plan:
                task = Task(**item.model_dump())

                # Store complete task data in Redis
                task_key = f"task:{task.id}"
                task_data = {
                    "id": str(task.id),
                    "experiment_id": str(task.experiment_id),
                    "task_type": task.task_type.value,
                    "status": task.status.value,
                    "parameters": json.dumps(task.parameters),
                    "result": json.dumps(task.result) if task.result else "",
                    "error_message": task.error_message or "",
                    "created_at": task.created_at.isoformat(),
                    "started_at": task.started_at.isoformat() if task.started_at else "",
                    "completed_at": task.completed_at.isoformat() if task.completed_at else "",
                    "worker_id": task.worker_id or "",
                    "progress": str(task.progress),
                }
                pipe.hset(task_key, mapping=task_data)

                # Add to experiment task queue
                pipe.lpush(queue_key, str(task.id))

                # Collect for global queue
                task_ids.append(str(task.id))

            # Add all to global task queue for workers
            if task_ids:
                pipe.lpush("task_queue", *task_ids)

            # Execute all operations atomically
            await pipe.execute()

            logger.info(f"Started experiment {experiment_id} with {len(task_plan)} tasks")
            return True

        except Exception as e:
            logger.error(f"Failed to start experiment {experiment_id}: {e}")
            return False

    async def _get_experiment_config(self, experiment_id: str) -> Dict[str, Any]:
        """Get experiment configuration"""
        try:
            workspace = self._experiment_workspaces / str(experiment_id)
            config_path = workspace / "config.json"
            if config_path.exists():
                config_json = await asyncio.to_thread(config_path.read_text)
                return json.loads(config_json)
        except Exception as e:
            logger.error(f"Failed to load config for experiment {experiment_id}: {e}")
        return {}

    async def stop_experiment(self, experiment_id: str) -> bool:
        """Stop experiment execution"""
        try:
            experiment_id_str = str(experiment_id)
            redis = await self._get_redis()

            # Update status to cancelled
            status_key = f"experiment:{experiment_id_str}:status"
            await redis.hset(status_key, "status", ExperimentStatus.CANCELLED.value)
            await redis.hset(status_key, "stopped_at", datetime.now(timezone.utc).isoformat())

            # Get all tasks for this experiment
            queue_key = f"experiment:{experiment_id_str}:tasks"
            task_ids = await redis.lrange(queue_key, 0, -1)

            # Cancel all running tasks
            for task_id in task_ids:
                task_key = f"task:{task_id}"
                task_data = await redis.hgetall(task_key)
                if task_data:
                    status = task_data.get("status", "")
                    if status in [TaskStatus.QUEUED.value, TaskStatus.RUNNING.value]:
                        await redis.hset(task_key, "status", TaskStatus.CANCELLED.value)
                        await redis.hset(task_key, "cancelled_at", datetime.now(timezone.utc).isoformat())

            # Remove from global task queue (only if task_ids is not empty)
            if task_ids:
                # LREM accepts a single value per call; remove each task id
                pipe = redis.pipeline()
                for tid in task_ids:
                    pipe.lrem("task_queue", 0, tid)
                await pipe.execute()

            logger.info(f"Stopped experiment {experiment_id}")
            return True

        except Exception as e:
            logger.error(f"Failed to stop experiment {experiment_id}: {e}")
            return False

    async def get_experiment_status(self, experiment_id: str) -> Optional[Dict[str, Any]]:
        """Get experiment execution status"""
        try:
            experiment_id_str = str(experiment_id)
            redis = await self._get_redis()

            # Get experiment status
            status_key = f"experiment:{experiment_id_str}:status"
            status_data = await redis.hgetall(status_key)

            if not status_data:
                return None

            # Get task progress
            queue_key = f"experiment:{experiment_id_str}:tasks"
            task_ids = await redis.lrange(queue_key, 0, -1)

            total_tasks = len(task_ids)
            completed_tasks = 0
            failed_tasks = 0
            queued_tasks = 0
            running_tasks = 0

            if total_tasks > 0:
                # Fetch all task hashes in one round-trip
                pipe = redis.pipeline()
                for task_id in task_ids:
                    pipe.hgetall(f"task:{task_id}")
                task_hashes = await pipe.execute()

                for task_data in task_hashes:
                    if not task_data:
                        continue
                    task_status = task_data.get("status", "")
                    if task_status == TaskStatus.COMPLETED.value:
                        completed_tasks += 1
                    elif task_status == TaskStatus.FAILED.value:
                        failed_tasks += 1
                    elif task_status == TaskStatus.QUEUED.value:
                        queued_tasks += 1
                    elif task_status == TaskStatus.RUNNING.value:
                        running_tasks += 1

            progress = (completed_tasks / total_tasks * 100) if total_tasks > 0 else 0.0

            return {
                "experiment_id": experiment_id_str,
                "status": status_data.get("status", "unknown"),
                "progress": progress,
                "total_tasks": total_tasks,
                "completed_tasks": completed_tasks,
                "failed_tasks": failed_tasks,
                "queued_tasks": queued_tasks,
                "running_tasks": running_tasks,
                "workspace": status_data.get("workspace", ""),
                "created_at": status_data.get("created_at", ""),
                "started_at": status_data.get("started_at", ""),
                "metrics": {
                    "progress_percentage": progress,
                    "task_completion_rate": completed_tasks / total_tasks if total_tasks > 0 else 0,
                },
            }

        except Exception as e:
            logger.error(f"Failed to get status for experiment {experiment_id}: {e}")
            return None

    async def get_experiment_visualization(self, experiment_id: str) -> Optional[Dict[str, Any]]:
        """Return serialized metrics_plot.png from workspace results as base64."""
        try:
            experiment_id_str = str(experiment_id)
            workspace = self._experiment_workspaces / experiment_id_str
            img_path = workspace / "results" / "metrics_plot.png"
            if not img_path.exists():
                return None
            # Read image bytes and encode to base64
            data = await asyncio.to_thread(img_path.read_bytes)
            b64 = base64.b64encode(data).decode("utf-8")
            return {
                "experiment_id": experiment_id_str,
                "filename": "metrics_plot.png",
                "content_type": "image/png",
                "data_base64": b64,
            }
        except Exception as e:
            logger.error(f"Failed to get visualization for experiment {experiment_id}: {e}")
            return None

    async def get_best_program(self, experiment_id: str) -> Optional[Dict[str, Any]]:
        """Get best program from experiment by reading evolution_report.json in workspace results."""
        try:
            experiment_id_str = str(experiment_id)
            workspace = self._experiment_workspaces / experiment_id_str
            report_path = workspace / "results" / "evolution_report.json"
            if not report_path.exists():
                return None
            # Read and parse JSON report
            raw = await asyncio.to_thread(report_path.read_text, encoding="utf-8")
            try:
                payload = json.loads(raw)
            except Exception:
                return None
            code = payload.get("best_program")
            if not code:
                return None
            return {
                "experiment_id": experiment_id_str,
                "program_id": payload.get("best_program_id"),
                "fitness": payload.get("best_fitness"),
                "created_at": payload.get("best_created_at"),
                "code": code,
            }

        except Exception as e:
            logger.error(f"Failed to get best program for experiment {experiment_id}: {e}")
            return None

    async def get_experiment_logs(self, experiment_id: str) -> Optional[str]:
        """Get experiment logs"""
        try:
            experiment_id_str = str(experiment_id)
            workspace = self._experiment_workspaces / experiment_id_str

            # Try evolution.log first (new location)
            log_file = workspace / "logs" / "evolution.log"
            if not log_file.exists():
                # Fallback to old location
                log_file = workspace / "experiment.log"

            if log_file.exists():
                return await asyncio.to_thread(log_file.read_text)
            return None

        except Exception as e:
            logger.error(f"Failed to get logs for experiment {experiment_id}: {e}")
            return None

    async def list_uploaded_experiments(self, limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
        """List uploaded experiments from MinIO storage"""
        try:
            minio = self._get_minio_client()
            bucket_name = self.config.storage.bucket_name

            # List experiment objects in storage under "experiments/{experiment_id}/..."
            experiment_prefix = "experiments/"
            objects = await asyncio.to_thread(
                lambda: list(minio.list_objects(bucket_name, prefix=experiment_prefix, recursive=True))
            )

            # Group by experiment_id (first path segment after "experiments/")
            experiments_map: Dict[str, Dict[str, Any]] = {}
            redis = await self._get_redis()

            for obj in objects:
                # Skip any objects that don't live under the experiments/ prefix
                object_name = obj.object_name
                if not object_name.startswith(experiment_prefix):
                    continue

                # Strip leading prefix and split on first '/'
                rel = object_name[len(experiment_prefix) :]
                if not rel:
                    continue
                experiment_id = rel.split("/", 1)[0]
                if not experiment_id:
                    continue

                # Fetch metadata from Redis only once per experiment_id
                info = experiments_map.get(experiment_id)
                if info is None:
                    status_key = f"experiment:{experiment_id}:status"
                    status_data = await redis.hgetall(status_key)
                    created_at = status_data.get("created_at") or obj.last_modified.isoformat()
                    info = {
                        "experiment_id": experiment_id,
                        "storage_path": f"{experiment_prefix}{experiment_id}/",
                        "status": status_data.get("status", "uploaded"),
                        "created_at": created_at,
                        "last_modified": obj.last_modified.isoformat(),
                        "size": obj.size,
                        "workspace": status_data.get("workspace", ""),
                        "initialized": bool(status_data),
                    }
                    experiments_map[experiment_id] = info
                else:
                    # Update aggregate metadata (e.g., last_modified, size)
                    if obj.last_modified.isoformat() > info["last_modified"]:
                        info["last_modified"] = obj.last_modified.isoformat()
                    info["size"] += obj.size

            experiments = list(experiments_map.values())

            # Sort by creation date (newest first) and apply pagination
            experiments.sort(key=lambda x: x["created_at"], reverse=True)
            paginated_experiments = experiments[offset : offset + limit]

            logger.info(f"Listed {len(paginated_experiments)} experiments (total: {len(experiments)})")
            return paginated_experiments

        except Exception as e:
            logger.error(f"Failed to list uploaded experiments: {e}")
            return []

    async def cleanup_experiment(self, experiment_id: str) -> bool:
        """Clean up experiment workspace and Redis data"""
        try:
            experiment_id_str = str(experiment_id)
            redis = await self._get_redis()

            # Get task IDs
            queue_key = f"experiment:{experiment_id_str}:tasks"
            task_ids = await redis.lrange(queue_key, 0, -1)

            # Delete task data
            for task_id in task_ids:
                task_key = f"task:{task_id}"
                await redis.delete(task_key)

            # Remove from global task queue
            if task_ids:
                pipe = redis.pipeline()
                for tid in task_ids:
                    pipe.lrem("task_queue", 0, tid)
                await pipe.execute()

            # Remove Redis data
            status_key = f"experiment:{experiment_id_str}:status"
            await redis.delete(status_key, queue_key)

            # Clean up workspace
            workspace = self._experiment_workspaces / experiment_id_str
            if workspace.exists():
                await asyncio.to_thread(shutil.rmtree, workspace)

            # Clean up experiment outputs under the cloned repo (outputs/<id>)
            try:
                clone_root = Path(self.config.gigavolve.clone_path).resolve()
                exp_outputs_dir = clone_root / "outputs" / experiment_id_str
                if exp_outputs_dir.exists():
                    await asyncio.to_thread(shutil.rmtree, exp_outputs_dir)
                    logger.info(f"Removed experiment outputs at {exp_outputs_dir}")
            except Exception as e:
                logger.warning(f"Failed to remove experiment outputs for experiment {experiment_id_str}: {e}")

            # Remove uploaded artifacts from storage (experiments_results/<id>/...)
            try:
                minio = self._get_minio_client()
                bucket_name = self.config.storage.bucket_name
                prefix = f"experiments_results/{experiment_id_str}/"
                objects = await asyncio.to_thread(
                    lambda: list(minio.list_objects(bucket_name, prefix=prefix, recursive=True))
                )
                for obj in objects:
                    try:
                        await asyncio.to_thread(minio.remove_object, bucket_name, obj.object_name)
                    except Exception:
                        # Best-effort cleanup; continue with next object
                        pass
                logger.info(f"Removed storage artifacts with prefix s3://{bucket_name}/{prefix}")
            except Exception as e:
                logger.warning(f"Failed to remove storage artifacts for experiment {experiment_id_str}: {e}")

            logger.info(f"Cleaned up experiment {experiment_id}")
            return True

        except Exception as e:
            logger.error(f"Failed to cleanup experiment {experiment_id}: {e}")
            return False
