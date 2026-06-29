"""Configuration module for the web UI."""

from .settings import (
    DEFAULT_LIMITS,
    DEFAULT_TIMEOUTS,
    INPUT_DATA_DIR,
    MASTER_API_URL,
    S3_API_URL,
    STATUS_COLORS,
    STORAGE_BUCKET_NAME,
)
from .themes import AIRI_CSS

__all__ = [
    "MASTER_API_URL",
    "S3_API_URL",
    "INPUT_DATA_DIR",
    "STORAGE_BUCKET_NAME",
    "DEFAULT_TIMEOUTS",
    "DEFAULT_LIMITS",
    "STATUS_COLORS",
    "AIRI_CSS",
]
