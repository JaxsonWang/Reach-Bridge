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

固定镜像：`ghcr.io/jaxsonwang/reach-bridge:0.3.1-agent-reach-1.6.1`。发布包同时提供分架构 `.tar.gz` 离线镜像，可用 `docker load -i <镜像文件>` 导入后跳过 `pull`。

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

### NAS 浏览器兼容模式

需要在受限内核上使用 X / Twitter、小红书等站内工具时，可显式关闭 Chromium 内部 sandbox，无需安装虚拟机。在 `.env` 设置：

```dotenv
REACH_BRIDGE_BROWSER_ENABLED=true
REACH_BRIDGE_BROWSER_SANDBOX=false
REACH_BRIDGE_SECURITY_OPT=no-new-privileges=true
```

保留非 root 的 `PUID` / `PGID`。Compose 的安全选项改用 `no-new-privileges=true`，不引用宿主不支持的自定义 seccomp 文件；不添加 `SYS_ADMIN`、`privileged`、`seccomp=unconfined` 或 Docker socket。已有自定义 Compose 必须将 `REACH_BRIDGE_BROWSER_SANDBOX` 传入容器，并将原 `security_opt` 中的 Chromium seccomp 引用替换为 `no-new-privileges=true`；只修改 `.env` 不会改变写死的 Compose 配置。

拉取新镜像并重新创建服务后，在 WebUI 对所需平台点击“准备依赖”，再“准备并登录”。准备操作会将之前选定的系统 Chromium 切换为镜像内统一启动入口；保留专用资料目录、已有会话和渠道策略。新入口仅在显式 `false` 时添加 `--no-sandbox`，保留 CDP pipe、信号与原有浏览器生命周期。查询接口不代替管理员执行首次准备。小红书 / X 仍通过 OpenCLI 执行站内动作，不改成公开网页搜索。

WebUI 显示“NAS 兼容模式：Chromium sandbox 已关闭”；这表示运行方式，不表示平台已登录或搜索已验证。关闭内部 sandbox 后，浏览器漏洞可能影响其能访问的会话及容器内数据；Docker 隔离不能替代浏览器 sandbox。仅在接受此取舍的专用环境启用，不挂载个人浏览器或额外敏感目录。该模式不关闭 HTTPS 证书校验或网页同源安全，不公开浏览器调试端口，也不会在标准模式启动失败时自动启用。

恢复标准模式时，将 `REACH_BRIDGE_BROWSER_SANDBOX` 改回 `true`、`REACH_BRIDGE_SECURITY_OPT` 改回 `seccomp=./docker/chromium-seccomp.json` 并重新创建服务，确保旧浏览器进程已经结束。只需要原生工具时使用下面的无浏览器模式。

### 无浏览器模式

只需要原生工具，或不采用上述兼容模式时，在 `.env` 设置：

```dotenv
REACH_BRIDGE_BROWSER_ENABLED=false
```

此模式保留 WebUI、API 凭据和哔哩哔哩 / YouTube 的原生 Cookie 导入，允许 GitHub、哔哩哔哩、YouTube、V2EX、Web、小宇宙的原生动作，以及雪球 `quote` / `search`。转录仍需对应 Key 和服务额度。小红书、X / Twitter 等浏览器动作返回 `BROWSER_DISABLED`，不会因为存在 Cookie 就尝试启动浏览器，也不会偷偷改走公开网页后端；通用公开搜索可继续使用既有 SearXNG 或 Web 渠道。

渠道策略和已有凭据会保留，控制台只列出当前模式可执行的动作。网页登录及浏览器 Cookie 导入关闭；“准备依赖”仅允许原生渠道，雪球查询无需先执行会准备浏览器的 `add xueqiu`。原生模式的雪球验证仅运行 `check xueqiu --search`，不触发社区浏览器路径。

该开关约束桥接服务的调用路径；管理员在容器终端手动执行的上游命令不受它控制。启用浏览器的默认模式保持不变，在支持的主机上设回 `true` 并使用所需 seccomp 配置即可。

