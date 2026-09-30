# unikraft-deploy

在 [Unikraft Cloud](https://unikraft.cloud) 上部署 VLESS over WebSocket 代理服务。

## 准备工作

### 1. Unikraft 账号与 Token

1. 注册并登录 [Unikraft Cloud Console](https://console.unikraft.cloud/)

2. 获取 API Token（用于 CLI 登录）

   登录后点击 **左边菜单 Organization → API Keys**，可以查看当前用户的 API TOKEN。

### 2. 准备 UUID

生成一个标准 UUID（例如 `uuidgen` 或在线工具），请自行妥善保存，不要提交到仓库。

### 3.（可选）Cloudflare Tunnel

若希望节点额外多一个 Cloudflare 隧道的入口，准备：

| 参数 | 说明 |
|------|------|
| `ARGO_DOMAIN` | Tunnel 对外域名，如 `example.com` |
| `ARGO_TOKEN` | Cloudflare Tunnel Token |

两者需**同时填写**或**同时留空**，Argo 服务的内部访问地址是 `http://127.0.0.1:8080` (必须一致)

### 4. Fork 本仓库

将本仓库 Fork 到你的 GitHub 账号下，也可以顺手点个赞 ⭐ 支持一下。

## GitHub 配置

在仓库 **Settings → Secrets and variables → Actions** 中添加：

| Secret | 必填 | 说明 |
|--------|------|------|
| `UNIKRAFT_TOKEN` | 是 | Unikraft API Token |
| `UUID` | 是 | VLESS UUID，兼作解密密钥 |
| `ARGO_DOMAIN` | 否 | Cloudflare Tunnel 域名 |
| `ARGO_TOKEN` | 否 | Cloudflare Tunnel Token（新建实例时用） |
| `ARGO_TOKEN_SFO` `ARGO_TOKEN_SIN` `ARGO_TOKEN_DAL` `ARGO_TOKEN_WAS` | 否 | 各节点自己的 Tunnel Token；**Node Image Upgrade** 按节点从这里取（轮换 token 时必填） |

> 隧道 token 是构建时打进镜像的，所以**轮换 token 必须逐节点重建镜像**（Actions → Node Image Upgrade → `phase=apply`）。
> 不要再把 token 做成 `workflow_dispatch` 输入，也不要从运行日志里恢复——两者都会把 token 明文暴露在公开仓库里。

## 部署步骤

1. 打开仓库 **Actions → Create Unikraft Instance**
2. 点击 **Run workflow**
3. 选择部署地区（metro）：

| 选项 | 代码 | 说明 |
|------|------|------|
| `dal - 达拉斯 (Dallas, US)` | `dal` | 美国达拉斯 |
| `fra - 法兰克福 (Frankfurt, DE)` | `fra` | 德国法兰克福 |
| `sfo - 旧金山 (San Francisco, US)` | `sfo` | 美国旧金山 |
| `sin - 新加坡 (Singapore)` | `sin` | 新加坡（默认） |
| `was - 华盛顿 (Washington DC, US)` | `was` | 美国华盛顿 |

4. 运行完成后，在 **Job Summary** 或构建日志中可以查看加密的订阅信息。

## 解密订阅

1. Actions 构建完成后，打开 **Summary** 页面，查看加密的订阅信息，格式如下：

   `https://vevc.github.io/unikraft-deploy/?payload=...`

2. 直接点击链接打开，页面会自动填入加密 Payload
3. 在「解密密钥」输入框填入你配置的 `UUID`
4. 点击 **解密订阅**，得到明文订阅信息

全程在浏览器本地完成（Web Crypto），不会上传 UUID 和订阅信息，安全可控。

## 注意事项

- `UUID`、`UNIKRAFT_TOKEN`、`ARGO_TOKEN` 属于敏感信息，只放在 GitHub Secrets，不要写入代码提交
- 不要为 `ARGO_TOKEN` 增加 `workflow_dispatch` 输入：GitHub 会打印每步的 `env:` 块，secret 派生的值会自动掩码为 `***`，而 dispatch 输入不会，会把隧道 token 明文留在运行日志里（公开仓库可被任何人读取）。
- 每次部署会构建镜像 `<org>/unikraft:latest` 并启动新实例
- 如需清理旧实例，可以使用 **Actions → Delete Unikraft Instance** 删除

## 资源参数与性能升配（2026-09-30）

`Create Unikraft Instance` 现在支持两个显式输入：

| 输入 | 可选值 | 默认值 |
|---|---|---|
| `vcpus` | `1`、`2`、`4`、`8`、`16` | `1` |
| `memory` | `512M`、`1024M`、`2048M`、`4096M` | `4096M` |

- 默认规格现为 1 vCPU / 4096 MiB，按 2026-09-30 四个正式节点统一内存的要求更新。创建新节点仍须核实账号、单实例及地区剩余额度；选项不代表平台一定允许所有规格。
- CPU 不再依赖平台默认值；Nginx 使用 `worker_processes auto`，按客体系统可见的 CPU 数运行。现有协议、端口、路径及隧道设置不变。
- **这是创建新实例的流程，不会把已有节点原地升配。不要为了升级直接重复运行它。** 新旧实例同时运行会消耗额外 CPU/内存/实例名额，同一隧道的双连接器也可能造成故障。
- 控制台 Metros 的 `vCPUs 1/16` 表示该地区已用/总配额，不是单实例已拥有 16 核。单实例上限与镜像多核能力须另行验证；`Storage` 也不是 RAM。
- 已有节点优先检查平台的实例资源更新 API：`PATCH /v1/instances/{uuid}` 可以表达 `vcpus` / `memory_mb` 更新。能否在运行中更新须核实；若需停机，先确认窗口，再逐节点处理，并保留原规格用于回滚。不要先删除旧节点或批量删除地区实例。
- 增加 CPU/内存不等于带宽提高。高峰期应同时比较 CPU 使用率、实际代理吞吐、丢包/重连及 Argo/直连路径。
- 测本机到各入口的真实线路时，TUN 仍会拦截 `curl --noproxy "*"`；不能把此时的结果当成直连优选结论。切换 TUN 前须确认并准备恢复步骤。

参数化本身不会改变已有节点的线上规格；是否已升配，以对应工作流结果及平台 API 读回为准。


## 2026-09-30 部署变更

正式四节点为 SFO / SIN / DAL / WAS，均为 1 vCPU / 4096 MiB。原 FRA 已按明确授权精确删除。WAS 保留 Argo 域名 `zheshi111.mkvskg.dpdns.org`，直连为 `crimson-breeze-k85idd62.was.unikraft.app`，已验证代理出口 `209.50.250.193`。

WAS 采用独立镜像标签 `qilonglin/unikraft:was-migration-20260930`，不要再把四个地区含不同隧道配置的镜像都覆盖到同一个 `latest` 标签后依赖旧摘要跨区拉取。迁移和历史修复入口属于一次性运维；当前 `node-maintenance.yml` 的 `inspect` 可只读核实正式四节点。

实际云端变更记录以 GitHub Actions 和平台 API 读回为准；配置文件默认值本身不代表既有节点已经升配。
