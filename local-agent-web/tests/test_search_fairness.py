"""超限时怎么选：**同级轮转**（架构 §6.7 的 v0.6 T63 第三片）。

要钉住的是**盲区的形状**，而不是某个数字：

1. 命中上限绑定在"名字最靠前的那个子树"上时，目标文件（在名字**靠后**的顶层条目里）仍要被看到；
2. 文件数上限同理——扫描额度不能被一个子树吃光（B-34 的原始症状）；
3. **未触顶时结果集合与从前逐字相同**（轮转只改变"先看到哪些"，不改变"能扫到哪些"）；
4. 顺序确定、可复现（同级按名排序 + 固定轮转）；
5. `grep` / `glob` / 按需 RAG 三处用**同一份实现**（同一棵树上给出同一批文件）。
"""

from __future__ import annotations

import time
from pathlib import Path

from local_agent.config import AppConfig
from local_agent.rag import search as rag_search
from local_agent.security.policy import SecurityPolicy
from local_agent.tools import search_tools


def _write(root: Path, name: str, body: str) -> None:
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")


def _policy(root: Path, app_config: AppConfig) -> SecurityPolicy:
    return SecurityPolicy.from_config(app_config, workspace_root=root, environ={})


def test_match_cap_no_longer_locks_onto_one_subtree(
    tmp_path: Path, app_config: AppConfig
) -> None:
    """命中上限触顶时，名字靠后的顶层条目也要被看到——这正是"系统性盲区"的正面证据。"""
    for index in range(20):  # 名字最靠前的子树，每个文件都命中
        _write(tmp_path, f"aaa/generated_{index:02d}.py", "hit\n")
    _write(tmp_path, "zzz/target.py", "hit\n")
    policy = _policy(tmp_path, app_config)

    result = search_tools.grep_content("hit", policy=policy, max_matches=3)

    seen = {match.path for match in result.matches}
    # 老行为：深度优先取前 3 个文件，全部落在 aaa/ —— zzz/target.py 永远看不到
    assert "zzz/target.py" in seen
    assert any(path.startswith("aaa/") for path in seen)  # 也不是"只给最后一个子树"
    assert result.truncated is True
    assert "同级轮转采样" in (result.note or "")


def test_scan_cap_cannot_be_eaten_by_one_subtree(
    tmp_path: Path, app_config: AppConfig, monkeypatch
) -> None:
    """文件数上限同理：额度按条目轮转分配，不能被第一个子树吃光。"""
    monkeypatch.setattr(search_tools, "MAX_SCAN_FILES", 2)
    for index in range(5):
        _write(tmp_path, f"aaa/one_{index}.py", "nope\n")
    _write(tmp_path, "zzz/target.py", "target\n")
    policy = _policy(tmp_path, app_config)

    result = search_tools.grep_content("target", policy=policy)

    assert [hit.path for hit in result.hits] == ["zzz/target.py"]
    assert "已达文件数上限 2" in (result.note or "")
    assert "同级轮转采样" in (result.note or "")


def test_uncapped_result_set_is_unchanged_and_order_is_deterministic(
    tmp_path: Path, app_config: AppConfig
) -> None:
    """未触顶：**集合**与从前相同（上限没有被触及），顺序确定可复现。"""
    _write(tmp_path, "f1.py", "hit\n")
    _write(tmp_path, "d1/a.py", "hit\n")
    _write(tmp_path, "d1/b.py", "hit\n")
    _write(tmp_path, "d2/c.py", "hit\n")
    policy = _policy(tmp_path, app_config)

    listed = search_tools.glob_files("**/*.py", policy=policy)
    again = search_tools.glob_files("**/*.py", policy=policy)
    found = search_tools.grep_content("hit", policy=policy)

    assert set(listed.files) == {"f1.py", "d1/a.py", "d1/b.py", "d2/c.py"}
    assert listed.truncated is False
    assert listed.files == again.files  # 可复现
    assert {match.path for match in found.matches} == set(listed.files)
    assert found.total_matches == 4


def test_order_follows_same_level_rotation(tmp_path: Path, app_config: AppConfig) -> None:
    """顺序本身是契约的一部分：**同级按名排序 + 轮流各出一条**（不是"文件先、子目录后"）。"""
    _write(tmp_path, "f1.py", "x\n")
    _write(tmp_path, "d1/a.py", "x\n")
    _write(tmp_path, "d1/b.py", "x\n")
    _write(tmp_path, "d2/c.py", "x\n")
    policy = _policy(tmp_path, app_config)

    listed = search_tools.glob_files("**/*.py", policy=policy)

    # 顶层条目按名：d1 < d2 < f1.py；每轮各出一条 ⇒ d1/a、d2/c、f1，第二轮再轮到 d1/b
    assert listed.files == ["d1/a.py", "d2/c.py", "f1.py", "d1/b.py"]


def test_deep_subtree_does_not_starve_its_siblings(tmp_path: Path, app_config: AppConfig) -> None:
    """递归轮转：一个**很深的**大子树也不能把同级的其他条目饿死。"""
    for index in range(6):
        _write(tmp_path, f"big/nested/deep_{index}.py", "hit\n")
    _write(tmp_path, "small/one.py", "hit\n")
    policy = _policy(tmp_path, app_config)

    result = search_tools.grep_content("hit", policy=policy, max_matches=2)

    assert "small/one.py" in {match.path for match in result.matches}


def test_the_three_walks_share_one_implementation(tmp_path: Path, app_config: AppConfig) -> None:
    """同一棵树上，`glob` 与按需 RAG 的遍历给出**同一批文件**（跳过口径与遍历实现都只有一份）。"""
    _write(tmp_path, "src/a.py", "x\n")
    _write(tmp_path, "src/deep/b.py", "x\n")
    _write(tmp_path, "node_modules/dep.py", "x\n")  # 依赖目录
    _write(tmp_path, ".hidden/c.py", "x\n")  # 隐藏目录
    _write(tmp_path, ".gitignore", "ignored/\n")
    _write(tmp_path, "ignored/d.py", "x\n")  # 被 git 忽略
    _write(tmp_path, ".env", "SECRET=1\n")  # 敏感文件
    policy = _policy(tmp_path, app_config)

    listed = search_tools.glob_files("**/*", policy=policy)
    files, _notes, _truncated = rag_search._walk_files(
        tmp_path, config=app_config.rag, deadline=time.monotonic() + 5
    )

    assert set(listed.files) == {"src/a.py", "src/deep/b.py"}
    assert {path.relative_to(tmp_path).as_posix() for path in files} == set(listed.files)
