#!/usr/bin/env bash
# Initialize WARP inside the container at startup.
# This runs before the bot starts, ensuring warp-cli is ready when needed.

set -e

echo "=== WARP initialization ==="

# Start the WARP daemon in the background
warp-svc &
WARP_PID=$!
echo "warp-svc started (PID $WARP_PID)"

# Wait for the daemon to be ready
for i in {1..10}; do
    if warp-cli --accept-tos status >/dev/null 2>&1; then
        echo "warp-cli ready"
        break
    fi
    echo "Waiting for warp-cli... ($i/10)"
    sleep 2
done

# Check if already registered
if warp-cli --accept-tos status 2>&1 | grep -qi "registration missing"; then
    echo "Registering WARP..."
    warp-cli --accept-tos register
else
    echo "Already registered"
fi

# Configure proxy mode and port
echo "Setting proxy mode..."
warp-cli --accept-tos mode proxy
warp-cli --accept-tos proxy port "${WARP_PROXY_PORT:-40000}"

# Connect
echo "Connecting..."
warp-cli --accept-tos connect

# Wait for connection
for i in {1..15}; do
    if warp-cli --accept-tos status 2>&1 | grep -qi "connected"; then
        echo "✓ WARP connected"
        warp-cli status
        break
    fi
    echo "Waiting for connection... ($i/15)"
    sleep 2
done

echo "=== WARP initialization complete ==="
