FROM alpine AS builder

ARG SING_BOX_VERSION=1.14.1

RUN wget https://github.com/SagerNet/sing-box/releases/download/v${SING_BOX_VERSION}/sing-box-${SING_BOX_VERSION}-linux-amd64.tar.gz && \
    tar -xf sing-box-${SING_BOX_VERSION}-linux-amd64.tar.gz && \
    mv sing-box-${SING_BOX_VERSION}-linux-amd64/sing-box /app

############################################################

FROM debian:trixie-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends nginx ca-certificates && \
    rm -rf /var/lib/apt/lists/* && \
    mkdir -p /tmp/nginx/body /tmp/nginx/proxy /tmp/nginx/fastcgi /tmp/nginx/uwsgi /tmp/nginx/scgi

COPY templates/config.json /config.json
COPY templates/nginx.conf /etc/nginx/nginx.conf
COPY templates/decoy /var/www/decoy
COPY --from=builder /app /app

EXPOSE 8080 8081 8090

# 运行时不 inflate /etc/hosts 会令 cloudflared 解析不到 localhost -> argo 502
CMD ["/bin/sh","-c","echo '127.0.0.1 localhost' >> /etc/hosts 2>/dev/null; echo '::1 localhost' >> /etc/hosts 2>/dev/null; nginx -g 'daemon off;' & sleep 1; exec /app run -c /config.json"]