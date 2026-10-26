"""FastAPI 应用装配（T29；契约 §15.1、§15.3，计划 §6.2）。

`create_app` 是**唯一的装配点**：中间件顺序、错误信封、路由与静态托管都在这里定一次，
测试与官方入口都走它，不存在两套配置。

中间件顺序：先加 CORS、后加本地边界 → 边界在最外层，坏人（非法 `Host`）连 CORS 逻辑都进不来。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Final

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

from local_agent.api.browsers import build_browser_router
from local_agent.api.container import ApiRuntime
from local_agent.api.routes import build_router, status_for_code
from local_agent.api.schema_extras import merge_event_schemas
from local_agent.api.security import LocalBoundaryMiddleware
from local_agent.api.sse import build_events_router
from local_agent.api.static import mount_frontend
from local_agent.api.terminals import build_terminal_router
from local_agent.application.browsers import BrowserManager
from local_agent.application.cron import CronScheduler
from local_agent.application.errors import ApplicationError

#: OpenAPI 里的接口版本：跟着 v0.4 的契约走（不是包版本）。
API_VERSION: Final[str] = "0.4.0"

DESCRIPTION = """v0.4 本机 Web 服务（单进程、单 worker）。

- 业务契约：`ARCHITECTURE.md` §15；路由形态：`V0_4_DEVELOPMENT_PLAN.md` §6.2。
- 所有响应体是应用层的 DTO（`local_agent.application.models`）；业务错误统一是
  `{"error": ErrorView}`，传输层拒绝（403/413/422）是 `{"detail": ...}`。
- 前端类型由这份 OpenAPI 生成（`npm run generate:api`），生成文件不得手改。
"""


async def _application_error(_request: Request, exc: Exception) -> Response:
    """`ApplicationError` → HTTP：状态码来自显式映射表，正文是统一的错误信封。

    信封形状是 `ErrorResponse`（`{"error": ErrorView}`），不是 FastAPI 自己的 `{"detail": ...}`：
    两者撞形状的话，前端就分不清"业务错误"和"传输层拒绝"（403/413/422）。
    """
    if not isinstance(exc, ApplicationError):  # pragma: no cover - 由注册的异常类型保证
        raise exc
    return JSONResponse(
        status_code=status_for_code(exc.view.code),
        content={"error": jsonable_encoder(exc.view)},
        headers={"Cache-Control": "no-store"},
    )


def create_app(runtime: ApiRuntime) -> FastAPI:
    """按一套运行时装出可服务的应用。"""
    # 定时任务的调度线程随应用启停（§21.5）。**不**放在 ApplicationService 构造里：
    # 每个测试都会新建 service，那样每建一个就泄漏一个后台线程。
    scheduler = CronScheduler(runtime.service)
    browsers = BrowserManager(runtime.service)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        runtime.service.cron_scheduler = scheduler
        terminals = runtime.service.user_terminals()
        try:
            scheduler.start()
            terminals.start()
            browsers.start()
            yield
        finally:
            try:
                try:
                    browsers.close()
                finally:
                    terminals.close()
            finally:
                scheduler.stop()
                runtime.service.cron_scheduler = None

    app = FastAPI(
        title="local-agent-web",
        version=API_VERSION,
        lifespan=lifespan,
        description=DESCRIPTION,
        openapi_url="/api/openapi.json",
        # 交互文档不挂在本服务上：本机无鉴权的调试页面没有价值，只会扩大暴露面。
        docs_url=None,
        redoc_url=None,
    )
    app.state.runtime = runtime

    origins = list(runtime.web.dev_origins)
    if origins:
        # 开发期跨源：只允许配置里列出的地址（默认就是 Vite dev server）；生产同源。
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["content-type", "if-none-match"],
            allow_credentials=False,
            max_age=600,
        )
    app.add_middleware(  # 最后加 = 最外层：先做本机边界判定
        LocalBoundaryMiddleware,
        allowed_hosts=runtime.web.allowed_hosts,
        allowed_origins=origins,
        max_body_bytes=runtime.web.max_body_bytes,
    )

    app.add_exception_handler(ApplicationError, _application_error)
    async def validation_error(request: Request, exc: Exception) -> Response:
        if not isinstance(exc, RequestValidationError):
            raise exc
        if request.url.path.startswith("/api/mcp"):
            return JSONResponse(status_code=422, content={"detail": [
                {"loc": error["loc"], "type": error["type"], "msg": "字段格式不合法"}
                for error in exc.errors()
            ]})
        response = await request_validation_exception_handler(request, exc)
        if request.url.path.startswith("/api/terminals"):
            response.headers["Cache-Control"] = "no-store"
        return response

    app.add_exception_handler(RequestValidationError, validation_error)
    app.include_router(build_router(runtime))
    app.include_router(build_events_router(runtime))
    app.include_router(build_terminal_router(runtime))
    app.include_router(build_browser_router(browsers))
    app.state.browsers = browsers
    mount_frontend(app, runtime.static_dir)

    # SSE 事件面不在路由的 `response_model` 里，但在**装配点**补进文档：这样真实服务的
    # `/api/openapi.json` 与 `scripts/export_openapi.py` 的导出逐字节一致（T30、§15.1）。
    base_openapi = app.openapi

    def openapi_with_event_schemas() -> dict[str, Any]:
        document = base_openapi()
        merge_event_schemas(document)
        return document

    # 覆盖实例上的 `app.openapi` 是 FastAPI 的官方扩展点（`/api/openapi.json` 路由动态调用它）。
    app.openapi = openapi_with_event_schemas  # type: ignore[method-assign]
    return app
