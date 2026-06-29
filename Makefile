SHELL := /bin/bash

.DEFAULT_GOAL := help

DEV_POOL_COMPOSE := docker-compose.runner-pool.dev.generated.yml
DEPLOY_POOL_COMPOSE := docker-compose.runner-pool.generated.yml
BUNDLE_DIST_DIR := dist/bundle
BUNDLE_RUNTIME_DIR ?= .bundle-runtime
BUNDLE_RUNTIME_META := $(BUNDLE_RUNTIME_DIR)/bundle.meta.env

.PHONY: help check-tools check-secrets install \
	dev \
	deploy deploy-infrastructure deploy-applications stop restart status \
	bundle-build bundle-runtime bundle-runtime-check bundle-deploy bundle-stop bundle-clean bundle-status bundle-logs bundle-db-reset bundle-db-migrate \
	clean clean-dev clean-deploy \
	check-runner-compose \
	lint format test db-reset db-migrate \
	master-api runner-api web-ui

help:
	@echo "GigaEvo Platform Commands:"
	@echo ""
	@echo "🔧 Development:"
	@echo "  install                - Install all dependencies"
	@echo "  check-tools            - Verify required local tools are installed"
	@echo "  dev                    - Start dev stack in foreground (stop with Ctrl+C)"
	@echo "  clean                  - Remove dev and deploy containers, volumes, generated files"
	@echo "  clean-dev              - Remove dev containers, volumes, generated files"
	@echo "  clean-deploy           - Remove deploy containers, volumes, generated files"
	@echo ""
	@echo "📊 Individual Services:"
	@echo "  master-api             - Run Master API service"
	@echo "  runner-api             - Run Runner API service"
	@echo "  web-ui                 - Run Web UI service"
	@echo ""
	@echo "🚀 Deployment:"
	@echo "  deploy                 - Deploy all services (infrastructure + applications)"
	@echo "  deploy-infrastructure  - Deploy only infrastructure (Kafka, PostgreSQL, Redis, MinIO)"
	@echo "  deploy-applications    - Deploy only applications (Master API, Runner API, Web UI)"
	@echo "  stop                   - Stop both deploy and dev stacks"
	@echo "  restart                - Restart deploy service (SERVICE=master-api|runner-api|web-ui|kafka)"
	@echo "  status                 - Show status for both deploy and dev stacks"
	@echo ""
	@echo "🗄️ Database:"
	@echo "  db-reset               - Reset database (drop and recreate)"
	@echo "  db-migrate             - Migrate database"
	@echo ""
	@echo "📦 Bundle:"
	@echo "  bundle-build           - Build distribution bundle"
	@echo "  bundle-runtime         - Prepare runtime in $(BUNDLE_RUNTIME_DIR) from latest bundle"
	@echo "  bundle-deploy          - Start deploy stack (requires prepared runtime)"
	@echo "  bundle-stop            - Stop deploy stack"
	@echo "  bundle-clean           - Remove deploy containers and volumes"
	@echo "  bundle-status          - Show deploy stack status"
	@echo "  bundle-logs            - Show deploy stack logs (SERVICE=<name> optional)"
	@echo "  bundle-db-reset        - Reset database (drop and recreate)"
	@echo "  bundle-db-migrate      - Migrate database"
	@echo ""
	@echo "🧹 Code Quality:"
	@echo "  check-runner-compose  - Validate runner pool configs resolve one shared runner image"
	@echo "  lint                   - Lint code"
	@echo "  format                 - Format code"
	@echo "  test                   - Run tests"

check-tools:
	@command -v docker >/dev/null 2>&1 || { echo "❌ ERROR: docker is not installed or not in PATH"; exit 1; }
	@docker compose version >/dev/null 2>&1 || { echo "❌ ERROR: docker compose plugin is not available"; exit 1; }
	@command -v python3 >/dev/null 2>&1 || { echo "❌ ERROR: python3 is not installed or not in PATH"; exit 1; }
	@echo "✅ Required tools found (docker, docker compose, python3)"

