"""上游声明结构与桥接授权分开：发现新动作不会自动赋予执行权限。"""

import json
import os
import re
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

from .config import ChannelPolicy, Settings, validate_url
from .runner import BridgeError

READ_ACTIONS = frozenset(
    {
        "search",
        "comments",
        "read",
        "hot",
        "subtitle",
        "transcribe",
        "profile",
        "tweets",
        "thread",
        "subreddit",
        "posts",
        "jobs",
        "node",
        "user",
        "quote",
        "hot_posts",
        "hot_stocks",
    }
)
# 固定上游 1.6.1 的原生路径；不从 Cookie 是否存在推断浏览器依赖。
NATIVE_ACTIONS = {
    "github": {"search", "read"},
    "youtube": {"read", "search", "subtitle", "transcribe"},
    "bilibili": {"search", "read", "hot", "subtitle"},
    "v2ex": {"hot", "node", "read", "user"},
    "web": {"search", "read"},
    "xiaoyuzhou": {"transcribe"},
    "xueqiu": {"quote", "search"},
}
NATIVE_INSTALL = {"github", "youtube", "bilibili", "v2ex", "web", "xiaoyuzhou"}


DEFAULT_HOSTS = {
    "github": ["github.com"],
    "bilibili": ["www.bilibili.com", "bilibili.com"],
    "youtube": ["www.youtube.com", "youtube.com", "youtu.be"],
    "twitter": ["x.com", "www.x.com", "twitter.com", "www.twitter.com"],
    "reddit": ["www.reddit.com", "reddit.com", "old.reddit.com"],
    "facebook": ["www.facebook.com", "facebook.com"],
    "instagram": ["www.instagram.com", "instagram.com"],
    "xiaohongshu": ["www.xiaohongshu.com", "xiaohongshu.com"],
    "linkedin": ["www.linkedin.com", "linkedin.com"],
    "boss": ["www.zhipin.com", "zhipin.com"],
    "v2ex": ["www.v2ex.com", "v2ex.com"],
    "xueqiu": ["xueqiu.com", "www.xueqiu.com"],
    "xiaoyuzhou": ["www.xiaoyuzhoufm.com", "xiaoyuzhoufm.com"],
    "web": ["github.com", "www.bilibili.com", "www.youtube.com"],
}


