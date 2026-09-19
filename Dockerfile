FROM alpine AS builder

ARG SING_BOX_VERSION=1.14.1

RUN wget https://github.com/SagerNet/sing-box/releases/download/v${SING_BOX_VERSION}/sing-box-${SING_BOX_VERSION}-linux-amd64.tar.gz && \
    tar -xf sing-box-${SING_BOX_VERSION}-linux-amd64.tar.gz && \
    mv sing-box-${SING_BOX_VERSION}-linux-amd64/sing-box /app

############################################################

FROM debian:trixie-slim

# unikernel 里没有 /etc/hosts 和 /etc/resolv.conf（Docker 是运行时注入的，不打包进镜像）
# 不补上，cloudflared 解析不了 localhost，隧道回 502
RUN printf '127.0.0.1\tlocalhost\n::1\tlocalhost\n' > /etc/hosts && \
    printf 'nameserver 1.1.1.1\nnameserver 8.8.8.8\n' > /etc/resolv.conf

COPY templates/config.json /config.json
COPY --from=builder /app /app

EXPOSE 8080 8081 8090

CMD ["/app", "run", "-c", "/config.json"]