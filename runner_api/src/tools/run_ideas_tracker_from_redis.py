from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from gigaevo.database.redis_program_storage import RedisProgramStorage, RedisProgramStorageConfig
from gigaevo.memory.ideas_tracker.cli import (
    _apply_cli_overrides,
    _build_runtime_memory_payload,
    _write_runtime_memory_config,
)
from gigaevo.memory.ideas_tracker.ideas_tracker import IdeaTracker
from gigaevo.programs.program import EXCLUDE_STAGE_RESULTS


def _build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run Ideas Tracker against an existing Redis-backed evolution run.",
    )
    parser.add_argument("--config-path", default=None)
    parser.add_argument("--checkpoint-dir", default=None)
    parser.add_argument("--logs-dir", default=None)
    parser.add_argument(
        "--memory-write",
        action=argparse.BooleanOptionalAction,
        default=None,
    )
    parser.add_argument("--redis-host", required=True)
    parser.add_argument("--redis-port", type=int, required=True)
    parser.add_argument("--redis-db", type=int, required=True)
    parser.add_argument("--redis-prefix", required=True)
    parser.add_argument("--redis-label", default=None)
    return parser


def _build_tracker_kwargs(args: argparse.Namespace) -> dict[str, object]:
    tracker_kwargs: dict[str, object | None] = {
        "logs_dir": args.logs_dir,
        "checkpoint_dir": args.checkpoint_dir,
        "namespace": os.getenv("MEMORY_NAMESPACE"),
        "redis_prefix": args.redis_prefix,
    }
    if args.memory_write is not None:
        tracker_kwargs["memory_write_enabled"] = bool(args.memory_write)

    return {name: value for name, value in tracker_kwargs.items() if value is not None}


async def _load_programs(args: argparse.Namespace):
    storage = RedisProgramStorage(
        RedisProgramStorageConfig(
            redis_url=f"redis://{args.redis_host}:{args.redis_port}/{args.redis_db}",
            key_prefix=args.redis_prefix,
            max_connections=50,
            connection_pool_timeout=30.0,
            health_check_interval=60,
            read_only=True,
        )
    )
    try:
        await storage.__aenter__()
        return await storage.get_all(exclude=EXCLUDE_STAGE_RESULTS)
    finally:
        await storage.close()


async def _run(args: argparse.Namespace) -> int:
    config_path = Path(args.config_path) if args.config_path else None
    runtime_payload = _build_runtime_memory_payload(config_path)
    runtime_payload = _apply_cli_overrides(
        runtime_payload,
        checkpoint_dir=args.checkpoint_dir,
        memory_write=args.memory_write,
        redis_host=args.redis_host,
        redis_port=args.redis_port,
        redis_db=args.redis_db,
        redis_prefix=args.redis_prefix,
        redis_label=args.redis_label,
    )
    runtime_config_path = _write_runtime_memory_config(runtime_payload)

    previous_config_path = os.environ.get("EVO_MEMORY_CONFIG_PATH")
    os.environ["EVO_MEMORY_CONFIG_PATH"] = str(runtime_config_path)
    try:
        tracker = IdeaTracker(**_build_tracker_kwargs(args))
        programs = await _load_programs(args)
        if not programs:
            raise RuntimeError(
                f"No programs found for prefix='{args.redis_prefix}' at "
                f"redis://{args.redis_host}:{args.redis_port}/{args.redis_db}"
            )
        tracker.run(programs)
        return 0
    finally:
        if previous_config_path is None:
            os.environ.pop("EVO_MEMORY_CONFIG_PATH", None)
        else:
            os.environ["EVO_MEMORY_CONFIG_PATH"] = previous_config_path


def main() -> int:
    parser = _build_argument_parser()
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
