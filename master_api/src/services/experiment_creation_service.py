#!/usr/bin/env python3

import asyncio
import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd
from loguru import logger

from ..config import load_config
from ..folder_constructor.prompt_experiment_builder import build_prompt_experiment
from ..folder_constructor.chain_experiment_builder import build_chain_experiment
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

    async def create_prompt_experiment_files(
        self,
        experiment_id: str,
        prompt_spec: Dict[str, Any],
        data_path: Optional[str],
    ) -> Optional[str]:
        """
        Create prompt-evolution experiment files from a prompt spec and upload to storage.
        Updates DB config with experiment_files_path and switches experiment.data_path
        to the storage base prefix so the runner can download the whole folder.
        """
        try:
            logger.info(f"Creating prompt experiment files for {experiment_id}")

            # Create a temporary working directory
            temp_work_dir = tempfile.mkdtemp(prefix=f"prompt_exp_{experiment_id[:8]}_")

            # Prepare local dataset path with optional size limiting and train/test split
            # Accept either local path or object key in MinIO (e.g., data/xxx.csv)
            local_dataset_path = None
            if data_path and os.path.exists(data_path):
                local_dataset_path = data_path
            else:
                # Download from storage if looks like an object key or experiments prefix
                local_dataset_path = os.path.join(temp_work_dir, "data.csv")
                if data_path:
                    downloaded = await self.storage_service.download_file(data_path, local_dataset_path)
                    if not downloaded:
                        logger.error(f"Failed to download dataset for prompt experiment: {data_path}")
                        return None
                else:
                    logger.error("No dataset path provided for prompt experiment")
                    return None

            # Process dataset if size limiting is needed
            dataset_size = prompt_spec.get("dataset_size")
            test_size = prompt_spec.get("test_size", 0.2)

            if dataset_size is not None and dataset_size > 0:
                try:
                    df = pd.read_csv(local_dataset_path)
                    original_size = len(df)

                    # Limit dataset size
                    if len(df) > dataset_size:
                        df = df.head(int(dataset_size))
                        logger.info(f"Limited prompt dataset from {original_size} to {len(df)} rows")

                    # For prompts, we save the full dataset (train/test split happens in context.py)
                    processed_path = os.path.join(temp_work_dir, "processed_data.csv")
                    df.to_csv(processed_path, index=False)
                    local_dataset_path = processed_path
                except Exception as e:
                    logger.error(f"Error processing prompt dataset: {e}")
                    # Continue with original dataset

            # Resolve template base:
            template_base = os.path.join(os.path.dirname(__file__), "..", "folder_constructor", "validate_templates")

            output_root = tempfile.mkdtemp(prefix="gigaevo_prompt_")

            # Build prompt experiment folder
            exp_dir = build_prompt_experiment(
                spec=prompt_spec,
                output_root=output_root,
                template_base=template_base,
                dataset_path=local_dataset_path,
                experiment_id=experiment_id,
            )

            if not exp_dir or not os.path.exists(exp_dir):
                logger.error("Prompt folder constructor did not produce an experiment directory")
                return None

            # Upload folder to storage
            storage_base_path = await self.storage_service.upload_experiment_files(str(exp_dir), experiment_id)
            if not storage_base_path:
                logger.error("Failed to upload prompt experiment files to storage")
                # Mark failed in DB
                try:
                    await self.db_service.update_experiment_status(
                        experiment_id, "failed", error_message="Failed to upload prompt experiment files"
                    )
                except Exception:
                    pass
                return None

            logger.info(f"Uploaded prompt experiment files for {experiment_id} to {storage_base_path}")

            # Update DB: set experiment_files_path in config and point data_path to storage folder prefix
            experiment = await self.db_service.get_experiment(experiment_id)
            if experiment:
                updated_config = (experiment.config or {}).copy()
                updated_config["experiment_files_path"] = storage_base_path
                await self.db_service.update_experiment(
                    experiment_id,
                    config=updated_config,
                    data_path=storage_base_path,
                    status="prepared",
                )

            return storage_base_path

        except Exception as e:
            logger.error(f"Error creating prompt experiment files for {experiment_id}: {e}")
            try:
                await self.db_service.update_experiment_status(experiment_id, "failed", error_message=str(e))
            except Exception:
                pass
            return None
        finally:
            try:
                if "temp_work_dir" in locals() and os.path.exists(temp_work_dir):  # type: ignore
                    shutil.rmtree(temp_work_dir, ignore_errors=True)  # type: ignore
            except Exception:
                pass

    async def create_chain_experiment_files(
        self,
        experiment_id: str,
        chain_spec: Dict[str, Any],
        data_path: Optional[str],
    ) -> Optional[str]:
        try:
            logger.info(f"Creating chain experiment files for {experiment_id}")

            temp_work_dir = tempfile.mkdtemp(prefix=f"chain_exp_{experiment_id[:8]}_")

            local_dataset_path = None
            if data_path and os.path.exists(data_path):
                local_dataset_path = data_path
            else:
                local_dataset_path = os.path.join(temp_work_dir, "data.csv")
                if data_path:
                    downloaded = await self.storage_service.download_file(data_path, local_dataset_path)
                    if not downloaded:
                        logger.error(f"Failed to download dataset for chain experiment: {data_path}")
                        return None
                else:
                    logger.error("No dataset path provided for chain experiment")
                    return None

            dataset_size = chain_spec.get("dataset_size")
            test_size = chain_spec.get("test_size", 0.2)

            if dataset_size is not None and dataset_size > 0:
                try:
                    df = pd.read_csv(local_dataset_path)
                    original_size = len(df)

                    if len(df) > dataset_size:
                        df = df.head(int(dataset_size))
                        logger.info(f"Limited chain dataset from {original_size} to {len(df)} rows")

                    processed_path = os.path.join(temp_work_dir, "processed_data.csv")
                    df.to_csv(processed_path, index=False)
                    local_dataset_path = processed_path
                except Exception as e:
                    logger.error(f"Error processing chain dataset: {e}")

            template_base = os.path.join(os.path.dirname(__file__), "..", "folder_constructor", "validate_templates")

            output_root = tempfile.mkdtemp(prefix="gigaevo_chain_")

            exp_dir = build_chain_experiment(
                spec=chain_spec,
                output_root=output_root,
                template_base=template_base,
                dataset_path=local_dataset_path,
                experiment_id=experiment_id,
            )

            if not exp_dir or not os.path.exists(exp_dir):
                logger.error("Chain folder constructor did not produce an experiment directory")
                return None

            storage_base_path = await self.storage_service.upload_experiment_files(str(exp_dir), experiment_id)
            if not storage_base_path:
                logger.error("Failed to upload chain experiment files to storage")
                try:
                    await self.db_service.update_experiment_status(
                        experiment_id, "failed", error_message="Failed to upload chain experiment files"
                    )
                except Exception:
                    pass
                return None

            logger.info(f"Uploaded chain experiment files for {experiment_id} to {storage_base_path}")

            experiment = await self.db_service.get_experiment(experiment_id)
            if experiment:
                updated_config = (experiment.config or {}).copy()
                updated_config["experiment_files_path"] = storage_base_path
                await self.db_service.update_experiment(
                    experiment_id,
                    config=updated_config,
                    data_path=storage_base_path,
                    status="prepared",
                )

            return storage_base_path

        except Exception as e:
            logger.error(f"Error creating chain experiment files for {experiment_id}: {e}")
            try:
                await self.db_service.update_experiment_status(experiment_id, "failed", error_message=str(e))
            except Exception:
                pass
            return None
        finally:
            try:
                if "temp_work_dir" in locals() and os.path.exists(temp_work_dir):  # type: ignore
                    shutil.rmtree(temp_work_dir, ignore_errors=True)  # type: ignore
            except Exception:
                pass

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
            temp_work_dir = os.path.join(self.temp_dir, f"gigaevo_experiment_{experiment_id}")
            os.makedirs(temp_work_dir, exist_ok=True)

            # Create spec JSON for folder_constructor
            spec_json = self._create_spec_json(experiment_config, data_path)
            spec_path = os.path.join(temp_work_dir, "experiment_spec.json")

            with open(spec_path, "w") as f:
                json.dump(spec_json, f, indent=2)

            # Get or download dataset
            dataset_path = await self._prepare_dataset(spec_json, temp_work_dir, experiment_config)
            if not dataset_path:
                logger.error(f"Failed to prepare dataset for experiment {experiment_id}")
                try:
                    await self.db_service.update_experiment_status(
                        experiment_id, "preparation_failed", error_message="Failed to prepare dataset"
                    )
                except Exception:
                    pass
                return None

            # Use folder_constructor to create experiment files
            output_dir = await self._run_folder_constructor(spec_path, dataset_path)

            if not output_dir or not os.path.exists(output_dir):
                logger.error(f"Folder constructor failed for experiment {experiment_id}")
                try:
                    await self.db_service.update_experiment_status(
                        experiment_id, "preparation_failed", error_message="Folder constructor failed"
                    )
                except Exception:
                    pass
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
                        status="prepared",
                    )

                return storage_path
            else:
                logger.error(f"Failed to upload experiment files for {experiment_id}")
                # Persist FAILED status explicitly if upload did not return a storage path
                try:
                    await self.db_service.update_experiment_status(
                        experiment_id,
                        "preparation_failed",
                        error_message="Failed to upload experiment files",
                    )
                except Exception as db_err:
                    logger.error(f"Failed to set experiment {experiment_id} status to failed: {db_err}")
                return None

        except Exception as e:
            logger.error(f"Error creating experiment files for {experiment_id}: {e}")
            # Ensure the FAILED status and error are persisted for visibility in DB/UI
            try:
                await self.db_service.update_experiment_status(
                    experiment_id, "preparation_failed", error_message=str(e)
                )
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

        timeout_seconds = experiment_config.get("timeout_seconds")
        if timeout_seconds is None or timeout_seconds == 0:
            max_iterations = experiment_config.get("max_iterations", 100)
            timeout_seconds = max(3600, int(max_iterations * 90 * 1.3))
        
        # Create spec JSON structure that matches build_uuid_experiment expectations
        spec_json = {
            "task_type": task_type,
            "task_description": description,
            "target_field": target_column,
            "dataset_path": data_path or "data.csv",
            # Additional fields for our own use
            "llm_model": experiment_config.get("llm_model", "local-inference"),
            "max_iterations": experiment_config.get("max_iterations", 100),
            "timeout_seconds": timeout_seconds,
            "parameters": experiment_config.get("parameters", {}),
        }

        # Add dataset configuration if provided
        if experiment_config.get("dataset_size") is not None:
            spec_json["dataset_size"] = experiment_config.get("dataset_size")
        if experiment_config.get("test_size") is not None:
            spec_json["test_size"] = experiment_config.get("test_size")

        # Add n_clusters for clustering tasks
        if task_type == "clustering":
            spec_json["n_clusters"] = 3  # Default value

        return spec_json

    async def _prepare_dataset(
        self, spec_json: Dict[str, Any], work_dir: str, experiment_config: Optional[Dict[str, Any]] = None
    ) -> Optional[str]:
        """Prepare dataset for experiment creation with optional size limiting and train/test split"""
        dataset_path = spec_json.get("dataset_path")

        if not dataset_path:
            logger.warning("No dataset path specified in experiment config")
            return None

        # Get dataset configuration from experiment_config or spec_json
        dataset_size = None
        test_size = None
        if experiment_config:
            dataset_size = experiment_config.get("dataset_size")
            test_size = experiment_config.get("test_size")
        if dataset_size is None:
            dataset_size = spec_json.get("dataset_size")
        if test_size is None:
            test_size = spec_json.get("test_size")

        # Download or locate dataset file
        local_path = None
        if os.path.exists(dataset_path):
            local_path = dataset_path
        elif dataset_path.startswith(("data/", "prompt_data/", "experiments/")):
            local_path = os.path.join(work_dir, os.path.basename(dataset_path))
            success = await self.storage_service.download_file(dataset_path, local_path)
            if not success:
                logger.error(f"Failed to download dataset from MinIO: {dataset_path}")
                return None
        else:
            # Try to find a matching CSV file in work_dir
            csv_files = list(Path(work_dir).glob("*.csv"))
            if csv_files:
                logger.info(f"Using CSV file found in work directory: {csv_files[0]}")
                local_path = str(csv_files[0])
            else:
                logger.error(f"Could not find dataset file: {dataset_path}")
                return None

        # Process dataset if size limiting or split is needed
        if dataset_size is not None and dataset_size > 0:
            try:
                df = pd.read_csv(local_path)
                original_size = len(df)

                # Limit dataset size
                if len(df) > dataset_size:
                    df = df.head(int(dataset_size))
                    logger.info(f"Limited dataset from {original_size} to {len(df)} rows")

                # Save processed dataset
                processed_path = os.path.join(work_dir, "processed_data.csv")
                df.to_csv(processed_path, index=False)
                return processed_path
            except Exception as e:
                logger.error(f"Error processing dataset: {e}")
                return local_path

        return local_path

    async def _run_folder_constructor(self, spec_path: str, dataset_path: str) -> Optional[str]:
        """Run the folder_constructor to create experiment files"""
        try:
            # Import the UUID experiment builder function

            # Create output directory
            output_root = os.path.join(self.temp_dir, f"gigaevo_output_{uuid.uuid4().hex[:8]}")
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
            output_root = os.path.join(self.temp_dir, f"gigaevo_output_{uuid.uuid4().hex[:8]}")
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
