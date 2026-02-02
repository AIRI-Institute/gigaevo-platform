#!/usr/bin/env python3

from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query

from ...models.instance import RunnerInstance, RunnerInstanceStatus
from ...services.runner_instance_service import RunnerInstanceService
from ...services.service_manager import ServiceManager

router = APIRouter()

# Global service manager instance
_service_manager: ServiceManager = None


def set_service_manager(service_manager: ServiceManager):
    """Set service manager instance"""
    global _service_manager
    _service_manager = service_manager


def get_instance_service() -> RunnerInstanceService:
    """Get instance service instance from service manager"""
    global _service_manager
    if not _service_manager:
        raise RuntimeError("Service manager not initialized")
    return _service_manager.get_instance_service()


@router.get("/", response_model=List[RunnerInstance])
async def list_instances(
    status: RunnerInstanceStatus = Query(None, description="Filter by instance status"),
    service: RunnerInstanceService = Depends(get_instance_service),
):
    """List all runner instances"""
    return await service.list_instances(status)


@router.get("/{instance_id}", response_model=RunnerInstance)
async def get_instance(instance_id: str, service: RunnerInstanceService = Depends(get_instance_service)):
    """Get instance by ID"""
    instance = await service.get_instance(instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")
    return instance


@router.post("/{instance_id}/initialize")
async def initialize_instance(instance_id: str, service: RunnerInstanceService = Depends(get_instance_service)):
    """Initialize a runner instance (start Docker container)"""
    success = await service.initialize_instance(instance_id)
    if not success:
        raise HTTPException(status_code=400, detail="Cannot initialize instance")
    return {"message": "Instance initialized successfully", "instance_id": instance_id}


@router.post("/initialize-all")
async def initialize_all_instances(service: RunnerInstanceService = Depends(get_instance_service)):
    """Initialize all configured runner instances"""
    results = await service.initialize_all_instances()

    successful_count = sum(1 for success in results.values() if success)
    total_count = len(results)

    return {
        "message": f"Initialized {successful_count}/{total_count} instances",
        "results": results,
        "summary": {"total": total_count, "successful": successful_count, "failed": total_count - successful_count},
    }


@router.post("/{instance_id}/stop")
async def stop_instance(instance_id: str, service: RunnerInstanceService = Depends(get_instance_service)):
    """Stop a runner instance"""
    success = await service.stop_instance(instance_id)
    if not success:
        raise HTTPException(status_code=400, detail="Cannot stop instance")
    return {"message": "Instance stopped successfully", "instance_id": instance_id}


@router.post("/{instance_id}/restart")
async def restart_instance(instance_id: str, service: RunnerInstanceService = Depends(get_instance_service)):
    """Restart a runner instance"""
    success = await service.restart_instance(instance_id)
    if not success:
        raise HTTPException(status_code=400, detail="Cannot restart instance")
    return {"message": "Instance restarted successfully", "instance_id": instance_id}


@router.get("/{instance_id}/logs")
async def get_instance_logs(
    instance_id: str,
    lines: int = Query(50, ge=1, le=1000, description="Number of log lines to retrieve"),
    service: RunnerInstanceService = Depends(get_instance_service),
):
    """Get logs from a specific runner instance"""
    logs = await service.get_instance_logs(instance_id, lines)
    if logs.startswith("Error"):
        raise HTTPException(status_code=404, detail=logs)
    return {"instance_id": instance_id, "logs": logs, "lines": lines}


@router.get("/available/instance", response_model=RunnerInstance)
async def get_available_instance(service: RunnerInstanceService = Depends(get_instance_service)):
    """Get an available (ready) instance for experiment deployment"""
    instance = await service.get_available_instance()
    if not instance:
        raise HTTPException(status_code=404, detail="No available instances")
    return instance


@router.get("/health/summary")
async def get_health_summary(service: RunnerInstanceService = Depends(get_instance_service)):
    """Get health summary of all runner instances"""
    instances = await service.list_instances()

    summary = {
        "total_instances": len(instances),
        "healthy_instances": 0,
        "unhealthy_instances": 0,
        "busy_instances": 0,
        "ready_instances": 0,
        "offline_instances": 0,
        "instances_detail": [],
    }

    for instance in instances:
        status_detail = {
            "id": instance.id,
            "name": instance.name,
            "status": instance.status.value,
            "endpoint_url": instance.endpoint_url,
            "last_heartbeat": instance.last_heartbeat.isoformat() if instance.last_heartbeat else None,
            "current_experiment_id": instance.current_experiment_id,
        }

        # Count by status
        if instance.status == RunnerInstanceStatus.READY:
            summary["healthy_instances"] += 1
            summary["ready_instances"] += 1
        elif instance.status == RunnerInstanceStatus.BUSY:
            summary["healthy_instances"] += 1
            summary["busy_instances"] += 1
        elif instance.status in [RunnerInstanceStatus.ONLINE, RunnerInstanceStatus.INITIALIZING]:
            summary["healthy_instances"] += 1
        elif instance.status == RunnerInstanceStatus.ERROR:
            summary["unhealthy_instances"] += 1
        elif instance.status == RunnerInstanceStatus.OFFLINE:
            summary["offline_instances"] += 1

        summary["instances_detail"].append(status_detail)

    return summary


@router.get("/debug/info")
async def debug_info(service: RunnerInstanceService = Depends(get_instance_service)):
    """Debug endpoint to show service status and configuration"""
    try:
        # Get service configuration
        config = service.config

        # Get all instances from database
        db_instances = []
        try:
            db_service = service.db_service
            db_instances = await db_service.list_runners()
            db_instances_info = [
                {
                    "id": inst.id,
                    "name": inst.name,
                    "status": inst.status,
                    "endpoint_url": inst.endpoint_url,
                    "created_at": inst.created_at.isoformat() if inst.created_at else None,
                }
                for inst in db_instances
            ]
        except Exception as e:
            db_instances_info = [{"error": f"Failed to get DB instances: {str(e)}"}]

        # Get configured instances
        configured_instances = []
        for instance_id, instance_config in config.runner.instances.items():
            configured_instances.append(
                {
                    "id": instance_id,
                    "host": instance_config.host,
                    "port": instance_config.port,
                    "is_local": instance_config.is_local,
                    "endpoint_url": f"http://{instance_config.host}:{instance_config.port}",
                }
            )

        return {
            "service_status": "running",
            "configured_instances_count": len(configured_instances),
            "database_instances_count": len(db_instances_info),
            "auto_initialize": config.runner.auto_initialize,
            "configured_instances": configured_instances,
            "database_instances": db_instances_info,
            "docker_services_count": len(service.docker_services),
            "docker_service_ids": list(service.docker_services.keys()) if service.docker_services else [],
        }
    except Exception as e:
        import traceback

        return {"error": str(e), "traceback": traceback.format_exc()}
