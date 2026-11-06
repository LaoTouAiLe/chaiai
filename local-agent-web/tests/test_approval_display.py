"""审批卡上说人话（2026-11-06）。

用户原话：「不删，我怕这个没脑子的，删点其他的就完了」。查下来机制都是齐的——`delete_file`
永远要审批、批准一次性、走系统回收站可还原——但**卡面上的影响范围是机器串**：
``删除 E:/x（dir:4096:1699999999:parent-exists:True:files:12,dirs:3）``。
看不懂的东西只会让人更不敢点。

两条契约：
1. 给人看的一句必须说清"删什么、多大、能不能还原"；
2. 机器串继续进动作指纹：目录内容一变，旧批准就必须失效。
"""

from __future__ import annotations

from pathlib import Path

from local_agent.config import AppConfig
from local_agent.security.approval import action_fingerprint, describe_impact
from local_agent.security.policy import SecurityPolicy


def _policy(root: Path) -> SecurityPolicy:
    return SecurityPolicy.from_config(AppConfig(), workspace_root=root)


def _impact(policy: SecurityPolicy, arguments: dict[str, object]) -> str:
    decision = policy.classify_tool("delete_file", arguments)
    return describe_impact("delete_file", arguments, decision=decision, policy=policy)


def test_directory_delete_says_what_goes_away(tmp_path: Path) -> None:
    target = tmp_path / "draft"
    (target / "sub").mkdir(parents=True)
    (target / "a.txt").write_text("x", encoding="utf-8")
    (target / "sub" / "b.txt").write_text("y", encoding="utf-8")

    text = _impact(_policy(tmp_path), {"path": str(target), "recursive": True})

    assert "整个目录" in text
    assert "2 个文件" in text
    assert "1 个子目录" in text
    assert "回收站" in text
    # 机器串不许出现在卡面上
    assert "parent-exists" not in text
    assert "dir:" not in text


def test_file_delete_gives_size_and_restorability(tmp_path: Path) -> None:
    target = tmp_path / "notes.txt"
    target.write_text("hello", encoding="utf-8")

    text = _impact(_policy(tmp_path), {"path": str(target)})

    assert "文件" in text
    assert "B" in text
    assert "回收站" in text


def test_missing_target_is_stated_plainly(tmp_path: Path) -> None:
    text = _impact(_policy(tmp_path), {"path": str(tmp_path / "gone.txt")})

    assert "目标已不存在" in text


def test_external_target_is_still_flagged(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-file.txt"
    outside.write_text("x", encoding="utf-8")
    workspace = tmp_path / "ws"
    workspace.mkdir()

    text = _impact(_policy(workspace), {"path": str(outside)})

    assert "工作区外" in text


def test_the_fingerprint_still_reacts_to_the_directory_content(tmp_path: Path) -> None:
    """展示文案改了，但"目录内容一变旧批准就失效"这条语义不能丢。"""
    target = tmp_path / "d"
    target.mkdir()
    (target / "a.txt").write_text("1", encoding="utf-8")
    policy = _policy(tmp_path)
    arguments = {"path": str(target), "recursive": True}

    before = action_fingerprint("delete_file", arguments, policy=policy)
    (target / "b.txt").write_text("2", encoding="utf-8")

    assert action_fingerprint("delete_file", arguments, policy=policy) != before
