# gigaevo-platform · CARE preparation TODO

This file tracks the **platform-side** work required to unblock CARE
(`~/Development/care`). It is the platform's view of the deliverables
described in `~/Development/care/PREPARE.md` §4.

Priority legend:

- **[P0]** blocker for CARE M0 demo
- **[P1]** required for CARE M1 (local execution + re-run)
- **[P2]** required for CARE M2 (evolution + multi-tenant)
- **[P3]** quality / polish
- **[DONE]** shipped on `main` with tests

---

## 1. Evolution API (CARE M2)

- **[DONE] §4.1 — `POST /api/v1/evolutions` accepted on master_api.**
  New `EvolutionCreate` model with `seed_chains` (XOR
  `memory_chain_id` / inline `chain_content`, 1–64 entries),
  `FitnessSpec` (prompt + optional judge model + `higher_is_better`
  flag), unique non-empty `objectives` list (capped at 8), and
  `GAConfig` (`population_size` 1–256, `max_iterations` 1–1000,
  bounded `mutation_rate` / `crossover_rate`, `elitism`,
  reproducibility `random_seed`). Persistence in
  `master_api/src/services/evolution_service.py`: each evolution
  serialised to MinIO at `evolutions/<id>.json` with metadata
  `{type: evolution_record, status: queued}` for cheap S3 LIST
  filters. Returns 201 + full record body (id, name, status,
  created_at, …); 422 on bad payload; 500 on persist failure.
  Tests: `tests/test_evolutions.py` (24 tests) cover schema
  validation edge cases, service-level round-trip with a fake
  storage, corrupt-record + missing-id error paths, status-patch
  semantics, and a real-app TestClient smoke run through the
  fully-wired master_api with auth on (401 without key, 201 happy
  path, 422 malformed, 404 unknown, POST→GET round-trip).
