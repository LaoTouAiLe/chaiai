"""`@路径` 引用的解析与注入（2026-10-30）。

纯函数测试：不发消息、不调模型。越界、二进制、超预算这三条是**安全与可用性边界**，
必须钉住——它们出错的后果分别是"把工作区外的文件塞进模型上下文"和
"让模型基于半截内容下结论"。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from local_agent.application import file_mentions
from local_agent.config import AppConfig
from local_agent.cancellation import CancellationToken


# --------------------------------------------------------------------- 解析


def test_finds_paths_and_stops_at_chinese_punctuation() -> None:
    """中文里 `@src/app.py，这段` 很常见，标点必须当终止符。"""
    assert file_mentions.mentions_in("看下 @src/app.py，这段是干什么的") == ["src/app.py"]


def test_deduplicates_the_same_path() -> None:
    assert file_mentions.mentions_in("@a.py 和 @a.py 都看看") == ["a.py"]


def test_ignores_the_trailing_full_stop() -> None:
    assert file_mentions.mentions_in("参见 @README.md.") == ["README.md"]


def test_accepts_quoted_paths_with_spaces() -> None:
    """含空格的路径用引号包起来（与写入侧 `quoteMentionPath` 同一套写法）。"""
    assert file_mentions.mentions_in('看下 @"my notes.md" 里写了什么') == ["my notes.md"]
    assert file_mentions.mentions_in("看下 @`my notes.md` 里写了什么") == ["my notes.md"]


def test_returns_nothing_without_a_mention() -> None:
    assert file_mentions.mentions_in("没有引用") == []
    assert file_mentions.build(workspace_root=Path("."), text="没有引用") == ""


@pytest.mark.parametrize("blocked", [True, False])
def test_secret_mentions_follow_the_configured_read_rule(tmp_path: Path, blocked: bool) -> None:
    (tmp_path / ".env").write_text("FAKE_MENTION_SECRET=example", encoding="utf-8")
    block = file_mentions.build(
        workspace_root=tmp_path, text="@.env", block_known_secret_files=blocked
    )
    assert ("FAKE_MENTION_SECRET" in block) is not blocked
    if blocked:
        assert "敏感文件" in block


@pytest.mark.parametrize("blocked", [True, False])
def test_context_prefix_uses_the_bound_runtime_config(tmp_path: Path, blocked: bool) -> None:
    (tmp_path / ".env").write_text("BOUND_CONFIG_SENTINEL", encoding="utf-8")
    service = SimpleNamespace(
        _side_chat_context=lambda session: None,
        _session_context_window=lambda session_id: 32000,
    )
    config = AppConfig.model_validate({
        "security": {"secrets": {"block_known_secret_files": blocked}},
    })
    prefix = file_mentions.context_prefix(
        service, SimpleNamespace(workspace_root=str(tmp_path), session_id="s1"), "@.env", config
    )
    assert ("BOUND_CONFIG_SENTINEL" in prefix) is not blocked


def test_git_reference_obeys_cancellation_before_starting(tmp_path: Path) -> None:
    token = CancellationToken()
    token.cancel("任务已取消")
    assert file_mentions.git_patch(tmp_path, staged=False, cancellation=token) is None


@pytest.mark.parametrize("staged", [True, False])
@pytest.mark.parametrize("blocked", [True, False])
def test_git_mentions_are_scoped_and_filter_secret_paths(
    tmp_path: Path, staged: bool, blocked: bool,
) -> None:
    repo = tmp_path / "repo"
    workspace = repo / "sub"
    workspace.mkdir(parents=True)

    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

    git("init")
    files = {repo / "outside.txt": "OUTSIDE_WORKSPACE", workspace / ".env": "SECRET_PATCH",
             workspace / "file with spaces.txt": "ALLOWED_PATCH"}
    for path in files:
        path.write_text("initial", encoding="utf-8")
    git("add", ".")
    for path, text in files.items():
        path.write_text(text, encoding="utf-8")
    if staged:
        git("add", ".")
    patch = file_mentions.git_patch(
        workspace, staged=staged, block_known_secret_files=blocked
    )
    assert patch is not None
    assert "ALLOWED_PATCH" in patch[0]
    assert "OUTSIDE_WORKSPACE" not in patch[0]
    assert ("SECRET_PATCH" in patch[0]) is not blocked


# --------------------------------------------------------------------- 注入


def test_injects_file_contents_with_a_header(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("print('hi')", encoding="utf-8")
    block = file_mentions.build(workspace_root=tmp_path, text="看下 @app.py")
    assert "### @app.py" in block
    assert "print('hi')" in block
    # 抬头要说明"不必再读一遍"，否则模型会重复调用读取工具。
    assert "不必再调用读取工具" in block


def test_reads_a_path_with_spaces(tmp_path: Path) -> None:
    (tmp_path / "my notes.md").write_text("备忘", encoding="utf-8")
    block = file_mentions.build(workspace_root=tmp_path, text='看下 @"my notes.md"')
    assert "备忘" in block


# --------------------------------------------------------------------- 边界


def test_refuses_paths_outside_the_workspace(tmp_path: Path) -> None:
    """越界读是这条功能唯一的安全风险面：必须拒绝，且不能把内容带出来。"""
    outside = tmp_path.parent / "secret.txt"
    outside.write_text("绝密内容", encoding="utf-8")
    block = file_mentions.build(workspace_root=tmp_path / "ws", text="看下 @../secret.txt")
    assert "绝密内容" not in block
    assert "工作区之外" in block


def test_skips_binary_files_but_states_the_type(tmp_path: Path) -> None:
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01\x02")
    block = file_mentions.build(workspace_root=tmp_path, text="看下 @blob.bin")
    assert "二进制文件" in block
    # 给 MIME 类型，模型才知道该用什么工具处理；只说"读不了"等于没说。
    assert "application/octet-stream" in block


def test_says_so_when_the_file_is_missing(tmp_path: Path) -> None:
    """打错路径不静默：模型要知道"没读到"，而不是以为"这个文件不存在"。"""
    block = file_mentions.build(workspace_root=tmp_path, text="看下 @nope.py")
    assert "文件不存在" in block


def test_degrades_to_path_only_when_over_budget(tmp_path: Path) -> None:
    """超预算**不截断**：半个文件比没有文件更危险，模型会拿残缺内容下结论。"""
    body = "x" * 8_000
    (tmp_path / "big.txt").write_text(body, encoding="utf-8")
    # 窗口 2000 → 硬限 1000 tokens，8000 字符的英文远超它。
    block = file_mentions.build(workspace_root=tmp_path, text="看下 @big.txt", context_window=2_000)
    assert "文件在磁盘上" in block
    assert body not in block


def test_falls_back_to_a_fixed_budget_without_a_window(tmp_path: Path) -> None:
    """窗口未知时用保守兜底：小文件照常注入，不会被"没有窗口"卡住。"""
    (tmp_path / "small.txt").write_text("短内容", encoding="utf-8")
    block = file_mentions.build(workspace_root=tmp_path, text="看下 @small.txt")
    assert "短内容" in block


def test_git_scopes_are_scopes_not_files() -> None:
    """`@diff` 不该被当成"去找一个叫 diff 的文件"，否则会报文件不存在。"""
    assert file_mentions.mentions_in("@diff 看看") == []
    assert file_mentions.git_scopes_in("@diff 看看") == ["diff"]
    assert file_mentions.git_scopes_in("看下 @staged") == ["staged"]
    # 邮箱式写法（`foo@diff`）不是引用：`@` 前面必须是行首或空白。
    assert file_mentions.git_scopes_in("foo@diff") == []


def test_injects_git_diff_through_the_provider(tmp_path: Path) -> None:
    seen: list[bool] = []

    def provider(staged: bool) -> tuple[str, bool]:
        seen.append(staged)
        return "@@ -1 +1 @@\n-a\n+b", False

    block = file_mentions.build(workspace_root=tmp_path, text="看下 @diff", git_patch_provider=provider)
    assert "```diff" in block and "-a" in block
    assert seen == [False]


def test_says_so_when_there_is_no_diff(tmp_path: Path) -> None:
    block = file_mentions.build(
        workspace_root=tmp_path, text="@staged", git_patch_provider=lambda staged: ("", False),
    )
    assert "没有这类改动" in block


def test_degrades_an_oversized_patch_to_a_pointer(tmp_path: Path) -> None:
    """改动太大时不截断：让模型自己用 git_diff 工具挑着看。"""
    huge = "+" + "x" * 20_000
    block = file_mentions.build(
        workspace_root=tmp_path, text="@diff", context_window=2_000,
        git_patch_provider=lambda staged: (huge, False),
    )
    assert "git_diff 工具" in block
    assert "xxxxxxxx" not in block


def test_caps_the_number_of_references(tmp_path: Path) -> None:
    count = file_mentions.MAX_REFERENCES + 2
    for index in range(count):
        (tmp_path / f"f{index}.txt").write_text(f"内容{index}", encoding="utf-8")
    text = " ".join(f"@f{index}.txt" for index in range(count))
    block = file_mentions.build(workspace_root=tmp_path, text=text)
    assert block.count("### @") == file_mentions.MAX_REFERENCES
    assert "只处理了前" in block
