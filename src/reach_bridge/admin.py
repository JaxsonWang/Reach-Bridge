"""独立管理认证、短期会话与串行管理任务。"""

import asyncio
import contextlib
import hmac
import json
import secrets
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from .catalog import NATIVE_ACTIONS, NATIVE_INSTALL, AccessGate, Catalog
from .config import ChannelPolicy, Settings
from .runner import BridgeError, Runner

CONFIG_KEYS = {"github-token", "groq-key", "openai-key"}


@dataclass
class Session:
    csrf: str
    expires: float


class AdminAuth:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.sessions: dict[str, Session] = {}
        self.attempts: dict[str, list[float]] = {}
        self.origin = (settings.admin_origin or "").rstrip("/")

    def origin_check(self, request: Request):
        if not self.settings.admin_token:
            raise BridgeError("ADMIN_DISABLED", "尚未配置独立管理员凭据", 503)
        if request.headers.get("origin") != self.origin:
            raise BridgeError("ORIGIN_REJECTED", "管理来源与配置的 Origin 不一致", 403)

    def require(self, request: Request) -> str:
        key = request.cookies.get("reach_admin", "")
        session = self.sessions.get(key)
        if not self.settings.admin_token or session is None or session.expires <= time.monotonic():
            self.sessions.pop(key, None)
            raise BridgeError("UNAUTHORIZED", "请先登录管理界面", 401)
        if request.method not in ("GET", "HEAD"):
            self.origin_check(request)
            if not hmac.compare_digest(request.headers.get("x-csrf-token", ""), session.csrf):
                raise BridgeError("CSRF_REJECTED", "管理请求校验失败，请刷新页面", 403)
        return key

    def login(self, request: Request, value: str) -> tuple[str, Session]:
        self.origin_check(request)
        now = time.monotonic()
        ip = request.client.host if request.client else "unknown"
        recent = [stamp for stamp in self.attempts.get(ip, []) if now - stamp < 60]
        if len(recent) >= 5:
            raise BridgeError("RATE_LIMITED", "尝试过于频繁，请一分钟后重试", 429)
        if not hmac.compare_digest(value.encode(), self.settings.admin_token.encode()):
            self.attempts[ip] = recent + [now]
            raise BridgeError("UNAUTHORIZED", "管理员凭据不正确", 401)
        self.attempts.pop(ip, None)
        self.sessions = {k: v for k, v in self.sessions.items() if v.expires > now}
        if len(self.sessions) >= 32:
            raise BridgeError("BUSY", "管理会话已满，请先退出其他会话", 503)
        key = secrets.token_urlsafe(32)
        session = Session(secrets.token_urlsafe(24), now + 8 * 3600)
        self.sessions[key] = session
        return key, session


class LoginInput(BaseModel):
    token: SecretStr


class SecretInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str
    value: SecretStr | None = None


class CookieInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    platform: str
    cookies: SecretStr


class ChannelInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    platform: str


class BrowserInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str
    x: int | None = Field(default=None, ge=0, le=1920)
    y: int | None = Field(default=None, ge=0, le=1080)
    text: SecretStr | None = Field(default=None)
    key: str | None = None
    delta: int | None = Field(default=None, ge=-2000, le=2000)


