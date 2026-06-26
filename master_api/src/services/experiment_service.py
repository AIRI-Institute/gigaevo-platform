#!/usr/bin/env python3

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import uuid4

import httpx
import redis.asyncio as redis_async
from loguru import logger
from src.config import Config

from ..config import load_config
from ..models.database import ExperimentModel
from ..models.experiment import Experiment, ExperimentCreate, ExperimentStatus
from .database_service import DatabaseService
from .kafka_service import KafkaService
from .runner_instance_service import RunnerInstanceService
from .storage_service import StorageService
from .token_metrics import aggregate_cumulative_tokens, token_scan_pattern
from .workflow_consumer import WorkflowConsumer


@dataclass(frozen=True)
class StartResult:
    ok: bool
    http_status: int
    payload: Optional[Dict[str, Any]] = None
    detail: Optional[str] = None


class ExperimentService:
    """Enhanced experiment service with database and workflow integration"""

    def __init__(
        self,
        db_service: DatabaseService,
        storage_service: StorageService,
        kafka_service: Optional[KafkaService] = None,
        workflow_consumer: Optional[WorkflowConsumer] = None,
        instance_service: Optional[RunnerInstanceService] = None,
        config: Optional[Config] = None,
    ):
        self.config = config or load_config()
        self.db_service = db_service
        self.kafka_service = kafka_service
        self.storage_service = storage_service
        self.workflow_consumer = workflow_consumer
        self.instance_service = instance_service

        # Track consecutive 404s per (runner_id, experiment_id) to avoid premature releases
        # when RunnerAPI hasn't yet created its status record right after /start.
        self._missing_status_404_counts: Dict[tuple[str, str], int] = {}
        # Lazy gigavolve-redis connection; opened on first
        # ``_read_live_evolution_metrics`` call so unit tests
        # that don't touch live data don't need the broker up.
        self._gigavolve_redis: Optional[redis_async.Redis] = None
        # One-shot warning latches so a missing / unreachable
        # gigavolve Redis surfaces in the logs ONCE (not on every
        # 2s poll) — without this, empty ``/results.metrics`` (and
        # therefore CARE's empty evolution charts) is undiagnosable.
        self._gigavolve_redis_unset_warned = False
        self._gigavolve_redis_unreachable_warned = False

    async def initialize(self):
        """Initialize the experiment service"""
        try:
            logger.info("Initializing experiment service with dependency injection")

            if not self.db_service:
                raise RuntimeError("Database service is required")
            if not self.storage_service:
                raise RuntimeError("Storage service is required")

            # Verify Kafka consistency
            if self.config.kafka.enabled:
                if not self.kafka_service:
                    logger.warning("Kafka is enabled but kafka_service not provided")
                if not self.workflow_consumer:
                    logger.warning("Kafka is enabled but workflow_consumer not provided")

            logger.info("Experiment service initialized successfully")

            # Background scheduler for queued experiments (pool orchestration).
            self._queue_scheduler_task: Optional[asyncio.Task] = None
            if bool(getattr(self.config.runner, "queueing_enabled", True)):
                self._queue_scheduler_task = asyncio.create_task(self._queue_scheduler_loop())

        except Exception as e:
            logger.error(f"Failed to initialize experiment service: {e}")
            raise

    async def cleanup(self):
        """Cleanup resources"""
        if getattr(self, "_queue_scheduler_task", None):
            self._queue_scheduler_task.cancel()
            try:
                await self._queue_scheduler_task
            except asyncio.CancelledError:
                pass
        if self._gigavolve_redis is not None:
            try:
                await self._gigavolve_redis.aclose()
            except Exception:
                pass
            self._gigavolve_redis = None

    async def _get_gigavolve_redis(self) -> Optional[redis_async.Redis]:
        """Lazy-init gigavolve Redis client."""
        if self._gigavolve_redis is not None:
            return self._gigavolve_redis
        url = getattr(self.config, "gigavolve_redis_url", None)
        if not url:
            if not self._gigavolve_redis_unset_warned:
                logger.warning(
                    "gigavolve_redis_url is not configured — live evolution "
                    "metrics (fitness history, valid/invalid program counts, "
                    "frontier programs) will be EMPTY, so /results.metrics "
                    "stays empty and CARE's EvolutionScreen charts show no "
                    "data. Set GIGAVOLVE_REDIS_URL to the runner's Redis."
                )
                self._gigavolve_redis_unset_warned = True
            return None
        try:
            self._gigavolve_redis = redis_async.from_url(url, decode_responses=True)
            return self._gigavolve_redis
        except Exception as exc:
            logger.warning(f"Failed to open gigavolve redis at {url}: {exc}")
            return None

    async def _read_live_evolution_metrics(
        self, experiment_id: str
    ) -> Dict[str, Any]:
        """Tail the runner's per-experiment metrics from
        ``redis-gigavolve`` and return a small dict the
        ``Experiment`` model can absorb.

        Runner stores each metric as a Redis LIST under
        ``<problem-uuid>:metrics:history:program_metrics:<name>``
        with entries shaped ``{"s": <seq>, "t": <ts>, "v": <val>,
        "k": "scalar"}``. ``<problem-uuid>`` is the experiment id
        without the ``exp_`` prefix (matches the
        ``problem.name=`` arg the runner CLI receives).

        Returns ``{}`` when no data is available yet.
        """
        if not experiment_id:
            return {}
        client = await self._get_gigavolve_redis()
        if client is None:
            return {}
        problem_uuid = experiment_id[4:] if experiment_id.startswith("exp_") else experiment_id
        prefix = f"{problem_uuid}:metrics:history:program_metrics:"
        # Metric base key is "fitness" everywhere in the runner (CMA_SCORE_KEY,
        # CLI defaults, and the frontier reader's metrics.get("fitness") below),
        # so the per-iteration mean lands under valid_iter_fitness_mean — NOT
        # valid_iter_val_fitness_mean (no such metric), which previously left
        # current_fitness / the curve's mean line permanently empty.
        # NOTE: confirm against a live runner — see TODO.md "Verification" task.
        keys = (
            prefix + "valid_frontier_fitness",
            prefix + "valid_iter_fitness_mean",
            prefix + "valid_program_fitness",
            prefix + "programs_valid_count",
            prefix + "programs_invalid_count",
        )
        try:
            tails = await asyncio.gather(
                *(client.lrange(k, -1, -1) for k in keys),
                return_exceptions=True,
            )
        except Exception as exc:
            logger.debug(f"gigavolve metrics tail failed for {experiment_id}: {exc}")
            return {}

        # ``return_exceptions=True`` turns a down Redis into an exception
        # per key rather than a raise. If EVERY key errored the broker is
        # unreachable — warn ONCE (loudly) so empty live metrics, and
        # therefore CARE's empty charts, are diagnosable instead of
        # silently swallowed at DEBUG.
        if tails and all(isinstance(t, Exception) for t in tails):
            if not self._gigavolve_redis_unreachable_warned:
                first = next((t for t in tails if isinstance(t, Exception)), None)
                logger.warning(
                    "gigavolve redis at %s is unreachable (%s) — live "
                    "evolution metrics will be empty for running experiments; "
                    "CARE evolution charts will show no data until it recovers.",
                    getattr(self.config, "gigavolve_redis_url", "?"),
                    type(first).__name__ if first is not None else "?",
                )
                self._gigavolve_redis_unreachable_warned = True
            return {}

        # Pull a bounded fitness *history* (best + mean per
        # iteration) so dashboards/screens can render a real
        # line plot. Cap at the most recent 200 points to keep
        # the JSON payload small; line plots over more than a
        # few hundred ticks all look the same anyway.
        history_keys = (
            prefix + "valid_frontier_fitness",
            prefix + "valid_iter_fitness_mean",
        )
        history_tails: List[Any] = []
        try:
            history_tails = list(
                await asyncio.gather(
                    *(client.lrange(k, -200, -1) for k in history_keys),
                    return_exceptions=True,
                )
            )
        except Exception:
            history_tails = []

        out: Dict[str, Any] = {}
        for key, tail in zip(keys, tails):
            if isinstance(tail, Exception) or not tail:
                continue
            try:
                entry = json.loads(tail[0])
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            seq = entry.get("s")
            value = entry.get("v")
            if key.endswith("valid_frontier_fitness"):
                if isinstance(value, (int, float)):
                    out["best_fitness"] = float(value)
                if isinstance(seq, int):
                    out["generation"] = max(int(out.get("generation", 0)), seq)
            elif key.endswith("valid_iter_fitness_mean"):
                if isinstance(seq, int):
                    out["generation"] = max(int(out.get("generation", 0)), seq)
                if isinstance(value, (int, float)):
                    out["current_fitness"] = float(value)
            elif key.endswith("valid_program_fitness"):
                if "best_fitness" not in out and isinstance(value, (int, float)):
                    out["best_fitness"] = float(value)
            elif key.endswith("programs_valid_count"):
                if isinstance(value, (int, float)) and value >= 0:
                    out["programs_valid"] = int(value)
            elif key.endswith("programs_invalid_count"):
                if isinstance(value, (int, float)) and value >= 0:
                    out["programs_invalid"] = int(value)

        # Assemble the fitness history series. Each entry is
        # ``{"generation": int, "best_fitness": float|None,
        # "current_fitness": float|None}``. Generation numbers
        # are sourced from the Redis list ``s`` field, which the
        # runner increments per iteration.
        history_by_gen: Dict[int, Dict[str, Any]] = {}

        def _absorb(entries: Any, field_name: str) -> None:
            if isinstance(entries, Exception) or not entries:
                return
            for raw in entries:
                try:
                    e = json.loads(raw)
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                seq = e.get("s")
                value = e.get("v")
                if not isinstance(seq, int):
                    continue
                slot = history_by_gen.setdefault(
                    seq, {"generation": seq}
                )
                if isinstance(value, (int, float)):
                    slot[field_name] = float(value)

        if history_tails:
            _absorb(history_tails[0], "best_fitness")
            _absorb(history_tails[1], "current_fitness")

        history = [
            history_by_gen[k] for k in sorted(history_by_gen.keys())
        ]
        if history:
            out["fitness_history"] = history

        # Frontier programs — one record per generation that
        # produced a fitness improvement, projecting the
        # embedded chain config + mutation rationale so CARE's
        # Versions tab can render real chain content instead of
        # the "not exposed by Platform" placeholder.
        try:
            frontier = await self._read_frontier_programs(
                client, problem_uuid
            )
        except Exception as exc:
            logger.debug(
                f"frontier scan failed for {experiment_id}: {exc}",
            )
            frontier = []
        if frontier:
            out["frontier_programs"] = frontier

        # Cumulative LLM token spend (best-effort) so CARE's cost meter can
        # show real evolution spend instead of staying blank. The runner
        # books tokens per agent/stage/model; ``aggregate_cumulative_tokens``
        # sums the coarsest granularity to avoid double-counting.
        try:
            token_total = await self._read_cumulative_tokens(client, problem_uuid)
        except Exception as exc:
            logger.debug(f"token scan failed for {experiment_id}: {exc}")
            token_total = 0
        if token_total:
            out["total_tokens"] = token_total
        return out

    async def _read_cumulative_tokens(self, client: Any, problem_uuid: str) -> int:
        """Sum the runner's cumulative LLM-token history (best-effort, 0 on miss)."""
        pattern = token_scan_pattern(problem_uuid)
        keys: List[str] = []
        try:
            async for key in client.scan_iter(match=pattern, count=200):
                keys.append(key)
                if len(keys) >= 500:
                    break
        except Exception:
            return 0
        if not keys:
            return 0
        try:
            tails = await asyncio.gather(
                *(client.lrange(k, -1, -1) for k in keys),
                return_exceptions=True,
            )
        except Exception:
            return 0
        items: List[tuple[str, Any]] = []
        for key, tail in zip(keys, tails):
            if isinstance(tail, Exception) or not tail:
                continue
            try:
                entry = json.loads(tail[0])
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            value = entry.get("v")
            if isinstance(value, (int, float)):
                items.append((key, value))
        return aggregate_cumulative_tokens(items)

    async def _read_frontier_programs(
        self, client: Any, problem_uuid: str
    ) -> List[Dict[str, Any]]:
        """Scan ``<uuid>:program:*`` and return one record per
        generation that produced the highest fitness so far."""
        keys: List[str] = []
        try:
            async for key in client.scan_iter(
                match=f"{problem_uuid}:program:*",
                count=200,
            ):
                keys.append(key)
                if len(keys) >= 500:
                    break
        except Exception:
            keys = []
        if not keys:
            return []
        try:
            raws = await client.mget(*keys)
        except Exception:
            return []
        import re

        chain_pat = re.compile(
            r'BASE_CHAIN_CONFIG\s*:\s*str\s*=\s*"""(.*?)"""',
            re.DOTALL,
        )

        best_by_gen: Dict[int, Dict[str, Any]] = {}
        for raw in raws:
            if not raw:
                continue
            try:
                p = json.loads(raw)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            metrics = p.get("metrics") or {}
            fitness = metrics.get("fitness")
            if not isinstance(fitness, (int, float)):
                continue
            if fitness <= -100:
                continue
            lineage = p.get("lineage") or {}
            gen = lineage.get("generation")
            if not isinstance(gen, int):
                gen = p.get("iteration")
            if not isinstance(gen, int):
                continue
            existing = best_by_gen.get(gen)
            if existing is not None and existing["fitness"] >= float(fitness):
                continue
            chain_config: Optional[Dict[str, Any]] = None
            code = p.get("code") or ""
            if isinstance(code, str):
                m = chain_pat.search(code)
                if m:
                    try:
                        chain_config = json.loads(m.group(1).strip())
                    except (TypeError, ValueError, json.JSONDecodeError):
                        chain_config = None
            mutation_meta = (p.get("metadata") or {}).get("mutation_output") or {}
            best_by_gen[gen] = {
                "generation": gen,
                "program_id": p.get("id"),
                "fitness": float(fitness),
                "chain_config": chain_config,
                "mutation_summary": mutation_meta.get("justification") or "",
                "mutation_changes": mutation_meta.get("changes") or [],
            }

        if not best_by_gen:
            return []
        ordered = [best_by_gen[g] for g in sorted(best_by_gen.keys())]
        frontier: List[Dict[str, Any]] = []
        last_best = float("-inf")
        for row in ordered:
            if row["fitness"] > last_best:
                frontier.append(row)
                last_best = row["fitness"]
        return frontier
        # NOTE: Do not cleanup shared dependencies here; ServiceManager owns their lifecycle.

    async def create_experiment(self, experiment_create: ExperimentCreate) -> Experiment:
        """Create a new experiment"""
        try:
            logger.info(f"Creating new experiment: {experiment_create.name}")

            # Generate experiment ID with "exp_" prefix
            experiment_id = f"exp_{uuid4()}"

            # Create experiment in database
            experiment_model = await self.db_service.create_experiment(experiment_create, experiment_id)

            # Convert to API model
            experiment = self._model_to_api(experiment_model)

            # Publish experiment config to Kafka if enabled
            if self.kafka_service:
                await self.kafka_service.publish_experiment_config(
                    str(experiment.id),
                    experiment.config.model_dump(),
                )
                logger.info(f"Published experiment config for {experiment.id}")
            else:
                logger.info(f"Kafka disabled, skipping config publish for {experiment.id}")

            return experiment

        except Exception as e:
            logger.error(f"Failed to create experiment: {e}")
            raise

    async def list_experiments(
        self, status: Optional[ExperimentStatus] = None, limit: int = 100, offset: int = 0
    ) -> List[Experiment]:
        """List experiments"""
        try:
            status_str = status.value if status else None
            experiment_models = await self.db_service.list_experiments(status_str, limit, offset)

            # Keep statuses fresh for the UI:
            # - Prefer RunnerAPI for RUNNING experiments (Kafka/Redis cache can be stale or absent)
            # - Fall back to cached Kafka/Redis sync for non-running experiments
            try:
                running = [
                    m
                    for m in experiment_models
                    if str(getattr(m, "status", "") or "").lower() == ExperimentStatus.RUNNING.value
                ]
                if running:
                    sem = asyncio.Semaphore(10)

                    async def _refresh_one(m: ExperimentModel) -> None:
                        async with sem:
                            try:
                                payload = await self.get_experiment_status(str(getattr(m, "id")))
                                if payload and payload.get("status"):
                                    m.status = str(payload["status"])
                                    if payload.get("error_message") is not None:
                                        m.error_message = payload.get("error_message")
                            except Exception:
                                pass

                    await asyncio.gather(*[asyncio.create_task(_refresh_one(m)) for m in running])
            except Exception:
                pass

            # Best-effort cached sync (may update queued/preparing/etc.)
            for model in experiment_models:
                await self._sync_status_from_redis(model, update_in_place=True)

            await self._merge_live_metrics_for_models(experiment_models)

            return [self._model_to_api(model) for model in experiment_models]
        except Exception as e:
            logger.error(f"Failed to list experiments: {e}")
            raise

    async def _merge_live_metrics_for_models(
        self, models: List[ExperimentModel]
    ) -> None:
        """Tail gigavolve Redis for every model that may still
        have live data and merge values into ``model.metrics``
        in place."""
        active = {"running", "preparing", "initializing", "dispatching",
                  "queued", "completed", "failed"}
        targets = [m for m in models if str(getattr(m, "status", "") or "").lower() in active]
        if not targets:
            return
        sem = asyncio.Semaphore(8)

        async def _fill(m: ExperimentModel) -> None:
            async with sem:
                try:
                    live = await self._read_live_evolution_metrics(str(getattr(m, "id")))
                except Exception:
                    return
                if not live:
                    return
                base = dict(m.metrics or {})
                base.update(live)
                m.metrics = base

        await asyncio.gather(*[asyncio.create_task(_fill(m)) for m in targets])

    async def get_experiment(self, experiment_id: str) -> Optional[Experiment]:
        """Get experiment by ID"""
        try:
            experiment_model = await self.db_service.get_experiment(experiment_id)
            if not experiment_model:
                return None

            # Sync status from Redis to database (runner API is source of truth for running experiments)
            if await self._sync_status_from_redis(experiment_model):
                # Refresh model from database after sync
                experiment_model = await self.db_service.get_experiment(experiment_id)
                if not experiment_model:
                    return None

            await self._merge_live_metrics_for_models([experiment_model])
            return self._model_to_api(experiment_model)

        except Exception as e:
            logger.error(f"Failed to get experiment {experiment_id}: {e}")
            raise

    async def get_experiment_status(self, experiment_id: str) -> Optional[Dict[str, Any]]:
        """Get experiment status"""
        try:
            experiment_model = await self.db_service.get_experiment(experiment_id)
            if not experiment_model:
                return None

            assigned_runner_id = (experiment_model.config or {}).get("assigned_runner_id")
            terminal_statuses = {
                ExperimentStatus.COMPLETED.value,
                ExperimentStatus.FAILED.value,
                ExperimentStatus.TERMINATED.value,
                ExperimentStatus.CANCELLED.value,
            }
            grace = int(getattr(self.config.runner, "missing_status_grace_seconds", 30) or 30)
            grace = max(0, grace)
            max_404s = int(getattr(self.config.runner, "status_404_release_threshold", 2) or 2)
            max_404s = max(1, max_404s)

            # Prefer querying the assigned runner directly (pool source of truth)
            if assigned_runner_id and self.instance_service:
                inst = await self.instance_service.get_instance(str(assigned_runner_id))
                if inst:
                    runner_base = inst.endpoint_url.rstrip("/")
                    status_url = f"{runner_base}/api/v1/experiments/{experiment_id}/status"
                    try:
                        async with httpx.AsyncClient(timeout=15.0) as client:
                            resp = await client.get(status_url)
                        if resp.status_code == 200:
                            payload = resp.json()
                            runner_status = str(payload.get("status", "")).lower()
                            runner_error = payload.get("error_message")
                            runner_status_message = payload.get("status_message")
                            _ = self._missing_status_404_counts.pop((str(assigned_runner_id), str(experiment_id)), None)
                            # If runner reports a terminal status, reflect it in DB and release runner.
                            if runner_status in terminal_statuses:
                                update_kwargs: Dict[str, Any] = {}
                                # Capture messages
                                if (
                                    runner_status == ExperimentStatus.FAILED.value
                                    and runner_error
                                    and not getattr(experiment_model, "error_message", None)
                                ):
                                    update_kwargs["error_message"] = str(runner_error)
                                if runner_status == ExperimentStatus.TERMINATED.value and runner_status_message:
                                    update_kwargs["status_message"] = str(runner_status_message)
                                    update_kwargs["error_message"] = None
                                if runner_status != experiment_model.status or update_kwargs:
                                    await self.db_service.update_experiment_status(
                                        experiment_id, runner_status, **update_kwargs
                                    )
                                    experiment_model = await self.db_service.get_experiment(experiment_id)
                            if runner_status in terminal_statuses:
                                await self.instance_service.release_runner_by_id_if_experiment(
                                    str(assigned_runner_id), experiment_id
                                )
                        elif resp.status_code == 404:
                            # Hardening: RunnerAPI may return 404 briefly right after /start.
                            within_grace = False
                            try:
                                started_at = getattr(experiment_model, "started_at", None)
                                if started_at and grace > 0:
                                    age = (datetime.now() - started_at).total_seconds()
                                    within_grace = age >= 0 and age < grace
                            except Exception:
                                within_grace = False

                            if within_grace:
                                logger.debug(
                                    f"{experiment_id}: runner {assigned_runner_id} returned 404 for /status; "
                                    f"skipping release (within grace {grace}s)"
                                )
                            else:
                                key = (str(assigned_runner_id), str(experiment_id))
                                self._missing_status_404_counts[key] = self._missing_status_404_counts.get(key, 0) + 1
                                if self._missing_status_404_counts[key] >= max_404s:
                                    _ = self._missing_status_404_counts.pop(key, None)
                                    await self.instance_service.release_runner_by_id_if_experiment(
                                        str(assigned_runner_id), experiment_id
                                    )
                                else:
                                    logger.debug(
                                        f"{experiment_id}: runner {assigned_runner_id} returned 404 for /status; "
                                        f"not releasing yet ({self._missing_status_404_counts[key]}/{max_404s})"
                                    )
                    except Exception:
                        # Best-effort: fall back to cached sync below
                        pass

            # Sync status from cached Redis/Kafka status if enabled (best-effort fallback)
            if await self._sync_status_from_redis(experiment_model):
                experiment_model = await self.db_service.get_experiment(experiment_id)

            # If DB is terminal, also release runner (covers cases where status was synced without polling)
            if (
                self.instance_service
                and assigned_runner_id
                and str(experiment_model.status).lower() in terminal_statuses
            ):
                await self.instance_service.release_runner_by_id_if_experiment(str(assigned_runner_id), experiment_id)

            # Return current status from database (now synced with Redis)
            return {
                "experiment_id": experiment_id,
                "status": experiment_model.status,
                "updated_at": experiment_model.updated_at.isoformat(),
                "runner_id": experiment_model.config.get("assigned_runner_id"),
                "status_message": getattr(experiment_model, "status_message", None),
                "error_message": experiment_model.error_message,
            }

        except Exception as e:
            logger.error(f"Failed to get experiment status {experiment_id}: {e}")
            raise

    async def get_experiment_results(self, experiment_id: str) -> Optional[Dict[str, Any]]:
        """Get experiment results (live during RUNNING, final when COMPLETED)."""
        try:
            experiment_model = await self.db_service.get_experiment(experiment_id)
            if not experiment_model:
                return None

            # Return current metrics regardless of status; include status for the client
            # Compute S3 object paths under experiments_results/
            base_prefix = f"experiments_results/{experiment_id}/"
            plot_object = f"{base_prefix}metrics_plot.png"
            program_object = f"{base_prefix}best_program.py"
            validation_object = f"{base_prefix}validate.py"
            archive_object = f"{base_prefix}results.zip"
            best_chain_config_object = f"{base_prefix}best_chain_config.json"
            initial_chain_config_object = f"{base_prefix}initial_chain_config.json"

            # Optional presigned URLs for convenience (short expiration)
            plot_url = await self.storage_service.get_presigned_url(plot_object, expires_in_seconds=120)
            program_url = await self.storage_service.get_presigned_url(program_object, expires_in_seconds=120)
            archive_url = await self.storage_service.get_presigned_url(archive_object, expires_in_seconds=120)

            artifacts: Dict[str, Any] = {
                "plot_image_s3": plot_object,
                "best_program_s3": program_object,
                "validation_s3": validation_object,
                "archive_s3": archive_object,
                "plot_url": plot_url,
                "best_program_url": program_url,
                "archive_url": archive_url,
            }

            # Include chain config artifacts if they exist (chain experiments)
            if await self.storage_service.object_exists(best_chain_config_object):
                artifacts["best_chain_config_s3"] = best_chain_config_object
            if await self.storage_service.object_exists(initial_chain_config_object):
                artifacts["initial_chain_config_s3"] = initial_chain_config_object

            # Merge live runner metrics (current generation +
            # best fitness, pulled from gigavolve Redis) into the
            # response so the dashboard/EvolutionScreen sees real
            # progress mid-run. Falls back to the DB ``metrics``
            # dict (always empty for chain experiments until
            # completion) when the live read returns nothing.
            db_metrics: Dict[str, Any] = dict(experiment_model.metrics or {})
            metrics: Dict[str, Any] = dict(db_metrics)
            try:
                live = await self._read_live_evolution_metrics(experiment_id)
            except Exception:
                live = {}
            metrics.update(live)

            # Diagnostic so clients (CARE EvolutionScreen) can tell WHY
            # the charts may be empty: "gigavolve_redis" = live data,
            # "db" = only the persisted summary, "none" = nothing yet
            # (engine Redis unreachable/unset — see the one-shot warnings
            # logged in _read_live_evolution_metrics).
            if live:
                metrics_source = "gigavolve_redis"
            elif db_metrics:
                metrics_source = "db"
            else:
                metrics_source = "none"

            payload: Dict[str, Any] = {
                "experiment_id": experiment_id,
                "status": experiment_model.status,
                "metrics": metrics,
                "metrics_source": metrics_source,
                "generation": metrics.get("generation"),
                "best_fitness": metrics.get("best_fitness"),
                "completed_at": experiment_model.completed_at.isoformat() if experiment_model.completed_at else None,
                "runner_id": experiment_model.config.get("assigned_runner_id"),
                "artifacts": artifacts,
            }

            # Include best chain config from DB best_result if available
            best_result = experiment_model.best_result or {}
            if best_result.get("best_chain_config"):
                payload["best_chain_config"] = best_result["best_chain_config"]
            if best_result.get("initial_chain_config"):
                payload["initial_chain_config"] = best_result["initial_chain_config"]

            return payload

        except Exception as e:
            logger.error(f"Failed to get experiment results {experiment_id}: {e}")
            raise

    async def start_experiment_forward(
        self, experiment_id: str, instance_id: Optional[str] = None, *, from_scheduler: bool = False
    ) -> StartResult:
        """Start experiment execution by directly calling RunnerAPI endpoints step by step"""
        allocated_runner_id: Optional[str] = None
        try:
            experiment_model = await self.db_service.get_experiment(experiment_id)
            if not experiment_model:
                msg = f"Experiment {experiment_id} not found"
                logger.warning(msg)
                return StartResult(ok=False, http_status=404, detail=msg)

            status = str(experiment_model.status or "").lower()

            # Prerequisite: experiment files must be prepared (experiment_files_path present in config)
            cfg = dict(getattr(experiment_model, "config", {}) or {})
            if not cfg.get("experiment_files_path"):
                msg = "Experiment files are not ready yet (missing experiment_files_path)"
                logger.warning(f"{experiment_id}: {msg}")
                return StartResult(ok=False, http_status=409, detail=msg)

            if status == ExperimentStatus.PREPARATION_FAILED.value:
                msg = f"Experiment files failed to prepare: {experiment_model.error_message or ''}".strip()
                return StartResult(ok=False, http_status=409, detail=msg or "Experiment preparation failed")

            # Idempotent start for user-initiated calls
            if not from_scheduler and status in {
                ExperimentStatus.QUEUED.value,
                ExperimentStatus.DISPATCHING.value,
            }:
                return StartResult(
                    ok=True,
                    http_status=202,
                    payload={
                        "message": "Experiment already queued",
                        "experiment_id": experiment_id,
                        "status": status,
                        "status_message": getattr(experiment_model, "status_message", None),
                    },
                )

            if status in {
                ExperimentStatus.PENDING.value,
                ExperimentStatus.INITIALIZING.value,
                ExperimentStatus.PREPARING.value,
                ExperimentStatus.RUNNING.value,
                ExperimentStatus.CANCELLED.value,
            }:
                msg = f"Cannot start experiment in status '{status}'"
                logger.warning(f"{experiment_id}: {msg}")
                return StartResult(ok=False, http_status=409, detail=msg)

            # Find available instance or use specified one
            if not self.instance_service:
                msg = "Instance service not available"
                logger.error(msg)
                return StartResult(ok=False, http_status=503, detail=msg)

            # Allocate a runner from the pool (binary READY/BUSY).
            # If instance_id is provided, try to allocate that specific runner.
            if instance_id:
                instance = await self.instance_service.allocate_specific_runner(str(instance_id), experiment_id)
            else:
                instance = await self.instance_service.allocate_runner(experiment_id)
            if not instance:
                waiting_msg = "Waiting for runner capacity"
                await self.db_service.update_experiment(
                    experiment_id,
                    status=ExperimentStatus.QUEUED.value,
                    status_message=waiting_msg,
                    error_message=None,
                )
                return StartResult(
                    ok=True,
                    http_status=202,
                    payload={
                        "message": "Experiment queued",
                        "experiment_id": experiment_id,
                        "status": ExperimentStatus.QUEUED.value,
                        "status_message": waiting_msg,
                    },
                )
            instance_id = instance.id
            allocated_runner_id = str(instance_id)

            logger.info(f"Starting experiment {experiment_id} on instance {instance_id}")

            # Step 1: Update status to initializing
            await self.db_service.update_experiment(
                experiment_id,
                status=ExperimentStatus.INITIALIZING.value,
                started_at=datetime.now(),
                status_message=None,
                error_message=None,
            )
            logger.info(f"Updated experiment {experiment_id} status to initializing")

            # Step 2: Call RunnerAPI initialize endpoint
            runner_base_url = instance.endpoint_url.rstrip("/")
            initialize_url = f"{runner_base_url}/api/v1/experiments/{experiment_id}/initialize"

            # Prepare experiment config for RunnerAPI
            experiment_config = {
                "task_type": experiment_model.config.get("parameters", {}).get("task_type", "classification"),
                "task_description": experiment_model.config.get("description", ""),
                "dataset_path": experiment_model.data_path or "",
                "target_column": experiment_model.config.get("parameters", {}).get("target_column"),
                "n_classes": experiment_model.config.get("parameters", {}).get("n_classes"),
                "n_clusters": experiment_model.config.get("parameters", {}).get("n_clusters"),
                "llm_model": experiment_model.config.get("llm_model", "local-inference"),
                "prompt_llm_model": experiment_model.config.get("prompt_llm_model"),
                # CARL: chain_llm_model stored in parameters by carl-chains route
                "chain_llm_model": experiment_model.config.get("parameters", {}).get("chain_llm_model"),
                "max_iterations": experiment_model.config.get("max_iterations", 100),
                "dataset_size": experiment_model.config.get("dataset_size"),
                "test_size": experiment_model.config.get("test_size"),
                "enable_memory": experiment_model.config.get("parameters", {}).get("enable_memory", False),
                "memory_namespace": experiment_model.config.get("parameters", {}).get("memory_namespace"),
            }

            # Remove None values from config
            experiment_config = {k: v for k, v in experiment_config.items() if v is not None}

            # Initialize experiment on RunnerAPI with small built-in retry to handle
            # transient DNS / connectivity issues (e.g., name resolution right after startup).
            try:
                init_max_attempts = 3
                last_init_error: Optional[Exception] = None

                for attempt in range(1, init_max_attempts + 1):
                    try:
                        async with httpx.AsyncClient(timeout=30.0) as client:
                            logger.info(f"Sending initialize request to {initialize_url} (attempt {attempt})")
                            init_response = await client.post(initialize_url, json=experiment_config)

                        if init_response.status_code != 200:
                            error_msg = (
                                f"Failed to initialize experiment on RunnerAPI: "
                                f"{init_response.status_code} - {init_response.text}"
                            )
                            logger.error(error_msg)
                            await self.db_service.update_experiment(
                                experiment_id,
                                status=ExperimentStatus.FAILED.value,
                                error_message=error_msg,
                                status_message=None,
                            )
                            # Release allocated runner on failure
                            if allocated_runner_id and self.instance_service:
                                await self.instance_service.release_runner_by_id_if_experiment(
                                    allocated_runner_id, experiment_id
                                )
                            return StartResult(ok=False, http_status=502, detail=error_msg)

                        _ = init_response.json()
                        logger.info(f"Successfully initialized experiment {experiment_id} on RunnerAPI")
                        last_init_error = None
                        break
                    except httpx.RequestError as e:
                        last_init_error = e
                        logger.warning(
                            f"Initialize request to RunnerAPI at {runner_base_url} failed on attempt {attempt}: {e}"
                        )
                        if attempt < init_max_attempts:
                            # Backoff before retrying
                            await asyncio.sleep(1 * attempt)

                if last_init_error:
                    error_msg = f"Failed to connect to RunnerAPI at {runner_base_url}: {last_init_error}"
                    logger.error(error_msg)
                    await self.db_service.update_experiment(
                        experiment_id,
                        status=ExperimentStatus.FAILED.value,
                        error_message=error_msg,
                        status_message=None,
                    )
                    # Release allocated runner on failure
                    if allocated_runner_id and self.instance_service:
                        await self.instance_service.release_runner_by_id_if_experiment(
                            allocated_runner_id, experiment_id
                        )
                    return StartResult(ok=False, http_status=502, detail=error_msg)

            except Exception as e:
                # Catch-all to avoid leaving experiment in inconsistent state
                error_msg = f"Error during initialization on RunnerAPI at {runner_base_url}: {e}"
                logger.error(error_msg)
                await self.db_service.update_experiment(
                    experiment_id,
                    status=ExperimentStatus.FAILED.value,
                    error_message=error_msg,
                    status_message=None,
                )
                if allocated_runner_id and self.instance_service:
                    await self.instance_service.release_runner_by_id_if_experiment(allocated_runner_id, experiment_id)
                return StartResult(ok=False, http_status=502, detail=error_msg)

            # Step 3: Update status to preparing
            await self.db_service.update_experiment(experiment_id, status=ExperimentStatus.PREPARING.value)
            logger.info(f"Updated experiment {experiment_id} status to preparing")

            # Step 4: Call RunnerAPI start endpoint
            start_url = f"{runner_base_url}/api/v1/experiments/{experiment_id}/start"

            # Start experiment on RunnerAPI with small built-in retry similar to initialize step.
            try:
                start_max_attempts = 3
                last_start_error: Optional[Exception] = None

                for attempt in range(1, start_max_attempts + 1):
                    try:
                        async with httpx.AsyncClient(timeout=30.0) as client:
                            logger.info(f"Sending start request to {start_url} (attempt {attempt})")
                            start_response = await client.post(start_url)

                        if start_response.status_code != 200:
                            error_msg = (
                                f"Failed to start experiment on RunnerAPI: "
                                f"{start_response.status_code} - {start_response.text}"
                            )
                            logger.error(error_msg)
                            await self.db_service.update_experiment(
                                experiment_id,
                                status=ExperimentStatus.FAILED.value,
                                error_message=error_msg,
                                status_message=None,
                            )
                            # Release allocated runner on failure
                            if allocated_runner_id and self.instance_service:
                                await self.instance_service.release_runner_by_id_if_experiment(
                                    allocated_runner_id, experiment_id
                                )
                            return StartResult(ok=False, http_status=502, detail=error_msg)

                        _ = start_response.json()
                        logger.info(f"Successfully started experiment {experiment_id} on RunnerAPI")
                        last_start_error = None
                        break
                    except httpx.RequestError as e:
                        last_start_error = e
                        logger.warning(
                            f"Start request to RunnerAPI at {runner_base_url} failed on attempt {attempt}: {e}"
                        )
                        if attempt < start_max_attempts:
                            await asyncio.sleep(1 * attempt)

                if last_start_error:
                    error_msg = f"Failed to connect to RunnerAPI at {runner_base_url}: {last_start_error}"
                    logger.error(error_msg)
                    await self.db_service.update_experiment(
                        experiment_id,
                        status=ExperimentStatus.FAILED.value,
                        error_message=error_msg,
                        status_message=None,
                    )
                    # Release allocated runner on failure
                    if allocated_runner_id and self.instance_service:
                        await self.instance_service.release_runner_by_id_if_experiment(
                            allocated_runner_id, experiment_id
                        )
                    return StartResult(ok=False, http_status=502, detail=error_msg)

            except Exception as e:
                error_msg = f"Error during start on RunnerAPI at {runner_base_url}: {e}"
                logger.error(error_msg)
                await self.db_service.update_experiment(
                    experiment_id,
                    status=ExperimentStatus.FAILED.value,
                    error_message=error_msg,
                    status_message=None,
                )
                if allocated_runner_id and self.instance_service:
                    await self.instance_service.release_runner_by_id_if_experiment(allocated_runner_id, experiment_id)
                return StartResult(ok=False, http_status=502, detail=error_msg)

            # Step 5: Update status to running and assign runner
            await self.db_service.update_experiment(
                experiment_id, status=ExperimentStatus.RUNNING.value, status_message=None
            )

            logger.info(f"Successfully started experiment {experiment_id} on instance {instance_id}")
            return StartResult(
                ok=True,
                http_status=200,
                payload={"message": "Experiment started", "experiment_id": experiment_id},
            )

        except Exception as e:
            error_msg = f"Failed to start experiment {experiment_id}: {e}"
            logger.error(error_msg)
            # Update experiment status to failed on any error
            try:
                await self.db_service.update_experiment(
                    experiment_id,
                    status=ExperimentStatus.FAILED.value,
                    error_message=error_msg,
                    status_message=None,
                )
            except Exception as db_error:
                logger.error(f"Failed to update experiment status to failed: {db_error}")
            # Rollback/release runner if we allocated one
            try:
                if allocated_runner_id and self.instance_service:
                    await self.instance_service.release_runner_by_id_if_experiment(allocated_runner_id, experiment_id)
            except Exception:
                pass
            return StartResult(ok=False, http_status=500, detail=error_msg)

    async def stop_experiment(self, experiment_id: str) -> bool:
        """Stop experiment execution"""
        try:
            experiment_model = await self.db_service.get_experiment(experiment_id)
            if not experiment_model:
                logger.warning(f"Experiment {experiment_id} not found")
                return False

            if experiment_model.status != "running":
                logger.warning(f"Cannot stop experiment {experiment_id} in status {experiment_model.status}")
                return False

            # Update status to cancelled
            success = await self.db_service.update_experiment_status(experiment_id, "cancelled")

            if success and self.kafka_service:
                assigned_runner_id = experiment_model.config.get("assigned_runner_id")
                if assigned_runner_id:
                    # Send stop command to runner
                    await self.kafka_service.publish_experiment_stop_command(experiment_id, assigned_runner_id)
                logger.info(f"Published stop command for experiment {experiment_id}")

            logger.info(f"Stopped experiment {experiment_id}")
            return True

        except Exception as e:
            logger.error(f"Failed to stop experiment {experiment_id}: {e}")
            return False

    async def stop_experiment_forward(self, experiment_id: str, instance_id: Optional[str] = None) -> tuple[bool, str]:
        """Stop experiment execution by directly calling RunnerAPI."""
        try:
            # Resolve experiment and instance
            experiment_model = await self.db_service.get_experiment(experiment_id)
            if not experiment_model:
                error_msg = f"Experiment {experiment_id} not found"
                logger.warning(error_msg)
                return False, error_msg

            status = str(experiment_model.status or "").lower()

            # If not running, treat stop as cancel (remove from queue/scheduler eligibility).
            if status != ExperimentStatus.RUNNING.value:
                await self.db_service.update_experiment(
                    experiment_id,
                    status=ExperimentStatus.CANCELLED.value,
                    status_message=None,
                )
                # Best-effort release if somehow assigned
                assigned_runner_id = (experiment_model.config or {}).get("assigned_runner_id")
                if assigned_runner_id and self.instance_service:
                    await self.instance_service.release_runner_by_id_if_experiment(
                        str(assigned_runner_id), experiment_id
                    )
                return True, ""

            if not self.instance_service:
                error_msg = "Instance service not available"
                logger.error(error_msg)
                return False, error_msg

            # Prefer explicitly provided instance_id, else use assigned runner from experiment config
            target_instance_id = instance_id or experiment_model.config.get("assigned_runner_id")
            if not target_instance_id:
                error_msg = "No assigned runner instance found for this experiment"
                logger.error(error_msg)
                return False, error_msg

            instance = await self.instance_service.get_instance(target_instance_id)
            if not instance:
                error_msg = f"Runner instance '{target_instance_id}' not found"
                logger.error(error_msg)
                return False, error_msg

            runner_base_url = instance.endpoint_url.rstrip("/")
            stop_url = f"{runner_base_url}/api/v1/experiments/{experiment_id}/stop"

            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    logger.info(f"Sending stop request to {stop_url}")
                    resp = await client.post(stop_url)
                    if resp.status_code != 200:
                        error_msg = f"Failed to stop experiment on RunnerAPI: {resp.status_code} - {resp.text}"
                        logger.error(error_msg)
                        return False, error_msg
            except httpx.RequestError as e:
                error_msg = f"Failed to connect to RunnerAPI at {runner_base_url}: {e}"
                logger.error(error_msg)
                return False, error_msg

            # Reflect cancelled state in DB
            await self.db_service.update_experiment_status(experiment_id, ExperimentStatus.CANCELLED.value)
            # Release runner back to pool (best-effort, ownership-checked)
            await self.instance_service.release_runner_by_id_if_experiment(target_instance_id, experiment_id)
            logger.info(
                f"Successfully stopped experiment {experiment_id} via RunnerAPI on instance {target_instance_id}"
            )
            return True, ""

        except Exception as e:
            error_msg = f"Failed to stop experiment {experiment_id}: {e}"
            logger.error(error_msg)
            return False, error_msg

    async def _queue_scheduler_loop(self) -> None:
        """Background loop to start queued experiments when capacity is available."""
        poll = int(getattr(self.config.runner, "queue_poll_interval_seconds", 3) or 3)
        poll = max(1, poll)
        ttl = int(getattr(self.config.runner, "dispatching_ttl_seconds", 60) or 60)
        ttl = max(10, ttl)
        logger.info(f"Starting experiment queue scheduler (poll={poll}s, dispatching_ttl={ttl}s)")
        while True:
            try:
                # Recover stale dispatching -> queued
                try:
                    await self.db_service.recover_stale_dispatching(ttl_seconds=ttl)
                except Exception:
                    pass

                # Best UX: do not flip QUEUED->DISPATCHING unless there is runner capacity.
                try:
                    if not await self.db_service.has_ready_runner_capacity():
                        await asyncio.sleep(poll)
                        continue
                except Exception:
                    # If capacity check fails, fall back to previous behavior
                    pass

                claimed = await self.db_service.claim_next_queued_for_dispatching()
                if not claimed:
                    await asyncio.sleep(poll)
                    continue

                exp_id = str(claimed.id)

                # Enforce prerequisites (files ready) at scheduler time too
                cfg = dict(getattr(claimed, "config", {}) or {})
                if not cfg.get("experiment_files_path"):
                    await self.db_service.update_experiment(
                        exp_id,
                        status=ExperimentStatus.PREPARATION_FAILED.value,
                        error_message="Experiment files not ready (missing experiment_files_path)",
                        status_message=None,
                    )
                    continue

                res = await self.start_experiment_forward(exp_id, from_scheduler=True)
                if res.ok and res.http_status == 202:
                    # Still no runners available -> back to queued
                    await self.db_service.update_experiment(
                        exp_id,
                        status=ExperimentStatus.QUEUED.value,
                        status_message="Waiting for runner capacity",
                    )
                await asyncio.sleep(0)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Queue scheduler tick error: {e}")
                await asyncio.sleep(poll)

    async def upload_experiment_data(self, experiment_id: str, file_path: str, filename: str) -> bool:
        """Upload data file for experiment"""
        try:
            # Upload file to storage
            storage_path = await self.storage_service.upload_experiment_data(file_path, filename)
            if not storage_path:
                logger.error(f"Failed to upload data file {filename}")
                return False

            # Update experiment with data path
            success = await self.db_service.update_experiment(experiment_id, data_path=storage_path)
            if success:
                logger.info(f"Uploaded data file for experiment {experiment_id}: {storage_path}")
            else:
                logger.error("Failed to update experiment data path")

            return success

        except Exception as e:
            logger.error(f"Failed to upload experiment data for {experiment_id}: {e}")
            return False

    async def drop_all_experiments(self) -> Dict[str, Any]:
        """Drop all experiments and their data"""
        try:
            logger.info("Starting to drop all experiments and data")

            deleted_experiments = 0
            deleted_objects = 0
            stopped_runner_ids: set[str] = set()
            stop_failed_runner_ids: set[str] = set()
            runner_release_failed = False
            experiments_deletion_ok = True
            storage_deletion_ok = True
            errors: List[str] = []

            # Best-effort stop/cancel before wiping.
            try:
                if self.db_service:
                    # Fetch all experiments (page through to avoid hard-coded limits).
                    all_models: List[ExperimentModel] = []
                    limit = 1000
                    offset = 0
                    while True:
                        batch = await self.db_service.list_experiments(status=None, limit=limit, offset=offset)
                        if not batch:
                            break
                        all_models.extend(batch)
                        offset += len(batch)
                        if len(batch) < limit:
                            break

                    inflight_statuses = {
                        ExperimentStatus.PENDING.value,
                        ExperimentStatus.QUEUED.value,
                        ExperimentStatus.DISPATCHING.value,
                        ExperimentStatus.INITIALIZING.value,
                        ExperimentStatus.PREPARING.value,
                        ExperimentStatus.PREPARED.value,
                        ExperimentStatus.DEPLOYED.value,
                    }

                    sem = asyncio.Semaphore(5)

                    async def _stop_one(exp_id: str, runner_id: Optional[str]) -> None:
                        nonlocal stopped_runner_ids, stop_failed_runner_ids
                        async with sem:
                            try:
                                ok, err = await asyncio.wait_for(
                                    self.stop_experiment_forward(exp_id),
                                    timeout=30,
                                )
                                if ok:
                                    if runner_id:
                                        stopped_runner_ids.add(str(runner_id))
                                else:
                                    if runner_id:
                                        stop_failed_runner_ids.add(str(runner_id))
                                    logger.warning(f"Drop-all stop failed for {exp_id}: {err}")
                            except Exception as e:
                                if runner_id:
                                    stop_failed_runner_ids.add(str(runner_id))
                                logger.warning(f"Drop-all stop exception for {exp_id}: {e}")

                    stop_tasks: List[asyncio.Task] = []
                    for m in all_models:
                        st = str(getattr(m, "status", "") or "").lower()
                        exp_id = str(getattr(m, "id"))
                        assigned_runner_id = (getattr(m, "config", {}) or {}).get("assigned_runner_id")

                        if st == ExperimentStatus.RUNNING.value:
                            stop_tasks.append(asyncio.create_task(_stop_one(exp_id, assigned_runner_id)))
                            continue

                        # Cancel any inflight non-running statuses to avoid stale UI/DB state
                        # and best-effort release runners if assigned.
                        if st in inflight_statuses:
                            try:
                                await self.db_service.update_experiment_status(exp_id, ExperimentStatus.CANCELLED.value)
                            except Exception:
                                pass
                            if assigned_runner_id and self.instance_service:
                                try:
                                    await self.instance_service.release_runner_by_id_if_experiment(
                                        str(assigned_runner_id), exp_id
                                    )
                                except Exception:
                                    runner_release_failed = True
                                    pass

                    if stop_tasks:
                        await asyncio.gather(*stop_tasks, return_exceptions=True)

            except Exception as e:
                # Best-effort only; proceed with wipe regardless
                msg = f"Drop-all: pre-wipe stop/cancel phase failed/skipped: {e}"
                logger.warning(msg)
                runner_release_failed = True
                errors.append(msg)

            # Delete all experiments from database
            if self.db_service:
                try:
                    deleted_experiments = await self.db_service.delete_all_experiments()
                except Exception as e:
                    experiments_deletion_ok = False
                    msg = f"Drop-all: experiments DB deletion failed: {e}"
                    logger.error(msg)
                    errors.append(msg)

            # Delete all experiment data from storage
            if self.storage_service:
                try:
                    deleted_objects = await self.storage_service.delete_all_experiment_data()
                except Exception as e:
                    storage_deletion_ok = False
                    msg = f"Drop-all: storage deletion failed: {e}"
                    logger.error(msg)
                    errors.append(msg)

            logger.info(f"Successfully dropped {deleted_experiments} experiments and {deleted_objects} storage objects")

            failed_runner_count = len(stop_failed_runner_ids)
            runners_releasing_ok = (failed_runner_count == 0) and (runner_release_failed is False)

            message = (
                f"Experiments deletion: {'succeeded' if experiments_deletion_ok else 'failed'}\n"
                f"Storage objects deletion: {'succeeded' if storage_deletion_ok else 'failed'}\n"
                f"Runners releasing: {'succeeded' if runners_releasing_ok else 'failed'}"
            )

            error: Optional[str] = None
            if errors:
                # Keep response concise; full details already in logs.
                error = "; ".join(errors)

            resp: Dict[str, Any] = {
                "deleted_experiments": deleted_experiments,
                "deleted_storage_objects": deleted_objects,
                "stopped_runner_ids": sorted(stopped_runner_ids) if stopped_runner_ids else [],
                "failed_runner_ids": sorted(stop_failed_runner_ids) if stop_failed_runner_ids else [],
                "message": message,
            }
            # Only include error field when we actually have one (UI checks key presence).
            if error:
                resp["error"] = error
            return resp

        except Exception as e:
            logger.error(f"Failed to drop all experiments: {e}")
            raise

    async def _sync_status_from_redis(self, experiment_model: ExperimentModel, update_in_place: bool = False) -> bool:
        """
        Sync experiment status from Redis to database.
        Returns True if status was synced, False otherwise.
        If update_in_place is True, updates the model's status field directly.
        """
        if not self.kafka_service:
            return False

        try:
            cached_status = await self.kafka_service.get_cached_experiment_status(str(experiment_model.id))
            if not cached_status or not cached_status.get("status"):
                return False

            redis_status = str(cached_status.get("status"))
            # Only sync if status differs (avoid unnecessary DB writes)
            if redis_status == experiment_model.status:
                return False

            # Update database to match Redis status
            update_kwargs = {}
            if cached_status.get("updated_at"):
                try:
                    update_kwargs["updated_at"] = datetime.fromisoformat(str(cached_status.get("updated_at")))
                except (ValueError, TypeError) as e:
                    logger.debug(f"Failed to parse updated_at from Redis for {experiment_model.id}: {e}")

            success = await self.db_service.update_experiment_status(
                str(experiment_model.id), redis_status, **update_kwargs
            )

            if success:
                if update_in_place:
                    experiment_model.status = redis_status
                return True
            else:
                logger.warning(f"Failed to sync Redis status to database for experiment {experiment_model.id}")
                return False

        except Exception as sync_error:
            logger.debug(f"Failed to sync status from Redis for experiment {experiment_model.id}: {sync_error}")
            return False

    def _model_to_api(self, model: ExperimentModel) -> Experiment:
        """Convert database model to API model"""
        return Experiment(
            id=str(model.id),
            name=model.name,
            config=model.config,
            data_path=model.data_path or "",
            status=ExperimentStatus(model.status),
            created_at=model.created_at,
            updated_at=model.updated_at,
            started_at=getattr(model, "started_at", None),
            completed_at=getattr(model, "completed_at", None),
            metrics=model.metrics or {},
            best_result=getattr(model, "best_result", None),
            error_message=model.error_message,
            status_message=getattr(model, "status_message", None),
        )
