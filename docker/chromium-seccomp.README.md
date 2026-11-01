# Chromium sandbox 的容器策略

`chromium-seccomp.json` 基于 Docker 官方引用的 [Moby 默认 seccomp 配置](https://github.com/moby/profiles/blob/4b8214fc4971e06172938a1f1554ccff6647ee80/seccomp/default.json)。只增加 `clone`、`setns`、`unshare` 三项允许规则，做法来自 [Playwright 官方 Docker 文档](https://playwright.dev/docs/docker#crawling-and-scraping)。其余默认拒绝、参数限制、能力条件和 `clone3` 行为保持不变，未开放 `io_uring_*`。

使用方式：

```sh
docker run --security-opt seccomp=./docker/chromium-seccomp.json ...
```

标准模式的浏览器仍以非 root 用户运行并启用 Chromium sandbox，不添加 `SYS_ADMIN`，不使用 `seccomp=unconfined` 或 `--no-sandbox`。此配置不关闭 AppArmor，也不修改宿主内核设置。

此策略只能放行内核已经具备的能力。宿主必须支持 Chromium 所需的用户命名空间；`CONFIG_USER_NS` 缺失或宿主 AppArmor／内核策略继续拒绝时，浏览器准备仍会失败，应检查宿主审计日志与内核配置。GitHub CI 的 amd64／arm64 通过不证明旧 NAS 内核兼容。

## 受限 NAS 的显式兼容模式

仅在接受关闭 Chromium 内部 sandbox 的取舍时，设置 `REACH_BRIDGE_BROWSER_SANDBOX=false` 并保持 `REACH_BRIDGE_BROWSER_ENABLED=true`。使用仓库 Compose 时另设 `REACH_BRIDGE_SECURITY_OPT=no-new-privileges=true`，替换本自定义 seccomp 引用；不需要 `SYS_ADMIN`、`privileged` 或 `seccomp=unconfined`。

镜像内统一 Chromium 启动入口只增加 `--no-sandbox`，不关闭 HTTPS 校验、网页同源安全，不改变原生模式和标准模式的默认值。兼容模式强制非 root；WebUI 和渠道接口报告已选择的 sandbox 设置。完整使用步骤见 [README 的 NAS 浏览器兼容模式](../README.md#nas-浏览器兼容模式)。

基线由 Moby 项目以 Apache-2.0 发布，原许可证保存在同目录 `LICENSE.moby-profiles`；Reach-Bridge 对基线的修改仅为上述命名空间规则。