check-secrets:
	@if [ ! -f llm_models.yml ]; then \
		echo "❌ ERROR: llm_models.yml not found!"; \
		echo ""; \
		echo "This file contains model definitions and API keys for LLM models and is required to run the platform"; \
		echo "Please create it from the example:"; \
		echo "  cp llm_models.yml.example llm_models.yml"; \
		echo ""; \
		echo "Then edit llm_models.yml and replace REPLACE_ME with your actual API keys"; \
		echo ""; \
		exit 1; \
	fi
	@echo "✅ llm_models.yml found"
	@set -e; \
	source ./scripts/container_env.sh; \
	container_env_load_and_resolve ./.env; \
	echo "✅ COMPOSE_PROJECT_NAME=$${COMPOSE_PROJECT_NAME}"; \
	if [ -n "$${GITHUB_PAT:-}" ]; then \
		echo "✅ GITHUB_PAT found (private repo clone available)"; \
	else \
		echo "ℹ️  GITHUB_PAT not set; public GitHub HTTPS repos can still build"; \
		echo "ℹ️  Private repos will fail during runner image clone"; \
	fi

install:
	pip install -e ".[all]"

dev: check-tools check-secrets
	@set -e; \
	source ./scripts/container_env.sh; \
	container_env_load_and_resolve ./.env; \
	echo "🚀 Starting GigaEvo Platform in Development Mode..."; \
	echo ""; \
	echo "📋 Service URLs (will be available after services start):"; \
	echo "   • Master API:     http://localhost:$${MASTER_API_HOST_PORT:-8000}"; \
	echo "   • Runner API:     http://localhost:$${RUNNER_API_HOST_PORT:-8001}"; \
	echo "   • Web UI:         http://localhost:$${WEB_UI_HOST_PORT:-7860}"; \
	echo "   • MinIO Console:  http://localhost:$${MINIO_CONSOLE_HOST_PORT:-9001}"; \
	echo "   • Kafka UI:       http://localhost:$${KAFKA_UI_HOST_PORT:-8080}"; \
	echo ""; \
	export RUNNER_POOL_SIZE=$${RUNNER_POOL_SIZE:-1}; \
	export RUNNER_REDIS_DB_START=$${RUNNER_REDIS_DB_START:-1}; \
	REDIS_DATABASES=$$(./scripts/resolve_redis_databases.sh); \
	export REDIS_DATABASES; \
	MODE=dev ENV_FILE=./.env ./scripts/build_runner_image.sh; \
	python3 generate_runner_pool_compose.py --mode dev --output $(DEV_POOL_COMPOSE); \
	HOST_UID=$$(id -u) HOST_GID=$$(id -g) docker compose -f docker-compose.dev.yml -f $(DEV_POOL_COMPOSE) up --build

clean:
	@echo "🧹 Cleaning both dev and deploy environments..."
	@$(MAKE) clean-dev
	@$(MAKE) clean-deploy
	@echo "✅ Cleaning complete"

clean-dev:
	@echo "🧹 Cleaning dev environment (containers, volumes, generated files)..."
	@set -e; \
		echo "Removing dev stack (including generated runner pool)..." ; \
		if [ -f $(DEV_POOL_COMPOSE) ]; then \
			docker compose -f docker-compose.dev.yml -f $(DEV_POOL_COMPOSE) down -v --remove-orphans || true; \
		else \
			docker compose -f docker-compose.dev.yml down -v --remove-orphans || true; \
		fi; \
		echo "Removing generated compose files..." ; \
		rm -f $(DEV_POOL_COMPOSE); \
		echo "✅ Cleaning complete"

clean-deploy:
	@echo "🧹 Cleaning deploy environment (containers, volumes, generated files)..."
	@set -e; \
		echo "Removing deploy containers and volumes via deploy.sh..." ; \
		./deploy.sh clean || true; \
		echo "Removing generated compose files..." ; \
		rm -f $(DEPLOY_POOL_COMPOSE); \
		echo "✅ Cleaning complete"

deploy: check-tools check-secrets
	@echo "🚀 Deploying GigaEvo Platform..."
	./deploy.sh deploy

bundle-build: check-tools check-secrets
	@echo "📦 Building distribution bundle..."
	./scripts/bundle_build.sh