class Manager:
    def __init__(self, settings: Settings, runner: Runner, catalog: Catalog, gate: AccessGate):
        self.settings, self.runner, self.catalog, self.gate = settings, runner, catalog, gate
        self.jobs: dict[str, dict] = {}
        self.active: dict | None = None
        self.tasks: set[asyncio.Task] = set()
        self.browser_lock = asyncio.Lock()
        self.authentication: dict = {}

    def python(self) -> str:
        if self.settings.upstream_python:
            return self.settings.upstream_python
        binary = shutil.which(self.settings.executable)
        if binary:
            candidate = Path(binary).resolve().with_name("python")
            if candidate.is_file():
                return str(candidate)
        raise BridgeError(
            "NEED_SETUP", "找不到上游 Python，请配置 REACH_BRIDGE_UPSTREAM_PYTHON", 503
        )

    async def helper(self, operation: str, platform: str = "-", data: dict | None = None):
        result = await self.runner.run(
            [str(Path(__file__).with_name("upstream_admin.py")), operation, platform],
            executable=self.python(),
            input_data=json.dumps(data or {}).encode(),
            execution_timeout=30
            if operation in {"browser_frame", "browser_input"}
            else self.settings.admin_timeout,
        )
        if not result["ok"]:
            error = result.get("error") or {}
            # 适配器只返回固定的安全诊断，不返回 Cookie、Token 或库异常文本。
            raise BridgeError(
                error.get("code", "ADMIN_FAILED"), error.get("message", "管理操作失败"), 424
            )
        return result.get("result") or {}

    async def cli(self, args: list[str], *, secret: str | None = None):
        result = await self.runner.run(
            args,
            input_data=secret.encode() if secret is not None else None,
            execution_timeout=self.settings.admin_timeout,
        )
        if not result["ok"]:
            code = (result.get("error") or {}).get("code", "UPSTREAM_BROKEN")
            raise BridgeError(code, "上游管理操作未完成，请检查渠道依赖与出站网络", 424)
        return result.get("result") or result

    def require_browser(self):
        if not self.settings.browser_enabled:
            raise BridgeError(
                "BROWSER_DISABLED", "当前部署已禁用浏览器，请使用原生 Cookie 或其他执行端", 503
            )

    def authentication_capabilities(self, description: dict) -> dict:
        if not self.settings.browser_enabled:
            for name, capability in description.get("channels", {}).items():
                capability["login"] = False
                capability["cookie_import"] = name in {"bilibili", "youtube"}
        description["browser_enabled"] = self.settings.browser_enabled
        description["browser_sandbox"] = self.settings.browser_sandbox
        return description

    def check_channel(self, platform: str):
        if platform not in self.catalog.channels:
            raise BridgeError("BAD_INPUT", "未知渠道", 422)

    @staticmethod
    def public(job: dict) -> dict:
        return {
            key: value
            for key, value in job.items()
            if key not in ("owner", "event", "target_id", "task")
        }

    def start(self, owner: str, kind: str, platform: str, operation):
        if self.active is not None:
            raise BridgeError("BUSY", "已有管理任务，请等待完成或先结束登录", 409)
        if len(self.jobs) >= 30:
            self.jobs.pop(next(iter(self.jobs)))
        job = {
            "id": secrets.token_urlsafe(12),
            "owner": owner,
            "kind": kind,
            "platform": platform,
            "state": "queued",
            "message": "等待正在执行的查询结束",
        }
        self.jobs[job["id"]] = self.active = job

        async def run():
            try:
                async with asyncio.timeout(self.settings.admin_timeout), self.gate.manage():
                    job.update(state="running", message="正在执行")
                    job["result"] = await operation(job)
                    job.update(state="succeeded", message="操作已完成")
            except asyncio.CancelledError:
                job.update(state="cancelled", message="任务已取消，既有凭据未主动清除")
            except (BridgeError, TimeoutError) as error:
                job.update(
                    state="failed",
                    code=getattr(error, "code", "TIMEOUT"),
                    message=getattr(error, "message", "管理任务超过总时限"),
                )
            except Exception:
                # 失败可见，但不能把第三方异常中可能带出的凭据写入 UI 或任务历史。
                job.update(
                    state="failed", code="ADMIN_FAILED", message="管理任务异常结束，未确认成功"
                )
            finally:
                if self.active is job:
                    self.active = None

        task = asyncio.create_task(run())
        job["task"] = task
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return self.public({k: v for k, v in job.items() if k != "task"})

    async def close(self):
        for task in tuple(self.tasks):
            task.cancel()
        await asyncio.gather(*tuple(self.tasks), return_exceptions=True)

    async def cancel_owner(self, owner: str):
        if self.active and self.active["owner"] == owner:
            job = self.active
            job.update(state="running", message="正在结束任务")
            job["task"].cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await job["task"]
            # 刚排队便取消时，协程可能尚未进入自己的 finally。
            if self.active is job:
                job.update(state="cancelled", message="任务已取消")
                self.active = None

    async def login(self, job: dict):
        self.require_browser()
        try:
            async with self.browser_lock:
                job.update(message="正在准备浏览器、扩展与渠道依赖")
                await self.helper("browser_prepare", job["platform"])
                await self.cli(["add", job["platform"]])
                opened = await self.helper("browser_login", job["platform"])
            job["target_id"] = opened["target_id"]
            event = job["event"] = asyncio.Event()
            job.update(state="waiting_user", message="请在登录窗口完成操作，然后点击保存会话")
            await event.wait()
            job.update(state="running", message="正在保存并同步会话")
            async with self.browser_lock:
                return await self.helper(
                    "browser_finish", job["platform"], {"target_id": job["target_id"]}
                )
        finally:
            # 登录取消或超时后，先排空已经开始的输入，再释放管理独占。
            job.update(state="running", message="正在结束登录窗口")
            async with self.browser_lock:
                pass

    def login_job(self, owner: str) -> dict:
        self.require_browser()
        job = self.active
        if (
            not job
            or job["owner"] != owner
            or job["kind"] != "login"
            or job["state"] != "waiting_user"
        ):
            raise BridgeError("NO_LOGIN", "当前会话没有可操作的登录窗口", 409)
        return job


