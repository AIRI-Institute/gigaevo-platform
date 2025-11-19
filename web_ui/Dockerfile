FROM python:3.12-slim

WORKDIR /app

# Install system dependencies and uv
RUN apt-get update && apt-get install -y \
    curl \
    && rm -rf /var/lib/apt/lists/* \
    && pip install uv

# Copy application code and pyproject.toml
COPY app.py .
COPY pyproject.toml .

# Install the application using uv
RUN uv sync

# Create non-root user
RUN useradd -m -u 1000 gigaevouser && chown -R gigaevouser:gigaevouser /app
USER gigaevouser

# Expose port
EXPOSE 7860

# Health check
HEALTHCHECK --interval=30s --timeout=30s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:7860 || exit 1

# Run the application
CMD ["uv", "run", "python", "app.py"]