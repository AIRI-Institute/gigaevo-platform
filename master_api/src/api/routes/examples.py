#!/usr/bin/env python3

import csv
import json
import os
import re
from typing import Dict, List, Optional
from uuid import uuid4

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ...models.experiment import (
    Experiment,
    ExperimentConfig,
    ExperimentCreate,
    PromptExperimentCreate,
    PromptValidationCriteria,
)
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


def _prompt_examples_dir() -> str:
    # repo_root/master_api/prompt_examples
    here = os.path.dirname(__file__)
    return os.path.normpath(os.path.join(here, "..", "..", "..", "prompt_examples"))


def _humanize(name: str) -> str:
    return name.replace("_", " ").strip().title()


def _local_prompt_examples_dir() -> str:
    # repo_root/master_api/prompt_examples
    here = os.path.dirname(__file__)
    return os.path.normpath(os.path.join(here, "..", "..", "..", "prompt_examples"))


def _extract_prompt_template(baseline_path: str) -> str:
    """Extract PROMPT_TEMPLATE content from initial_programs/baseline.py."""
    try:
        with open(baseline_path, "r", encoding="utf-8") as f:
            content = f.read()
        # Match PROMPT_TEMPLATE = """...""" or '''...''' or "..." or '...'
        pattern = re.compile(
            r"PROMPT_TEMPLATE\s*=\s*(?P<q>\"\"\"|'''|\"|')(?P<body>.*?)(?P=q)",
            re.DOTALL,
        )
        m = pattern.search(content)
        if m:
            return m.group("body")
    except Exception:
        pass
    return ""


def _extract_prompt_template_from_sh(create_sh_path: str) -> str:
    """
    Extract PROMPT_TEMPLATE content from a shell script that defines it via heredoc, e.g.:
      export PROMPT_TEMPLATE="$(cat << 'EOF'\n...\nEOF\n)"
    Returns empty string if not found.
    """
    try:
        if not os.path.exists(create_sh_path):
            return ""
        with open(create_sh_path, "r", encoding="utf-8") as f:
            content = f.read()

        # Match export PROMPT_TEMPLATE="...(heredoc)..."
        # Capture content between EOF markers in common variants
        pattern = re.compile(
            r"export\s+PROMPT_TEMPLATE\s*=\s*\"\$\(\s*cat\s*<<\s*['\"]?EOF['\"]?\s*\n(?P<body>[\s\S]*?)\nEOF\s*\)\"",
            re.MULTILINE,
        )
        m = pattern.search(content)
        if m:
            return m.group("body").strip("\n")
        # Fallback: simple inline assignment export PROMPT_TEMPLATE='...'
        pattern_inline = re.compile(
            r"export\s+PROMPT_TEMPLATE\s*=\s*(?P<q>\"\"\"|'''|\"|')(?P<body>.*?)(?P=q)",
            re.DOTALL,
        )
        m2 = pattern_inline.search(content)
        if m2:
            return m2.group("body")
    except Exception:
        pass
    return ""


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


@router.get("/api/v1/prompt-examples/")
async def list_prompt_examples():
    """List available prompt evolution examples from master_api/prompt_examples."""
    base = _prompt_examples_dir()
    try:
        items: List[Dict[str, str]] = []
        if not os.path.isdir(base):
            return JSONResponse({"error": "prompt_examples_dir_missing", "path": base}, status_code=404)
        for name in sorted(os.listdir(base)):
            path = os.path.join(base, name)
            if os.path.isdir(path):
                items.append({"name": name, "label": name.replace("_", " ").title()})
        return {"examples": items}
    except Exception as e:
        return JSONResponse({"error": f"failed_to_list_prompt_examples: {e}"}, status_code=500)


