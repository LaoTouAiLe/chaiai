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


ExternalPathsMode = Literal["deny", "whitelist", "allow"]
ExternalPathAccess = Literal["read", "read_write"]

EXTERNAL_PATHS_MODES: Final[tuple[ExternalPathsMode, ...]] = get_args(ExternalPathsMode)
EXTERNAL_PATH_ACCESSES: Final[tuple[ExternalPathAccess, ...]] = get_args(ExternalPathAccess)


class ExternalPathGrantView(_Dto):
    """一条"工作区外路径"授权（T38）。

    ``path`` 是**用户自己填进来的目录**，与"从机器上发现再泄露给页面"的私人路径不是一回事：
    不回路径，用户就没法在这张列表上撤销授权。它已经规范化（统一大小写与分隔符、展开符号链接）。
    """

    path: str = Field(min_length=1)
    access: ExternalPathAccess
    created_at: datetime | None = None
    updated_at: datetime | None = None


class PermissionsView(_Dto):
    """「设置 → 权限」的读 DTO（T38）。

    ``mode`` 是当前生效档位（用户的选择优先）；``default_mode`` 是 ``config.yaml`` 的出厂默认，
    用于说明"库里没有用户选择时按哪一档跑"。授权条目只在 ``whitelist`` 档生效。
    """

    mode: ExternalPathsMode
    default_mode: ExternalPathsMode
    grants: list[ExternalPathGrantView] = Field(default_factory=list)
    #: 没有可用设置库时为真：这时改档位与加授权都存不下，界面必须如实说明。
    store_unavailable: bool = False


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


class CapabilitiesView(_Dto):
    """当前 Agent 档位与每个能力的状态（T56；§16.6）。

    `profiles` 是**页面可选**的档位（服务端枚举：内置三档 + `profiles.custom`），`default_profile`
    是 `config.yaml` 的出厂默认（`profile` 是**当前生效**那一个）——三者一起让页面既能画选择器，
    也能说清"现在这一档是从哪儿来的"（§16.6 档位切换）。
    """

    profile: str = Field(min_length=1)
    #: `config.yaml` 的出厂默认档位；用户切换过后 `profile` 与它不同。
    default_profile: str = ""
    #: 可选档位（固定顺序）。没有设置库时仍然给出，但 `store_unavailable` 为真、页面不提供切换。
    profiles: list[str] = Field(default_factory=list)
    #: 没有可用设置库：档位只能来自 `config.yaml`，写下去会被拒（503）。
    store_unavailable: bool = False
    capabilities: list[CapabilityStatusView] = Field(default_factory=list)


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

ToolOutcomeKind = Literal["read", "list", "search", "edit", "write", "delete", "shell"]

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


class SearchMatchView(_Dto):
    """A bounded, sanitized content match with its real source line."""

    path: str
    line_number: int = Field(ge=1)
    text: str


class ToolOutcomeView(_Dto):
    """工具结果的**结构化、有界**展示分区（§15.8 工具行）。

    页面用它把"输出详情"分成路径、结果摘要、正文与 stdout/stderr 几块，而不是从自然语言里
    重新猜结构。所有正文都按展示上限截断：`truncated=True` 表示这里只是有界预览，完整内容仍
    保存在本地工具事件中，不进入页面（§15.8）。

    认不出的工具或解析不出的载荷**不产出**这个对象（`None`）：调用方必须退回安全的通用渲染器，
    不能把一段任意文本当成已知工具的结构。
    """

    kind: ToolOutcomeKind
    #: 规范化目标（工作区相对路径或命令 cwd）；投影层给不出时为空。
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
    truncated: bool = False


class ToolEvidenceView(_Dto):
    """一次工具调用的机械校验证据（§4.3、§6.7）。

    `summary` 是**已脱敏**的可读摘要；`verified` 来自反思层的机械校验，不是模型的自我评价。
    """

    evidence_type: str | None = Field(default=None, min_length=1, exclude=True)
    summary: str = Field(min_length=1)
    verified: bool


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
TurnFinalState = Literal["running", "awaiting_approval", "blocked", "completed", "failed"]


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


class TurnUsageView(_Dto):
    """持久轮次与其消息锚点；供刷新后把统计放回对应轮次。"""

    user_message_id: str = Field(min_length=1)
    usage: TurnRecordView
    model_calls: list[ModelCallRecordView] = Field(default_factory=list)


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


# ---- 会话快照（§15.4 / §6.6）---------------------------------------------


SessionState = Literal["idle", "running", "awaiting_approval", "blocked", "completed", "failed"]

#: 一轮最终结果的对外状态。``blocked``：本轮已结束、这条回答有效，但目标未达成（在等用户补充）。
#: 它既不能冒充 ``completed``，也不该被当成 ``failed`` 丢掉——它带着一条真实生成的回答。
FinalResultState = Literal["completed", "blocked", "failed"]


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
    memory: MemoryUseView | None = None
    #: 这个会话**自己的**下一轮模型选择；`None` = 跟随默认连接。
    #: 页面据此把"我看到的选择"回传，写入才不会被另一个页面悄悄覆盖（§6.1）。
    #: 只读展示，切换它不修改正在运行的那一轮（那一轮用提交时固定的连接）。
    model_selection: ModelSelectionView | None = None
    #: 本会话**下一轮**是否按增量流式展示最终回答（v0.8 T74，§19.4）。
    #:
    #: 页面**只能**用这个字段判断"在不在流式"：拿"有没有增量"或"上一条消息完不完整"去推断，
    #: 在刚连上、网络慢、回答极短时都会得出错误答案。它与那一轮实际生效的配置同源（连接模式取
    #: 该会话选中的连接，旧配置模式取 `llm.streaming`），是**配置的只读投影**，不新增真源。
    answer_streaming: bool = False
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


class ConversationMapView(_Dto):
    session_id: str
    active_branch_id: str
    nodes: list[ConversationNodeView]
    branches: list[ConversationBranchView]
    matched_turn_ids: list[str] = Field(default_factory=list)


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
    created_at: datetime
    updated_at: datetime
    last_checkpoint_id: str | None = Field(default=None, exclude=True)

    @field_validator("created_at", "updated_at")
    @classmethod
    def _tz(cls, value: datetime) -> datetime:
        return _require_timezone(value)


Operation = Literal["submit_message", "resolve_approval", "cancel_task"]


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
    status: Literal["completed", "awaiting_approval", "failed", "running", "cancelled"]
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
