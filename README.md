# GigaEvo Platform

A machine learning experiment management system with a microservices architecture, featuring Kafka-based messaging and three-tier service separation.

[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

## 🏗️ Architecture Overview

GigaEvo Platform consists of three main components:

### 🔧 Master API (Port 8000)

- **Role**: Experiment orchestration and coordination
- **Technology**: FastAPI, Kafka, PostgreSQL, Redis
- **Features**:
  - Kafka integration for async messaging
  - Experiment lifecycle management
  - Configuration storage and retrieval
  - uv-based dependency management

### 🏃 Runner API (Port 8001)

- **Role**: Task execution with GigaEvolve integration
- **Technology**: FastAPI, GigaEvolve tools
- **Features**:
  - Experiment code execution
  - Results visualization
  - Best program extraction
  - Background task processing

### 🌐 WebUI (Port 7860)

- **Role**: Gradio-based user interface
- **Technology**: Gradio, Plotly, Requests
- **Features**:
  - Interactive experiment creation
  - Real-time progress monitoring
  - Results visualization
  - System status dashboard

## 🚀 Quick Start

### Prerequisites

- Docker & Docker Compose
- Python 3.12+ (for local development)
- uv (recommended) or pip

### LLM configuration

GigaEvo platform reads all LLM settings from a single repo-level file: `llm_models.yml`. Create `llm_models.yml` from the `llm_models.yml.example` template and fill in your credentials.

### Using the Deployment System

GigaEvo Platform uses the **deploy.sh** script with Docker Compose for service orchestration:

#### 1. Deploy Everything (Recommended)

```bash
make deploy
# Or directly:
./deploy.sh deploy
```

This will deploy with automated health checks:

- **Infrastructure**: PostgreSQL, Kafka, Zookeeper, Redis (2 instances), MinIO
- **Applications**: Master API, Runner API, WebUI
- **Networking**: Docker network and shared volumes
- **Health Monitoring**: Automatic service health verification

#### 2. Deploy Development Environment

```bash
make dev
```

#### 3. Individual Service Development

```bash
# Run services locally for development (requires infrastructure running)
make master-api    # Master API on port 8000
make runner-api    # Runner API on port 8001
make web-ui        # WebUI on port 7860
```

#### 4. Service Management

```bash
# Check all services status
make status
# Or:
./deploy.sh status

# Stop all services
make stop
# Or:
./deploy.sh stop

# Restart specific service
make restart SERVICE=master-api
make restart SERVICE=runner-api
make restart SERVICE=web-ui
make restart SERVICE=kafka

# View service logs
./deploy.sh logs [service-name]
```

### Access Points

- **WebUI**: http://localhost:7860
- **Master API**: http://localhost:8000
- **Runner API**: http://localhost:8001
- **MinIO Console**: http://localhost:9001 (user: minioadmin, pass: minioadmin)
- **Kafka Broker**: localhost:9092
- **Kafka UI**: Available in dev mode at http://localhost:9000 (via `make dev`)

## 📚 API Endpoints

### Master API (as per docs/api_endpoints.md)

- `POST /api/v1/experiments/` - Initialize experiment
- `GET /api/v1/experiments/` - Get list of experiments
- `GET /api/v1/experiments/{experiment_id}/status` - Request status
- `POST /api/v1/experiments/{experiment_id}/start` - Start experiment
- `POST /api/v1/experiments/{experiment_id}/stop` - Stop experiment
- `GET /api/v1/experiments/{experiment_id}/results` - Get results

### Runner API (as per docs/api_endpoints.md)

- `POST /api/v1/experiments/{experiment_id}/upload` - Load experiment code
- `POST /api/v1/experiments/{experiment_id}/start` - Start experiment
- `POST /api/v1/experiments/{experiment_id}/stop` - Stop experiment
- `GET /api/v1/experiments/{experiment_id}/status` - Get execution status
- `GET /api/v1/experiments/{experiment_id}/visualization` - Get visualization
- `GET /api/v1/experiments/{experiment_id}/best-program` - Get best program
- `GET /api/v1/experiments/{experiment_id}/logs` - Get logs (optional)

## 🔄 Kafka Topics

The system uses these Kafka topics for coordination:

- `experiment-config` - Experiment configuration received
- `experiment-prepared` - Experiment prepared for execution
- `experiment-started` - Experiment execution started
- `experiment-stopped` - Experiment execution stopped
- `runner-status` - Runner status updates

## 🛠️ Development

### Local Development Setup

```bash
# Install all dependencies
make install

# Run services individually (infrastructure must be running first)
make master-api    # Master API on port 8000
make runner-api    # Runner API on port 8001
make web-ui        # WebUI on port 7860
```

### Container-Based Development

```bash
# Development with hot reload (legacy architecture)
make dev

# Production environment (legacy)
make prod

# Clean up containers and volumes
make docker-clean
```

### Code Quality

```bash
make lint     # Run linting with ruff
make format   # Format code with ruff
make test     # Run tests (individual components)
```

### Database Management

```bash
make db-reset     # Drop and recreate database
make db-migrate   # Run database migrations
```

## 🐛 Troubleshooting

### Common Issues

1. **Port Conflicts**: Ensure these ports are free:

   - 5432: PostgreSQL
   - 6379, 6380: Redis (2 instances)
   - 7860: WebUI
   - 8000: Master API
   - 8001: Runner API
   - 9000, 9001: MinIO
   - 9092, 29092, 29093: Kafka
   - 2181: Zookeeper

2. **Deployment Issues**:

   ```bash
   # Check deployment status
   ./deploy.sh status
   # Or:
   make status

   # View service logs
   ./deploy.sh logs [service-name]
   # Or for all services:
   ./deploy.sh logs

   # Restart specific service
   make restart SERVICE=master-api
   make restart SERVICE=runner-api
   make restart SERVICE=web-ui
   make restart SERVICE=kafka
   ```

3. **Service Health Check Failures**:

   ```bash
   # The deploy script automatically checks service health
   # If services fail to start, check logs:
   ./deploy.sh logs postgres
   ./deploy.sh logs kafka
   ./deploy.sh logs master-api
   ```

4. **Database Connection Issues**:

   ```bash
   # Reset database (use after schema changes)
   make db-reset

   # Check PostgreSQL logs
   ./deploy.sh logs postgres
   ```

### Environment Variables

Key environment variables for Master API:

- `DATABASE__URL` - PostgreSQL connection string
- `KAFKA__BOOTSTRAP_SERVERS` - Kafka bootstrap servers
- `REDIS_URL` - Redis connection URL
- `STORAGE__ENDPOINT_URL` - MinIO endpoint
- `STORAGE__ACCESS_KEY` - MinIO access key
- `STORAGE__SECRET_KEY` - MinIO secret key

## 📊 Architecture Details

### Current Kafka-Based Architecture

The platform uses a modern microservices architecture with:

1. **Kafka Message Broker** - Asynchronous service communication with topics for experiment coordination
2. **Separate Docker Compositions** - Modular deployment with infrastructure and application services
3. **Health Monitoring** - Automated service health checks and recovery
4. **Resource Isolation** - Dedicated Redis instances and MinIO storage
5. **uv Dependency Management** - Fast package installation and dependency caching

### Service Orchestration

- **deploy.sh**: Main deployment script with health checks and service management
- **docker-compose.kafka.yml**: Core infrastructure services
- **docker-compose.\*.yml**: Individual application service configurations
- **Makefile**: Development commands and shortcuts

## 🤝 Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Run tests and linting: `make test && make lint`
5. Submit a pull request


## 📄 License

MIT License - see [LICENSE](LICENSE) file for details.