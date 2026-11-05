"""网页检索与子 Agent 委派的结构化详情（2026-11-05 从 `projection` 拆出）。

这两个工具的载荷比较宽（检索来源列表、每路子任务的结局与工具链），渲染逻辑放在投影主文件里
会把 `_tool_outcome` 撑过函数行数与复杂度上限。拆出来之后，投影只做"按工具名分派"。

两者共用同一套展示口径：**只在形状可识别时**产出结果，认不出就返回 `None`，由调用方退回
通用渲染器——不能把一段任意文本当成已知工具的结构。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Final

from local_agent import delegation_live
from local_agent.application.models import (
    DelegationChildView,
    DelegationToolStepView,
    ToolOutcomeView,
)
from local_agent.application.text_render import (
    TOOL_DETAIL_TEXT_CHARS,
    WEB_DETAIL_HITS,
    _bounded,
    _int_of,
    _mappings,
    _optional_sanitized,
    sanitize_text,
)

#: 委派链路里不展示的工具：`task_control` 是内部收尾信号，不是调查动作。
_HIDDEN_DELEGATION_STEPS: Final[frozenset[str]] = frozenset({"task_control"})


def web_outcome(
    tool_name: str, payload: Mapping[str, Any], secrets: Iterable[str], note: str | None
) -> ToolOutcomeView | None:
    """`web_search` / `web_fetch` 的卡片。

    此前它们不在识别表里，卡片只剩一句"该结果格式暂无可展示的详情"——而工具其实回了不少东西。
    这里**只在形状可识别时**产出：失败结果的载荷是 `{"error": ...}` 或残缺对象，必须让它走
    `None` 分支，由通用渲染器配 `_public_error_text` 说明真实原因；否则会被"0 条来源"盖掉。
    所以先按关键字段是否存在 + 类型对不对把关，不按有没有内容猜。
    """
    if tool_name == "web_search":
        hits_raw = payload.get("results")
        if not isinstance(hits_raw, list):
            return None
        hits = _mappings(hits_raw)
        provider = sanitize_text(str(payload.get("provider") or ""), secrets).strip()
        summary = f"{len(hits)} 条来源"
        if provider:
            summary = f"{summary} · 后端 {provider}"
        # 变量名不能叫 `lines`：`read_file` 分支在同一个函数里用这个名字存"总行数"。
        hit_lines: list[str] = []
        for item in hits[:WEB_DETAIL_HITS]:
            title = sanitize_text(str(item.get("title") or ""), secrets).strip()
            source = sanitize_text(str(item.get("source") or ""), secrets).strip()
            if title:
                hit_lines.append(f"{title} · {source}" if source else title)
        if note:
            hit_lines.append(f"说明：{sanitize_text(note, secrets)}")
        text, clipped = _bounded("\n".join(hit_lines), TOOL_DETAIL_TEXT_CHARS)
        return ToolOutcomeView(
            kind="web",
            target=_optional_sanitized(payload.get("query"), secrets),
            summary=sanitize_text(summary, secrets),
            text=text or None,
            total=len(hits),
            truncated=clipped or bool(payload.get("truncated")),
        )

    # web_fetch：正文本身就是最有用的东西，摘要只说体积。
    if not isinstance(payload.get("url"), str) or not isinstance(payload.get("text"), str):
        return None
    total_chars = _int_of(payload.get("total_chars"))
    summary = f"{total_chars if total_chars is not None else len(str(payload.get('text') or ''))} 字符"
    if sanitize_text(str(payload.get("final_url") or ""), secrets).strip():
        summary = f"{summary} · 已跟随重定向"
    parts = [sanitize_text(str(payload.get("text") or ""), secrets)]
    if note:
        parts.append(f"说明：{sanitize_text(note, secrets)}")
    text, clipped = _bounded("\n\n".join(parts), TOOL_DETAIL_TEXT_CHARS)
    return ToolOutcomeView(
        kind="web",
        target=_optional_sanitized(payload.get("title"), secrets)
        or _optional_sanitized(payload.get("url"), secrets),
        summary=sanitize_text(summary, secrets),
        text=text or None,
        total=total_chars,
        truncated=clipped or bool(payload.get("truncated")),
    )


def delegation_outcome(
    payload: Mapping[str, Any], secrets: Iterable[str], note: str | None
) -> ToolOutcomeView:
    """`delegate_research` 的卡片（§18.7）：把每个子 Agent 的结局摊成一行行，而不是一段 JSON。"""
    children: list[DelegationChildView] = []
    for item in _mappings(payload.get("children")):
        scope = _optional_sanitized(item.get("scope"), secrets)
        if scope is None:
            continue
        child_error = sanitize_text(str(item.get("error") or ""), secrets)[:TOOL_DETAIL_TEXT_CHARS]
        children.append(
            DelegationChildView(
                scope=scope,
                status="completed" if item.get("status") == "completed" else "failed",
                tool_calls=_int_of(item.get("tool_calls")) or 0,
                model_calls=_int_of(item.get("model_calls")) or 0,
                evidence_count=_int_of(item.get("evidence_count")) or 0,
                changed_files=_int_of(item.get("changed_files")) or 0,
                # 缺失时给 None 而不是 0：0 毫秒是个断言（"它瞬间跑完了"），而"这台机器没记"
                # 是另一件事，页面该区别对待。
                elapsed_ms=_int_of(item.get("elapsed_ms")),
                error=child_error or None,
            )
        )
    # 逐路的工具链（2026-11-04）：子事件没有并进 Checkpoint（`delegation_audit` 那条链没打通），
    # 所以从进程内实时表按目录回填——它是同一份数据的"跑完之后"形态，保留 30 分钟。
    # 过期就留空，让页面如实说"没有记录到工具调用"，不编造。
    lanes = delegation_live.lanes_for([child.scope for child in children])
    for child in children:
        lane = lanes.get(child.scope)
        child.goal = lane.goal if lane is not None else ""
        steps = lane.steps if lane is not None else []
        child.tools = [
            DelegationToolStepView(
                name=step.name,
                # `list_dir` 的 path 常是 `.`（子任务的 cwd 就是这一路的目录），
                # 直接显示 `.` 等于没说；换成目录本身，用户才知道在哪儿列的。
                target=step.target if step.target not in {"", "."} else child.scope,
                status=step.status,
            )
            for step in steps
            # `task_control` 是收尾信号，不是调查动作——它出现在链路里只会让人以为查了东西。
            if step.name not in _HIDDEN_DELEGATION_STEPS
        ]

    done = sum(1 for child in children if child.status == "completed")
    summary = f"{len(children)} 个子任务 · {done} 个已完成"
    if note:
        summary = f"{summary} · {sanitize_text(note, secrets)[:120]}"
    text, clipped = _bounded(
        sanitize_text(str(payload.get("report") or ""), secrets), TOOL_DETAIL_TEXT_CHARS
    )
    return ToolOutcomeView(
        kind="delegation",
        summary=summary,
        text=text or None,
        children=children,
        truncated=clipped or bool(payload.get("truncated")),
    )
