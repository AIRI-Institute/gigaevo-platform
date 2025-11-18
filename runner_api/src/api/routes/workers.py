#!/usr/bin/env python3

from typing import List

from fastapi import APIRouter, Depends, HTTPException

from ...models.worker import Worker, WorkerUpdate
from ...services.worker_service import WorkerService

router = APIRouter()


def get_worker_service() -> WorkerService:
    # TODO: Implement dependency injection
    return WorkerService()


@router.get("/", response_model=List[Worker])
async def list_workers(service: WorkerService = Depends(get_worker_service)):
    """List all workers"""
    return await service.list_workers()


@router.get("/{worker_id}", response_model=Worker)
async def get_worker(worker_id: str, service: WorkerService = Depends(get_worker_service)):
    """Get worker by ID"""
    worker = await service.get_worker(worker_id)
    if not worker:
        raise HTTPException(status_code=404, detail="Worker not found")
    return worker


@router.post("/{worker_id}/heartbeat")
async def worker_heartbeat(worker_id: str, service: WorkerService = Depends(get_worker_service)):
    """Update worker heartbeat"""
    success = await service.update_heartbeat(worker_id)
    if not success:
        raise HTTPException(status_code=404, detail="Worker not found")
    return {"message": "Heartbeat updated"}


@router.put("/{worker_id}", response_model=Worker)
async def update_worker(
    worker_id: str, worker_update: WorkerUpdate, service: WorkerService = Depends(get_worker_service)
):
    """Update worker status"""
    worker = await service.update_worker(worker_id, worker_update)
    if not worker:
        raise HTTPException(status_code=404, detail="Worker not found")
    return worker


@router.post("/{worker_id}/stop")
async def stop_worker(worker_id: str, service: WorkerService = Depends(get_worker_service)):
    """Stop a worker"""
    success = await service.stop_worker(worker_id)
    if not success:
        raise HTTPException(status_code=400, detail="Cannot stop worker")
    return {"message": "Worker stopped"}
