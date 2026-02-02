#!/bin/bash

# GigaEvo Platform Deployment Script
# This script sets up the three-tier architecture with Kafka

set -e

echo "🚀 Starting GigaEvo Platform Deployment..."

RUNNER_POOL_COMPOSE_FILE="docker-compose.runner-pool.generated.yml"

# Load pool size defaults from .env if present (Compose uses .env, but this script also needs the values).
if [ -f .env ]; then
    if [ -z "${RUNNER_POOL_SIZE:-}" ]; then
        RUNNER_POOL_SIZE=$(grep -E '^RUNNER_POOL_SIZE=' .env | tail -n 1 | cut -d= -f2- | tr -d '"' | tr -d "'" || true)
        export RUNNER_POOL_SIZE
    fi
    if [ -z "${RUNNER_REDIS_DB_START:-}" ]; then
        RUNNER_REDIS_DB_START=$(grep -E '^RUNNER_REDIS_DB_START=' .env | tail -n 1 | cut -d= -f2- | tr -d '"' | tr -d "'" || true)
        export RUNNER_REDIS_DB_START
    fi
fi

# Function to check if Docker network exists
create_network_if_not_exists() {
    if ! docker network ls | grep -q "gigaevo-network"; then
        echo "📡 Creating Docker network: gigaevo-network"
        docker network create gigaevo-network
    else
        echo "📡 Docker network gigaevo-network already exists"
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
    # Ensure Redis has enough logical DBs for the runner pool.
    # IMPORTANT: never shrink this automatically (shrinking can break Redis RDB load if old DB indexes exist).
    RUNNER_POOL_SIZE=${RUNNER_POOL_SIZE:-1}
    RUNNER_REDIS_DB_START=${RUNNER_REDIS_DB_START:-1}
    REDIS_DATABASES_MIN=${REDIS_DATABASES_MIN:-512}
    export RUNNER_POOL_SIZE RUNNER_REDIS_DB_START
    req_dbs=$((RUNNER_REDIS_DB_START + RUNNER_POOL_SIZE))
    if [ "$req_dbs" -gt "$REDIS_DATABASES_MIN" ]; then
        max_pool=$((REDIS_DATABASES_MIN - RUNNER_REDIS_DB_START))
        echo "❌ ERROR: RUNNER_POOL_SIZE=${RUNNER_POOL_SIZE} exceeds Redis DB limit."
        echo "   Required databases: RUNNER_REDIS_DB_START(${RUNNER_REDIS_DB_START}) + RUNNER_POOL_SIZE(${RUNNER_POOL_SIZE}) = ${req_dbs}"
        echo "   Configured REDIS_DATABASES_MIN=${REDIS_DATABASES_MIN}"
        echo "   Max supported RUNNER_POOL_SIZE is ${max_pool} (for RUNNER_REDIS_DB_START=${RUNNER_REDIS_DB_START})."
        exit 1
    fi
    export REDIS_DATABASES="${REDIS_DATABASES_MIN}"
    docker compose -f docker-compose.kafka.yml up -d

    echo "⏳ Waiting for services to be healthy..."
    sleep 30

    # Check service health
    echo "🔍 Checking service health..."
    for service in postgres zookeeper kafka redis redis-gigavolve minio; do
        echo "Checking $service..."
        timeout 90s bash -c "until docker compose -f docker-compose.kafka.yml ps $service | grep -q 'healthy\|Up (healthy)'; do sleep 3; echo 'Still waiting for $service...'; done"
        echo "✅ $service is healthy"
    done
}