def _parse_create_sh(example_dir: str) -> Dict[str, str]:
    """Parse create.sh to extract dataset path, target field, task description, and regexp pattern if present."""
    result = {
        "dataset_path": "",
        "target_field": "target",
        "task_description": "",
        "task_type": "",
        "regexp_pattern": "",
    }
    create_path = os.path.join(example_dir, "create.sh")
    if not os.path.exists(create_path):
        return result
    try:
        with open(create_path, "r", encoding="utf-8") as f:
            content = f.read()
        m_ds = re.search(r"--dataset-path\s+([^\s\\]+)", content)
        if m_ds:
            result["dataset_path"] = m_ds.group(1).strip()
        m_tf = re.search(r"--target-field\s+([^\s\\]+)", content)
        if m_tf:
            result["target_field"] = m_tf.group(1).strip()
        m_td = re.search(r'--task-description\s+"([^"]+)"', content)
        if m_td:
            result["task_description"] = m_td.group(1).strip()
        m_tt = re.search(r"--task-type\s+([^\s\\]+)", content)
        if m_tt:
            result["task_type"] = m_tt.group(1).strip()
        m_rp = re.search(r'--regexp-pattern\s+"([^"]+)"', content)
        if m_rp:
            result["regexp_pattern"] = m_rp.group(1).strip()
    except Exception:
        pass
    return result


@router.get("/api/v1/prompt-examples/{name}")
async def get_prompt_example_details(name: str):
    """Return details for a prompt example: dataset file, target column, and optional task description."""
    base = _prompt_examples_dir()
    example_dir = os.path.join(base, name)
    if not os.path.isdir(example_dir):
        return JSONResponse({"error": "not_found"}, status_code=404)
    try:
        parsed = _parse_create_sh(example_dir)
        dataset_dir = os.path.join(example_dir, "dataset")
        train_csv = os.path.join(dataset_dir, "train.csv")
        dataset_filename = os.path.basename(train_csv) if os.path.exists(train_csv) else ""

        # Baseline prompt: prefer PROMPT_TEMPLATE from initial_programs/baseline.py; fallback to CSV-derived prompt
        baseline_prompt = ""
        try:
            baseline_path = os.path.join(example_dir, "initial_programs", "baseline.py")
            if os.path.exists(baseline_path):
                baseline_from_file = _extract_prompt_template(baseline_path)
                if baseline_from_file:
                    baseline_prompt = baseline_from_file
        except Exception:
            pass

        target_field = parsed.get("target_field") or "target"
        if (not baseline_prompt) and os.path.exists(train_csv):
            try:
                with open(train_csv, "r", encoding="utf-8") as f:
                    reader = csv.reader(f)
                    headers = next(reader, [])
                feature_cols = [h for h in headers if h and h != target_field]
                features_text = "\n".join([f"- {{{col}}}" for col in feature_cols])
                task_desc = (
                    parsed.get("task_description") or "Analyze features and predict the target based on context."
                )
                baseline_prompt = (
                    f"{task_desc}\n\n"
                    f"Available Features:\n{features_text}\n\n"
                    f"Target Variable: {{{target_field}}}\n\n"
                    f"Answer:"
                )
            except Exception:
                baseline_prompt = ""

        # Suggest validation defaults based on task_type
        task_type = (parsed.get("task_type") or "").lower()
        validation_defaults: Dict[str, Optional[str]] = {}
        if task_type in {"classification", "multi_choice", "math"}:
            validation_defaults = {
                "validation_type": "Binary (0/1)",
                "binary_method": "equality",
                "continuous_metric": None,
            }
        elif task_type in {"summarization"}:
            validation_defaults = {
                "validation_type": "Continuous (0..1)",
                "binary_method": None,
                "continuous_metric": "ROUGE-L",
            }
        else:
            validation_defaults = {"validation_type": None, "binary_method": None, "continuous_metric": None}

        return {
            "name": name,
            "label": name.replace("_", " ").title(),
            "dataset_filename": dataset_filename,
            "target_field": target_field,
            "task_description": parsed.get("task_description") or "",
            "task_type": parsed.get("task_type") or "",
            "baseline_prompt": baseline_prompt,
            "validation_defaults": validation_defaults,
        }
    except Exception as e:
        return JSONResponse({"error": f"failed_to_read_prompt_example: {e}"}, status_code=500)


