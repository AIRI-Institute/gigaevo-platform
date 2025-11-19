#!/bin/bash

# Fix permissions for the repos directory at runtime
# This ensures the volume can be written to by gigaevouser
echo "Setting up permissions for GigaEvo Platform application..."

# Always fix permissions as root
if [ "$(id -u)" = "0" ]; then
    echo "Running as root, fixing ownership of /app/repos..."
    chown -R gigaevouser:gigaevouser /app/repos
    chown -R gigaevouser:gigaevouser /app

    # Switch to gigaevouser for the main application
    echo "Switching to gigaevouser..."
    exec gosu gigaevouser "$@"
else
    echo "Running as non-root user $(id -un)"
    exec "$@"
fi