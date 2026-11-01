# 更新 Agent-Reach，不修改桥接代码

Reach-Bridge 将 Agent-Reach 的正式 release 安装进镜像。`upstream.env` 记录具体版本，运行时不会拉取 Git 主分支，也不会自动升级。软链接只负责指向文件，不能代替版本下载、依赖同步和服务切换；上游还会拒绝私有状态目录中的软链接，因此持久化目录直接挂载到 HOME。

## 版本与数据分开管理

- Agent-Reach 核心程序及其 uv tool 环境随镜像发布。
- `data/home/` 持久化上游配置、渠道工具、登录会话和缓存。它含有凭据，应限制访问，不提交 Git。
- 更新先构建候选镜像，再停服务备份 HOME，随后使用候选版本同步渠道状态并启动服务。
- 回滚必须同时恢复旧镜像和对应的 HOME 快照。只换回旧程序不能撤销渠道工具及数据的变化。

`latest` 只在维护时解析为 GitHub 最新正式 release。实际构建始终使用具体版本地址，不使用浮动的下载结果作为版本记录。

## 维护命令

在项目根目录执行，需要 Python 3.10 及以上、Docker Compose V2，以及能读取 `data/home/` 的本机账户。维护脚本只用 Python 标准库，不需要在部署主机安装桥接服务的 Python 依赖。主流程直接拉取发布镜像；上游维护脚本则显式使用 `compose.build.yaml` 构建所选版本，不会把新版本假定为已经发布的镜像。

```sh
# 只查询 GitHub 最新正式版本，不修改部署。
python3 scripts/upstream.py check

# 切换到最新正式版，或指定已有正式 release。
python3 scripts/upstream.py update
python3 scripts/upstream.py update 1.6.1

# 恢复最近一次切换前的镜像与 HOME 快照。
python3 scripts/upstream.py rollback
```

先运行 `check` 阅读版本变化，再执行 `update`。首次运行先按 README 完成 `setup` 与部署。对相同版本执行 `update` 不重建、不改数据；若只是要重建桥接代码，使用 README 中显式增加 `compose.build.yaml` 的构建命令与启动命令。

维护会使用 `.env` 和 `upstream.env`，不向终端打印令牌。更新前必须保留当前镜像，不能清理未使用镜像后再期待回滚；脚本会核对恢复点对应的镜像身份，不用同名新镜像替代。

维护期间不要并行启动其他挂载同一 HOME 的管理或登录容器。宿主的维护 Python 需要正常的 CA 信任配置；证书错误应修复证书安装，不关闭验证。Mac 本地开发可用 `uv run python scripts/upstream.py check`，使用项目 Python 的系统信任配置。

快照在 `backups/<时间>/home`，同步结果在同目录 `sync.json`；两者按凭据保护。切换失败时保留 `.upstream-state/pending.json`，阻止下一次升级覆盖恢复点，管理员检查错误后显式执行 `rollback`。构建失败不会停止现有服务。备份阶段失败尚未修改 HOME，`rollback` 在这一阶段只恢复原服务，不使用半份快照或上一次升级的记录。

回滚将当前配置、渠道工具、登录状态恢复到升级前，因此回滚点之后的修改不再生效；原当前 HOME 保留在 `data/home.displaced-<恢复点时间>`，不删除。完整回滚前应告知服务使用者。先在同一文件系统的 `data/home.restore-<恢复点时间>` 完整准备恢复副本，再切换目录；复制或切换中断后可再次执行 `rollback`。恢复后的启动检查失败也可重试，不重新覆盖已恢复的 HOME。

快照和恢复副本保留每个目录、文件及符号链接的 UID/GID，不跟随链接复制卷外数据。维护账户须有读取源数据、保留这些属主并写入目标目录的权限；可以由原数据所有者执行，或由管理员以 root 执行。若保留属主失败，复制阶段会明确失败，不把不可读的 HOME 切入服务。

快照与旧镜像不会自动清理，也没有后台自动升级。确认新版本满足需要、不再需要恢复后，由管理员自行管理存储。需要浏览器登录或受平台限流影响的渠道，仍需在目标主机做实际查询验收；容器就绪只验证 CLI 与接口契约。

## 上游命令的边界

首次配置使用 `agent-reach setup`。升级时使用候选镜像中的 `agent-reach update --sync-only`，同步已经启用的渠道工具和 Skill；不调用会自行替换核心程序的完整 `agent-reach update`。

同步可能更新渠道工具、清理上游已移除的渠道及不再使用的工具。需要停服备份，并检查输出中的 `problems` 和 `user_actions`。安装、登录、升级均由管理员执行，不提供给查询 API。

## 资料依据

- [官方安装流程](https://github.com/Panniantong/Agent-Reach/blob/main/docs/install.md)
- [正式 releases](https://github.com/Panniantong/Agent-Reach/releases)
- [上游同步实现](https://github.com/Panniantong/Agent-Reach/blob/main/agent_reach/runtime/manage.py)
- [上游私有路径规则](https://github.com/Panniantong/Agent-Reach/blob/main/agent_reach/utils/paths.py)
