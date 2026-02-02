FROM python:3.12-slim

WORKDIR /app

ENV PYTHONPATH=/app

# Install system dependencies and uv
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    curl \
    docker.io \
    gosu \
    && rm -rf /var/lib/apt/lists/* \
    && pip install uv

# Copy application code and pyproject.toml
COPY master_api/src/ ./src/
COPY master_api/data_examples/ ./data_examples/
COPY master_api/prompt_examples/ ./prompt_examples/
COPY master_api/pyproject.toml ./pyproject.toml

# Shared code
COPY common/ ./common/

# Install the application using uv
RUN uv sync

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
