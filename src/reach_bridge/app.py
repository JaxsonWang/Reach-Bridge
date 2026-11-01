"""供受限终端环境调用的只读 HTTP API。"""

import asyncio
import contextlib
import hmac
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.exceptions import HTTPException as StarletteHTTPException

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
            if len(body) > 16384:
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


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    policies = settings.policies()
    runner = Runner(settings)
    catalog: dict[str, dict] = {}
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
            for channel in channels["channels"]:
                if channel["name"] in policies:
                    catalog[channel["name"]] = channel
            missing = policies.keys() - catalog.keys()
            if missing:
                raise RuntimeError(
                    "配置的渠道不在当前 Agent-Reach 中：" + ", ".join(sorted(missing))
                )
            ready = True
            yield
        finally:
            ready = False
            await runner.close()

    app = FastAPI(
        title="Reach-Bridge",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        responses={422: {"model": ErrorResponse}},
    )
    app.add_middleware(BodyLimit)
    app.state.runner = runner
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
        return {
            "agent_reach_version": version,
            "channels": [
                {
                    "name": name,
                    "description": row["description"],
                    "actions": [
                        a["name"] for a in row["actions"] if a["name"] in ("search", "read")
                    ],
                    "hosts": policies[name].hosts,
                }
                for name, row in catalog.items()
            ],
        }

    def action_for(platform: str, action: str) -> dict:
        if platform not in catalog:
            raise HTTPException(403, "未启用该平台")
        for item in catalog[platform]["actions"]:
            if item["name"] == action:
                return item
        raise HTTPException(422, "该平台不支持此动作")

    async def execute(request: Request, args: list[str]) -> Any:
        task = asyncio.create_task(runner.run(args))

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
