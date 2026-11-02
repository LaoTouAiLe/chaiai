"""文档引用的出站物化（2026-10-29）。

钉住的是"模型能看到文件存在、并知道下一步该干什么"，以及"页面气泡不会被这段提示污染"
（物化只发生在出站副本上）。
"""

from __future__ import annotations

from langchain_core.messages import HumanMessage

from local_agent.graph.files import _FILE_REFS_KEY, materialize_message_files

REFS = [
    {
        "path": r"C:\data\attachments\a\b.pdf",
        "name": "b.pdf",
        "size": 1_258_291,
        "suffix": ".pdf",
    },
]


def _message(text: str = "看看这个", refs: list | None = None) -> HumanMessage:
    return HumanMessage(
        content=text,
        id="m1",
        additional_kwargs={_FILE_REFS_KEY: REFS if refs is None else refs},
    )


def test_hint_carries_absolute_path_size_and_next_action() -> None:
    """三条信息缺一不可：绝对路径、文件大小（判断值不值得整份读）、下一步动作。"""
    message = materialize_message_files(_message())

    text = str(message.content)
    assert r"C:\data\attachments\a\b.pdf" in text
    assert "1.2 MB" in text
    assert "read_file" in text
    assert "看看这个" in text


def test_original_object_is_returned_when_there_is_nothing_to_do() -> None:
    """没有文件引用的消息必须原样返回同一个对象，零开销。"""
    plain = HumanMessage(content="只有文字", id="m2")

    assert materialize_message_files(plain) is plain


def test_reference_key_is_removed_after_materializing() -> None:
    """物化后要摘掉引用键，否则同一段提示会被重复拼上去。"""
    message = materialize_message_files(_message())

    assert _FILE_REFS_KEY not in (message.additional_kwargs or {})


def test_empty_text_still_gets_the_hint() -> None:
    """只传文件不打字是合法提交：提示不能因此丢失，否则模型不知道有文件可读。"""
    message = materialize_message_files(_message(text=""))

    assert str(message.content).startswith("用户上传了以下文件")


def test_empty_refs_change_nothing() -> None:
    message = _message(refs=[])

    assert materialize_message_files(message) is message
