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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Literal, cast

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse

from local_agent import directory_picker
from local_agent import workspaces as workspaces_module
from local_agent.api.container import ApiRuntime
from local_agent.api.schemas import (
    PATH_MAX_CHARS,
    ApprovalDecisionRequest,
    ArrangeBranchRequest,
    AttachmentUploadRequest,
    CancelTaskRequest,
    CapabilitiesRequest,
    CreateSessionRequest,
    CreateSideChatRequest,
    ErrorResponse,
    ExternalPathGrantRequest,
    HealthView,
    McpServerCreateRequest,
    McpServerRequest,
    McpServerSpecRequest,
    ModelConnectionRequest,
    OssCredentialRequest,
    OssCredentialView,
    OssTestResult,
    PermissionsModeRequest,
    PickDirectoryView,
    RecoveryRequest,
    RenameRequest,
    ResolveTaskReviewRequest,
    SelectDefaultConnectionRequest,
    SelectionExpectation,
    SelectSessionModelRequest,
    SelectWorkspaceRequest,
    SessionModeRequest,
    SkillRootRequest,
    SkillToggleRequest,
    StorageLocationRequest,
    SubmitMessageRequest,
    WebSearchKeyRequest,
)
from local_agent.api.security import require_browser_origin
from local_agent.application import errors as app_errors
from local_agent.application.cron_models import (
    CronJobCreateRequest,
    CronJobsView,
    CronJobUpdateRequest,
    CronJobView,
    CronRunsPageView,
    CronRunView,
)
from local_agent.application.data_center_models import (
    ArtifactPreviewView,
    ArtifactsPageView,
    MemoryPageView,
    UsagePageView,
)
from local_agent.application.models import (
    SCHEMA_VERSION,
    AttachmentView,
    CapabilitiesView,
    CommandResultView,
    ConversationMapView,
    FileChangeDiffView,
    McpView,
    ModelConnectionView,
    PermissionsView,
    PreferencesSchemaView,
    SessionChangesView,
    SessionSnapshot,
    SessionSummaryView,
    SkillsView,
    StorageView,
    TaskCenterView,
    WebSearchKeyView,
    WorkspaceRef,
)
from local_agent.application.workspace_models import (
    GitDiffView,
    GitStatusView,
    WorkspaceFilesView,
)
from local_agent.exceptions import AttachmentError, SessionStoreError
from local_agent.model_settings import ConnectionDraft, SelectionRef
from local_agent.model_settings.preferences import (
    MemoryProbeRequest,
    MemoryProbeResult,
    PreferencesRequest,
    PreferencesView,
)
from local_agent.model_settings.preferences_schema import preferences_schema
from local_agent.persistence.job_store import JobStoreError

