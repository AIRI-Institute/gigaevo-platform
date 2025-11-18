#!/usr/bin/env python3

from typing import Any, Dict

from fastapi import APIRouter, Depends

from ...services.service_manager import ServiceManager
from ...services.status_service import StatusService

router = APIRouter()

# Global service manager instance
_service_manager: ServiceManager = None


def set_service_manager(service_manager: ServiceManager):
    """Set service manager instance"""
    global _service_manager
    _service_manager = service_manager


def get_status_service() -> StatusService:
    """Get status service instance from service manager"""
    global _service_manager
    if not _service_manager:
        raise RuntimeError("Service manager not initialized")
    return _service_manager.get_status_service()


@router.get("/health", response_model=Dict[str, Any])
async def health_check(service: StatusService = Depends(get_status_service)):
    """Overall system health check"""
    return await service.get_system_health()


@router.get("/experiments", response_model=Dict[str, Any])
async def get_experiments_status(service: StatusService = Depends(get_status_service)):
    """Get overall experiments status"""
    return await service.get_experiments_status()


@router.get("/runners", response_model=Dict[str, Any])
async def get_runners_status(service: StatusService = Depends(get_status_service)):
    """Get all runners status"""
    return await service.get_runners_status()


@router.get("/metrics", response_model=Dict[str, Any])
async def get_system_metrics(service: StatusService = Depends(get_status_service)):
    """Get system performance metrics"""
    return await service.get_system_metrics()


@router.get("/storage", response_model=Dict[str, Any])
async def get_storage_config(service: StatusService = Depends(get_status_service)):
    """Expose minimal storage configuration needed by Web UI (e.g., bucket name)."""
    return await service.get_storage_info()
