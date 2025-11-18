FROM python:3.12-slim

WORKDIR /app

# Install system dependencies and uv
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    git \
    curl \
    ca-certificates \
    && update-ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && pip install uv

# Copy application code and pyproject.toml
COPY src/ ./src/
COPY pyproject.toml .

# Install the application using uv
RUN uv sync

# Create directories for GigaEvolve cloning
RUN mkdir -p /tmp/gigavolve && \
    useradd -m -u 1000 gemluser && \
    chown -R gemluser:gemluser /app /tmp/gigavolve

USER gemluser

# Expose port
EXPOSE 8001

# Health check
HEALTHCHECK --interval=30s --timeout=30s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8001/health || exit 1

# Run the application
CMD ["uv", "run", "uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8001"]