"""侧边会话（§21.7，2026-10-23）：从父会话某几轮的文本播种一段上下文。

口径与 hermes 的 `session.branch` 一致：**只取 user/assistant 的可见文本**，
不复制工具调用、计划、记忆与 Checkpoint。原因写在那条契约里——搬完整 state
会破坏 tool_call/tool_result 的配对，而侧聊真正需要的只是"这段在说什么"。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from local_agent.application.service import ApplicationService

#: 种子上限：最近 20 轮、合计 8000 字符。超出从**最早**开始丢，并如实标注省略了几轮。
SEED_MAX_TURNS = 20
SEED_MAX_CHARS = 8_000

#: 前缀与用户实际问题的分界。**必须可识别**：`question` 里存的是拼好的整段，
#: 前端据此把上下文折叠起来，只显示用户真正问的那句（否则对话里会躺着一大块前缀）。
INJECTION_MARKER = "【以上为上下文，以下是我的问题】"


def compose_first_prompt(prefix: str, question: str) -> str:
    """把上下文前缀与用户问题拼成首轮提问。"""
    return f"{prefix}{INJECTION_MARKER}\n\n{question}"


def split_injected(text: str) -> tuple[str, str]:
    """拆出 ``(上下文前缀, 用户问题)``；没有标记时前缀为空。"""
    if INJECTION_MARKER not in text:
        return "", text
    prefix, _, question = text.partition(INJECTION_MARKER)
    return prefix, question.lstrip("\n")


def build_context_prefix(
    service: ApplicationService, *, parent_session_id: str, origin_turn_id: str | None = None
) -> tuple[str, int]:
    """把父会话到某轮为止的问答压成一段前缀文本，返回 ``(文本, 省略轮数)``。

    `origin_turn_id` 为空表示取到最新一轮。父会话或轮次不存在时抛 `LookupError`——
    静默降级成"没有上下文"会让用户以为侧聊知道前面在聊什么。
    """
    parent = service._sqlite_access(lambda: service._sessions.get(parent_session_id))
    if parent is None:
        raise LookupError("父会话不存在，可能已被删除。")
    turns = service._sqlite_access(lambda: service._conversations.turns(parent_session_id))
    if origin_turn_id:
        index = next(
            (position for position, turn in enumerate(turns) if turn.turn_id == origin_turn_id),
            None,
        )
        if index is None:
            raise LookupError("指定的轮次不在这个会话里，可能已被删除。")
        turns = turns[: index + 1]

    pairs = [(turn.question or "", turn.answer_text or "") for turn in turns]
    kept: list[str] = []
    total = 0
    # 从最新往前取：用户问的总是"最近这段"，更早的丢了影响最小。
    for question, answer in reversed(pairs):
        block = f"用户：{question}\n助手：{answer}"
        if len(kept) >= SEED_MAX_TURNS or total + len(block) > SEED_MAX_CHARS:
            break
        kept.append(block)
        total += len(block)
    kept.reverse()
    omitted = len(pairs) - len(kept)

    lines = ["以下是主会话此前的对话，供你理解上下文：", ""]
    if omitted > 0:
        lines.append(f"（已省略更早的 {omitted} 轮）")
    lines.extend(kept)
    lines.extend(["", "---", ""])
    return "\n".join(lines), omitted


def create_side_chat(
    service: ApplicationService,
    *,
    parent_session_id: str,
    origin_turn_id: str | None = None,
) -> str:
    """建一个侧聊会话，返回新会话 id。

    侧聊**不抢焦点**（`activate=False`）：否则用户正在看的主会话会被切走，
    而"边看边问"的前提就是主线不动。
    """
    parent = service._sqlite_access(lambda: service._sessions.get(parent_session_id))
    if parent is None:
        raise LookupError("父会话不存在，可能已被删除。")
    if parent.parent_session_id is not None:
        # 侧聊再开侧聊会让血统变成一条链，而上下文只取一层——语义上说不清。
        raise ValueError("侧聊不能再开侧聊，请在主会话上开。")
    siblings = service._sqlite_access(lambda: service._sessions.side_chats(parent_session_id))
    summary = service.create_session(
        title=f"{parent.title} · 侧聊 {len(siblings) + 1}",
        workspace_root=Path(parent.workspace_root),
        # 闲聊模式（2026-10-23 用户裁定）：侧聊不是"另一个执行线程"，是"旁边问一句"。
        # 继承父会话的 mode 会让它带上全套工具，与闲聊的定位不符。
        mode="chat",
        activate=False,
        parent_session_id=parent_session_id,
        origin_turn_id=origin_turn_id,
    )
    return summary.session_id
