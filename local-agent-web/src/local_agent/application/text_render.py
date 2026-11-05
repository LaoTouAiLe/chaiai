"""展示文本的脱敏、有界裁切与取值小工具（2026-11-05 从 `projection` 拆出）。

**为什么单独一个模块**：工具卡片的渲染（:mod:`local_agent.application.tool_outcome_views`）
与投影都要用这几个函数。放在 `projection.py` 里会让工具视图反向依赖投影模块，形成环；
拆出来之后依赖方向是 `tool_outcome_views → text_render`、`projection → text_render`。

这里的方法**只做展示层的取整与脱敏**，不做业务判定：脱敏规则与 `security` 侧同一套
（`redact_secrets`），密钥样式串按 `_KEY_LIKE` 抹掉。
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Final

from local_agent.config import redact_secrets

#: 密钥**样式**（不只是已知密钥值）：即使调用方没把环境里的密钥传进来，这里也要抹掉，
#: 否则 `ErrorView` 的 fail-closed 校验会让整份快照构造失败——那比少显示一段文字更糟。
_KEY_LIKE = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}")
_MASK = "***"

#: 工具详情的展示上限（§15.8）：超长内容有界折叠，完整正文留在本地工具事件里，不进入页面。
TOOL_DETAIL_TEXT_CHARS: Final[int] = 4000
TOOL_DETAIL_STREAM_CHARS: Final[int] = 2000
TOOL_DETAIL_PATHS: Final[int] = 200
#: 网页检索卡片里列出的前几条标题（2026-10-26）：卡片只回答"命中了什么"，完整来源由
#: `projection._web_sources` 另行渲染——两边都列全量会让同一批结果在页面上出现两遍。
WEB_DETAIL_HITS: Final[int] = 5


def sanitize_text(text: str, secrets: Iterable[str] = ()) -> str:
    """展示文本仅脱敏已知密钥与密钥样式串；路径原样保留。

    节点侧的 `error_events` 文案是给内部看的，进入页面之前必须过这一层（§15.5、§15.8）。
    这里**不静默吞掉**别的内容，只做这两类替换，其余原样保留。
    """
    cleaned = redact_secrets(text, list(secrets))
    return _KEY_LIKE.sub(_MASK, cleaned)


def _bounded(text: str, limit: int) -> tuple[str, bool]:
    """有界正文：超过 `limit` 就截断并如实说"这里还有更多"。"""
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def _int_of(value: Any) -> int | None:
    """整数取值；`bool` 是 `int` 的子类，但"真/假"不是数字，不能当计数用。"""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None


def _optional_sanitized(value: Any, secrets: Iterable[str]) -> str | None:
    """非空字符串 → 已脱敏文本；空值 → `None`（不伪造空串）。"""
    text = _optional_str(value)
    return sanitize_text(text, secrets) if text is not None else None


def _as_mapping(value: Any) -> Mapping[str, Any] | None:
    """单个 State 值 → 映射视图（同样兼容 dataclass）。"""
    if isinstance(value, Mapping):
        return value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {field.name: getattr(value, field.name) for field in dataclasses.fields(value)}
    if hasattr(value, "__dict__"):
        return {str(key): item for key, item in vars(value).items()}
    return None


def _mappings(value: Any) -> list[Mapping[str, Any]]:
    """把 State 里的序列字段统一成**映射视图**。

    State 里同时存在 TypedDict（`ErrorEvent` / `ToolResultRecord` / `ApprovalDecision`…，运行时就是 dict）
    与 dataclass（`PlanStep` / `ToolEvent` / `EvidenceReference` / `MemoryCandidate`）。投影只按字段名读，
    不关心载体，所以这里统一成"能 `.get` 的东西"——**不能只认 dict**：实测 `tool_events` 是 dataclass 列表，
    只按 dict 读会让工具行全部退回 `pending`（页面把已完成的工具显示成等待中）。
    """
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    items: list[Mapping[str, Any]] = []
    for item in value:
        normalized = _as_mapping(item)
        if normalized is not None:
            items.append(normalized)
    return items