@router.post("/api/v1/prompt-examples/{name}/create")
async def create_prompt_experiment_from_prompt_example(name: str):
    """
    Create a prompt-based experiment from master_api/prompt_examples using internal builder:
    - Upload example dataset train.csv to storage
    - Prefer baseline prompt from initial_programs/baseline.py PROMPT_TEMPLATE; fallback to CSV headers
    - Create DB experiment and build/upload prompt folder
    """
    if not _service_manager:
        return JSONResponse({"error": "service_manager_not_initialized"}, status_code=503)

    base = _prompt_examples_dir()
    example_dir = os.path.join(base, name)
    if not os.path.isdir(example_dir):
        return JSONResponse({"error": "not_found"}, status_code=404)

    try:
        parsed = _parse_create_sh(example_dir)
        target_field = parsed.get("target_field") or "target"
        task_desc = parsed.get("task_description") or ""

        # Locate train.csv
        train_csv = os.path.join(example_dir, "dataset", "train.csv")
        if not os.path.exists(train_csv):
            return JSONResponse({"error": "train_csv_missing"}, status_code=404)

        # Baseline prompt: prefer PROMPT_TEMPLATE from initial_programs/baseline.py; fallback to CSV-derived prompt
        base_prompt = ""
        try:
            baseline_path = os.path.join(example_dir, "initial_programs", "baseline.py")
            if os.path.exists(baseline_path):
                baseline_from_file = _extract_prompt_template(baseline_path)
                if baseline_from_file:
                    base_prompt = baseline_from_file
        except Exception:
            pass
        if not base_prompt:
            with open(train_csv, "r", encoding="utf-8") as f:
                reader = csv.reader(f)
                headers = next(reader, [])
            feature_cols = [h for h in headers if h and h != target_field]
            features_text = "\n".join([f"- {{{col}}}" for col in feature_cols])
            base_prompt = (
                f"{task_desc or 'Analyze features and predict the target based on context.'}\n\n"
                f"Available Features:\n{features_text}\n\n"
                f"Target Variable: {{{target_field}}}\n\n"
                f"Answer:"
            )

        # Upload dataset to storage under data/
        storage = _service_manager.get_storage_service()
        object_name = f"prompt_data/{name}/train.csv"
        exists = False
        try:
            exists = await storage.object_exists(object_name)
        except Exception:
            exists = False
        if not exists:
            ok = await storage.upload_file(train_csv, object_name, metadata={"source": "prompt_example", "name": name})
            if not ok:
                return JSONResponse({"error": "upload_failed"}, status_code=500)

        # Prepare PromptExperimentCreate-like structure
        pec = PromptExperimentCreate(
            name=name.replace("_", " ").title(),
            description=task_desc,
            data_path=object_name,
            target_column=target_field,
            base_prompt=base_prompt,
            validation_criteria=PromptValidationCriteria(
                validation_type="Binary (0/1)",
                binary_method="equality",
                regexp_pattern=None,
                continuous_metric=None,
            ),
            llm_model="local-inference",
            prompt_llm_model=None,
            max_iterations=100,
        )

        # Create DB experiment
        db = _service_manager.get_db_service()
        exp_service = _service_manager.get_experiment_service()
        creation = _service_manager.creation_service
        if creation is None:
            raise Exception("Experiment Creation service is not initialized!")

        config = ExperimentConfig(
            description=pec.description or "",
            parameters={
                "task_type": (parsed.get("task_type") or "classification"),
                "target_column": pec.target_column,
                "base_prompt": pec.base_prompt,
                "validation_criteria": pec.validation_criteria.model_dump(),
            },
            llm_model=pec.llm_model,
            prompt_llm_model=pec.prompt_llm_model,
            max_iterations=pec.max_iterations,
        )
        exp_create = ExperimentCreate(name=pec.name, config=config, data_path=pec.data_path)

        experiment_id = f"exp_{uuid4()}"
        await db.create_experiment(exp_create, experiment_id)

        # Build and upload folder
        prompt_spec = {
            "name": pec.name,
            "description": pec.description or "",
            "data_path": pec.data_path,
            "target_column": pec.target_column,
            "base_prompt": pec.base_prompt,
            "validation_criteria": pec.validation_criteria.model_dump(),
            "llm_model": pec.llm_model,
            "max_iterations": pec.max_iterations,
        }
        storage_base = await creation.create_prompt_experiment_files(experiment_id, prompt_spec, pec.data_path)
        if not storage_base:
            return JSONResponse({"error": "prompt_folder_build_failed"}, status_code=500)

        experiment: Optional[Experiment] = await exp_service.get_experiment(experiment_id)
        if not experiment:
            return JSONResponse({"error": "experiment_created_but_not_found"}, status_code=500)
        return experiment.model_dump()

    except Exception as e:
        return JSONResponse({"error": f"failed_to_create_prompt_experiment_from_example: {e}"}, status_code=500)