def atomic_write(path: Path, data: dict) -> None:
    if path.parent.is_symlink() or path.is_symlink():
        raise BridgeError("UNSAFE_PATH", "管理状态目录不能使用符号链接", 500)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".policy-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class Catalog:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.policy_file = (
            ((settings.state_dir or Path.home() / ".reach-bridge") / "channels.json")
            if settings.admin_token
            else None
        )
        if self.policy_file and self.policy_file.exists():
            if self.policy_file.is_symlink():
                raise ValueError("管理策略文件不能是符号链接")
            self.policies = {
                name: ChannelPolicy.model_validate(row)
                for name, row in json.loads(self.policy_file.read_text()).items()
            }
        else:
            self.policies = settings.policies()
            if self.policy_file:
                self.save()
        self.channels: dict[str, dict] = {}
        self.version = ""

    def save(self):
        if self.policy_file:
            atomic_write(
                self.policy_file, {name: row.model_dump() for name, row in self.policies.items()}
            )

    def load(self, version: str, rows: list[dict]):
        self.version = version
        self.channels = {row["name"]: row for row in rows}
        missing = self.policies.keys() - self.channels.keys()
        if missing:
            raise RuntimeError("配置的渠道不在当前 Agent-Reach 中：" + ", ".join(sorted(missing)))

    def actions(self, name: str, *, enabled_only: bool = True) -> list[dict]:
        row = self.policies.get(name)
        if enabled_only and (row is None or not row.enabled):
            return []
        allowed = (
            READ_ACTIONS if row is None or row.actions is None else READ_ACTIONS & set(row.actions)
        )
        if not self.settings.browser_enabled:
            allowed = allowed & NATIVE_ACTIONS.get(name, set())
        return [a for a in self.channels[name]["actions"] if a["name"] in allowed]

    def describe(self, *, admin: bool = False) -> dict:
        rows = []
        for name, channel in self.channels.items():
            policy = self.policies.get(name)
            if not admin and (policy is None or not policy.enabled):
                continue
            actions = self.actions(name, enabled_only=not admin)
            rows.append(
                {
                    "name": name,
                    "description": channel["description"],
                    "enabled": policy is not None and policy.enabled,
                    "can_install": self.settings.browser_enabled or name in NATIVE_INSTALL,
                    "actions": [a["name"] for a in actions],
                    "action_schemas": [
                        {
                            **action,
                            "browser_backend": bool(
                                {"opencli", "boss-agent-cli"} & set(action.get("backends", []))
                            )
                            or (name == "xueqiu" and action["name"] in {"hot_posts", "hot_stocks"}),
                            "public_web_available": bool(action.get("public_web")),
                        }
                        for action in actions
                    ],
                    "available_actions": [
                        a["name"] for a in channel["actions"] if a["name"] in READ_ACTIONS
                    ],
                    "hosts": policy.hosts if policy else DEFAULT_HOSTS.get(name, []),
                    "selected_actions": policy.actions if policy else None,
                    "login": channel.get("login"),
                }
            )
        return {
            "agent_reach_version": self.version,
            "browser_enabled": self.settings.browser_enabled,
            "channels": rows,
        }

    def set_policy(self, name: str, policy: ChannelPolicy):
        if name not in self.channels:
            raise BridgeError("BAD_INPUT", "未知渠道", 422)
        available = {a["name"] for a in self.channels[name]["actions"]} & READ_ACTIONS
        if policy.actions is not None and not set(policy.actions) <= available:
            raise BridgeError("BAD_INPUT", "策略包含未获授权的动作", 422)
        previous = self.policies.get(name)
        self.policies[name] = policy
        try:
            self.save()
        except BaseException:
            if previous is None:
                del self.policies[name]
            else:
                self.policies[name] = previous
            raise

    def action(self, platform: str, action: str) -> dict:
        if (
            platform not in self.channels
            or platform not in self.policies
            or not self.policies[platform].enabled
        ):
            raise BridgeError("FORBIDDEN", "未启用该平台", 403)
        if not self.settings.browser_enabled and action not in NATIVE_ACTIONS.get(platform, set()):
            raise BridgeError(
                "BROWSER_DISABLED", "当前部署已禁用浏览器，请在支持浏览器的执行端调用该动作", 503
            )
        for row in self.actions(platform):
            if row["name"] == action:
                return row
        raise BridgeError("FORBIDDEN", "未启用或不支持该只读动作", 403)

    def build_args(self, platform: str, action: str, params: dict) -> list[str]:
        definition = self.action(platform, action)
        known = {p["name"]: p for p in definition["params"]}
        if set(params) - known.keys():
            raise BridgeError("BAD_INPUT", "含有动作未声明的参数", 422)
        positional, options = [], []
        for name, schema in known.items():
            if name not in params:
                if schema["required"]:
                    raise BridgeError("BAD_INPUT", "缺少必填参数：" + name, 422)
                continue
            value = params[name]
            kind = schema.get("type", "str")
            if kind == "int":
                if type(value) is not int or not 1 <= value <= 50:
                    raise BridgeError("BAD_INPUT", "数值参数须为 1～50 的整数", 422)
                value = str(value)
            elif kind == "str":
                if (
                    not isinstance(value, str)
                    or not value.strip()
                    or len(value) > (2048 if name == "target" else 300)
                    or any(ord(c) < 32 for c in value)
                ):
                    raise BridgeError("BAD_INPUT", "字符串参数为空、过长或含控制字符", 422)
                value = value.strip()
                if name in ("target", "url"):
                    if "://" in value:
                        try:
                            value = validate_url(value, self.policies[platform])
                        except ValueError as exc:
                            raise BridgeError(
                                "BAD_INPUT", "目标 URL 不在平台允许范围内", 422
                            ) from exc
                    elif platform == "web" or not re.fullmatch(
                        r"[A-Za-z0-9_@/-]{1,200}"
                        if platform != "github"
                        else r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:/(?:issues|pull)/[0-9]+)?",
                        value,
                    ):
                        raise BridgeError("BAD_INPUT", "请提供明确的 HTTPS URL 或平台内容 ID", 422)
            else:
                raise BridgeError("UNSUPPORTED_ACTION", "当前桥接未支持该参数类型")
            if schema["required"]:
                positional.append(value)
            else:
                options.append("--" + name + "=" + value)
        return [
            platform,
            action,
            "--timeout",
            str(self.settings.timeout),
            *options,
            *(["--", *positional] if positional else []),
        ]


class AccessGate:
    """查询可并发；管理写入和人工登录覆盖完整生命周期，避免混用账号。"""

    def __init__(self):
        import asyncio

        self.condition = asyncio.Condition()
        self.readers = 0
        self.writer = False

    @asynccontextmanager
    async def query(self):
        async with self.condition:
            if self.writer:
                raise BridgeError("BUSY", "管理员正在配置或登录，请稍后查询", 503)
            self.readers += 1
        try:
            yield
        finally:
            async with self.condition:
                self.readers -= 1
                self.condition.notify_all()

    @asynccontextmanager
    async def manage(self):
        async with self.condition:
            if self.writer:
                raise BridgeError("BUSY", "已有管理操作正在执行", 409)
            self.writer = True
            try:
                await self.condition.wait_for(lambda: self.readers == 0)
            except BaseException:
                self.writer = False
                self.condition.notify_all()
                raise
        try:
            yield
        finally:
            async with self.condition:
                self.writer = False
                self.condition.notify_all()
