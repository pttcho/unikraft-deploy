#!/bin/sh
# unikernel 无 /etc/hosts，cloudflared 解析不到 localhost 会令 Argo 回 502
echo '127.0.0.1 localhost' >> /etc/hosts 2>/dev/null
echo '::1 localhost' >> /etc/hosts 2>/dev/null

# nginx 前置：8080/8081 平台入口，127.0.0.1:8090 Argo 入口
nginx -g 'daemon off;' &
sleep 1
exec /app run -c /config.json