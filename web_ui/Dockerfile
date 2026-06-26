FROM python:3.12-slim

WORKDIR /app

ENV PYTHONPATH=/app

# Install system dependencies and uv
RUN apt-get update && apt-get install -y \
    curl \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv

# Copy dependency manifests first so the baked venv matches the lockfile.
COPY web_ui/pyproject.toml ./pyproject.toml
COPY web_ui/uv.lock ./uv.lock
RUN uv sync --frozen --no-install-project --no-cache

# Copy application source code
COPY web_ui/ ./

# Shared code
COPY common/ ./common/

# Chain diff for Experiment Results (import as src.services.chain_diff_service)
COPY master_api/src /app/_master_api_vendor/src

# Create non-root user and fix ownership
RUN useradd -m -u 1000 gigaevouser && chown -R gigaevouser:gigaevouser /app
USER gigaevouser

# Expose port
EXPOSE 7860

# Health check
HEALTHCHECK --interval=30s --timeout=30s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:7860 || exit 1

# Run the application - command specified in docker-compose
