"""两种"等待用户"的派生视图：审批（`project_approval`）与提问（`project_question`）。

为什么单独一个模块：`projection.py` 已是超限文件（规模棘轮不允许继续增长），而这两件事是
同一族——都从「当前挂起请求 + 历史决定」推导一个 `pending/...` 状态，都不带状态机字段。
放在一起也让"审批与提问互斥"这条约束在代码里看得见。

**导入方向**：本模块只从 :mod:`local_agent.application.projection` 取展示层的基础工具
（脱敏、映射视图、时间归一），不反向依赖它；`projection.project_snapshot` 在**函数内**导入
本模块——那是聚合函数调用子视图，模块级导入会形成循环。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from local_agent.application.models import (
    ApprovalState,
    ApprovalView,
    QuestionItemView,
    QuestionState,
    QuestionView,
)
from local_agent.application.projection import (
    _DISPLAY_EXTERNAL_POLICY,
    _KEY_LIKE,
    _MASK,
    _NOT_RECORDED,
    DISPLAY_ARGUMENT_CHARS,
    _as_mapping,
    _as_utc,
    _display_arguments,
    _mappings,
    _optional_sanitized,
    _optional_str,
    _run_failed,
    _tool_name_of,
    sanitize_text,
    tool_scope_key,
)
from local_agent.config import redact_secrets
from local_agent.security.path_guard import WorkspaceBoundaryError, resolve_target


def project_approval(
    state: Mapping[str, Any], *, has_live_interrupt: bool, secrets: Iterable[str] = (),
    workspace_root: Path | None = None, confirmation_stage: int = 1,
) -> ApprovalView | None:
    """审批的派生视图状态。

    底层只有 `pending_approval`（当前请求）与 `approval_history`（决定），没有状态机字段：
    `pending`/`executing`/`approved`/`rejected`/`expired` 全部在这里推导（规则见
    `models.ApprovalView` 的 docstring）。`edit` 归入 `approved` 并保留被改写的参数。
    """
    pending = _as_mapping(state.get("pending_approval"))
    decisions = _mappings(state.get("approval_history"))

    request: Mapping[str, Any] | None = pending
    last_decision = decisions[-1] if decisions else None
    approval_id = str(
        (request or {}).get("approval_id") or (last_decision or {}).get("approval_id") or ""
    )
    if not approval_id:
        return None

    decided = next(
        (item for item in reversed(decisions) if str(item.get("approval_id") or "") == approval_id),
        None,
    )
    if decided is not None:
        decision = str(decided.get("decision") or "")
        call_id = str(decided.get("tool_call_id") or (request or {}).get("tool_call_id") or "")
        # 决定与请求都带轮次：作用域用它 + 调用 id，跨轮复用同一个调用 id 时不会互相冒充。
        run = str(decided.get("run_id") or (request or {}).get("run_id") or "")
        if decision == "reject":
            state_value: ApprovalState = "rejected"
            note = _optional_str(decided.get("reason")) or "该操作未执行。"
        else:
            scope = tool_scope_key(run, call_id)
            finished = any(
                tool_scope_key(str(result.get("run_id") or ""), str(result.get("tool_call_id") or "")) == scope
                for result in _mappings(state.get("tool_results"))
            )
            state_value = "approved" if finished else "executing"
            note = None
            if not finished and _run_failed(state, run):
                state_value = "interrupted"
                note = "本轮已结束，工具执行结果未完整记录；请以核对的文件或命令结果为准。"
        target, outside = _approval_target(
            (request or {}).get("tool_name"), (request or {}).get("arguments"), secrets, workspace_root
        )
        return ApprovalView(
            approval_id=approval_id,
            # `run_id` / `tool_call_id` 在旧 Checkpoint 里可能确实没有（决定记录早于这两个字段，或
            # `pending_approval` 已被清掉）。DTO 要求非空，直接填空串会让**整个会话详情接口 500**——
            # 实测一个会话因此完全打不开（`GET /api/sessions/{id}` → `ApprovalView` 校验失败）。
            # 按这一层既有口径写 `_NOT_RECORDED`：如实说没有记录，不猜一个看起来合理的值。
            run_id=run or _NOT_RECORDED,
            tool_call_id=call_id or _NOT_RECORDED,
            # 决定本身不带工具名，`pending_approval` 又可能已被清掉：从工具事件里补；
            # 旧 Checkpoint 两处都没有时写"未记录"，而不是猜一个名字。
            tool_name=_tool_name_of(state, run, call_id) or _NOT_RECORDED,
            action_fingerprint=str(decided.get("action_fingerprint") or (request or {}).get("action_fingerprint") or "")
            or _NOT_RECORDED,
            redacted_arguments=_display_arguments((request or {}).get("arguments"), secrets),
            edited_arguments=_display_arguments(decided.get("edited_arguments"), secrets),
            # 目标只能从原始请求参数来；请求已被清掉时如实为空，不去别处猜。
            target=target,
            outside_workspace=outside,
            impact_summary=_approval_impact(request, secrets, workspace_root),
            risk="high" if bool((request or {}).get("requires_second_confirmation")) else "normal",
            state=state_value,
            created_at=_as_utc((request or {}).get("created_at")) or _as_utc(decided.get("created_at")) or _epoch(),
            note=sanitize_text(note, secrets) if note is not None else None,
        )

    if request is None:
        return None

    approved_now = has_live_interrupt
    approval_state: ApprovalState = "pending" if approved_now else "expired"
    target, outside = _approval_target(request.get("tool_name"), request.get("arguments"), secrets, workspace_root)
    return ApprovalView(
        approval_id=approval_id,
        # 与上面的"已决定"分支同一口径：缺失写 `_NOT_RECORDED`，绝不填空串——DTO 要求非空，
        # 空串会让整个会话详情接口 500，把"某个字段没有记录"升级成"这个会话打不开"。
        run_id=str(request.get("run_id") or "") or _NOT_RECORDED,
        tool_call_id=str(request.get("tool_call_id") or "") or _NOT_RECORDED,
        tool_name=str(request.get("tool_name") or ""),
        action_fingerprint=str(request.get("action_fingerprint") or ""),
        redacted_arguments=_display_arguments(request.get("arguments"), secrets),
        target=target,
        outside_workspace=outside,
        confirmation_stage=2 if confirmation_stage == 2 else 1,
        impact_summary=_approval_impact(request, secrets, workspace_root),
        risk="high" if bool(request.get("requires_second_confirmation")) else "normal",
        state=approval_state,
        created_at=_as_utc(request.get("created_at")) or _epoch(),
        note=None if approved_now else "该审批已失效，未执行任何操作。请重新发起请求。",
    )


def project_question(
    state: Mapping[str, Any], *, has_live_interrupt: bool, secrets: Iterable[str] = ()
) -> QuestionView | None:
    """提问（题组）的派生视图状态（2026-10-28，架构 §6.14）。

    底层只有 ``pending_question``（当前请求）与 ``question_history``（整组答复记录），没有状态机
    字段：``pending`` / ``answered`` / ``skipped`` / ``expired`` 全部在这里推导。提问不会自己过期
    ——只有"运行在执行中断里结束"会把它变成 ``expired``。
    """
    pending = _as_mapping(state.get("pending_question"))
    answers = _mappings(state.get("question_history"))
    request: Mapping[str, Any] | None = pending if pending else None
    last = answers[-1] if answers else None
    question_id = str(
        (request or {}).get("question_id") or (last or {}).get("question_id") or ""
    )
    if not question_id:
        return None

    decided = next(
        (item for item in reversed(answers) if str(item.get("question_id") or "") == question_id),
        None,
    )
    asked_at = (
        _as_utc((request or {}).get("asked_at"))
        or _as_utc((decided or {}).get("at"))
        or _epoch()
    )
    if decided is not None:
        views = _question_item_views(request, decided, secrets)
        answered = any(view.answer for view in views)
        return QuestionView(
            question_id=question_id,
            run_id=str(decided.get("run_id") or (request or {}).get("run_id") or "") or _NOT_RECORDED,
            tool_call_id=(
                str(decided.get("tool_call_id") or (request or {}).get("tool_call_id") or "")
                or _NOT_RECORDED
            ),
            question=views[0].question if views else "（问题未记录）",
            items=views,
            state="answered" if answered else "skipped",
            answer=views[0].answer if len(views) == 1 else None,
            asked_at=asked_at,
            answered_at=_as_utc(decided.get("at")),
            note=None if answered else "你跳过了这组提问，本轮按没有补充信息继续。",
        )

    if request is None:
        return None
    views = _question_item_views(request, None, secrets)
    pending_state: QuestionState = "pending" if has_live_interrupt else "expired"
    return QuestionView(
        question_id=question_id,
        run_id=str(request.get("run_id") or "") or _NOT_RECORDED,
        tool_call_id=str(request.get("tool_call_id") or "") or _NOT_RECORDED,
        question=views[0].question if views else "（问题未记录）",
        items=views,
        state=pending_state,
        asked_at=asked_at,
        note=None if pending_state == "pending" else "该提问已失效：本轮已结束。",
    )


def _question_source_items(
    request: Mapping[str, Any] | None, decided: Mapping[str, Any] | None
) -> list[dict[str, Any]]:
    """题组正文：请求还在就用它；请求已被清掉时从答复记录重建（每题都留了问题原文）。"""
    raw = (request or {}).get("items")
    items: list[dict[str, Any]] = []
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        for entry in raw:
            if not isinstance(entry, Mapping):
                continue
            items.append({
                "question": str(entry.get("question") or ""),
                "options": [str(option) for option in (entry.get("options") or [])],
                "allow_free_text": bool(entry.get("allow_free_text", True)),
            })
    if items:
        return items
    recorded = (decided or {}).get("answers")
    if isinstance(recorded, Sequence) and not isinstance(recorded, (str, bytes)):
        rebuilt = [
            {"question": str(entry.get("question") or ""), "options": [], "allow_free_text": True}
            for entry in recorded
            if isinstance(entry, Mapping)
        ]
        if rebuilt:
            return rebuilt
    # 旧 Checkpoint：只有单句 `question` / `answer`
    legacy = str((request or {}).get("question") or (decided or {}).get("question") or "")
    return [{"question": legacy, "options": [], "allow_free_text": True}]


def _question_item_views(
    request: Mapping[str, Any] | None,
    decided: Mapping[str, Any] | None,
    secrets: Iterable[str],
) -> list[QuestionItemView]:
    """逐题视图：待决时只有题目与选项；已答时带上每题结果（空答复 = 该题跳过）。"""
    items = _question_source_items(request, decided)
    by_index: dict[int, str] = {}
    if decided is not None and decided.get("action") != "skip":
        recorded = decided.get("answers")
        if isinstance(recorded, Sequence) and not isinstance(recorded, (str, bytes)):
            for entry in recorded:
                if not isinstance(entry, Mapping):
                    continue
                try:
                    index = int(str(entry.get("index")))
                except (TypeError, ValueError):
                    continue
                by_index[index] = str(entry.get("answer") or "")
        else:  # 旧记录：单题单值
            by_index[0] = str(decided.get("answer") or "")

    views: list[QuestionItemView] = []
    for index, item in enumerate(items):
        given = by_index.get(index) if decided is not None else None
        text = sanitize_text(given, secrets) if given else ""
        views.append(
            QuestionItemView(
                index=index,
                question=sanitize_text(str(item.get("question") or ""), secrets) or "（问题未记录）",
                options=[sanitize_text(str(option), secrets) for option in item.get("options") or []],
                allow_free_text=bool(item.get("allow_free_text", True)),
                answer=text or None,
                skipped=decided is not None and not text,
            )
        )
    return views


def _approval_target(
    tool_name: Any, arguments: Any, secrets: Iterable[str], workspace_root: Path | None
) -> tuple[str | None, bool]:
    """审批的规范化展示目标：**从结构化请求参数取**，不从影响摘要文本里反推。

    认不出的工具或没有相应参数的请求一律 `None`：宁可让卡片少一行"目标"，
    也不要猜一个看起来合理的路径（§15.8）。
    """
    if not isinstance(arguments, Mapping):
        return None, False
    name = str(tool_name or "")
    raw: Any = None
    if name in {"edit_file", "write_file", "delete_file"}:
        raw = arguments.get("path")
    elif name == "run_shell":
        raw = arguments.get("cwd") or "."
    if raw is None:
        return None, False
    if workspace_root is None:
        return _optional_sanitized(raw, secrets), False
    try:
        resolved = resolve_target(
            str(raw),
            base_dir=workspace_root,
            workspace_root=workspace_root,
            external_policy=_DISPLAY_EXTERNAL_POLICY,
        )
    except WorkspaceBoundaryError:
        return None, False
    # 审批卡必须能让用户核对完整目标；与普通输出一样只抹密钥。
    path = resolved.path.as_posix() if resolved.external else resolved.path.relative_to(workspace_root.resolve()).as_posix()
    target = _KEY_LIKE.sub(_MASK, redact_secrets(path, list(secrets)))
    return target, resolved.external


def _approval_impact(
    request: Mapping[str, Any] | None, secrets: Iterable[str], workspace_root: Path | None
) -> str:
    text = str((request or {}).get("impact_summary") or "（未记录影响摘要）")
    if workspace_root is not None:
        root = workspace_root.resolve()
        for prefix in (root.as_posix() + "/", str(root) + "\\"):
            text = text.replace(prefix, "")
    return sanitize_text(text.replace("（T10）", "").replace("(T10)", ""), secrets)[:DISPLAY_ARGUMENT_CHARS]


def _epoch() -> datetime:
    """没有时间戳时的占位：用 Unix epoch 而不是"现在"，否则同一 State 每次投影都不同。"""
    return datetime(1970, 1, 1, tzinfo=timezone.utc)
