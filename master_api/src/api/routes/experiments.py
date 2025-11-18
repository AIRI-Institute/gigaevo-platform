#!/usr/bin/env python3

import os
import tempfile
from typing import Any, Dict, List

import aiofiles
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from loguru import logger

from ...models.experiment import Experiment, ExperimentCreate
from ...services.experiment_service import ExperimentService
from ...services.service_manager import ServiceManager

router = APIRouter()

# Global service manager instance
_service_manager: ServiceManager = None


def set_service_manager(service_manager: ServiceManager):
    """Set service manager instance"""
    global _service_manager
    _service_manager = service_manager


def get_experiment_service() -> ExperimentService:
    """Get experiment service instance from service manager"""
    global _service_manager
    if not _service_manager:
        error_msg = "Service manager not initialized - Master API may still be starting up"

        logger.error(error_msg)
        raise RuntimeError(error_msg)
    return _service_manager.get_experiment_service()


@router.post("/", response_model=Experiment)
async def create_experiment(experiment: ExperimentCreate, service: ExperimentService = Depends(get_experiment_service)):
    """Initialize experiment"""
    return await service.create_experiment(experiment)


@router.post("/upload")
async def upload_data_file(
    file: UploadFile = File(...),
    service: ExperimentService = Depends(get_experiment_service),
):
    """Upload a data file to storage and return the storage path"""
    try:
        # Save uploaded file temporarily
        with tempfile.NamedTemporaryFile(delete=False, suffix=".csv") as tmp_file:
            async with aiofiles.open(tmp_file.name, "wb") as f:
                content = await file.read()
                await f.write(content)
            tmp_file_path = tmp_file.name

        try:
            # Upload to storage
            storage_path = await service.storage_service.upload_experiment_data(tmp_file_path, str(file.filename))
            if not storage_path:
                raise HTTPException(status_code=500, detail="Failed to upload data file")
            return {"data_path": storage_path, "filename": file.filename}

        finally:
            # Clean up temporary file
            os.unlink(tmp_file_path)

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to upload file: {str(e)}")


@router.post("/{experiment_id}/deploy")
async def deploy_experiment(
    experiment_id: str,
    runner_id: str | None = None,
    service: ExperimentService = Depends(get_experiment_service),
):
    """Manually deploy experiment to runner"""
    success = await service.manually_deploy_experiment(experiment_id, runner_id)
    if not success:
        raise HTTPException(status_code=400, detail="Cannot deploy experiment")
    return {"message": "Experiment deployed", "experiment_id": experiment_id}


@router.get("/", response_model=List[Experiment])
async def list_experiments(
    service: ExperimentService = Depends(get_experiment_service),
):
    """Get list of experiments"""
    return await service.list_experiments()


@router.get("/{experiment_id}/status", response_model=Dict[str, Any])
async def get_experiment_status(experiment_id: str, service: ExperimentService = Depends(get_experiment_service)):
    """Request status of specific experiment"""
    status = await service.get_experiment_status(experiment_id)
    if not status:
        raise HTTPException(status_code=404, detail="Experiment not found")
    return status


@router.post("/{experiment_id}/start")
async def start_experiment(experiment_id: str, service: ExperimentService = Depends(get_experiment_service)):
    """Start experiment"""
    success, error_message = await service.start_experiment_forward(experiment_id)
    if not success:
        raise HTTPException(status_code=400, detail=f"ERROR: {error_message}")
    return {"message": "Experiment started", "experiment_id": experiment_id}


@router.post("/{experiment_id}/stop")
async def stop_experiment(experiment_id: str, service: ExperimentService = Depends(get_experiment_service)):
    """Stop experiment"""
    success, error_message = await service.stop_experiment_forward(experiment_id)
    if not success:
        raise HTTPException(status_code=400, detail=f"ERROR: {error_message}")
    return {"message": "Experiment stopped", "experiment_id": experiment_id}


@router.get("/{experiment_id}/results", response_model=Dict[str, Any])
async def get_experiment_results(experiment_id: str, service: ExperimentService = Depends(get_experiment_service)):
    """Get results of experiment (live metrics while running, final when completed)."""
    results = await service.get_experiment_results(experiment_id)
    if not results:
        raise HTTPException(status_code=404, detail="Experiment not found")
    return results


@router.get("/{experiment_id}", response_model=Experiment)
async def get_experiment(experiment_id: str, service: ExperimentService = Depends(get_experiment_service)):
    """Get experiment by ID"""
    experiment = await service.get_experiment(experiment_id)
    if not experiment:
        raise HTTPException(status_code=404, detail="Experiment not found")
    return experiment


@router.delete("/drop-all", response_model=Dict[str, Any])
async def drop_all_experiments(service: ExperimentService = Depends(get_experiment_service)):
    """Drop all experiments and their data"""
    try:
        result = await service.drop_all_experiments()
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to drop all experiments: {str(e)}")
