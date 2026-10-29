"""UI/API 的唯一业务契约来源（ARCHITECTURE §15.4–§15.9）。

本模块只放**纯数据契约**：不导入 LangChain 消息类、LangGraph、`local_agent.graph` 或 `local_agent.ui`，
不做 I/O，不读 Checkpoint。把 State / Checkpoint 映射成这些 DTO 是 T28 的事。

几条贯穿全模块的硬规则：

- **不暴露内部类型**：DTO 里不出现 `AnyMessage`、`AgentState`、节点名或不可序列化对象；
  参数一律是**已脱敏的展示字符串**（`dict[str, str]`），嵌套结构由投影层渲染成字符串。
- **未知字段前向兼容**：所有 DTO `extra="ignore"`，旧/新版本多出来的字段既不会报错也不会被回显。
- **不确定就不写确定值**：供应商没返回的缓存字段是 `None`，不是 `0`；窗口未确认时比例是 `None`。
- **可见性矩阵（§15.8）**：`RuntimeContextSnapshotView` **不含** `canonical_text`——页面可以显示"存在哪个
  版本的上下文快照及其来源"，但不能显示提示词正文。
"""

from __future__ import annotations

import ipaddress
import json
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Annotated, Final, Literal, get_args
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from local_agent.application.errors import ErrorView
from local_agent.capabilities import CapabilityReason

# 只借**任务状态的类型别名**（叶子模块，与索引闭集同源）：契约模块不得依赖持久化层，见该模块
# docstring 与 `tests/test_events.py::test_pure_contract_modules_do_not_import_runtime_layers`。
from local_agent.job_state import JobState

#: 契约版本。任何破坏性改名都要提升它，并在 API 层显式声明。
SCHEMA_VERSION: Final[int] = 1


def _require_timezone(value: datetime) -> datetime:
    if value.tzinfo is None:
        msg = "时间字段必须带时区（使用 timezone.utc）"
        raise ValueError(msg)
    return value.astimezone(timezone.utc)


def canonical_json(model: BaseModel) -> str:
    """确定性序列化：同一输入必须得到**逐字节相同**的结果。

    排序键、无多余空白、`ensure_ascii=False`，因此中文正文不会因转义方式不同而产生两份"同一"快照。
    """
    payload = model.model_dump(mode="json")
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class _Dto(BaseModel):
    """所有公开 DTO 的公共配置。"""

    model_config = ConfigDict(extra="ignore")


# ---- 引用 ------------------------------------------------------------------


class WorkspaceRef(_Dto):
    """工作区引用。

    **不含绝对路径**：`workspace_id` 是规范化工作区路径的稳定哈希，不是目录名；
    页面可凭它选回列表中的工作区，新工作区仍通过 `select_workspace(path)` 添加。
    路径本身可能暴露私人目录，不进入读 DTO（§15.8）。
    """

    workspace_id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)


class TaskRef(_Dto):
    """任务引用。v0.4 一次用户提交就是一个任务；`task_id` 与 `run_id` 复用同一底层标识。"""

    task_id: str | None = Field(default=None, min_length=1, exclude=True)
    run_id: str = Field(min_length=1)


# ---- 模型连接（§15.9）-----------------------------------------------------


CapabilityResult = Literal["passed", "failed", "unknown"]
CapabilityName = Literal["chat", "tool_calling", "streaming"]

CAPABILITY_RESULTS: Final[tuple[CapabilityResult, ...]] = get_args(CapabilityResult)
CAPABILITY_NAMES: Final[tuple[CapabilityName, ...]] = get_args(CapabilityName)

ConnectionStatus = Literal["unverified", "testing", "verified", "failed", "unsupported"]
ConnectionTransport = Literal["https", "loopback_http", "plaintext_http"]
KeyStatus = Literal["configured", "missing", "session_only"]


class CapabilityCheckView(_Dto):
    """单项能力检测结果。未检测就是 `unknown`，不能写成 `passed`。"""

    capability: CapabilityName
    result: CapabilityResult


class AttachmentView(_Dto):
    """一张已上传图片的**引用**（2026-10-16）。

    `attachment_id` 是**引用**：本地后端是相对路径（`2026/10/16/<会话>/<内容哈希>.png`），
    对象存储后端是**公开 URL**。取回内容都走
    `GET /api/sessions/{id}/attachments?attachment_id=…`（对象存储会 302 到公开地址）。

    `url` 只有对象存储后端才给（页面可以直接用它，不必再走一次接口）。

    **没有文件名、没有磁盘路径、没有原始字节**：页面要的是"能显示、能引用"，不是一份文件管理器。
    """

    attachment_id: str = Field(min_length=1)
    #: 服务端按**魔数**判定的类型（`image/png` / `image/jpeg` / `image/gif` / `image/webp`）。
    media_type: str = Field(min_length=1)
    byte_count: int = Field(gt=0)
    #: 对象存储后端的公开地址；本地后端为 `None`（2026-10-20）。
    url: str | None = None