#
# Local prompt presets under master_api/prompt_examples
#
@router.get("/api/v1/prompt-presets/local/")
async def list_local_prompt_presets():
    """List local prompt presets under master_api/prompt_examples."""
    base = _local_prompt_examples_dir()
    try:
        items: List[Dict[str, str]] = []
        if not os.path.isdir(base):
            return {"examples": []}
        for name in sorted(os.listdir(base)):
            path = os.path.join(base, name)
            if os.path.isdir(path):
                items.append({"name": name, "label": _humanize(name)})
        return {"examples": items}
    except Exception as e:
        return JSONResponse({"error": f"failed_to_list_local_prompt_presets: {e}"}, status_code=500)


@router.get("/api/v1/prompt-presets/local/{name}")
async def get_local_prompt_preset(name: str):
    """Return baseline prompt, dataset filenames, and .sh-derived fields for a local preset."""
    base = _local_prompt_examples_dir()
    example_dir = os.path.join(base, name)
    if not os.path.isdir(example_dir):
        return JSONResponse({"error": "not_found"}, status_code=404)
    try:
        # Parse create.sh for task settings using shared helper
        parsed = _parse_create_sh(example_dir)
        task_type = parsed.get("task_type") or ""
        target_field = parsed.get("target_field") or "target"
        task_description = parsed.get("task_description") or ""
        regexp_pattern = parsed.get("regexp_pattern") or ""

        baseline_path = os.path.join(example_dir, "initial_programs", "baseline.py")
        baseline_prompt = _extract_prompt_template(baseline_path) if os.path.exists(baseline_path) else ""
        if not baseline_prompt:
            # Fallback to PROMPT_TEMPLATE defined in create.sh (heredoc)
            create_sh = os.path.join(example_dir, "create.sh")
            baseline_prompt = _extract_prompt_template_from_sh(create_sh) or ""
        dataset_dir = os.path.join(example_dir, "dataset")
        train_csv = os.path.join(dataset_dir, "train.csv")
        val_csv = os.path.join(dataset_dir, "val.csv")
        # Suggest validation defaults based on task_type
        tt_lower = task_type.lower()
        validation_defaults: Dict[str, Optional[str]] = {}
        if tt_lower in {"classification", "multi_choice", "math"}:
            validation_defaults = {
                "validation_type": "Binary (0/1)",
                "binary_method": "equality",
                "continuous_metric": None,
            }
        elif tt_lower in {"summarization"}:
            validation_defaults = {
                "validation_type": "Continuous (0..1)",
                "binary_method": None,
                "continuous_metric": "ROUGE-L",
            }
        else:
            validation_defaults = {"validation_type": None, "binary_method": None, "continuous_metric": None}
        return {
            "name": name,
            "label": _humanize(name),
            "baseline_prompt": baseline_prompt,
            "dataset": {
                "train": os.path.basename(train_csv) if os.path.exists(train_csv) else None,
                "val": os.path.basename(val_csv) if os.path.exists(val_csv) else None,
            },
            "task_type": task_type,
            "target_field": target_field,
            "task_description": task_description,
            "regexp_pattern": regexp_pattern,
            "validation_defaults": validation_defaults,
        }
    except Exception as e:
        return JSONResponse({"error": f"failed_to_get_local_prompt_preset: {e}"}, status_code=500)


