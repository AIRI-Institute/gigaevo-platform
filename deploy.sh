#!/bin/bash

# GEML Deployment Script
# This script sets up the three-tier architecture with Kafka

set -e

echo "🚀 Starting GEML Deployment..."

# Function to check if Docker network exists
create_network_if_not_exists() {
    if ! docker network ls | grep -q "geml-network"; then
        echo "📡 Creating Docker network: geml-network"
        docker network create geml-network
    else
        echo "📡 Docker network geml-network already exists"
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

    echo "📊 Starting Master API..."
    docker compose -f docker-compose.kafka.yml -f docker-compose.master-api.yml up -d --build master-api

    echo "⏳ Waiting for Master API to be healthy..."
    timeout 180s bash -c "until docker compose -f docker-compose.kafka.yml -f docker-compose.master-api.yml ps master-api | grep -q 'healthy\|Up (healthy)'; do sleep 3; echo 'Still waiting for Master API...'; done"
    echo "✅ Master API is healthy"

    echo "🏃 Starting Runner API..."
    docker compose -f docker-compose.kafka.yml -f docker-compose.runner-api.yml up -d --build runner-api

    echo "⏳ Waiting for Runner API to be healthy..."
    timeout 180s bash -c "until docker compose -f docker-compose.kafka.yml -f docker-compose.runner-api.yml ps runner-api | grep -q 'healthy\|Up (healthy)'; do sleep 10; echo 'Still waiting for Runner API...'; done"
    echo "✅ Runner API is healthy"

    echo "🌐 Starting Web UI..."
    docker compose -f docker-compose.kafka.yml -f docker-compose.master-api.yml -f docker-compose.web-ui.yml up -d --build web-ui

    echo "⏳ Waiting for Web UI to be healthy..."
    timeout 180s bash -c "until docker compose -f docker-compose.kafka.yml -f docker-compose.master-api.yml -f docker-compose.web-ui.yml ps web-ui | grep -q 'healthy\|Up (healthy)'; do sleep 3; echo 'Still waiting for Web UI...'; done"
    echo "✅ Web UI is healthy"
}

# Function to show deployment status
show_status() {
    echo ""
    echo "🎉 GEML Deployment Complete!"
    echo ""
    echo "📋 Service URLs:"
    echo "   • Master API:     http://localhost:8000"
    echo "   • Runner API:     http://localhost:8001"
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
    echo "🛑 Stopping all GEML services..."
    docker compose -f docker-compose.kafka.yml -f docker-compose.master-api.yml -f docker-compose.runner-api.yml -f docker-compose.web-ui.yml down
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
            echo "🔄 Restarting Runner API..."
            docker compose -f docker-compose.kafka.yml -f docker-compose.runner-api.yml restart runner-api
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
        docker compose -f docker-compose.kafka.yml -f docker-compose.master-api.yml -f docker-compose.runner-api.yml -f docker-compose.web-ui.yml ps
        ;;
    "logs")
        service=${2:-all}
        echo "📋 Showing logs for: $service"
        if [ "$service" = "all" ]; then
            docker compose -f docker-compose.kafka.yml logs -f
        else
            docker compose -f docker-compose.kafka.yml logs -f $service
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