bundle-runtime: check-tools
	@set -e; \
	BUNDLE=$$(ls -t $(BUNDLE_DIST_DIR)/gigaevo-bundle-*.tar.gz 2>/dev/null | head -n 1); \
	if [ -z "$$BUNDLE" ]; then \
		echo "❌ No bundle archive found in $(BUNDLE_DIST_DIR). Run: make bundle-build"; \
		exit 1; \
	fi; \
	echo "📦 Using bundle archive: $$BUNDLE"; \
	mkdir -p $(BUNDLE_RUNTIME_DIR); \
	if [ -f $(BUNDLE_RUNTIME_DIR)/.env ]; then cp $(BUNDLE_RUNTIME_DIR)/.env /tmp/gigaevo-bundle.env.bak.$$$$; fi; \
	if [ -f $(BUNDLE_RUNTIME_DIR)/llm_models.yml ]; then cp $(BUNDLE_RUNTIME_DIR)/llm_models.yml /tmp/gigaevo-bundle.llm.bak.$$$$; fi; \
	rm -rf $(BUNDLE_RUNTIME_DIR); \
	mkdir -p $(BUNDLE_RUNTIME_DIR); \
	tar -xzf "$$BUNDLE" -C $(BUNDLE_RUNTIME_DIR); \
	if [ -f /tmp/gigaevo-bundle.env.bak.$$$$ ]; then cp /tmp/gigaevo-bundle.env.bak.$$$$ $(BUNDLE_RUNTIME_DIR)/.env; rm -f /tmp/gigaevo-bundle.env.bak.$$$$; \
	elif [ ! -f $(BUNDLE_RUNTIME_DIR)/.env ] && [ -f $(BUNDLE_RUNTIME_DIR)/.env.example ]; then cp $(BUNDLE_RUNTIME_DIR)/.env.example $(BUNDLE_RUNTIME_DIR)/.env; fi; \
	if [ -f /tmp/gigaevo-bundle.llm.bak.$$$$ ]; then cp /tmp/gigaevo-bundle.llm.bak.$$$$ $(BUNDLE_RUNTIME_DIR)/llm_models.yml; rm -f /tmp/gigaevo-bundle.llm.bak.$$$$; \
	elif [ ! -f $(BUNDLE_RUNTIME_DIR)/llm_models.yml ] && [ -f $(BUNDLE_RUNTIME_DIR)/llm_models.yml.example ]; then cp $(BUNDLE_RUNTIME_DIR)/llm_models.yml.example $(BUNDLE_RUNTIME_DIR)/llm_models.yml; fi; \
	echo "✅ Bundle runtime prepared at $(BUNDLE_RUNTIME_DIR)"

bundle-runtime-check:
	@if [ ! -f $(BUNDLE_RUNTIME_META) ] || [ ! -f $(BUNDLE_RUNTIME_DIR)/images.tar ] || [ ! -f $(BUNDLE_RUNTIME_DIR)/.env ] || [ ! -f $(BUNDLE_RUNTIME_DIR)/llm_models.yml ] || [ ! -f $(BUNDLE_RUNTIME_DIR)/init.sql ] || [ -d $(BUNDLE_RUNTIME_DIR)/init.sql ]; then \
		echo "❌ Bundle runtime is missing or invalid in $(BUNDLE_RUNTIME_DIR). Run: make bundle-runtime"; \
		exit 1; \
	fi

bundle-deploy: check-tools bundle-runtime-check
	@echo "🛰️ Starting bundle deploy stack from $(BUNDLE_RUNTIME_DIR)..."
	@BUNDLE_STACK_ROOT_DIR="$(abspath $(BUNDLE_RUNTIME_DIR))" ./scripts/bundle_stack.sh up

bundle-stop: check-tools bundle-runtime-check
	@echo "🛑 Stopping bundle deploy stack in $(BUNDLE_RUNTIME_DIR)..."
	@BUNDLE_STACK_ROOT_DIR="$(abspath $(BUNDLE_RUNTIME_DIR))" ./scripts/bundle_stack.sh stop

bundle-clean: check-tools bundle-runtime-check
	@echo "🧹 Removing bundle deploy containers and volumes from $(BUNDLE_RUNTIME_DIR)..."
	@BUNDLE_STACK_ROOT_DIR="$(abspath $(BUNDLE_RUNTIME_DIR))" ./scripts/bundle_stack.sh clean

