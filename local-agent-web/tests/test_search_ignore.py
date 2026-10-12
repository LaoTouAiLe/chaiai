"""检索遍历尊重 `.gitignore`（架构 §6.7 的 v0.6 T63 第二片）。

要钉住四件事：

1. **被忽略的目录整个剪掉**——本机实测的盲区就是这么来的（12828 个候选里 8138 个来自被忽略的
   `local-agent-web/eval/`，`glob="*.py"` 时前 2000 个候选全来自它，`src/` 一个文件都扫不到）；
2. **嵌套与反选的 git 语义**：深层优先；但**父目录被排除后无法再包含其中的文件**（同 git）；
3. **只影响枚举**：显式点名一个文件（`grep path=<file>`）不受影响；
4. **没有 `.gitignore` 时行为与从前逐字相同**（新增能力关闭时旧行为不变）。

另有一条：按需 RAG 的遍历用**同一套**规则（§6.7 要求两处共用一套，不各留一份）。
"""

from __future__ import annotations

import time
from pathlib import Path

from local_agent.config import AppConfig
from local_agent.rag import search as rag_search
from local_agent.security.policy import SecurityPolicy
from local_agent.tools import search_tools


def _write(root: Path, name: str, body: str = "hit\n") -> None:
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")


def _policy(root: Path, app_config: AppConfig) -> SecurityPolicy:
    return SecurityPolicy.from_config(app_config, workspace_root=root, environ={})


def test_ignored_directory_is_pruned_entirely(tmp_path: Path, app_config: AppConfig) -> None:
    """被忽略的目录整个剪掉：它里面的文件不出现在 `grep` / `glob` 结果里。"""
    _write(tmp_path, ".gitignore", "eval/\n")
    _write(tmp_path, "eval/generated.py")
    _write(tmp_path, "src/real.py")
    policy = _policy(tmp_path, app_config)

    found = search_tools.grep_content("hit", policy=policy, glob="*.py")
    listed = search_tools.glob_files("**/*.py", policy=policy)

    assert [hit.path for hit in found.hits] == ["src/real.py"]
    assert listed.files == ["src/real.py"]


def test_the_measured_blind_spot_is_gone(tmp_path: Path, app_config: AppConfig) -> None:
    """复现本机实测的形状：产物目录里的文件多到能吃光扫描额度，规则必须把它挡在遍历之外。"""
    _write(tmp_path, ".gitignore", "eval/\n")
    for index in range(300):  # 假装是产物目录（真实仓库这里是 8138 个文件）
        _write(tmp_path, f"eval/generated_{index:03d}.py")
    _write(tmp_path, "src/real.py", "target\n")
    policy = _policy(tmp_path, app_config)

    # 额度只够扫几十个文件：没有规则时会被 eval/ 吃光（按排序 it 排在 src/ 前面）
    result = search_tools.grep_content("target", policy=policy, glob="*.py", max_matches=5)

    assert [hit.path for hit in result.hits] == ["src/real.py"]
    assert result.truncated is False


def test_nested_gitignore_deeper_layer_wins(tmp_path: Path, app_config: AppConfig) -> None:
    """嵌套规则：深层 `.gitignore` 的 `!` 能反选外层的忽略（git 的同一语义）。"""
    _write(tmp_path, ".gitignore", "*.log\n")
    _write(tmp_path, "keep/.gitignore", "!important.log\n")
    _write(tmp_path, "keep/important.log")
    _write(tmp_path, "keep/noise.log")
    policy = _policy(tmp_path, app_config)

    listed = search_tools.glob_files("**/*.log", policy=policy)

    assert listed.files == ["keep/important.log"]


def test_ignored_parent_blocks_reinclusion(tmp_path: Path, app_config: AppConfig) -> None:
    """父目录被排除后无法再包含其中的文件——我们剪掉整个目录，与 git 的口径一致。"""
    _write(tmp_path, ".gitignore", "build/\n")
    _write(tmp_path, "build/.gitignore", "!keep.py\n")
    _write(tmp_path, "build/keep.py")
    policy = _policy(tmp_path, app_config)

    assert search_tools.glob_files("**/*.py", policy=policy).files == []


def test_explicit_path_is_not_affected_by_ignore(tmp_path: Path, app_config: AppConfig) -> None:
    """**只影响枚举**：点名要看的文件照给（同 ripgrep 的取向，也与 `read_file` 一致）。"""
    _write(tmp_path, ".gitignore", "eval/\n")
    _write(tmp_path, "eval/generated.py", "target\n")
    policy = _policy(tmp_path, app_config)

    found = search_tools.grep_content("target", policy=policy, path="eval/generated.py")

    assert [(match.path, match.line_number) for match in found.matches] == [
        ("eval/generated.py", 1)
    ]


def test_without_gitignore_behaviour_is_unchanged(tmp_path: Path, app_config: AppConfig) -> None:
    """没有 `.gitignore` 的工作区：结果与"只有静态清单"的旧行为一字不差。"""
    _write(tmp_path, "a.py")
    _write(tmp_path, "node_modules/dep.py")  # 静态清单本来就跳
    _write(tmp_path, ".hidden/x.py")  # 隐藏目录本来就跳
    _write(tmp_path, "build/out.py")  # 静态清单里的产物目录
    policy = _policy(tmp_path, app_config)

    listed = search_tools.glob_files("**/*.py", policy=policy)

    assert listed.files == ["a.py"]


def test_rag_walk_shares_the_same_rules(tmp_path: Path, app_config: AppConfig) -> None:
    """按需 RAG 的遍历用**同一套**规则（§6.7：两处共用一套，不各留一份）。"""
    _write(tmp_path, ".gitignore", "eval/\n")
    _write(tmp_path, "eval/generated.py")
    _write(tmp_path, "src/real.py")

    files, _notes, _truncated = rag_search._walk_files(
        tmp_path, config=app_config.rag, deadline=time.monotonic() + 5
    )

    assert [path.relative_to(tmp_path).as_posix() for path in files] == ["src/real.py"]


def test_gitignore_that_is_not_a_file_does_not_break_the_search(
    tmp_path: Path, app_config: AppConfig
) -> None:
    """`.gitignore` 不是普通文件（这里用一个同名目录）时当作没有规则并继续——检索不能因此整体失败。"""
    (tmp_path / ".gitignore").mkdir()
    _write(tmp_path, "src/real.py")
    policy = _policy(tmp_path, app_config)

    listed = search_tools.glob_files("**/*.py", policy=policy)

    assert listed.files == ["src/real.py"]
