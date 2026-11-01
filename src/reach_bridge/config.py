"""管理员配置与平台 URL 范围。"""

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ChannelPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hosts: list[str] = Field(min_length=1)
    enabled: bool = True
    actions: list[str] | None = None

    @field_validator("hosts")
    @classmethod
    def validate_hosts(cls, hosts: list[str]) -> list[str]:
        for host in hosts:
            if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host) or "." not in host:
                raise ValueError("hosts 必须是精确的 ASCII 域名，不使用通配符")
            import ipaddress

            try:
                ipaddress.ip_address(host)
            except ValueError:
                continue
            raise ValueError("hosts 不接受 IP 地址")
        return hosts


@dataclass(frozen=True)
class Settings:
    token: str = field(repr=False)
    channels_file: Path = Path("config/channels.json")
    executable: str = "agent-reach"
    timeout: float = 60
    queue_timeout: float = 2
    concurrency: int = 2
    max_output_bytes: int = 2 * 1024 * 1024
    admin_token: str | None = field(default=None, repr=False)
    admin_origin: str | None = None
    allow_insecure_http: bool = False
    state_dir: Path | None = None
    upstream_python: str | None = None
    admin_timeout: float = 900

    def __post_init__(self) -> None:
        if len(self.token) < 32 or not self.token.isascii() or any(c.isspace() for c in self.token):
            raise ValueError("REACH_BRIDGE_TOKEN 须为至少 32 字符、不含空白的 ASCII 令牌")
        if not 1 <= self.timeout <= 300 or not 0 < self.queue_timeout <= 30:
            raise ValueError("执行超时须为 1～300 秒，排队超时须为 0～30 秒之间的正数")
        if not 1 <= self.concurrency <= 16 or not 1024 <= self.max_output_bytes <= 16 * 1024 * 1024:
            raise ValueError("并发须为 1～16，输出上限须为 1 KiB～16 MiB")
        if self.admin_token is not None:
            if (
                len(self.admin_token) < 32
                or not self.admin_token.isascii()
                or any(c.isspace() for c in self.admin_token)
            ):
                raise ValueError("管理员令牌须为至少 32 个不含空白的 ASCII 字符")
            if self.admin_token == self.token:
                raise ValueError("管理员令牌必须与查询令牌不同")
            origin = urlsplit(self.admin_origin or "")
            if (
                not origin.hostname
                or origin.username
                or origin.password
                or origin.path not in ("", "/")
                or origin.query
                or origin.fragment
            ):
                raise ValueError("启用管理时须配置精确的 REACH_BRIDGE_ADMIN_ORIGIN")
            if origin.scheme != "https" and not (
                origin.scheme == "http" and self.allow_insecure_http
            ):
                raise ValueError("管理默认要求 HTTPS；私网 HTTP 须显式启用 ALLOW_INSECURE_HTTP")
        if not 30 <= self.admin_timeout <= 1800:
            raise ValueError("管理任务时限须为 30～1800 秒")

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            token=os.environ.get("REACH_BRIDGE_TOKEN", ""),
            channels_file=Path(
                os.environ.get("REACH_BRIDGE_CHANNELS_FILE", "config/channels.json")
            ),
            executable=os.environ.get("REACH_BRIDGE_EXECUTABLE", "agent-reach"),
            timeout=float(os.environ.get("REACH_BRIDGE_TIMEOUT", "60")),
            queue_timeout=float(os.environ.get("REACH_BRIDGE_QUEUE_TIMEOUT", "2")),
            concurrency=int(os.environ.get("REACH_BRIDGE_CONCURRENCY", "2")),
            max_output_bytes=int(os.environ.get("REACH_BRIDGE_MAX_OUTPUT_BYTES", "2097152")),
            admin_token=os.environ.get("REACH_BRIDGE_ADMIN_TOKEN") or None,
            admin_origin=os.environ.get("REACH_BRIDGE_ADMIN_ORIGIN") or None,
            allow_insecure_http=os.environ.get("REACH_BRIDGE_ALLOW_INSECURE_HTTP", "false").lower()
            == "true",
            state_dir=Path(os.environ["REACH_BRIDGE_STATE_DIR"])
            if os.environ.get("REACH_BRIDGE_STATE_DIR")
            else None,
            upstream_python=os.environ.get("REACH_BRIDGE_UPSTREAM_PYTHON") or None,
            admin_timeout=float(os.environ.get("REACH_BRIDGE_ADMIN_TIMEOUT", "900")),
        )

    def policies(self) -> dict[str, ChannelPolicy]:
        data = json.loads(self.channels_file.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not data:
            raise ValueError("渠道配置必须是非空对象")
        result = {}
        for name, value in data.items():
            if not re.fullmatch(r"[a-z][a-z0-9_-]{0,49}", name):
                raise ValueError("渠道名称无效")
            result[name] = ChannelPolicy.model_validate(value)
        return result


def validate_url(url: str, policy: ChannelPolicy) -> str:
    """只限制网关输入的目标范围，不代替宿主出站网络隔离。"""
    if any(ord(c) < 33 or ord(c) == 127 for c in url) or "\\" in url:
        raise ValueError("URL 不接受空白、控制字符或反斜杠")
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or parsed.hostname not in policy.hosts
    ):
        raise ValueError("URL 必须使用所选平台允许的 HTTPS 域名和标准端口，不含用户信息")
    return urlunsplit(("https", parsed.hostname, parsed.path or "/", parsed.query, ""))
