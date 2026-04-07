#!/usr/bin/env python3

from datetime import datetime, timezone
from typing import Any, Dict

import psutil
from loguru import logger

from common.version import __version__

from ..models.experiment import ExperimentStatus
from ..models.instance import RunnerInstanceStatus


class StatusService:
    def __init__(self, service_manager=None):
        self.service_manager = service_manager
        self.start_time = datetime.now(timezone.utc)
        # Latch once runners reach a healthy state; startup/offline before this is treated as initializing.
        self._runners_ever_healthy = False

    async def get_storage_info(self) -> Dict[str, Any]:
        """Return minimal storage info for clients (bucket name, endpoint)."""
        try:
            cfg = self.service_manager.config if self.service_manager else None
            if not cfg:
                return {"error": "config not available"}
            return {
                "endpoint_url": cfg.storage.endpoint_url,
                "bucket_name": cfg.storage.bucket_name,
            }
        except Exception as e:
            return {"error": str(e)}

    async def get_system_health(self) -> Dict[str, Any]:
        """Overall system health check"""
        try:
            # Check critical services
            critical_services_healthy = self.service_manager and self.service_manager.is_healthy()

            overall_status = "healthy" if critical_services_healthy else "degraded"

            # Component health
            components = {}

            # Database health
            if self.service_manager and self.service_manager.db_service:
                try:
                    # Simple database health check - try to query count of experiments
                    _ = await self.service_manager.db_service.list_experiments()
                    components["database"] = "healthy"
                except Exception as e:
                    logger.error(f"Database health check failed: {e}")
                    components["database"] = "unhealthy"
                    overall_status = "unhealthy"
            else:
                components["database"] = "not_configured"

            # Storage health
            if self.service_manager and self.service_manager.storage_service:
                try:
                    # Check if we can list buckets or access storage
                    components["storage"] = "healthy"
                except Exception as e:
                    logger.error(f"Storage health check failed: {e}")
                    components["storage"] = "unhealthy"
                    if overall_status == "healthy":
                        overall_status = "degraded"
            else:
                components["storage"] = "not_configured"

            # Kafka health
            if self.service_manager and self.service_manager.kafka_service:
                # Kafka health check would require more complex logic
                # For now, check if service is configured
                components["kafka"] = "configured" if self.service_manager.config.kafka.enabled else "disabled"
            else:
                components["kafka"] = "not_configured"

            # Runner instances health
            if self.service_manager and self.service_manager.instance_service:
                try:
                    instances = await self.service_manager.instance_service.list_instances()
                    total_instances = len(instances)
                    healthy_instances = sum(
                        1
                        for inst in instances
                        if inst.status in [RunnerInstanceStatus.READY, RunnerInstanceStatus.BUSY]
                    )

                    if total_instances == 0:
                        components["runners"] = "no_instances"
                    elif healthy_instances == total_instances:
                        components["runners"] = "healthy"
                        self._runners_ever_healthy = True
                    else:
                        has_error_or_offline = any(
                            inst.status in [RunnerInstanceStatus.ERROR, RunnerInstanceStatus.OFFLINE]
                            for inst in instances
                        )
                        has_initializing_or_online = any(
                            inst.status in [RunnerInstanceStatus.INITIALIZING, RunnerInstanceStatus.ONLINE]
                            for inst in instances
                        )
                        if has_error_or_offline and not self._runners_ever_healthy:
                            components["runners"] = "initializing"
                            if overall_status == "healthy":
                                overall_status = "initializing"
                        elif has_error_or_offline:
                            components["runners"] = "degraded"
                            if overall_status == "healthy":
                                overall_status = "degraded"
                        elif has_initializing_or_online:
                            components["runners"] = "initializing"
                            if overall_status == "healthy":
                                overall_status = "initializing"
                        else:
                            components["runners"] = "degraded"
                            if overall_status == "healthy":
                                overall_status = "degraded"
                except Exception as e:
                    logger.error(f"Runner health check failed: {e}")
                    components["runners"] = "unhealthy"
                    overall_status = "unhealthy"
            else:
                components["runners"] = "not_configured"

            return {
                "status": overall_status,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "version": __version__,
                "uptime_seconds": int((datetime.now(timezone.utc) - self.start_time).total_seconds()),
                "components": components,
            }

        except Exception as e:
            logger.error(f"System health check failed: {e}")
            return {
                "status": "error",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "version": __version__,
                "error": str(e),
                "components": {},
            }

    async def get_experiments_status(self) -> Dict[str, Any]:
        """Get overall experiments status"""
        try:
            if not self.service_manager or not self.service_manager.db_service:
                return {"error": "Database service not available"}

            # Get experiments from database
            experiments = await self.service_manager.db_service.list_experiments()

            # Count by status
            status_counts = {
                "total": len(experiments),
                "pending": 0,
                "queued": 0,
                "dispatching": 0,
                "preparing": 0,
                "prepared": 0,
                "initializing": 0,
                "running": 0,
                "completed": 0,
                "failed": 0,
                "terminated": 0,
                "preparation_failed": 0,
                "stopped": 0,
            }

            for exp in experiments:
                status = exp.status
                if status in status_counts:
                    status_counts[status] += 1
                else:
                    # Unknown statuses: count as failed for visibility but keep distinct bucket too
                    status_counts["failed"] += 1

            # Add recent experiments (last 5)
            recent_experiments = []
            for exp in sorted(experiments, key=lambda x: x.created_at or "", reverse=True)[:5]:
                recent_experiments.append(
                    {
                        "id": exp.id,
                        "name": exp.name,
                        "status": exp.status,
                        "created_at": exp.created_at.isoformat() if exp.created_at else None,
                        "progress": exp.metrics.get("progress", 0) if exp.metrics else 0,
                        "status_message": getattr(exp, "status_message", None),
                        "error_message": exp.error_message,
                    }
                )

            return {
                **status_counts,
                "recent_experiments": recent_experiments,
            }

        except Exception as e:
            logger.error(f"Failed to get experiments status: {e}")
            return {"error": str(e), "total": 0, "pending": 0, "running": 0, "completed": 0, "failed": 0}

    async def get_runners_status(self) -> Dict[str, Any]:
        """Get all runners status"""
        try:
            if not self.service_manager or not self.service_manager.instance_service:
                return {"error": "Instance service not available"}

            instances = await self.service_manager.instance_service.list_instances()

            # Count by status
            status_counts = {
                "total": len(instances),
                "online": 0,
                "offline": 0,
                "busy": 0,
                "ready": 0,
                "error": 0,
                "initializing": 0,
                "terminating": 0,
            }

            instance_details = []
            for instance in instances:
                if instance.status.value not in status_counts:
                    status_counts[instance.status.value] = 0
                status_counts[instance.status.value] += 1

                # Convert online/offline counts based on status
                if instance.status in [
                    RunnerInstanceStatus.READY,
                    RunnerInstanceStatus.BUSY,
                    RunnerInstanceStatus.ONLINE,
                    RunnerInstanceStatus.INITIALIZING,
                ]:
                    status_counts["online"] += 1
                elif instance.status == RunnerInstanceStatus.OFFLINE:
                    status_counts["offline"] += 1

                instance_details.append(
                    {
                        "id": instance.id,
                        "name": instance.name,
                        "status": instance.status.value,
                        "endpoint_url": instance.endpoint_url,
                        "last_heartbeat": instance.last_heartbeat.isoformat() if instance.last_heartbeat else None,
                        "current_experiment_id": instance.current_experiment_id,
                        "capabilities": instance.capabilities,
                        "resources": instance.resources,
                    }
                )

            return {
                **status_counts,
                "instances": instance_details,
            }

        except Exception as e:
            logger.error(f"Failed to get runners status: {e}")
            return {"error": str(e), "total": 0, "online": 0, "offline": 0, "busy": 0, "instances": []}

    async def get_system_metrics(self) -> Dict[str, Any]:
        """Get system performance metrics"""
        try:
            # System resource usage
            cpu_percent = psutil.cpu_percent(interval=1)
            memory = psutil.virtual_memory()
            disk = psutil.disk_usage("/")

            # Network I/O
            network = psutil.net_io_counters()

            # Get active experiments and tasks counts
            experiments_metrics = {"active": 0, "queued": 0}
            if self.service_manager and self.service_manager.db_service:
                try:
                    experiments = await self.service_manager.db_service.list_experiments()
                    experiments_metrics["active"] = sum(
                        1 for exp in experiments if exp.status == ExperimentStatus.RUNNING
                    )
                    experiments_metrics["queued"] = sum(
                        1
                        for exp in experiments
                        if exp.status
                        in [
                            ExperimentStatus.QUEUED.value,
                            ExperimentStatus.DISPATCHING.value,
                        ]
                    )
                except Exception as e:
                    logger.debug(f"Could not get experiment metrics: {e}")

            # Get runner instances metrics
            runners_metrics = {"total": 0, "busy": 0, "ready": 0}
            if self.service_manager and self.service_manager.instance_service:
                try:
                    instances = await self.service_manager.instance_service.list_instances()
                    runners_metrics["total"] = len(instances)
                    runners_metrics["busy"] = sum(1 for inst in instances if inst.status == RunnerInstanceStatus.BUSY)
                    runners_metrics["ready"] = sum(1 for inst in instances if inst.status == RunnerInstanceStatus.READY)
                except Exception as e:
                    logger.debug(f"Could not get runner metrics: {e}")

            return {
                "cpu_usage": round(cpu_percent, 2),
                "memory_usage": round(memory.percent, 2),
                "memory_total_gb": round(memory.total / (1024**3), 2),
                "memory_used_gb": round(memory.used / (1024**3), 2),
                "disk_usage": round(disk.percent, 2),
                "disk_total_gb": round(disk.total / (1024**3), 2),
                "disk_used_gb": round(disk.used / (1024**3), 2),
                "network_io": {
                    "bytes_sent": network.bytes_sent,
                    "bytes_received": network.bytes_recv,
                    "packets_sent": network.packets_sent,
                    "packets_recv": network.packets_recv,
                },
                "active_experiments": experiments_metrics["active"],
                "queued_experiments": experiments_metrics["queued"],
                "runner_instances": runners_metrics,
                "uptime_seconds": int((datetime.now(timezone.utc) - self.start_time).total_seconds()),
            }

        except Exception as e:
            logger.error(f"Failed to get system metrics: {e}")
            return {
                "error": str(e),
                "cpu_usage": 0.0,
                "memory_usage": 0.0,
                "disk_usage": 0.0,
                "network_io": {"bytes_sent": 0, "bytes_received": 0},
                "active_experiments": 0,
                "queued_experiments": 0,
            }
