# GitHub Personal Access Token (PAT) Setup

This document explains how to configure a GitHub Personal Access Token (PAT) for **build-time** access to `gigaevo-core`.

## Overview

Runner images now bake `gigaevo-core` into the image during Docker build and install the `gigaevo-memory` Python client from PyPI into the baked core virtualenv.

- Public GitHub HTTPS repos can be cloned without a PAT
- Private GitHub repos require a PAT
- PAT is used only at build time (BuildKit secret `github_pat`)
- No runtime clone/fetch/install flow in Runner API
- Core repository is configurable via `GIGAEVO_CORE_REPO_URL`
- Core version is pinned by `GIGAEVO_CORE_REF`
- When provided, the PAT is mounted transiently at `/run/secrets/github_pat` for a single build step and must never appear in logs

## Steps

### 1. Generate a GitHub PAT (private repos only)

1. Go to GitHub Settings: https://github.com/settings/tokens
2. Click "Generate new token (classic)"
3. Set required scope:
   - `repo` (for private repository read access)
4. Generate and copy the token

### 2. Configure `.env`

```bash
# Copy template
cp .env.example .env
```

Set:

```bash
# Optional for public GitHub HTTPS repos; required for private repos
# Used as a transient BuildKit secret during runner image build
GITHUB_PAT=ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# Optional: GitHub HTTPS repository root URL (not /tree/... or /blob/...)
GIGAEVO_CORE_REPO_URL=https://github.com/FusionBrainLab/gigaevo-core

# Optional: pin core ref baked into runner image
GIGAEVO_CORE_REF=main

# Runtime URL of the external gigaevo-memory API as seen from runner containers
# Recommended local-dev value:
MEMORY_API_URL=http://host.docker.internal:8002
```

### 3. Build and run

Use the standard project flow (`make dev`, `make deploy`) or rebuild only the shared runner image.

Recommended:

```bash
make dev
make deploy
```

Rebuild only the shared runner image:

```bash
MODE=dev ./scripts/build_runner_image.sh
MODE=prod ./scripts/build_runner_image.sh
```

The helper script derives the runner image tag from `COMPOSE_PROJECT_NAME` and forwards `GIGAEVO_CORE_*` and the optional `GITHUB_PAT` secret automatically.

Low-level manual Docker build for debugging:

```bash
DOCKER_BUILDKIT=1 docker build \
  --tag "${COMPOSE_PROJECT_NAME}-runner-api" \
  --build-arg GIGAEVO_CORE_REPO_URL=https://github.com/FusionBrainLab/gigaevo-core \
  --build-arg GIGAEVO_CORE_REF=main \
  -f runner_api/Dockerfile \
  .
```

Private GitHub repo:

```bash
DOCKER_BUILDKIT=1 docker build \
  --secret id=github_pat,env=GITHUB_PAT \
  --tag "${COMPOSE_PROJECT_NAME}-runner-api" \
  --build-arg GIGAEVO_CORE_REPO_URL=https://github.com/FusionBrainLab/gigaevo-core \
  --build-arg GIGAEVO_CORE_REF=main \
  -f runner_api/Dockerfile \
  .
```

## Troubleshooting

### Authentication/build errors

1. Verify `GITHUB_PAT` value is correct
2. Ensure PAT has `repo` scope
3. Ensure your GitHub account has access to the repository set in `GIGAEVO_CORE_REPO_URL`
4. Ensure BuildKit is enabled (`DOCKER_BUILDKIT=1`)
5. Ensure `GIGAEVO_CORE_REPO_URL` is a GitHub HTTPS repository root URL, not a `tree` or `blob` URL
6. If you are cloning a public repo and have a stale `GITHUB_PAT`, unset it to force anonymous clone

### Updated token/ref is not applied

Rebuild runner image after changing `.env`:

```bash
MODE=prod ./scripts/build_runner_image.sh
```

Then rerun the relevant stack command (`make dev` or `make deploy`) if containers are already running.

## Environment Variables Reference

| Variable | Description | Required | Default |
| --- | --- | --- | --- |
| `GITHUB_PAT` | GitHub PAT for build-time clone secret | Only for private GitHub repos | None |
| `GIGAEVO_CORE_REPO_URL` | GitHub HTTPS repository root URL baked into runner image | No | `https://github.com/FusionBrainLab/gigaevo-core` |
| `GIGAEVO_CORE_REF` | Core tag/branch/commit baked into runner image | No | `main` |
| `MEMORY_API_URL` | External gigaevo-memory API URL as seen from runner containers | Required for memory-enabled runs/uploads | `http://host.docker.internal:8002` |

## How It Works

During Docker build, the runner `core-builder` stage:

1. clones `gigaevo-core` using BuildKit secret `github_pat` when provided, otherwise clones anonymously
2. creates `/opt/gigaevo-core/.venv` and installs core plus the fixed `gigaevo-memory` PyPI package
3. applies build-time patches and writes `.buildinfo.json`
4. copies prepared `/opt/gigaevo-core` into the final runner image

At runtime, Runner API reads and writes experiment artifacts under `/opt/gigaevo-core` (for example `problems/*`, `outputs/*`, `config/llm/custom.yaml`). Memory-enabled runs and `upload_to_memory` calls also require `MEMORY_API_URL` to point at a runner-reachable external memory API endpoint.
