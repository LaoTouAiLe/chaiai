"""按轮次修正的会话投影（T18 的记忆写入链路 ↔ 架构 §10.1）。

设计约束：

- ``messages`` 是 Checkpoint 里的**原始审计记录**（架构 §10.1、T19 的"原文恢复"验收），
  任何节点都不得删改；
- 但用户看到的内容与下一轮送模型的窗口不能停在模型的原始措辞上——闲聊记忆保存失败时，原回答
  可能写着"好的，我记住了"（复审实证）。

因此这里提供一层**读时投影**：``reply_overrides``（``{消息 id: 修正文本}``）由"写入结果已确定"
的节点写进 State，页面渲染与 Prompt 窗口都经 :func:`project_messages` 取文本。原文仍在 Checkpoint
里，投影不改变存储、也不改变消息条数与顺序。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage

__all__ = ["current_turn_ai_message", "message_id", "project_messages"]


def message_id(message: AnyMessage) -> str:
    """消息的投影键（langgraph 的 ``add_messages`` 会为消息分配稳定 id）。"""
    return str(getattr(message, "id", "") or "")


def current_turn_ai_message(messages: Sequence[AnyMessage]) -> AIMessage | None:
    """本轮最后一条 AI 消息（**允许没有文本**）；只在本轮最后一条 ``HumanMessage`` 之后寻找。

    两个边界都不能少：

    - **必须划界**：本轮模型没产出文本时继续往前找，会拿到**上一轮**的回答——把它当成本轮目标会改错
      消息，也会把旧问题的内容混进重写素材（复审实证）；
    - **允许空文本**：本轮那条 AI 消息可能确实没有文本（只提工具调用），但它仍要作为投影目标——否则
      "记忆没保存成功"这类确定性说明无处落地，而页面渲染会跳过空消息，用户什么也看不到（复审实证）。
    """
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            return None
        if isinstance(message, AIMessage):
            return message
    return None


def project_messages(
    messages: Sequence[AnyMessage], overrides: Mapping[str, str]
) -> list[AnyMessage]:
    """按投影表替换 AI 消息文本（返回新列表，不修改入参）。

    只替换**有对应键且文本确实不同**的消息，其余原样返回；长度与顺序不变，因此页面按索引增量渲染的
    语义（``start_index``）不受影响。
    """
    if not overrides:
        return list(messages)
    projected: list[AnyMessage] = []
    for message in messages:
        text = overrides.get(message_id(message))
        if isinstance(message, AIMessage) and text and text != message.text:
            projected.append(message.model_copy(update={"content": text}))
        else:
            projected.append(message)
    return projected