class ModelConnectionView(_Dto):
    """聊天模型连接的**读** DTO。

    密钥只以 `key_status` 出现（已配置 / 未配置 / 仅本次进程内）：本 DTO **没有**任何承载密钥值的字段，
    因此即使上游多传一个 `api_key`，它也会被 `extra="ignore"` 丢掉。公开 URL
    还必须拒绝内嵌凭据，不能仅靠“没有 api_key 字段”断言安全（§15.9）。
    """

    connection_id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    provider: Literal["openai_compatible"] = "openai_compatible"
    base_url: str = Field(min_length=1)
    transport: ConnectionTransport
    model_id: str = Field(min_length=1)
    context_window_tokens: int = Field(gt=0)
    max_tokens: int = Field(gt=0)
    temperature: float = Field(ge=0, le=2)
    timeout_seconds: int = Field(gt=0)
    retry_count: int = Field(ge=0)
    streaming: bool
    #: 用户声明：这条连接**是否支持图片输入**（2026-10-16）。按**连接**而不是全局——一条不收图的
    #: 连接不该连累另一条。默认 `False` = 失败闭合。
    supports_vision: bool = False
    auth_mode: Literal["api_key", "none"]
    key_status: KeyStatus
    #: 不含明文的凭据引用；`None` 表示该连接不需要密钥或尚未绑定。
    credential_ref: str | None = None
    status: ConnectionStatus
    capabilities: list[CapabilityCheckView] = Field(default_factory=list)
    #: 配置版本；编辑产生新版本，旧版本保留到引用它的运行结束（§15.9）。
    config_version: str = Field(min_length=1)
    is_default: bool = False

    @field_validator("base_url")
    @classmethod
    def _safe_base_url(cls, value: str) -> str:
        # A base URL has no reason to carry a query or fragment. Rejecting both
        # prevents credentials from hiding outside the dedicated secret store.
        if "\\" in value or any(char.isspace() for char in value):
            raise ValueError("模型端点 URL 不能包含反斜杠或空白字符")
        try:
            parsed = urlsplit(value)
            hostname = parsed.hostname
            _ = parsed.port  # Also reject malformed/out-of-range ports.
        except ValueError as exc:
            raise ValueError("模型端点 URL 格式无效") from exc
        if parsed.scheme not in {"http", "https"} or not hostname:
            raise ValueError("模型端点必须是带主机名的 HTTP(S) URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("模型端点 URL 不得内嵌凭据")
        if parsed.query or parsed.fragment or "?" in value or "#" in value:
            raise ValueError("模型端点 URL 不得包含查询参数或片段")
        return value

    @model_validator(mode="after")
    def _transport_matches_url(self) -> ModelConnectionView:
        parsed = urlsplit(self.base_url)
        if parsed.scheme == "https":
            expected: ConnectionTransport = "https"
        else:
            hostname = (parsed.hostname or "").rstrip(".").lower()
            try:
                loopback = ipaddress.ip_address(hostname).is_loopback
            except ValueError:
                loopback = hostname == "localhost"
            expected = "loopback_http" if loopback else "plaintext_http"
        if self.transport != expected:
            raise ValueError("模型端点的 transport 与 URL 不一致")
        return self


# ---- 权限（T38；§1.5）------------------------------------------------------


ExternalPathsMode = Literal["read-only", "workspace-write", "danger-full-access"]
ExternalPathAccess = Literal["read", "read_write"]

EXTERNAL_PATHS_MODES: Final[tuple[ExternalPathsMode, ...]] = get_args(ExternalPathsMode)
EXTERNAL_PATH_ACCESSES: Final[tuple[ExternalPathAccess, ...]] = get_args(ExternalPathAccess)


class ExternalPathGrantView(_Dto):
    """一条"工作区外路径"授权（T38）。

    ``path`` 完整显示，便于用户核对和撤销授权。
    它已经规范化（统一大小写与分隔符、展开符号链接）。
    """

    path: str = Field(min_length=1)
    access: ExternalPathAccess
    created_at: datetime | None = None
    updated_at: datetime | None = None


class PermissionsView(_Dto):
    """「设置 → 权限」的读 DTO（T38）。

    ``mode`` 是当前生效档位（用户的选择优先）；``default_mode`` 是 ``config.yaml`` 的出厂默认，
    用于说明"库里没有用户选择时按哪一档跑"。授权条目决定工作区外目标的访问级别
    （``read-only`` 档把 ``read_write`` 降级为 ``read``）。**写侧沙箱不再是独立开关**：
    各档位在 Windows 上均限制命令写入；可写档位仅授权工作区、专属临时目录与已授权目录。
    """

    mode: ExternalPathsMode
    default_mode: ExternalPathsMode
    grants: list[ExternalPathGrantView] = Field(default_factory=list)
    #: 没有可用设置库时为真：这时改档位与加授权都存不下，界面必须如实说明。
    store_unavailable: bool = False


# ---- 数据位置（2026-10-26）--------------------------------------------------

class StorageView(_Dto):
    """数据根（所有 SQLite、向量库、附件、日志的家）及其来源。

    为什么**不能**存在设置库里：设置库自己就在数据根下。把数据根写进设置库，就等于让一个东西
    定义它自己的位置——改到新位置后新库是空的，那条偏好丢了，启动又回到旧位置。所以它走一个
    固定放在平台默认目录的**指针文件**，与 ``CODEX_HOME`` / ``HERMES_HOME`` 同一性质，
    只是可以在页面上改。
    """

    data_root: str
    #: 平台标准目录（Windows 是 ``%LOCALAPPDATA%\\chaiai``）：「恢复默认」回到这里。
    default_root: str
    #: 谁在决定这个值。``environment`` = 环境变量（页面改不生效，必须如实说明）。
    source: Literal["environment", "settings", "config", "default"]
    #: 刚保存过、还没重启：路径在启动时解析，改完必须重启才生效。
    restart_required: bool = False
    note: str | None = None
    #: 数据根当前占用的字节数（2026-10-26）。算不出来时 ``None``——统计失败不该让整页打不开。
    #:
    #: 为什么要有它：C 盘小的用户装完就开始往满盘写，而"这个目录占了多大、那个盘还剩多少"
    #: 此前只能在资源管理器里自己翻。设置页既然能改位置，就该同时给出改的理由。
    used_bytes: int | None = None
    #: 数据根所在卷的剩余字节数。
    disk_free_bytes: int | None = None
    #: 盘快满了**且用的还是默认位置**（2026-10-26）。
    #:
    #: 只在"默认位置"这一档给 `True`：用户已经主动换过盘，就不该再劝他换一次。
    low_disk: bool = False


# ---- 设置页字段清单（2026-10-26）-------------------------------------------

class PreferenceFieldView(_Dto):
    """一个可改字段的**机械信息**：键、输入类型、上下界、候选项、能否留空。

    文案（``label`` / ``note``）与条件显示（"选了对象存储才出现端点"）**刻意不在这里**：
    它们是产品语义，pydantic 推不出来，只在页面侧维护。这份清单的用途是让"后端加了字段、
    页面没接"变成一次失败的测试，而不是一个靠人记住的约定。
    """

    key: str = Field(min_length=1)
    kind: Literal["bool", "int", "float", "text", "choice"]
    #: 仅 ``choice`` 非空：可选值按声明顺序给出。
    options: tuple[str, ...] = ()
    minimum: float | None = None
    maximum: float | None = None
    #: 边界是否**严格**（``Field(gt=0)``）。整数上"最小 1"不能写成 ``minimum=0``——
    #: 那会让页面允许一个保存必然 422 的值。
    minimum_exclusive: bool = False
    maximum_exclusive: bool = False
    #: 能否留空（``int | None``）：页面对应"留空表示不限"。
    nullable: bool = False


class PreferenceGroupView(_Dto):
    """设置页的一个分区（与左栏分组一一对应）。"""

    group: str = Field(min_length=1)
    fields: tuple[PreferenceFieldView, ...] = ()


class PreferencesSchemaView(_Dto):
    """设置页可改字段的权威清单。

    ``version`` 是**反射格式**的版本，字段增减不需要改它；只有这份清单自身的形状变了才 +1。
    """

    version: int = Field(default=1, ge=1)
    groups: tuple[PreferenceGroupView, ...] = ()


# ---- 联网检索密钥（T-联网设置）---------------------------------------------

#: 凭据存储的对外状态：与 `SettingsView.credential_store.kind` 同一套取值（页面共用一种读法）。
CredentialStoreState = Literal["local", "os", "memory", "unavailable"]


class WebSearchKeyView(_Dto):
    """联网检索的服务凭据（Tavily / DeepSeek）状态。

    **只回答"有没有"，不回读密钥**（§6.8 第 2 条）——与模型连接的密钥同一条口径。

    四个字段回答四个不同的问题，不能合并：
    ``provider`` = 这是**哪个后端**的密钥（两个 API 后端各占一条，页面据此确认读写的是同一格）；
    ``configured`` = 现在能不能构造出检索请求；``source`` = 这份密钥**从哪来**
    （``settings`` 设置库 / ``environment`` 旧环境变量 / ``none`` 都没有）；
    ``store`` = 设置库那边**能不能存下来**（``memory`` 表示只在本进程内有效、重启后要重新录入）。

    取值与 ``config.ApiSearchProvider`` 同源；本模块不导入配置层，所以就地写一份字面量。
    """

    provider: Literal["tavily", "deepseek"] = "tavily"
    configured: bool = False
    source: Literal["settings", "environment", "none"] = "none"
    store: CredentialStoreState = "unavailable"
    note: str | None = None


# ---- 能力状态（v0.5 T56；契约 §16.6）--------------------------------------


class CapabilityStatusView(_Dto):
    """一个能力的对外状态（T56；§16.6）。

    **只有能力与解释，没有工具名**（§15.8 的字段门槛）：用户要判断的是"这台机器现在能不能联网、
    能不能提交"，不是内部绑了哪几个工具。原因取值直接复用 `capabilities.CapabilityReason`，
    页面因此不会遇到"没见过的原因"。
    """

    capability: str = Field(min_length=1)
    label: str = Field(min_length=1)
    available: bool
    reason: CapabilityReason
    detail: str = Field(min_length=1)
    #: 可照做提示；**不可用时必须有**（只灰掉不解释会让用户以为功能坏了）。
    hint: str | None = None
    #: 不可关闭的基础能力（§16.1）：页面据此把开关画成"始终开启"，不给一个点不动的控件。
    locked: bool = False


class CapabilitiesView(_Dto):
    """能力开关表与每个能力的状态（T56；§16.1、§16.6）。

    `switches` 是**解析后的完整开关表**（出厂默认 + 用户改过的项），页面据此画每一项的开关状态；
    `switchable` 是**服务端枚举**的可开关集（= 闭集减去不可关闭的两个），页面照它渲染、不自己拼一份。
    两者一起让页面既能画开关，也能说清"这一项现在到底开没开"。
    """

    #: 能力名 → 是否开启（闭集里每一项都有，含不可关闭的那两个）。
    switches: dict[str, bool] = Field(default_factory=dict)
    #: 可开关的能力（固定顺序）。没有设置库时仍然给出，但 `store_unavailable` 为真、页面不提供开关。
    switchable: list[str] = Field(default_factory=list)
    #: 没有可用设置库：开关只能来自 `config.yaml`，写下去会被拒（503）。
    store_unavailable: bool = False
    capabilities: list[CapabilityStatusView] = Field(default_factory=list)


# ---- MCP 外部工具（2026-10-20；契约 §6.13）--------------------------------


class McpToolView(_Dto):
    """一个外部工具在页面上的样子。

    **这里必须显示工具名**——与能力面板"只讲能力不讲工具"（§16.6）相反。理由是工具名不是内部
    实现细节：它是用户自己配的外部资产，不显示出来就没法核对"我配的这个 server 到底暴露了什么"。
    """

    #: 模型可见名（``mcp__<server>__<tool>``）。
    name: str = Field(min_length=1)
    #: server 报的原始名。
    tool: str = Field(min_length=1)
    description: str = ""
    exposure: Literal["direct", "deferred", "hidden"] = "hidden"
    #: 为什么落在这一档（``ExposureRow.reason`` 的闭集取值）。
    reason: str | None = None
    #: 是否被用户**显式停用**（写进 ``disabled_tools``）。页面据此画勾选状态；
    #: 与 ``exposure`` 分开是因为"停用"是用户可撤销的选择，"超配额"是系统行为。
    disabled: bool = False


class McpServerView(_Dto):
    """一台 MCP Server 的状态与工具清单（**不含任何凭据**）。"""

    name: str = Field(min_length=1)
    env_keys: list[str] = Field(default_factory=list)
    command: str = ""
    args: list[str] = Field(default_factory=list)
    enabled_tools: list[str] | None = None
    disabled_tools: list[str] = Field(default_factory=list)
    enabled: bool = False
    status: Literal["disabled", "idle", "connecting", "connected", "failed"] = "disabled"
    tool_count: int = 0
    tools: list[McpToolView] = Field(default_factory=list)
    failure_kind: str | None = None
    #: 可照做的失败原因（"装 Node"、"看 server 日志"由 ``kind`` 决定）。
    failure_message: str | None = None


class McpSummaryView(_Dto):
    """一行能读懂的规模事实（页面必须明说"多少工具、多少按需检索"）。"""

    configured: int = 0
    enabled: int = 0
    connected: int = 0
    failed: int = 0
    tools: int = 0
    direct: int = 0
    deferred: int = 0


class McpView(_Dto):
    """MCP 面板的全部数据（**每次现算**，与能力面板同一口径）。"""

    #: 能力开关里 ``mcp`` 的当前值（总闸）。
    enabled: bool = False
    #: ``mcp.json`` 的位置。**只给路径不给内容**：文件里可能有凭据。
    config_path: str | None = None
    #: 配置文件解析失败时的可读原因（每次现算，用户改完文件刷新就能看到新结论）。
    config_error: str | None = None
    #: 没有可用设置库：开关只能来自 ``config.yaml``，写下去会被拒（503）。
    store_unavailable: bool = False
    servers: list[McpServerView] = Field(default_factory=list)
    summary: McpSummaryView = Field(default_factory=McpSummaryView)


# ---- 技能（技能管理页，2026-10-16；§16.5）--------------------------------


class SkillEntryView(_Dto):
    """一个技能在设置页上的样子（技能管理页，2026-10-16；契约 §16.5）。

    **不给技能目录的绝对路径**：技能常来自用户主目录（`~/.agents/skills`、`~/.codex/skills`…），
    设置页没有"必须知道主目录结构"的理由；要排查"这个技能从哪来"，`source`（来源层）与
    `diagnostics` 够用。三个布尔各答一件事：`enabled` 是**用户**的选择，
    `model_invocable` / `user_invocable` 是**技能作者**在 frontmatter 里的声明。
    """

    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    #: 发现它的**来源层**展示名（例如"个人级"）——不是路径。
    source: str = Field(min_length=1)
    #: 用户是否允许使用它（技能管理页的开关；**默认可用**，库里只记被禁用的那些）。
    enabled: bool = True
    #: 作者声明：是否允许模型自己调用（`disable-model-invocation: true` 时为假，不进模型清单）。
    model_invocable: bool = True
    #: 作者声明：是否允许用户手动调用（`user-invocable`）。
    user_invocable: bool = True
    #: 技能声明的能力（由 `allowed-tools` 映射而来，**只能收窄本档能力**，§16.5）。
    declared_capabilities: list[str] = Field(default_factory=list)


class SkillRootView(_Dto):
    """一个技能发现根（2026-10-20）。

    页面用它回答"为什么某个技能没出现"：路径、来源层、这个根下发现了几个技能、能不能移除。
    `removable` 只对**用户自己添加**的根为真——随包根与个人级根是产品/约定的一部分，不允许在此删除。
    """

    path: str = Field(min_length=1)
    #: 来源层展示名（例如"随包技能"、"个人级（.agents）"、"自定义根"）。
    source: str = Field(min_length=1)
    #: 这个根下发现了几个技能（0 也要显示：用户据此判断路径是不是填错了）。
    count: int = Field(default=0, ge=0)
    #: 是否允许在这个页面移除（只有自定义根可以）。
    removable: bool = False
    #: 目录当前是否可读；不可读时页面提示"路径不存在或没有权限"，而不是静默当作没有技能。
    readable: bool = True


class SkillsView(_Dto):
    """技能清单与启用状态（技能管理页，2026-10-16；契约 §16.5）。

    ``enabled`` 是**总闸**（`skill` 能力开关）：它关着时技能对模型完全不可见，页面据此说明
    "技能能力关着"。逐条开关与它**不是一回事**——总闸是"这个功能用不用"，逐条是"哪个技能可用"。
    """

    #: `skill` 能力当前是否开启（总闸）。关闭时下面仍列出技能，让用户先看清再决定。
    enabled: bool = False
    #: 没有可用设置库：逐条开关写不下去（503），页面必须如实说明。
    store_unavailable: bool = False
    skills: list[SkillEntryView] = Field(default_factory=list)
    #: 当前在扫的发现根（2026-10-20）：随包 / 个人级 / 用户自己添加的，各自发现到几个技能。
    roots: list[SkillRootView] = Field(default_factory=list)
    #: 被发现过程跳过的条目说明（格式不合规、越界等）——不列出来，用户只会看到"技能不见了"。
    diagnostics: list[str] = Field(default_factory=list)


# ---- 计划 / 工具 / 审批 ----------------------------------------------------


PlanStepStatus = Literal["pending", "running", "completed", "failed", "skipped", "blocked"]


class PlanStepView(_Dto):
    """计划步骤。**完成一步不等于完成整个任务**，状态由 Graph 给出，页面不自算。"""

    step_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    status: PlanStepStatus


class PlanView(_Dto):
    goal: str = Field(min_length=1)
    steps: list[PlanStepView] = Field(default_factory=list)


ToolStatus = Literal["pending", "running", "ok", "error", "denied", "unknown"]

ToolOutcomeKind = Literal["read", "list", "search", "edit", "write", "delete", "shell", "delegation", "web"]

FileDiffLineKind = Literal["context", "add", "del"]


class FileDiffLineView(_Dto):
    """unified diff 的一行（§15.8 工具行）。

    `old_line`/`new_line` 是**解析出来的行号**：新增行在旧文件里没有行号（`None`），
    删除行在新文件里没有行号。页面据此对齐两侧 gutter，不需要自己数行。
    """

    kind: FileDiffLineKind
    old_line: int | None = Field(default=None, ge=1)
    new_line: int | None = Field(default=None, ge=1)
    text: str


class FileDiffHunkView(_Dto):
    """一个 hunk：`@@ -x,y +z,w @@` 头部加它覆盖的行。"""

    header: str
    lines: list[FileDiffLineView] = Field(default_factory=list)


class FileDiffView(_Dto):
    """结构化的文件改动（§15.8 工具行）。

    这是"已解析"的 diff，不是一段文本：页面直接渲染 hunk 与行号，**不从自然语言或
    unified diff 文本里重新解析**（架构 §6.1）。`additions`/`deletions` 如实计数
    hunk 里的 `+`/`-` 行，供卡片折叠时显示。
    """

    #: 规范化目标（工作区相对路径或已脱敏路径）。
    path: str
    additions: int = Field(ge=0)
    deletions: int = Field(ge=0)
    hunks: list[FileDiffHunkView] = Field(default_factory=list)


class FileChangeSummaryView(_Dto):
    """一次任务里某个文件的累计变更（2026-10-21）。

    ``edits`` 是**被改了几次**（同一个文件被 edit 多次是正常的：一次改一处），
    ``additions``/``deletions`` 是这一组内的累计行数，不是最后一次的片段。
    """

    path: str
    kind: Literal["create", "modify", "delete"]
    additions: int = Field(ge=0)
    deletions: int = Field(ge=0)
    edits: int = Field(ge=1)
    last_at: str
    #: 有没有可对比的"改前内容"。``False`` 时 `unavailable_reason` 说明为什么（新建/二进制/超大）。
    has_baseline: bool
    unavailable_reason: str | None = None


class TurnChangesView(_Dto):
    """一轮（``run_id``）里的文件变更；页面按轮次分组显示。"""

    run_id: str | None
    started_at: str | None
    files: list[FileChangeSummaryView] = Field(default_factory=list)


class UnrecordedChangeView(_Dto):
    """命令/代码改过、但**没有进账本**的文件（2026-10-22）。

    为什么单列而不并进账本：这些写入没有"改前原文"，塞进账本会让面板假装自己有基线。
    用户需要知道的是"还有这些文件被动过"，而不是让面板谎报完整性。
    """

    path: str
    status: str
    #: 为什么没有基线：目前只有一种——经 `run_shell` / `execute_code` 写入。
    reason: str = "经命令或代码写入，没有逐次改前内容"


class SessionChangesView(_Dto):
    """整个会话的文件变更总览（最新的轮次在前）。"""

    session_id: str
    turns: list[TurnChangesView] = Field(default_factory=list)
    total_files: int = Field(ge=0)
    additions: int = Field(ge=0)
    deletions: int = Field(ge=0)
    #: 账本之外、本轮被命令/代码动过的文件（2026-10-22）。`unrecorded_note` 解释为什么为空。
    unrecorded: list[UnrecordedChangeView] = Field(default_factory=list)
    unrecorded_note: str | None = None


class FileChangeDiffView(_Dto):
    """整文件 diff：``baseline``（首次改动前原文）对 ``当前磁盘内容``。

    与工具卡片里的 :class:`FileDiffView` 结构一致，但**覆盖整个文件**，不是单次调用的片段。
    """

    path: str
    additions: int = Field(ge=0)
    deletions: int = Field(ge=0)
    hunks: list[FileDiffHunkView] = Field(default_factory=list)
    truncated: bool = False
    has_baseline: bool
    unavailable_reason: str | None = None
    before_sha256: str | None = None
    after_sha256: str | None = None


class SearchMatchView(_Dto):
    """A bounded, sanitized content match with its real source line."""

    path: str
    line_number: int = Field(ge=1)
    text: str


class DelegationChildView(_Dto):
    """一次自主委派里**一个子 Agent**的结局（§18.7）。

    字段取自参照实现（CodeBuddy 的任务面板：状态 / token / 工具调用次数），再加证据条数与失败原因。
    用户要判断的是"这一路查到没有、花了多少"，而不是去读一段 JSON：把这些摆成一行行，卡片才说明问题。

    **刻意不含"预留 token"**（2026-10-13 裁定）：它是 `_prepare` 切出来的**上限**，同一份配置下每一路
    几乎一样，且长得像费用——摆在对话卡里必然被读成"这一路花了多少"（§15.7 要求预留与实际分开写）。
    实际花费要看用量区；要做"每路实际用量"必须先把子任务的模型调用记录保留子标识（§18.7 的未做项）。
    """

    scope: str
    status: Literal["completed", "failed"]
    tool_calls: int = Field(default=0, ge=0)
    model_calls: int = Field(default=0, ge=0)
    evidence_count: int = Field(default=0, ge=0)
    #: 未完成时的原因（已脱敏、已有界）；完成时为 `None`。
    error: str | None = None


class ToolOutcomeView(_Dto):
    """工具结果的**结构化、有界**展示分区（§15.8 工具行）。

    页面用它把"输出详情"分成路径、结果摘要、正文与 stdout/stderr 几块，而不是从自然语言里
    重新猜结构。所有正文都按展示上限截断：`truncated=True` 表示这里只是有界预览，完整内容仍
    保存在本地工具事件中，不进入页面（§15.8）。

    认不出的工具或解析不出的载荷**不产出**这个对象（`None`）：调用方必须退回安全的通用渲染器，
    不能把一段任意文本当成已知工具的结构。
    """

    #: ``web``（2026-10-26）= 网页检索 / 读取：``target`` 是搜索词或页面标题，``text`` 是有界正文
    #: 或前几条命中标题。页面按它渲染"结果"卡；不与 ``read`` 混用，后者带文件行号语义。
    kind: ToolOutcomeKind
    #: 规范化目标（工作区相对路径、命令 cwd、搜索词或页面标题）；投影层给不出时为空。
    target: str | None = None
    #: 一行结果摘要（变更行数、条目数、退出码、字节数……）。
    summary: str | None = None
    #: 有界正文：读取内容或 diff 文本。
    text: str | None = None
    #: 结构化文件改动；只有能解析出 unified diff 的工具（当前是 `edit_file`）才有。
    diff: FileDiffView | None = None
    #: 路径列表（目录清单等）；有界。
    paths: list[str] = Field(default_factory=list)
    matches: list[SearchMatchView] = Field(default_factory=list)
    #: 总条目数或文件总行数；未知为 `None`。
    total: int | None = Field(default=None, ge=0)
    #: 展示正文的**首行在源文件里的行号**（1 基）；只有 `read` 给，其余为 `None`（v0.8 T73）。
    #: 页面据此把行号还原成文件真实行号（§19.3）——投影只搬运工具结果里**已有**的 `first_line`，
    #: 不新增工具、不加参数、不新增读取通道。取不到时页面退回"页内序号"的如实措辞。
    first_line: int | None = Field(default=None, ge=1)
    stdout: str | None = None
    stderr: str | None = None
    exit_code: int | None = None
    timed_out: bool | None = None
    #: 命令仍在后台运行（用户点了"转后台"，2026-10-21）。
    #: 它与 `timed_out` 是两件不同的事：超时=这一轮不等了并终止；后台=这一轮不等了但进程还活着。
    background: bool = False
    #: 一次自主委派的每个子任务结局（§18.7）；只有 `kind="delegation"` 会填。
    children: list[DelegationChildView] = Field(default_factory=list)
    truncated: bool = False


class ToolEvidenceView(_Dto):
    """一次工具调用的机械校验证据（§4.3、§6.7）。

    `summary` 是**已脱敏**的可读摘要；`verified` 来自反思层的机械校验，不是模型的自我评价。
    """

    evidence_type: str | None = Field(default=None, min_length=1, exclude=True)
    summary: str = Field(min_length=1)
    verified: bool


class WebSourceView(_Dto):
    """成功联网结果里的实际来源；不代表模型已读取搜索命中的正文。"""

    title: str
    url: str
    kind: Literal["search", "read"]
    retrieved_at: str | None = None
    source_id: str = ""
    cited: bool = False
    published_at: str | None = None
    provider: str | None = None


class ToolUseView(_Dto):
    """一次工具调用的可见信息（§15.8 工具行）。

    `redacted_arguments` 的值是**已脱敏的展示字符串**：布尔、数字与嵌套结构由投影层渲染成字符串，
    因此 DTO 结构上不可能携带任意对象或未脱敏原文。

    `outcome` 与 `evidence` 是可空的结构化分区：认不出的工具没有 `outcome`（页面走通用渲染器），
    尚未结算的调用没有 `evidence`（那是本轮收尾后由工具事件重算的事实）。
    """

    tool_call_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    status: ToolStatus
    redacted_arguments: dict[str, str] = Field(default_factory=dict)
    approval_id: str | None = None
    output_preview: str | None = None
    output_digest: str | None = Field(default=None, exclude=True)
    truncated: bool = False
    started_at: datetime | None = None
    finished_at: datetime | None = None
    elapsed_ms: int | None = Field(default=None, ge=0)
    outcome: ToolOutcomeView | None = None
    evidence: ToolEvidenceView | None = None
    web_sources: list[WebSourceView] = Field(default_factory=list)

    @field_validator("started_at", "finished_at")
    @classmethod
    def _tz(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _require_timezone(value)


ApprovalState = Literal["pending", "executing", "approved", "rejected", "expired", "interrupted"]


class ApprovalView(_Dto):
    """审批的**派生视图状态**。

    底层只有 `pending_approval` 与 `approval_history`（决定值 `approve`/`edit`/`reject`），没有状态机字段。
    映射规则（T28 实现）：运行活跃且存在待决请求 → `pending`；决定已被接受、工具仍在跑 → `executing`；
    `approve` → `approved`；`reject` → `rejected`；运行在待决期间结束 → `expired`。
    `edit` 目前归入 `approved`，被改写的参数通过 `edited_arguments` 携带，不新增页面状态。
    """

    approval_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    tool_call_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    #: 动作指纹（已哈希）：用于说明"这次批准只对这一份参数有效"。
    action_fingerprint: str | None = Field(default=None, min_length=1, exclude=True)
    redacted_arguments: dict[str, str] = Field(default_factory=dict)
    edited_arguments: dict[str, str] = Field(default_factory=dict)
    #: 规范化展示目标：工作区相对路径或命令 cwd；投影层给不出时为空。
    target: str | None = None
    outside_workspace: bool = False
    confirmation_stage: Literal[1, 2] = 1
    impact_summary: str = Field(min_length=1)
    risk: Literal["normal", "high"]
    state: ApprovalState
    created_at: datetime
    #: 失效或被拒绝时的说明；正文由调用方给出，不含原始异常。
    note: str | None = None

    @field_validator("created_at")
    @classmethod
    def _tz(cls, value: datetime) -> datetime:
        return _require_timezone(value)


QuestionState = Literal["pending", "answered", "skipped", "expired"]


class QuestionItemView(_Dto):
    """题组里的一题（含用户对它的答复）。`answer` 为空且 `skipped` 为真 = 这题被跳过。"""

    index: int = Field(ge=0)
    question: str = Field(min_length=1)
    #: 最多 3 个可选答案（空 = 只能自由输入）。
    options: list[str] = Field(default_factory=list)
    allow_free_text: bool = True
    #: 用户对这一题的答复（已脱敏）；未答或跳过时为 `None`。
    answer: str | None = None
    skipped: bool = False


class QuestionView(_Dto):
    """提问的**派生视图状态**（2026-10-28，架构 §6.14）。

    底层只有 `pending_question`（当前请求）与 `question_history`（答复记录），没有状态机字段：
    `pending` / `answered` / `skipped` / `expired` 全部在这里推导。

    映射规则：挂着活的 interrupt 且有待决请求 → `pending`；有对应答复记录 → `answered`
    （至少一题给了答复）或 `skipped`（整组跳过 / 无人值守跳过）；有待决请求但 interrupt 已不在了
    （运行在执行中断里结束） → `expired`，页面不给按钮。

    **一次挂起对应一次提交**：`items` 是整组题，用户翻页答完一起提交；页面用 `items` 的长度做
    "1/3"式的进度，服务端不记录"答到第几题"。
    """

    question_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    tool_call_id: str = Field(min_length=1)
    #: 第一题的正文（单题兼容；多题时以 `items` 为准）。
    question: str = Field(min_length=1)
    #: 整组题（1–5 题）。旧 Checkpoint 的单题请求在这里是一条一题的列表。
    items: list[QuestionItemView] = Field(default_factory=list)
    state: QuestionState
    #: 单题兼容的答复副本（只有一题时等于它的答复）；多题时给 `None`，逐题看 `items`。
    answer: str | None = None
    asked_at: datetime
    answered_at: datetime | None = None
    #: 失效或跳过时的说明；正文由调用方给出，不含原始异常。
    note: str | None = None

    @field_validator("asked_at")
    @classmethod
    def _asked_tz(cls, value: datetime) -> datetime:
        return _require_timezone(value)

    @field_validator("answered_at")
    @classmethod
    def _answered_tz(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _require_timezone(value)


# ---- 记忆 / RAG（§15.8）---------------------------------------------------


RetrievalOutcome = Literal["hit", "miss", "failed"]
RetrievalSourceKind = Literal["workspace_file", "long_term_memory", "session_history"]


class RetrievalSourceView(_Dto):
    """安全引用：只给定位与用途，**不给正文、向量或内部评分**。"""

    kind: RetrievalSourceKind
    #: 形如 `path:line_start-line_end` 或记忆条目 id + 写入时间。
    reference: str = Field(min_length=1)
    usage: str = Field(min_length=1)


class MemoryUseView(_Dto):
    """一轮的检索与长期记忆结果。

    `miss`（查了没有）与 `failed`（看不了）是两个不同事实，不得合并；写入失败不影响本轮回答。
    """

    retrieval_outcome: RetrievalOutcome
    sources: list[RetrievalSourceView] = Field(default_factory=list)
    retrieval_note: str = Field(min_length=1)
    #: 闭集：`written`（真落了库）/ `failed`（写了但失败）/ `skipped`（有候选但一条都没落，必须说明为什么）；
    #: `None` = 本轮没走到写入节点（§10.4）。
    write_outcome: Literal["written", "failed", "skipped"] | None = None
    write_detail: str | None = None


# ---- 消息 ------------------------------------------------------------------


class UserMessageView(_Dto):
    kind: Literal["user"] = "user"
    message_id: str = Field(min_length=1)
    text: str
    #: 这条消息带的图片**引用**（2026-10-16，相对路径 `2026/10/16/<会话>/<哈希>.png`）：
    #: 页面拿它去 `GET /api/sessions/{id}/attachments?attachment_id=…` 取缩略图。
    #: 只回引用——不回字节、不回磁盘路径（与 `AttachmentView` 同一条门槛）。
    #: 空列表 = 这条消息没有图（也兼容所有旧 Checkpoint：那时没有 `user_attachment_refs`）。
    attachments: list[str] = Field(default_factory=list)
    #: **可空**：Checkpoint 里的 LangChain 消息不携带时间戳，投影层不伪造时间（T28 实测）。
    #: 用量记录落地后（§15.6 的 `model_call_records`）可由调用记录补齐。
    created_at: datetime | None = None

    @field_validator("created_at")
    @classmethod
    def _tz(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _require_timezone(value)


class AssistantMessageView(_Dto):
    kind: Literal["assistant"] = "assistant"
    message_id: str = Field(min_length=1)
    text: str
    #: 只投影供应商实际返回的 reasoning_content；不合成、不回写模型历史。
    reasoning: str | None = None
    reasoning_truncated: bool = False
    chunk_sequence: int = Field(default=0, ge=0)
    stream_channel: Literal["text", "reasoning"] = "text"
    #: 与 `UserMessageView.created_at` 同理：没有时间戳就是 `None`。
    created_at: datetime | None = None
    #: 产生这段回答的模型调用；流式未完成时 `complete=False`，由 Checkpoint 的稳定消息 id 接管终态。
    model_call_id: str | None = None
    complete: bool = True

    @field_validator("created_at")
    @classmethod
    def _tz(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _require_timezone(value)


class ToolMessageView(_Dto):
    kind: Literal["tool"] = "tool"
    message_id: str = Field(min_length=1)
    tool: ToolUseView


class MemoryMessageView(_Dto):
    kind: Literal["memory"] = "memory"
    message_id: str = Field(min_length=1)
    memory: MemoryUseView


class ErrorMessageView(_Dto):
    kind: Literal["error"] = "error"
    message_id: str = Field(min_length=1)
    error: ErrorView


MessageView = Annotated[
    UserMessageView
    | AssistantMessageView
    | ToolMessageView
    | MemoryMessageView
    | ErrorMessageView,
    Field(discriminator="kind"),
]


# ---- 用量（§15.6）---------------------------------------------------------


def cache_hit_ratio(
    uncached_input_tokens: int | None,
    cache_read_tokens: int | None,
    cache_write_tokens: int | None,
) -> float | None:
    """命中率 = 读缓存 /（未缓存输入 + 读缓存 + 写缓存）。

    2026-09-30 修正一处**让这个比率在 DeepSeek 上恒为未知**的判定。

    原来要求三个字段全部非空，否则返回 `None`。但 DeepSeek 是**隐式缓存**：缓存写入由服务端自动
    完成，根本不存在"写缓存"这个上报项（计费只有"命中"与"未命中"两个桶）。于是
    ``cache_write_tokens`` 恒为 `None`，比率**永远**算不出来——实测页面一直显示"缓存命中 未知"，
    而后台明明有命中数据。这不是"未上报"，是"这个供应商没有这个概念"，两者必须分开。

    正确口径（§15.6「未上报 ≠ 0」反过来读）：
    - 连**缓存读**都没上报（``None``）→ 这个供应商没有缓存概念，返回 `None`（显示"未知"）；
    - `cache_read` 已知、只缺 `cache_write` → 按 0 计入分母（隐式缓存没有独立写桶，分母就是
      供应商实际计费的输入量＝未命中 + 命中）；**不是**把未知当成 0，而是该桶本就不存在；
    - 分母为零仍返回 `None`（不显示 0%）。
    """
    if uncached_input_tokens is None or cache_read_tokens is None:
        return None
    total = uncached_input_tokens + cache_read_tokens + (cache_write_tokens or 0)
    if total <= 0:
        return None
    return cache_read_tokens / total


def context_usage_ratio(used_tokens: int | None, window_tokens: int | None) -> float | None:
    """计量器比例：任一侧未知或窗口非正时返回 `None`（显示"未知"，不猜 0%）。"""
    if used_tokens is None or window_tokens is None or window_tokens <= 0:
        return None
    return min(used_tokens / window_tokens, 1.0)


class ModelCallRecordView(_Dto):
    """一次模型调用的记录（§15.6）。

    **重试使用新的 `model_call_id`**，用 `retry_of` 关联，不覆盖失败尝试。
    """

    model_call_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    purpose: str = Field(min_length=1)
    connection_id: str = Field(min_length=1)
    config_version: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    #: 端点安全指纹（不含密钥），用于确认"缓存基线可比"。
    endpoint_fingerprint: str | None = Field(default=None, min_length=1, exclude=True)
    retry_of: str | None = None
    started_at: datetime
    first_token_at: datetime | None = None
    finished_at: datetime | None = None
    status: Literal["running", "ok", "error", "aborted"]
    estimated_prompt_tokens: int | None = Field(default=None, ge=0)
    actual_uncached_input_tokens: int | None = Field(default=None, ge=0)
    actual_cache_read_tokens: int | None = Field(default=None, ge=0)
    actual_cache_write_tokens: int | None = Field(default=None, ge=0)
    actual_output_tokens: int | None = Field(default=None, ge=0)
    actual_reasoning_tokens: int | None = Field(default=None, ge=0)
    #: 供应商是否上报了缓存字段。`False` 时页面显示"未上报"，而不是显示 0%。
    cache_metrics_reported: bool = False
    model_elapsed_ms: int | None = Field(default=None, ge=0)
    time_to_first_token_ms: int | None = Field(default=None, ge=0)
    context_window_tokens: int | None = Field(default=None, gt=0)
    context_used_tokens: int | None = Field(default=None, ge=0)
    tools_schema_fingerprint: str | None = Field(default=None, exclude=True)
    system_prompt_fingerprint: str | None = Field(default=None, exclude=True)
    history_prefix_fingerprint: str | None = Field(default=None, exclude=True)
    dynamic_context_fingerprint: str | None = Field(default=None, exclude=True)

    @field_validator("started_at", "first_token_at", "finished_at")
    @classmethod
    def _tz(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _require_timezone(value)


#: 一轮的终态。``blocked``：本轮已结束，但目标未达成、在等用户补充信息（与 ``awaiting_approval``
#: 并列的第二种"等待"——前者等用户做决定，后者等用户给信息）。
#: ``cancelled``（2026-10-13 追加）：用户停下来的那一轮。它是**终态**，且与 `failed` 必须分开——
#: "被中止"不是"执行失败"（§17.4）。``running`` 之外的所有取值都是终态。
TurnFinalState = Literal[
    "running", "awaiting_approval", "blocked", "completed", "failed", "cancelled"
]


class TurnRecordView(_Dto):
    """一轮（一次用户提交到终态）的汇总记录（§15.6）。

    用途有二：刷新恢复的用量真源，以及页面统计行的确定性来源。聚合值必须由明细计算得出，
    因此这里同时给出 `model_call_ids` / `tool_event_ids` 以便核对。
    """

    run_id: str = Field(min_length=1)
    submitted_at: datetime
    final_at: datetime | None = None
    final_state: TurnFinalState
    model_call_ids: list[str] = Field(default_factory=list)
    tool_event_ids: list[str] = Field(default_factory=list)
    approval_wait_ms: int | None = Field(default=None, ge=0)
    turn_elapsed_ms: int | None = Field(default=None, ge=0)
    model_elapsed_ms: int | None = Field(default=None, ge=0)
    tool_elapsed_ms: int | None = Field(default=None, ge=0)
    time_to_first_token_ms: int | None = Field(default=None, ge=0)
    estimated_prompt_tokens: int | None = Field(default=None, ge=0)
    actual_uncached_input_tokens: int | None = Field(default=None, ge=0)
    actual_cache_read_tokens: int | None = Field(default=None, ge=0)
    actual_cache_write_tokens: int | None = Field(default=None, ge=0)
    actual_output_tokens: int | None = Field(default=None, ge=0)
    actual_reasoning_tokens: int | None = Field(default=None, ge=0)
    cache_metrics_reported: bool = False
    cache_hit_ratio: float | None = Field(default=None, ge=0, le=1)
    context_used_tokens: int | None = Field(default=None, ge=0)
    context_window_tokens: int | None = Field(default=None, gt=0)
    context_usage_ratio: float | None = Field(default=None, ge=0, le=1)
    #: 老 Checkpoint 没有这些记录时必须显式说明，不能伪造估算或零值（§15.6）。
    usage_recorded: bool = True

    @field_validator("submitted_at", "final_at")
    @classmethod
    def _tz(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _require_timezone(value)


class TraceEventView(_Dto):
    """轨迹页的一条事件（2026-10-16；§15.6 的审计面）。

    为什么要它：轨迹此前只有"工具调用表 + 每轮用量"，**看不到审批与压缩发生过什么**——
    而这两件事恰恰是"为什么这一轮慢、上下文为什么变小、是谁批准的"的答案。数据本来就在
    State 里（`tool_events` / `approval_history` / `compaction_events`），这里只做投影。

    `kind` 是闭集；`label` 只放**已脱敏的一句展示文案**（工具名、审批结论/理由、压缩条数），
    **不复制参数正文与文件内容**：审计要的是"发生过什么"，不是再来一份数据。
    """

    kind: Literal["tool", "approval", "compaction"]
    event_id: str = Field(min_length=1)
    run_id: str | None = None
    #: 主时刻：工具 = 开始、审批 = **请求**、压缩 = 发生。
    at: datetime | None = None
    #: 结束时刻：工具 = 完成、审批 = **决定**（两者之差就是等待时长）；压缩没有结束时刻。
    finished_at: datetime | None = None
    label: str = Field(min_length=1)
    #: 工具状态（`ok` / `denied` / `failed`…）或审批结论（`approved` / `denied`…）。
    status: str | None = None
    #: 审批等待毫秒（只有 `approval` 有）。
    wait_ms: int | None = Field(default=None, ge=0)
    #: 压缩折叠掉的消息条数与前后**估算** token（只有 `compaction` 有）。
    folded_messages: int | None = Field(default=None, ge=0)
    tokens_before: int | None = Field(default=None, ge=0)
    tokens_after: int | None = Field(default=None, ge=0)


class TurnUsageView(_Dto):
    """持久轮次与其消息锚点；供刷新后把统计放回对应轮次。"""

    user_message_id: str = Field(min_length=1)
    usage: TurnRecordView
    model_calls: list[ModelCallRecordView] = Field(default_factory=list)
    #: 本轮的事件时间线（工具 / 审批 / 压缩，2026-10-16）：轨迹页按轮渲染，
    #: 缺记录（旧 Checkpoint）时是空表——页面照旧工作，不编一条假事件。
    events: list[TraceEventView] = Field(default_factory=list)


# ---- 上下文快照（§15.7）---------------------------------------------------


class RuntimeSourceVersionView(_Dto):
    """来源与其版本/安全引用；只记引用，不复制正文或密钥。"""

    source: str = Field(min_length=1)
    version: str = Field(min_length=1)


class RuntimeContextSnapshotView(_Dto):
    """**公开**的运行上下文快照视图。

    刻意**不含** `canonical_text`：提示词正文属于"明确不展示"的一类（§15.8）。页面可以显示上下文
    快照的标识、版本、内容哈希与来源版本，用来解释"这一轮带了哪些上下文"，但不能显示其文本。
    """

    schema_version: int = SCHEMA_VERSION
    snapshot_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    context_version: int = Field(ge=0)
    created_at: datetime
    content_hash: str = Field(min_length=1)
    #: `canonical_text` 的字符数：用于判断快照是否变化，不泄露内容。
    text_length: int = Field(ge=0)
    source_versions: list[RuntimeSourceVersionView] = Field(default_factory=list)

    @field_validator("created_at")
    @classmethod
    def _tz(cls, value: datetime) -> datetime:
        return _require_timezone(value)


# ---- 上下文计量器（§15.6）-------------------------------------------------


ContextMeterScope = Literal["current", "previous", "unknown"]


class CompressionView(_Dto):
    """最新一次实际压缩；running 仅存在于进程内，完成/失败从 Checkpoint 恢复。"""

    operation_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    state: Literal["running", "completed", "failed"]


class ContextMeterView(_Dto):
    """圆形计量器的口径。

    `scope`：执行中为 `current`（正在组装的这一次请求），空闲为 `previous`（装配器上一次构造的请求），
    从未组装或窗口未知为 `unknown`。
    """

    used_tokens: int | None = Field(default=None, ge=0)
    window_tokens: int | None = Field(default=None, gt=0)
    scope: ContextMeterScope = "unknown"
    #: 输出预留单列，不重复计入已用输入。
    completion_reserve_tokens: int | None = Field(default=None, ge=0)
    compression: CompressionView | None = None


# ---- 会话快照（§15.4 / §6.6）---------------------------------------------


SessionState = Literal[
    "idle", "running", "awaiting_approval", "awaiting_input", "blocked", "completed", "failed"
]

#: 一轮最终结果的对外状态。``blocked``：本轮已结束、这条回答有效，但目标未达成（在等用户补充）。
#: 它既不能冒充 ``completed``，也不该被当成 ``failed`` 丢掉——它带着一条真实生成的回答。
#: ``cancelled``（2026-10-13 追加）：这一轮被用户停下来了。它**要带着已产出的部分回答发出去**
#: （不是"还没有结果"），页面据此标「已停止」而不是"失败"（§17.4）。
FinalResultState = Literal["completed", "blocked", "failed", "cancelled"]


class FinalResultView(_Dto):
    """本轮最终结果的可读摘要与证据引用。"""

    run_id: str = Field(min_length=1)
    state: FinalResultState
    summary: str
    evidence_references: list[str] = Field(default_factory=list)


class WorkflowChildView(_Dto):
    child_id: str
    scope: str
    status: str
    evidence_count: int = Field(ge=0)
    error: str | None = None


class WorkflowView(_Dto):
    """同轮检查点的只读事实，不暴露模型输入或可执行规格。"""

    name: Literal["readonly_research", "code_change", "delegated_research"]
    run_id: str
    phase: str
    steps: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    model_calls: int = Field(ge=0)
    model_tokens_reserved: int = Field(ge=0)
    evidence_count: int = Field(ge=0)
    error: str | None = None
    children: list[WorkflowChildView] = Field(default_factory=list)


class ModelSelectionView(_Dto):
    """某个作用域**当前选中的连接**（默认或某个会话的下一轮模型）。

    `connection_id` 与 `config_version` 与连接视图同源、也同义：页面把这两个值**原样回传**
    做写入校验（§6.1 的"选择也必须按版本校验"）。因此它必须来自设置库的那一行，
    不能由页面自己拼。
    """

    connection_id: str = Field(min_length=1)
    config_version: str = Field(min_length=1)


class SessionSnapshot(_Dto):
    """`session_snapshot` 事件的载荷，也是刷新恢复的权威展示状态。

    只读：生成快照不得写回 State、调用模型或执行工具（§15.4）。`schema_version` 与
    `snapshot_id` 一起让恢复逻辑能判断"这份快照来自哪个契约版本"。
    """

    schema_version: int = SCHEMA_VERSION
    snapshot_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    workspace: WorkspaceRef
    title: str
    branch_id: str = "main"
    branch_title: str = "原讨论"
    mode: Literal["chat", "agent"] = "agent"
    session_state: SessionState
    #: 执行锁已释放但台账未收口；仅当前快照提供核对入口，历史快照不附加此信息。
    interrupted_request_id: str | None = None
    #: 空会话没有当前任务，这里就是 `None`——不得用随机占位值填充（§15.4）。
    current_task: TaskRef | None = None
    messages: list[MessageView] = Field(default_factory=list)
    plan: PlanView | None = None
    approval: ApprovalView | None = None
    #: 正在等用户回答的那一个提问（2026-10-28，架构 §6.14）。与 `approval` **互斥**。
    question: QuestionView | None = None
    memory: MemoryUseView | None = None
    #: 这个会话**自己的**下一轮模型选择；`None` = 跟随默认连接。
    #: 页面据此把"我看到的选择"回传，写入才不会被另一个页面悄悄覆盖（§6.1）。
    #: 只读展示，切换它不修改正在运行的那一轮（那一轮用提交时固定的连接）。
    model_selection: ModelSelectionView | None = None
    #: 本会话**下一轮**是否按增量流式展示最终回答（v0.8 T74，§19.4）。
    #:
    #: 输入区使用下一轮配置；运行中的活动提示使用下面的 current_answer_streaming。
    answer_streaming: bool = False
    #: 当前未结束轮次的固定配置；没有活动轮次或历史版本无法读取时为 None，不猜下一轮配置。
    current_answer_streaming: bool | None = None
    errors: list[ErrorView] = Field(default_factory=list)
    usage: TurnRecordView | None = None
    #: 本轮逐次模型调用明细（§15.6）：统计行的可展开部分用它，来源与 `usage` 同为 Checkpoint
    #: 里的 `model_call_records`，所以刷新与重启后仍在（不是只有事件流里才看得到）。每次重试是
    #: 独立一条，用 `retry_of` 关联；没有记录（旧 Checkpoint）时为空列表，页面据此不显示明细。
    model_calls: list[ModelCallRecordView] = Field(default_factory=list)
    turn_usages: list[TurnUsageView] = Field(default_factory=list)
    context: ContextMeterView = Field(default_factory=ContextMeterView)
    runtime_context: RuntimeContextSnapshotView | None = Field(default=None, exclude=True)
    final_result: FinalResultView | None = None
    workflow: WorkflowView | None = None
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def _tz(cls, value: datetime) -> datetime:
        return _require_timezone(value)


class ConversationNodeView(_Dto):
    turn_id: str
    parent_turn_id: str | None = None
    origin_branch_id: str
    run_id: str | None = None
    question: str
    answer_preview: str
    state: str
    submitted_at: str | None = None
    ordinal: int
    can_continue: bool


class ConversationBranchView(_Dto):
    branch_id: str
    title: str
    fork_turn_id: str | None = None
    tip_turn_id: str | None = None
    position: int
    active: bool


class SideChatView(_Dto):
    """会话地图里挂着的侧聊（§21.7，2026-10-23）。

    侧聊是**独立会话**（各自 thread、各自 checkpoint），但在图上要显示成主会话的
    关联分支——否则用户在"会话地图"里看不到它，就会以为侧聊是凭空冒出来的。
    """

    session_id: str
    title: str
    #: 从主会话的哪一轮开出；为空表示"从最新一轮"。
    origin_turn_id: str | None = None
    created_at: str | None = None


class ConversationMapView(_Dto):
    session_id: str
    active_branch_id: str
    nodes: list[ConversationNodeView]
    branches: list[ConversationBranchView]
    matched_turn_ids: list[str] = Field(default_factory=list)
    #: 从本会话开出的侧聊。**独立会话**，只是在地图上挂成关联分支（见 `SideChatView`）。
    side_chats: list[SideChatView] = Field(default_factory=list)


class SessionSummaryView(_Dto):
    """会话列表里的一行。

    **不含运行状态**：运行中/等待审批/已完成要从该会话线程的 Checkpoint 推出来，逐行读所有线程的
    Checkpoint 不属于列表用例。T30 的列表工作要么按需读、要么落一份会话级摘要；本节不伪造状态。
    """

    session_id: str = Field(min_length=1)
    thread_id: str = Field(default="（未记录）", min_length=1, exclude=True)
    title: str
    workspace: WorkspaceRef
    mode: Literal["chat", "agent"] = "agent"
    #: 是否是"当前打开的会话"（`sessions.status='active'` 全库唯一）。
    is_active: bool = False
    archived: bool = False
    #: 侧边会话（§21.7）：从哪个会话开出；普通会话为 `None`。
    parent_session_id: str | None = None
    created_at: datetime
    updated_at: datetime
    last_checkpoint_id: str | None = Field(default=None, exclude=True)

    @field_validator("created_at", "updated_at")
    @classmethod
    def _tz(cls, value: datetime) -> datetime:
        return _require_timezone(value)


Operation = Literal["submit_message", "resolve_approval", "resolve_question", "cancel_task"]

#: 一次命令的结局（与 `CommandResultView.status` 同值）。
#: ``running`` 只在 ``jobs.background=true`` 时出现（§17.2）：命令已被接受、任务已进入运行。
#: ``cancelled`` 只在取消命令上出现（§17.4）：任务已被中止。
#: ``awaiting_input``（2026-10-28）：提问挂起——与 ``awaiting_approval`` 并列的第二种"等你"。
CommandStatus = Literal[
    "completed", "awaiting_approval", "awaiting_input", "failed", "running", "cancelled"
]


class CommandResultView(_Dto):
    """一次**执行状态修改**的命令结果引用（§6.2，v0.6 起含后台任务）。

    POST 完成时必须能凭它确认结局，不需要 SSE 才能知道命令成没成。`replayed=True` 表示
    这条结果是幂等台账给的（本次没有真的跑图）。

    `status="running"` 只在 `jobs.background=true` 时出现（§17.2）：含义是"**命令已被接受、
    任务已进入运行**"（`queued` 与执行中都映射到它），结局由任务索引 + Checkpoint 快照收敛，
    凭 `job_id` 或重新拉快照都能拿到。默认路径（`background=false`）不会产生这个取值。
    """

    request_id: str = Field(min_length=1)
    operation: Operation
    session_id: str = Field(min_length=1)
    run_id: str | None = None
    #: 稳定结果引用：本次命令对应的 Checkpoint 快照标识。
    snapshot_id: str = Field(min_length=1)
    status: Literal[
        "completed", "awaiting_approval", "awaiting_input", "failed", "running", "cancelled"
    ]
    session_state: SessionState
    replayed: bool = False
    #: 后台任务标识（§17.2）：只有 `jobs.background=true` 的提交带上它；其余为 `None`。
    job_id: str | None = None
    error: ErrorView | None = None


class TaskView(_Dto):
    """任务中心里的一行（架构 §17.6，v0.6 T62）。

    **字段门槛**（§15.8）：不含 `run_id` / `thread_id` / `request_id` / 动作指纹 / 工具名 / 诊断号 /
    内部路径。`task_id` 是**稳定引用**（列表键与动作参数），页面**不展示**它——与 `session_id` /
    `approval_id` 同一口径。

    **没有"在等什么"字段**：它是 `state` 的函数（§17.6 的闭集表），由页面按状态推导。服务端不猜，
    也不需要因此去读会话的 Checkpoint。
    """

    task_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    #: 会话标题：任务中心靠它让用户认出"这是哪个会话在跑"，而不是显示内部标识。
    session_title: str
    workspace: WorkspaceRef
    state: JobState
    #: 服务端写好的用户可读说明（失败 / 取消 / 需核对的原因）；没有就是 `None`。
    detail: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @field_validator("created_at", "started_at", "finished_at")
    @classmethod
    def _tz(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _require_timezone(value)


class TaskCenterView(_Dto):
    """任务中心的一页（架构 §17.6）。

    `counts` **覆盖全部任务**（不只 `tasks` 里被 `limit` 截断的那些），因此页面上的筛选计数是准的；
    键**恰好**是任务状态闭集（`local_agent/job_state.py` 的 `JOB_STATES`），没有的状态是 0 而不是
    缺键——这条由服务端填充 + 测试守住（`dict[str, int]` 而不是 `Literal` 键：闭集只有一处来源，
    这里再写一份字面量就是第二套）。
    `pending_count` = `waiting_user` + `needs_review`：入口角标用它（为 0 时页面不显示角标）。
    """

    tasks: list[TaskView]
    counts: dict[str, int]
    pending_count: int


def redact_values(values: Mapping[str, str], secrets: list[str]) -> dict[str, str]:
    """把已知密钥值从展示字符串里抹掉（复用 v0.3 的脱敏入口，保持同一套规则）。

    `local_agent.config.redact_secrets` 是既有实现；这里只做薄封装，避免应用层各处自己拼替换逻辑。
    """
    from local_agent.config import redact_secrets

    return {key: redact_secrets(text, secrets) for key, text in values.items()}
