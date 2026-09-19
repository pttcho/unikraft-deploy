FROM alpine AS builder

ARG SING_BOX_VERSION=1.14.1

RUN wget https://github.com/SagerNet/sing-box/releases/download/v${SING_BOX_VERSION}/sing-box-${SING_BOX_VERSION}-linux-amd64.tar.gz && \
    tar -xf sing-box-${SING_BOX_VERSION}-linux-amd64.tar.gz && \
    mv sing-box-${SING_BOX_VERSION}-linux-amd64/sing-box /app

############################################################

FROM debian:trixie-slim

COPY templates/config.json /config.json
COPY --from=builder /app /app

EXPOSE 8080 8081 8090

# 构建时 /etc/hosts 是只读挂载写不进去，改到运行时补。
# unikernel 里没有 /etc/hosts，cloudflared 解析不了 localhost 就回 502。
CMD ["/bin/sh","-c","echo '127.0.0.1 localhost' >> /etc/hosts 2>/dev/null; echo '::1 localhost' >> /etc/hosts 2>/dev/null; exec /app run -c /config.json"]