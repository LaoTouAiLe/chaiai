"""记忆候选的校验与落库映射（架构 §10.4，开发文档 T18）。

写入链路分四段，本模块负责第三段（确定性映射）：

1. 触发事件（任务完成 / 用户明确要求记住等）只产生 ``memory_write_intent``，不能直接写库；
2. :func:`~local_agent.reflection.reflector.reflect` 把它验证成
   :class:`~local_agent.graph.state.MemoryCandidate`；
3. **本模块**把候选映射成 :class:`WriteIntent`（落哪个库、什么作用域、什么载荷）；
4. :mod:`local_agent.memory.writer` 按计划执行写入。

映射契约（T18 定稿，校验方与写入方必须一致）：

- ``conversation_summary`` → 中期库。跨会话召回用；与 T16 的 State 会话摘要**不是同一份数据**
  （那份只用于当前会话窗口，不落库）。
- ``task_fragment`` → 中期库；任务完成且机械证据 ``verified`` 时**同时**写长期 ``task_conclusion``。
- ``entity_fact`` → 长期 ``facts``：``subject`` 是实体名，``content`` 是规范 JSON
  ``{"entity_type": ..., "key": ..., "value": ...}``。``subject`` 缺失时按 ``entity_type``
  **由代码推导**（``user`` → 当前逻辑用户；``project`` / ``workspace`` → 当前工作区目录名），
  推不出来才拒绝——模型漏填一个字段不该让整条记忆丢掉（真实模型实证：用户说"请记住：我个人习惯
  用 pytest"，模型给出了正确的 ``entity_type`` / ``key`` / ``value`` 却没给 ``subject``）。
  推导过的动作带 ``note``，决策轨迹里能看出实体名是代码补的而不是模型给的。

作用域：自动写入**只允许 ``project`` / ``user``**（不自动写 ``global``），按"已验证意图 + 候选类型
+ 显式授权"**机械推导**（架构 §10.4：作用域不属于候选，也不新增候选字段）：

- ``entity_fact`` 且规范 JSON 里的 ``entity_type == "user"``，**并且配置显式打开**
  ``memory.long_term.allow_user_scope`` → ``user`` 作用域（跨项目的个人偏好、身份信息）；
- 其它 ``entity_type``、其它候选类型，或未打开该开关 → ``project`` 作用域。

为什么用户级还需要一个开关：``entity_type`` 是**模型生成的 JSON 值**，而 ``user`` 作用域意味着
"跨项目可见"。只凭它决定隔离边界时，一次误分类（把项目事实写成 ``entity_type="user"``）就会让
这条事实被别的项目召回（复审探针复现）。跨项目共享因此是**显式、可核验的配置授权**，默认保守落
项目级——记忆仍然保存，只是不跨项目。拿不到作用域键时**拒绝写入**。

（历史背景：本模块先按"project 优先、user 兜底"推导，导致 ``user`` 永远不可达、个人偏好被一律
项目化；改成按 ``entity_type`` 推导后 user 可达，却把隔离边界交给了模型输出；最终加上
``allow_user_scope`` 开关，兼顾"可达"与"默认不泄漏"。）

写入门槛：**不设 confidence / importance 下限**，延续 T15 的口径（阈值只做召回过滤，不阻止
写入）。这里只校验类型合法、内容非空、来源事件非空，以及两个数值有限且在 ``[0, 1]``。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

from local_agent.graph.state import MemoryCandidate
from local_agent.memory.schemas import ORIGINS
from local_agent.scope import (
    SCOPE_PROJECT,
    SCOPE_USER,
    MemoryScope,
    logical_user_id,
    project_id_for,
    scope_key_for,
)

#: 一条候选可以落到哪里。
WriteTarget = Literal["medium_term", "long_term_fact", "long_term_conclusion"]

#: 允许自动写入的作用域，按推导优先级排列（``global`` / ``workspace`` 不自动写）。
AUTO_WRITE_SCOPES: tuple[MemoryScope, ...] = (SCOPE_PROJECT, SCOPE_USER)

#: ``entity_fact`` 的规范 JSON 必须提供的键。
ENTITY_FACT_KEYS: tuple[str, ...] = ("entity_type", "key", "value")

#: ``entity_type`` 取这个值时写**用户级**记忆（跨项目成立的个人偏好 / 身份信息）；其余一律项目级。
USER_ENTITY_TYPE = "user"

#: 候选的三种类型（架构 §4.3）。
MEMORY_TYPES: tuple[str, ...] = ("conversation_summary", "task_fragment", "entity_fact")


@dataclass(frozen=True, kw_only=True)
class EntityFactPayload:
    """``entity_fact`` 的解析结果。"""

    entity_type: str
    key: str
    value: Any


@dataclass(frozen=True, kw_only=True)
class WriteIntent:
    """一条候选对应的**落库动作**：目标库、作用域与载荷。"""

    candidate: MemoryCandidate
    target: WriteTarget
    scope: MemoryScope
    scope_key: str
    payload: EntityFactPayload | None = None
    #: 计划阶段的说明（例如"实体名由系统推导"）。写入方会把它并进结果的 ``reason``，
    #: 于是决策轨迹里能看出这条记忆的哪些部分是代码补的，而不是模型给的。
    note: str | None = None

    @property
    def source_event_ids(self) -> tuple[str, ...]:
        """来源事件（写入幂等键）。"""
        return tuple(self.candidate.source_event_ids)


@dataclass(frozen=True, kw_only=True)
class RejectedCandidate:
    """被拒绝的候选与原因：调用方据此记决策/错误事件，不能静默丢弃。"""

    candidate: MemoryCandidate
    reason: str


def plan_writes(
    candidate: MemoryCandidate,
    *,
    workspace_root: Path,
    user_id: str | None = None,
    project_id: str | None = None,
    task_verified: bool = False,
    user_scope_allowed: bool = False,
) -> tuple[WriteIntent, ...] | RejectedCandidate:
    """把一条候选映射成落库动作；不合法时返回 :class:`RejectedCandidate`。

    Args:
        candidate: 反思验证后的候选（架构 §4.3）。
        workspace_root: 工作区根目录，用于推导项目作用域。
        user_id: 逻辑用户；``None`` 时按 ``local`` 处理。
        project_id: 项目标识；省略时按 ``workspace_root`` 现算（与会话索引同一规则）。
        task_verified: 本轮机械证据是否 ``verified``，决定 ``task_fragment`` 是否同时写长期结论。
        user_scope_allowed: ``memory.long_term.allow_user_scope``——是否允许写**用户级**（跨项目可见）
            作用域。默认 ``false``：即使模型把 ``entity_type`` 写成 ``user`` 也落项目级，
            避免一次误分类就让项目事实跨项目泄漏。

    Returns:
        落库动作（可能多条：``task_fragment`` 在证据充分时同时写中期与长期）；
        校验失败时返回 :class:`RejectedCandidate`。
    """
    reason = _static_rejection(candidate)
    if reason is not None:
        return RejectedCandidate(candidate=candidate, reason=reason)
    payload: EntityFactPayload | None = None
    if candidate.memory_type == "entity_fact":
        payload = parse_entity_fact(candidate.content)
        if payload is None:
            return RejectedCandidate(
                candidate=candidate,
                reason=(
                    "entity_fact 的 content 必须是规范 JSON "
                    '{"entity_type": ..., "key": ..., "value": ...}'
                ),
            )
    subject = candidate.subject.strip()
    note: str | None = None
    if candidate.memory_type == "entity_fact" and payload is not None and not subject:
        # 模型经常漏填 subject（真实模型实证：用户说"请记住：我个人习惯用 pytest"，模型给出了
        # 正确的 entity_type/key/value 却没给 subject），而 user / project 的实体名在本机是确定的：
        # 为这点信息拒绝整条记忆，用户会觉得"明明说了记住却没记住"。
        derived = _derive_subject(
            entity_type=payload.entity_type, workspace_root=workspace_root, user_id=user_id
        )
        if derived is None:
            return RejectedCandidate(
                candidate=candidate,
                reason=(
                    "entity_fact 缺少 subject（实体名），且 entity_type "
                    f"{payload.entity_type!r} 推导不出实体名，请在候选里给出 subject"
                ),
            )
        subject, note = derived
    effective = replace(candidate, subject=subject) if subject else candidate
    resolved = _resolve_scope(
        payload=payload,
        workspace_root=workspace_root,
        user_id=user_id,
        project_id=project_id,
        user_scope_allowed=user_scope_allowed,
    )
    if resolved is None:
        return RejectedCandidate(
            candidate=candidate,
            reason="无法确定 user/project 作用域（自动写入不写 global），本轮不写入记忆",
        )
    scope, scope_key = resolved
    if payload is not None:
        return (
            WriteIntent(
                candidate=effective,
                target="long_term_fact",
                scope=scope,
                scope_key=scope_key,
                payload=payload,
                note=note,
            ),
        )
    intents = [
        WriteIntent(candidate=effective, target="medium_term", scope=scope, scope_key=scope_key)
    ]
    if effective.memory_type == "task_fragment" and task_verified:
        intents.append(
            WriteIntent(
                candidate=effective,
                target="long_term_conclusion",
                scope=scope,
                scope_key=scope_key,
            )
        )
    return tuple(intents)


#: ``entity_fact`` 缺少 ``subject`` 时可按 ``entity_type`` 推导实体名的类型。
#: 只覆盖本机**确定可知**的实体名；其它类型推不出来就拒绝（宁可少写，也不写一个猜出来的实体名）。
_DERIVABLE_ENTITY_TYPES: tuple[str, ...] = ("user", "project", "workspace")


def _derive_subject(
    *, entity_type: str, workspace_root: Path, user_id: str | None
) -> tuple[str, str] | None:
    """按 ``entity_type`` 推导实体名，返回 ``(实体名, 说明)``；推导不出时返回 ``None``。"""
    kind = entity_type.strip().lower()
    if kind not in _DERIVABLE_ENTITY_TYPES:
        return None
    if kind == USER_ENTITY_TYPE:
        return (
            logical_user_id(user_id),
            f"实体名由系统推导：entity_type={entity_type} → 当前逻辑用户",
        )
    name = Path(workspace_root).name.strip()
    if not name:
        return None
    return name, f"实体名由系统推导：entity_type={entity_type} → 当前工作区目录名 {name}"


def parse_entity_fact(content: str) -> EntityFactPayload | None:
    """解析 ``entity_fact`` 的规范 JSON；解析失败或键不合规时返回 ``None``。"""
    try:
        parsed = json.loads(content)
    except ValueError:
        return None
    if not isinstance(parsed, dict) or any(key not in parsed for key in ENTITY_FACT_KEYS):
        return None
    entity_type = parsed["entity_type"]
    key = parsed["key"]
    if not isinstance(entity_type, str) or not entity_type.strip():
        return None
    if not isinstance(key, str) or not key.strip():
        return None
    return EntityFactPayload(
        entity_type=entity_type.strip(), key=key.strip(), value=parsed["value"]
    )


def _static_rejection(candidate: MemoryCandidate) -> str | None:
    """与作用域无关的静态校验；通过时返回 ``None``。"""
    if candidate.origin not in ORIGINS:
        # 架构 §16.2：origin 是闭集且没有默认值。"判定不出来"绝不允许落成 owner——那是自动注入
        # 的通行证。这里宁可拒绝整条候选（会被记成 RejectedCandidate 并说明原因），也不猜。
        return (
            f"未知的 origin：{candidate.origin!r}（合法取值：{'、'.join(ORIGINS)}；"
            "见架构 §16.2）"
        )
    if candidate.memory_type not in MEMORY_TYPES:
        return f"未知的 memory_type：{candidate.memory_type!r}"
    if not str(candidate.content).strip():
        return "候选内容为空，不写入记忆"
    if not candidate.source_event_ids:
        return "候选没有来源事件（source_event_ids），按架构 §10.4 不得写入"
    for name, value in (
        ("importance", candidate.importance),
        ("confidence", candidate.confidence),
    ):
        if not math.isfinite(float(value)) or not 0.0 <= float(value) <= 1.0:
            return f"{name} 必须是 [0, 1] 内的有限数值，实际是 {value!r}"
    return None


def _resolve_scope(
    *,
    payload: EntityFactPayload | None,
    workspace_root: Path,
    user_id: str | None,
    project_id: str | None,
    user_scope_allowed: bool,
) -> tuple[MemoryScope, str] | None:
    """按"已验证意图 + 显式授权"机械推导作用域；拿不到时返回 ``None``。

    ``user`` 需要**两个条件同时满足**：``entity_type == "user"``（模型的意图信号）与
    ``user_scope_allowed``（运维的显式授权）——只凭模型输出的 JSON 值决定跨项目隔离边界太危险
    （复审探针：项目事实带 ``entity_type="user"`` 就被映射成用户级）。``project`` 分支用显式
    ``project_id`` 或按工作区现算，两者都拿不到才拒绝。
    """
    if payload is not None and payload.entity_type == USER_ENTITY_TYPE and user_scope_allowed:
        return SCOPE_USER, scope_key_for(SCOPE_USER, user_id=logical_user_id(user_id))
    resolved_project = (project_id or "").strip() or project_id_for(workspace_root)
    if not resolved_project:
        return None
    return SCOPE_PROJECT, scope_key_for(SCOPE_PROJECT, project_id=resolved_project)


__all__ = [
    "AUTO_WRITE_SCOPES",
    "ENTITY_FACT_KEYS",
    "MEMORY_TYPES",
    "EntityFactPayload",
    "RejectedCandidate",
    "WriteIntent",
    "WriteTarget",
    "parse_entity_fact",
    "plan_writes",
]
