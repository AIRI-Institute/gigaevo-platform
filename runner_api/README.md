# GEML Runner API

The Runner API is responsible for executing experiments and managing worker processes. It integrates with the GigaEvolve repository to run machine learning experiments.

## Features

- **Repository Management**: Automatically clones and manages the GigaEvolve repository
- **Task Queue**: Redis-based task queuing for experiment execution
- **Worker Management**: Manages multiple worker processes
- **LLM Integration**: Supports multiple LLM providers for code generation
- **Health Monitoring**: Comprehensive health checks and repository status

## Repository Cloning

The Runner API automatically clones the GigaEvolve repository on startup:

### Clone Location
- **Default Path**: `./repos/metaevolve` (relative to the runner_api directory)
- **Configurable**: Can be changed via `GIGAVOLVE__CLONE_PATH` environment variable

### Clone Behavior
1. **Startup Clone**: Repository is automatically cloned when the Runner API starts
2. **Smart Updates**: If repository exists, it will `git pull` latest changes
3. **Force Refresh**: Use `/api/v1/repository/refresh` endpoint to force re-clone
4. **Error Handling**: Comprehensive logging and error recovery

### API Endpoints

#### Repository Management
- `GET /health` - Basic health check with repository status
- `GET /api/v1/repository/status` - Detailed repository information
- `POST /api/v1/repository/refresh` - Force repository refresh

#### Repository Status Response
```json
{
  "path": "/app/repos/metaevolve",
  "commit_hash": "abc123...",
  "remote_url": "https://github.com/FusionBrainLab/gigaevo-core",
  "branch": "main",
  "is_ready": true
}
```

## Configuration

The GigaEvolve repository can be configured via environment variables:

```bash
# Repository URL (required)
GIGAVOLVE__REPO_URL=https://github.com/FusionBrainLab/gigaevo-core.git

# Optional branch/tag to checkout (e.g., release tag)
GIGAVOLVE__REPO_REF=v1.1.0

# Clone path (relative to runner_api)
GIGAVOLVE__CLONE_PATH=./repos/metaevolve

# Python executable for experiments
GIGAVOLVE__PYTHON_PATH=python3

# Experiment timeout in seconds
GIGAVOLVE__EXPERIMENT_TIMEOUT=7200
```

## Development

### Local Development
```bash
cd runner_api
python src/main.py
```

### Docker Development
```bash
# The repository will be cloned into the container at /app/repos/metaevolve
docker-compose -f docker-compose.dev.yml up runner-api
```

### Monitoring Repository Status
```bash
# Check health (includes repository status)
curl http://localhost:8001/health

# Get detailed repository information
curl http://localhost:8001/api/v1/repository/status

# Force refresh repository
curl -X POST http://localhost:8001/api/v1/repository/refresh
```

## File Structure

```
runner_api/
├── src/
│   ├── services/
│   │   └── gigavolve_service.py    # Repository management
│   ├── api/routes/
│   │   ├── experiments.py          # Experiment endpoints
│   │   ├── workers.py             # Worker management
│   │   └── tasks.py               # Task management
│   ├── main.py                    # FastAPI application
│   └── config.py                  # Configuration
├── repos/                         # Cloned repositories (gitignored)
│   └── metaevolve/               # GigaEvolve repository
├── Dockerfile                    # Production container
├── Dockerfile.dev               # Development container
└── requirements.txt             # Python dependencies
```

## Logging

The Runner API provides comprehensive logging for repository operations:

- Repository cloning progress
- Git operations (pull, clone, status)
- Error conditions and recovery
- Experiment execution using repository code

Logs are written to stdout and can be viewed via:
```bash
docker-compose logs -f runner-api
```