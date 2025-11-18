#!/usr/bin/env python3

import json
import os
from typing import Dict, List, Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ...services.service_manager import ServiceManager

router = APIRouter()

_service_manager: Optional[ServiceManager] = None


def set_service_manager(service_manager: ServiceManager):
    global _service_manager
    _service_manager = service_manager


def _examples_dir() -> str:
    # repo_root/master_api/data_examples
    here = os.path.dirname(__file__)
    return os.path.normpath(os.path.join(here, "..", "..", "..", "data_examples"))


def _humanize(name: str) -> str:
    return name.replace("_", " ").strip().title()


@router.get("/api/v1/examples/")
async def list_examples():
    """List available example specs in data_examples directory."""
    base = _examples_dir()
    try:
        entries: List[Dict[str, str]] = []
        for fname in os.listdir(base):
            if fname.endswith("_spec.json"):
                name = fname[: -len("_spec.json")]
                entries.append({"name": name, "label": _humanize(name)})
        # stable order
        entries.sort(key=lambda x: x["label"])
        return {"examples": entries}
    except Exception as e:
        return JSONResponse({"error": f"failed_to_list_examples: {e}"}, status_code=500)


@router.get("/api/v1/examples/{name}")
async def get_example_spec(name: str):
    """Return spec JSON for an example along with detected dataset filename."""
    base = _examples_dir()
    spec_path = os.path.join(base, f"{name}_spec.json")
    if not os.path.exists(spec_path):
        return JSONResponse({"error": "not_found"}, status_code=404)
    try:
        with open(spec_path, "r", encoding="utf-8") as f:
            spec = json.load(f)
        # Determine dataset filename from spec.dataset_path basename
        dataset_rel = spec.get("dataset_path", "")
        dataset_basename = os.path.basename(dataset_rel) if dataset_rel else ""
        return {
            "name": name,
            "label": _humanize(name),
            "spec": spec,
            "dataset_filename": dataset_basename,
        }
    except Exception as e:
        return JSONResponse({"error": f"failed_to_read_spec: {e}"}, status_code=500)


@router.post("/api/v1/examples/{name}/upload")
async def upload_example_dataset(name: str):
    """Upload the example dataset CSV to storage and return data_path."""
    if not _service_manager:
        return JSONResponse({"error": "service_manager_not_initialized"}, status_code=503)
    base = _examples_dir()
    spec_path = os.path.join(base, f"{name}_spec.json")
    if not os.path.exists(spec_path):
        return JSONResponse({"error": "not_found"}, status_code=404)
    try:
        with open(spec_path, "r", encoding="utf-8") as f:
            spec = json.load(f)
        dataset_rel = spec.get("dataset_path", "")
        dataset_basename = os.path.basename(dataset_rel) if dataset_rel else ""
        if not dataset_basename:
            return JSONResponse({"error": "dataset_not_specified"}, status_code=400)
        # Dataset expected to be present in data_examples directory
        csv_path = os.path.join(base, dataset_basename)
        if not os.path.exists(csv_path):
            return JSONResponse({"error": "dataset_file_missing"}, status_code=404)
        storage = _service_manager.get_storage_service()
        # Reuse data/ prefix via upload_experiment_data
        object_name = f"data/{dataset_basename}"
        ok = await storage.upload_file(csv_path, object_name, metadata={"source": "example", "example_name": name})
        if not ok:
            return JSONResponse({"error": "upload_failed"}, status_code=500)
        return {"data_path": object_name, "filename": dataset_basename}
    except Exception as e:
        return JSONResponse({"error": f"failed_to_upload_dataset: {e}"}, status_code=500)
