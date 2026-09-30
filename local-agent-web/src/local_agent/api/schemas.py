"""HTTP 请求与响应形状（T29；契约 §6.2、§15.1）。

请求模型**只描述传输层**：`decision` / `edited_arguments` / `reason` 原样透传给应用服务，
因为"什么算合法决定"的唯一裁定权在服务里（`APPROVAL_DECISIONS`）；本层只挡下**形状**问题
（未知字段、空输入、edit 缺参数），它们返回 422，而不是业务错误 DTO。

响应模型一律是 T27 的 DTO（`application/models.py`），本层不新增业务字段——前端类型从
OpenAPI 生成，多一个私有字段就等于在契约里加了一条 UI 私有通道（§15.1 明令禁止）。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from local_agent.application.errors import ErrorView
from local_agent.application.models import (
    SCHEMA_VERSION,
    ExternalPathAccess,
    ExternalPathsMode,
)

#: 会话名与请求标识的长度上限：够用即可，真正的截断规则在会话库里。
TITLE_MAX_CHARS = 1000
REQUEST_ID_MAX_CHARS = 128
MESSAGE_MAX_CHARS = 100_000
PATH_MAX_CHARS = 4096
REASON_MAX_CHARS = 2000


class _Request(BaseModel):
    """请求基类：未知字段**拒绝**（打错字段名不能静默忽略）。"""

    model_config = ConfigDict(extra="forbid")


class SelectWorkspaceRequest(_Request):
    """选择工作区：`workspace_id`（已列出的工作区）与 `path`（新目录）二选一。"""

    workspace_id: str | None = Field(default=None, min_length=1, max_length=REQUEST_ID_MAX_CHARS)
    path: str | None = Field(default=None, min_length=1, max_length=PATH_MAX_CHARS)

    @model_validator(mode="after")
    def _exactly_one(self) -> SelectWorkspaceRequest:
        if (self.workspace_id is None) == (self.path is None):
            msg = "workspace_id 与 path 必须二选一"
            raise ValueError(msg)
        return self


class CreateSessionRequest(_Request):
    """新建会话；两项都可以缺省（沿用库的启动默认工作区、自动命名）。"""

    title: str | None = Field(default=None, max_length=TITLE_MAX_CHARS)
    workspace_id: str | None = Field(default=None, min_length=1, max_length=REQUEST_ID_MAX_CHARS)


class SubmitMessageRequest(_Request):
    """提交消息。`client_request_id` 是幂等键（§6.1），由前端生成并复用。"""

    text: str = Field(min_length=1, max_length=MESSAGE_MAX_CHARS)
    client_request_id: str = Field(min_length=1, max_length=REQUEST_ID_MAX_CHARS)

    @model_validator(mode="after")
    def _text_is_not_blank(self) -> SubmitMessageRequest:
        if not self.text.strip():
            msg = "text 不能只有空白字符"
            raise ValueError(msg)
        return self


class ApprovalDecisionRequest(_Request):
    """审批决定。

    `edit` 必须带 `edited_arguments`（否则服务不知道该执行什么）；反过来，非 `edit` 带参数
    会被拒绝——那会让"用户改了参数"与"实际执行的参数"出现两份不一致的真相（§15.7）。
    """

    decision: Literal["approve", "edit", "reject"]
    client_request_id: str = Field(min_length=1, max_length=REQUEST_ID_MAX_CHARS)
    edited_arguments: dict[str, Any] | None = None
    reason: str = Field(default="", max_length=REASON_MAX_CHARS)
    confirm_recursive: bool = False

    @model_validator(mode="after")
    def _edit_arguments_match_the_decision(self) -> ApprovalDecisionRequest:
        if self.decision == "edit" and not self.edited_arguments:
            msg = "decision=edit 必须给出 edited_arguments"
            raise ValueError(msg)
        if self.decision != "edit" and self.edited_arguments:
            msg = "只有 decision=edit 才能带 edited_arguments"
            raise ValueError(msg)
        return self


CONNECTION_NAME_MAX_CHARS = 60
MODEL_ID_MAX_CHARS = 200
SECRET_MAX_CHARS = 4096


class ModelConnectionRequest(_Request):
    """新增 / 编辑一条模型连接（§6.8）。

    请求体**只描述连接字段**：没有 `credential_ref`（引用由服务端生成）、没有连接 id
    （路径里给）、也没有任何能改安全策略、工具权限或 Graph 配置的入口——模型设置不是提权通道。
    `api_key` 是"本次录入的密钥"，不填表示不改密钥（编辑时不重填不会把已保存的 Key 抹掉）。
    """

    display_name: str = Field(min_length=1, max_length=CONNECTION_NAME_MAX_CHARS)
    base_url: str = Field(min_length=1, max_length=PATH_MAX_CHARS)
    model: str = Field(min_length=1, max_length=MODEL_ID_MAX_CHARS)
    #: 上下文窗口**必填**：§6.8 要求由用户填写或由可信检测确认，不能猜。
    context_window_tokens: int = Field(gt=0)
    max_tokens: int = Field(default=4096, gt=0)
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    timeout_seconds: int = Field(default=120, gt=0)
    retry_count: int = Field(default=2, ge=0, le=10)
    streaming: bool = False
    auth_mode: Literal["api_key", "none"] = "api_key"
    #: 用户对"非回环明文 HTTP"的明确确认（§6.8 第 5 条）。
    allow_plaintext_http: bool = False
    api_key: str | None = Field(default=None, max_length=SECRET_MAX_CHARS)
    #: 客户端手上的版本号：**编辑必填**（防两个页面互相覆盖，§6.1）。
    expected_version: int | None = Field(default=None, ge=1)


class SelectionExpectation(_Request):
    """调用方**看到的当前选择**，用于选择写入的乐观校验（§6.1）。

    `connection_id=None` 表示"我当时看到这个作用域没有选择"（全新安装还没有默认，
    或某个会话跟随默认）。两个字段要么都给、要么都给 `None`——只给一半说明调用方自己没想清楚。
    """

    connection_id: str | None = Field(min_length=1, max_length=REQUEST_ID_MAX_CHARS)
    config_version: int | None = Field(ge=1)

    @model_validator(mode="after")
    def matching_fields(self) -> SelectionExpectation:
        if (self.connection_id is None) != (self.config_version is None):
            raise ValueError("connection_id 与 config_version 要么都给值、要么都给 null")
        return self


class SelectDefaultConnectionRequest(_Request):
    """设置新会话继承的默认连接。

    `expected_selection` **必填**：不填就等于"我不管别人改了什么，覆盖掉"，
    那正是 §6.1 要避免的两个页面互相覆盖。
    """

    connection_id: str = Field(min_length=1, max_length=REQUEST_ID_MAX_CHARS)
    expected_selection: SelectionExpectation
    #: 目标连接你看到的版本；不符说明这条连接在你选之前已被改过。
    expected_version: int = Field(ge=1)


class SelectSessionModelRequest(_Request):
    """为某个会话选择**下一轮**用的连接；`connection_id=None` 表示恢复为跟随默认。

    `expected_selection` 对"恢复跟随默认"同样是必填的（那也是一次写入）。
    """

    connection_id: str | None = Field(default=None, min_length=1, max_length=REQUEST_ID_MAX_CHARS)
    expected_selection: SelectionExpectation
    expected_version: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def target_version_matches_connection(self) -> SelectSessionModelRequest:
        if (self.connection_id is None) != (self.expected_version is None):
            raise ValueError("选中连接时必须给目标版本；恢复跟随默认时不应给目标版本")
        return self


class ErrorResponse(BaseModel):
    """统一错误信封：业务错误一律 `{"error": ErrorView}`（§15.5）。

    响应体刻意**不是** `{"detail": ...}`：那会和 FastAPI 自己的 422/403 撞形状，前端就分不清
    "业务错误"和"传输层拒绝"了。
    """

    error: ErrorView


class HealthView(BaseModel):
    """健康检查：只说可用性，不含路径、密钥或内部标识（§15.8）。

    - `model_configured=false` 是初始设置模式的正常状态（§15.9）；
    - `credential_store` 让**设置页**在还没有任何连接时也能说清"密钥重启后还在不在"
      （§6.8 要求凭据存储不可用时必须明确提示，而不是等用户录完密钥才发现）。
    """

    status: Literal["ok"] = "ok"
    schema_version: int = SCHEMA_VERSION
    model_configured: bool
    model_name: str
    #: `os` = 持久（重启后仍在）；`memory` = 仅本进程内有效。
    credential_store: Literal["os", "memory"] = "memory"
    #: 降级原因；`credential_store="os"` 时为 `null`。
    credential_note: str | None = None
    #: 只说到"本地库"这一层：具体的后端标识（配置里的 `sqlite_checkpoint`）属内部细节，
    #: 不该固化进公开契约（前端也不需要）。
    storage: Literal["sqlite"] = "sqlite"
    static_frontend: bool


class PickDirectoryView(BaseModel):
    """本机目录选择器的结果：`path` 是选中的**绝对路径**，用户取消时为 `null`。

    刻意**不做成业务 DTO**（不放 `application/models.py`）：弹一个原生窗口是宿主进程的能力，
    不是应用服务的用例——应用服务只认"按路径登记工作区"，对"路径怎么来的"没有意见。

    取消用 `200 + null` 表达而不是错误：取消是正常操作，不该被读成失败、也不该改变任何状态。
    """

    path: str | None = None


# ---- 权限（T38；§1.5）------------------------------------------------------


class PermissionsModeRequest(_Request):
    """切换"工作区外路径"档位（T38）。

    档位取值与领域层同源（`ExternalPathsMode`），传输层不另立一套字符串——两处各写一份迟早会
    漂移。这里的 `extra="forbid"` 保证打错档位名得到 422，而不是静默保存成别的档位。
    """

    mode: ExternalPathsMode


class ExternalPathGrantRequest(_Request):
    """新增 / 改写一条外部路径授权（T38）。

    只校验**形状**（非空、长度上限）：路径的规范化（大小写、分隔符、符号链接）与"是不是受保护
    位置"都在服务层裁定——那需要真实文件系统，不是传输层能替它做主的。
    `access` 只允许 `read` / `read_write`："不给访问"用撤销表达，不另立一个级别。
    """

    path: str = Field(min_length=1, max_length=PATH_MAX_CHARS)
    access: ExternalPathAccess
