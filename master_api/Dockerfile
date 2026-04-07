FROM python:3.12-slim

WORKDIR /app

ENV PYTHONPATH=/app

# Install system dependencies, the Docker CLI client, and uv
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    curl \
    docker-cli \
    gosu \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv

# Copy dependency manifests first so dependency layers survive source-only edits.
COPY master_api/pyproject.toml ./pyproject.toml
COPY master_api/uv.lock ./uv.lock

# Install dependencies without caching package downloads into the image layer.
RUN uv sync --frozen --no-install-project --no-cache

# Copy application code after dependency resolution.
COPY master_api/src/ ./src/
COPY master_api/data_examples/ ./data_examples/
COPY master_api/prompt_examples/ ./prompt_examples/
COPY common/ ./common/

# Copy entrypoint script
COPY master_api/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 755 /usr/local/bin/entrypoint.sh

# Create non-root user (run app as this user via entrypoint)
RUN useradd -m -u 1000 gigaevouser && chown -R gigaevouser:gigaevouser /app

# Expose port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=30s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Run the application
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["uv", "run", "uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
