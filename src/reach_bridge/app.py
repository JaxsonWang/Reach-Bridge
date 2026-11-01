"""供受限终端环境调用的只读 HTTP API。"""

import asyncio
import contextlib
import hmac
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.exceptions import HTTPException as StarletteHTTPException

from .admin import mount_admin
from .catalog import AccessGate, Catalog
from .config import Settings, validate_url
from .runner import BridgeError, Runner


class SearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    platform: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,49}$")
    query: str = Field(min_length=1, max_length=300)
    limit: int = Field(default=5, ge=1, le=20)

    @field_validator("query")
    @classmethod
    def trim_query(cls, value: str) -> str:
        if not value.strip() or any(ord(c) < 32 for c in value):
            raise ValueError("搜索词不能为空或包含控制字符")
        return value.strip()


class ReadInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    platform: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,49}$")
    url: str = Field(min_length=1, max_length=2048)


class ActionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    params: dict[str, Any] = Field(default_factory=dict, max_length=32)


class ErrorResponse(BaseModel):
    error: dict[str, str]


class BodyLimit:
    """在 JSON 解析前限制请求体，兼容无 Content-Length 的分块请求。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in ("POST", "PUT", "PATCH"):
            await self.app(scope, receive, send)
            return
        body = bytearray()
        while True:
            event = await receive()
            if event["type"] == "http.disconnect":
                return
            body.extend(event.get("body", b""))
            if len(body) > (131072 if scope["path"] == "/admin/api/cookies" else 16384):
                await JSONResponse({"error": {"code": "BODY_TOO_LARGE"}}, 413)(scope, receive, send)
                return
            if not event.get("more_body", False):
                break
        delivered = False

        async def replay():
            nonlocal delivered
            if delivered:
                return await receive()
            delivered = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self.app(scope, replay, send)


class SecurityHeaders:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        async def secured(event):
            if event["type"] == "http.response.start":
                event["headers"] = [
                    *event.get("headers", []),
                    (b"cache-control", b"no-store"),
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"cross-origin-resource-policy", b"same-origin"),
                    (
                        b"content-security-policy",
                        b"default-src 'self'; script-src 'self'; style-src 'self'; "
                        b"img-src 'self' data: blob:; object-src 'none'; base-uri 'none'; "
                        b"frame-ancestors 'none'; form-action 'self'",
                    ),
                ]
            await send(event)

        await self.app(scope, receive, secured)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    catalog = Catalog(settings)
    policies = catalog.policies
    runner = Runner(settings)
    gate = AccessGate()
    version = ""
    ready = False

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal ready, version
        try:
            info = await runner.run(["version"])
            channels = await runner.run(["channels"])
            if not info["ok"] or not channels["ok"]:
                raise RuntimeError("Agent-Reach 启动检查失败")
            version = info["result"]["version"]
            catalog.load(version, channels["channels"])
            ready = True
            yield
        finally:
            ready = False
            await manager.close()
            await runner.close()

    app = FastAPI(
        title="Reach-Bridge",
        version="0.2.1",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        responses={422: {"model": ErrorResponse}},
    )
    app.add_middleware(BodyLimit)
    app.add_middleware(SecurityHeaders)
    app.state.runner = runner
    manager, admin_auth = mount_admin(app, settings, runner, catalog, gate)
    web = Path(__file__).with_name("web")
    app.mount("/assets", StaticFiles(directory=web), name="assets")

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(web / "index.html")

    bearer = HTTPBearer(auto_error=False)

    def authenticate(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> None:
        if credentials is None or not hmac.compare_digest(
            credentials.credentials.encode(), settings.token.encode()
        ):
            raise HTTPException(401, "访问令牌无效", headers={"WWW-Authenticate": "Bearer"})

    auth = [Depends(authenticate)]

    @app.exception_handler(BridgeError)
    async def bridge_error(request: Request, exc: BridgeError):
        return JSONResponse({"error": {"code": exc.code, "message": exc.message}}, exc.status)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        code = {401: "UNAUTHORIZED", 403: "FORBIDDEN", 404: "NOT_FOUND", 422: "BAD_INPUT"}.get(
            exc.status_code, "HTTP_ERROR"
        )
        return JSONResponse(
            {"error": {"code": code, "message": str(exc.detail)}},
            exc.status_code,
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        # 不在错误中回显查询、URL 或客户端提交的其他字段。
        return JSONResponse(
            {"error": {"code": "BAD_INPUT", "message": "请求参数不符合接口定义"}}, 422
        )

    @app.get("/healthz", include_in_schema=False)
    async def health():
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readiness():
        return JSONResponse({"status": "ready" if ready else "starting"}, 200 if ready else 503)

    @app.get("/openapi.json", dependencies=auth, include_in_schema=False)
    async def openapi():
        return app.openapi()

    @app.get("/v1/channels", dependencies=auth)
    async def channels():
        return catalog.describe()

    def action_for(platform: str, action: str) -> dict:
        return catalog.action(platform, action)

    async def execute(request: Request, args: list[str]) -> Any:
        async def query():
            async with gate.query():
                return await runner.run(args)

        task = asyncio.create_task(query())

        async def disconnected():
            while True:
                if (await request.receive())["type"] == "http.disconnect":
                    return

        watcher = asyncio.create_task(disconnected())
        try:
            done, _ = await asyncio.wait((task, watcher), return_when=asyncio.FIRST_COMPLETED)
            if task not in done:
                raise BridgeError("CANCELLED", "客户端已断开，请求已取消", 499)
            result = task.result()
            if not isinstance(result.get("items"), list) or (
                not result["ok"] and not isinstance(result.get("error"), dict)
            ):
                raise BridgeError("INVALID_UPSTREAM", "上游动作结果缺少 items 或 error")
            # raw 是平台原始结构，非通用接口契约；不向模型暴露它。
            for item in result["items"]:
                if not isinstance(item, dict):
                    raise BridgeError("INVALID_UPSTREAM", "上游条目格式无效")
                item.pop("raw", None)
            return JSONResponse(result, 200 if result["ok"] else 424)
        finally:
            for pending in (watcher, task):
                if not pending.done() and not pending.cancelling():
                    pending.cancel()
            for pending in (watcher, task):
                with contextlib.suppress(asyncio.CancelledError):
                    await pending

    @app.post("/v1/search", dependencies=auth)
    async def search(body: SearchInput, request: Request):
        action = action_for(body.platform, "search")
        params = {p["name"] for p in action["params"]}
        if not {"query", "limit"} <= params:
            raise BridgeError("UNSUPPORTED_ACTION", "该渠道搜索参数与桥接接口不匹配")
        return await execute(
            request,
            [
                body.platform,
                "search",
                "--timeout",
                str(settings.timeout),
                "--limit",
                str(body.limit),
                "--",
                body.query,
            ],
        )

    @app.post("/v1/read", dependencies=auth)
    async def read(body: ReadInput, request: Request):
        action = action_for(body.platform, "read")
        if [p["name"] for p in action["params"] if p["required"]] != ["target"]:
            raise BridgeError("UNSUPPORTED_ACTION", "该渠道读取参数与桥接接口不匹配")
        try:
            url = validate_url(body.url, policies[body.platform])
        except ValueError as exc:
            raise HTTPException(422, "URL 不在所选平台允许的范围内") from exc
        return await execute(
            request, [body.platform, "read", "--timeout", str(settings.timeout), "--", url]
        )

    @app.post("/v1/actions/{platform}/{action}", dependencies=auth)
    async def action(platform: str, action: str, body: ActionInput, request: Request):
        return await execute(request, catalog.build_args(platform, action, body.params))

    @app.post("/admin/api/actions/{platform}/{action}")
    async def admin_action(platform: str, action: str, body: ActionInput, request: Request):
        admin_auth.require(request)
        return await execute(request, catalog.build_args(platform, action, body.params))

    return app


def main() -> None:
    import uvicorn

    uvicorn.run(
        "reach_bridge.app:create_app",
        factory=True,
        host="0.0.0.0",
        port=8080,
        workers=1,
        access_log=False,
    )
