#!/bin/bash

# GigaEvo Platform Deployment Script
# This script sets up the three-tier architecture with Kafka

set -e

echo "🚀 Starting GigaEvo Platform Deployment..."

RUNNER_POOL_COMPOSE_FILE="docker-compose.runner-pool.generated.yml"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REDIS_DB_RESOLVER="${SCRIPT_DIR}/scripts/resolve_redis_databases.sh"
ENV_FILE="${SCRIPT_DIR}/.env"

cd "${SCRIPT_DIR}"

# shellcheck disable=SC1091
source "${SCRIPT_DIR}/scripts/container_env.sh"
container_env_load_and_resolve "${ENV_FILE}"

network_name() {
    printf '%s' "${GIGAEVO_NETWORK_NAME:-gigaevo-network}"
}

build_runner_image() {
    MODE=prod ENV_FILE="${ENV_FILE}" "${SCRIPT_DIR}/scripts/build_runner_image.sh"
}

resolve_runner_pool_env() {
    export RUNNER_POOL_SIZE="${RUNNER_POOL_SIZE:-1}"
    export RUNNER_REDIS_DB_START="${RUNNER_REDIS_DB_START:-1}"
    REDIS_DATABASES="$(${REDIS_DB_RESOLVER})"
    export REDIS_DATABASES
}

generate_runner_pool_compose() {
    resolve_runner_pool_env
    python3 generate_runner_pool_compose.py --mode deploy --output "${RUNNER_POOL_COMPOSE_FILE}"
}

# Function to check if Docker network exists
create_network_if_not_exists() {
    local net
    net="$(network_name)"
    if ! docker network inspect "${net}" >/dev/null 2>&1; then
        echo "📡 Creating Docker network: ${net}"
        docker network create "${net}"
    else
        echo "📡 Docker network ${net} already exists"
    fi
}

# Function to check if Docker volume exists
create_volume_if_not_exists() {
    if ! docker volume ls | grep -q "input_data"; then
        echo "📦 Creating Docker volume: input_data"
        docker volume create input_data
    else
        echo "📦 Docker volume input_data already exists"
    fi
}

# Function to start infrastructure services
start_infrastructure() {
    echo "🏗️  Starting infrastructure services (PostgreSQL, Kafka, Redis, MinIO)..."
    resolve_runner_pool_env
    docker compose -f docker-compose.kafka.yml up -d

    echo "⏳ Waiting for services to be healthy..."
    sleep 30

    # Check service health
    echo "🔍 Checking service health..."
    for service in postgres kafka redis redis-gigavolve minio; do
        echo "Checking $service..."
        timeout 90s bash -c "until docker compose -f docker-compose.kafka.yml ps $service | grep -q 'healthy\|Up (healthy)'; do sleep 3; echo 'Still waiting for $service...'; done"
        echo "✅ $service is healthy"
    done
}

# Function to start application services
start_applications() {
    echo "🎯 Starting application services..."
    resolve_runner_pool_env

    echo "📊 Starting Master API..."
    docker compose -f docker-compose.kafka.yml -f docker-compose.master-api.yml up -d --build master-api

    echo "⏳ Waiting for Master API to be healthy..."
    timeout 180s bash -c "until docker compose -f docker-compose.kafka.yml -f docker-compose.master-api.yml ps master-api | grep -q 'healthy\|Up (healthy)'; do sleep 3; echo 'Still waiting for Master API...'; done"
    echo "✅ Master API is healthy"

    echo "🏃 Generating Runner API pool compose (N=${RUNNER_POOL_SIZE})..."
    generate_runner_pool_compose

    echo "🏗️  Building shared Runner API image..."
    build_runner_image

    echo "🏃 Starting Runner API pool..."
    docker compose -f docker-compose.kafka.yml -f "${RUNNER_POOL_COMPOSE_FILE}" up -d --no-build

    echo "⏳ Waiting for Runner API pool to be healthy..."
    for i in $(seq 1 "${RUNNER_POOL_SIZE}"); do
        svc="runner-api-${i}"
        echo "Waiting for ${svc}..."
        timeout 180s bash -c "until docker compose -f docker-compose.kafka.yml -f \"${RUNNER_POOL_COMPOSE_FILE}\" ps ${svc} | grep -q 'healthy\\|Up (healthy)'; do sleep 10; echo 'Still waiting for ${svc}...'; done"
        echo "✅ ${svc} is healthy"
    done

    echo "🌐 Starting Web UI..."
    docker compose -f docker-compose.kafka.yml -f docker-compose.master-api.yml -f docker-compose.web-ui.yml up -d --build web-ui

    echo "⏳ Waiting for Web UI to be healthy..."
    timeout 180s bash -c "until docker compose -f docker-compose.kafka.yml -f docker-compose.master-api.yml -f docker-compose.web-ui.yml ps web-ui | grep -q 'healthy\|Up (healthy)'; do sleep 3; echo 'Still waiting for Web UI...'; done"
    echo "✅ Web UI is healthy"
}

