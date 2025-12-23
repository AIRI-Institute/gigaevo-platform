FROM python:3.12-slim

WORKDIR /app

ENV PYTHONPATH=/app

# Install system dependencies and uv
RUN apt-get update && apt-get install -y \
    curl \
    && rm -rf /var/lib/apt/lists/* \
    && pip install uv

# Copy dependency manifest first (better caching), then install deps
COPY web_ui/pyproject.toml ./pyproject.toml
RUN uv sync

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