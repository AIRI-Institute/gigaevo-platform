.PHONY: help install dev prod clean clean-dev clean-deploy lint format test docker-build docker-up docker-down deploy-infrastructure deploy-applications check-secrets

# Default target
help:
	@echo "GigaEvo Platform Commands:"
	@echo ""
	@echo "🔧 Development:"
	@echo "  install                - Install all dependencies"
	@echo "  dev                    - Start development environment"
	@echo "  prod                   - Start production environment"
	@echo "  clean                  - Nuke dev + deploy environments"
	@echo "  clean-dev              - Nuke dev stack + local artifacts"
	@echo "  clean-deploy           - Nuke deploy stack + volumes"
	@echo ""
	@echo "📊 Individual Services:"
	@echo "  master-api             - Run Master API locally"
	@echo "  runner-api             - Run Runner API locally"
	@echo "  web-ui                 - Run Web UI locally"
	@echo ""
	@echo "🚀 Deployment (New Kafka Architecture):"
	@echo "  deploy                 - Deploy all services (infrastructure + applications)"
	@echo "  deploy-infrastructure   - Deploy only infrastructure (Kafka, PostgreSQL, Redis, MinIO)"
	@echo "  deploy-applications    - Deploy only applications (Master API, Runner API, Web UI)"
	@echo "  stop                   - Stop all deployed services"
	@echo "  restart [service]      - Restart specific service"
	@echo "  status                 - Show status of all deployed services"
	@echo ""
	@echo "🗄️ Database:"
	@echo "  db-reset               - Drop and recreate database (use after schema changes)"
	@echo "  db-migrate             - Run database migrations"
	@echo ""
	@echo "🐳 Docker Commands:"
	@echo "  docker-build           - Build Docker images"
	@echo "  docker-up             - Start Docker services (legacy)"
	@echo "  docker-down           - Stop Docker services (legacy)"
	@echo ""
	@echo "🧹 Code Quality:"
	@echo "  lint                  - Run linting"
	@echo "  format                - Format code"
	@echo "  test                  - Run tests"

# Check for required configuration file
check-secrets:
	@if [ ! -f llm_models.yml ]; then \
		echo "❌ ERROR: llm_models.yml not found!"; \
		echo ""; \
		echo "This file contains model definitions and API keys for LLM models and is required to run the platform."; \
		echo "Please create it from the example:"; \
		echo "  cp llm_models.yml.example llm_models.yml"; \
		echo ""; \
		echo "Then edit llm_models.yml and replace REPLACE_ME with your actual API keys."; \
		echo ""; \
		exit 1; \
	fi
	@echo "✅ llm_models.yml found"

# Installation
install:
	pip install -e ".[all]"

# Development
dev: check-secrets
	@echo "🚀 Starting GigaEvo Platform in Development Mode..."
	@echo ""
	@echo "📋 Service URLs (will be available after services start):"
	@echo "   • Master API:     http://localhost:8000"
	@echo "   • Runner API:     http://localhost:8001"
	@echo "   • Web UI:         http://localhost:7860"
	@echo "   • MinIO Console:  http://localhost:9001"
	@echo "   • Kafka UI:       http://localhost:8080"
	@echo ""
	@set -a; [ -f .env ] && . ./.env; set +a; \
	RUNNER_POOL_SIZE=$${RUNNER_POOL_SIZE:-1}; \
	RUNNER_REDIS_DB_START=$${RUNNER_REDIS_DB_START:-1}; \
	REDIS_DATABASES_MIN=$${REDIS_DATABASES_MIN:-512}; \
	export RUNNER_POOL_SIZE RUNNER_REDIS_DB_START; \
	req_dbs=$$((RUNNER_REDIS_DB_START + RUNNER_POOL_SIZE)); \
	if [ $$req_dbs -gt $$REDIS_DATABASES_MIN ]; then \
		max_pool=$$((REDIS_DATABASES_MIN - RUNNER_REDIS_DB_START)); \
		echo "❌ ERROR: RUNNER_POOL_SIZE=$$RUNNER_POOL_SIZE exceeds Redis DB limit." ; \
		echo "   Required databases: RUNNER_REDIS_DB_START($$RUNNER_REDIS_DB_START) + RUNNER_POOL_SIZE($$RUNNER_POOL_SIZE) = $$req_dbs" ; \
		echo "   Configured REDIS_DATABASES_MIN=$$REDIS_DATABASES_MIN" ; \
		echo "   Max supported RUNNER_POOL_SIZE is $$max_pool (for RUNNER_REDIS_DB_START=$$RUNNER_REDIS_DB_START)." ; \
		exit 1; \
	fi; \
	export REDIS_DATABASES=$$REDIS_DATABASES_MIN; \
	python3 generate_runner_pool_compose.py --mode dev --output docker-compose.runner-pool.dev.generated.yml; \
	HOST_UID=$$(id -u) HOST_GID=$$(id -g) docker compose -f docker-compose.dev.yml -f docker-compose.runner-pool.dev.generated.yml up --build