# Function to show deployment status
show_status() {
    echo ""
    echo "🎉 GigaEvo Platform Deployment Complete!"
    echo ""
    echo "📋 Service URLs:"
    echo "   • Master API:     http://localhost:${MASTER_API_HOST_PORT:-8000}"
    echo "   • Runner API:     http://localhost:${RUNNER_API_HOST_PORT:-8001} (runner-api-1)"
    echo "   • Web UI:         http://localhost:${WEB_UI_HOST_PORT:-7860}"
    echo "   • MinIO Console:  http://localhost:${MINIO_CONSOLE_HOST_PORT:-9001}"
    echo ""
    echo "🔧 Management Commands:"
    echo "   • View logs:       docker compose -f docker-compose.kafka.yml logs -f [service]"
    echo "   • Stop all:        ./deploy.sh stop"
    echo "   • Clean all:       ./deploy.sh clean"
    echo "   • Restart service:  ./deploy.sh restart [service]"
    echo ""
    echo "📊 Kafka Topics (for debugging):"
    echo "   • experiment-config"
    echo "   • experiment-prepared"
    echo "   • experiment-started"
    echo "   • experiment-stopped"
    echo "   • runner-status"
}

# Function to stop services
stop_services() {
    echo "🛑 Stopping all GigaEvo Platform services..."
    generate_runner_pool_compose
    docker compose -f docker-compose.kafka.yml -f docker-compose.master-api.yml -f "${RUNNER_POOL_COMPOSE_FILE}" -f docker-compose.web-ui.yml stop
    echo "✅ All services stopped"
}

# Function to remove services
clean_services() {
    echo "🧹 Removing all GigaEvo Platform containers and volumes..."
    generate_runner_pool_compose
    docker compose -f docker-compose.kafka.yml -f docker-compose.master-api.yml -f "${RUNNER_POOL_COMPOSE_FILE}" -f docker-compose.web-ui.yml down -v --remove-orphans
    docker compose -f docker-compose.kafka.yml down -v --remove-orphans
    echo "✅ All services removed"
}

# Function to restart a specific service
restart_service() {
    local service=$1
    if [ -z "$service" ]; then
        echo "❌ Please specify a service (master-api, runner-api, web-ui, kafka)"
        exit 1
    fi

    case $service in
        "master-api")
            echo "🔄 Restarting Master API..."
            docker compose -f docker-compose.kafka.yml -f docker-compose.master-api.yml restart master-api
            ;;
        "runner-api")
            echo "🔄 Restarting Runner API pool..."
            generate_runner_pool_compose
            for i in $(seq 1 "${RUNNER_POOL_SIZE}"); do
                docker compose -f docker-compose.kafka.yml -f "${RUNNER_POOL_COMPOSE_FILE}" restart "runner-api-${i}"
            done
            ;;
        "web-ui")
            echo "🔄 Restarting Web UI..."
            docker compose -f docker-compose.kafka.yml -f docker-compose.master-api.yml -f docker-compose.web-ui.yml restart web-ui
            ;;
        "kafka")
            echo "🔄 Restarting Kafka infrastructure..."
            docker compose -f docker-compose.kafka.yml restart kafka
            ;;
        *)
            echo "❌ Unknown service: $service"
            echo "Available services: master-api, runner-api, web-ui, kafka"
            exit 1
            ;;
    esac
    echo "✅ $service restarted"
}

# Main script logic
case "${1:-deploy}" in
    "deploy")
        create_network_if_not_exists
        create_volume_if_not_exists
        start_infrastructure
        start_applications
        show_status
        ;;
    "stop")
        stop_services
        ;;
    "clean")
        clean_services
        ;;
    "restart")
        restart_service $2
        ;;
    "status")
        echo "📊 Service Status:"
        generate_runner_pool_compose
        docker compose -f docker-compose.kafka.yml -f docker-compose.master-api.yml -f "${RUNNER_POOL_COMPOSE_FILE}" -f docker-compose.web-ui.yml ps
        ;;
    "logs")
        service=${2:-all}
        echo "📋 Showing logs for: $service"
        generate_runner_pool_compose
        if [ "$service" = "all" ]; then
            docker compose -f docker-compose.kafka.yml -f docker-compose.master-api.yml -f "${RUNNER_POOL_COMPOSE_FILE}" -f docker-compose.web-ui.yml logs -f
        else
            docker compose -f docker-compose.kafka.yml -f docker-compose.master-api.yml -f "${RUNNER_POOL_COMPOSE_FILE}" -f docker-compose.web-ui.yml logs -f $service
        fi
        ;;
    *)
        echo "❌ Unknown command: $1"
        echo "Usage: $0 [deploy|stop|clean|restart|status|logs] [service]"
        echo ""
        echo "Commands:"
        echo "  deploy           Deploy all services (default)"
        echo "  stop             Stop all services without removing containers"
        echo "  clean            Remove all services, containers, and volumes"
        echo "  restart [svc]    Restart specific service"
        echo "  status           Show service status"
        echo "  logs [svc]       Show service logs"
        echo ""
        echo "Services: master-api, runner-api, web-ui, kafka"
        exit 1
        ;;
esac
