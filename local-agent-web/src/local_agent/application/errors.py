"""公开错误契约（ARCHITECTURE §15.5）。

规则（本节冻结）：

- 分类固定为七种，不得增加；
- 错误 DTO 必须同时给出**用户可执行提示**、**稳定错误码**、**是否可重试**与**安全的诊断关联 id**；
- 会话互斥与幂等至少固定 `SESSION_BUSY` / `IDEMPOTENCY_CONFLICT` / `RECOVERY_REQUIRED`；
- 异常原文只进本地受控日志：本模块的 DTO 不承载原始异常消息，构造时会先脱敏。

`retryable` 说的是**同一次提交能否在条件满足后原样重试**，不是"页面此刻是否显示按钮"：
`SESSION_BUSY` 在契约上可重试（等当前一轮结束），页面是否给按钮由页面按 T30 裁定自行决定，
所以 DTO 另给 `retry_condition` 说明条件。
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Final, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: 明显是密钥或私人绝对路径的内容：宁可构造失败，也不要把它送进页面或日志（§15.5、§15.8）。
_FORBIDDEN_IN_TEXT: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"[A-Za-z]:\\"),
    re.compile(r"/(?:home|Users)/[^/\s]+"),
)

ErrorCategory = Literal[
    "configuration",
    "model",
    "tool",
    "security",
    "approval",
    "recovery",
    "system",
]

ERROR_CATEGORIES: Final[tuple[ErrorCategory, ...]] = get_args(ErrorCategory)

# ---- 稳定错误码 -------------------------------------------------------------

# 会话互斥与幂等（§15.5，名称由契约固定）。
SESSION_BUSY: Final[str] = "SESSION_BUSY"
IDEMPOTENCY_CONFLICT: Final[str] = "IDEMPOTENCY_CONFLICT"
RECOVERY_REQUIRED: Final[str] = "RECOVERY_REQUIRED"
CONVERSATION_CONFLICT: Final[str] = "CONVERSATION_CONFLICT"

# 模型：与 v0.3 已有的供应商失败路径对齐（T26 原型已渲染过前三个）。
MODEL_AUTH_FAILED: Final[str] = "MODEL_AUTH_FAILED"
MODEL_CALL_FAILED: Final[str] = "MODEL_CALL_FAILED"
RUN_INTERRUPTED: Final[str] = "RUN_INTERRUPTED"
MODEL_RATE_LIMITED: Final[str] = "MODEL_RATE_LIMITED"
MODEL_RETRY_EXHAUSTED: Final[str] = "MODEL_RETRY_EXHAUSTED"
# 能力检测不通过（§15.9：进入需要工具的任务前明确拒绝，不静默换模型）。
MODEL_CAPABILITY_UNSUPPORTED: Final[str] = "MODEL_CAPABILITY_UNSUPPORTED"
# 未配置任何可用连接时的初始设置模式（§15.9）。
MODEL_CONNECTION_REQUIRED: Final[str] = "MODEL_CONNECTION_REQUIRED"
# 指向的连接不存在（被停用的连接也不在可选项里）。
MODEL_CONNECTION_NOT_FOUND: Final[str] = "MODEL_CONNECTION_NOT_FOUND"
# 设置写入携带的版本号与当前不一致（两个页面同时编辑同一条连接）。
MODEL_SETTINGS_CONFLICT: Final[str] = "MODEL_SETTINGS_CONFLICT"
# 连接字段或版本号不合法（服务端校验）。展示文本是**脱敏后的规则说明**，不回显用户填的原文。
MODEL_SETTINGS_INVALID: Final[str] = "MODEL_SETTINGS_INVALID"

# 配置：没有可用设置库时明确告知降级原因（§15.9）。
SETTINGS_STORE_UNAVAILABLE: Final[str] = "SETTINGS_STORE_UNAVAILABLE"

# 宿主能力：本机原生目录选择器打不开（没有桌面会话、Tk 缺失、子进程起不来或超时）。
# 文案必须把"没弹出来"与"你选错了目录"分开——后者是 `WORKSPACE_NOT_FOUND`。
DIRECTORY_PICKER_UNAVAILABLE: Final[str] = "DIRECTORY_PICKER_UNAVAILABLE"

# 安全：与既有 SecurityError / WorkspaceBoundaryError 对应（v0.1–v0.3 语义不变）。
SECURITY_POLICY_DENIED: Final[str] = "SECURITY_POLICY_DENIED"
WORKSPACE_BOUNDARY_VIOLATION: Final[str] = "WORKSPACE_BOUNDARY_VIOLATION"

# 审批：请求在等待期间随运行结束而失效（v0.3 `find_rejection` 语义）。
APPROVAL_EXPIRED: Final[str] = "APPROVAL_EXPIRED"

# 运行阶段失败：由投影层把内部 `error_events.stage`（节点名）换成这些**公开**码，节点名不进 DTO。
MEMORY_RETRIEVAL_FAILED: Final[str] = "MEMORY_RETRIEVAL_FAILED"
RAG_RETRIEVAL_FAILED: Final[str] = "RAG_RETRIEVAL_FAILED"
CONTEXT_COMPRESSION_FAILED: Final[str] = "CONTEXT_COMPRESSION_FAILED"
MEMORY_WRITE_FAILED: Final[str] = "MEMORY_WRITE_FAILED"
TOOL_CALL_FAILED: Final[str] = "TOOL_CALL_FAILED"
TOOL_CALL_BLOCKED: Final[str] = "TOOL_CALL_BLOCKED"
APPROVAL_FLOW_FAILED: Final[str] = "APPROVAL_FLOW_FAILED"
#: 兜底：没有专门映射的阶段失败，页面看到的是"未分类异常"，不是节点名。
#:
#: 它**只该留给真正未知的 stage**：已知的降级与"证据不足"都有专门码（下面三个）。否则用户会把
#: 正常的兜底路径读成崩溃——复审实证（2026-09-28），一轮正常的机械校验被显示成
#: "本轮出现未分类的异常 / 该步骤已中止"，而它既不是异常、也没有中止。
RUN_STAGE_FAILED: Final[str] = "RUN_STAGE_FAILED"
#: 本轮有一步**降级**（流式回退、回答合成降级、记忆收尾重写降级……）：回答仍可用，但走的不是
#: 完整路径。它不是崩溃，措辞必须让用户知道"东西还在，只是换了条路"。
RUN_DEGRADED: Final[str] = "RUN_DEGRADED"
#: 本轮没有拿到**可验证的证据**（工具被拒/报错/根本没跑）。这是**正常路径**：反思据此不判完成、
#: 改为请用户补充，不是异常。
EVIDENCE_NOT_VERIFIED: Final[str] = "EVIDENCE_NOT_VERIFIED"
#: 存在**未确认的副作用**（可能已经落盘但 Checkpoint 没确认）：不能自动重放，需要人工核对。
SIDE_EFFECT_UNCONFIRMED: Final[str] = "SIDE_EFFECT_UNCONFIRMED"

#: 会话索引里没有这个 id（应用层用例的前置校验）。
SESSION_NOT_FOUND: Final[str] = "SESSION_NOT_FOUND"
WORKSPACE_NOT_FOUND: Final[str] = "WORKSPACE_NOT_FOUND"

_ERROR_CATEGORY_OF: Final[dict[str, ErrorCategory]] = {
    SESSION_BUSY: "system",
    IDEMPOTENCY_CONFLICT: "system",
    RECOVERY_REQUIRED: "recovery",
    CONVERSATION_CONFLICT: "system",
    MODEL_AUTH_FAILED: "model",
    MODEL_CALL_FAILED: "model",
    RUN_INTERRUPTED: "recovery",
    MODEL_RATE_LIMITED: "model",
    MODEL_RETRY_EXHAUSTED: "model",
    MODEL_CAPABILITY_UNSUPPORTED: "model",
    MODEL_CONNECTION_REQUIRED: "configuration",
    MODEL_CONNECTION_NOT_FOUND: "configuration",
    MODEL_SETTINGS_CONFLICT: "configuration",
    MODEL_SETTINGS_INVALID: "configuration",
    SETTINGS_STORE_UNAVAILABLE: "configuration",
    SECURITY_POLICY_DENIED: "security",
    WORKSPACE_BOUNDARY_VIOLATION: "security",
    APPROVAL_EXPIRED: "approval",
    MEMORY_RETRIEVAL_FAILED: "system",
    RAG_RETRIEVAL_FAILED: "system",
    CONTEXT_COMPRESSION_FAILED: "system",
    MEMORY_WRITE_FAILED: "system",
    TOOL_CALL_FAILED: "tool",
    TOOL_CALL_BLOCKED: "security",
    APPROVAL_FLOW_FAILED: "approval",
    RUN_STAGE_FAILED: "system",
    RUN_DEGRADED: "system",
    EVIDENCE_NOT_VERIFIED: "system",
    SIDE_EFFECT_UNCONFIRMED: "recovery",
    SESSION_NOT_FOUND: "system",
    WORKSPACE_NOT_FOUND: "configuration",
    DIRECTORY_PICKER_UNAVAILABLE: "system",
}

#: 同一次提交在条件满足后能否原样重试（§15.5）。
_RETRYABLE: Final[frozenset[str]] = frozenset(
    {SESSION_BUSY, MODEL_RATE_LIMITED, MODEL_RETRY_EXHAUSTED, DIRECTORY_PICKER_UNAVAILABLE}
)

#: 条件说明。重试条件不是"现在就能重试"，页面按此决定是否显示按钮与如何措辞。
_RETRY_CONDITION: Final[dict[str, str]] = {
    SESSION_BUSY: "当前一轮结束，或取消并处理待审批/核对事项后可原样重发；请求不会排队。",
    IDEMPOTENCY_CONFLICT: "需要生成新的请求 id，或修正请求内容后重发。",
    RECOVERY_REQUIRED: "需要先人工核对上一次运行的记录，不能自动重试。",
    CONVERSATION_CONFLICT: "刷新会话地图并重新选择路径后，以新的请求提交；不能自动原样重试。",
    MODEL_AUTH_FAILED: "需要先更新该连接的密钥。",
    MODEL_CALL_FAILED: "检查模型连接后发起新一轮，不自动重放已执行的工具。",
    RUN_INTERRUPTED: "已核对并结束本轮，可以发起新一轮。",
    MODEL_RATE_LIMITED: "稍后可直接重发。",
    MODEL_RETRY_EXHAUSTED: "检查端点可达性或调小输出上限后重发。",
    MODEL_CAPABILITY_UNSUPPORTED: "需要改用支持该能力的连接，或去掉需要工具的任务。",
    MODEL_CONNECTION_REQUIRED: "先在设置里添加并测试一个可用连接。",
    MODEL_CONNECTION_NOT_FOUND: "刷新连接列表后重新选择；已停用的连接不会恢复。",
    MODEL_SETTINGS_CONFLICT: "刷新这条连接后重新提交修改，别让两个页面互相覆盖。",
    MODEL_SETTINGS_INVALID: "按提示改正字段后重新保存；上下文窗口必须大于输出上限。",
    SETTINGS_STORE_UNAVAILABLE: "需要先恢复本地设置库；当前会话仍使用原配置。",
    SECURITY_POLICY_DENIED: "需要调整请求本身，或由人工审批放行。",
    WORKSPACE_BOUNDARY_VIOLATION: "需要把操作限制在当前工作区内。",
    APPROVAL_EXPIRED: "需要重新发起请求并重新审批。",
    MEMORY_RETRIEVAL_FAILED: "重新发送即可重试；本轮回答没有引用记忆。",
    RAG_RETRIEVAL_FAILED: "确认工作区可读后重新发送即可重试。",
    CONTEXT_COMPRESSION_FAILED: "可缩短对话或调小保留窗口后重试。",
    MEMORY_WRITE_FAILED: "整理好要记住的内容后重新发送。",
    TOOL_CALL_FAILED: "修正工具参数或目标路径后重新发送；已产生的副作用不会自动重放。",
    TOOL_CALL_BLOCKED: "调整请求本身或改为在工作区内操作。",
    APPROVAL_FLOW_FAILED: "重新发起请求并重新确认。",
    RUN_STAGE_FAILED: "重新发送请求；本地日志里可用诊断编号定位细节。",
    RUN_DEGRADED: "直接重发即可；本轮已经用兜底结论给出了回答。",
    EVIDENCE_NOT_VERIFIED: "补充必要信息后重新发送；本轮结论没有工具证据支撑。",
    SIDE_EFFECT_UNCONFIRMED: "需要先人工核对上一次运行的记录，不能自动重试。",
    SESSION_NOT_FOUND: "刷新会话列表后重新选择；已删除的会话不会恢复。",
    WORKSPACE_NOT_FOUND: "刷新工作区列表后重新选择；新工作区可按路径添加。",
    DIRECTORY_PICKER_UNAVAILABLE: "确认本机桌面会话可用（远程桌面断开时弹不出窗口）后重新点击。",
}

ERROR_CODES: Final[tuple[str, ...]] = tuple(_ERROR_CATEGORY_OF)


def category_of(code: str) -> ErrorCategory | None:
    """已知错误码的分类；未知码返回 `None`（前向兼容，不抛异常）。"""
    return _ERROR_CATEGORY_OF.get(code)


def is_retryable(code: str) -> bool:
    """该错误码是否允许原样重试；未知码一律按不可重试处理。"""
    return code in _RETRYABLE


def retry_condition_of(code: str) -> str | None:
    """重试/继续的条件说明；未知码返回 `None`。"""
    return _RETRY_CONDITION.get(code)


class ErrorView(BaseModel):
    """页面可见的错误 DTO。

    `message` / `hint` / `detail` 都是**已脱敏的展示文本**：供应商原文、密钥与绝对私人路径
    不得进入这里（§15.5、§15.8）。原始异常只进本地日志。
    """

    model_config = ConfigDict(extra="ignore")

    code: str = Field(min_length=1)
    category: ErrorCategory
    severity: Literal["error", "warning"] = "error"
    title: str = Field(min_length=1)
    message: str = Field(min_length=1)
    hint: str = Field(min_length=1)
    retryable: bool
    retry_condition: str | None = None
    #: 不可重试时的具体原因；与 `retry_condition` 一样是文案，不是状态机。
    blocked_reason: str | None = None
    #: 安全的诊断关联 id（本地日志可查），不含密钥与私人路径。
    diagnostic_id: str = Field(min_length=1)
    occurred_at: datetime
    #: 稳定的来源定位（例如 `run` / `session` / `model_call`），用于页面分组。
    source: str | None = None

    @field_validator("occurred_at")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        """时间必须带时区：同一输入重复序列化要字节一致（§15.4）。"""
        if value.tzinfo is None:
            msg = "occurred_at 必须带时区（使用 timezone.utc）"
            raise ValueError(msg)
        return value.astimezone(timezone.utc)

    @field_validator("title", "message", "hint", "retry_condition", "blocked_reason", "source")
    @classmethod
    def _reject_unsafe_text(cls, value: str | None) -> str | None:
        """展示文本 fail-closed：出现密钥样式或私人绝对路径就拒绝构造。

        调用方要先把异常原文脱敏（`local_agent.config.redact_secrets`）再填进来；这里不"顺手抹掉"，
        因为悄悄改写错误信息比直接报错更危险。
        """
        if value is None:
            return None
        for pattern in _FORBIDDEN_IN_TEXT:
            if pattern.search(value):
                msg = f"错误展示文本包含密钥样式或私人绝对路径：{pattern.pattern}"
                raise ValueError(msg)
        return value

    @field_validator("diagnostic_id")
    @classmethod
    def _safe_diagnostic_id(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value):
            raise ValueError("诊断编号包含不安全字符")
        for pattern in _FORBIDDEN_IN_TEXT:
            if pattern.search(value):
                raise ValueError("诊断编号包含密钥样式或私人绝对路径")
        return value

    @classmethod
    def for_code(
        cls,
        code: str,
        *,
        title: str,
        message: str,
        hint: str,
        diagnostic_id: str,
        occurred_at: datetime,
        severity: Literal["error", "warning"] | None = None,
        source: str | None = None,
        blocked_reason: str | None = None,
    ) -> ErrorView:
        """按错误码填充分类与可重试性，避免各处各写一份。

        未知错误码归入 `system` 且按不可重试处理——不确定时不得承诺可重试。
        """
        category = category_of(code) or "system"
        retryable = is_retryable(code)
        if severity is None:
            severity = "warning" if category in {"model", "system"} and retryable else "error"
        return cls(
            code=code,
            category=category,
            severity=severity,
            title=title,
            message=message,
            hint=hint,
            retryable=retryable,
            retry_condition=retry_condition_of(code),
            blocked_reason=None if retryable else blocked_reason,
            diagnostic_id=diagnostic_id,
            occurred_at=occurred_at,
            source=source,
        )


def diagnostic_id_for(code: str, *parts: str) -> str:
    """安全的诊断关联编号：`CODE:<稳定摘要>`。

    不把 `session_id` / `request_id` 原样透出（它们是内部标识），但仍可复现：本地日志里用同样的
    拼接方式算一次摘要即可与页面上的编号对上。
    """
    material = "|".join(part for part in parts if part)
    digest = hashlib.sha256(material.encode()).hexdigest()[:12]
    return f"{code}:{digest}"


class ApplicationError(Exception):
    """带公开错误 DTO 的应用层失败。

    调用方（API / 页面）据此渲染并决定是否重试，**不要解析异常文本**。
    """

    def __init__(self, view: ErrorView) -> None:
        super().__init__(f"{view.code}: {view.message}")
        self.view = view

    @property
    def code(self) -> str:
        return self.view.code


def directory_picker_unavailable(*, occurred_at: datetime) -> ApplicationError:
    """宿主没能弹出目录选择器：这是**宿主能力**故障，不是"你选的目录有问题"。

    文案里不带任何路径、也不带子进程原文（§15.8）——具体原因只进本地日志。
    """
    return ApplicationError(
        ErrorView.for_code(
            DIRECTORY_PICKER_UNAVAILABLE,
            title="打不开目录选择器",
            message="没能弹出系统的文件夹选择窗口：这次没有选中任何目录，工作区没有被改变。",
            hint="重新点一次「添加工作区」即可；若反复打不开，先确认本机桌面会话可用。",
            diagnostic_id=diagnostic_id_for(DIRECTORY_PICKER_UNAVAILABLE, "pick"),
            occurred_at=occurred_at,
        )
    )


def session_busy(*, session_id: str, request_id: str, occurred_at: datetime) -> ApplicationError:
    """§15.2：同一会话已有执行在跑，第二个不同 id 的执行请求**不排队**。"""
    return ApplicationError(
        ErrorView.for_code(
            SESSION_BUSY,
            title="该会话已有正在运行的一轮",
            message="本次提交未被接受：同一会话同一时刻只允许一轮运行，请求不会排队。",
            hint="等待当前一轮结束；也可在会话中取消任务并处理待审批或核对事项。取消不撤销已经发生的副作用。",
            diagnostic_id=diagnostic_id_for(SESSION_BUSY, session_id, request_id),
            occurred_at=occurred_at,
            source="session",
        )
    )


def idempotency_conflict(*, session_id: str, request_id: str, occurred_at: datetime) -> ApplicationError:
    """§15.2：相同请求 id 配不同参数。"""
    return ApplicationError(
        ErrorView.for_code(
            IDEMPOTENCY_CONFLICT,
            title="同一请求标识的内容不一致",
            message="这个请求标识此前已用于另一次提交，且内容不同，因此本次提交被拒绝，未执行任何工具。",
            hint="先在历史里确认上一次提交的结果，再把它当作新的请求发送；不要改写后沿用同一个请求标识重发。",
            diagnostic_id=diagnostic_id_for(IDEMPOTENCY_CONFLICT, session_id, request_id),
            occurred_at=occurred_at,
            source="session",
            blocked_reason="内容不一致，必须先确认上一次的结果，不能直接重发。",
        )
    )


def recovery_required(
    *, session_id: str, request_id: str, detail: str, occurred_at: datetime
) -> ApplicationError:
    """§15.2：台账仍为"执行中"且**无法证明终态**——绝不自动重提或再次执行工具。"""
    return ApplicationError(
        ErrorView.for_code(
            RECOVERY_REQUIRED,
            title="需要先完成上一次运行的恢复",
            message=f"无法证明上一次运行的终态（{detail}），因此不会自动重提同一输入或再次执行工具。",
            hint="人工核对上一次运行的记录：确认没有未收尾的副作用后，再把它作为新的请求重新提交。",
            diagnostic_id=diagnostic_id_for(RECOVERY_REQUIRED, session_id, request_id),
            occurred_at=occurred_at,
            source="session",
            blocked_reason="需要人工核对上一次运行的记录；不会自动重提或再次执行工具。",
        )
    )


def session_not_found(*, session_id: str, occurred_at: datetime) -> ApplicationError:
    """会话索引里没有这个 id。"""
    return ApplicationError(
        ErrorView.for_code(
            SESSION_NOT_FOUND,
            title="找不到该会话",
            message="这个会话不在当前工作区的会话索引里，因此没有执行任何操作。",
            hint="刷新会话列表后重新选择；已删除的会话不会恢复。",
            diagnostic_id=diagnostic_id_for(SESSION_NOT_FOUND, session_id),
            occurred_at=occurred_at,
            source="session",
        )
    )


def workspace_not_found(*, workspace_id: str, occurred_at: datetime) -> ApplicationError:
    """工作区标识未出现在已使用列表中；不接受客户端猜出的路径映射。"""
    return ApplicationError(
        ErrorView.for_code(
            WORKSPACE_NOT_FOUND,
            title="找不到该工作区",
            message="这个工作区不在当前的已使用列表里，没有切换会话。",
            hint="刷新工作区列表后重新选择；新工作区请通过路径选择入口添加。",
            diagnostic_id=diagnostic_id_for(WORKSPACE_NOT_FOUND, workspace_id),
            occurred_at=occurred_at,
            source="workspace",
        )
    )


def workspace_unusable(*, occurred_at: datetime) -> ApplicationError:
    """用户给出的工作区路径不可用（非绝对路径 / 不存在 / 不是目录 / 不可访问）。

    复用 `WORKSPACE_NOT_FOUND`，**不新增错误码**。文案里刻意不带那条路径：`ErrorView` 的展示
    文本 fail-closed 拒绝私人绝对路径（§15.8），而这里正是用户刚输入的那条路径；具体是哪一条
    由调用方（页面 / 前端）自己回显——它本来就知道用户填了什么。
    """
    return ApplicationError(
        ErrorView.for_code(
            WORKSPACE_NOT_FOUND,
            title="工作区路径不可用",
            message="这个工作区路径不可用：需要是不含 ~ 的绝对路径，且目录存在、当前进程可读可进入。",
            hint="先用文件管理器确认目录存在，再填写它的绝对路径；相对路径与 ~ 都会被拒绝。",
            diagnostic_id=diagnostic_id_for(WORKSPACE_NOT_FOUND, "workspace-path"),
            occurred_at=occurred_at,
            source="workspace",
        )
    )


def approval_not_actionable(
    *, session_id: str, approval_id: str, occurred_at: datetime
) -> ApplicationError:
    """审批已失效或与当前挂起的请求不匹配——此时 resume 会答非所问。"""
    return ApplicationError(
        ErrorView.for_code(
            APPROVAL_EXPIRED,
            title="该审批已失效",
            message="这个审批请求已经不在等待决策，因此没有执行任何操作。",
            hint="重新发起请求并重新确认；不要对已失效的审批重复提交决定。",
            diagnostic_id=diagnostic_id_for(APPROVAL_EXPIRED, session_id, approval_id),
            occurred_at=occurred_at,
            source="approval",
        )
    )


# ---- 模型连接与设置（§6.8、§15.9）------------------------------------------


def model_connection_required(*, session_id: str, occurred_at: datetime) -> ApplicationError:
    """§15.9：还没有任何可用连接时的"初始设置模式"——模型调用被明确拒绝，不是静默失败。"""
    return ApplicationError(
        ErrorView.for_code(
            MODEL_CONNECTION_REQUIRED,
            title="还没有可用的模型连接",
            message="本次提交没有执行：当前还没有配置好的聊天模型连接，因此不会调用任何模型。",
            hint="先在设置里添加一条连接，点一次“测试连接”确认可用，再回来重新发送这条内容。",
            diagnostic_id=diagnostic_id_for(MODEL_CONNECTION_REQUIRED, session_id),
            occurred_at=occurred_at,
            source="settings",
        )
    )


def model_connection_not_found(*, connection_id: str, occurred_at: datetime) -> ApplicationError:
    """设置里没有这条连接（已停用的连接按不存在处理）。"""
    return ApplicationError(
        ErrorView.for_code(
            MODEL_CONNECTION_NOT_FOUND,
            title="找不到这条连接",
            message="这条模型连接不在当前的连接列表里，因此没有改动任何设置。",
            hint="刷新连接列表后重新选择；已停用的连接不会恢复，需要的话请新建一条。",
            diagnostic_id=diagnostic_id_for(MODEL_CONNECTION_NOT_FOUND, connection_id),
            occurred_at=occurred_at,
            source="settings",
        )
    )


def model_settings_conflict(*, connection_id: str, occurred_at: datetime) -> ApplicationError:
    """写入携带的版本号与当前版本不一致（两个页面同时编辑同一条连接）。"""
    return ApplicationError(
        ErrorView.for_code(
            MODEL_SETTINGS_CONFLICT,
            title="这条连接已被改动",
            message="你的改动基于一个旧版本，因此没有覆盖当前设置。",
            hint="刷新这条连接拿到最新版本，再把你的改动重新提交一次。",
            diagnostic_id=diagnostic_id_for(MODEL_SETTINGS_CONFLICT, connection_id),
            occurred_at=occurred_at,
            source="settings",
        )
    )


def preferences_conflict(*, occurred_at: datetime) -> ApplicationError:
    """偏好使用同一设置版本冲突码，但展示应用设置自己的文案。"""
    return ApplicationError(ErrorView.for_code(
        MODEL_SETTINGS_CONFLICT,
        title="应用设置已被另一个页面改过",
        message="设置版本已更新，本次修改没有覆盖已保存的内容。",
        hint="重新读取最新设置后再修改；当前草稿仍保留。",
        diagnostic_id=diagnostic_id_for(MODEL_SETTINGS_CONFLICT, "app-preferences"),
        occurred_at=occurred_at, source="settings",
    ))


def selection_conflict(*, scope_id: str, occurred_at: datetime) -> ApplicationError:
    """写入的**选择**已被别的页面改过（§6.1）。

    与 `model_settings_conflict` 同一个错误码（都是"基于旧版本写入"），但文案不同：
    这一条说的是"选中的模型被别人换了"，用户要做的是**刷新后重选**，而不是重提编辑。
    """
    return ApplicationError(
        ErrorView.for_code(
            MODEL_SETTINGS_CONFLICT,
            title="模型选择已被另一个页面改过",
            message="当前选中的模型和你看到的不一样，所以这次选择没有覆盖它。",
            hint="刷新页面拿到最新的选择，再重新选一次。",
            diagnostic_id=diagnostic_id_for(MODEL_SETTINGS_CONFLICT, scope_id),
            occurred_at=occurred_at,
            source="settings",
        )
    )


def model_settings_invalid(*, detail: str, occurred_at: datetime) -> ApplicationError:
    """连接字段（或版本号）没通过服务端校验。

    `detail` 必须是**脱敏后的规则说明**：说的是"哪条规则不满足、怎么改"，不是请求原文——
    用户填的端点可能带内网主机名，密钥更不该回显（§15.8）。
    """
    return ApplicationError(
        ErrorView.for_code(
            MODEL_SETTINGS_INVALID,
            title="这条连接没有通过校验",
            message=f"没有保存：{detail}",
            hint="按上面的说明改正字段后重新保存；不确定的值宁可留空，也不要填一个猜的。",
            diagnostic_id=diagnostic_id_for(MODEL_SETTINGS_INVALID),
            occurred_at=occurred_at,
            source="settings",
        )
    )


def model_capability_unsupported(
    *, session_id: str, connection_id: str, capability: str, occurred_at: datetime
) -> ApplicationError:
    """§6.8：任务需要的能力这条连接没有——明确拒绝，**不静默切回别的模型或降安全策略**。"""
    return ApplicationError(
        ErrorView.for_code(
            MODEL_CAPABILITY_UNSUPPORTED,
            title="当前连接缺少这项能力",
            message=f"这次任务需要“{capability}”，而这条连接没有通过对应能力检测，因此没有执行任何工具。",
            hint="换一条已验证该能力的连接，或去掉需要它的步骤；本地不会自动改用别的模型。",
            diagnostic_id=diagnostic_id_for(MODEL_CAPABILITY_UNSUPPORTED, session_id, connection_id),
            occurred_at=occurred_at,
            source="model",
        )
    )


def model_key_missing(*, connection_id: str, occurred_at: datetime) -> ApplicationError:
    """这条连接需要密钥，但取不到（没录入、临时模式的进程重启了、或环境变量没了）。

    复用 `MODEL_AUTH_FAILED`：它的重试条件正是"需要先更新该连接的密钥"，不新增错误码。
    """
    return ApplicationError(
        ErrorView.for_code(
            MODEL_AUTH_FAILED,
            title="这条连接还没有可用的密钥",
            message="这条连接需要密钥，但当前取不到，因此没有发起请求。",
            hint="在设置里给这条连接填入密钥并保存；若刚刚重启过服务，临时模式的密钥需要重新录入。",
            diagnostic_id=diagnostic_id_for(MODEL_AUTH_FAILED, connection_id),
            occurred_at=occurred_at,
            source="model",
        )
    )


def permission_grant_invalid(*, detail: str, occurred_at: datetime) -> ApplicationError:
    """新增外部路径授权被拒（T38）。

    复用 ``MODEL_SETTINGS_INVALID``：它表达的是"设置写入没通过服务端校验"，不新增稳定错误码
    （错误码表是冻结契约，加一个码要同时动契约导出与前端）。``detail`` 是**不含路径**的规则说明：
    展示文本 fail-closed，``E:\\...`` 这种私人绝对路径会被 `ErrorView` 直接挡下。
    """
    return ApplicationError(
        ErrorView.for_code(
            MODEL_SETTINGS_INVALID,
            title="这条外部路径授权没有通过校验",
            message=f"没有保存：{detail}",
            hint="换一个可授权的目录（不能是根目录、主目录、Git 元数据或系统目录）。",
            diagnostic_id=diagnostic_id_for(MODEL_SETTINGS_INVALID, detail),
            occurred_at=occurred_at,
            source="settings",
        )
    )


def settings_store_unavailable(*, detail: str, occurred_at: datetime) -> ApplicationError:
    """§15.9：设置库不可用时要说明降级原因，而不是让页面看到一堆内部异常。

    ``detail`` 只放安全的短语（例如"目录不可写"），**不能**带绝对路径——展示文本 fail-closed。
    """
    return ApplicationError(
        ErrorView.for_code(
            SETTINGS_STORE_UNAVAILABLE,
            title="本地模型设置不可用",
            message=f"模型连接设置读不到也存不下（{detail}）。",
            hint="检查设置库所在目录是否可写后重启服务；设置恢复前，会话仍按原有配置运行。",
            diagnostic_id=diagnostic_id_for(SETTINGS_STORE_UNAVAILABLE, detail),
            occurred_at=occurred_at,
            source="settings",
        )
    )