# When CLEAN_PRUNE=0, skip docker system prune (used by `clean` to prune once).
CLEAN_PRUNE ?= 1

# Nuke everything (dev + deploy). DANGEROUS: removes volumes => wipes DB/MinIO.
clean:
	@echo "🧹 Cleaning EVERYTHING (dev + deploy)..."
	@$(MAKE) clean-dev CLEAN_PRUNE=0
	@$(MAKE) clean-deploy CLEAN_PRUNE=0
	@echo "Pruning unused Docker data (once)..." ; \
		docker system prune -f; \
		echo "✅ clean complete."

# Clean up all generated/dev artifacts (DANGEROUS: removes volumes => wipes DB/MinIO)
clean-dev:
	@echo "🧹 Cleaning DEV environment (containers, volumes, generated files)..."
	@set -e; \
		echo "Stopping dev stack (including generated runner pool)..." ; \
		if [ -f docker-compose.runner-pool.dev.generated.yml ]; then \
			docker compose -f docker-compose.dev.yml -f docker-compose.runner-pool.dev.generated.yml down -v --remove-orphans || true; \
		else \
			docker compose -f docker-compose.dev.yml down -v --remove-orphans || true; \
		fi; \
		echo "Stopping any legacy/default compose stack..." ; \
		docker compose down -v --remove-orphans || true; \
		echo "Removing generated compose files..." ; \
		rm -f docker-compose.runner-pool.dev.generated.yml; \
		echo "Removing runner clone directories (runner_api/repos/gigaevo-core-*)..." ; \
		rm -rf runner_api/repos/gigaevo-core-*; \
		if [ "$(CLEAN_PRUNE)" = "1" ]; then \
			echo "Pruning unused Docker data..." ; \
			docker system prune -f; \
		fi; \
		echo "✅ clean-dev complete."

# Clean up all deploy artifacts (DANGEROUS: removes volumes => wipes DB/MinIO)
clean-deploy:
	@echo "🧹 Cleaning DEPLOY environment (containers, volumes, generated files)..."
	@set -e; \
		echo "Stopping deploy stack via deploy.sh (best-effort)..." ; \
		./deploy.sh stop || true; \
		echo "Stopping deploy compose stacks (with volumes)..." ; \
		if [ -f docker-compose.runner-pool.generated.yml ]; then \
			docker compose -f docker-compose.kafka.yml -f docker-compose.master-api.yml -f docker-compose.runner-pool.generated.yml -f docker-compose.web-ui.yml down -v --remove-orphans || true; \
		elif [ -f docker-compose.runner-api.yml ]; then \
			docker compose -f docker-compose.kafka.yml -f docker-compose.master-api.yml -f docker-compose.runner-api.yml -f docker-compose.web-ui.yml down -v --remove-orphans || true; \
		else \
			docker compose -f docker-compose.kafka.yml -f docker-compose.master-api.yml -f docker-compose.web-ui.yml down -v --remove-orphans || true; \
		fi; \
		docker compose -f docker-compose.kafka.yml down -v --remove-orphans || true; \
		echo "Removing generated compose files..." ; \
		rm -f docker-compose.runner-pool.generated.yml; \
		if [ "$(CLEAN_PRUNE)" = "1" ]; then \
			echo "Pruning unused Docker data..." ; \
			docker system prune -f; \
		fi; \
		echo "✅ clean-deploy complete."

# Production (legacy - use deploy instead)
prod: check-secrets
	docker compose up --build -d

# Deployment (New Kafka Architecture)
deploy: check-secrets
	@echo "🚀 Deploying GigaEvo Platform with Kafka architecture..."
	./deploy.sh deploy

