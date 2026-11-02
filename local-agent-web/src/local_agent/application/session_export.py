"""会话导出（2026-10-30）：把人读的 Markdown 与机读的 JSON 两种形态做出来。

**数据源是轮次表，不是原始消息流**。`conversation.collect` 给的已经是"按真实顺序、
含旧会话兼容、跟着活动分支走"的问答清单；而消息流里还混着工具行、思考行、压缩行——
那些是执行细节，不是用户想带走的对话。导出只回答"我问了什么、它答了什么"。

格式化函数可独立测试；`export_session` 负责从应用服务收集轮次并调度格式化。

**不导出**：trace、工具参数、附件内容、token 用量。理由见 ARCHITECTURE §15.7——
导出的是对话，不是运行审计；审计走轨迹页。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from local_agent.application import conversation

if TYPE_CHECKING:  # pragma: no cover - 只为类型标注导入
    from local_agent.persistence.conversation_store import Branch, ConversationTurn
    from local_agent.persistence.session_store import Session

#: 导出形态。与路由的 `?format=` 取值一一对应，多一个都算契约漂移。
ExportFormat = Literal["markdown", "json"]

#: Windows / POSIX 都非法或会截断文件名的字符，统一压成空格。
#: 冒号尤其要处理——会话标题里出现 "14:30" 这种时间在文件名里会变成备用数据流。
_UNSAFE_FILENAME = re.compile(r'[\\/:*?"<>|\r\n\t]+')

#: `state` 的中文说法。**只翻译表里有的**：认不出的状态原样输出，
#: 而不是猜一个更好看的词——导出件里的每个字都该对得上库里的值。
_STATE_LABEL = {
    "completed": "已完成",
    "failed": "失败",
    "cancelled": "已取消",
    "running": "未完成",
    "awaiting_approval": "等你确认",
    "awaiting_input": "等你回答",
}


def filename_for(title: str, fmt: ExportFormat, now: datetime) -> str:
    """生成下载文件名：`{标题}-{yyyyMMdd-HHmm}.{md|json}`。

    标题里的非法字符压成空格后**再压缩连续空白**：直接替换会在 `a/b` 这种标题里
    留下两个空格，看起来像手滑。全空时退回"会话"，而不是给出 `-.md`。
    """
    stem = " ".join(_UNSAFE_FILENAME.sub(" ", title).split())[:60] or "会话"
    extension = "md" if fmt == "markdown" else "json"
    return f"{stem}-{now.strftime('%Y%m%d-%H%M')}.{extension}"


def _answer(turn: ConversationTurn) -> str:
    text = turn.answer_text.strip()
    if text:
        return text
    label = _STATE_LABEL.get(turn.state, turn.state)
    return f"（没有内容：本轮{label}）"


def _local_label(value: str | None) -> str:
    """ISO 时间 → `YYYY-MM-DD HH:MM` 本地时间；解析不出来就留空，不编时间。"""
    if not value:
        return ""
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return ""
    return moment.astimezone().strftime("%Y-%m-%d %H:%M")


def to_markdown(*, session: Session, turns: list[ConversationTurn],
                branches: list[Branch], workspace_label: str, exported_at: datetime) -> str:
    """人读形态：一问一答，标题为会话名，抬头写明来源与导出时刻。"""
    lines: list[str] = [f"# {session.title}", ""]
    header = [f"会话 `{session.session_id}`"]
    if workspace_label:
        header.append(f"工作区 {workspace_label}")
    header.append(f"导出于 {exported_at.astimezone().strftime('%Y-%m-%d %H:%M')}")
    lines.extend(["> " + " · ".join(header), ""])

    # 多条分支时说明导的是哪一条：默认活动分支，否则读者会以为少了内容。
    if len(branches) > 1:
        active = next((branch for branch in branches if branch.active), None)
        name = active.title if active is not None else branches[0].title
        lines.extend([f"> 共 {len(branches)} 条分支，本文件是「{name}」。", ""])

    if not turns:
        lines.extend(["（这个会话还没有问答。）", ""])
        return "\n".join(lines)

    for index, turn in enumerate(turns, start=1):
        stamp = _local_label(turn.submitted_at)
        asked = f"**你** · {stamp}" if stamp else "**你**"
        lines.extend([f"### {index}. 提问", "", asked, "", turn.question.strip() or "（空消息）", ""])
        state = _STATE_LABEL.get(turn.state, turn.state)
        answer_head = "**chaiai**" if turn.state == "completed" else f"**chaiai** · {state}"
        lines.extend([answer_head, "", _answer(turn), ""])
        lines.extend(["---", ""])

    return "\n".join(lines).rstrip() + "\n"


def to_json(*, session: Session, turns: list[ConversationTurn],
            branches: list[Branch], workspace_label: str, exported_at: datetime) -> str:
    """机读形态：结构稳定、字段名为 snake_case，与库里的拼写一致，便于再导入或分析。"""
    payload = {
        "schema_version": 1,
        "exported_at": exported_at.isoformat(),
        "session": {
            "session_id": session.session_id,
            "title": session.title,
            "mode": "chat" if session.mode == "chat" else "agent",
            "workspace": workspace_label,
            "created_at": session.created_at,
        },
        "branches": [
            {"branch_id": branch.branch_id, "title": branch.title, "active": branch.active}
            for branch in branches
        ],
        "turns": [
            {
                "ordinal": turn.ordinal,
                "turn_id": turn.turn_id,
                "parent_turn_id": turn.parent_turn_id,
                "branch_id": turn.origin_branch_id,
                "state": turn.state,
                "submitted_at": turn.submitted_at,
                "question": turn.question,
                "answer": turn.answer_text,
            }
            for turn in turns
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def export_session(service: Any, session_id: str, fmt: str = "markdown") -> tuple[str, str, str]:
    """把一次会话导出成 ``(正文, 文件名, media_type)``（2026-10-30）。

    只读：不写库、不调模型、不动分支。**已归档的会话也允许导出**——归档只是"离开视线"，
    数据还在；拒绝导出会逼用户先取消归档、导完再归档一次。

    数据源是 `conversation.collect` 的轮次表（按真实顺序、跟着活动分支），不是原始消息流：
    后者夹着工具行与思考行，那不是用户想带走的对话。
    """
    session = service._session_or_raise(session_id, allow_archived=True)
    with service._catalog_session_guard(session_id, allow_archived=True):
        conversation.initialize(service, session)
        turns, branches = conversation.collect(service, session)
    exported_at = datetime.now(timezone.utc)
    workspace_label = service._ref_for(Path(session.workspace_root)).display_name
    # 收窄成字面量再往下传：路由层已经用 `Literal` 校验过，这里只是把类型对齐，
    # 不用 `cast` —— 认不出的值按 markdown 处理，不会造出第三种形态。
    shape: ExportFormat = "json" if fmt == "json" else "markdown"
    filename = filename_for(session.title, shape, exported_at)
    if shape == "json":
        return (
            to_json(session=session, turns=turns, branches=branches,
                    workspace_label=workspace_label, exported_at=exported_at),
            filename,
            "application/json",
        )
    return (
        to_markdown(session=session, turns=turns, branches=branches,
                    workspace_label=workspace_label, exported_at=exported_at),
        filename,
        "text/markdown; charset=utf-8",
    )
