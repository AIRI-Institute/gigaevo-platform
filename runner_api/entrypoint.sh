#!/bin/bash

# Fix permissions for the repos directory at runtime
# This ensures the volume can be written to by gemluser
echo "Setting up permissions for GEML application..."

# Always fix permissions as root
if [ "$(id -u)" = "0" ]; then
    echo "Running as root, fixing ownership of /app/repos..."
    chown -R gemluser:gemluser /app/repos
    chown -R gemluser:gemluser /app

    # Switch to gemluser for the main application
    echo "Switching to gemluser..."
    exec gosu gemluser "$@"
else
    echo "Running as non-root user $(id -un)"
    exec "$@"
fi