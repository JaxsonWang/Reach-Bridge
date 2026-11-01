# Reach-Bridge

将 Agent-Reach 的只读平台能力提供为受认证的 HTTP API，便于受限终端环境通过网络调用。内置 WebUI，用来开放渠道、配置 Cookie / API 凭据、完成网页登录和验证实际动作。

## Docker 部署

下载 [Release 发布包](https://github.com/JaxsonWang/Reach-Bridge/releases/latest)中的源码 ZIP 并解压。镜像支持 `linux/amd64`、`linux/arm64`，宿主只需 Docker Compose。首次部署：

```sh
cp .env.example .env
# 填写查询令牌，以及 data/home 所有者对应的 PUID / PGID。
mkdir -p data/home
docker compose --env-file .env --env-file upstream.env pull
docker compose --env-file .env --env-file upstream.env up -d --no-build --wait
```

令牌至少 32 个不含空白的 ASCII 字符；在本机用 `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'` 生成，不提交 Git。容器默认以 `1000:1000` 运行；修改 `.env` 的 `PUID` / `PGID` 与宿主目录所有者一致，保证 `data/home` 可读写。

默认仅绑定 `127.0.0.1:8080`，适合反向代理提供 HTTPS。需要私网直连时修改 `REACH_BRIDGE_BIND` 和 `REACH_BRIDGE_PORT`，并限制宿主入站访问。不要把明文 HTTP 暴露到公网。

固定镜像：`ghcr.io/jaxsonwang/reach-bridge:0.2.0-agent-reach-1.6.1`。发布包同时提供分架构 `.tar.gz` 离线镜像，可用 `docker load -i <镜像文件>` 导入后跳过 `pull`。

### 启用 WebUI 管理

访问服务根地址即可打开控制台。管理功能默认关闭，查询接口仍按原有方式工作。在 `.env` 增加：

```dotenv
REACH_BRIDGE_ADMIN_TOKEN=在本机生成与查询令牌不同的管理员令牌
REACH_BRIDGE_ADMIN_ORIGIN=https://reach.example.com
```

`ADMIN_ORIGIN` 必须与浏览器实际访问的协议、域名、端口完全一致，不带路径。修改后重新执行上面的 `up -d --no-build --wait`，在页面中输入管理员令牌。查询令牌不能登录管理界面；管理会话使用 HttpOnly / SameSite Cookie，写操作另有 Origin 和 CSRF 校验。反向代理须保留浏览器的 Origin，不需要对外暴露浏览器调试端口。

仅在可信私网确需 HTTP 时，显式配置以下两项；此连接会明文传输管理员令牌、平台 Cookie 和登录输入：

```dotenv
REACH_BRIDGE_ADMIN_ORIGIN=http://192.168.50.2:8804
REACH_BRIDGE_ALLOW_INSECURE_HTTP=true
```

### 从控制台连接平台

1. **渠道管理**：按需开放平台，调整只读动作及精确 HTTPS 域名。开放接口不代表平台依赖已准备好；点击“准备依赖”会显式安装该渠道所需工具。
2. **凭据与登录**：保存 GitHub Token、Groq / OpenAI Key，或选择平台导入 Cookie。支持 Cookie 请求头和常见浏览器扩展导出的 JSON 数组；只接受本站 Cookie，不回显已保存值。
3. **准备并登录**：启动容器内的专用浏览器，在页面画面里由你本人扫码或输入，完成后点击“保存会话并结束”。浏览器操作仅限所选平台的登录域，不提供任意 URL、JavaScript 或原始 CDP。首次准备会下载 OpenCLI 与扩展，可能耗时几分钟。
4. **操作台**：选择真实动作并填写参数，验证访问结果。转录会使用你配置的外部语音服务及其额度，只有明确执行时才调用。

管理任务覆盖依赖安装、凭据修改和整个人工登录过程，期间暂停新查询，等待既有查询完成后再写入。默认总时限 900 秒，可在 `.env` 调整为 30～1800 秒。退出管理会取消本人正在执行的任务；取消登录不会撤回网站侧已经发生的登录操作。

平台的认证方式由上游决定：GitHub 使用 Token；哔哩哔哩 Cookie 验证后写入原生 bili-cli 凭据；YouTube Cookie 写入 yt-dlp 的 Netscape 文件及配置；其他受支持网站复用上游专用浏览器会话。不会导出凭据，也不会挂载你的个人浏览器资料。YouTube 已有自定义 yt-dlp 配置冲突时返回 `CONFIG_CONFLICT`，需要管理员处理，不覆盖已有配置。

“已保存”不等于登录验证成功或拥有平台全部能力。Cookie 可能过期，平台也可能限流、要求验证或限制内容；通过实际动作验收。上游实验性渠道仍保留实验性限制。

### 从 0.1 升级

保留 `.env`、`data/`、`config/`、`backups/` 和 `.upstream-state/`，使用新版镜像与 Compose 配置。旧 `/v1/search`、`/v1/read`、查询令牌和只读配置挂载继续可用，不要求启用 WebUI。

**第一次启用管理时**，服务将 `config/channels.json` 复制为 `$HOME/.reach-bridge/channels.json`；之后以此可写文件为唯一渠道策略来源，WebUI 保存立即生效，原只读配置仅作为首次种子。停用管理会恢复读取原配置。请在备份中保留整个 `data/home`。

## 从终端调用

令牌通过调用方环境变量传入，不写入脚本：

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

# 使用渠道声明的其他只读动作。
curl --fail-with-body http://127.0.0.1:8080/v1/actions/bilibili/hot \
  -H "Authorization: Bearer $REACH_BRIDGE_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"params":{"limit":5}}'
```

`GET /v1/channels` 返回已开放平台、动作名和 `action_schemas` 参数声明；本版可管理上游 14 个渠道，覆盖搜索、读取、字幕、转录、评论读取、热门、资料及行情等只读动作。新增上游动作不会自动获得执行权限。普通读取仅接受配置的 HTTPS 域名；动态动作还支持相应平台的内容 ID。`web` 渠道仍受精确域名白名单约束。

- `GET /openapi.json`：受查询令牌保护的接口定义。
- `GET /healthz`：进程存活；`GET /readyz`：CLI 协议与配置已载入，不探测远端平台。
- 响应保留上游 `ok`、渠道、实际后端、条目、元信息与错误码，删除条目的平台原始 `raw`。上游动作失败返回 HTTP 424；需读取 JSON 错误，不把它当空结果成功。
- 查询 API 不安装依赖、不修改凭据、不登录、不升级、不自动重试。管理接口不能用查询令牌调用，也不开放任意 Shell、发帖或评论写入。
- 查询默认执行 60 秒、排队 2 秒、并发 2 个，总输出 2 MiB；普通请求体 16 KiB，Cookie 导入 128 KiB。参数见 `.env.example`。

不启用 WebUI 时，也可由管理员在容器中显式运行 `agent-reach setup` 或 `agent-reach add <平台>`，随后检查结果。不要与服务端管理任务同时修改同一个 HOME。

## 更新到上游 release

`upstream.env` 固定 Agent-Reach 核心版本。维护入口为 `python3 scripts/upstream.py check / update / rollback`，需要宿主 Python 3.10 及以上。它使用正式 release 构建新镜像，不拉取浮动主分支。备份、回滚与失败处理见 [更新说明](docs/UPDATES.md)。

只读动作按上游声明发现、再由桥接授权。凭据和浏览器管理使用上游内部接口，**当前管理适配器仅支持 Agent-Reach 1.6.1**；升级其他上游版本前须使用已适配的桥接版本。未适配时管理返回 `UNSUPPORTED_UPSTREAM`，不影响兼容的只读查询。

## 部署边界

- 网关限制输入域名，不代替宿主出站防火墙：上游会重定向、访问外部服务及加载浏览器子资源。普通 Docker bridge 不隔离敏感内网。
- 镜像内置 Chromium、Node、Xvfb、ffmpeg 和中文字体。浏览器以非 root 身份运行，保留 sandbox；主机内核及容器策略须支持 Chromium sandbox。未准备好时明确失败，不自动使用 `--no-sandbox` 或特权容器。
- 不公开 CDP / VNC 端口。登录画面与输入仅经管理会话转发；可见画面仍含账号内容，请保护管理访问入口。
- `data/home` 与更新快照包含 Cookie、凭据和工具状态。限制访问，不共享、不提交 Git。仅镜像切回不能撤销数据变化，完整回滚须恢复对应 HOME 快照。
- 请求超时、断开和服务关闭会取消对应 CLI 进程组；上游 detached 共享浏览器另有生命周期，停止整个容器负责最终回收。
- 服务保持单个 Uvicorn worker。会话与任务记录在内存中，服务重启后需重新登录管理，持久化凭据不受影响。

## 本地开发

```sh
uv sync --python 3.12
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run python scripts/upstream.py check
```

本地启动需自行安装固定 release 的 `agent-reach`，设置 `REACH_BRIDGE_TOKEN`，再执行 `uv run reach-bridge`。测试使用隔离的 CLI / 上游模块夹具，不读取个人账号。WebUI 为随包分发的静态资源，无需前端构建或第三方 CDN。

从源码构建镜像时显式增加构建配置：

```sh
docker compose --env-file .env --env-file upstream.env -f compose.yaml -f compose.build.yaml build
```

[Agent-Reach 上游](https://github.com/Panniantong/Agent-Reach) · [官方安装流程](https://github.com/Panniantong/Agent-Reach/blob/main/docs/install.md)
