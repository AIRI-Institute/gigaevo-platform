#!/usr/bin/env python3

import os
import tempfile
from typing import Any, Dict, List
from uuid import uuid4

import aiofiles
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from loguru import logger

from ...models.experiment import Experiment, ExperimentCreate, ExperimentConfig, PromptExperimentCreate
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


@router.post("/prompts", response_model=Experiment)
async def create_prompt_experiment(
    prompt_experiment: PromptExperimentCreate,
    service: ExperimentService = Depends(get_experiment_service),
):
    """Create a new prompt-based experiment: create DB record, build files, upload to storage."""
    try:
        logger.info(f"Received prompt experiment creation request: {prompt_experiment.name}")
        import re

        # Basic prompt placeholder validation
        template_pattern = r"\{([^}]+)\}"
        placeholders = re.findall(template_pattern, prompt_experiment.base_prompt)
        if not placeholders:
            raise HTTPException(
                status_code=400,
                detail="Base prompt must contain at least one template placeholder in {column_name} format",
            )

        # Optional simplified task_type validation
        if prompt_experiment.task_type:
            valid_task_types = ["classification", "multi_choice", "math", "summarization"]
            if prompt_experiment.task_type not in valid_task_types:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid task_type. Must be one of: {', '.join(valid_task_types)}",
                )

        # Prepare validation criteria dict (optional)
        vc_dict = None
        try:
            if prompt_experiment.validation_criteria is not None:
                vc_dict = prompt_experiment.validation_criteria.dict()
        except Exception:
            vc_dict = None

        # Build Experiment config and create DB record
        config = ExperimentConfig(
            description=prompt_experiment.description or "",
            parameters={
                "task_type": (prompt_experiment.task_type or "prompt"),
                "target_column": prompt_experiment.target_column,
                "base_prompt": prompt_experiment.base_prompt,
                **({"validation_criteria": vc_dict} if vc_dict else {}),
            },
            llm_model=prompt_experiment.llm_model,
            max_iterations=prompt_experiment.max_iterations,
        )
        experiment_create = ExperimentCreate(
            name=prompt_experiment.name,
            config=config,
            data_path=prompt_experiment.data_path,
        )

        # Create experiment directly in DB to avoid Kafka workflow for prompt presets
        from ...services.database_service import DatabaseService  # noqa: F401
        global _service_manager
        if not _service_manager:
            raise HTTPException(status_code=503, detail="Service manager not initialized")
        db = _service_manager.get_db_service()
        from uuid import uuid4
        experiment_id = f"exp_{uuid4()}"
        await db.create_experiment(experiment_create, experiment_id)

        # Build and upload prompt experiment files to storage so it's ready to start
        try:
            from ...services.service_manager import ServiceManager  # noqa: F401
            if _service_manager and hasattr(_service_manager, "creation_service"):
                creation = _service_manager.creation_service
                prompt_spec: Dict[str, Any] = {
                    "name": prompt_experiment.name,
                    "description": prompt_experiment.description or "",
                    "data_path": prompt_experiment.data_path,
                    "target_column": prompt_experiment.target_column,
                    "base_prompt": prompt_experiment.base_prompt,
                    "validation_criteria": (vc_dict or {}),
                    "llm_model": prompt_experiment.llm_model,
                    "max_iterations": prompt_experiment.max_iterations,
                }
                await creation.create_prompt_experiment_files(experiment_id, prompt_spec, prompt_experiment.data_path)
        except Exception as e:
            # Do not fail creation if files can't be prepared; log for observability
            logger.error(f"Failed to prepare prompt experiment files for {experiment_id}: {e}")

        # Return created experiment model
        created = await service.get_experiment(experiment_id)
        if not created:
            raise HTTPException(status_code=500, detail="Experiment created but not found")
        return created

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating prompt experiment: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to create prompt experiment: {str(e)}")


@router.delete("/drop-all", response_model=Dict[str, Any])
async def drop_all_experiments(service: ExperimentService = Depends(get_experiment_service)):
    """Drop all experiments and their data"""
    try:
        result = await service.drop_all_experiments()
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to drop all experiments: {str(e)}")
