.PHONY: help install dev prod clean lint format test docker-build docker-up docker-down deploy-infrastructure deploy-applications

# Default target
help:
	@echo "GigaEvo Platform Commands:"
	@echo ""
	@echo "🔧 Development:"
	@echo "  install                - Install all dependencies"
	@echo "  dev                    - Start development environment"
	@echo "  prod                   - Start production environment"
	@echo "  clean                  - Clean up containers and volumes"
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

# Installation
install:
	pip install -e ".[all]"

# Development
dev:
	@echo "🚀 Starting GigaEvo Platform in Development Mode..."
	@echo ""
	@echo "📋 Service URLs (will be available after services start):"
	@echo "   • Master API:     http://localhost:8000"
	@echo "   • Runner API:     http://localhost:8001"
	@echo "   • Web UI:         http://localhost:7860"
	@echo "   • MinIO Console:  http://localhost:9001"
	@echo "   • Kafka UI:       http://localhost:8080"
	@echo ""
	HOST_UID=$$(id -u) HOST_GID=$$(id -g) docker compose -f docker-compose.dev.yml up --build

# Production (legacy - use deploy instead)
prod:
	docker compose up --build -d

# Deployment (New Kafka Architecture)
deploy:
	@echo "🚀 Deploying GigaEvo Platform with Kafka architecture..."
	./deploy.sh deploy

deploy-infrastructure:
	@echo "🏗️ Deploying infrastructure services..."
	docker compose -f docker-compose.kafka.yml up -d
	@echo "✅ Infrastructure deployed (PostgreSQL, Kafka, Redis, MinIO)"

deploy-applications:
	@echo "🎯 Deploying application services..."
	docker compose -f docker-compose.master-api.yml up -d --build
	docker compose -f docker-compose.runner-api.yml up -d --build
	docker compose -f docker-compose.web-ui.yml up -d --build
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

docker-up:
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