"""File handling utilities for the web UI."""

import os
from pathlib import Path
from typing import Optional

import pandas as pd
import requests
from config.settings import INPUT_DATA_DIR, S3_API_URL
from loguru import logger


def extract_source_path_from_upload(data_file) -> Optional[str]:
    """Extract local source path from a Gradio file upload value.

    Args:
        data_file: Gradio file upload object

    Returns:
        Local file path if file exists and is accessible, None otherwise
    """
    if data_file is None:
        return None

    src_path = None
    if isinstance(data_file, (str, Path)):
        src_path = str(data_file)
    else:
        src_path = getattr(data_file, "name", None)

    return src_path if (src_path and os.path.exists(src_path)) else None


def read_csv_columns(file_path: str) -> list:
    """Read CSV header and return column names.

    Args:
        file_path: Path to CSV file

    Returns:
        List of column names, empty list on failure
    """
    try:
        df = pd.read_csv(file_path, nrows=100)
        return list(df.columns)
    except Exception as e:
        logger.error(f"Failed to read CSV columns from {file_path}: {e}")
        return []


def download_preset_dataset(dataset_path: str, bucket_name: str) -> Optional[str]:
    """Download preset dataset from S3 storage.

    Args:
        dataset_path: Relative path to dataset in bucket
        bucket_name: Name of the S3 bucket

    Returns:
        Local path to downloaded file, None if failed
    """
    try:
        # Extract filename from dataset path
        filename = os.path.basename(dataset_path)
        if not filename:
            logger.error(f"No filename found in dataset path: {dataset_path}")
            return None

        # Create local path
        os.makedirs(INPUT_DATA_DIR, exist_ok=True)
        local_path = os.path.join(INPUT_DATA_DIR, filename)

        # Download file
        url = f"{S3_API_URL}/{bucket_name}/data/{filename}"
        resp = requests.get(url, timeout=5)

        if resp.ok:
            with open(local_path, "wb") as f:
                f.write(resp.content)
            return local_path
        else:
            logger.error(f"Failed to download dataset {filename}: {resp.status_code}")
            return None

    except Exception as e:
        logger.error(f"Error downloading preset dataset {dataset_path}: {e}")
        return None


def cleanup_temp_file(file_path: str) -> None:
    """Safely clean up temporary file.

    Args:
        file_path: Path to file to clean up
    """
    try:
        if file_path and os.path.exists(file_path):
            os.unlink(file_path)
    except Exception as e:
        logger.error(f"Failed to cleanup temp file {file_path}: {e}")


def get_file_size_info(file_path: str) -> str:
    """Get human-readable file size information.

    Args:
        file_path: Path to file

    Returns:
        Human-readable size string
    """
    try:
        if not os.path.exists(file_path):
            return "File not found"

        size_bytes = os.path.getsize(file_path)
        if size_bytes < 1024:
            return f"{size_bytes} B"
        elif size_bytes < 1024 * 1024:
            return f"{size_bytes / 1024:.1f} KB"
        else:
            return f"{size_bytes / (1024 * 1024):.1f} MB"

    except Exception as e:
        logger.error(f"Failed to get file size for {file_path}: {e}")
        return "Unknown size"


def validate_file_type(file_path: str, allowed_types: list) -> bool:
    """Validate file type against allowed extensions.

    Args:
        file_path: Path to file
        allowed_types: List of allowed file extensions

    Returns:
        True if file type is allowed, False otherwise
    """
    try:
        _, ext = os.path.splitext(file_path)
        return ext.lower() in [t.lower() for t in allowed_types]
    except Exception:
        return False