- **[DONE] §4.2 — `/individuals` (record / list / get + Pareto).**
  New `IndividualCreate` / `IndividualResponse` / `IndividualsListResponse`
  in `master_api/src/models/evolution.py`. Persistence under
  `evolutions/<eid>/individuals/<ind_id>.json` with S3 metadata
  including `evolution_id` and `generation` for cheap LIST. New
  `EvolutionService.record_individual` validates that
  `fitness_scores` keys are a subset of the parent evolution's
  `objectives` (422 on mismatch), persists the record, bumps
  `current_generation` only when the new individual is from a later
  generation (so out-of-order GA arrivals don't roll back),
  and flips `best_individual_id` when the new score wins the
  primary objective under the evolution's
  `higher_is_better` direction. Publishes `individual_evaluated`
  on every call and `best_updated` when the best changes — both
  surfaced via §4.3 SSE. New `list_individuals` supports
  `generation` filter, `pareto=true` filter (multi-objective
  dominance check that handles missing scores as worst, exported as
  `pareto_front` helper for direct unit testing), and a `limit`.
  Routes: `POST /api/v1/evolutions/{id}/individuals` (201, 404, 422,
  500), `GET /api/v1/evolutions/{id}/individuals?generation=&pareto=&limit=`
  returns a paginated list with `total`, `primary_objective`,
  `higher_is_better` echoed back so CARE can render fitness columns
  correctly, `GET /api/v1/evolutions/{id}/individuals/{ind_id}`.
  Tests: `tests/test_individuals.py` (14 tests) — service-level
  schema validation, generation-bump semantics + both `higher_is_better`
  directions for the best-flip rule, event publication (bus
  subscriber asserts `individual_evaluated` + `best_updated`),
  Pareto front correctness (single-obj, multi-obj three-point
  front, missing-score treated as worst, lower-is-better), list
  with generation + pareto + limit + corrupt-blob tolerance, and a
  real-app TestClient smoke covering POST + GET list + GET one +
  auth + every failure mode.
- **[DONE] §4.3 — `GET /api/v1/evolutions/{id}/events` SSE
  stream.** New in-process `EvolutionEventBus` in
  `master_api/src/services/evolution_event_bus.py` (eager subscription
  registration so publishes are never lost between subscribe and
  iterate; ``_Subscription`` class that ALWAYS tears down its queue on
  `aclose()` regardless of whether anyone pumped the iterator;
  bounded queue per subscriber that drops oldest on overflow so the
  publisher never back-pressures). `EvolutionService` hooks into the
  bus: `create_evolution` publishes a `created` event;
  `update_status` maps each `EvolutionStatus` transition to the right
  event type (RUNNING→`generation_started`, COMPLETED→`completed`,
  FAILED→`failed`, CANCELLED→`cancelled`) and forwards extra payload
  fields like `best_individual_id`. The SSE endpoint subscribes
  BEFORE yielding the snapshot (so events fired during the
  read-snapshot window land in the queue), keeps one
  `__anext__` task alive across heartbeat-timeout ticks (a key
  correctness bug — cancelling the task on each tick would close the
  underlying generator), emits ``: heartbeat`` every 15 s when idle,
  and exits cleanly when the client disconnects. Tests:
  `tests/test_evolution_events.py` (13 tests) — bus pub/sub
  semantics including fan-out, overflow, cross-evolution isolation,
  cleanup; service integration; direct-generator tests for snapshot +
  live event + heartbeat-on-idle; HTTP-layer smokes for 404 + 401.
- **[DONE] §4.4 — `POST /api/v1/evolutions/{id}/accept` accepts an
  evolution winner.** New `EvolutionStatus.ACCEPTED` enum value;
  `EvolutionResponse` gains `accepted_individual_id` and
  `accepted_at`. New `AcceptIndividualRequest` body (non-empty
  `individual_id` + optional `note` passed through to Memory in §4.6).
  `EvolutionService.accept_individual` flips the individual's
  `accepted` flag in storage (with S3 metadata `accepted: "true"` so
  cheap LIST filters can find it), patches the parent record with
  `status=ACCEPTED` + `accepted_individual_id` + `accepted_at` + an
  operator override of `best_individual_id` (even if a higher-
  scoring individual exists, the human's pick wins for downstream
  consumers), and publishes an `accepted` event with the individual
  payload on the §4.3 SSE bus. Idempotent on the same id; switching
  to a different id on an already-accepted evolution raises
  `EvolutionAcceptError` → 409 so a downstream Memory write doesn't
  get silently invalidated. Routes: 200 happy, 401 no auth, 404
  missing evolution / individual, 409 switch-after-accept, 422 empty
  id. Tests: `tests/test_evolution_accept.py` (15 tests) — schema
  validation, service-level idempotency + override semantics + every
  error branch, SSE event payload assertion via a bus subscriber, and
  a real-app TestClient smoke covering all four status codes.
  Memory-side forwarding (writing the accepted chain to Memory's
  `stable` channel) is §4.6.
- **[DONE] §4.6 — Memory integration on platform side.** New
  `master_api/src/services/memory_client.py` — thin httpx-based
  async client with `save_chain` (POST /v1/chains with `content` +
  `evolution_meta`) and `set_channel` (PATCH /v1/chains/{id}/channel
  for stable-channel promotion). Driven by env `MEMORY_API_URL`
  (already plumbed through `Config.memory_api_url`); when unset the
  client is a no-op so local dev + the existing test suite keep
  working untouched. All calls are **best-effort** — HTTP errors,
  5xx, malformed responses, and `httpx.ConnectError` are logged but
  never raised, so a misbehaving Memory can't corrupt the platform's
  own evolution state machine. `EvolutionService.record_individual`
  now forwards every recorded individual to Memory with channel
  `latest`; `accept_individual` forwards (again) with channel
  `stable` so CARE's Memory library sees the human-accepted chain
  promoted. The `evolution_meta` block built by
  `EvolutionService._evolution_meta` follows PREPARE.md §1.6
  exactly: `parent_version_ids`, `fitness_score` (primary objective
  only), `generation`, `experiment_id`, `objectives`,
  `mutation_kind`. Route wires the client from
  `Config.memory_api_url` and gracefully tolerates test stubs
  without a `config` attribute. Tests: `tests/test_memory_integration.py`
  (13 tests) — MemoryClient behaviour over `httpx.MockTransport`
  (happy path, 5xx, missing entity_id, alternate `id` key,
  connection error, unconfigured no-op), `_evolution_meta` schema
  conformance, end-to-end forwarding from `record_individual` +
  `accept_individual` (correct content, evolution_meta, channel),
  and **graceful degradation** asserts that both methods complete
  successfully even when Memory returns 500 / 503 — the platform's
  own persistence and event publishing keep working.

## 2. AgentSkill runner (CARE M1)

- **[DONE] §4.5b — `RUN_AGENT_SKILL` wired into the runner task
  pipeline.** New `TaskType.RUN_AGENT_SKILL` enum value; new
  `SandboxConfig` (`backend=auto|docker|local`,
  `unsafe_local_allowed`, resource caps, image); new
  `runner_api/src/services/skill_executor.py` with
  `SkillExecutionRequest.from_task_parameters` (validates payload),
  `SkillExecutor.select_backend` (Docker preferred; Local requires
  explicit `unsafe_local_allowed=True`; `auto` refuses if Docker is
  down and Local isn't opted in), `SkillExecutor.execute`
  (materialises a temp workspace with the SKILL.md + `out/` dir,
  runs through the sandbox, cleans up). TaskWorker dispatch in
  `_handle_run_agent_skill` maps results onto task status:
  exit 0 → `COMPLETED`, non-zero → `FAILED` with code in message,
  timeout → `TERMINATED`, sandbox refusal / payload error → `FAILED`
  without ever calling the backend. Real-execution proof: 23 tests
  in `tests/test_skill_executor.py` — 8 backend-selection branches
  (all paths covered including the refuse-by-default safety case),
  5 payload-validation cases, 5 real-subprocess `execute()` tests
  (SKILL.md is read from the materialised workspace, stdout flows
  back, timeout/non-zero exit/bad-base64 all behave), and 5
  TaskWorker dispatch tests using a `Task` instance + mocked
  `task_repository` to assert status transitions end-to-end.
  **Still open as a separate item:** HTTP-CONNECT egress proxy so
  `NetworkPolicy.SKILL_DECLARED` resolves real hosts (today the
  `--add-host` entries point at the `127.0.0.1` sentinel from
  §4.5a). Tracked as §4.5c below.

- **[DONE] §4.5c — HTTP-CONNECT egress proxy for
  `NetworkPolicy.SKILL_DECLARED`.** New
  `runner_api/src/sandbox/egress_proxy.py` —
  `AllowList(hosts, allowed_ports=…)` parses SKILL.md
  `WebFetch(domain:*)` entries into literal hosts +
  `*.subdomain` wildcards (case-insensitive; bare `*` / `*.` /
  invalid chars / unreachable patterns refused at construction),
  plus a port restriction list (defaults to `{80, 443}` so a
  CONNECT to `example.com:22` is still denied even with an
  ``example.com`` allowlist entry). `EgressProxy.start(...)` is an
  ``async with``-style listener on ``127.0.0.1:<random>`` speaking
  enough of HTTP/1.1 to handle CONNECT: ALLOW + 200 + bidirectional
  ``asyncio`` byte splice to the real upstream; 403 on deny, 405 on
  non-CONNECT verbs, 400 on malformed CONNECT, 502 on upstream
  unreachable, 408 on idle preamble. Per-run counters
  ``allowed_count`` / ``denied_count`` for tests + future
  ``/debug`` instrumentation. `build_docker_run_args` in
  `docker_backend.py` now takes an optional ``proxy_url`` kwarg —
  when paired with `SKILL_DECLARED`, the container joins
  ``--network bridge`` with
  ``--add-host host.docker.internal:host-gateway`` and the proxy
  URL is injected via `HTTP_PROXY` / `HTTPS_PROXY` env (with a
  `127.0.0.1`-to-`host.docker.internal` rewrite so the in-container
  HTTP client can actually reach the proxy). The legacy fail-closed
  ``--add-host <domain>:127.0.0.1`` sentinel mode from §4.5a is
  preserved as the no-proxy fallback for backward compatibility.
  Tests: `tests/test_egress_proxy.py` (21 tests) — AllowList
  parsing edge cases + wildcard semantics + port restrictions +
  dangerous-input rejection; **real loopback CONNECT bridge** (start
  proxy, spawn a fake target server on a sibling port, open TCP to
  the proxy, send CONNECT, read 200 response, send HTTP/1.0 GET
  through the tunnel, assert ``hello-skill`` flows back — proves
  end-to-end byte transit); deny paths (403 disallowed host, 403
  allowed-host-wrong-port, 405 non-CONNECT verb, 405 malformed
  CONNECT); and `build_docker_run_args` integration confirming the
  proxy-on path switches to `HTTP_PROXY` + drops the legacy
  sentinel, while the proxy-off path keeps the legacy mode intact.
  **Test-isolation hardening** also landed: each master_api smoke
  fixture (`test_evolutions`, `test_evolution_events`,
  `test_individuals`, `test_evolution_accept`) now re-prepends
  `master_api/` to `sys.path[0]` right before re-importing
  `src.main`, so a sibling test file (this one!) that pushed
  `runner_api/` to the front during collection no longer makes the
  fixtures resolve `src.api.routes.evolutions` against the wrong
  tree. Full suite: 182/182.
- **[DONE] §4.5a — Sandbox abstraction landed in
  `runner_api/src/sandbox/`.** `SandboxBackend` Protocol +
  `RunRequest`/`RunResult` dataclasses (resource caps validated on
  construction), `LocalSandboxBackend` (host subprocess, marked
  `unsafe=True`), `DockerSandboxBackend` (composes `docker run` args
  with `--network none` default, `--read-only` rootfs, `--cap-drop
  ALL`, `--security-opt no-new-privileges`, tmpfs `/tmp`, workspace
  bind mount, resource caps, skill labels), and
  `network_policy.extract_allowed_domains` turning SKILL.md
  `WebFetch(domain:*.x.com)` tokens into an allowlist (rejects
  smuggled args like `WebFetch(domain:rm -rf /)`). Real-execution
  proof: `LocalSandboxBackend` is exercised against an actual
  `python3` subprocess (stdout capture, non-zero exit, timeout, missing
  workspace); `DockerSandboxBackend.is_available()` runs against the
  real `docker` CLI. Tests: `tests/test_sandbox.py` (24 tests).
  **Follow-up §4.5b** still open: wire `RUN_AGENT_SKILL` into the
  runner task pipeline + add a real HTTP-CONNECT proxy for
  `skill_declared` (today hosts resolve to `127.0.0.1` sentinel).
- **[DONE] §4.8 — `POST /api/v1/agent-skills/resolve`.** Master API
  fetches a SKILL.md from a URI, parses its YAML frontmatter
  (`name`, `description`, `allowed-tools`), and caches the bytes in
  MinIO under `agent_skills/<sha256>/SKILL.md` plus an
  `agent_skills/_index.json` URI→SHA index. Idempotent on the URI
  (returns `cached=True` on subsequent calls without re-fetching);
  `force_refresh=true` bypasses cache; `expected_sha256` mismatch
  → 409 with both hashes in the body; malformed SKILL.md → 422;
  network failure → 502. Tests:
  `tests/test_agent_skill_resolver.py` (15 unit tests covering
  frontmatter edge cases + resolver state machine).

## 3. Hardening (CARE M2)

- **[DONE] §4.9 — `GET /api/v1/evolutions` paginated list.** New
  `EvolutionsListResponse` model (`items`, opaque `next_cursor`,
  `total_scanned` for filter-cost observability) plus
  `EvolutionService.list_evolutions` reading from MinIO under the
  ``evolutions/`` prefix and **explicitly skipping
  ``evolutions/<eid>/individuals/<ind>.json`` blobs** so the
  individuals sub-tree doesn't pollute evolution-level results.
  Filters compose: ``status=`` (exact enum match), ``tag=``
  (case-insensitive membership), ``q=`` (case-insensitive substring
  match on `name`). Ordering is `created_at` descending —
  most-recent-first is the natural CARE "recent evolutions" view.
  Cursor pagination uses the last item's `evolution_id`; an
  unknown cursor falls back to the first page (fewer surprises than
  a 404). Route: `GET /api/v1/evolutions?status=&tag=&q=&cursor=&limit=`
  with `limit` bounded `1..200` (422 outside). Corrupt blobs are
  logged + skipped without breaking the page. Tests:
  `tests/test_evolutions_list.py` (15 tests) — service-level
  ordering, every filter and their composition, cursor walk across
  3 pages with `total_scanned` consistency, unknown-cursor
  fallback, individuals-blob exclusion, corrupt-blob tolerance, plus
  real-app TestClient smokes (401 no-auth, empty list, 3-create +
  paginate cursor walk, status filter via the §4.4 accept path, `q`
  substring, 422 on out-of-range `limit`). All 197 tests across 11
  suites green together.

- **[DONE] §4.7 — API-key auth + tightened CORS.** Master API and
  Runner API both accept an optional `X-API-Key` header (env
  `MASTER_API_KEY` / `RUNNER_API_KEY`). CORS origins controlled by
  env `CORS_ALLOWED_ORIGINS` (comma-separated). Backwards-compatible:
  when env vars are unset, behaviour matches the previous open
  `allow_origins=["*"]` + no-auth baseline so local dev keeps
  working. Web UI forwards the key from `GIGAEVO_API_KEY`. Tests:
  `tests/test_auth_and_cors.py`.

---

## How to use this file

1. Pick the highest-priority open item (P0 → P1 → P2 → P3) you can
   ship in one PR.
2. Implement, write tests, run `make lint`.
3. Flip the bullet's priority tag from `[Pn]` to `[DONE]` and add a
   one-sentence outcome note describing what landed and how it's
   tested.
4. Update `~/Development/care/PREPARE.md` §4 Status column from `⛔`
   to `✅` for the corresponding row (4.1, 4.7, etc.) so CARE
   planning stays in sync.
