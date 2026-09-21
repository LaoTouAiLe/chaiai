"""工具证据渲染（架构 §5.4；`:mod:`local_agent.graph.evidence``）。

回归的是实测缺陷：工具结果是 JSON，字段顺序是"元数据在前、正文在后"，只取前 N 个字符当摘要会让
模型（反思、收尾合成）只看到 ``path`` / ``line_count``，于是判断"正文被截断、任务没完成"，
连续空转最后反过来向用户索取**已经读到**的内容。所以这里固定两条不变量：

1. 标识字段与正文片段都要能看到；
2. 一旦截断**必须**带"此处为摘要，原文共 N 字符"标注——不能把"摘要被截断"读成"工具没返回内容"。
"""

from __future__ import annotations

import json

from local_agent.graph.evidence import TOOL_EVIDENCE_CHARS, tool_evidence_text


def _read_file_payload(*, lines: int = 349) -> str:
    """一个真实形态的 ``read_file`` 结果：元数据在前、正文在后（``content`` 很长）。"""
    return json.dumps(
        {
            "path": "src/local_agent/graph/routers.py",
            "encoding": "utf-8",
            "sha256": "4" * 64,
            "line_count": lines,
            "truncated": False,
            "is_binary": False,
            "content": "\n".join(f"第 {index} 行：def decide_mode(): ..." for index in range(1, 60)),
            "note": None,
        },
        ensure_ascii=False,
    )


def test_file_content_is_visible_not_only_metadata() -> None:
    """实测缺陷的回归：摘要里必须能看到正文片段，而不是只有元数据。"""
    line = tool_evidence_text("read_file", "ok", _read_file_payload(), limit=TOOL_EVIDENCE_CHARS)

    assert len(line) <= TOOL_EVIDENCE_CHARS
    assert line.startswith("read_file（ok）：")
    assert "path=src/local_agent/graph/routers.py" in line
    assert "line_count=349" in line
    assert "content=第 1 行" in line  # 正文片段
    assert "truncated=false" in line
    # 截断标注必须出现（否则模型会把片段当成完整输出）
    assert "原文片段" in line and "原文共" in line


def test_multi_line_content_keeps_its_line_breaks() -> None:
    """正文必须按原文保留换行：压成一行会被模型当成"概述性文本"（实测又反过来追问用户）。"""
    payload = json.dumps(
        {"path": "a.py", "content": "第一行\n第二行\n第三行"}, ensure_ascii=False
    )

    line = tool_evidence_text("read_file", "ok", payload, limit=600)

    assert "content=第一行\n第二行\n第三行" in line


def test_unshown_fields_are_counted_when_the_note_fits() -> None:
    """额度还有富余时，没展示的字段要明确计数（``sha256`` 这类长字段不该静默消失）。"""
    payload = json.dumps(
        {"path": "a.py", "line_count": 3, "content": "x = 1\ny = 2\n", "sha256": "y" * 64},
        ensure_ascii=False,
    )

    line = tool_evidence_text("read_file", "ok", payload, limit=600)

    assert "另有 1 个字段未展示" in line


def test_short_payload_is_rendered_whole_and_unmarked() -> None:
    """放得下就原样展示，不画蛇添足地加截断标注。"""
    payload = json.dumps({"path": "a.py", "content": "x = 1"}, ensure_ascii=False)

    assert tool_evidence_text("read_file", "ok", payload, limit=600) == (
        "read_file（ok）：path=a.py，content=x = 1"
    )


def test_every_budget_keeps_the_truncation_note() -> None:
    """收尾合成会把额度一路收敛到 40 字符：每一档都不能丢掉截断标注。"""
    payload = _read_file_payload()

    for limit in (600, 400, 200, 100, 50, 40):
        line = tool_evidence_text("read_file", "ok", payload, limit=limit)

        assert len(line) <= limit, limit
        assert "原文片段" in line, limit


def test_plain_text_output_is_clipped_with_the_same_note() -> None:
    """非 JSON 输出（Shell 等）同样带标注。"""
    line = tool_evidence_text("run_shell", "ok", "x" * 900, limit=200)

    assert len(line) <= 200
    assert line.startswith("run_shell（ok）：x")
    assert "原文片段" in line and "原文共 900 字符" in line


def test_missing_payload_is_stated_explicitly() -> None:
    """工具确实没返回内容时也要说清楚，别让模型去猜。"""
    assert tool_evidence_text("read_file", "ok", "", limit=200) == (
        "read_file（ok）：（工具没有返回内容）"
    )


def test_mapping_payload_is_accepted_directly() -> None:
    """调用方已经解析过时可以直接给映射（不必再序列化一次）。"""
    line = tool_evidence_text(
        "edit_file", "ok", {"path": "a.py", "changed_lines": 2, "diff": "-a\n+b"}, limit=600
    )

    assert line == "edit_file（ok）：path=a.py，changed_lines=2，diff=-a\n+b"
