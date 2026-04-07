# GigaEvo Platform Runner API

The Runner API executes experiments and manages worker processes for GigaEvo Platform.

## Baked `gigaevo-core` runtime

Runner images now include a prebuilt `gigaevo-core` checkout and its `.venv` at image build time.
The `gigaevo-memory` Python client is installed into that baked core venv from PyPI.

- Runtime path: `/opt/gigaevo-core`
- No runtime clone/fetch/install flow
- Core updates are delivered by rebuilding runner image with a new core ref
- Memory-client updates are delivered by rebuilding runner image after changing the pinned runner dependencies

## Build-time inputs

Runner Dockerfiles accept:

- `GIGAEVO_CORE_REPO_URL` (build arg, GitHub HTTPS repository root URL, defaults to `https://github.com/FusionBrainLab/gigaevo-core`)
- `GIGAEVO_CORE_REF` (build arg, defaults to `main`)
- `github_pat` (optional BuildKit secret, sourced from `GITHUB_PAT`; required only for private GitHub repos)

Prefer the repo-level flows:

```bash
make dev
make deploy
```

To rebuild only the shared runner image used by generated runner pools:

```bash
MODE=dev ./scripts/build_runner_image.sh
MODE=prod ./scripts/build_runner_image.sh
```

The helper script derives the shared runner image tag from `COMPOSE_PROJECT_NAME` and passes `GIGAEVO_CORE_*` automatically.

Low-level manual Docker build for debugging:

```bash
DOCKER_BUILDKIT=1 docker build \
  --tag "${COMPOSE_PROJECT_NAME}-runner-api" \
  --build-arg GIGAEVO_CORE_REPO_URL=https://github.com/FusionBrainLab/gigaevo-core \
  --build-arg GIGAEVO_CORE_REF=main \
  -f runner_api/Dockerfile \
  .
```

Private GitHub repo build:

```bash
DOCKER_BUILDKIT=1 docker build \
  --secret id=github_pat,env=GITHUB_PAT \
  --tag "${COMPOSE_PROJECT_NAME}-runner-api" \
  --build-arg GIGAEVO_CORE_REPO_URL=https://github.com/FusionBrainLab/gigaevo-core \
  --build-arg GIGAEVO_CORE_REF=main \
  -f runner_api/Dockerfile \
  .
```

When provided, the PAT is mounted transiently at `/run/secrets/github_pat` for the clone step only and must never appear in build logs.

## API endpoints

- `GET /health` - health check with repository readiness
- `GET /api/v1/repository/status` - baked repository metadata from `.buildinfo.json`

`POST /api/v1/repository/refresh` is removed.

## Runtime config

Most repository lifecycle config is removed. Relevant settings:

```bash
GIGAVOLVE__CLONE_PATH=/opt/gigaevo-core
GIGAVOLVE__PYTHON_PATH=python3
GIGAVOLVE__EXPERIMENT_TIMEOUT=7200
GIGAVOLVE__RESULTS_COLLECTION_INTERVAL=10
MEMORY_API_URL=http://host.docker.internal:8002
```

`MEMORY_API_URL` must be reachable from runner containers. In local development the recommended value is `http://host.docker.internal:8002`; the compose generator and `docker run` path both add a host-gateway mapping so this works on Linux too.

## Development

```bash
cd runner_api
python src/main.py
```

## Docker development

Preferred:

```bash
make dev
```

This builds the shared runner image, generates `docker-compose.runner-pool.dev.generated.yml`, and starts the dev stack.

## Status checks

```bash
curl http://localhost:8001/health
curl http://localhost:8001/api/v1/repository/status
```
