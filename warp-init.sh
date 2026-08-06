#!/usr/bin/env bash
# Bring WARP up inside the container before the bot starts.
#
# Two things need arranging that a normal host gets from systemd: the D-Bus
# system bus (warp-svc watches it for suspend/resume events and otherwise
# retries the connection every 3s forever), and warp-svc itself, which has no
# service manager here to start it.

WARP_PORT="${WARP_PROXY_PORT:-40000}"
WARP_LOG=/var/log/warp-svc.log

echo "=== WARP initialization ==="

# The bus only needs to exist; nothing here talks to it directly.
if [ ! -S /run/dbus/system_bus_socket ]; then
    mkdir -p /run/dbus
    dbus-daemon --system --fork 2>/dev/null && echo "dbus-daemon started" \
        || echo "dbus-daemon unavailable, warp-svc will log power-notifier warnings"
fi

# warp-svc is noisy at DEBUG and would interleave with the bot's own output
# for the life of the container, so it goes to a file.
mkdir -p "$(dirname "$WARP_LOG")"
warp-svc >>"$WARP_LOG" 2>&1 &
echo "warp-svc started (PID $!), logging to $WARP_LOG"

warp() { warp-cli --accept-tos "$@"; }

for _ in $(seq 1 15); do
    warp status >/dev/null 2>&1 && break
    sleep 2
done

if ! warp status >/dev/null 2>&1; then
    echo "warp-cli never came up; see $WARP_LOG. Continuing without WARP -"
    echo "Mega downloads will use the container's own IP."
    exit 0
fi

# Registration lives in /var/lib/cloudflare-warp, so this only does work the
# first time unless that directory is a fresh layer.
if warp registration show >/dev/null 2>&1; then
    echo "already registered"
else
    echo "registering..."
    warp registration new || echo "registration failed, see above"
fi

warp mode proxy
warp proxy port "$WARP_PORT"
warp connect

for _ in $(seq 1 15); do
    if warp status 2>/dev/null | grep -qi "connected"; then
        echo "WARP connected, SOCKS5 on 127.0.0.1:$WARP_PORT"
        break
    fi
    sleep 2
done

warp status 2>/dev/null | head -3
echo "=== WARP initialization complete ==="
