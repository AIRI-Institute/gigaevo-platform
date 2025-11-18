FROM python:3.12-slim

WORKDIR /app

# Install system dependencies and uv
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    curl \
    && rm -rf /var/lib/apt/lists/* \
    && pip install uv

# Copy application code and pyproject.toml
COPY src/ ./src/
COPY data_examples/ ./data_examples/
COPY pyproject.toml .

# Install the application using uv
RUN uv sync

# Create non-root user
RUN useradd -m -u 1000 gemluser && chown -R gemluser:gemluser /app
USER gemluser

# Expose port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=30s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Run the application
CMD ["uv", "run", "uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]