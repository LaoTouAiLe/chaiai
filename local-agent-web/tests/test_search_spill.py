"""超限检索结果可恢复（架构 §6.7 的 v0.6 T63 段）。

要钉住三件事：

1. **完整 ≠ 预览的副本**：`save_full=true` 时落盘的是**全部**命中（而 `matches` 预览仍受 `max_matches` 约束）；
2. **locator 走既有读取通道**：它是一个普通的工作区相对路径，`read_file` 与 `grep path=<locator>` 都能读它——
   不新增工具、不开安全例外、不改可见性；
3. **落盘失败绝不变成错误**：检索照常返回、`note` 如实说"未能保存"，更不许谎称已保存。

另外必须钉住"**默认一字不变**"：省略 `save_full` 时结果与落盘行为与旧版相同（这也是 §4 硬验收
"关闭新增能力后行为不变"在本任务上的落地）。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from local_agent.config import AppConfig
from local_agent.security.policy import SecurityPolicy
from local_agent.tools import file_tools, result_spill, search_tools
from local_agent.tools.result_spill import SPILL_DIR


@pytest.fixture
def policy(tmp_path: Path, app_config: AppConfig) -> SecurityPolicy:
    """与 `test_tools.py` 的 `policy` 同一形态（两行，重复一份比跨测试模块引用更省事）。"""
    return SecurityPolicy.from_config(app_config, workspace_root=tmp_path, environ={})

#: 从 `note` 里取出 locator（它就是 `note` 里那串工作区相对路径，也是模型要照着用的东西）。
_LOCATOR = re.compile(r"([\w./-]*\.local-agent/results/[\w.-]+\.txt)")


def _locator(note: str | None) -> str:
    assert note is not None, "落盘成功时 note 必须给出 locator"
    found = _LOCATOR.search(note)
    assert found is not None, f"note 里没有 locator：{note!r}"
    return found.group(1)


def _write(root: Path, name: str, body: str) -> None:
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")


def _many_matches(root: Path, *, files: int = 4, per_file: int = 5) -> int:
    for index in range(files):
        _write(root, f"f{index}.py", "target line\n" * per_file)
    return files * per_file


def test_save_full_writes_every_match_and_keeps_the_preview_capped(
    tmp_path: Path, policy: SecurityPolicy
) -> None:
    total = _many_matches(tmp_path)

    result = search_tools.grep_content("target", policy=policy, max_matches=3, save_full=True)

    # 预览仍受上限；但计数与落盘是**完整**的
    assert len(result.matches) == 3
    assert result.total_matches == total
    assert result.truncated is False  # 收集没触到文件/时间上限
    locator = _locator(result.note)
    spilled = (tmp_path / locator).read_text(encoding="utf-8").splitlines()
    assert len(spilled) == total
    assert spilled[0].startswith("f0.py:1:target line")


def test_locator_is_readable_through_the_existing_read_tool(
    tmp_path: Path, policy: SecurityPolicy
) -> None:
    """**关键断言**：取回完整结果不需要新工具——既有的 `read_file` 就能读 locator。"""
    _many_matches(tmp_path)
    result = search_tools.grep_content("target", policy=policy, max_matches=2, save_full=True)

    read = file_tools.read_file_content(_locator(result.note), policy=policy)

    assert read.is_binary is False
    assert read.line_count == 20
    assert "target line" in read.content


def test_locator_can_be_grepped_again(tmp_path: Path, policy: SecurityPolicy) -> None:
    """同一份落盘结果还能再用 `grep path=<locator>` 在其中细查（模型不必整篇读完）。"""
    _many_matches(tmp_path)
    saved = search_tools.grep_content("target", policy=policy, save_full=True)
    locator = _locator(saved.note)

    again = search_tools.grep_content("line", policy=policy, path=locator, max_matches=2)

    assert again.truncated is True and again.total_matches >= 2
    assert {match.path for match in again.matches} == {locator}


def test_count_mode_with_save_full_is_complete(tmp_path: Path, policy: SecurityPolicy) -> None:
    """`count` 的汇总在 `save_full` 下不再被命中上限截断（否则"先看汇总"会骗人）。"""
    _many_matches(tmp_path, files=3, per_file=4)

    capped = search_tools.grep_content("target", policy=policy, output_mode="count", max_matches=2)
    full = search_tools.grep_content(
        "target", policy=policy, output_mode="count", max_matches=2, save_full=True
    )

    assert capped.total_matches == 2
    assert full.total_matches == 12
    assert len(full.hits) == 3


def test_spill_failure_never_turns_the_search_into_an_error(
    tmp_path: Path, policy: SecurityPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    """写盘失败 → 结果照常返回、`note` 如实说"未能保存"，且**没有**任何文件被写出来。"""
    _many_matches(tmp_path)
    # 补丁打在**调用方引用的那个名字**上（`search_tools` 是 `from ... import save_result`）。
    monkeypatch.setattr(search_tools, "save_result", lambda **_kwargs: None)

    result = search_tools.grep_content("target", policy=policy, max_matches=1, save_full=True)

    assert result.total_matches == 20  # 检索本身没受影响
    assert result.truncated is False
    assert result_spill.SPILL_FAILED_NOTE in (result.note or "")
    assert not (tmp_path / SPILL_DIR).exists()


def test_save_result_reports_failure_instead_of_raising(tmp_path: Path) -> None:
    """`save_result` 自己在无法建目录时返回 `None`（不抛异常——调用方才可能如实说明）。"""
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("x", encoding="utf-8")

    assert result_spill.save_result(
        workspace_root=blocked, kind="grep", key="k", lines=["a"], complete=True
    ) is None


def test_default_writes_nothing_and_is_unchanged(
    tmp_path: Path, policy: SecurityPolicy
) -> None:
    """省略 `save_full` ⇒ 与显式 `false` 逐字段相同，且**不创建任何落盘目录**（默认行为不变）。"""
    _many_matches(tmp_path)

    omitted = search_tools.grep_content("target", policy=policy, max_matches=2)
    explicit = search_tools.grep_content(
        "target", policy=policy, max_matches=2, save_full=False
    )

    assert omitted.model_dump() == explicit.model_dump()
    assert "已达命中上限 2 条" in (omitted.note or "")
    assert not (tmp_path / SPILL_DIR).exists()


def test_incomplete_collect_is_labeled_in_the_note(
    tmp_path: Path, policy: SecurityPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    """收集自己触到扫描上限时，"落盘结果也不完整"必须一起说出来（不许让 locator 显得无所不能）。"""
    _many_matches(tmp_path, files=3, per_file=1)
    monkeypatch.setattr(search_tools, "MAX_SCAN_FILES", 1)

    result = search_tools.grep_content("target", policy=policy, save_full=True)

    assert result.truncated is True
    assert "已达文件数上限 1" in (result.note or "")
    assert "落盘结果本身也未完整" in (result.note or "")


def test_spill_dir_is_self_ignored(tmp_path: Path, policy: SecurityPolicy) -> None:
    """目录自带 `.gitignore`（内容 `*`）：agent 不该在用户仓库里留下待提交的东西。"""
    _many_matches(tmp_path)

    search_tools.grep_content("target", policy=policy, save_full=True)

    ignore = tmp_path / SPILL_DIR / ".gitignore"
    assert ignore.read_text(encoding="utf-8") == "*\n"


def test_spill_files_never_appear_in_later_searches(
    tmp_path: Path, policy: SecurityPolicy
) -> None:
    """落盘目录是隐藏目录，且**不进**后续检索结果（否则检索会自我污染、越搜越多）。"""
    _many_matches(tmp_path)
    search_tools.grep_content("target", policy=policy, save_full=True)

    later = search_tools.grep_content("target", policy=policy, max_matches=500, save_full=True)
    listed = search_tools.glob_files("**/*.txt", policy=policy, max_files=100)

    assert all(SPILL_DIR not in hit.path for hit in later.hits)
    assert all(SPILL_DIR not in path for path in listed.files)


def test_spill_keeps_only_the_newest_files(
    tmp_path: Path, policy: SecurityPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    """保留策略只有一条：最新 :data:`MAX_SPILL_FILES` 个（否则磁盘会一直长）。"""
    monkeypatch.setattr(result_spill, "MAX_SPILL_FILES", 2)

    for index in range(5):
        result_spill.save_result(
            workspace_root=tmp_path, kind="grep", key=f"k{index}", lines=["x"], complete=True
        )

    kept = sorted(
        item.name for item in (tmp_path / SPILL_DIR).iterdir() if item.name != ".gitignore"
    )
    assert len(kept) == 2


def test_spill_is_also_capped_by_total_bytes(
    tmp_path: Path, policy: SecurityPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    """只按个数封顶不够（20 × 8 MB = 160 MB）：目录总量也有上限，超了从最旧的删起。"""
    monkeypatch.setattr(result_spill, "MAX_SPILL_FILES", 50)
    monkeypatch.setattr(result_spill, "MAX_SPILL_DIR_BYTES", 3_000)

    for index in range(5):
        result_spill.save_result(
            workspace_root=tmp_path,
            kind="grep",
            key=f"big{index}",
            lines=["x" * 900] * 2,  # 每个文件约 1.8 KB
            complete=True,
        )

    kept = [item for item in (tmp_path / SPILL_DIR).iterdir() if item.name != ".gitignore"]
    assert sum(item.stat().st_size for item in kept) <= 3_000
    # 最新那个一定在（清理只从最旧的删起）
    assert (tmp_path / SPILL_DIR / "grep-big4.txt").is_file()


def _directory_link(target: Path, link: Path) -> None:
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


@pytest.mark.parametrize("linked_part", [".local-agent", ".local-agent/results"])
def test_spill_rejects_directory_links_outside_workspace(tmp_path: Path, linked_part: str) -> None:
    workspace = tmp_path / "ws"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    link = workspace / linked_part
    link.parent.mkdir(parents=True, exist_ok=True)
    _directory_link(outside, link)
    ref = result_spill.save_result(workspace_root=workspace, kind="grep", key="link", lines=["x"], complete=True)
    assert ref is None
    assert list(outside.iterdir()) == []


def test_spill_protects_the_current_file_when_timestamps_tie(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(result_spill, "MAX_SPILL_FILES", 1)
    directory = tmp_path / SPILL_DIR
    directory.mkdir(parents=True)
    older = directory / "grep-zold.txt"
    current = directory / "grep-anew.txt"
    for target in [older, current]:
        target.write_text("x", encoding="utf-8")
        os.utime(target, (1_700_000_000, 1_700_000_000))
    result_spill.prune(directory, keep=current, workspace_root=tmp_path)
    assert current.is_file() and not older.exists()


def test_spill_cleanup_does_not_delete_unowned_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(result_spill, "MAX_SPILL_FILES", 1)
    directory = tmp_path / SPILL_DIR
    directory.mkdir(parents=True)
    unowned = directory / "user-note.txt"
    unowned.write_text("保留", encoding="utf-8")
    for key in ["old", "new"]:
        ref = result_spill.save_result(workspace_root=tmp_path, kind="glob", key=key, lines=["x"], complete=True)
        assert ref is not None
    assert unowned.read_text(encoding="utf-8") == "保留"


def test_locator_is_deterministic_per_query(tmp_path: Path, policy: SecurityPolicy) -> None:
    """同一组参数 → 同一个文件（重复查询覆盖写，locator 稳定可复现）。"""
    _many_matches(tmp_path)

    first = _locator(search_tools.grep_content("target", policy=policy, save_full=True).note)
    same = _locator(search_tools.grep_content("target", policy=policy, save_full=True).note)
    other = _locator(
        search_tools.grep_content("target", policy=policy, save_full=True, ignore_case=True).note
    )

    assert first == same
    assert first != other


def test_glob_save_full_lists_every_file(tmp_path: Path, policy: SecurityPolicy) -> None:
    for index in range(6):
        _write(tmp_path, f"g{index}.py", "x\n")

    result = search_tools.glob_files("*.py", policy=policy, max_files=2, save_full=True)

    assert len(result.files) == 2  # 预览仍受上限
    assert result.truncated is False
    locator = _locator(result.note)
    spilled = (tmp_path / locator).read_text(encoding="utf-8").splitlines()
    assert spilled == [f"g{index}.py" for index in range(6)]


def test_hidden_workspace_relative_locator_stays_inside_the_boundary(
    tmp_path: Path, policy: SecurityPolicy
) -> None:
    """locator 必须落在工作区内——这是"既有 read_file 能读它"的前提，也是它不构成边界例外的原因。"""
    _many_matches(tmp_path)

    result = search_tools.grep_content("target", policy=policy, save_full=True)

    locator = _locator(result.note)
    assert not locator.startswith(("/", "\\"))
    assert ".." not in Path(locator).parts
    assert json.dumps(result.model_dump(), ensure_ascii=False).count(locator) >= 1


def test_hits_summary_has_a_safety_ceiling_under_save_full(
    tmp_path: Path, policy: SecurityPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`save_full` 下汇总**刻意**不受 `max_matches` 截断（见上一条 count 用例），但**不能无限增长**：
    全仓命中时若把扫描到的每个文件都灌回上下文，正好抵消落盘把大头搬出上下文的意义。"""
    monkeypatch.setattr(search_tools, "MAX_HITS_FILES", 3)
    for index in range(5):
        _write(tmp_path, f"f{index}.py", "target\n")

    result = search_tools.grep_content("target", policy=policy, save_full=True)

    assert len(result.hits) == 3  # 汇总被安全上限截住
    assert result.total_matches == 5  # 但总数仍是真实的
    assert "汇总上限 3" in (result.note or "")
    # 保留的是"命中数最多"的前几个（同数时按文件名），不是遍历顺序的前几个
    assert [hit.path for hit in result.hits] == ["f0.py", "f1.py", "f2.py"]
    # 汇总截断**不丢数据**：完整清单仍在 locator 里
    spilled = (tmp_path / _locator(result.note)).read_text(encoding="utf-8").splitlines()
    assert len(spilled) == 5
