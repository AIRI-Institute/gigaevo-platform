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
from ..folder_constructor.chain_experiment_builder import build_chain_experiment
from ..folder_constructor.prompt_experiment_builder import build_prompt_experiment
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
                # Prepare local dataset path(s)
                local_dataset_path = os.path.join(temp_work_dir, "data.csv")
                if data_path:
                    # If remote path is a ZIP, download as zip, then extract CSV
                    is_zip = str(data_path).lower().endswith(".zip")
                    if is_zip:
                        local_zip_path = os.path.join(temp_work_dir, "dataset.zip")
                        downloaded = await self.storage_service.download_file(data_path, local_zip_path)
                        if not downloaded:
                            logger.error(f"Failed to download ZIP dataset for chain experiment: {data_path}")
                            try:
                                await self.db_service.update_experiment_status(
                                    experiment_id,
                                    "preparation_failed",
                                    error_message=f"Failed to download dataset: {data_path}",
                                )
                            except Exception:
                                pass
                            return None
                        # Extract zip and look for any CSV (prefer train.csv or data.csv)
                        try:
                            import zipfile

                            with zipfile.ZipFile(local_zip_path, "r") as zf:
                                extract_dir = os.path.join(temp_work_dir, "extracted")
                                zf.extractall(extract_dir)
                            # Heuristics to pick a CSV
                            preferred: list[str] = []
                            any_csv: list[str] = []
                            for root, _, files in os.walk(extract_dir):
                                for f in files:
                                    if f.lower().endswith(".csv"):
                                        full = os.path.join(root, f)
                                        any_csv.append(full)
                                        if f.lower() in ("train.csv", "data.csv", "dataset.csv"):
                                            preferred.append(full)
                            chosen = preferred[0] if preferred else (any_csv[0] if any_csv else None)
                            if chosen:
                                import shutil as _shutil

                                _shutil.copy(chosen, local_dataset_path)
                            else:
                                # No CSV found: try to synthesize image/mask pairs CSV from directory structure
                                from pathlib import Path as _Path
                                import pandas as _pd

                                img_exts = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}
                                image_files: dict[str, str] = {}
                                mask_files: dict[str, str] = {}
                                for root, _, files in os.walk(extract_dir):
                                    root_lower = root.lower()
                                    for f in files:
                                        ext = _Path(f).suffix.lower()
                                        if ext in img_exts:
                                            full = os.path.join(root, f)
                                            stem = _Path(f).stem
                                            # classify as image or mask by folder hint or filename
                                            is_mask = any(
                                                k in root_lower for k in ["mask", "masks", "gt", "label"]
                                            ) or any(
                                                k in stem.lower()
                                                for k in ["_mask", "-mask", "_gt", "-gt", "_label", "-label"]
                                            )
                                            if is_mask:
                                                mask_files[stem] = full
                                            else:
                                                image_files[stem] = full
                                pairs: list[tuple[str, str]] = []
                                # Exact stem matches
                                for stem, img in image_files.items():
                                    if stem in mask_files:
                                        pairs.append((img, mask_files[stem]))
                                # Fallback: try normalize stems by removing common suffixes
                                if not pairs and image_files and mask_files:

                                    def _norm(s: str) -> str:
                                        s = s.lower()
                                        for suf in [
                                            "_img",
                                            "-img",
                                            "_image",
                                            "-image",
                                            "_mask",
                                            "-mask",
                                            "_gt",
                                            "-gt",
                                            "_label",
                                            "-label",
                                        ]:
                                            if s.endswith(suf):
                                                s = s[: -len(suf)]
                                        return s

                                    norm_masks = {_norm(k): v for k, v in mask_files.items()}
                                    for stem, img in image_files.items():
                                        ns = _norm(stem)
                                        if ns in norm_masks:
                                            pairs.append((img, norm_masks[ns]))
                                if pairs:
                                    # Limit to a small number to keep runs quick; keep first N deterministically
                                    pairs_sorted = sorted(pairs, key=lambda p: p[0])
                                    df_pairs = _pd.DataFrame(
                                        [{"image_path": p[0], "mask_path": p[1], "target": 0} for p in pairs_sorted]
                                    )
                                    df_pairs.to_csv(local_dataset_path, index=False)
                                    logger.info(
                                        f"Synthesized dataset CSV with {len(df_pairs)} image/mask pair(s) from {data_path}"
                                    )
                                else:
                                    # Still nothing: create a minimal placeholder CSV with 4 rows
                                    rows = 4
                                    df_min = _pd.DataFrame(
                                        {
                                            "problem": [
                                                f"Sample {i + 1}: segmentation evaluation item" for i in range(rows)
                                            ],
                                            "target": [0 for _ in range(rows)],
                                        }
                                    )
                                    df_min.to_csv(local_dataset_path, index=False)
                                    logger.warning(
                                        f"No CSV or image/mask pairs found inside ZIP {data_path}; created minimal placeholder CSV with 4 rows"
                                    )
                        except Exception as ex:
                            logger.error(f"Failed to extract/process ZIP dataset {data_path}: {ex}")
                            try:
                                await self.db_service.update_experiment_status(
                                    experiment_id,
                                    "preparation_failed",
                                    error_message=f"Failed to extract/process dataset ZIP",
                                )
                            except Exception:
                                pass
                            return None
                    else:
                        # Regular file in storage (assumed CSV)
                        downloaded = await self.storage_service.download_file(data_path, local_dataset_path)
                        if not downloaded:
                            logger.error(f"Failed to download dataset for chain experiment: {data_path}")
                            try:
                                await self.db_service.update_experiment_status(
                                    experiment_id,
                                    "preparation_failed",
                                    error_message=f"Failed to download dataset: {data_path}",
                                )
                            except Exception:
                                pass
                            return None

                        # Check if CSV contains storage paths (image_path/mask_path pointing to storage)
                        # If so, download those files and update CSV paths to local paths
                        try:
                            import pandas as pd

                            df_check = pd.read_csv(local_dataset_path)
                            if "image_path" in df_check.columns or "mask_path" in df_check.columns:
                                # Check if paths look like storage paths (start with "data/" or don't start with "/")
                                needs_download = False
                                for col in ["image_path", "mask_path"]:
                                    if col in df_check.columns:
                                        sample_path = str(df_check[col].iloc[0]) if len(df_check) > 0 else ""
                                        # If path doesn't exist locally and looks like storage path
                                        if sample_path and not os.path.exists(sample_path):
                                            if sample_path.startswith("data/") or (
                                                not sample_path.startswith("/") and not os.path.isabs(sample_path)
                                            ):
                                                needs_download = True
                                                break

                                if needs_download:
                                    logger.info(
                                        f"CSV contains storage paths, downloading referenced files for experiment {experiment_id}"
                                    )
                                    # Create local directory for downloaded images/masks
                                    local_images_dir = os.path.join(temp_work_dir, "images")
                                    local_masks_dir = os.path.join(temp_work_dir, "masks")
                                    os.makedirs(local_images_dir, exist_ok=True)
                                    os.makedirs(local_masks_dir, exist_ok=True)

                                    updated_rows = []
                                    for idx, row in df_check.iterrows():
                                        updated_row = row.to_dict()

                                        # Download and update image_path
                                        if "image_path" in updated_row and updated_row["image_path"]:
                                            storage_img_path = str(updated_row["image_path"])
                                            if not os.path.exists(storage_img_path):
                                                img_filename = os.path.basename(storage_img_path)
                                                local_img_path = os.path.join(local_images_dir, img_filename)
                                                img_downloaded = await self.storage_service.download_file(
                                                    storage_img_path, local_img_path
                                                )
                                                if img_downloaded:
                                                    updated_row["image_path"] = local_img_path
                                                else:
                                                    logger.warning(
                                                        f"Failed to download image from storage: {storage_img_path}"
                                                    )

                                        # Download and update mask_path
                                        if "mask_path" in updated_row and updated_row["mask_path"]:
                                            storage_mask_path = str(updated_row["mask_path"])
                                            if not os.path.exists(storage_mask_path):
                                                mask_filename = os.path.basename(storage_mask_path)
                                                local_mask_path = os.path.join(local_masks_dir, mask_filename)
                                                mask_downloaded = await self.storage_service.download_file(
                                                    storage_mask_path, local_mask_path
                                                )
                                                if mask_downloaded:
                                                    updated_row["mask_path"] = local_mask_path
                                                else:
                                                    logger.warning(
                                                        f"Failed to download mask from storage: {storage_mask_path}"
                                                    )

                                        updated_rows.append(updated_row)

                                    # Save updated CSV with local paths
                                    updated_df = pd.DataFrame(updated_rows)
                                    updated_csv_path = os.path.join(temp_work_dir, "dataset_with_local_paths.csv")
                                    updated_df.to_csv(updated_csv_path, index=False)
                                    local_dataset_path = updated_csv_path
                                    logger.info(f"Updated CSV with local file paths for experiment {experiment_id}")
                        except Exception as e:
                            logger.warning(
                                f"Failed to process storage paths in CSV for experiment {experiment_id}: {e}"
                            )
                            # Continue with original CSV if processing fails
                else:
                    logger.error("No dataset path provided for chain experiment")
                    try:
                        await self.db_service.update_experiment_status(
                            experiment_id, "preparation_failed", error_message="Dataset path was not provided"
                        )
                    except Exception:
                        pass
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

            # Before building experiment, check if CSV contains local image/mask paths
            # If so, copy those files to a directory that will be included in the experiment
            final_dataset_path = local_dataset_path
            try:
                import pandas as pd

                df_final = pd.read_csv(local_dataset_path)
                if ("image_path" in df_final.columns or "mask_path" in df_final.columns) and len(df_final) > 0:
                    # Check if paths are local (exist on filesystem)
                    sample_img_path = str(df_final["image_path"].iloc[0]) if "image_path" in df_final.columns else None
                    if sample_img_path and os.path.exists(sample_img_path):
                        # Create dataset subdirectory for images and masks
                        dataset_files_dir = os.path.join(temp_work_dir, "dataset_files")
                        dataset_images_dir = os.path.join(dataset_files_dir, "images")
                        dataset_masks_dir = os.path.join(dataset_files_dir, "masks")
                        os.makedirs(dataset_images_dir, exist_ok=True)
                        os.makedirs(dataset_masks_dir, exist_ok=True)

                        updated_rows = []
                        for idx, row in df_final.iterrows():
                            updated_row = row.to_dict()

                            # Copy image file if it exists locally
                            if "image_path" in updated_row and updated_row["image_path"]:
                                img_path = str(updated_row["image_path"])
                                if os.path.exists(img_path):
                                    img_filename = os.path.basename(img_path)
                                    dest_img_path = os.path.join(dataset_images_dir, img_filename)
                                    shutil.copy2(img_path, dest_img_path)
                                    # Update path to be relative to dataset directory
                                    updated_row["image_path"] = os.path.join("dataset_files", "images", img_filename)

                            # Copy mask file if it exists locally
                            if "mask_path" in updated_row and updated_row["mask_path"]:
                                mask_path = str(updated_row["mask_path"])
                                if os.path.exists(mask_path):
                                    mask_filename = os.path.basename(mask_path)
                                    dest_mask_path = os.path.join(dataset_masks_dir, mask_filename)
                                    shutil.copy2(mask_path, dest_mask_path)
                                    # Update path to be relative to dataset directory
                                    updated_row["mask_path"] = os.path.join("dataset_files", "masks", mask_filename)

                            updated_rows.append(updated_row)

                        # Save updated CSV with relative paths
                        updated_df = pd.DataFrame(updated_rows)
                        updated_csv_path = os.path.join(temp_work_dir, "dataset_final.csv")
                        updated_df.to_csv(updated_csv_path, index=False)
                        final_dataset_path = updated_csv_path
                        logger.info(
                            f"Updated CSV with relative paths and copied image/mask files for experiment {experiment_id}"
                        )
            except Exception as e:
                logger.warning(f"Failed to process local image/mask files for experiment {experiment_id}: {e}")
                # Continue with original CSV if processing fails

            exp_dir = build_chain_experiment(
                spec=chain_spec,
                output_root=output_root,
                template_base=template_base,
                dataset_path=final_dataset_path,
                experiment_id=experiment_id,
            )

            # Copy dataset_files directory (images/masks) to experiment directory if it exists
            dataset_files_src = os.path.join(temp_work_dir, "dataset_files")
            if os.path.exists(dataset_files_src):
                dataset_files_dst = os.path.join(str(exp_dir), "dataset_files")
                shutil.copytree(dataset_files_src, dataset_files_dst, dirs_exist_ok=True)
                logger.info(f"Copied dataset_files (images/masks) to experiment directory for {experiment_id}")

            # Save custom_tools.py if python_code is provided
            python_code = chain_spec.get("python_code")
            if python_code and python_code.strip():
                try:
                    custom_tools_path = os.path.join(str(exp_dir), "custom_tools.py")
                    with open(custom_tools_path, "w", encoding="utf-8") as f:
                        f.write(python_code.strip())
                    logger.info(f"Saved custom_tools.py for experiment {experiment_id} ({len(python_code)} chars)")
                except Exception as e:
                    logger.warning(f"Failed to save custom_tools.py for experiment {experiment_id}: {e}")

            # chain_spec.json is already created by build_chain_experiment() with all necessary fields
            # (enable_feedback, feedback_template, target_column, evolution_mode)
            # No need to overwrite it here - just verify it exists
            chain_spec_path = os.path.join(str(exp_dir), "chain_spec.json")
            if not os.path.exists(chain_spec_path):
                logger.warning(
                    f"chain_spec.json not found after build_chain_experiment for {experiment_id}, creating minimal version"
                )
                try:
                    import json

                    minimal_spec = {
                        "enable_feedback": chain_spec.get("enable_feedback", True),
                        "feedback_template": chain_spec.get("feedback_template", "detailed"),
                        "target_column": chain_spec.get("target_column", ""),
                        "evolution_mode": chain_spec.get("evolution_mode", "full_chain"),
                    }
                    with open(chain_spec_path, "w", encoding="utf-8") as f:
                        json.dump(minimal_spec, f, indent=2)
                    logger.info(f"Created minimal chain_spec.json for experiment {experiment_id}")
                except Exception as e:
                    logger.warning(f"Failed to create minimal chain_spec.json for experiment {experiment_id}: {e}")

            # Copy feedback integration files if feedback is enabled
            if chain_spec.get("enable_feedback", False):
                try:
                    from pathlib import Path as _Path
                    import shutil as _shutil

                    # Get the path to master_api directory
                    # Try multiple potential root locations
                    current_file = _Path(__file__).resolve()

                    # List of potential master_api directories to search
                    potential_roots = [
                        current_file.parent.parent.parent,  # master_api/src/services -> master_api
                        _Path("/app/master_api"),  # Docker path
                        _Path("/app"),  # Alternative Docker path
                        current_file.parent.parent.parent.parent,  # gigaevo-platform
                    ]

                    master_api_dir = None
                    feedback_integration_src = None
                    chain_feedback_service_src = None

                    # Search for feedback_integration.py
                    feedback_integration_paths = [
                        "folder_constructor/validate_templates/chain/feedback_integration.py",
                        "src/folder_constructor/validate_templates/chain/feedback_integration.py",
                    ]

                    chain_feedback_service_paths = [
                        "services/chain_feedback_service.py",
                        "src/services/chain_feedback_service.py",
                    ]

                    for root in potential_roots:
                        if root.exists() and master_api_dir is None:
                            # Try to find feedback_integration.py
                            for rel_path in feedback_integration_paths:
                                test_path = root / rel_path
                                if test_path.exists():
                                    master_api_dir = root
                                    feedback_integration_src = test_path
                                    logger.info(f"Found master_api at {root}")
                                    break
                            if master_api_dir:
                                break

                    # If still not found, try direct absolute paths for Docker
                    if not feedback_integration_src:
                        docker_paths = [
                            _Path(
                                "/app/master_api/src/folder_constructor/validate_templates/chain/feedback_integration.py"
                            ),
                            _Path("/app/src/folder_constructor/validate_templates/chain/feedback_integration.py"),
                        ]
                        for docker_path in docker_paths:
                            if docker_path.exists():
                                feedback_integration_src = docker_path
                                logger.info(f"Found feedback_integration.py at Docker path: {docker_path}")
                                break

                    # Find chain_feedback_service.py
                    if master_api_dir:
                        for rel_path in chain_feedback_service_paths:
                            test_path = master_api_dir / rel_path
                            if test_path.exists():
                                chain_feedback_service_src = test_path
                                break

                    if not chain_feedback_service_src:
                        docker_paths = [
                            _Path("/app/master_api/src/services/chain_feedback_service.py"),
                            _Path("/app/src/services/chain_feedback_service.py"),
                        ]
                        for docker_path in docker_paths:
                            if docker_path.exists():
                                chain_feedback_service_src = docker_path
                                logger.info(f"Found chain_feedback_service.py at Docker path: {docker_path}")
                                break

                    # Copy feedback_integration.py to experiment root
                    if feedback_integration_src and feedback_integration_src.exists():
                        feedback_integration_dst = os.path.join(str(exp_dir), "feedback_integration.py")
                        _shutil.copy(str(feedback_integration_src), feedback_integration_dst)
                        logger.info(f"Copied feedback_integration.py for experiment {experiment_id}")
                    else:
                        logger.error(
                            f"feedback_integration.py not found in any location for experiment {experiment_id}"
                        )

                    # Create src/services/ directory structure
                    src_services_dir = os.path.join(str(exp_dir), "src", "services")
                    os.makedirs(src_services_dir, exist_ok=True)

                    # Create __init__.py files
                    with open(os.path.join(str(exp_dir), "src", "__init__.py"), "w") as f:
                        f.write("")
                    with open(os.path.join(src_services_dir, "__init__.py"), "w") as f:
                        f.write("")

                    # Copy chain_feedback_service.py
                    if chain_feedback_service_src and chain_feedback_service_src.exists():
                        chain_feedback_service_dst = os.path.join(src_services_dir, "chain_feedback_service.py")
                        _shutil.copy(str(chain_feedback_service_src), chain_feedback_service_dst)
                        logger.info(f"Copied chain_feedback_service.py for experiment {experiment_id}")
                    else:
                        logger.error(
                            f"chain_feedback_service.py not found in any location for experiment {experiment_id}"
                        )

                    if feedback_integration_src and chain_feedback_service_src:
                        logger.info(f"Successfully set up feedback integration files for experiment {experiment_id}")
                    else:
                        logger.warning(f"Partial setup of feedback integration files for experiment {experiment_id}")

                except Exception as feedback_err:
                    logger.warning(
                        f"Failed to copy feedback integration files for experiment {experiment_id}: {feedback_err}"
                    )
                    import traceback

                    traceback.print_exc()
                    # Don't fail the experiment creation if feedback files can't be copied
                    # The experiment will still work, just without feedback

            if not exp_dir or not os.path.exists(exp_dir):
                logger.error("Chain folder constructor did not produce an experiment directory")
                try:
                    await self.db_service.update_experiment_status(
                        experiment_id, "preparation_failed", error_message="Folder constructor produced no output"
                    )
                except Exception:
                    pass
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

        # Create spec JSON structure that matches build_uuid_experiment expectations
        spec_json = {
            "task_type": task_type,
            "task_description": description,
            "target_field": target_column,
            "dataset_path": data_path or "data.csv",
            # Additional fields for our own use
            "llm_model": experiment_config.get("llm_model", "local-inference"),
            "max_iterations": experiment_config.get("max_iterations", 100),
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
