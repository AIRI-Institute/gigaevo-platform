#!/usr/bin/env python3

from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, Query

from ...services.experiment_service import ExperimentService

router = APIRouter()

_experiment_service: ExperimentService | None = None


def set_experiment_service(service: ExperimentService | None) -> None:
    global _experiment_service
    _experiment_service = service


def get_experiment_service() -> ExperimentService:
    if _experiment_service is None:
        raise HTTPException(status_code=500, detail="ExperimentService not initialized")
    return _experiment_service


@router.post("/{experiment_id}/initialize")
async def initialize_experiment(
    experiment_id: str, config: Dict[str, Any], service: ExperimentService = Depends(get_experiment_service)
):
    """Initialize experiment"""
    success = await service.initialize_experiment(experiment_id, config)
    if not success:
        raise HTTPException(status_code=400, detail="Cannot initialize experiment")
    return {"message": "Experiment initialized", "experiment_id": experiment_id}


@router.post("/{experiment_id}/start")
async def start_experiment(experiment_id: str, service: ExperimentService = Depends(get_experiment_service)):
    """Start experiment execution"""
    success = await service.start_experiment(experiment_id)
    if not success:
        raise HTTPException(status_code=400, detail="Cannot start experiment")
    return {"message": "Experiment started", "experiment_id": experiment_id}


@router.post("/{experiment_id}/stop")
async def stop_experiment(experiment_id: str, service: ExperimentService = Depends(get_experiment_service)):
    """Stop experiment execution"""
    success = await service.stop_experiment(experiment_id)
    if not success:
        raise HTTPException(status_code=400, detail="Cannot stop experiment")
    return {"message": "Experiment stopped", "experiment_id": experiment_id}


@router.get("/{experiment_id}/status")
async def get_experiment_status(experiment_id: str, service: ExperimentService = Depends(get_experiment_service)):
    """Get experiment execution status"""
    status = await service.get_experiment_status(experiment_id)
    if not status:
        raise HTTPException(status_code=404, detail="Experiment not found")
    return status


@router.get("/{experiment_id}/visualization")
async def get_experiment_visualization(
    experiment_id: str, service: ExperimentService = Depends(get_experiment_service)
):
    """Get visualization of experiment results"""
    visualization = await service.get_experiment_visualization(experiment_id)
    if not visualization:
        raise HTTPException(status_code=404, detail="Visualization not found")
    return visualization


@router.get("/{experiment_id}/best-program")
async def get_best_program(experiment_id: str, service: ExperimentService = Depends(get_experiment_service)):
    """Get best program from experiment"""
    best_program = await service.get_best_program(experiment_id)
    if not best_program:
        raise HTTPException(status_code=404, detail="Best program not found")
    return best_program


@router.get("/{experiment_id}/logs")
async def get_experiment_logs(experiment_id: str, service: ExperimentService = Depends(get_experiment_service)):
    """Get experiment logs (optional)"""
    logs = await service.get_experiment_logs(experiment_id)
    if logs is None:
        raise HTTPException(status_code=404, detail="Logs not found")
    return {"logs": logs, "experiment_id": experiment_id}


@router.post("/{experiment_id}/cleanup")
async def cleanup_experiment(experiment_id: str, service: ExperimentService = Depends(get_experiment_service)):
    """Remove all tasks and workspace for an experiment"""
    success = await service.cleanup_experiment(experiment_id)
    if not success:
        raise HTTPException(status_code=400, detail="Cannot cleanup experiment")
    return {"message": "Experiment cleaned up", "experiment_id": experiment_id}


@router.get("/")
async def list_uploaded_experiments(
    limit: int = Query(default=100, le=1000, description="Maximum number of experiments to return"),
    offset: int = Query(default=0, ge=0, description="Number of experiments to skip"),
    service: ExperimentService = Depends(get_experiment_service),
) -> List[Dict[str, Any]]:
    """List uploaded experiments from storage"""
    experiments = await service.list_uploaded_experiments(limit=limit, offset=offset)
    return experiments
