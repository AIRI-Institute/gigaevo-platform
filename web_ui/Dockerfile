# syntax=docker/dockerfile:1.7

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

ENV PYTHONPATH=/app
# Copy instead of hardlink out of the uv cache mount (different filesystem).
ENV UV_LINK_MODE=copy
# Compile .pyc at install time so the first import per container is faster.
ENV UV_COMPILE_BYTECODE=1

# uv is provided by the base image. apt cache mounts keep .debs across rebuilds.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean \
    && echo 'Binary::apt::APT::Keep-Downloaded-Packages "true";' > /etc/apt/apt.conf.d/keep-cache \
    && apt-get update && apt-get install -y --no-install-recommends \
    curl

# Copy dependency manifests first so the baked venv matches the lockfile.
COPY web_ui/pyproject.toml ./pyproject.toml
COPY web_ui/uv.lock ./uv.lock
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project

# Copy application source code
COPY web_ui/ ./

# Shared code
COPY common/ ./common/

# Create non-root user and fix ownership
RUN useradd -m -u 1000 gigaevouser && chown -R gigaevouser:gigaevouser /app
USER gigaevouser

# Expose port
EXPOSE 7860

# Health check
HEALTHCHECK --interval=30s --timeout=30s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:7860 || exit 1

# Run the application - command specified in docker-compose
