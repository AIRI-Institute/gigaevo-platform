#!/bin/bash

# Fix permissions for the repos directory at runtime
# This ensures the volume can be written to by the correct user
echo "Setting up permissions for GigaEvo Platform application..."

# Get host user UID/GID from environment or use defaults
HOST_UID=${HOST_UID:-1000}
HOST_GID=${HOST_GID:-1000}

# Always fix permissions as root
if [ "$(id -u)" = "0" ]; then
    echo "Running as root, fixing ownership of /app/repos..."
    echo "Using UID: $HOST_UID, GID: $HOST_GID"
    
    # Create user with host UID/GID if it doesn't exist
    if ! id -u gigaevouser >/dev/null 2>&1; then
        # Check if user with this UID already exists
        if getent passwd "$HOST_UID" >/dev/null 2>&1; then
            EXISTING_USER=$(getent passwd "$HOST_UID" | cut -d: -f1)
            echo "User with UID $HOST_UID already exists: $EXISTING_USER"
            # Use existing user
            USER_TO_USE="$EXISTING_USER"
        else
            # Create new user with host UID/GID
            groupadd -g "$HOST_GID" gigaevouser 2>/dev/null || true
            useradd -u "$HOST_UID" -g "$HOST_GID" -m gigaevouser 2>/dev/null || true
            USER_TO_USE="gigaevouser"
        fi
    else
        USER_TO_USE="gigaevouser"
        # Update UID/GID if needed
        usermod -u "$HOST_UID" gigaevouser 2>/dev/null || true
        groupmod -g "$HOST_GID" gigaevouser 2>/dev/null || true
    fi
    
    chown -R "$USER_TO_USE:$USER_TO_USE" /app/repos
    chown -R "$USER_TO_USE:$USER_TO_USE" /app

    # Switch to the user for the main application
    echo "Switching to $USER_TO_USE..."
    exec gosu "$USER_TO_USE" "$@"
else
    echo "Running as non-root user $(id -un)"
    exec "$@"
fi