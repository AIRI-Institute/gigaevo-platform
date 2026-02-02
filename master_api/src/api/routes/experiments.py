#!/usr/bin/env python3

import json
import os
import re
import tempfile
from typing import Any, Dict, List

import aiofiles
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from loguru import logger

from common.llm_registry import get_allowed_llm_model_ids, is_allowed_llm_model

from ...models.experiment import (
    Experiment,
    ExperimentConfig,
    ExperimentCreate,
    PromptExperimentCreate,
    ChainExperimentCreate,
)
from ...services.experiment_service import ExperimentService
from ...services.service_manager import ServiceManager

router = APIRouter()

# Global service manager instance
_service_manager: ServiceManager | None = None


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


@router.post("/ml", response_model=Experiment)
async def create_experiment(experiment: ExperimentCreate, service: ExperimentService = Depends(get_experiment_service)):
    """Create a new ML experiment (classification, regression, clustering)."""
    if experiment.config and experiment.config.llm_model:
        if not is_allowed_llm_model(experiment.config.llm_model):
            allowed = ", ".join(sorted(get_allowed_llm_model_ids()))
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported llm_model '{experiment.config.llm_model}'. Allowed: {allowed}",
            )
    if experiment.config and getattr(experiment.config, "prompt_llm_model", None):
        plm = str(getattr(experiment.config, "prompt_llm_model"))
        if plm and not is_allowed_llm_model(plm):
            allowed = ", ".join(sorted(get_allowed_llm_model_ids()))
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported prompt_llm_model '{plm}'. Allowed: {allowed}",
            )
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
    res = await service.start_experiment_forward(experiment_id)
    if not res.ok:
        raise HTTPException(status_code=res.http_status, detail=f"ERROR: {res.detail or 'Failed to start'}")
    return JSONResponse(status_code=res.http_status, content=res.payload or {})


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
        if prompt_experiment.llm_model and not is_allowed_llm_model(prompt_experiment.llm_model):
            allowed = ", ".join(sorted(get_allowed_llm_model_ids()))
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported llm_model '{prompt_experiment.llm_model}'. Allowed: {allowed}",
            )
        if getattr(prompt_experiment, "prompt_llm_model", None):
            plm = str(getattr(prompt_experiment, "prompt_llm_model"))
            if plm and not is_allowed_llm_model(plm):
                allowed = ", ".join(sorted(get_allowed_llm_model_ids()))
                raise HTTPException(
                    status_code=400,
                    detail=f"Unsupported prompt_llm_model '{plm}'. Allowed: {allowed}",
                )

        # Basic prompt placeholder validation
        template_pattern = r"\{([^}]+)\}"
        placeholders = re.findall(template_pattern, prompt_experiment.base_prompt)
        if not placeholders:
            raise HTTPException(
                status_code=400,
                detail="Base prompt must contain at least one template placeholder in {column_name} format",
            )

        # Prepare validation criteria dict
        vc_dict = prompt_experiment.validation_criteria.dict()

        # Build Experiment config and create DB record
        config = ExperimentConfig(
            description=prompt_experiment.description or "",
            parameters={
                "target_column": prompt_experiment.target_column,
                "base_prompt": prompt_experiment.base_prompt,
                "validation_criteria": vc_dict,
            },
            llm_model=prompt_experiment.llm_model,
            prompt_llm_model=getattr(prompt_experiment, "prompt_llm_model", None),
            max_iterations=prompt_experiment.max_iterations,
            dataset_size=prompt_experiment.dataset_size,
            test_size=prompt_experiment.test_size,
        )
        experiment_create = ExperimentCreate(
            name=prompt_experiment.name,
            config=config,
            data_path=prompt_experiment.data_path,
        )

        global _service_manager
        if not _service_manager:
            raise HTTPException(status_code=503, detail="Service manager not initialized")
        db = _service_manager.get_db_service()
        from uuid import uuid4

        experiment_id = f"exp_{uuid4()}"
        await db.create_experiment(experiment_create, experiment_id)

        # Build and upload prompt experiment files to storage so it's ready to start
        try:
            if _service_manager and hasattr(_service_manager, "creation_service"):
                creation = _service_manager.creation_service
                if creation is None:
                    raise Exception("Experiment Creation service is not initialized!")
                prompt_spec: Dict[str, Any] = {
                    "name": prompt_experiment.name,
                    "description": prompt_experiment.description or "",
                    "data_path": prompt_experiment.data_path,
                    "target_column": prompt_experiment.target_column,
                    "base_prompt": prompt_experiment.base_prompt,
                    "validation_criteria": vc_dict,
                    "llm_model": prompt_experiment.llm_model,
                    "prompt_llm_model": getattr(prompt_experiment, "prompt_llm_model", None),
                    "max_iterations": prompt_experiment.max_iterations,
                    "dataset_size": prompt_experiment.dataset_size,
                    "test_size": prompt_experiment.test_size,
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


@router.post("/chains", response_model=Experiment)
async def create_chain_experiment(
    chain_experiment: ChainExperimentCreate,
    service: ExperimentService = Depends(get_experiment_service),
):
    try:
        logger.info(f"Received chain experiment creation request: {chain_experiment.name}")
        if chain_experiment.llm_model and not is_allowed_llm_model(chain_experiment.llm_model):
            allowed = ", ".join(sorted(get_allowed_llm_model_ids()))
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported llm_model '{chain_experiment.llm_model}'. Allowed: {allowed}",
            )

        try:
            chain_config = json.loads(chain_experiment.base_chain_config)
            if "steps" not in chain_config or not isinstance(chain_config["steps"], list):
                raise HTTPException(
                    status_code=400,
                    detail="Chain config must contain a 'steps' array with at least one step",
                )
            if len(chain_config["steps"]) == 0:
                raise HTTPException(
                    status_code=400,
                    detail="Chain config must contain at least one step",
                )
        except json.JSONDecodeError as e:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid JSON in base_chain_config: {str(e)}",
            )

        vc_dict: Dict[str, Any] = {}
        try:
            if getattr(chain_experiment, "validation_criteria", None) is not None:
                vc = chain_experiment.validation_criteria
                vc_dict = vc.dict() if hasattr(vc, "dict") else vc.model_dump()  # type: ignore[attr-defined]
        except Exception:
            vc_dict = {}

        timeout_seconds = getattr(chain_experiment, "timeout_seconds", None)
        if timeout_seconds is None:
            timeout_seconds = max(3600, int(chain_experiment.max_iterations * 90 * 1.3))
        
        config = ExperimentConfig(
            description=chain_experiment.description or "",
            parameters={
                "target_column": chain_experiment.target_column,
                "base_chain_config": chain_experiment.base_chain_config,
                "validation_criteria": vc_dict,
            },
            llm_model=chain_experiment.llm_model,
            max_iterations=chain_experiment.max_iterations,
            timeout_seconds=timeout_seconds,
            dataset_size=chain_experiment.dataset_size,
            test_size=chain_experiment.test_size,
        )
        experiment_create = ExperimentCreate(
            name=chain_experiment.name,
            config=config,
            data_path=chain_experiment.data_path,
        )

        global _service_manager
        if not _service_manager:
            raise HTTPException(status_code=503, detail="Service manager not initialized")
        db = _service_manager.get_db_service()
        from uuid import uuid4

        experiment_id = f"exp_{uuid4()}"
        await db.create_experiment(experiment_create, experiment_id)

        try:
            if _service_manager and hasattr(_service_manager, "creation_service"):
                creation = _service_manager.creation_service
                if creation is None:
                    raise Exception("Experiment Creation service is not initialized!")
                chain_spec: Dict[str, Any] = {
                    "name": chain_experiment.name,
                    "description": chain_experiment.description or "",
                    "data_path": chain_experiment.data_path,
                    "target_column": chain_experiment.target_column,
                    "base_chain_config": chain_experiment.base_chain_config,
                    "validation_criteria": vc_dict,
                    "llm_model": chain_experiment.llm_model,
                    "max_iterations": chain_experiment.max_iterations,
                    "timeout_seconds": timeout_seconds,
                    "dataset_size": chain_experiment.dataset_size,
                    "test_size": chain_experiment.test_size,
                    "evolution_mode": getattr(chain_experiment, "evolution_mode", "full_chain") or "full_chain",
                    "step_number": getattr(chain_experiment, "step_number", None),
                }
                await creation.create_chain_experiment_files(experiment_id, chain_spec, chain_experiment.data_path)
        except Exception as e:
            logger.error(f"Failed to prepare chain experiment files for {experiment_id}: {e}")

        created = await service.get_experiment(experiment_id)
        if not created:
            raise HTTPException(status_code=500, detail="Experiment created but not found")
        return created

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating chain experiment: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to create chain experiment: {str(e)}")


@router.delete("/drop-all", response_model=Dict[str, Any])
async def drop_all_experiments(service: ExperimentService = Depends(get_experiment_service)):
    """Drop all experiments and their data"""
    try:
        result = await service.drop_all_experiments()
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to drop all experiments: {str(e)}")
