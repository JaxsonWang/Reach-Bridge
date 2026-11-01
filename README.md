# Reach-Bridge

将 Agent-Reach 的只读平台搜索与内容读取能力提供为受认证的 HTTP API，便于受限终端环境通过网络调用。服务负责参数、权限与任务生命周期，上游 CLI 负责平台访问。

## Docker 部署

下载 [Release 发布包](https://github.com/JaxsonWang/Reach-Bridge/releases/latest)中的源码 ZIP 并解压。镜像支持 `linux/amd64`、`linux/arm64`，使用 Docker Compose 拉取运行，无需在宿主安装服务依赖。版本维护脚本另需宿主 Python 3.10 及以上。

```sh
cp .env.example .env
# 在 .env 中填写自行生成的访问令牌，以及运行账户的 PUID / PGID。
mkdir -p data/home
docker compose --env-file .env --env-file upstream.env pull
docker compose --env-file .env --env-file upstream.env run --rm reach-bridge agent-reach setup
docker compose --env-file .env --env-file upstream.env up -d --no-build --wait
```

令牌至少 32 个不含空白的 ASCII 字符。可使用 `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'` 在本机生成，不提交 Git。`data/home` 必须由容器运行 UID/GID 读写；设置 `PUID=$(id -u)`、`PGID=$(id -g)` 对应的值后再启动。

默认仅绑定宿主的 `127.0.0.1:8080`，适合由反向代理提供 HTTPS。需要私网直连时修改 `.env` 中的 `REACH_BRIDGE_BIND`，并在宿主配置访问控制。不要直接暴露明文 HTTP 到公网。

上游 `setup` 会安装默认渠道依赖，可能下载较大的工具；首次配置与升级结果必须检查。`GET /v1/channels` 仅表示桥接开放的接口，不代表每个平台已通过实际访问测试。

本版固定镜像为 `ghcr.io/jaxsonwang/reach-bridge:0.1.0-agent-reach-1.6.1`。发布包同时提供分架构的 `.tar.gz` 离线镜像，可用 `docker load -i <对应架构的镜像文件>` 导入，然后跳过 `pull` 步骤。升级桥接版本时，使用新发布包中的 Compose 配置，保留 `.env`、`data/`、`backups/` 和 `.upstream-state/`；上游版本变化按[更新说明](docs/UPDATES.md)处理。

## 从终端调用

以下令牌由调用方通过环境变量提供，不把真实值写入脚本：

```sh
curl --fail-with-body http://127.0.0.1:8080/v1/channels \
  -H "Authorization: Bearer $REACH_BRIDGE_TOKEN"

curl --fail-with-body http://127.0.0.1:8080/v1/search \
  -H "Authorization: Bearer $REACH_BRIDGE_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"platform":"bilibili","query":"Python 入门","limit":5}'

curl --fail-with-body http://127.0.0.1:8080/v1/read \
  -H "Authorization: Bearer $REACH_BRIDGE_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"platform":"github","url":"https://github.com/Panniantong/Agent-Reach"}'
```

可将这两个接口注册为模型工具。响应保留上游的 `ok`、渠道、实际后端、条目、元信息和错误码，不返回条目的平台原始 `raw`。上游查询失败返回 HTTP 424，仍需读取 JSON 中的错误；HTTP 查询不会安装依赖、登录或升级，也不会由桥接层自动重试。

- `GET /openapi.json`：受相同令牌保护的接口定义。
- `GET /healthz`：服务存活；`GET /readyz`：CLI 协议与配置渠道已载入，不探测远端平台。
- 管理员在 `config/channels.json` 启用平台和精确域名，重启生效。首版只开放 `search` / `read`，不开放任意命令、评论、发帖、安装、登录或升级接口。
- 每个请求执行默认最多 60 秒，排队最多 2 秒，并发 2 个；总输出最多 2 MiB，请求体最多 16 KiB。参数见 `.env.example`。

## 更新到上游 release

`upstream.env` 固定 Agent-Reach 版本。维护入口为 `python3 scripts/upstream.py check / update / rollback`，更新使用正式 release 构建新镜像，不改桥接代码、不拉取浮动主分支。备份、回滚与失败处理见 [更新说明](docs/UPDATES.md)。

## 部署边界

- 这是受认证的指定平台网关，不是任意 URL 抓取代理。读取只接受所选平台配置的 HTTPS 域名，不支持短链接、IP、用户信息段和非标准端口。
- 输入域名限制不等于完整 SSRF 隔离：上游会进行重定向、访问其他服务或加载浏览器子资源。敏感内网访问必须由宿主出站防火墙／网络策略隔离，普通 Docker bridge 不提供这种保证。
- 不需要浏览器的渠道适合无桌面容器。需要浏览器的平台还依赖 CPU 架构、系统库和图形登录环境；API 不提供远程桌面、浏览器调试端口或代登录。未准备好时如实返回 `NEED_SETUP` / `NEED_LOGIN`。
- 请求超时、断开和服务关闭会取消对应 CLI 进程组。上游 detached 的共享浏览器有独立生命周期，不为取消一个请求而关闭所有人的浏览器；停止整个容器负责最终回收。
- `data/home` 和更新快照可能包含凭据，不共享、不提交 Git，也不挂载个人浏览器资料目录。
- 容器保持单个 Uvicorn worker，避免并发上限被多个进程分别放大。上游可能自行选择其声明的后端，响应的 `backend` 和访问来源是判断依据。

## 本地开发

```sh
uv sync --python 3.12
uv run pytest
uv run ruff check .
uv run python scripts/upstream.py check
```

本地启动还需自行安装固定 release 的 `agent-reach`，设置 `REACH_BRIDGE_TOKEN`，然后执行 `uv run reach-bridge`。测试使用本机 CLI 夹具，不访问平台、部署主机或真实登录数据。

从源码构建镜像时显式增加构建配置：

```sh
docker compose --env-file .env --env-file upstream.env -f compose.yaml -f compose.build.yaml build
```

[Agent-Reach 上游](https://github.com/Panniantong/Agent-Reach) · [官方安装流程](https://github.com/Panniantong/Agent-Reach/blob/main/docs/install.md)