@router.post("/api/v1/prompt-presets/local/{name}/create")
async def create_prompt_experiment_from_local_preset(name: str):
    """
    Create a prompt-based experiment from a local preset under master_api/prompt_examples/{name}.
    Steps:
    - Parse create.sh to get task-type, target-field, task-description
    - Read baseline prompt from initial_programs/baseline.py (if present)
    - Upload dataset train.csv to MinIO under data/{name}_train.csv (if not uploaded yet)
    - Create DB record and build/upload prompt folder to MinIO
    """
    if not _service_manager:
        return JSONResponse({"error": "service_manager_not_initialized"}, status_code=503)

    base = _local_prompt_examples_dir()
    example_dir = os.path.join(base, name)
    if not os.path.isdir(example_dir):
        return JSONResponse({"error": "not_found"}, status_code=404)

    try:
        # Parse create.sh using shared helper
        parsed = _parse_create_sh(example_dir)
        task_type = parsed.get("task_type") or ""
        target_field = parsed.get("target_field") or "target"
        task_description = parsed.get("task_description") or ""
        regexp_pattern = parsed.get("regexp_pattern") or ""

        # Baseline prompt from initial_programs/baseline.py (optional), fallback to create.sh PROMPT_TEMPLATE
        baseline_path = os.path.join(example_dir, "initial_programs", "baseline.py")
        create_sh = os.path.join(example_dir, "create.sh")
        base_prompt = _extract_prompt_template(baseline_path) if os.path.exists(baseline_path) else ""
        if not base_prompt:
            base_prompt = _extract_prompt_template_from_sh(create_sh) or ""

        # Dataset path
        dataset_dir = os.path.join(example_dir, "dataset")
        train_csv = os.path.join(dataset_dir, "train.csv")
        if not os.path.exists(train_csv):
            return JSONResponse({"error": "train_csv_missing"}, status_code=404)

        # Upload dataset to MinIO
        storage = _service_manager.get_storage_service()
        object_name = f"prompt_data/{name}/train.csv"
        exists = False
        try:
            exists = await storage.object_exists(object_name)
        except Exception:
            exists = False
        if not exists:
            ok = await storage.upload_file(
                train_csv, object_name, metadata={"source": "local_prompt_preset", "example_name": name}
            )
            if not ok:
                return JSONResponse({"error": "upload_failed"}, status_code=500)

        # Prepare payloads and create DB record
        exp_service = _service_manager.get_experiment_service()
        if exp_service is None:
            raise Exception("Experiment service is not initialized!")
        creation = _service_manager.creation_service
        if creation is None:
            raise Exception("Experiment Creation service is not initialized!")

        human_name = name.replace("_", " ").title()
        config = ExperimentConfig(
            description=task_description,
            parameters={
                "task_type": (task_type or "classification"),
                "target_column": target_field,
                "base_prompt": base_prompt,
                "validation_criteria": {
                    "validation_type": "Binary (0/1)"
                    if task_type.lower() in {"classification", "multi_choice", "math"}
                    else "Continuous (0..1)",
                    "binary_method": "regexp"
                    if regexp_pattern
                    else "equality"
                    if task_type.lower() in {"classification", "multi_choice", "math"}
                    else None,
                    "regexp_pattern": regexp_pattern or None,
                    "continuous_metric": "ROUGE-L" if task_type.lower() in {"summarization"} else None,
                },
            },
            llm_model="local-inference",
            prompt_llm_model=None,
            max_iterations=100,
        )
        exp_create = ExperimentCreate(name=human_name, config=config, data_path=object_name)

        # Create experiment in DB (use service to keep behavior consistent)
        experiment = await exp_service.create_experiment(exp_create)

        # Build and upload prompt experiment folder, update DB to prepared
        prompt_spec = {
            "name": human_name,
            "description": task_description,
            "data_path": object_name,
            "target_column": target_field,
            "base_prompt": base_prompt,
            "validation_criteria": config.parameters["validation_criteria"],
            "llm_model": "local-inference",
            "max_iterations": 100,
        }
        storage_base = await creation.create_prompt_experiment_files(str(experiment.id), prompt_spec, object_name)
        if not storage_base:
            return JSONResponse({"error": "prompt_folder_build_failed"}, status_code=500)

        # Return full experiment JSON
        return experiment.model_dump()

    except Exception as e:
        return JSONResponse({"error": f"failed_to_create_local_prompt_preset_experiment: {e}"}, status_code=500)


