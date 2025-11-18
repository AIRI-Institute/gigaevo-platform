#!/usr/bin/env python3

import asyncio
import json
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from loguru import logger

from ..config import load_config
from ..folder_constructor.uuid_experiment_builder import build_uuid_experiment
from ..services.database_service import DatabaseService
from ..services.storage_service import StorageService


class ExperimentCreationService:
    """Service for creating experiment files and uploading to storage"""

    def __init__(self, db_service: DatabaseService, storage_service: StorageService, config=None):
        self.config = config or load_config()
        self.db_service = db_service
        self.storage_service = storage_service
        self.temp_dir = tempfile.gettempdir()

    async def create_experiment_files(
        self, experiment_id: str, experiment_config: Dict[str, Any], data_path: Optional[str] = None
    ) -> Optional[str]:
        """
        Create experiment files using folder_constructor and upload to MinIO.

        Args:
            experiment_id: UUID of the experiment
            experiment_config: Configuration dictionary for the experiment
            data_path: Path to data file in MinIO (optional)

        Returns:
            MinIO storage path for the uploaded experiment files, or None if failed
        """
        try:
            logger.info(f"Creating experiment files for {experiment_id}")

            # Create temporary working directory
            temp_work_dir = os.path.join(self.temp_dir, f"geml_experiment_{experiment_id}")
            os.makedirs(temp_work_dir, exist_ok=True)

            # Create spec JSON for folder_constructor
            spec_json = self._create_spec_json(experiment_config, data_path)
            spec_path = os.path.join(temp_work_dir, "experiment_spec.json")

            with open(spec_path, "w") as f:
                json.dump(spec_json, f, indent=2)

            # Get or download dataset
            dataset_path = await self._prepare_dataset(spec_json, temp_work_dir)
            if not dataset_path:
                logger.error(f"Failed to prepare dataset for experiment {experiment_id}")
                return None

            # Use folder_constructor to create experiment files
            output_dir = await self._run_folder_constructor(spec_path, dataset_path)

            if not output_dir or not os.path.exists(output_dir):
                logger.error(f"Folder constructor failed for experiment {experiment_id}")
                return None

            # Upload experiment files to MinIO
            storage_path = await self.storage_service.upload_experiment_files(output_dir, experiment_id)

            if storage_path:
                logger.info(f"Successfully created and uploaded experiment files for {experiment_id}: {storage_path}")

                # Update experiment in database - add storage path to config
                experiment = await self.db_service.get_experiment(experiment_id)
                if experiment:
                    updated_config = experiment.config.copy()
                    updated_config["experiment_files_path"] = storage_path
                    await self.db_service.update_experiment(
                        experiment_id,
                        config=updated_config,
                        status="preparing",  # Update status to indicate files are ready
                    )

                return storage_path
            else:
                logger.error(f"Failed to upload experiment files for {experiment_id}")
                # Persist FAILED status explicitly if upload did not return a storage path
                try:
                    await self.db_service.update_experiment_status(
                        experiment_id, "failed", error_message="Failed to upload experiment files"
                    )
                except Exception as db_err:
                    logger.error(f"Failed to set experiment {experiment_id} status to failed: {db_err}")
                return None

        except Exception as e:
            logger.error(f"Error creating experiment files for {experiment_id}: {e}")
            # Ensure the FAILED status and error are persisted for visibility in DB/UI
            try:
                await self.db_service.update_experiment_status(experiment_id, "failed", error_message=str(e))
            except Exception as db_err:
                logger.error(f"Failed to set experiment {experiment_id} status to failed: {db_err}")
            return None

        finally:
            # Cleanup temporary directory
            try:
                if "temp_work_dir" in locals() and os.path.exists(temp_work_dir):  # type: ignore
                    import shutil

                    shutil.rmtree(temp_work_dir, ignore_errors=True)  # type: ignore
            except Exception as e:
                logger.warning(f"Failed to cleanup temp directory {temp_work_dir}: {e}")  # type: ignore

    def _create_spec_json(self, experiment_config: Dict[str, Any], data_path: Optional[str] = None) -> Dict[str, Any]:
        """Create spec JSON for folder_constructor from experiment configuration"""

        # Extract parameters from experiment config (support both top-level and nested under 'parameters')
        params = experiment_config.get("parameters", {}) or {}
        task_type = experiment_config.get("task_type") or params.get("task_type") or "classification"
        description = experiment_config.get("description") or params.get("task_description") or ""
        target_column = experiment_config.get("target_column") or params.get("target_column") or "target"

        # Create spec JSON structure that matches build_uuid_experiment expectations
        spec_json = {
            "task_type": task_type,
            "task_description": description,
            "target_field": target_column,
            "dataset_path": data_path or "data.csv",
            # Additional fields for our own use
            "llm_model": experiment_config.get("llm_model", "local-inference"),
            "max_iterations": experiment_config.get("max_iterations", 100),
            "timeout_seconds": experiment_config.get("timeout_seconds", 3600),
            "parameters": experiment_config.get("parameters", {}),
        }

        # Add n_clusters for clustering tasks
        if task_type == "clustering":
            spec_json["n_clusters"] = 3  # Default value

        return spec_json

    async def _prepare_dataset(self, spec_json: Dict[str, Any], work_dir: str) -> Optional[str]:
        """Prepare dataset for experiment creation"""
        dataset_path = spec_json.get("dataset_path")

        if not dataset_path:
            logger.warning("No dataset path specified in experiment config")
            return None

        # If dataset_path is already a local file path
        if os.path.exists(dataset_path):
            return dataset_path

        # If dataset_path is a MinIO path, download it
        if dataset_path.startswith(("data/", "experiments/")):
            local_path = os.path.join(work_dir, os.path.basename(dataset_path))
            success = await self.storage_service.download_file(dataset_path, local_path)
            if success:
                return local_path
            else:
                logger.error(f"Failed to download dataset from MinIO: {dataset_path}")
                return None

        # Try to find a matching CSV file in work_dir
        csv_files = list(Path(work_dir).glob("*.csv"))
        if csv_files:
            logger.info(f"Using CSV file found in work directory: {csv_files[0]}")
            return str(csv_files[0])

        logger.error(f"Could not find dataset file: {dataset_path}")
        return None

    async def _run_folder_constructor(self, spec_path: str, dataset_path: str) -> Optional[str]:
        """Run the folder_constructor to create experiment files"""
        try:
            # Import the UUID experiment builder function

            # Create output directory
            output_root = os.path.join(self.temp_dir, f"geml_output_{uuid.uuid4().hex[:8]}")
            os.makedirs(output_root, exist_ok=True)

            # Load spec JSON
            with open(spec_path, "r") as f:
                spec = json.load(f)

            # Get template base directory
            template_base = os.path.join(os.path.dirname(__file__), "..", "folder_constructor", "validate_templates")

            # Run the builder
            exp_dir = build_uuid_experiment(
                spec=spec, output_root=output_root, template_base=template_base, dataset_path=dataset_path
            )

            if exp_dir and os.path.exists(exp_dir):
                logger.info(f"Successfully created experiment directory: {exp_dir}")
                return str(exp_dir)
            else:
                logger.error("No experiment directory created by folder_constructor")
                return None

        except ImportError as e:
            logger.error(f"Failed to import folder_constructor: {e}")
            logger.info("Trying alternative method using subprocess...")
            return await self._run_folder_constructor_subprocess(spec_path, dataset_path)
        except Exception as e:
            logger.error(f"Error running folder_constructor: {e}")
            return None

    async def _run_folder_constructor_subprocess(self, spec_path: str, dataset_path: str) -> Optional[str]:
        """Alternative method using subprocess to run folder_constructor"""
        try:
            # Get the path to the UUID experiment builder
            builder_path = os.path.join(
                os.path.dirname(__file__), "..", "folder_constructor", "uuid_experiment_builder.py"
            )

            # Create output directory
            output_root = os.path.join(self.temp_dir, f"geml_output_{uuid.uuid4().hex[:8]}")
            os.makedirs(output_root, exist_ok=True)

            # Build command
            cmd = [
                "python",
                builder_path,
                "--spec-json",
                spec_path,
                "--dataset-path",
                dataset_path,
                "--output-root",
                output_root,
            ]

            # Run the command
            process = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )

            stdout, stderr = await process.communicate()

            if process.returncode == 0:
                # Find the created experiment directory
                exp_dirs = [d for d in os.listdir(output_root) if os.path.isdir(os.path.join(output_root, d))]
                if exp_dirs:
                    logger.info(f"Successfully created experiment directory: {exp_dirs[0]}")
                    return os.path.join(output_root, exp_dirs[0])
                else:
                    logger.error("No experiment directory created")
                    return None
            else:
                logger.error(f"Folder constructor failed with return code {process.returncode}")
                logger.error(f"stderr: {stderr.decode()}")
                return None

        except Exception as e:
            logger.error(f"Error running folder_constructor subprocess: {e}")
            return None

    async def get_experiment_files_info(self, experiment_id: str) -> Optional[Dict[str, Any]]:
        """Get information about experiment files for a given experiment"""
        try:
            experiment = await self.db_service.get_experiment(experiment_id)
            if not experiment:
                return None

            # Get experiment files path from config
            experiment_files_path = experiment.config.get("experiment_files_path")
            if not experiment_files_path:
                return None

            # List files in the experiment directory in MinIO
            prefix = self.storage_service.get_experiment_prefix(experiment_id)
            files = await self.storage_service.list_objects(prefix=prefix)

            return {
                "experiment_id": experiment_id,
                "storage_path": experiment_files_path,
                "files": files,
                "created_at": experiment.updated_at.isoformat() if experiment.updated_at else None,
            }

        except Exception as e:
            logger.error(f"Error getting experiment files info for {experiment_id}: {e}")
            return None

    async def download_experiment_files(self, experiment_id: str, download_path: str) -> bool:
        """Download experiment files from MinIO to local directory"""
        try:
            experiment = await self.db_service.get_experiment(experiment_id)
            if not experiment:
                logger.error(f"Experiment {experiment_id} not found")
                return False

            # Get experiment files path from config
            experiment_files_path = experiment.config.get("experiment_files_path")
            if not experiment_files_path:
                logger.error(f"No experiment files found for experiment {experiment_id}")
                return False

            # Create download directory
            os.makedirs(download_path, exist_ok=True)

            # Download the ZIP file
            zip_path = os.path.join(download_path, "experiment_files.zip")
            success = await self.storage_service.download_file(experiment_files_path, zip_path)

            if success:
                # Extract ZIP file
                import zipfile

                with zipfile.ZipFile(zip_path, "r") as zip_ref:
                    zip_ref.extractall(download_path)

                # Remove ZIP file
                os.unlink(zip_path)

                logger.info(f"Downloaded and extracted experiment files for {experiment_id} to {download_path}")
                return True
            else:
                logger.error(f"Failed to download experiment files for {experiment_id}")
                return False

        except Exception as e:
            logger.error(f"Error downloading experiment files for {experiment_id}: {e}")
            return False
