"""HTTP 请求与响应形状（T29；契约 §6.2、§15.1）。

请求模型**只描述传输层**：`decision` / `edited_arguments` / `reason` 原样透传给应用服务，
因为"什么算合法决定"的唯一裁定权在服务里（`APPROVAL_DECISIONS`）；本层只挡下**形状**问题
（未知字段、空输入、edit 缺参数），它们返回 422，而不是业务错误 DTO。

响应模型一律是 T27 的 DTO（`application/models.py`），本层不新增业务字段——前端类型从
OpenAPI 生成，多一个私有字段就等于在契约里加了一条 UI 私有通道（§15.1 明令禁止）。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from local_agent.application.errors import ErrorView
from local_agent.application.models import (
    SCHEMA_VERSION,
    ExternalPathAccess,
    ExternalPathsMode,
)
from local_agent.attachments import MAX_ATTACHMENTS_PER_MESSAGE
from local_agent.workflow_contracts import WorkflowRequest

#: 会话名与请求标识的长度上限：够用即可，真正的截断规则在会话库里。
TITLE_MAX_CHARS = 1000
REQUEST_ID_MAX_CHARS = 128
MESSAGE_MAX_CHARS = 100_000
PATH_MAX_CHARS = 4096
REASON_MAX_CHARS = 2000
#: 服务凭据（Tavily）的长度上限：常见密钥是几十个字符，512 只用来挡住"粘进来一整篇文档"。
KEY_MAX_CHARS = 512


class _Request(BaseModel):
    """请求基类：未知字段**拒绝**（打错字段名不能静默忽略）。"""

    model_config = ConfigDict(extra="forbid")


class RenameRequest(_Request):
    """会话与工作区的显示名称；不改变目录名或会话绑定。"""

    title: str = Field(min_length=1, max_length=60)

    @field_validator("title")
    @classmethod
    def _nonblank(cls, value: str) -> str:
        title = " ".join(value.split())
        if not title:
            raise ValueError("名称不能为空")
        return title


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
    mode: Literal["chat", "agent"] = "agent"


class SessionModeRequest(_Request):
    mode: Literal["chat", "agent"]
    expected_mode: Literal["chat", "agent"]


class RecoveryRequest(_Request):
    request_id: str = Field(min_length=1, max_length=REQUEST_ID_MAX_CHARS)
    expected_snapshot_id: str = Field(min_length=1, max_length=REQUEST_ID_MAX_CHARS)
    confirm_reviewed: Literal[True]


class CancelTaskRequest(_Request):
    """取消当前会话在跑的任务（架构 §15.2、§17.4）。**幂等**：没有可取消的任务时是空操作。

    刻意**不带** `client_request_id`：取消不进幂等台账（重复调用本来就是空操作），
    要求一个用不上的幂等键只会让调用方以为"取消也会被去重"。
    """

    reason: str = Field(default="", max_length=200)


class SubmitMessageRequest(_Request):
    """提交消息。`client_request_id` 是幂等键（§6.1），由前端生成并复用。"""

    text: str = Field(default="", max_length=MESSAGE_MAX_CHARS)
    client_request_id: str = Field(min_length=1, max_length=REQUEST_ID_MAX_CHARS)
    #: 随这条消息一起交给模型的图片（2026-10-16）：**引用**（上传接口返回的 `attachment_id`），
    #: 不是字节——字节留在 `data/attachments/` 里，消息里只放能定位它的相对路径。
    #: 默认空 ⇒ 老客户端照常工作；上限与存储层同一条口径（`MAX_ATTACHMENTS_PER_MESSAGE`）。
    attachments: list[str] = Field(
        default_factory=list, max_length=MAX_ATTACHMENTS_PER_MESSAGE
    )
    workflow: WorkflowRequest | None = None
    parent_turn_id: str | None = Field(default=None, min_length=1, max_length=REQUEST_ID_MAX_CHARS)
    expected_branch_id: str | None = Field(default=None, min_length=1, max_length=REQUEST_ID_MAX_CHARS)
    expected_snapshot_id: str | None = Field(default=None, min_length=1, max_length=REQUEST_ID_MAX_CHARS)

    @model_validator(mode="after")
    def _text_or_attachments(self) -> SubmitMessageRequest:
        """文字与图片**至少有一个**（2026-10-16）。

        `text` 因此允许为空：只贴一张图、一个字不打也是合法提问（"看看这是什么"）。但不能两者都空：
        那样提交的是一条**什么都不含**的消息，模型没有可回应的内容，用户也看不出自己漏了什么。
        """
        if not self.text.strip() and not self.attachments:
            msg = "text 与 attachments 至少要有一个"
            raise ValueError(msg)
        return self


class ArrangeBranchRequest(_Request):
    title: str = Field(min_length=1, max_length=80)
    position: int = Field(ge=0)


class CreateSideChatRequest(_Request):
    """开侧聊（§21.7）：只传`从哪一轮开出`，上下文由服务端读。"""

    #: 缺省表示取父会话的最新一轮。
    origin_turn_id: str | None = Field(default=None, max_length=64)


class ResolveTaskReviewRequest(_Request):
    """人工核对结案（§17.1 唯一例外）。"""

    #: `completed`（核对后视为已完成）或 `cancelled`（确认没做完 / 放弃）。
    outcome: Literal["completed", "cancelled"]


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


class AttachmentUploadRequest(_Request):
    """上传一张图片（2026-10-16）。

    为什么是 **base64 的 JSON** 而不是 multipart：后者要给运行时（以及打包）多加一个
    `python-multipart` 依赖，而 4 MB 上限下 base64 的 33% 开销无所谓；契约与生成类型也干净。

    **没有 media_type 字段**是刻意的：客户端声明与扩展名一样不可信（`报告.png` 完全可能是 HTML），
    媒体类型一律由服务端按**魔数**判定。
    """

    #: 图片字节的 base64（标准编码，带 padding）。上限按"解码后 ≤ 4 MB"卡，
    #: 这里给一个略大的字符上限，免得把超大请求体先读进内存再拒绝。
    data_base64: str = Field(min_length=1)


class OssCredentialRequest(_Request):
    """对象存储的访问密钥（2026-10-20）。

    形状只挡空值与超长；内容不做任何校验（与 Tavily 同一条口径）。写入即整体覆盖，
    页面**不回读**：只回答"已配置 / 未配置"。
    """

    access_key: str = Field(min_length=1, max_length=KEY_MAX_CHARS)
    secret_key: str = Field(min_length=1, max_length=KEY_MAX_CHARS)


class OssCredentialView(BaseModel):
    """对象存储密钥状态：只报有没有，不回读任何一位密钥。"""

    configured: bool
    store: Literal["os", "local", "memory"] | None = None
    note: str | None = None


class OssTestResult(BaseModel):
    """「测试对象存储」的结果：能否写入、公开地址能否访问。"""

    ok: bool
    detail: str


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
    #: 用户声明：**这条连接支持图片输入**（2026-10-16）。默认关（失败闭合）——勾错会让每次带图的
    #: 请求失败，所以页面上必须写清"不确定就别勾"。它是**声明**，与 `capabilities` 那组**实测报告**
    #: 分开：收不收图只能靠发图试，测不出来与不支持是两回事。
    supports_vision: bool = False
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
    #: `local` = 持久，密钥在**本地设置库**里（2026-10-13 起的默认，§6.8）；
    #: `os` = 历史值（系统凭据存储，v1.0 之前默认，现无代码路径产出）；
    #: `memory` = 仅本进程内有效（重启后需重新录入）。
    credential_store: Literal["os", "local", "memory"] = "memory"
    #: 降级原因；持久存储（`local` / `os`）时为 `null`。
    credential_note: str | None = None
    #: 只说到"本地库"这一层：具体的后端标识（配置里的 `sqlite_checkpoint`）属内部细节，
    #: 不该固化进公开契约（前端也不需要）。
    storage: Literal["sqlite"] = "sqlite"
    static_frontend: bool


# ---- 应用身份与维护（2026-10-27，设置页「关于」）--------------------------


class BuildInfoView(BaseModel):
    """`BUILD_INFO.txt` 的摘要：打包机写入，让"这个包对应哪份代码"可以复核。"""

    built_at: str
    revision: str
    dirty: str
    python: str


class AppInfoView(BaseModel):
    """「关于」页要显示的**本机事实**。

    路径是绝对路径（与 `PickDirectoryView` 同一口径）：这一页就是给用户核对"装在哪儿、
    数据在哪儿"用的，隐藏路径等于把最有用的信息删掉。注意它与 `/health` 的约束不同——
    那里要公共可用性摘要，这里只给本机设置页看。
    """

    version: str
    #: `installed` 安装版 / `portable` 绿色包 / `source` 源码。
    install_kind: Literal["installed", "portable", "source"]
    #: 源码形态为 `null`（没有"安装目录"这回事）。
    install_dir: str | None = None
    data_root: str
    data_root_source: Literal["environment", "settings", "config", "default"]
    build: BuildInfoView | None = None
    #: 卸载入口可不可点；不可点时 `uninstall_note` 必须说明下一步怎么做。
    uninstall_available: bool
    uninstall_note: str | None = None
    #: 「同时删除数据」勾选框能不能勾；不能时 `remove_data_note` 说明原因。
    can_remove_data: bool
    remove_data_note: str | None = None


class UpdateCheckView(BaseModel):
    """一次检查更新的结果。**查不到不算错误**：`unavailable` + `note` 就是完整答复。"""

    status: Literal["update_available", "up_to_date", "unavailable"]
    current_version: str
    latest_version: str | None = None
    #: GitHub Release 页面（不是 API 地址）：前端直接交给系统浏览器打开。
    release_url: str | None = None
    published_at: str | None = None
    note: str | None = None


class UninstallRequest(_Request):
    """卸载请求。

    `remove_data=False` 是默认，也是**老约定**（`packaging/README.md` 的"用户数据不归安装包管"）：
    传 `true` 是用户在弹窗里显式勾选过"同时删除数据"，不是服务端替他决定。
    """

    remove_data: bool = False


class UninstallView(BaseModel):
    """卸载已交出去（202 + `scheduled=true`）：进程随后会被脚本结束，前端不该再发请求。"""

    scheduled: bool
    remove_data: bool
    data_dir: str
    message: str


class PickDirectoryView(BaseModel):
    """本机目录选择器的结果：`path` 是选中的**绝对路径**，用户取消时为 `null`。

    刻意**不做成业务 DTO**（不放 `application/models.py`）：弹一个原生窗口是宿主进程的能力，
    不是应用服务的用例——应用服务只认"按路径登记工作区"，对"路径怎么来的"没有意见。

    取消用 `200 + null` 表达而不是错误：取消是正常操作，不该被读成失败、也不该改变任何状态。
    """

    path: str | None = None


# ---- 能力开关（§16.1、§16.6）------------------------------------------------


class CapabilitiesRequest(_Request):
    """改能力开关（§16.1、§16.6）。

    只报告**这次动过的那几项**（合并式）：页面点一下开关就发一项，其余保持原样。取值是否合法由领域层
    裁定（未登记的能力名给 422，理由里带上可开关集）——传输层不另立一套能力名，与
    `PermissionsModeRequest` 同一条口径。
    """

    changes: dict[str, bool] = Field(min_length=1)


# ---- MCP（2026-10-20；契约 §6.13）------------------------------------------


class McpServerRequest(_Request):
    """改一台 MCP Server 的开关或工具名单。

    只带**本次动过的键**：省略 ``enabled_tools`` 表示"不改它"，显式传 ``null`` 表示"取消白名单"
    （两条语义不同，所以服务层按"有没有这个键"判断，不靠默认值）。
    """

    enabled: bool | None = None
    enabled_tools: list[str] | None = None
    disabled_tools: list[str] | None = None


class McpServerCreateRequest(_Request):
    """新增一台 MCP Server（只支持 stdio）。

    ``env`` 允许传——否则需要凭据的 server（cookie / token）根本配不出来。但它**只写不回读**：
    响应里的 `McpView` 没有任何 env 字段，日志也不记（与模型连接密钥同一条口径）。
    """

    name: str = Field(min_length=1)
    command: str = Field(min_length=1)
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)


class McpServerSpecRequest(_Request):
    """替换连接方式；省略字段保持原值，env 按键合并且不回读。"""

    command: str | None = Field(default=None, min_length=1)
    args: list[str] | None = None
    env: dict[str, str] = Field(default_factory=dict)
    remove_env: list[str] = Field(default_factory=list)


# ---- 技能（技能管理页，2026-10-16；§16.5）----------------------------------


class SkillRootRequest(_Request):
    """添加一个技能发现根（2026-10-20）。

    只收**绝对路径**：相对路径的基准在冻结形态是数据目录、在源码形态是配置文件目录，
    让用户在这里猜基准只会得到"路径明明对却什么都扫不到"。`~` 由页面展开后再提交。
    """

    path: str = Field(min_length=1, max_length=TITLE_MAX_CHARS)


class SkillToggleRequest(_Request):
    """启用/禁用一个技能（技能管理页，2026-10-16；§16.5）。

    **一次一个**：界面就是一个开关，报一批既没有对应操作，也会让"哪个名字错了"变含混。
    名字是否真有这个技能由领域层裁定（422，理由里带上当前可用的名字）。
    """

    name: str = Field(min_length=1)
    enabled: bool


# ---- 权限（T38；§1.5）------------------------------------------------------


class PermissionsModeRequest(_Request):
    """切换"工作区外路径"档位（T38）。

    档位取值与领域层同源（`ExternalPathsMode`），传输层不另立一套字符串——两处各写一份迟早会
    漂移。这里的 `extra="forbid"` 保证打错档位名得到 422，而不是静默保存成别的档位。
    """

    mode: ExternalPathsMode


class StorageLocationRequest(_Request):
    """把数据根改到指定目录（2026-10-26）。

    这里只挡空值：绝对路径、可创建、可写这些**文件系统层面**的判断在服务里做（要真的去建目录
    并写探针），放在传输层会把 I/O 塞进校验器。非法路径返回 422，文案由服务给出。
    """

    data_root: str = Field(min_length=1, max_length=1024)


class WebSearchKeyRequest(_Request):
    """录入 / 覆盖联网检索的服务凭据（哪一条由 query 的 ``provider`` 决定：Tavily / DeepSeek）。

    形状只挡空值：**内容不在这里做任何校验**（长度上限只是防"把整篇文档粘进来"），
    密钥对不对只有发起一次真实检索才知道——那件事由用户自己决定何时做。
    """

    api_key: str = Field(min_length=1, max_length=KEY_MAX_CHARS)


class ExternalPathGrantRequest(_Request):
    """新增 / 改写一条外部路径授权（T38）。

    只校验**形状**（非空、长度上限）：路径的规范化（大小写、分隔符、符号链接）与"是不是受保护
    位置"都在服务层裁定——那需要真实文件系统，不是传输层能替它做主的。
    `access` 只允许 `read` / `read_write`："不给访问"用撤销表达，不另立一个级别。
    """

    path: str = Field(min_length=1, max_length=PATH_MAX_CHARS)
    access: ExternalPathAccess
