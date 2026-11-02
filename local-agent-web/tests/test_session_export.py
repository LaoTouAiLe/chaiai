"""会话导出的格式化规则（2026-10-30）。

这里只测**纯函数**：格式化不碰数据库、不调模型，所以不需要起服务。
`filename_for` 单独测是必要的——它要处理中文标题与 Windows 非法字符，这两件事
在端到端用例里很难稳定复现。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

from local_agent.application import session_export


def _session(**overrides: object) -> SimpleNamespace:
    base = {
        "session_id": "s1",
        "title": "架构问答",
        "mode": "agent",
        "created_at": "2026-10-30T08:00:00Z",
        "workspace_root": "E:/repo",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _turn(**overrides: object) -> SimpleNamespace:
    base = {
        "turn_id": "t1",
        "parent_turn_id": None,
        "origin_branch_id": "main",
        "run_id": None,
        "question": "问题一",
        "answer_text": "答案一",
        "state": "completed",
        "submitted_at": "2026-10-30T08:01:00Z",
        "checkpoint_id": None,
        "ordinal": 1,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _branch(**overrides: object) -> SimpleNamespace:
    base = {"branch_id": "main", "title": "主线", "active": True}
    base.update(overrides)
    return SimpleNamespace(**base)


NOW = datetime(2026, 10, 30, 9, 30, tzinfo=timezone.utc)


# --------------------------------------------------------------------- 文件名


def test_filename_keeps_chinese_and_appends_stamp() -> None:
    assert session_export.filename_for("架构问答", "markdown", NOW) == "架构问答-20261030-0930.md"
    assert session_export.filename_for("架构问答", "json", NOW) == "架构问答-20261030-0930.json"


def test_filename_replaces_illegal_characters_instead_of_dropping_the_file() -> None:
    """标题里的 `:` `/` 在 Windows 上会截断路径或形成备用数据流，必须换掉。"""
    name = session_export.filename_for('14:30 讨论 a/b|c?d*e"f<g>h', "markdown", NOW)
    assert name.endswith("-20261030-0930.md")
    assert not any(char in name for char in '\\/:*?"<>|')


def test_filename_falls_back_when_the_title_is_empty() -> None:
    """全空时给"会话"，而不是 `-.md` 这种看起来像出错的形状。"""
    assert session_export.filename_for("   ", "json", NOW) == "会话-20261030-0930.json"


# --------------------------------------------------------------------- Markdown


def test_markdown_lists_every_turn_with_question_and_answer() -> None:
    text = session_export.to_markdown(
        session=_session(),
        turns=[_turn(), _turn(turn_id="t2", ordinal=2, question="问题二", answer_text="答案二")],
        branches=[_branch()],
        workspace_label="local-agent-web",
        exported_at=NOW,
    )
    assert text.startswith("# 架构问答")
    assert "local-agent-web" in text
    assert "问题一" in text and "答案一" in text
    assert "问题二" in text and "答案二" in text
    # 单分支时不写分支说明：没有第二条可比较，说了只是噪声。
    assert "条分支" not in text


def test_markdown_says_which_branch_was_exported() -> None:
    text = session_export.to_markdown(
        session=_session(),
        turns=[_turn(origin_branch_id="b2")],
        branches=[_branch(branch_id="main", title="主线", active=False),
                  _branch(branch_id="b2", title="分支二", active=True)],
        workspace_label="",
        exported_at=NOW,
    )
    assert "共 2 条分支" in text and "分支二" in text


def test_markdown_marks_an_unfinished_turn_instead_of_leaving_it_blank() -> None:
    """没答完的轮次要显式说明；留白会让人以为导出件丢了内容。"""
    text = session_export.to_markdown(
        session=_session(),
        turns=[_turn(state="failed", answer_text="")],
        branches=[_branch()],
        workspace_label="",
        exported_at=NOW,
    )
    assert "失败" in text
    assert "没有内容" in text


def test_markdown_handles_a_session_without_turns() -> None:
    text = session_export.to_markdown(
        session=_session(), turns=[], branches=[_branch()], workspace_label="", exported_at=NOW,
    )
    assert "还没有问答" in text


# --------------------------------------------------------------------- JSON


def test_json_is_machine_readable_and_keeps_turn_fields() -> None:
    payload = json.loads(session_export.to_json(
        session=_session(),
        turns=[_turn()],
        branches=[_branch()],
        workspace_label="local-agent-web",
        exported_at=NOW,
    ))
    assert payload["schema_version"] == 1
    assert payload["session"]["session_id"] == "s1"
    assert payload["session"]["workspace"] == "local-agent-web"
    assert payload["turns"][0]["question"] == "问题一"
    assert payload["turns"][0]["answer"] == "答案一"
    assert payload["branches"][0] == {"branch_id": "main", "title": "主线", "active": True}
