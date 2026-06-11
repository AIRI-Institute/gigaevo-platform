# Deploy From Prebuilt Bundle

Use this workflow to deploy the full stack from a prebuilt bundle with all images already packaged.

This deployment path is separate from the standard flow (`make deploy` / `./deploy.sh deploy`).
It uses the same required `COMPOSE_PROJECT_NAME` contract as `make dev` and `make deploy`.

All user-facing commands are `make bundle-*`.

## Breaking change

This is a hard-cut rename from the previous naming.
Legacy command/path names are no longer supported.
Build a fresh bundle and use the new `bundle-*` commands.

## Prerequisites

### Bundle build host

- Docker with Compose plugin
- Python 3
- `llm_models.yml` in repo root
- `GITHUB_PAT` available in shell or `.env` only if the configured runner repo is private; public GitHub HTTPS repos can build without it

### Bundle target host

- Docker with Compose plugin
- Python 3
- Repository checkout with the same `Makefile`
- Bundle archive copied to `dist/bundle/`

## Quick start

Build bundle on the build host:

```bash
make bundle-build
```

Copy artifacts to the target host (into repo-local `dist/bundle/`):

```bash
mkdir -p dist/bundle
cp /path/to/gigaevo-bundle-<UTCSTAMP>.tar.gz dist/bundle/
cp /path/to/gigaevo-bundle-<UTCSTAMP>.tar.gz.sha256 dist/bundle/
```

Prepare and deploy on the target host:

```bash
make bundle-runtime
make bundle-deploy
```

## Bundle output

`make bundle-build` produces:

- `dist/bundle/gigaevo-bundle-<UTCSTAMP>.tar.gz`
- `dist/bundle/gigaevo-bundle-<UTCSTAMP>.tar.gz.sha256`

The bundle includes runtime artifacts such as:

- `images.tar`
- `images.list`
- `bundle.meta.env`
- compose files
- internal helper scripts
- `.env.example` and `llm_models.yml.example`
- `init.sql`

The bundle contains one shared runner image for the project. Changing `RUNNER_POOL_SIZE` at runtime reuses that image and only changes the number of runner containers.

## Runtime layout

`make bundle-runtime` unpacks the latest bundle into `.bundle-runtime` (or `BUNDLE_RUNTIME_DIR` if overridden).

Expected runtime files include:

- `.bundle-runtime/bundle.meta.env`
- `.bundle-runtime/images.tar`
- `.bundle-runtime/.env`
- `.bundle-runtime/llm_models.yml`
- `.bundle-runtime/init.sql`

If `.bundle-runtime/.env` or `.bundle-runtime/llm_models.yml` already exist, they are preserved during prepare.

## Configure runner pool size

Edit `.bundle-runtime/.env`:

```bash
RUNNER_POOL_SIZE=3
```

Apply changes:

```bash
make bundle-deploy
```

No image rebuild is required. Scaling down (for example `3 -> 1`) is handled by `--remove-orphans`.

## Operations

From repository root:

```bash
make bundle-status
make bundle-logs
make bundle-logs SERVICE=master-api
make bundle-stop
make bundle-clean
make bundle-db-reset
make bundle-db-migrate
```

## Optional overrides

Build-time overrides:

```bash
COMPOSE_PROJECT_NAME=giagevo-platform-bundle OUT_DIR=dist/bundle make bundle-build
```

Runtime directory override:

```bash
make BUNDLE_RUNTIME_DIR=/tmp/gigaevo-bundle-runtime bundle-deploy
```

## Troubleshooting

### `COMPOSE_PROJECT_NAME` mismatch

Example:

```text
COMPOSE_PROJECT_NAME='X' does not match bundle metadata 'Y'
```

Fix: unset or correct `COMPOSE_PROJECT_NAME`, or rebuild with matching metadata.

### Missing stack network

`make bundle-deploy` creates `${GIGAEVO_NETWORK_NAME:-gigaevo-network}` automatically. If creation fails, run with Docker permissions that allow network management.

### Missing `llm_models.yml`

Create and configure:

```bash
cp .bundle-runtime/llm_models.yml.example .bundle-runtime/llm_models.yml
```

### `init.sql` missing or is a directory

`make bundle-deploy` requires `.bundle-runtime/init.sql` to be a regular file.
Rebuild the bundle and rerun `make bundle-runtime` if it is missing or invalid.

### LLM endpoints not reachable

The stack may start, but LLM-dependent flows will fail.
Point `.bundle-runtime/llm_models.yml` to reachable internal endpoints.
