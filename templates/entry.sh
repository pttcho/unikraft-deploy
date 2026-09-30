#!/bin/sh
# Supervisor: the node must never be left with a dead nginx or a dead tunnel
# client. Both children are polled and restarted if they exit. This script must
# never exit on its own, otherwise the instance stops.
#
# Previous behaviour was `nginx & ; exec sing-box`, so a crashed sing-box took
# the whole instance down (restart_policy is "never" on every node) and nobody
# watched nginx at all.

# unikernel 无 /etc/hosts，cloudflared 解析不到 localhost 会令 Argo 回 502
echo '127.0.0.1 localhost' >> /etc/hosts 2>/dev/null
echo '::1 localhost' >> /etc/hosts 2>/dev/null

CHECK_INTERVAL=10

# An exited child stays visible to `kill -0` while it is an unreaped zombie, so
# detect exit by the empty /proc cmdline. Without /proc this falls back to
# plain existence, which is weaker but still catches a cleanly reaped process.
alive() {
    _pid="$1"
    [ -n "$_pid" ] || return 1
    kill -0 "$_pid" 2>/dev/null || return 1
    [ -r "/proc/$_pid/cmdline" ] || return 0
    [ -s "/proc/$_pid/cmdline" ]
}

start_nginx() {
    nginx -g 'daemon off;' &
    NGINX_PID=$!
    sleep 1
}

start_app() {
    /app run -c /config.json &
    APP_PID=$!
}

stop_all() {
    echo "supervisor: shutting down" >&2
    kill "$NGINX_PID" "$APP_PID" 2>/dev/null
    exit 0
}

trap stop_all TERM INT

start_nginx
start_app
echo "supervisor: nginx pid $NGINX_PID, sing-box pid $APP_PID" >&2

while true; do
    sleep "$CHECK_INTERVAL"

    if ! alive "$NGINX_PID"; then
        echo "supervisor: nginx exited, restarting" >&2
        wait "$NGINX_PID" 2>/dev/null
        start_nginx
    fi

    if ! alive "$APP_PID"; then
        echo "supervisor: sing-box exited, restarting" >&2
        wait "$APP_PID" 2>/dev/null
        start_app
        sleep 2
    fi
done
