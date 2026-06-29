#!/usr/bin/env python3

import logging
from typing import Optional

from redis.asyncio import Redis

from ..config import load_config

logger = logging.getLogger(__name__)

_redis: Optional[Redis] = None


async def get_redis() -> Redis:
    global _redis
    if _redis is None:
        config = load_config()
        _redis = Redis.from_url(config.redis.url, decode_responses=True)
    return _redis


async def close_redis() -> None:
    global _redis
    if _redis is None:
        return
    try:
        await _redis.close()
    except Exception as exc:
        logger.warning(f"Failed to close Redis client: {exc}")
    finally:
        _redis = None
