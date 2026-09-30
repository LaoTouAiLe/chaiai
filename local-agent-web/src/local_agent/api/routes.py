"""HTTP 路由（T29；契约 §6.2、§15.3，计划 §6.2）。

这一层是**薄适配层**：解析请求 → 调应用服务 → 返回 T27 的 DTO。业务规则、每会话单飞、幂等
台账、错误码都在服务里；本层只做服务不做的那几件事：

1. 请求形状校验（pydantic，未知字段直接 422）；
2. 对用户给出的**路径**补工作区校验（绝对路径 + 存在 + 可访问）；
3. 把 `ApplicationError` 映射成 HTTP 状态码（显式表，未知码一律 500——猜状态码会让同一类
   错误在客户端出现两种处理方式）。

`/api/sessions/{id}/events`（SSE）不在本模块：它要写流式响应，单独放 `sse.py`。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, cast

from fastapi import APIRouter, HTTPException, Query, Request

from local_agent import directory_picker
from local_agent import workspaces as workspaces_module
from local_agent.api.container import ApiRuntime
from local_agent.api.schemas import (
    ApprovalDecisionRequest,
    CreateSessionRequest,
    ErrorResponse,
    HealthView,
    ModelConnectionRequest,
    PickDirectoryView,
    SelectDefaultConnectionRequest,
    SelectionExpectation,
    SelectSessionModelRequest,
    SelectWorkspaceRequest,
    SubmitMessageRequest,
)
from local_agent.api.security import is_allowed_origin
from local_agent.application import errors as app_errors
from local_agent.application.models import (
    SCHEMA_VERSION,
    CommandResultView,
    ModelConnectionView,
    SessionSnapshot,
    SessionSummaryView,
    WorkspaceRef,
)
from local_agent.exceptions import SessionStoreError
from local_agent.model_settings import ConnectionDraft, SelectionRef

#: 公开错误码 → HTTP 状态码。**显式列出**；表里没有的一律 500。
#: 409 用于"服务端状态决定了这次不能做"（忙碌、冲突、需要人工核对、审批失效）。
_STATUS_BY_CODE: Final[dict[str, int]] = {
    app_errors.SESSION_NOT_FOUND: 404,
    app_errors.WORKSPACE_NOT_FOUND: 404,
    app_errors.MODEL_CONNECTION_NOT_FOUND: 404,
    app_errors.SESSION_BUSY: 409,
    app_errors.IDEMPOTENCY_CONFLICT: 409,
    app_errors.RECOVERY_REQUIRED: 409,
    app_errors.APPROVAL_EXPIRED: 409,
    app_errors.MODEL_CAPABILITY_UNSUPPORTED: 409,
    app_errors.MODEL_CONNECTION_REQUIRED: 409,
    # 需要先更新这条连接的密钥：是"服务端状态决定了现在做不了"，不是客户端请求写错了。
    # （若将来它也用来表示"供应商拒绝了我们的密钥"，那更接近 502——错误码是稳定契约，映射可再分。）
    app_errors.MODEL_AUTH_FAILED: 409,
    app_errors.MODEL_SETTINGS_CONFLICT: 409,
    # 字段不合法：与 FastAPI 自己的形状校验同为 422——区别只在信封（业务错误是 `{"error": ...}`）。
    app_errors.MODEL_SETTINGS_INVALID: 422,
    app_errors.MODEL_RATE_LIMITED: 429,
    app_errors.SECURITY_POLICY_DENIED: 403,
    app_errors.WORKSPACE_BOUNDARY_VIOLATION: 403,
    app_errors.MODEL_RETRY_EXHAUSTED: 504,
    app_errors.SETTINGS_STORE_UNAVAILABLE: 503,
    # 宿主能力故障（弹不出目录选择器）：服务还在，依赖不可用，稍后可重试。
    app_errors.DIRECTORY_PICKER_UNAVAILABLE: 503,
}

#: 默认状态码：不在表里就是**未预期**，不假装知道它该怎么重试。
DEFAULT_STATUS = 500

logger = logging.getLogger(__name__)

_NOT_FOUND: Final[dict[int | str, dict[str, Any]]] = {
    404: {"model": ErrorResponse, "description": "会话或工作区不存在"}
}
_STATE_CONFLICT: Final[dict[int | str, dict[str, Any]]] = {
    404: {"model": ErrorResponse, "description": "会话不存在"},
    409: {"model": ErrorResponse, "description": "忙碌 / 幂等冲突 / 需要人工核对 / 审批已失效"},
}
_PICK_RESPONSES: Final[dict[int | str, dict[str, Any]]] = {
    403: {"description": "不是本机页面的同源请求（本接口只服务于页面）"},
    503: {"model": ErrorResponse, "description": "宿主弹不出目录选择器"},
}


def status_for_code(code: str) -> int:
    """公开错误码 → HTTP 状态码（未知码 500）。"""
    return _STATUS_BY_CODE.get(code, DEFAULT_STATUS)


def _validated_path(raw: str) -> Path:
    """用户给出的工作区路径先过校验（绝对路径 + 存在 + 可访问），再交给服务。

    校验失败用 `workspace_unusable`：它的文案里**不带那条路径**——用户填的绝对路径属于私人
    信息，`ErrorView` 的展示文本 fail-closed 拒绝它（§15.8）；回显由前端自己做。
    """
    try:
        return workspaces_module.validate_workspace(raw)
    except SessionStoreError as exc:
        raise app_errors.workspace_unusable(occurred_at=datetime.now(timezone.utc)) from exc


def _require_browser_origin(request: Request, *, allowed_origins: Iterable[str]) -> None:
    """只接受**带同源浏览器元数据**的请求，否则抛传输层 403。

    本服务只有这一个接口需要额外加严：它会往用户桌面上弹一个窗口，是唯一能**干扰桌面**的能力。
    跨源网页已被中间件挡住，这一条挡的是**本机脚本**（curl）——它想要原生对话框的话自己就能弹，
    没有理由借这条路。

    拒绝走传输层语义（`{"detail": ...}`）而不是业务信封：这不是业务结论，而是"这个请求不配进
    业务逻辑"，与中间件对 `Host` / `Origin` 的判定属于同一层。
    """
    origin = request.headers.get("origin")
    host = request.headers.get("host", "")
    if origin is None or not is_allowed_origin(origin, host, allowed_origins):
        raise HTTPException(status_code=403, detail="本接口只接受来自本机页面的同源请求。")


def _selection_ref(payload: SelectionExpectation) -> SelectionRef:
    """传输层的"我看到的选择" → 设置库的乐观引用（§6.1）。

    两个字段一起看：只给 `config_version` 不给 `connection_id`（或反过来）是自相矛盾的观测，
    直接当非法请求拒掉，而不是猜一半。
    """
    if (payload.connection_id is None) != (payload.config_version is None):
        raise app_errors.model_settings_invalid(
            detail="expected_selection 的 connection_id 与 config_version 要么都给、要么都给 null。",
            occurred_at=datetime.now(timezone.utc),
        )
    return SelectionRef(connection_id=payload.connection_id, config_version=payload.config_version)


def _connection_draft(
    payload: ModelConnectionRequest, *, connection_id: str | None = None
) -> ConnectionDraft:
    """请求体 → 领域草稿：**逐个字段映射**允许的项。

    刻意不写 `**payload.model_dump()`：那样以后请求体里多一个字段就会悄悄变成领域字段，
    而领域字段里有些（凭据引用、版本号）本来就不该由客户端决定。
    """
    return ConnectionDraft(
        connection_id=connection_id,
        display_name=payload.display_name,
        base_url=payload.base_url,
        model=payload.model,
        context_window_tokens=payload.context_window_tokens,
        max_tokens=payload.max_tokens,
        temperature=payload.temperature,
        timeout_seconds=payload.timeout_seconds,
        retry_count=payload.retry_count,
        streaming=payload.streaming,
        auth_mode=payload.auth_mode,
        allow_plaintext_http=payload.allow_plaintext_http,
    )


def build_router(runtime: ApiRuntime) -> APIRouter:
    """构造业务路由（不含 SSE 与静态托管）。"""
    router = APIRouter(prefix="/api")
    service = runtime.service

    @router.get("/health", response_model=HealthView, summary="健康检查")
    def health() -> HealthView:
        """服务可用性。不含路径、密钥或内部标识。

        `model_configured=false` 是**初始设置模式**的正常状态（§15.9）：服务照常可用，用户去设置页
        加一条连接就行；提交消息会得到可执行的配置提示。
        """
        credential_store, credential_note = service.credential_store_state()
        return HealthView(
            schema_version=SCHEMA_VERSION,
            model_configured=service.model_configured(),
            model_name=service.current_model_name(),
            credential_store=cast(Any, credential_store),
            credential_note=credential_note,
            static_frontend=runtime.static_dir is not None,
        )

    @router.get("/workspaces", response_model=list[WorkspaceRef], summary="已使用的工作区")
    def list_workspaces() -> list[WorkspaceRef]:
        return service.list_workspaces()

    @router.post(
        "/workspaces/select",
        response_model=SessionSummaryView,
        responses=_NOT_FOUND,
        summary="选择工作区",
    )
    def select_workspace(payload: SelectWorkspaceRequest) -> SessionSummaryView:
        """已列出的工作区用 `workspace_id`；新目录用绝对路径 `path`（服务端校验后绑定）。"""
        if payload.workspace_id is not None:
            return service.select_workspace_by_id(payload.workspace_id)
        return service.select_workspace(_validated_path(payload.path or ""))

    @router.post(
        "/workspaces/pick",
        response_model=PickDirectoryView,
        responses=_PICK_RESPONSES,
        summary="弹出目录选择器",
    )
    def pick_workspace_directory(request: Request) -> PickDirectoryView:
        """弹出**本机**原生目录选择器，返回选中的绝对路径；用户取消时 `path=null`。

        为什么是独立一步、而不是"选完直接登记"：取消是正常操作，不该改变任何状态。选完之后由
        前端调既有的 `POST /workspaces/select { path }`——校验、幂等与错误码全部复用那条路。
        """
        _require_browser_origin(request, allowed_origins=runtime.web.dev_origins)
        try:
            path = directory_picker.pick_directory()
        except directory_picker.DirectoryPickerError as exc:
            # 子进程原文只进本地日志：它可能带本机路径，不能进 ErrorView（§15.8）。
            logger.warning("目录选择器不可用：%s", exc)
            raise app_errors.directory_picker_unavailable(
                occurred_at=datetime.now(timezone.utc)
            ) from None
        return PickDirectoryView(path=path)

    @router.get("/sessions", response_model=list[SessionSummaryView], summary="会话列表")
    def list_sessions(
        workspace_id: str | None = Query(default=None, min_length=1, max_length=128),
    ) -> list[SessionSummaryView]:
        """`workspace_id` 缺省表示列出全部会话。"""
        return service.list_sessions(workspace_id=workspace_id)

    @router.post(
        "/sessions",
        response_model=SessionSummaryView,
        responses=_NOT_FOUND,
        summary="新建会话",
    )
    def create_session(payload: CreateSessionRequest | None = None) -> SessionSummaryView:
        """新建会话并切换过去；`workspace_id` 缺省表示沿用库的启动默认工作区。"""
        options = payload or CreateSessionRequest()
        return service.create_session(title=options.title, workspace_id=options.workspace_id)

    @router.get(
        "/sessions/{session_id}",
        response_model=SessionSnapshot,
        responses=_NOT_FOUND,
        summary="会话快照",
    )
    def get_session(session_id: str) -> SessionSnapshot:
        """只读快照；刷新与重连都以它为准（§15.3）。"""
        return service.get_session_snapshot(session_id)

    @router.post(
        "/sessions/{session_id}/messages",
        response_model=CommandResultView,
        responses=_STATE_CONFLICT,
        summary="提交消息",
    )
    def submit_message(session_id: str, payload: SubmitMessageRequest) -> CommandResultView:
        """同步跑完这一轮，返回**稳定的命令结果引用**：结局不依赖 SSE（§15.3）。"""
        return service.submit_message(session_id, payload.text, payload.client_request_id)

    @router.post(
        "/sessions/{session_id}/approvals/{approval_id}",
        response_model=CommandResultView,
        responses=_STATE_CONFLICT,
        summary="提交审批决定",
    )
    def resolve_approval(
        session_id: str, approval_id: str, payload: ApprovalDecisionRequest
    ) -> CommandResultView:
        return service.resolve_approval(
            session_id,
            approval_id,
            payload.decision,
            payload.client_request_id,
            edited_arguments=payload.edited_arguments,
            reason=payload.reason,
            confirm_recursive=payload.confirm_recursive,
        )

    # ---- 模型连接（T29A；§6.8）------------------------------------------------
    # 这组接口只读写"连接设置"：密钥只进凭据存储，读接口只回"已配置 / 未配置 / 仅本次进程内"。
    # 它们**不能**改安全策略、工具权限或 Graph 配置，也没有传入连接 id/版本的旁路。

    @router.get(
        "/model-connections",
        response_model=list[ModelConnectionView],
        summary="模型连接列表",
    )
    def list_model_connections() -> list[ModelConnectionView]:
        """按创建顺序；默认连接带 `is_default`。停用的连接不在这里出现（历史仍在库里）。"""
        return service.list_model_connections()

    @router.post(
        "/model-connections",
        response_model=ModelConnectionView,
        responses=_STATE_CONFLICT,
        summary="新增模型连接",
    )
    def create_model_connection(payload: ModelConnectionRequest) -> ModelConnectionView:
        """保存即可用（哪怕还没测）：未通过检测的连接标为"未验证"（§6.8）。"""
        return service.save_model_connection(_connection_draft(payload), api_key=payload.api_key)

    @router.patch(
        "/model-connections/{connection_id}",
        response_model=ModelConnectionView,
        responses=_STATE_CONFLICT,
        summary="编辑模型连接",
    )
    def update_model_connection(
        connection_id: str, payload: ModelConnectionRequest
    ) -> ModelConnectionView:
        """编辑产生**新版本**；必须带 `expected_version`，否则拒绝（§6.1 的并发写入口径）。"""
        return service.save_model_connection(
            _connection_draft(payload, connection_id=connection_id),
            api_key=payload.api_key,
            expected_version=payload.expected_version,
        )

    @router.delete(
        "/model-connections/{connection_id}",
        response_model=ModelConnectionView,
        responses=_STATE_CONFLICT,
        summary="停用模型连接",
    )
    def delete_model_connection(
        connection_id: str,
        expected_version: int | None = Query(default=None, ge=1),
    ) -> ModelConnectionView:
        """删除是**停用**：历史模型标识与用量不删，也不物理清除凭据引用（§6.8）。"""
        return service.retire_model_connection(connection_id, expected_version=expected_version)

    @router.post(
        "/model-connections/{connection_id}/test",
        response_model=ModelConnectionView,
        responses=_STATE_CONFLICT,
        summary="测试模型连接",
    )
    def test_model_connection(connection_id: str) -> ModelConnectionView:
        """用户主动触发的一次探测（会产生少量调用）：分项报告鉴权/聊天/工具/流式。

        失败只写进这条连接的检测结果，**不改变当前选择**，也不回退到别的模型（§6.8）。
        """
        return service.test_model_connection(connection_id)

    @router.put(
        "/model-connections/default",
        response_model=ModelConnectionView,
        responses=_STATE_CONFLICT,
        summary="设置默认连接",
    )
    def select_default_model(payload: SelectDefaultConnectionRequest) -> ModelConnectionView:
        """新会话继承它；只影响**下一次新用户提交**（正在运行/等待审批的那一轮已固定，§6.8）。

        必须带上你看到的**当前默认**（`expected_selection`）：库里已被别的页面改过就返回 409，
        而不是把对方的改动覆盖掉（§6.1）。
        """
        return service.select_default_model(
            payload.connection_id,
            expected_version=payload.expected_version,
            expected_selection=_selection_ref(payload.expected_selection),
        )

    @router.put(
        "/sessions/{session_id}/model",
        response_model=SessionSnapshot,
        responses=_NOT_FOUND,
        summary="选择会话下一轮模型",
    )
    def select_session_model(
        session_id: str, payload: SelectSessionModelRequest
    ) -> SessionSnapshot:
        """为这个会话选下一轮用的连接；`connection_id=null` 表示恢复为跟随默认。

        返回**新快照**：让页面直接看到"下一轮会用哪条"，而不用自己拼状态。
        `expected_selection` 必填（"恢复跟随默认"也是一次写入，同样不能覆盖别人的选择）。
        """
        service.select_session_model(
            session_id,
            payload.connection_id,
            expected_version=payload.expected_version,
            expected_selection=_selection_ref(payload.expected_selection),
        )
        return service.get_session_snapshot(session_id)

    return router