@router.post("/api/v1/prompt-presets/local/{name}/upload")
async def upload_local_prompt_preset_dataset(name: str):
    """
    Upload preset train.csv of a local prompt preset to storage and return data_path.
    """
    if not _service_manager:
        return JSONResponse({"error": "service_manager_not_initialized"}, status_code=503)
    base = _local_prompt_examples_dir()
    example_dir = os.path.join(base, name)
    if not os.path.isdir(example_dir):
        return JSONResponse({"error": "not_found"}, status_code=404)
    try:
        dataset_dir = os.path.join(example_dir, "dataset")
        train_csv = os.path.join(dataset_dir, "train.csv")
        if not os.path.exists(train_csv):
            return JSONResponse({"error": "train_csv_missing"}, status_code=404)
        storage = _service_manager.get_storage_service()
        object_name = f"prompt_data/{name}/train.csv"
        exists = await storage.object_exists(object_name)
        if not exists:
            ok = await storage.upload_file(
                train_csv, object_name, metadata={"source": "local_prompt_preset", "example_name": name}
            )
            if not ok:
                return JSONResponse({"error": "upload_failed"}, status_code=500)
        return {"data_path": object_name, "filename": os.path.basename(train_csv)}
    except Exception as e:
        return JSONResponse({"error": f"failed_to_upload_local_prompt_preset: {e}"}, status_code=500)


@router.post("/api/v1/prompt-examples/build-all")
async def build_all_prompt_examples():
    """
    Build prompt experiments for all examples in master_api/prompt_examples using the internal builder,
    create DB records, upload to storage, and return summary.
    """
    if not _service_manager:
        return JSONResponse({"error": "service_manager_not_initialized"}, status_code=503)

    base = _prompt_examples_dir()
    if not os.path.isdir(base):
        return JSONResponse({"error": "prompt_examples_dir_missing", "path": base}, status_code=404)

    try:
        db = _service_manager.get_db_service()
        creation = _service_manager.creation_service
        if creation is None:
            raise Exception("Experiment Creation service is not initialized!")

        results: List[Dict[str, str]] = []

        for name in sorted(os.listdir(base)):
            example_dir = os.path.join(base, name)
            if not os.path.isdir(example_dir):
                continue

            parsed = _parse_create_sh(example_dir)
            target_field = parsed.get("target_field") or "target"
            task_desc = parsed.get("task_description") or f"Prompt evolution for {name}"
            task_type = parsed.get("task_type") or "classification"

            # Local dataset path inside master_api/prompt_examples
            train_csv = os.path.join(example_dir, "dataset", "train.csv")
            if not os.path.exists(train_csv):
                continue

            # Create DB experiment
            human_name = name.replace("_", " ").title()
            config = ExperimentConfig(
                description=task_desc,
                parameters={"task_type": "prompt", "target_column": target_field},
                llm_model="local-inference",
                prompt_llm_model=None,
                max_iterations=100,
            )
            exp_create = ExperimentCreate(name=human_name, config=config, data_path="")
            experiment_id = f"exp_{uuid4()}"
            await db.create_experiment(exp_create, experiment_id)

            # Use internal builder to create prompt experiment
            # Build prompt spec from example data
            prompt_spec = {
                "task_name": name,
                "task_type": task_type,
                "task_description": task_desc,
                "target_field": target_field,
            }
            storage_base = await creation.create_prompt_experiment_files(
                experiment_id,
                prompt_spec=prompt_spec,
                data_path=train_csv,
            )

            if storage_base:
                results.append({"name": human_name, "id": experiment_id, "storage": storage_base})
            else:
                # Mark failed
                await db.update_experiment_status(experiment_id, "failed", error_message="wizard_failed")
                results.append({"name": human_name, "id": experiment_id, "storage": ""})

        return {"created": results}
    except Exception as e:
        return JSONResponse({"error": f"failed_to_build_all_prompt_examples: {e}"}, status_code=500)