def mount_admin(app, settings: Settings, runner: Runner, catalog: Catalog, gate: AccessGate):
    router = APIRouter(prefix="/admin")
    auth = AdminAuth(settings)
    manager = Manager(settings, runner, catalog, gate)

    @router.get("/bootstrap")
    async def bootstrap():
        return {
            "enabled": bool(settings.admin_token),
            "browser_enabled": settings.browser_enabled,
            "browser_sandbox": settings.browser_sandbox,
            "origin": auth.origin,
            "insecure_http": settings.allow_insecure_http,
            "version": "0.3.0",
        }

    @router.post("/login")
    async def login(body: LoginInput, request: Request):
        key, session = auth.login(request, body.token.get_secret_value())
        response = JSONResponse(
            {
                "csrf": session.csrf,
                "origin": auth.origin,
                "insecure_http": settings.allow_insecure_http,
                "version": "0.3.0",
            }
        )
        response.set_cookie(
            "reach_admin",
            key,
            httponly=True,
            samesite="strict",
            path="/admin",
            secure=urlsplit(auth.origin).scheme == "https",
            max_age=8 * 3600,
        )
        return response

    @router.post("/logout")
    async def logout(request: Request):
        key = auth.require(request)
        await manager.cancel_owner(key)
        auth.sessions.pop(key, None)
        response = JSONResponse({"ok": True})
        response.delete_cookie("reach_admin", path="/admin")
        return response

    @router.get("/api/session")
    async def session(request: Request):
        key = auth.require(request)
        return {
            "csrf": auth.sessions[key].csrf,
            "origin": auth.origin,
            "insecure_http": settings.allow_insecure_http,
            "version": "0.3.0",
        }

    @router.get("/api/channels")
    async def channels(request: Request):
        auth.require(request)
        return catalog.describe(admin=True)

    @router.put("/api/channels/{platform}")
    async def policy(platform: str, body: ChannelPolicy, request: Request):
        auth.require(request)
        async with gate.manage():
            catalog.set_policy(platform, body)
        return {"ok": True}

    @router.get("/api/credentials")
    async def credentials(request: Request):
        auth.require(request)
        if manager.active:
            return {"capabilities": manager.authentication, "busy": True}
        async with gate.manage():
            description = manager.authentication_capabilities(await manager.helper("describe"))
            manager.authentication = description
            state = await manager.helper("credentials_status")
        return {"capabilities": description, "status": state, "busy": False}

    @router.get("/api/jobs")
    async def jobs(request: Request):
        key = auth.require(request)
        return {
            "jobs": [
                manager.public({k: v for k, v in job.items() if k != "task"})
                for job in manager.jobs.values()
                if job["owner"] == key
            ]
        }

    @router.post("/api/jobs/{identifier}/cancel")
    async def cancel(identifier: str, request: Request):
        key = auth.require(request)
        if manager.active and manager.active["id"] == identifier and manager.active["owner"] == key:
            await manager.cancel_owner(key)
        return {"ok": True}

    @router.post("/api/status")
    async def status(request: Request):
        key = auth.require(request)

        async def operation(job):
            return await manager.cli(["doctor"])

        return manager.start(key, "status", "-", operation)

    @router.post("/api/install")
    async def install(body: ChannelInput, request: Request):
        key = auth.require(request)
        manager.check_channel(body.platform)
        if not settings.browser_enabled and body.platform not in NATIVE_INSTALL:
            raise BridgeError(
                "BROWSER_DISABLED", "该渠道的依赖准备需要浏览器；原生报价和搜索可直接测试", 503
            )

        async def operation(job):
            capability = (await manager.helper("describe"))["channels"].get(body.platform, {})
            if settings.browser_enabled and capability.get("login"):
                await manager.helper("browser_prepare", body.platform)
            return await manager.cli(["add", body.platform])

        return manager.start(key, "install", body.platform, operation)

    @router.post("/api/verify")
    async def verify(body: ChannelInput, request: Request):
        key = auth.require(request)
        manager.check_channel(body.platform)
        actions = {row["name"] for row in catalog.channels[body.platform]["actions"]}
        if not settings.browser_enabled and not actions <= NATIVE_ACTIONS.get(body.platform, set()):
            if "search" not in NATIVE_ACTIONS.get(body.platform, set()):
                manager.require_browser()
            # check --search 只跑搜索；不能让原生模式的雪球验证触发社区浏览器路径。
            actions = {"search"}
        if actions == {"transcribe"}:
            raise BridgeError("MANUAL_TEST", "请在操作台提供链接测试转录，不自动上传样本", 422)

        async def operation(job):
            args = ["check", body.platform, "--timeout", str(settings.timeout)]
            if "search" in actions:
                args.append("--search")
            result = await manager.cli(args)
            if result.get("ok") is False:
                raise BridgeError("CHECK_FAILED", "渠道验证未通过，请检查依赖、登录与平台限制", 424)
            return result

        return manager.start(key, "verify", body.platform, operation)

    @router.post("/api/credentials")
    async def credential(body: SecretInput, request: Request):
        key = auth.require(request)
        if body.key not in CONFIG_KEYS:
            raise BridgeError("BAD_INPUT", "不支持该凭据类型", 422)
        value = body.value.get_secret_value().strip() if body.value else None
        if value is not None and (
            not value or len(value) > 8192 or any(ord(c) < 32 for c in value)
        ):
            raise BridgeError("BAD_INPUT", "凭据为空、过长或含控制字符", 422)

        async def operation(job):
            if value is None:
                return await manager.helper("credential_delete", data={"key": body.key})
            await manager.cli(["configure", body.key, "--stdin"], secret=value)
            return {"configured": body.key, "saved": True}

        return manager.start(key, "credential", body.key, operation)

    @router.post("/api/cookies")
    async def cookies(body: CookieInput, request: Request):
        key = auth.require(request)
        manager.check_channel(body.platform)
        if not settings.browser_enabled and body.platform not in {"bilibili", "youtube"}:
            manager.require_browser()
        raw = body.cookies.get_secret_value().strip()
        if not raw or len(raw) > 120000:
            raise BridgeError("BAD_INPUT", "Cookie 内容为空或过长", 422)
        try:
            value = json.loads(raw) if raw.startswith("[") else raw
        except ValueError as exc:
            raise BridgeError("BAD_INPUT", "Cookie JSON 格式不正确", 422) from exc

        async def operation(job):
            return await manager.helper("cookie_import", body.platform, {"cookies": value})

        return manager.start(key, "cookies", body.platform, operation)

    @router.post("/api/cookies/delete")
    async def cookie_delete(body: ChannelInput, request: Request):
        key = auth.require(request)
        manager.check_channel(body.platform)

        async def operation(job):
            return await manager.helper("cookie_delete", body.platform)

        return manager.start(key, "cookie_delete", body.platform, operation)

    @router.post("/api/login")
    async def browser_login(body: ChannelInput, request: Request):
        key = auth.require(request)
        manager.check_channel(body.platform)
        manager.require_browser()
        return manager.start(key, "login", body.platform, manager.login)

    @router.post("/api/login/finish")
    async def browser_finish(request: Request):
        job = manager.login_job(auth.require(request))
        job["event"].set()
        return {"ok": True}

    @router.get("/api/login/frame")
    async def frame(request: Request):
        job = manager.login_job(auth.require(request))
        async with manager.browser_lock:
            job = manager.login_job(auth.require(request))
            return await manager.helper(
                "browser_frame", job["platform"], {"target_id": job["target_id"]}
            )

    @router.post("/api/login/input")
    async def browser_input(body: BrowserInput, request: Request):
        job = manager.login_job(auth.require(request))
        data = body.model_dump(exclude_none=True, exclude={"text"})
        if body.text is not None:
            data["text"] = body.text.get_secret_value()
            if len(data["text"]) > 2000:
                raise BridgeError("BAD_INPUT", "输入内容过长", 422)
        data["target_id"] = job["target_id"]
        async with manager.browser_lock:
            job = manager.login_job(auth.require(request))
            return await manager.helper("browser_input", job["platform"], data)

    @router.post("/api/logout-platform")
    async def logout_platform(body: ChannelInput, request: Request):
        key = auth.require(request)
        manager.check_channel(body.platform)
        if not settings.browser_enabled and body.platform != "bilibili":
            manager.require_browser()

        async def operation(job):
            return await manager.cli(["logout", body.platform])

        return manager.start(key, "logout", body.platform, operation)

    @router.get("/api/openapi")
    async def openapi(request: Request):
        auth.require(request)
        return app.openapi()

    app.include_router(router)
    app.state.admin = manager
    app.state.admin_auth = auth
    return manager, auth
