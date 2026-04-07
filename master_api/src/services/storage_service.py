#!/usr/bin/env python3

import json
import os
import tempfile
import zipfile
from datetime import timedelta
from io import BytesIO
from typing import Dict, List, Optional

from loguru import logger
from minio import Minio
from minio.error import S3Error


class StorageService:
    """Service for managing MinIO/S3 storage operations"""

    def __init__(self, config):
        self.config = config
        self.client: Optional[Minio] = None
        self.bucket_name = config.storage.bucket_name

    async def initialize(self):
        """Initialize MinIO client and ensure bucket exists"""
        try:
            endpoint = self.config.storage.endpoint_url.replace("http://", "").replace("https://", "")
            self.client = Minio(
                endpoint=endpoint,
                access_key=self.config.storage.access_key,
                secret_key=self.config.storage.secret_key,
                secure=self.config.storage.endpoint_url.startswith("https://"),
            )

            # Create bucket if it doesn't exist
            if not self.client.bucket_exists(self.bucket_name):
                self.client.make_bucket(self.bucket_name)
                logger.info(f"Created MinIO bucket: {self.bucket_name}")

            # Always ensure bucket has public read policy
            try:
                public_read_policy = {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Principal": {"AWS": "*"},
                            "Action": ["s3:GetObject"],
                            "Resource": [f"arn:aws:s3:::{self.bucket_name}/*"],
                        }
                    ],
                }
                policy_json = json.dumps(public_read_policy)
                self.client.set_bucket_policy(self.bucket_name, policy_json)
                logger.info(f"Set public read policy for bucket: {self.bucket_name}")
            except Exception as e:
                logger.error(f"Failed to set bucket policy: {e}")
                # Continue without policy - bucket may still work with proper credentials

        except Exception as e:
            logger.warning(f"MinIO storage not available - running in degraded mode: {e}")

    async def upload_file(self, file_path: str, object_name: str, metadata: Optional[Dict[str, str]] = None) -> bool:
        """Upload a file to MinIO storage"""
        if not self.client:
            logger.error("Storage service not initialized")
            return False

        try:
            result = self.client.fput_object(self.bucket_name, object_name, file_path, metadata=metadata or {})
            logger.info(f"Uploaded file to MinIO: {object_name} (etag: {result.etag})")
            return True
        except S3Error as e:
            logger.error(f"Failed to upload file {object_name}: {e}")
            return False

    async def upload_bytes(self, data: bytes, object_name: str, metadata: Optional[Dict[str, str]] = None) -> bool:
        """Upload bytes data to MinIO storage"""
        if not self.client:
            logger.error("Storage service not initialized")
            return False

        try:
            result = self.client.put_object(
                self.bucket_name, object_name, BytesIO(data), length=len(data), metadata=metadata or {}
            )
            logger.info(f"Uploaded data to MinIO: {object_name} (etag: {result.etag})")
            return True
        except S3Error as e:
            logger.error(f"Failed to upload data {object_name}: {e}")
            return False

    async def download_file(self, object_name: str, file_path: str) -> bool:
        """Download a file from MinIO storage"""
        if not self.client:
            logger.error("Storage service not initialized")
            return False

        try:
            self.client.fget_object(self.bucket_name, object_name, file_path)
            logger.info(f"Downloaded file from MinIO: {object_name} -> {file_path}")
            return True
        except S3Error as e:
            logger.error(f"Failed to download file {object_name}: {e}")
            return False

    async def download_bytes(self, object_name: str) -> Optional[bytes]:
        """Download file as bytes from MinIO storage"""
        if not self.client:
            logger.error("Storage service not initialized")
            return None

        try:
            response = self.client.get_object(self.bucket_name, object_name)
            data = response.read()
            response.close()
            response.release_conn()
            logger.info(f"Downloaded data from MinIO: {object_name} ({len(data)} bytes)")
            return data
        except S3Error as e:
            logger.error(f"Failed to download data {object_name}: {e}")
            return None

    async def upload_directory_as_zip(
        self, directory_path: str, object_name: str, metadata: Optional[Dict[str, str]] = None
    ) -> bool:
        """Upload an entire directory as a ZIP file to MinIO"""
        if not os.path.exists(directory_path):
            logger.error(f"Directory does not exist: {directory_path}")
            return False

        try:
            # Create temporary ZIP file
            with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp_file:
                tmp_path = tmp_file.name

            # Create ZIP archive
            with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zipf:
                for root, dirs, files in os.walk(directory_path):
                    for file in files:
                        file_path = os.path.join(root, file)
                        arcname = os.path.relpath(file_path, directory_path)
                        zipf.write(file_path, arcname)

            # Upload ZIP file
            success = await self.upload_file(tmp_path, object_name, metadata)

            # Clean up temporary file
            os.unlink(tmp_path)

            if success:
                logger.info(f"Uploaded directory as ZIP to MinIO: {directory_path} -> {object_name}")

            return success

        except Exception as e:
            logger.error(f"Failed to upload directory {directory_path}: {e}")
            # Clean up temporary file if it exists
            if "tmp_path" in locals():
                try:
                    os.unlink(tmp_path)  # type: ignore
                finally:
                    pass
            return False

    async def list_objects(self, prefix: str = "", recursive: bool = False) -> List[str]:
        """List objects in storage with optional prefix.

        Args:
            prefix: Object key prefix to filter by.
            recursive: If True, list all objects under the prefix including nested
                       "subdirectories". If False (default), only list direct children.
        """
        if not self.client:
            logger.error("Storage service not initialized")
            return []

        try:
            objects = []
            for obj in self.client.list_objects(self.bucket_name, prefix=prefix, recursive=recursive):
                objects.append(obj.object_name)
            return objects
        except S3Error as e:
            logger.error(f"Failed to list objects with prefix {prefix}: {e}")
            return []

    async def delete_object(self, object_name: str) -> bool:
        """Delete an object from storage"""
        if not self.client:
            logger.error("Storage service not initialized")
            return False

        try:
            self.client.remove_object(self.bucket_name, object_name)
            logger.info(f"Deleted object from MinIO: {object_name}")
            return True
        except S3Error as e:
            logger.error(f"Failed to delete object {object_name}: {e}")
            return False

    async def get_presigned_url(self, object_name: str, expires_in_seconds: int = 3600) -> Optional[str]:
        """Generate a presigned URL for object access"""
        if not self.client:
            logger.error("Storage service not initialized")
            return None

        try:
            # Convert seconds to timedelta for MinIO client
            expires_delta = timedelta(seconds=expires_in_seconds)
            url = self.client.presigned_get_object(self.bucket_name, object_name, expires=expires_delta)
            return url
        except S3Error as e:
            logger.error(f"Failed to generate presigned URL for {object_name}: {e}")
            return None

    def get_experiment_prefix(self, experiment_id: str) -> str:
        """Get storage prefix for experiment files"""
        return f"experiments/{experiment_id}/"

    def get_data_prefix(self) -> str:
        """Get storage prefix for uploaded data files"""
        return "data/"

    async def object_exists(self, object_name: str) -> bool:
        """Check if an object exists in storage."""
        if not self.client:
            logger.error("Storage service not initialized")
            return False
        try:
            self.client.stat_object(self.bucket_name, object_name)
            return True
        except S3Error as e:
            # Not found or other error – treat non-existence on 404, log others
            if getattr(e, "code", "") not in ("NoSuchKey", "NoSuchObject", "NoSuchBucket"):
                logger.debug(f"object_exists({object_name}) -> {e}")
            return False

    async def copy_object(self, src_object: str, dst_object: str, metadata: Optional[Dict[str, str]] = None) -> bool:
        """Copy an object within the same bucket."""
        if not self.client:
            logger.error("Storage service not initialized")
            return False
        try:
            from minio.commonconfig import CopySource

            self.client.copy_object(
                self.bucket_name,
                dst_object,
                CopySource(self.bucket_name, src_object),
                metadata=metadata,
                metadata_directive="REPLACE" if metadata else None,
            )
            return True
        except S3Error as e:
            logger.error(f"Failed to copy {src_object} -> {dst_object}: {e}")
            return False

    async def upload_experiment_data(self, file_path: str, filename: str) -> Optional[str]:
        """Upload user data file for experiment and return storage path"""
        object_name = f"{self.get_data_prefix()}{filename}"
        metadata = {"source": "webui", "type": "experiment_data", "filename": filename}

        success = await self.upload_file(file_path, object_name, metadata)
        return object_name if success else None

    async def upload_experiment_files(self, directory_path: str, experiment_id: str) -> Optional[str]:
        """Upload generated experiment files as folder structure and return storage base path"""
        if not os.path.exists(directory_path):
            logger.error(f"Directory does not exist: {directory_path}")
            return None

        try:
            base_prefix = self.get_experiment_prefix(experiment_id)
            uploaded_files = []

            # Upload each file in the directory maintaining folder structure
            for root, dirs, files in os.walk(directory_path):
                for file in files:
                    file_path = os.path.join(root, file)
                    # Calculate relative path from directory_path
                    rel_path = os.path.relpath(file_path, directory_path)
                    # Convert to S3 object name with forward slashes
                    object_name = f"{base_prefix}{rel_path.replace(os.sep, '/')}"

                    metadata = {
                        "source": "folder_constructor",
                        "type": "experiment_file",
                        "experiment_id": experiment_id,
                        "original_path": rel_path,
                    }

                    success = await self.upload_file(file_path, object_name, metadata)
                    if success:
                        uploaded_files.append(object_name)
                        logger.debug(f"Uploaded experiment file: {object_name}")
                    else:
                        logger.error(f"Failed to upload experiment file: {file_path}")
                        return None

            if uploaded_files:
                logger.info(f"Uploaded {len(uploaded_files)} experiment files to MinIO for experiment {experiment_id}")
                return base_prefix  # Return the base prefix, not a single file path
            else:
                logger.warning(f"No files found to upload in directory: {directory_path}")
                return None

        except Exception as e:
            logger.error(f"Failed to upload experiment directory {directory_path}: {e}")
            return None

    async def delete_all_experiment_data(self) -> int:
        """Delete all experiment-related data from storage"""
        if not self.client:
            logger.error("Storage service not initialized")
            return 0

        try:
            deleted_count = 0

            # Delete all experiment files
            experiment_objects = self.client.list_objects(self.bucket_name, prefix="experiments/")
            for obj in experiment_objects:
                self.client.remove_object(self.bucket_name, obj.object_name)
                deleted_count += 1
                logger.debug(f"Deleted experiment object: {obj.object_name}")

            # Delete all uploaded data files
            data_objects = self.client.list_objects(self.bucket_name, prefix="data/")
            for obj in data_objects:
                self.client.remove_object(self.bucket_name, obj.object_name)
                deleted_count += 1
                logger.debug(f"Deleted data object: {obj.object_name}")

            logger.info(f"Deleted {deleted_count} objects from MinIO storage")
            return deleted_count

        except S3Error as e:
            logger.error(f"Failed to delete experiment data: {e}")
            raise
