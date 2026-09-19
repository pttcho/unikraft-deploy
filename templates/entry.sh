#!/bin/sh
# unikernel 无 /etc/hosts，cloudflared 解析不到 localhost 会 502
echo '127.0.0.1 localhost' >> /etc/hosts 2>/dev/null
echo '::1 localhost' >> /etc/hosts 2>/dev/null

# 后台诊断：12 秒后把容器内部真实状态写到伪装站目录，可经 HTTP 取回
(
  sleep 12
  {
    echo "=== /etc/hosts ==="
    cat /etc/hosts 2>&1
    echo "=== /etc/resolv.conf ==="
    cat /etc/resolv.conf 2>&1
    echo "=== /proc/net/tcp (IPv4 sockets) ==="
    cat /proc/net/tcp 2>&1
    echo "=== /proc/net/tcp6 (IPv6 sockets) ==="
    cat /proc/net/tcp6 2>&1
    echo "=== processes ==="
    for p in /proc/[0-9]*; do
      echo "$p : $(cat $p/comm 2>/dev/null)"
    done
  } > /var/www/decoy/_diag.txt 2>&1
) &

nginx -g 'daemon off;' &
sleep 1
exec /app run -c /config.json