"""图片引用化（2026-10-20）：State 只存引用，出站前才物化。

两个要钉住的事实：

1. **Checkpoint 里不能再出现 base64**——它是单条快照 5~12 MB 的唯一来源；
2. **出站只保留最近 K 轮的图**——更早的换成占位，工具结果里的旧图一律剥离（端点只允许
   user 消息带图，2026-10-20 的 400 就是踩在这里）。

物化只作用于送给模型的副本，**不写回 State**：同一条消息被投影/压缩读到时仍然是引用。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from langchain_core.messages import HumanMessage, ToolMessage

from local_agent.attachments import LoadedAttachment
from local_agent.graph import nodes

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def _reader(_ref):
    return PNG, "image/png"


def _context(reader=_reader):
    return SimpleNamespace(image_reader=reader)


def _ref(attachment_id: str) -> dict[str, str]:
    return {"kind": "attachment", "id": attachment_id, "media_type": "image/png"}


def test_prepare_context_stores_only_references_not_bytes() -> None:
    """入口节点写进 State 的是引用：整份 update 里不能出现 base64（含 data URL 前缀）。"""
    attachment = LoadedAttachment(
        attachment_id="2026/10/20/s-1/abcd1234.png",
        media_type="image/png",
        data=PNG,
    )
    runtime = SimpleNamespace(context=SimpleNamespace(user_attachments=(attachment,)))

    update = nodes.prepare_context_node({"user_input": "帮我看下这个"}, runtime)

    message = update["messages"][0]
    assert message.content == "帮我看下这个"
    assert message.additional_kwargs["local_agent_image_refs"] == [
        {"kind": "attachment", "id": "2026/10/20/s-1/abcd1234.png", "media_type": "image/png"}
    ]
    dumped = json.dumps(update, default=str)
    assert "data:image" not in dumped
    assert "base64" not in dumped
    # 页面缩略图仍靠这份引用（与消息内容解耦）。
    assert update["user_attachment_refs"][0]["attachment_id"] == "2026/10/20/s-1/abcd1234.png"


def test_only_the_two_most_recent_turns_are_materialized() -> None:
    messages = [
        HumanMessage(content="第一轮", id="u1", additional_kwargs={"local_agent_image_refs": [_ref("a1")]}),
        HumanMessage(content="第二轮", id="u2", additional_kwargs={"local_agent_image_refs": [_ref("a2")]}),
        HumanMessage(content="第三轮", id="u3", additional_kwargs={"local_agent_image_refs": [_ref("a3")]}),
    ]

    resolved = nodes._resolve_message_images(messages, _context())

    first, second, third = resolved
    # 最早那轮：图换成占位，不再发给模型。
    assert not any(block.get("type") == "image_url" for block in first.content)
    assert any(nodes._IMAGE_OMITTED_NOTE in str(block) for block in first.content)
    # 最近两轮：物化成 data URL。
    for message in (second, third):
        urls = [block["image_url"]["url"] for block in message.content if block.get("type") == "image_url"]
        assert urls and urls[0].startswith("data:image/png;base64,")
    # 引用不留在出站副本里（它是 State 的字段，不是请求的一部分）。
    for message in resolved:
        assert "local_agent_image_refs" not in (message.additional_kwargs or {})
    # 原消息不被修改：State / Checkpoint 仍然是引用形态。
    assert messages[0].additional_kwargs["local_agent_image_refs"] == [_ref("a1")]


def test_images_inside_tool_messages_are_always_stripped() -> None:
    """旧数据里 tool 消息带过图（`view_image` 的老行为）：出站时必须剥离，否则整轮 400。"""
    old_tool = ToolMessage(
        content=[
            {"type": "text", "text": "已读取图片 hero.png"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ],
        tool_call_id="t1",
        name="view_image",
        id="t1-row",
    )

    resolved = nodes._resolve_message_images([old_tool], _context())

    message = resolved[0]
    assert not any(block.get("type") == "image_url" for block in message.content)
    assert any(nodes._TOOL_IMAGE_OMITTED_NOTE in str(block) for block in message.content)


def test_reader_uses_the_session_id_not_the_policy_scope(tmp_path) -> None:
    """附件引用必须按**会话 id** 读回（2026-10-20）。

    `session_scope` 带命名空间前缀（`namespace:session`），过不了 `AttachmentStore` 的引用校验——
    拿它去读，所有上传图都会变成"无法读取"，而且不报错、只是模型看不到图。
    """
    import pytest

    from local_agent.attachments import AttachmentStore
    from local_agent.config import SecurityConfig, WorkspaceConfig
    from local_agent.exceptions import AttachmentError
    from local_agent.graph.builder import _make_image_reader
    from local_agent.security.path_guard import ExternalPathPolicy
    from local_agent.security.policy import SecurityPolicy

    store = AttachmentStore.under(tmp_path)
    saved = store.save("a" * 32, PNG)
    policy = SecurityPolicy(
        workspace_root=tmp_path,
        security=SecurityConfig(),
        workspace=WorkspaceConfig(),
        external_policy=ExternalPathPolicy(mode="workspace-write"),  # type: ignore[arg-type]
    )

    reader = _make_image_reader(policy, store, "a" * 32)
    data, media_type = reader({"kind": "attachment", "id": saved.attachment_id})  # type: ignore[misc]
    assert data == PNG and media_type == "image/png"

    # 别的会话拿同一个引用：读不到（跨会话引用不允许变成读文件的通道）。
    other = _make_image_reader(policy, store, "b" * 32)
    with pytest.raises(AttachmentError):
        other({"kind": "attachment", "id": saved.attachment_id})


def test_missing_bytes_degrade_to_a_placeholder_instead_of_failing() -> None:
    """引用读不到（附件被清理、文件已删除）时给占位——一张旧图不能把整轮搞挂。"""
    message = HumanMessage(
        content="看图", id="u1", additional_kwargs={"local_agent_image_refs": [_ref("gone")]}
    )

    resolved = nodes._resolve_message_images([message], _context(reader=None))

    assert not any(block.get("type") == "image_url" for block in resolved[0].content)
    assert any("无法读取" in str(block) for block in resolved[0].content)