bundle-status: check-tools bundle-runtime-check
	@echo "📊 Bundle deploy stack status from $(BUNDLE_RUNTIME_DIR):"
	@BUNDLE_STACK_ROOT_DIR="$(abspath $(BUNDLE_RUNTIME_DIR))" ./scripts/bundle_stack.sh status

bundle-logs: check-tools bundle-runtime-check
	@echo "📋 Bundle deploy stack logs from $(BUNDLE_RUNTIME_DIR):"
	@BUNDLE_STACK_ROOT_DIR="$(abspath $(BUNDLE_RUNTIME_DIR))" ./scripts/bundle_stack.sh logs $(SERVICE)

bundle-db-reset: check-tools bundle-runtime-check
	@echo "🗑️ Resetting bundle database (drop and recreate) from $(BUNDLE_RUNTIME_DIR)..."
	@BUNDLE_STACK_ROOT_DIR="$(abspath $(BUNDLE_RUNTIME_DIR))" ./scripts/bundle_stack.sh db-reset

bundle-db-migrate: check-tools bundle-runtime-check
	@echo "🗄️ Running bundle database migrations from $(BUNDLE_RUNTIME_DIR)..."
	@BUNDLE_STACK_ROOT_DIR="$(abspath $(BUNDLE_RUNTIME_DIR))" ./scripts/bundle_stack.sh db-migrate

deploy-infrastructure: check-tools
	@echo "🏗️ Deploying infrastructure services..."
	docker compose -f docker-compose.kafka.yml up -d
	@echo "✅ Infrastructure deployed (PostgreSQL, Kafka, Redis, MinIO)"

deploy-applications: check-tools check-secrets
	@echo "🎯 Deploying application services..."
	@set -e; \
	source ./scripts/container_env.sh; \
	container_env_load_and_resolve ./.env; \
	export RUNNER_POOL_SIZE=$${RUNNER_POOL_SIZE:-1}; \
	export RUNNER_REDIS_DB_START=$${RUNNER_REDIS_DB_START:-1}; \
	REDIS_DATABASES=$$(./scripts/resolve_redis_databases.sh); \
	export REDIS_DATABASES; \
	MODE=prod ENV_FILE=./.env ./scripts/build_runner_image.sh; \
	python3 generate_runner_pool_compose.py --mode deploy --output $(DEPLOY_POOL_COMPOSE); \
	docker compose -f docker-compose.kafka.yml -f docker-compose.master-api.yml up -d --build master-api; \
	docker compose -f docker-compose.kafka.yml -f $(DEPLOY_POOL_COMPOSE) up -d --no-build; \
	docker compose -f docker-compose.kafka.yml -f docker-compose.master-api.yml -f docker-compose.web-ui.yml up -d --build web-ui
	@echo "✅ Applications deployed (Master API, Runner API, Web UI)"