#: 公开错误码 → HTTP 状态码。**显式列出**；表里没有的一律 500。
#: 409 用于"服务端状态决定了这次不能做"（忙碌、冲突、需要人工核对、审批失效）。
_STATUS_BY_CODE: Final[dict[str, int]] = {
    app_errors.SESSION_NOT_FOUND: 404,
    app_errors.WORKSPACE_NOT_FOUND: 404,
    app_errors.MODEL_CONNECTION_NOT_FOUND: 404,
    app_errors.SESSION_BUSY: 409,
    app_errors.CONVERSATION_CONFLICT: 409,
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
    # 请求本身成立，但当前形态（绿色包 / 源码）没有可卸载的安装：是状态不允许，不是服务故障。
    app_errors.UNINSTALL_UNAVAILABLE: 409,
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
#: 权限写接口：路径/档位没通过业务校验（422），或没有可用的设置库（503，改动存不下来）。
_PERMISSION_WRITE_RESPONSES: Final[dict[int | str, dict[str, Any]]] = {
    422: {"model": ErrorResponse, "description": "目录路径或档位没有通过校验"},
    503: {"model": ErrorResponse, "description": "没有可用的设置库，改动存不下来"},
}
#: 能力开关写接口（§16.1）：与权限写同一口径——未登记的能力名 422，没有设置库 503。
_CAPABILITY_WRITE_RESPONSES: Final[dict[int | str, dict[str, Any]]] = {
    422: {"model": ErrorResponse, "description": "能力名不在闭集里"},
    503: {"model": ErrorResponse, "description": "没有可用的设置库，改动存不下来"},
}
#: 联网检索凭据写接口：空白/超长密钥 422，没有凭据存储 503（不能复用权限那两条文案——
#: 那里说的是"目录路径或档位"，照着显示会把用户引到错误的字段上）。
_WEB_CREDENTIAL_WRITE_RESPONSES: Final[dict[int | str, dict[str, Any]]] = {
    422: {"model": ErrorResponse, "description": "密钥为空白或超出长度上限"},
    503: {"model": ErrorResponse, "description": "没有可用的凭据存储，密钥存不下来"},
}
#: 技能写接口（技能管理页，2026-10-16）：名字不是当前发现的技能 422，没有设置库 503。
_MCP_WRITE_RESPONSES: Final[dict[int | str, dict[str, Any]]] = {
    422: {"model": ErrorResponse, "description": "mcp.json 里没有这台 server，或配置/名单不合法"},
    503: {"model": ErrorResponse, "description": "没有可用的设置库，改动存不下来"},
}

_SKILL_WRITE_RESPONSES: Final[dict[int | str, dict[str, Any]]] = {
    422: {"model": ErrorResponse, "description": "没有发现这个技能（理由里列出当前可用的名字）"},
    503: {"model": ErrorResponse, "description": "没有可用的设置库，改动存不下来"},
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
        # 2026-10-16：逐连接声明"支持图片输入"（默认关了；勾错会让带图的请求每轮失败）。
        supports_vision=payload.supports_vision,
        auth_mode=payload.auth_mode,
        allow_plaintext_http=payload.allow_plaintext_http,
    )


def build_router(runtime: ApiRuntime) -> APIRouter:
    """构造业务路由（不含 SSE 与静态托管）。"""
    router = APIRouter(prefix="/api")
    service = runtime.service

    @router.get("/data-center/memories", response_model=MemoryPageView, summary="只读记忆列表")
    def memory_page(workspace_id: str | None = None,
                    kind: Literal["all", "medium", "fact", "conclusion"] = "all",
                    query: str = Query(default="", max_length=500),
                    status: Literal["all", "active", "expired", "superseded", "conflicted", "deleted", "unknown"] = "all",
                    offset: int = Query(default=0, ge=0, le=30000), limit: int = Query(default=30, ge=1, le=100)) -> MemoryPageView:
        return service.get_memory_page(workspace_id=workspace_id, kind=kind, query=query,
                                       status=status, offset=offset, limit=limit)

    @router.get("/data-center/usage", response_model=UsagePageView, summary="去重后的实际调用用量")
    def usage_page(workspace_id: str | None = None, model: str = Query(default="", max_length=200),
                   since: datetime | None = None, until: datetime | None = None,
                   offset: int = Query(default=0, ge=0, le=100000), limit: int = Query(default=30, ge=1, le=100)) -> UsagePageView:
        if any(moment is not None and moment.tzinfo is None for moment in (since, until)):
            raise HTTPException(status_code=422, detail="时间必须带时区")
        if since is not None and until is not None and since >= until:
            raise HTTPException(status_code=422, detail="开始时间必须早于结束时间")
        return service.get_usage_page(workspace_id=workspace_id, model=model, since=since,
                                      until=until, offset=offset, limit=limit)

    @router.get("/data-center/artifacts", response_model=ArtifactsPageView, summary="只读产物汇总")
    def artifacts_page(workspace_id: str | None = None,
                       kind: Literal["image", "file", "link"] | None = None,
                       days: int = Query(default=30, ge=1, le=365),
                       offset: int = Query(default=0, ge=0, le=100000),
                       limit: int = Query(default=50, ge=1, le=200),
                       refresh: bool = Query(default=False)) -> ArtifactsPageView:
        """Agent 跑出来的东西：图片、写出的文件、引用过的链接（§15.11.1）。

        每次请求现算，不新建表；触扫描闸门时 `incomplete=true` 并说明没扫全。
        `refresh=true` 跳过 60 秒结果缓存（页面「刷新」按钮用）。
        """
        return service.get_artifacts_page(workspace_id=workspace_id, kind=kind, days=days,
                                          offset=offset, limit=limit, refresh=refresh)

    @router.get("/data-center/artifacts/preview", response_model=ArtifactPreviewView,
                summary="产物内容预览")
    def preview_artifact(
        path: str = Query(min_length=1, max_length=500),
        workspace_id: str | None = Query(default=None, max_length=128),
        scope: Literal["workspace", "attachments"] = Query(default="workspace"),
    ) -> ArtifactPreviewView:
        """看一眼产物的内容（§15.11.3）：只读、有大小上限、不代打开外链。"""
        try:
            return service.preview_artifact(path=path, workspace_id=workspace_id, scope=scope)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    # ---- 侧边会话（§21.7，2026-10-23）------------------------------------
    @router.post("/sessions/{session_id}/side-chats", response_model=SessionSummaryView,
                 status_code=201, summary="从某个会话开一个侧聊")
    def create_side_chat(session_id: str, payload: CreateSideChatRequest) -> SessionSummaryView:
        """侧聊是**独立会话**（带 `parent_session_id`），不打断父会话的 active 分支。

        上下文由服务端读父会话的问答文本生成，**客户端不许传历史**——能传就能伪造。
        """
        try:
            return service.create_side_chat(
                parent_session_id=session_id, origin_turn_id=payload.origin_turn_id
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/sessions/{session_id}/promote", response_model=SessionSummaryView,
                 summary="侧聊提升为正式会话")
    def promote_session(session_id: str) -> SessionSummaryView:
        """清掉血统即可（§21.7）：**不改 id、不删历史**——改 id 会让已打开的页面与运行记录全部失效。"""
        try:
            return service.promote_side_chat(session_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    # ---- 右栏只读面板（§21.6，2026-10-22）--------------------------------
    @router.get("/workspace/files", response_model=WorkspaceFilesView, summary="列一层目录（只读）")
    def workspace_files(path: str = Query(default="", max_length=500),
                        workspace_id: str | None = Query(default=None, max_length=128),
                        recursive: bool = Query(default=False)) -> WorkspaceFilesView:
        """**不递归**、不含 `node_modules` 这类噪声目录；读文件内容走产物预览端点。"""
        try:
            return service.list_workspace_files(path=path, workspace_id=workspace_id,
                                              recursive=recursive)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/workspace/files/search", response_model=WorkspaceFilesView,
                summary="按文件名搜索工作区（只读）")
    def search_workspace_files(q: str = Query(min_length=1, max_length=120),
                               workspace_id: str | None = Query(default=None, max_length=128)) -> WorkspaceFilesView:
        """递归搜索文件名，跳过 `node_modules` 等噪声目录，最多 50 条、深度 8 层。"""
        try:
            return service.search_workspace_files(q=q, workspace_id=workspace_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/workspace/git/status", response_model=GitStatusView, summary="工作区 git 状态（只读）")
    def workspace_git_status(
        workspace_id: str | None = Query(default=None, max_length=128),
    ) -> GitStatusView:
        """工作区不是 git 仓库时返回 `available=false`，不是错误。"""
        return service.workspace_git_status(workspace_id=workspace_id)

    @router.get("/workspace/git/diff", response_model=GitDiffView, summary="单文件 diff（只读）")
    def workspace_git_diff(path: str = Query(min_length=1, max_length=500),
                           workspace_id: str | None = Query(default=None, max_length=128)) -> GitDiffView:
        """相对 `HEAD` 的 diff。未跟踪文件没有 diff——那是状态信息，不在这里编造。"""
        try:
            return service.workspace_git_diff(path=path, workspace_id=workspace_id)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    # ---- 定时任务（§21.5，2026-10-22 J2）--------------------------------
    @router.get("/cron/jobs", response_model=CronJobsView, summary="定时任务列表")
    def list_cron_jobs() -> CronJobsView:
        """`scheduler_running=false` 表示调度线程没起来——页面上要点明"不会触发"。"""
        return service.get_cron_jobs()

    @router.post("/cron/jobs", response_model=CronJobView, status_code=201, summary="新建定时任务")
    def create_cron_job(payload: CronJobCreateRequest) -> CronJobView:
        try:
            return service.create_cron_job(
                name=payload.name, prompt=payload.prompt, schedule=payload.schedule,
                workspace_id=payload.workspace_id, mode=payload.mode,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.patch("/cron/jobs/{job_id}", response_model=CronJobView, summary="修改定时任务")
    def update_cron_job(job_id: str, payload: CronJobUpdateRequest) -> CronJobView:
        try:
            return service.update_cron_job(
                job_id, name=payload.name, prompt=payload.prompt, schedule=payload.schedule,
                mode=payload.mode, enabled=payload.enabled,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.delete("/cron/jobs/{job_id}", status_code=204, summary="删除定时任务")
    def delete_cron_job(job_id: str) -> Response:
        try:
            service.delete_cron_job(job_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return Response(status_code=204)

    @router.post("/cron/jobs/{job_id}/run", response_model=CronRunView, summary="立即触发一次")
    def run_cron_job(job_id: str) -> CronRunView:
        """不等排期地跑一次；**排期本身不动**。上一次没跑完时返回 409。"""
        try:
            return service.run_cron_job_now(job_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.get("/cron/jobs/{job_id}/runs", response_model=CronRunsPageView, summary="运行记录")
    def list_cron_runs(job_id: str,
                       offset: int = Query(default=0, ge=0, le=100000),
                       limit: int = Query(default=20, ge=1, le=100)) -> CronRunsPageView:
        try:
            return service.get_cron_runs(job_id, offset=offset, limit=limit)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/sessions/{session_id}/attachments", response_model=AttachmentView,
                 summary="上传一张图片（base64）", responses=_NOT_FOUND)
    def upload_attachment(session_id: str, payload: AttachmentUploadRequest) -> AttachmentView:
        """存一张用户上传的图片，返回**引用**。

        **媒体类型由服务端按魔数判定**：客户端不声明、也不被信任（`报告.png` 完全可能是 HTML）。
        单图有字节上限（超限拒绝而不是截断）；数量不设配额。格式不支持、超限或 base64 不合法时
        返回 422，并说明怎么改。
        """
        try:
            saved = service.save_attachment(session_id, payload.data_base64)
        except AttachmentError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return AttachmentView(**saved)

    @router.get("/sessions/{session_id}/attachments", summary="取回一张已上传的图片",
                responses=_NOT_FOUND)
    def read_attachment(
        session_id: str, attachment_id: str = Query(min_length=1, max_length=300)
    ) -> Response:
        """把图片字节原样返回（页面用它显示缩略图）。

        引用是**相对路径**（含斜杠），所以走查询参数而不是路径参数——路径参数里的 `/` 会被当成
        路由分隔符，`%2F` 也不保证被还原。
        """
        try:
            result = service.read_attachment(session_id, attachment_id)
        except AttachmentError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if isinstance(result, str):
            # 对象存储引用：公开地址直接 302，服务端不代理字节（2026-10-20）。
            return RedirectResponse(result, status_code=302)
        data, media_type = result
        return Response(content=data, media_type=media_type)

    # ---- 对象存储凭据（2026-10-20）--------------------------------------------
    # 与模型连接同一套口径：只报"有没有"，不回读；密钥只进设置库或进程内存。

    @router.get("/attachments/oss/credential", response_model=OssCredentialView,
                summary="对象存储密钥状态")
    def get_oss_credential() -> OssCredentialView:
        return OssCredentialView(**service.oss_credential_view())

    @router.put("/attachments/oss/credential", response_model=OssCredentialView,
                summary="保存对象存储密钥", responses={
                    422: {"model": ErrorResponse, "description": "密钥不合法"},
                    503: {"model": ErrorResponse, "description": "设置库不可用"},
                })
    def put_oss_credential(payload: OssCredentialRequest) -> OssCredentialView:
        return OssCredentialView(**service.save_oss_credentials(payload.access_key, payload.secret_key))

    @router.delete("/attachments/oss/credential", response_model=OssCredentialView,
                   summary="删除对象存储密钥")
    def delete_oss_credential() -> OssCredentialView:
        return OssCredentialView(**service.clear_oss_credentials())

    @router.post("/attachments/oss/test", response_model=OssTestResult,
                 summary="测试对象存储（上传一张 1×1 测试图并检查公开读）")
    def test_oss() -> OssTestResult:
        return OssTestResult(**service.test_oss_upload())

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
        require_browser_origin(request, allowed_origins=runtime.web.dev_origins)
        try:
            path = directory_picker.pick_directory()
        except directory_picker.DirectoryPickerError as exc:
            # 子进程原文只进本地日志：它可能带本机路径，不能进 ErrorView（§15.8）。
            logger.warning("目录选择器不可用：%s", exc)
            raise app_errors.directory_picker_unavailable(
                occurred_at=datetime.now(timezone.utc)
            ) from None
        return PickDirectoryView(path=path)

    @router.patch("/workspaces/{workspace_id}", response_model=WorkspaceRef, responses=_NOT_FOUND)
    def rename_workspace(workspace_id: str, payload: RenameRequest) -> WorkspaceRef:
        return service.rename_workspace(workspace_id, payload.title)

    @router.delete("/workspaces/{workspace_id}", status_code=204, responses=_STATE_CONFLICT)
    def remove_workspace(workspace_id: str) -> None:
        """仅移除工作区登记，保留磁盘目录与全部会话。"""
        service.remove_workspace(workspace_id)

    @router.get("/sessions", response_model=list[SessionSummaryView], summary="会话列表或搜索")
    def list_sessions(
        workspace_id: str | None = Query(default=None, min_length=1, max_length=128),
        archived: bool = Query(default=False),
        q: str = Query(default="", max_length=200),
        limit: int | None = Query(default=None, ge=1, le=100),
    ) -> list[SessionSummaryView]:
        """`workspace_id` 缺省表示列出全部会话。

        `q` 非空时改为搜索（§15.11.2）：匹配标题与用户提问，按最近活动倒序。
        **不传 `q` 时行为与响应和旧实现完全一致**，`limit` 也只在搜索分支生效。
        """
        return service.list_sessions(workspace_id=workspace_id, archived=archived,
                                     query=q, limit=limit)

    @router.patch("/sessions/{session_id}", response_model=SessionSummaryView, responses=_NOT_FOUND)
    def rename_session(session_id: str, payload: RenameRequest) -> SessionSummaryView:
        return service.rename_session(session_id, payload.title)

    @router.post("/sessions/{session_id}/select", response_model=SessionSummaryView, responses=_NOT_FOUND)
    def select_session(session_id: str) -> SessionSummaryView:
        return service.select_session(session_id)

    @router.patch("/sessions/{session_id}/mode", response_model=SessionSummaryView, responses=_STATE_CONFLICT)
    def set_session_mode(session_id: str, payload: SessionModeRequest) -> SessionSummaryView:
        return service.set_session_mode(session_id, payload.mode, payload.expected_mode)

    @router.post("/sessions/{session_id}/recovery", response_model=SessionSnapshot, responses=_STATE_CONFLICT)
    def end_interrupted_request(session_id: str, payload: RecoveryRequest) -> SessionSnapshot:
        return service.end_interrupted_request(session_id, payload.request_id, payload.expected_snapshot_id)

    @router.post("/sessions/{session_id}/cancel", response_model=CommandResultView, responses=_NOT_FOUND)
    def cancel_task(session_id: str, payload: CancelTaskRequest) -> CommandResultView:
        """取消在跑的任务（§17.4）：**幂等**，没有可取消的任务时返回当时的结局。"""
        return service.cancel_task(session_id, payload.reason)

    @router.post(
        "/sessions/{session_id}/commands/{command_id}/detach",
        responses=_NOT_FOUND,
        summary="把正在前台等待的命令转入后台",
    )
    def detach_command(session_id: str, command_id: str) -> dict[str, bool]:
        """只发"转后台"信号，不结束进程（2026-10-21）。

        返回 ``{"detached": false}`` 表示这条命令已经不在等（跑完了、或 id 对不上）——
        页面据此说"这条命令已经结束"，而不是假装它还在后台。
        """
        return {"detached": service.detach_command(session_id, command_id)}

    @router.post("/sessions/{session_id}/archive", response_model=SessionSummaryView, responses=_STATE_CONFLICT)
    def archive_session(session_id: str) -> SessionSummaryView:
        return service.archive_session(session_id)

    # ---- 任务中心（T62；§17.6）--------------------------------------------------

    @router.post("/tasks/{job_id}/review", summary="人工核对结案")
    def close_task_review(job_id: str, payload: ResolveTaskReviewRequest) -> dict[str, str]:
        """把 `needs_review` 的任务结案（§17.1 唯一例外）。

        没有这个入口，那条任务**出不去终态**，会话会永远挂在侧栏的「需人工核对」上。
        """
        try:
            state = service.close_task_review(job_id, outcome=payload.outcome)
        except LookupError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except JobStoreError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"job_id": job_id, "state": state}

    @router.get("/tasks", response_model=TaskCenterView,
                summary="任务中心（只读索引，不读会话 Checkpoint）")
    def list_tasks(
        workspace_id: str | None = Query(default=None, min_length=1, max_length=128),
        include_finished: bool = Query(default=True),
        limit: int = Query(default=50, ge=1, le=200),
    ) -> TaskCenterView:
        """当前工作区的任务列表：状态、已运行时长与"在等什么"。

        **只读**，且字段全部由任务索引 + 会话索引推出（§17.6）：不读任何会话的 Checkpoint，
        因此"任务在等什么"不可能与真源不一致。页面**不展示** `task_id`（它是稳定引用，只作列表键
        与动作参数，与 `session_id` 同一口径）。
        """
        return service.list_tasks(
            workspace_id=workspace_id, include_finished=include_finished, limit=limit
        )

    @router.post("/sessions/{session_id}/restore", response_model=SessionSummaryView, responses=_NOT_FOUND)
    def restore_session(session_id: str) -> SessionSummaryView:
        return service.restore_session(session_id)

    @router.post(
        "/sessions",
        response_model=SessionSummaryView,
        responses=_NOT_FOUND,
        summary="新建会话",
    )
    def create_session(payload: CreateSessionRequest | None = None) -> SessionSummaryView:
        """新建会话并切换过去；`workspace_id` 缺省表示沿用库的启动默认工作区。"""
        options = payload or CreateSessionRequest()
        return service.create_session(
            title=options.title, workspace_id=options.workspace_id, mode=options.mode
        )

    @router.get(
        "/sessions/{session_id}",
        response_model=SessionSnapshot,
        responses=_NOT_FOUND,
        summary="会话快照",
    )
    def get_session(session_id: str) -> SessionSnapshot:
        """只读快照；刷新与重连都以它为准（§15.3）。"""
        return service.get_session_snapshot(session_id)

    @router.get(
        "/sessions/{session_id}/changes",
        response_model=SessionChangesView,
        responses=_NOT_FOUND,
        summary="会话内文件变更总览",
    )
    def session_changes(session_id: str) -> SessionChangesView:
        """按轮分组的"改了哪些文件、各改了几行"（2026-10-21）。

        数据来自变更账本（写盘时记账 + 首次改动前留基线），**不是**从工具消息里的事件拼出来的：
        一个文件被 edit 四次时，事件里只有四段互不相干的片段。
        """
        return service.session_changes(session_id)

    @router.get(
        "/sessions/{session_id}/changes/diff",
        response_model=FileChangeDiffView,
        responses=_NOT_FOUND,
        summary="单个文件的整文件 diff",
    )
    def session_change_diff(
        session_id: str, path: str = Query(..., max_length=PATH_MAX_CHARS)
    ) -> FileChangeDiffView:
        """``baseline``（首次改动前原文）对当前磁盘内容，覆盖整个文件。"""
        return service.session_change_diff(session_id, path)

    @router.post(
        "/sessions/{session_id}/messages",
        response_model=CommandResultView,
        responses=_STATE_CONFLICT,
        summary="提交消息",
    )
    def submit_message(session_id: str, payload: SubmitMessageRequest) -> CommandResultView:
        """同步跑完这一轮，返回**稳定的命令结果引用**：结局不依赖 SSE（§15.3）。"""
        return service.submit_message(session_id, payload.text, payload.client_request_id,
                                      workflow=payload.workflow,
                                      # 随消息交给模型的图片引用（2026-10-16）：默认空 ⇒ 不带图的老路径不变。
                                      attachments=payload.attachments,
                                      parent_turn_id=payload.parent_turn_id,
                                      expected_branch_id=payload.expected_branch_id,
                                      expected_snapshot_id=payload.expected_snapshot_id)

    @router.get("/sessions/{session_id}/conversation-map", response_model=ConversationMapView,
                responses=_STATE_CONFLICT, summary="会话地图")
    def conversation_map(session_id: str, query: str = Query(default="", max_length=500)) -> ConversationMapView:
        return service.get_conversation_map(session_id, query)

    @router.get("/sessions/{session_id}/turns/{turn_id}", response_model=SessionSnapshot,
                responses=_STATE_CONFLICT, summary="单轮问答与执行详情")
    def turn_detail(session_id: str, turn_id: str, include_ancestors: bool = False) -> SessionSnapshot:
        return service.get_turn_snapshot(session_id, turn_id, include_ancestors=include_ancestors)

    @router.post("/sessions/{session_id}/branches/{branch_id}/select", response_model=SessionSnapshot,
                 responses=_STATE_CONFLICT, summary="切换会话分支")
    def select_branch(session_id: str, branch_id: str) -> SessionSnapshot:
        return service.select_branch(session_id, branch_id)

    @router.patch("/sessions/{session_id}/branches/{branch_id}", response_model=ConversationMapView,
                  responses=_STATE_CONFLICT, summary="分支改名与展示排序")
    def arrange_branch(session_id: str, branch_id: str, payload: ArrangeBranchRequest) -> ConversationMapView:
        return service.arrange_branch(session_id, branch_id, payload.title, payload.position)

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

    # ---- 能力开关（T56；§16.1、§16.6）------------------------------------------

    @router.get("/capabilities", response_model=CapabilitiesView, summary="能力开关与能力状态")
    def capabilities() -> CapabilitiesView:
        """能力开关表与每个能力的可用性、原因与下一步。

        **只讲能力不讲工具**（§16.6）：用户要判断的是"现在能不能联网、能不能提交"。答案每次现算，
        因此打开开关或装好依赖后刷新页面就能看到新结果，不会读到一份过期结论。
        """
        return service.capabilities_view()

    @router.put(
        "/capabilities",
        response_model=CapabilitiesView,
        responses=_CAPABILITY_WRITE_RESPONSES,
        summary="改能力开关",
    )
    def set_capabilities(payload: CapabilitiesRequest) -> CapabilitiesView:
        """打开/关闭能力开关：**保存即生效、作用于下一轮**（§16.1、§16.6）。

        改完**不用重启**——运行上下文每次装配时现取开关表，`config.yaml` 只是出厂默认。请求只带
        **这次动过的那几项**；响应是保存后的完整视图（页面直接换上它，不必再发一次读请求）。
        """
        return service.set_capabilities(payload.changes)

    # ---- 技能管理（技能管理页，2026-10-16；§16.5）------------------------------

    @router.get("/skills", response_model=SkillsView, summary="技能清单与启用状态")
    def skills() -> SkillsView:
        """本机发现到的技能、来源层与启用状态（**每次现算**）。

        只给名字、描述、来源与开关状态，不给技能目录的绝对路径；`diagnostics` 如实报告被跳过的条目
        （格式不合规、越界等）——不说的话，用户只会看到"我的技能怎么不见了"。
        """
        return service.skills_view()

    @router.put(
        "/skills",
        response_model=SkillsView,
        responses=_SKILL_WRITE_RESPONSES,
        summary="启用/禁用一个技能",
    )
    def set_skill(payload: SkillToggleRequest) -> SkillsView:
        """启用或禁用一个技能：**保存即生效、作用于下一轮**（§16.5）。

        禁用只影响"这个技能还能不能被用到"（模型清单里不再出现、正文也取不到），**不删除任何文件**；
        技能是否被"发现"仍由发现根决定。响应是保存后的完整视图，页面直接换上它，不必再取一次。
        """
        return service.set_skill_enabled(payload.name, payload.enabled)

    @router.post(
        "/skills/roots",
        response_model=SkillsView,
        responses=_SKILL_WRITE_RESPONSES,
        summary="添加一个技能发现根",
    )
    def add_skill_root(payload: SkillRootRequest) -> SkillsView:
        """把一个**已存在的目录**加进发现根（2026-10-20）。

        只引用、不复制文件：作者更新技能后下一轮就生效；移除只是不再扫描，不删任何东西。
        """
        return service.add_skill_root(payload.path)

    @router.delete(
        "/skills/roots",
        response_model=SkillsView,
        responses=_SKILL_WRITE_RESPONSES,
        summary="移除一个自定义技能发现根",
    )
    def remove_skill_root(path: str = Query(min_length=1)) -> SkillsView:
        """只允许移除**用户自己添加**的根；随包与个人级根按契约不可删。"""
        return service.remove_skill_root(path)

    # ---- MCP 外部工具（2026-10-20；契约 §6.13）--------------------------------
    # 这一组**必须显示工具名**（与能力面板§16.6 相反）：工具名是用户自己配的外部资产，
    # 不显示出来他就没法核对"这个 server 到底暴露了什么"。凭据（``env``）永不出现在任何响应里。

    @router.get("/mcp", response_model=McpView, summary="MCP Server、工具清单与规模统计")
    def mcp() -> McpView:
        """每台 server 的状态、工具与"多少按需检索"（**每次现算**）。

        ``config_error`` 每次重读 ``mcp.json`` 现算：改坏文件后刷新页面就能看到第几行错了。
        """
        return service.mcp_view()

    @router.post(
        "/mcp/servers",
        response_model=McpView,
        responses=_MCP_WRITE_RESPONSES,
        summary="新增一台 MCP Server",
    )
    def add_mcp_server(payload: McpServerCreateRequest) -> McpView:
        """把一台只走 stdio 的 server 写进 ``mcp.json``（已存在的同名会被拒）。

        `env` 里的凭据**只写不回读**：响应与日志都没有它们。写完立即重载连接管理器。
        """
        return service.add_mcp_server(payload.model_dump())

    @router.put(
        "/mcp/servers/{name}",
        response_model=McpView,
        responses=_MCP_WRITE_RESPONSES,
        summary="改一台 MCP Server 的开关或工具名单",
    )
    def set_mcp_server(name: str, payload: McpServerRequest) -> McpView:
        """打开/关闭一台 server，或收窄它的工具名单：**保存即生效、作用于下一轮**。

        只带本次动过的键；显式传 ``enabled_tools: null`` 表示"取消白名单"。响应是保存后的完整视图。
        """
        return service.set_mcp_server(name, payload.model_dump(exclude_unset=True))

    @router.put("/mcp/servers/{name}/spec", response_model=McpView,
                responses=_MCP_WRITE_RESPONSES, summary="编辑 MCP Server 连接方式")
    def edit_mcp_server(name: str, payload: McpServerSpecRequest) -> McpView:
        return service.edit_mcp_server(name, payload.model_dump(exclude_unset=True))

    @router.delete("/mcp/servers/{name}", response_model=McpView,
                   responses=_MCP_WRITE_RESPONSES, summary="删除 MCP Server")
    def delete_mcp_server(name: str) -> McpView:
        return service.delete_mcp_server(name)

    @router.post(
        "/mcp/reload",
        response_model=McpView,
        responses=_MCP_WRITE_RESPONSES,
        summary="重读 mcp.json",
    )
    def reload_mcp() -> McpView:
        """重读配置文件并让连接管理器换一份（"添加 server"之后走这里）。

        与保存开关不同，这一步**会断开**新配置里已经不存在的 server，并清掉它的工具目录。
        """
        return service.reload_mcp()

    # ---- 权限（T38；§1.5）------------------------------------------------------
    # 这组接口只读写"工作区外路径"的档位与授权条目：档位存设置库（`config.yaml` 只是出厂默认），
    # 授权条目存设置库的专用表。它们**不能**放宽四项硬约束（删除文件、覆盖已有文件、破坏性 Shell、
    # 大范围编辑任何档位都要人工批准），因此这里没有"关闭审批"这类入口。

    @router.get("/permissions", response_model=PermissionsView, summary="权限设置")
    def get_permissions() -> PermissionsView:
        """当前档位与授权条目。

        没有可用设置库时**不报错**：返回出厂默认档位并置 `store_unavailable=true`——设置页要能
        打开并如实说明"这里改了也存不下来"，而不是整页打不开。
        """
        return service.permissions()

    @router.put(
        "/permissions",
        response_model=PermissionsView,
        responses=_PERMISSION_WRITE_RESPONSES,
        summary="切换外部路径档位",
    )
    def set_permissions_mode(payload: PermissionsModeRequest) -> PermissionsView:
        """保存档位。改动**作用于下一轮**：正在跑或等待审批的那些运行已按当时的档位判定完。

        `read-only` / `workspace-write` / `danger-full-access` 三档决定"能写到哪"：
        只读档连工作区都不许写、命令以无写授权令牌运行；工作区外写一律只按逐条授权。
        Windows 各档位均约束命令写入；可写档位允许工作区、专属临时目录及已授权目录。
        """
        return service.set_external_paths_mode(payload.mode)

    @router.post(
        "/permissions/grants",
        response_model=PermissionsView,
        responses=_PERMISSION_WRITE_RESPONSES,
        summary="新增外部路径授权",
    )
    def grant_external_path(payload: ExternalPathGrantRequest) -> PermissionsView:
        """为外部目录添加 `read_write` 写授权（覆盖其子目录）；只读档仍禁止写入。

        三档均允许显式本机外部路径的读取、列目录与搜索，敏感目标除外。
        `read` 保留旧记录兼容，不再扩大读取权限。写授权不代替覆盖、删除等逐次审批。

        同一个目标重复授权是**改写**（刷新访问级别），不是加两行；返回授权后的完整视图，
        让设置页直接渲染，不用再取一次。
        """
        return service.grant_external_path(payload.path, payload.access)

    @router.delete(
        "/permissions/grants",
        response_model=PermissionsView,
        responses=_PERMISSION_WRITE_RESPONSES,
        summary="撤销外部路径授权",
    )
    def revoke_external_path(
        path: str = Query(min_length=1, max_length=PATH_MAX_CHARS),
    ) -> PermissionsView:
        """移除目录记录；撤销写授权不影响默认读取，移除旧 `read` 记录也不禁止读取。

        **不存在也算成功**（幂等），返回撤销后的完整视图。

        路径走查询参数而不是请求体：它是这条记录的自然键（含层级分隔符），放 URL 里可以正常
        URL 编码；重复调用同一条不会产生第二种结果。
        """
        return service.revoke_external_path(path)

    # ---- 联网检索的服务凭据（Tavily）--------------------------------------------
    # 与模型连接的密钥同一条口径（§6.8）：**只报"有没有"，不回读**；密钥只进设置库或进程内存。
    # 数值型偏好（条数上限、域名策略等）走 `/preferences`，这里只有密钥这一件事。

    @router.get("/web-search/credential", response_model=WebSearchKeyView, summary="联网检索凭据状态")
    def get_web_search_credential(
        provider: Literal["tavily", "deepseek"] = "tavily",
    ) -> WebSearchKeyView:
        """当前有没有可用的检索服务凭据，以及它来自设置库还是旧环境变量。

        ``provider`` 缺省 ``tavily``（升级前只有这一个后端）：旧页面不传参时语义不变。
        两个后端**各占一条密钥**，读哪条由这个参数决定。

        返回值里**没有密钥**（连掩码都没有）：页面只需要知道"已配置 / 未配置"就能决定是提示录入
        还是提示可以开始检索。
        """
        return service.web_search_key_view(provider)

    @router.put(
        "/web-search/credential",
        response_model=WebSearchKeyView,
        responses=_WEB_CREDENTIAL_WRITE_RESPONSES,
        summary="保存联网检索凭据",
    )
    def set_web_search_credential(
        payload: WebSearchKeyRequest,
        provider: Literal["tavily", "deepseek"] = "tavily",
    ) -> WebSearchKeyView:
        """写入（覆盖）指定后端的密钥：**保存即生效**，下一轮检索就用它。

        空白密钥被拒（422）："清空"用 DELETE 表达，两种意图不该共用一个入口。
        """
        return service.save_web_search_key(payload.api_key, provider)

    @router.delete(
        "/web-search/credential",
        response_model=WebSearchKeyView,
        responses=_WEB_CREDENTIAL_WRITE_RESPONSES,
        summary="删除联网检索凭据",
    )
    def delete_web_search_credential(
        provider: Literal["tavily", "deepseek"] = "tavily",
    ) -> WebSearchKeyView:
        """删除指定后端已保存的密钥；**不存在也算成功**（幂等）。

        删掉之后旧环境变量仍然有效——如实报告由返回的 ``source`` 承担，本条路径不假装
        "删了就一定没有凭据了"。
        """
        return service.clear_web_search_key(provider)

    @router.get("/preferences", response_model=PreferencesView, summary="应用偏好与配置概览")
    def get_preferences() -> PreferencesView:
        return service.preferences_view()

    @router.get(
        "/preferences/schema",
        response_model=PreferencesSchemaView,
        summary="设置页可改字段清单",
    )
    def get_preferences_schema() -> PreferencesSchemaView:
        """后端管得着的偏好字段、各自的输入类型与上下界。

        与 `GET /preferences` 的分工：那个回答"现在是什么值"（随保存变化），这个回答
        "能改哪些、各是什么形状"（只随代码变化）。混在一起会让元信息跟着每次保存抖动。

        纯反射、不读运行时状态，所以直接在这里调用而不经 `service`——没有可注入的东西。

        用途：前端测试拿它比对那份手写的字段表，**漏接一个字段就红**。此前缺这个机制，
        后端加了可改字段、页面上没有输入框，测试仍然全绿。
        """
        return preferences_schema()

    @router.get("/storage", response_model=StorageView, summary="数据位置")
    def get_storage_location() -> StorageView:
        """所有 SQLite、向量库、附件与日志的根目录，以及这个值**由谁决定**。

        与 `/preferences` 分开是刻意的：数据根**不能**存在设置库里（设置库自己就在它下面），
        改完必须重启；而偏好的语义是"下一次新任务生效"。两者混在一起会让"什么时候生效"说不清。
        """
        return service.storage_location()

    @router.put(
        "/storage",
        response_model=StorageView,
        responses={422: {"model": ErrorResponse, "description": "路径不合法、不可写，或当前由环境变量决定"}},
        summary="修改数据位置",
    )
    def set_storage_location(payload: StorageLocationRequest) -> StorageView:
        """改到指定目录：**重启后才生效**，且新位置是空的——旧数据不会自动搬过去。

        路径在这里校验（绝对、能创建、能写），不等到启动：启动才失败意味着服务起不来。
        """
        return service.save_storage_location(payload.data_root)

    @router.delete(
        "/storage",
        response_model=StorageView,
        responses={422: {"model": ErrorResponse, "description": "无法恢复默认位置"}},
        summary="恢复默认数据位置",
    )
    def delete_storage_location() -> StorageView:
        """删掉指针文件，回到平台标准目录；同样要重启才生效。"""
        return service.reset_storage_location()

    @router.put("/preferences", response_model=PreferencesView, summary="保存应用偏好", responses={
        409: {"model": ErrorResponse, "description": "设置版本已更新，草稿未覆盖现有设置"},
        503: {"model": ErrorResponse, "description": "设置库不可用"},
    })
    def put_preferences(payload: PreferencesRequest) -> PreferencesView:
        return service.save_preferences(payload)

    @router.post("/preferences/memory/test", response_model=MemoryProbeResult,
                 summary="测试 Embedding 与已有向量集合（不保存草稿、不写入记忆）",
                 responses={409: {"model": ErrorResponse, "description": "设置版本已更新"}})
    def test_memory_preferences(payload: MemoryProbeRequest) -> MemoryProbeResult:
        return service.test_memory_preferences(payload)

    return router