### 空闲内存与按需显示

0.3.1 起，官方 Chromium 在空闲服务与无头查询时不启动 Xvfb；网页登录需要可见画面时才创建 1280×800 显示服务，随该浏览器退出释放。默认服务也不再保留 Python 入口监督进程。上游浏览器的 600 秒空闲回收、管理登录时限与会话保存方式不变；关闭 WebUI 弹窗不等于立即关闭共享浏览器。

已有部署选中的官方 `/usr/bin/chromium` 会在容器启动时切换到内置统一入口，不清除专用 profile 或凭据，手动 CLI 和 WebUI 无需先重新准备。标准模式下已有自定义浏览器仍保留原可执行文件选择及共享显示环境，不强制迁移；这类部署仍可能有常驻 Xvfb。

在 NAS 上检查实际进程，而不只看容器总内存：

```sh
docker stats --no-stream <容器名>
docker top <容器名> -eo pid,ppid,comm,rss,pcpu
```

`docker top` 的 RSS 单位为 KiB，包含共享映射，不能简单相加作为容器总量。Linux 下 Docker CLI 会扣除部分非活跃文件缓存，但活跃文件缓存仍可能计入显示值；高读数不一定是 Python 匿名内存增长。`shm_size: 512m` 是容量上限，不是预先占用。默认部署更新后可检查空闲时不再存在 Xvfb；实际内存降幅取决于进程和文件缓存，不能用旧 Xvfb 的 RSS 直接推算。不清理宿主缓存，不自动添加内存硬限制。

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
- 桥接查询接口不主动调用安装、凭据修改、登录或升级管理命令，也不自动重试。上游浏览器渠道可能自动准备／重开专用浏览器，并下载／刷新扩展；未选定浏览器时还可能下载其默认浏览器。建议先在管理页完成“准备依赖”，再调用浏览器渠道。管理接口不能用查询令牌调用，也不开放任意 Shell、发帖或评论写入。
- 查询默认执行 60 秒、排队 2 秒、并发 2 个，总输出 2 MiB；普通请求体 16 KiB，Cookie 导入 128 KiB。参数见 `.env.example`。

不启用 WebUI 时，也可由管理员在容器中显式运行 `agent-reach setup` 或 `agent-reach add <平台>`，随后检查结果。不要与服务端管理任务同时修改同一个 HOME。

## 更新到上游 release

`upstream.env` 固定 Agent-Reach 核心版本。维护入口为 `python3 scripts/upstream.py check / update / rollback`，需要宿主 Python 3.10 及以上。它使用正式 release 构建新镜像，不拉取浮动主分支。备份、回滚与失败处理见 [更新说明](docs/UPDATES.md)。

只读动作按上游声明发现、再由桥接授权。凭据和浏览器管理使用上游内部接口，**当前管理适配器仅支持 Agent-Reach 1.6.1**；升级其他上游版本前须使用已适配的桥接版本。未适配时管理返回 `UNSUPPORTED_UPSTREAM`，不影响兼容的只读查询。

## 部署边界

- 网关限制输入域名，不代替宿主出站防火墙：上游会重定向、访问外部服务及加载浏览器子资源。普通 Docker bridge 不隔离敏感内网。
- 镜像内置 Chromium、Node、Xvfb、ffmpeg 和中文字体。浏览器以非 root 身份运行，默认保留 sandbox；标准 Compose 安全选项使用随发布包提供的 `docker/chromium-seccomp.json`，仅在 Docker 默认策略基础上放行所需 namespace 系统调用，部署时须保留该文件。宿主仍须具备用户命名空间能力，策略文件不能补齐缺失的内核功能。详见 [浏览器隔离配置](docker/chromium-seccomp.README.md)。未准备好时明确失败；只有显式设置上述 NAS 兼容模式才使用 `--no-sandbox`，不使用特权容器。
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
