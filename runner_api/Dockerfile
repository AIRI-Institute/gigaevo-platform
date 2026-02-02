FROM python:3.12-slim

WORKDIR /app

ENV PYTHONPATH=/app

# Install system dependencies and uv
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    git \
    curl \
    ca-certificates \
    gosu \
    && update-ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && pip install uv

# Copy application code and pyproject.toml
COPY runner_api/src/ ./src/
COPY runner_api/pyproject.toml ./pyproject.toml

# Shared code
COPY common/ ./common/

# Install the application using uv
RUN uv sync

# Copy entrypoint script (fixes volume ownership and drops privileges via gosu)
COPY runner_api/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Create directories for GigaEvolve cloning (best-effort; volume mounts may override ownership at runtime)
RUN mkdir -p /tmp/gigavolve /tmp/problems && \
    useradd -m -u 1000 gigaevouser && \
    chown -R gigaevouser:gigaevouser /app /tmp/gigavolve /tmp/problems

# Set entrypoint (will switch to gigaevouser)
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

# Expose port
EXPOSE 8001

# Health check
HEALTHCHECK --interval=30s --timeout=30s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8001/health || exit 1

# Run the application
CMD ["uv", "run", "uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8001"]