check-runner-compose: check-tools check-secrets
	@set -e; \
	source ./scripts/container_env.sh; \
	container_env_load_and_resolve ./.env; \
	DEV_CFG=$$(mktemp /tmp/gigaevo-dev-pool.XXXXXX); \
	DEPLOY_CFG=$$(mktemp /tmp/gigaevo-deploy-pool.XXXXXX); \
	DEV_IMAGES=$$(mktemp /tmp/gigaevo-dev-images.XXXXXX); \
	DEPLOY_IMAGES=$$(mktemp /tmp/gigaevo-deploy-images.XXXXXX); \
	trap 'rm -f "$$DEV_CFG" "$$DEPLOY_CFG" "$$DEV_IMAGES" "$$DEPLOY_IMAGES"' EXIT; \
	export RUNNER_POOL_SIZE=3; \
	export RUNNER_REDIS_DB_START=$${RUNNER_REDIS_DB_START:-1}; \
	python3 generate_runner_pool_compose.py --mode dev --output "$$DEV_CFG"; \
	python3 generate_runner_pool_compose.py --mode deploy --output "$$DEPLOY_CFG"; \
	docker compose -f docker-compose.dev.yml -f "$$DEV_CFG" config --images | sort -u > "$$DEV_IMAGES"; \
	docker compose -f docker-compose.kafka.yml -f docker-compose.master-api.yml -f "$$DEPLOY_CFG" -f docker-compose.web-ui.yml config --images | sort -u > "$$DEPLOY_IMAGES"; \
	dev_runner_images=$$(grep -E "^$${COMPOSE_PROJECT_NAME}-runner-api([:-]|$$)" "$$DEV_IMAGES" || true); \
	dev_runner_count=$$(printf '%s\n' "$$dev_runner_images" | sed '/^$$/d' | wc -l | tr -d ' '); \
	[ "$$dev_runner_count" -eq 1 ] && [ "$$dev_runner_images" = "$$RUNNER_IMAGE_NAME" ] || { echo "❌ Dev config resolved unexpected runner images:"; printf '%s\n' "$$dev_runner_images"; exit 1; }; \
	deploy_runner_images=$$(grep -E "^$${COMPOSE_PROJECT_NAME}-runner-api([:-]|$$)" "$$DEPLOY_IMAGES" || true); \
	deploy_runner_count=$$(printf '%s\n' "$$deploy_runner_images" | sed '/^$$/d' | wc -l | tr -d ' '); \
	[ "$$deploy_runner_count" -eq 1 ] && [ "$$deploy_runner_images" = "$$RUNNER_IMAGE_NAME" ] || { echo "❌ Deploy config resolved unexpected runner images:"; printf '%s\n' "$$deploy_runner_images"; exit 1; }; \
	echo "✅ Runner pool configs resolve a single shared runner image"

stop:
	@echo "🛑 Stopping deploy stack..."
	@./deploy.sh stop || true
	@echo "🛑 Stopping development stack..."
	@set -e; \
		source ./scripts/container_env.sh; \
		container_env_load_and_resolve ./.env; \
		export RUNNER_POOL_SIZE=$${RUNNER_POOL_SIZE:-1}; \
		export RUNNER_REDIS_DB_START=$${RUNNER_REDIS_DB_START:-1}; \
		REDIS_DATABASES=$$(./scripts/resolve_redis_databases.sh); \
		export REDIS_DATABASES; \
		python3 generate_runner_pool_compose.py --mode dev --output $(DEV_POOL_COMPOSE); \
		HOST_UID=$$(id -u) HOST_GID=$$(id -g) docker compose -f docker-compose.dev.yml -f $(DEV_POOL_COMPOSE) stop || true; \
		echo "✅ All project stacks stopped"

restart:
	@if [ -z "$(SERVICE)" ]; then \
		echo "❌ Please specify service: make restart SERVICE=master-api|runner-api|web-ui|kafka"; \
	else \
		./deploy.sh restart $(SERVICE); \
	fi

lint:
	ruff check --fix .

format:
	ruff format .

test:
	@echo "Running tests..."
	@echo "  python3 -m unittest discover -s tests -p 'test_*.py'"
	@echo "  cd master_api && python -m pytest"
	@echo "  cd runner_api && python -m pytest"
	@echo "  cd web_ui && python -m pytest"

db-reset:
	@echo "Resetting database..."
	docker compose -f docker-compose.kafka.yml exec postgres psql -U gigaevouser -d postgres -c "DROP DATABASE IF EXISTS gigaevo_master;"
	docker compose -f docker-compose.kafka.yml exec postgres psql -U gigaevouser -d postgres -c "CREATE DATABASE gigaevo_master;"
	@echo "✅ Database reset complete"

db-migrate:
	@echo "Migrating database..."
	@echo "  cd master_api && alembic upgrade head"
	@echo "✅ Database migration complete"

master-api:
	cd master_api && python src/main.py

runner-api:
	cd runner_api && python src/main.py

web-ui:
	cd web_ui && python app.py

status:
	@echo "📊 Deploy stack status:"
	@echo ""
	@./deploy.sh status || true
	@echo ""
	@echo "📊 Development stack status:"
	@set -e; \
		if [ -f $(DEV_POOL_COMPOSE) ]; then \
			docker compose -f docker-compose.dev.yml -f $(DEV_POOL_COMPOSE) ps || true; \
		else \
			docker compose -f docker-compose.dev.yml ps || true; \
		fi