deploy-infrastructure:
	@echo "🏗️ Deploying infrastructure services..."
	docker compose -f docker-compose.kafka.yml up -d
	@echo "✅ Infrastructure deployed (PostgreSQL, Kafka, Redis, MinIO)"

deploy-applications: check-secrets
	@echo "🎯 Deploying application services..."
	@RUNNER_POOL_SIZE=$${RUNNER_POOL_SIZE:-1}; \
	RUNNER_REDIS_DB_START=$${RUNNER_REDIS_DB_START:-1}; \
	REDIS_DATABASES_MIN=$${REDIS_DATABASES_MIN:-512}; \
	export RUNNER_POOL_SIZE RUNNER_REDIS_DB_START; \
	req_dbs=$$((RUNNER_REDIS_DB_START + RUNNER_POOL_SIZE)); \
	if [ $$req_dbs -gt $$REDIS_DATABASES_MIN ]; then \
		max_pool=$$((REDIS_DATABASES_MIN - RUNNER_REDIS_DB_START)); \
		echo "❌ ERROR: RUNNER_POOL_SIZE=$$RUNNER_POOL_SIZE exceeds Redis DB limit." ; \
		echo "   Required databases: RUNNER_REDIS_DB_START($$RUNNER_REDIS_DB_START) + RUNNER_POOL_SIZE($$RUNNER_POOL_SIZE) = $$req_dbs" ; \
		echo "   Configured REDIS_DATABASES_MIN=$$REDIS_DATABASES_MIN" ; \
		echo "   Max supported RUNNER_POOL_SIZE is $$max_pool (for RUNNER_REDIS_DB_START=$$RUNNER_REDIS_DB_START)." ; \
		exit 1; \
	fi; \
	export REDIS_DATABASES=$$REDIS_DATABASES_MIN; \
	python3 generate_runner_pool_compose.py --mode deploy --output docker-compose.runner-pool.generated.yml; \
	docker compose -f docker-compose.kafka.yml -f docker-compose.master-api.yml up -d --build; \
	docker compose -f docker-compose.kafka.yml -f docker-compose.runner-pool.generated.yml up -d --build; \
	docker compose -f docker-compose.kafka.yml -f docker-compose.master-api.yml -f docker-compose.web-ui.yml up -d --build
	@echo "✅ Applications deployed (Master API, Runner API, Web UI)"

stop:
	@echo "🛑 Stopping all GigaEvo Platform services..."
	./deploy.sh stop

restart:
	@if [ -z "$(SERVICE)" ]; then \
		echo "❌ Please specify service: make restart SERVICE=master-api|runner-api|web-ui|kafka"; \
	else \
		./deploy.sh restart $(SERVICE); \
	fi

# Docker commands
docker-build:
	docker compose build

docker-up: check-secrets
	docker compose up -d

docker-down:
	docker compose down

docker-clean:
	docker compose down -v
	docker system prune -f

# Code quality
lint:
	ruff check --fix .

format:
	ruff format .

# Testing
test:
	@echo "Run tests for each component:"
	@echo "  cd master_api && python -m pytest"
	@echo "  cd runner_api && python -m pytest"
	@echo "  cd web_ui && python -m pytest"

# Database
db-reset:
	@echo "🗑️ Dropping and recreating database..."
	docker compose -f docker-compose.kafka.yml exec postgres psql -U gigaevouser -d postgres -c "DROP DATABASE IF EXISTS gigaevo_master;"
	docker compose -f docker-compose.kafka.yml exec postgres psql -U gigaevouser -d postgres -c "CREATE DATABASE gigaevo_master;"
	@echo "✅ Database reset complete"

db-migrate:
	@echo "Run database migrations:"
	@echo "  cd master_api && alembic upgrade head"

# Development helpers
master-api:
	cd master_api && python src/main.py

runner-api:
	cd runner_api && python src/main.py

web-ui:
	cd web_ui && python app.py

# Logs
logs:
	docker compose logs -f

logs-master:
	docker compose logs -f master-api

logs-runner:
	docker compose logs -f runner-api

logs-web:
	docker compose logs -f web-ui

# Service status
status:
	@echo "📊 GigaEvo Platform Service Status:"
	@echo ""
	./deploy.sh status