# Function to start application services
start_applications() {
    echo "🎯 Starting application services..."

    # Runner pool sizing and Redis DB count
    RUNNER_POOL_SIZE=${RUNNER_POOL_SIZE:-1}
    RUNNER_REDIS_DB_START=${RUNNER_REDIS_DB_START:-1}
    REDIS_DATABASES_MIN=${REDIS_DATABASES_MIN:-512}
    export RUNNER_POOL_SIZE RUNNER_REDIS_DB_START
    req_dbs=$((RUNNER_REDIS_DB_START + RUNNER_POOL_SIZE))
    if [ "$req_dbs" -gt "$REDIS_DATABASES_MIN" ]; then
        max_pool=$((REDIS_DATABASES_MIN - RUNNER_REDIS_DB_START))
        echo "❌ ERROR: RUNNER_POOL_SIZE=${RUNNER_POOL_SIZE} exceeds Redis DB limit."
        echo "   Required databases: RUNNER_REDIS_DB_START(${RUNNER_REDIS_DB_START}) + RUNNER_POOL_SIZE(${RUNNER_POOL_SIZE}) = ${req_dbs}"
        echo "   Configured REDIS_DATABASES_MIN=${REDIS_DATABASES_MIN}"
        echo "   Max supported RUNNER_POOL_SIZE is ${max_pool} (for RUNNER_REDIS_DB_START=${RUNNER_REDIS_DB_START})."
        exit 1
    fi
    export REDIS_DATABASES="${REDIS_DATABASES_MIN}"

    echo "📊 Starting Master API..."
    docker compose -f docker-compose.kafka.yml -f docker-compose.master-api.yml up -d --build master-api

    echo "⏳ Waiting for Master API to be healthy..."
    timeout 180s bash -c "until docker compose -f docker-compose.kafka.yml -f docker-compose.master-api.yml ps master-api | grep -q 'healthy\|Up (healthy)'; do sleep 3; echo 'Still waiting for Master API...'; done"
    echo "✅ Master API is healthy"

    echo "🏃 Generating Runner API pool compose (N=${RUNNER_POOL_SIZE})..."
    python3 generate_runner_pool_compose.py --mode deploy --output "${RUNNER_POOL_COMPOSE_FILE}"

    echo "🏃 Starting Runner API pool..."
    docker compose -f docker-compose.kafka.yml -f "${RUNNER_POOL_COMPOSE_FILE}" up -d --build

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
    echo "   • Master API:     http://localhost:8000"
    echo "   • Runner API:     http://localhost:8001 (runner-api-1)"
    echo "   • Web UI:         http://localhost:7860"
    echo "   • MinIO Console:  http://localhost:9001"
    echo ""
    echo "🔧 Management Commands:"
    echo "   • View logs:       docker compose -f docker-compose.kafka.yml logs -f [service]"
    echo "   • Stop all:        ./deploy.sh stop"
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
    if [ -f "${RUNNER_POOL_COMPOSE_FILE}" ]; then
        docker compose -f docker-compose.kafka.yml -f docker-compose.master-api.yml -f "${RUNNER_POOL_COMPOSE_FILE}" -f docker-compose.web-ui.yml down
    else
        docker compose -f docker-compose.kafka.yml -f docker-compose.master-api.yml -f docker-compose.runner-api.yml -f docker-compose.web-ui.yml down
    fi
    docker compose -f docker-compose.kafka.yml down
    echo "✅ All services stopped"
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
            RUNNER_POOL_SIZE=${RUNNER_POOL_SIZE:-1}
            if [ -f "${RUNNER_POOL_COMPOSE_FILE}" ]; then
                for i in $(seq 1 "${RUNNER_POOL_SIZE}"); do
                    docker compose -f docker-compose.kafka.yml -f "${RUNNER_POOL_COMPOSE_FILE}" restart "runner-api-${i}"
                done
            else
                docker compose -f docker-compose.kafka.yml -f docker-compose.runner-api.yml restart runner-api
            fi
            ;;
        "web-ui")
            echo "🔄 Restarting Web UI..."
            docker compose -f docker-compose.kafka.yml -f docker-compose.master-api.yml -f docker-compose.web-ui.yml restart web-ui
            ;;
        "kafka")
            echo "🔄 Restarting Kafka infrastructure..."
            docker compose -f docker-compose.kafka.yml restart kafka zookeeper
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
    "restart")
        restart_service $2
        ;;
    "status")
        echo "📊 Service Status:"
        if [ -f "${RUNNER_POOL_COMPOSE_FILE}" ]; then
            docker compose -f docker-compose.kafka.yml -f docker-compose.master-api.yml -f "${RUNNER_POOL_COMPOSE_FILE}" -f docker-compose.web-ui.yml ps
        else
            docker compose -f docker-compose.kafka.yml -f docker-compose.master-api.yml -f docker-compose.runner-api.yml -f docker-compose.web-ui.yml ps
        fi
        ;;
    "logs")
        service=${2:-all}
        echo "📋 Showing logs for: $service"
        if [ "$service" = "all" ]; then
            if [ -f "${RUNNER_POOL_COMPOSE_FILE}" ]; then
                docker compose -f docker-compose.kafka.yml -f docker-compose.master-api.yml -f "${RUNNER_POOL_COMPOSE_FILE}" -f docker-compose.web-ui.yml logs -f
            else
                docker compose -f docker-compose.kafka.yml -f docker-compose.master-api.yml -f docker-compose.runner-api.yml -f docker-compose.web-ui.yml logs -f
            fi
        else
            if [ -f "${RUNNER_POOL_COMPOSE_FILE}" ]; then
                docker compose -f docker-compose.kafka.yml -f docker-compose.master-api.yml -f "${RUNNER_POOL_COMPOSE_FILE}" -f docker-compose.web-ui.yml logs -f $service
            else
                docker compose -f docker-compose.kafka.yml -f docker-compose.master-api.yml -f docker-compose.runner-api.yml -f docker-compose.web-ui.yml logs -f $service
            fi
        fi
        ;;
    *)
        echo "❌ Unknown command: $1"
        echo "Usage: $0 [deploy|stop|restart|status|logs] [service]"
        echo ""
        echo "Commands:"
        echo "  deploy           Deploy all services (default)"
        echo "  stop            Stop all services"
        echo "  restart [svc]    Restart specific service"
        echo "  status           Show service status"
        echo "  logs [svc]       Show service logs"
        echo ""
        echo "Services: master-api, runner-api, web-ui, kafka"
        exit 1
        ;;
esac