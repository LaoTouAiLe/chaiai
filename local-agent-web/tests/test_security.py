"""T04/T05 验收：分支边界、路径安全与工具放行。

覆盖：闲聊分支不绑定工具、工具调用不能越出工作区、AGENTS 扫描不越过
``workspace.root``、敏感文件与密钥脱敏、未放行工具失败闭合。
"""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest
from pydantic import ValidationError

from local_agent.config import AgentsConfig, AppConfig
from local_agent.context import agents_loader
from local_agent.exceptions import SecurityError, WorkspaceBoundaryError
from local_agent.graph import builder
from local_agent.graph.state import RuntimeContext
from local_agent.security import shell_guard
from local_agent.security.approval import action_fingerprint
from local_agent.security.path_guard import (
    ExternalPathPolicy,
    GrantAccess,
    display_path,
    is_sensitive_path,
    normalize_grant_path,
    protected_target_reason,
    resolve_target,
    resolve_within_workspace,
)
from local_agent.security.policy import SecurityPolicy
from local_agent.security.shell_guard import (
    EXECUTOR_ARGV,
    EXECUTOR_CMD,
    EXECUTOR_PWSH,
    EXECUTOR_SH,
    build_shell_invocation,
    match_blocked_patterns,
    prepare_shell,
    split_command,
)
from local_agent.tools.tool_registry import ToolRegistry

ModelFactory = Callable[..., object]
OUTSIDE_SECRET = "SECRET-OUTSIDE-WORKSPACE"


def _context(workspace: Path, model: object, app_config: AppConfig) -> RuntimeContext:
    return builder.build_runtime_context(
        app_config=app_config,
        model_adapter=model,  # type: ignore[arg-type]
        workspace_root=workspace,
        current_cwd=workspace,
    )


@pytest.fixture(autouse=True)
def _stable_executables(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """把"解析不到"的可执行名补成稳定路径。

    白名单语义的用例不该依赖"这次是怎么启动 pytest 的"：``uv run python -m pytest``
    时 ``pytest`` 在 PATH 上，直接跑 ``.venv\\Scripts\\python.exe -m pytest`` 时不在，
    同一个用例就会时好时坏。这里只补齐解析不到的名字，能解析到的仍用真实结果；
    需要验证"解析失败"语义的用例用 ``@pytest.mark.real_resolution`` 退出该行为。
    """
    if request.node.get_closest_marker("real_resolution") is not None:
        return
    real_which = shell_guard.shutil.which

    def fake_which(name: str, *args: object, **kwargs: object) -> str:
        return real_which(name, *args, **kwargs) or f"/stable/{name}"

    monkeypatch.setattr(shell_guard.shutil, "which", fake_which)


def _snapshot(root: Path) -> list[tuple[str, int, float]]:
    """工作区快照：相对路径、大小与修改时间。"""
    entries: list[tuple[str, int, float]] = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            stat = path.stat()
            entries.append((path.relative_to(root).as_posix(), stat.st_size, stat.st_mtime))
    return entries


# --------------------------------------------------------------------------- AGENTS 边界


def test_agents_scan_never_reads_outside_workspace(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """工作区外的 AGENTS.md 不得进入运行上下文或模型提示。"""
    outer = tmp_path / "outer"
    inner = outer / "inner"
    inner.mkdir(parents=True)
    (outer / ".git").mkdir()
    (outer / "AGENTS.md").write_text(OUTSIDE_SECRET, encoding="utf-8")

    model = model_factory(decision="chat", reply="好的")
    context = _context(inner, model, app_config)
    builder.build_chat_graph().invoke({"user_input": "你好"}, context=context)

    assert context.agents_context is None
    assert OUTSIDE_SECRET not in "\n".join(model.seen_prompts)


def test_agents_config_disabled_reads_nothing(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "AGENTS.md").write_text("规则", encoding="utf-8")

    loaded = agents_loader.load_agents(
        AgentsConfig.model_validate({"enabled": False}),
        workspace_root=workspace,
        current_cwd=workspace,
    )

    assert loaded.is_empty


# --------------------------------------------------------------------------- 路径与密钥


def test_path_guard_blocks_symlink_escape(tmp_path: Path, app_config: AppConfig) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("外部内容", encoding="utf-8")
    link = workspace / "link.txt"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):  # pragma: no cover - Windows 需要开发者模式
        pytest.skip("当前环境不支持创建符号链接")

    policy = SecurityPolicy.from_config(app_config, workspace_root=workspace, environ={})

    with pytest.raises(WorkspaceBoundaryError):
        policy.resolve_path("link.txt")


def test_sensitive_name_patterns() -> None:
    assert is_sensitive_path(Path(".env"))
    assert is_sensitive_path(Path("/tmp/server.key"))
    assert is_sensitive_path(Path("id_rsa"))
    assert not is_sensitive_path(Path("README.md"))
    assert not is_sensitive_path(Path("main.py"))


def test_display_path_is_relative_inside_and_absolute_outside(tmp_path: Path) -> None:
    """展示路径口径（§15.8、v0.8 T73）：工作区内相对、工作区外保持绝对。

    "工作区外"那一支不能改成相对（那样用户核对不了真实目标），"工作区内"那一支不能留绝对
    （§15.8 明令优先相对路径）——两个方向都是硬要求，所以这条用例把**两支**都钉住。
    """
    workspace = tmp_path / "ws"
    nested = workspace / "src" / "pkg"
    nested.mkdir(parents=True)
    outside = tmp_path / "elsewhere"

    assert display_path(workspace, workspace) == "."
    assert display_path(nested, workspace) == "src/pkg"
    # 工作区外：原样绝对路径（获授权后用户要能核对真实目标）。
    assert display_path(outside, workspace) == outside.as_posix()


# ---- v0.9 T77：路径别名与边界收口（架构 §20.2）-------------------------------


def test_trailing_dot_and_space_are_normalized_before_judging(
    tmp_path: Path, app_config: AppConfig
) -> None:
    """① 每段结尾的点与空格：**先归一，再判定**（§20.2）。

    实证过的绕过：`.env ` 的两种形态**都还不存在**时 `resolve()` 不会展开它（它只对已存在的目标
    取真实名字），于是判定说"不敏感"，而写入真的造出了 `.env`——校验与执行看了两个名字。
    """
    if os.name != "nt":
        pytest.skip("尾点/空格的静默忽略是 Win32 API 的行为；POSIX 上它们是普通字符（如实标注）")

    workspace = tmp_path / "ws"
    workspace.mkdir()
    policy = SecurityPolicy.from_config(app_config, workspace_root=workspace, environ={})

    # 归一：解析结果就是系统真正会打开的那个名字。
    assert policy.resolve_path(".env ") == workspace / ".env"
    assert policy.resolve_path("credentials.") == workspace / "credentials"
    assert is_sensitive_path(policy.resolve_path(".env "))

    # 负例：**写入方向**的绕过必须被拒——这正是实证里"造出了 .env"的那一步。
    with pytest.raises(SecurityError, match="疑似密钥文件"):
        policy.resolve_writable_path(".env ")
    with pytest.raises(SecurityError, match="疑似密钥文件"):
        policy.resolve_writable_path("credentials ")

    # 归一不是"看到点或空格就拒绝"：普通文件的尾空格照旧只当尾空格。
    assert policy.resolve_writable_path("普通文件.py ") == workspace / "普通文件.py"


def test_protected_git_segment_is_case_insensitive_on_windows(tmp_path: Path) -> None:
    """② 受保护判定的 `.git` 分段在 Windows 上不区分大小写（§20.2）。

    已存在的目标由 `resolve()` 取磁盘真实大小写，这一半本来就对；**目标尚不存在**时取不到，
    只能靠比较来判——`.GIT` 与 `.git` 在 Windows 上是同一个目录。
    """
    if os.name != "nt":
        pytest.skip("大小写不敏感是 Windows 的路径语义；POSIX 上 `.GIT` 是另一个目录（如实标注）")

    assert protected_target_reason(tmp_path / "repo" / ".GIT" / "config") == "Git 元数据目录"
    assert protected_target_reason(tmp_path / "repo" / ".GiT" / "config") == "Git 元数据目录"
    assert protected_target_reason(tmp_path / "repo" / "src" / "a.py") is None


def test_device_and_extended_length_prefixes_are_rejected(tmp_path: Path) -> None:
    """③ `\\?\\` / `\\.\\` 前缀一律拒绝（§20.2）：它们按设计绕开正常化。

    包括"看起来指着工作区里"的那一条——判定与系统打开的目标必须是同一条路径，所以不因为
    "它指着里面"就放行。
    """
    if os.name != "nt":
        pytest.skip("这两种前缀是 Windows 的命名空间概念；POSIX 上 `\\\\?\\` 只是普通字符（如实标注）")

    inside = tmp_path / "ws"
    inside.mkdir()
    allow = ExternalPathPolicy(mode="danger-full-access")

    for raw in (
        r"\\?\C:\Windows\win.ini",
        rf"\\?\{inside}\a.txt",
        r"\\.\C:\Windows\win.ini",
        r"\\?\UNC\localhost\C$\Windows\win.ini",
    ):
        with pytest.raises(WorkspaceBoundaryError, match="设备路径"):
            resolve_target(raw, base_dir=inside, workspace_root=inside, external_policy=allow)
        with pytest.raises(WorkspaceBoundaryError, match="设备路径"):
            resolve_within_workspace(raw, base_dir=inside, workspace_root=inside)


def test_remote_paths_outside_the_workspace_are_rejected_in_every_mode(tmp_path: Path) -> None:
    """④ 工作区**外**的远程路径一律拒绝（§20.2）：任何档位、连授权了也不放行。

    实证：`\\\\localhost\\C$\\Windows\\win.ini` 判不出受保护目标（它的父目录里没有本机的 `C:\\Windows`），
    于是 `allow` 档下"系统关键目录不可审批"被一个 UNC 别名绕过。本阶段无法证明远程路径别名到本机哪里
    ⇒ 拒绝。工作区**就是**那个共享时不受影响（那一支走的是"工作区内"判定）。
    """
    if os.name != "nt":
        pytest.skip("UNC 是 Windows/SMB 的路径形态；POSIX 上不存在这条（如实标注）")

    workspace = tmp_path / "ws"
    workspace.mkdir()
    remote = r"\\localhost\C$\Windows\win.ini"
    policies = (
        ExternalPathPolicy(mode="read-only"),
        ExternalPathPolicy(mode="workspace-write"),
        ExternalPathPolicy(mode="danger-full-access"),
        # 最严格的一条：**已经授权**了也不放行——受保护目标判定在远程路径上不成立，
        # 授权解决不了这一点。
        ExternalPathPolicy(
            mode="workspace-write",
            grants={normalize_grant_path(r"\\localhost\C$"): "read_write"},
        ),
    )

    for policy in policies:
        with pytest.raises(WorkspaceBoundaryError, match="远程路径"):
            resolve_target(
                remote, base_dir=workspace, workspace_root=workspace, external_policy=policy
            )
    # 纯边界入口（工具层用它）本就以"工作区之外"拒绝；远程的**专项理由**加在解析/授权那一支。
    with pytest.raises(WorkspaceBoundaryError, match="工作区之外"):
        resolve_within_workspace(remote, base_dir=workspace, workspace_root=workspace)

    share = Path(r"\\nas\share\ws")
    assert resolve_within_workspace(
        r"\\nas\share\ws\src\a.py", base_dir=share, workspace_root=share
    ) == Path(r"\\nas\share\ws\src\a.py")


def _short_name(path: Path) -> str:
    """取 Windows 8.3 短名；卷/策略未启用 8.3 时返回长名本身。"""
    if os.name != "nt":  # pragma: no cover - 调用方先行跳过
        return str(path)
    buffer = ctypes.create_unicode_buffer(600)
    length = ctypes.windll.kernel32.GetShortPathNameW(str(path), buffer, 600)  # type: ignore[attr-defined]
    return buffer.value if length else str(path)


def test_short_names_are_equivalent_or_honestly_skipped(tmp_path: Path) -> None:
    """⑤ 8.3 短名（§20.2）：别名必须与长名**同等判定**；本卷未启用 8.3 时**跳过并如实标注**。

    本机卷实测未启用 8.3（`GetShortPathNameW` 返回长名），因此这条别名行为**无法在本机证明**——
    跳过并写明原因，不假装跑过。
    """
    long_path = tmp_path / "credentials.txt"
    long_path.write_text("token\n", encoding="utf-8")
    short = _short_name(long_path)
    if short == str(long_path):
        pytest.skip("本卷未启用 8.3（GetShortPathNameW 返回长名）：无法在本机证明别名行为，不假装跑过")

    assert Path(short).name != long_path.name  # 前提：确实拿到一个真短名
    resolved = resolve_within_workspace(short, base_dir=tmp_path, workspace_root=tmp_path)
    assert resolved == long_path  # 归一：短名被展开成长名
    assert is_sensitive_path(resolved)  # 于是与长名同等判定（敏感文件照旧拦得住）


def test_alias_rules_do_not_change_ordinary_paths(
    tmp_path: Path, app_config: AppConfig
) -> None:
    """四类口径是**收窄**不是重写：普通路径、`..` 逃逸与越界的既有行为一字不变。"""
    workspace = tmp_path / "ws"
    (workspace / "sub").mkdir(parents=True)
    (workspace / "sub" / "a.txt").write_text("a\n", encoding="utf-8")
    policy = SecurityPolicy.from_config(app_config, workspace_root=workspace, environ={})

    assert policy.resolve_path("sub/a.txt") == workspace / "sub" / "a.txt"
    assert policy.resolve_path("sub/../sub/a.txt") == workspace / "sub" / "a.txt"
    with pytest.raises(WorkspaceBoundaryError):
        policy.resolve_path("../outside.txt")
    # 仅由点构成的分段（`...`）：实测由 `resolve()` 按 Windows 语义**塌缩成父目录**
    # （`sub/.../a.txt` ⇒ `sub/a.txt`，与系统真正打开的目标一致）；本模块的逐段去尾点
    # 只对**非全点**分段生效，全点分段原样保留——不猜成空段。
    assert policy.resolve_path("sub/.../a.txt") == workspace / "sub" / "a.txt"


def test_policy_redacts_secret_values(app_config: AppConfig, tmp_path: Path) -> None:
    policy = SecurityPolicy(
        workspace_root=tmp_path,
        security=app_config.security,
        workspace=app_config.workspace,
        secret_values=("sk-secret-value",),
    )

    assert policy.redact("key=sk-secret-value;") == "key=***;"
    assert policy.redact("nothing") == "nothing"


def test_unknown_tool_is_denied(app_config: AppConfig, tmp_path: Path) -> None:
    policy = SecurityPolicy.from_config(app_config, workspace_root=tmp_path, environ={})

    assert policy.classify_tool("list_dir").allowed
    denied = policy.classify_tool("delete_file")
    assert denied.allowed
    assert not denied.requires_approval
    assert "无需人工审批" in denied.reason
    assert not policy.classify_tool("some_unknown_tool").allowed


def test_write_tools_need_approval_when_risky(app_config: AppConfig, tmp_path: Path) -> None:
    policy = SecurityPolicy.from_config(app_config, workspace_root=tmp_path, environ={})

    small_edit = policy.classify_tool("edit_file", {"old_text": "a", "new_text": "b"})
    assert small_edit.allowed and not small_edit.requires_approval

    # 一次改 500 行**不再**需要审批：大范围编辑按"本轮改过几个文件"判（2026-10-15）。
    huge_edit = policy.classify_tool(
        "edit_file", {"path": "a.py", "old_text": "a", "new_text": "\n".join(str(i) for i in range(500))}
    )
    assert huge_edit.allowed and not huge_edit.requires_approval

    assert policy.classify_tool("edit_file", {}).allowed is False
    assert policy.classify_tool("write_file", {}).allowed is True
    assert policy.classify_tool("write_file", {"overwrite": True}).requires_approval is False
    assert policy.classify_tool("delete_file", {"recursive": True}).requires_approval is False


# --------------------------------------------------------------------------- Shell 判定


def test_split_command_respects_quotes_and_keeps_backslashes() -> None:
    assert split_command('python "a b.py" --flag') == ["python", "a b.py", "--flag"]
    assert split_command(r"python C:\work\x.py") == ["python", r"C:\work\x.py"]
    assert split_command("   ") == []


def _policy_with_mode(
    app_config: AppConfig, tmp_path: Path, *, mode: str
) -> SecurityPolicy:
    """按指定权限档位构造策略（沙箱由档位派生，没有独立开关了）。"""
    data = app_config.model_dump()
    data["workspace"]["external_paths"]["mode"] = mode
    return SecurityPolicy.from_config(
        AppConfig.model_validate(data), workspace_root=tmp_path, environ={}
    )


def test_regular_commands_skip_approval_in_the_writable_tiers(
    app_config: AppConfig, tmp_path: Path
) -> None:
    """**常规命令一律免逐条审批**（2026-10-15 权限模型重构）。

    换模型之前是"不在白名单就审批"：于是"想干活的命令"（mkdir、npm、跑脚本）全要人工点一次，
    而且批准是一次性的。现在档位负责"能写到哪"、沙箱/授权条目负责把边界落实，命令本身不再是
    需要用户逐条回答的问题——这正是 workspace-write 档"可以跑 powershell"的含义。

    钉住三件事：常规命令免审；破坏性与提权**照旧要批准**；工作区外 cwd 也要批准。
    """
    command = {"command": "mkdir site", "cwd": "."}

    for mode in ("workspace-write", "danger-full-access", "read-only"):
        policy = _policy_with_mode(app_config, tmp_path, mode=mode)
        relaxed = policy.classify_tool("run_shell", command)
        assert relaxed.allowed and not relaxed.requires_approval, mode
        assert mode in relaxed.reason

        # 破坏性命令：任何档位都要人工批准（四项硬约束里唯一没被档位化的一项）。
        destructive = policy.classify_tool("run_shell", {"command": "del a.txt", "cwd": "."})
        assert not destructive.allowed and destructive.requires_approval, mode

    # 越是界（提权）照旧要批准：那正是"越界才问"里被问的那一次。
    writable = _policy_with_mode(app_config, tmp_path, mode="workspace-write")
    escalate = writable.classify_tool(
        "run_shell", {"command": "mkdir site", "cwd": ".", "escalate_sandbox": True}
    )
    assert not escalate.allowed and escalate.requires_approval

    # 工作区外 cwd 不受档位影响：未授权时**直接拒绝**（不是"点一下就能跑"），
    # 授权之后仍要人工批准（架构 §1.5 明写"不得命中免审白名单"；那条路径有单独用例）。
    outside = tmp_path.parent
    external = writable.classify_tool("run_shell", {"command": "mkdir site", "cwd": str(outside)})
    assert external.allowed and not external.requires_approval


def test_shell_whitelist_allows_exact_command(app_config: AppConfig, tmp_path: Path) -> None:
    policy = SecurityPolicy.from_config(app_config, workspace_root=tmp_path, environ={})

    decision = policy.classify_tool("run_shell", {"command": "python --version"})

    assert decision.allowed
    assert not decision.requires_approval
    assert "白名单" in decision.reason


def test_commands_no_longer_depend_on_the_whitelist(
    app_config: AppConfig, tmp_path: Path
) -> None:
    """白名单**不再是审批开关**（2026-10-15 权限模型重构）。

    旧模型里"不在白名单 ⇒ 逐条审批"是主要摩擦源：想干活的命令全要人工点一次。现在档位与沙箱
    负责边界，命令的审批只由"破坏性 / 提权 / 工作区外 cwd"三件事触发。
    `security.shell.whitelist` 因此退化为兼容字段（`prepare_shell` 仍会算它，但不影响放行）。
    """
    policy = SecurityPolicy.from_config(app_config, workspace_root=tmp_path, environ={})

    for command in ("pytest -q", "git diff --stat", "ruff check --fix", "mkdir site"):
        decision = policy.classify_tool("run_shell", {"command": command})
        assert decision.allowed and not decision.requires_approval, command


def test_shell_syntax_is_no_longer_an_approval_trigger(
    app_config: AppConfig, tmp_path: Path
) -> None:
    """Shell 语法**不再自动升级为审批**（2026-10-15 权限模型重构）。

    旧模型里"含管道/重定向 ⇒ 提高风险等级 ⇒ 人工审批"是主要摩擦之一（`pytest -q | tail` 这种
    日常命令也要点一次）。新模型把边界交给档位与沙箱：危险的是"命令做了什么"（破坏性模式、
    提权、工作区外 cwd），不是"有没有管道"——这三类仍各自有专门分支拦着。
    """
    policy = SecurityPolicy.from_config(app_config, workspace_root=tmp_path, environ={})

    for command in (
        "python --version | head -1",
        "git status > out.txt",
        "echo $(whoami)",
        "echo %PATH%",
        "pytest --version & echo done",
    ):
        decision = policy.classify_tool("run_shell", {"command": command})
        assert decision.allowed and not decision.requires_approval, command


@pytest.mark.real_resolution
def test_blocked_pattern_with_unresolvable_executable_is_rejected(
    app_config: AppConfig, tmp_path: Path
) -> None:
    """命中破坏性模式、但当前环境根本没有这个可执行文件 → 直接拒绝，不进入审批。

    否则用户批准的是一个必然启动失败的命令，而且判定对象与执行对象仍然不一致。
    """
    config = _with_shell_config(app_config, blocked_patterns=["no-such-destructive-tool"])
    policy = SecurityPolicy.from_config(config, workspace_root=tmp_path, environ={})

    decision = policy.classify_tool(
        "run_shell", {"command": "no-such-destructive-tool --now"}
    )

    assert not decision.allowed
    assert not decision.requires_approval  # 不可执行 → 不给审批机会
    assert "破坏性命令模式" in decision.reason  # 危险原因仍然要说清楚
    assert "找不到可执行文件" in decision.reason


def test_blocked_pattern_with_resolvable_executable_needs_approval(
    app_config: AppConfig, tmp_path: Path
) -> None:
    """命中破坏性模式、且执行对象可解析 → 保留审批通道（架构 §6.6）。"""
    config = _with_shell_config(app_config, blocked_patterns=["python"])
    policy = SecurityPolicy.from_config(config, workspace_root=tmp_path, environ={})

    decision = policy.classify_tool("run_shell", {"command": "python --version"})

    assert not decision.allowed
    assert decision.requires_approval
    assert "破坏性命令模式" in decision.reason


@pytest.mark.real_resolution
def test_shell_blocked_patterns_force_approval(app_config: AppConfig, tmp_path: Path) -> None:
    policy = SecurityPolicy.from_config(app_config, workspace_root=tmp_path, environ={})

    decision = policy.classify_tool("run_shell", {"command": "rm -rf build"})

    assert not decision.allowed
    assert "破坏性命令模式" in decision.reason


def test_shell_executable_name_matching_is_normalized(
    app_config: AppConfig, tmp_path: Path
) -> None:
    """可执行名归一仍要准——但验在 `prepare_shell` 那一层。

    判定层已经不看白名单了（常规命令一律免审），所以"`PYTHON.EXE` 命中 `python`、`python3` 不命中"
    这件事只能在 `prepare_shell` 里验。这条留着是因为它守着一个真问题：**判定对象必须与执行对象
    是同一个**（架构 §5.3）——归一错了，指纹与审计就都会跟着错。
    """
    data = app_config.model_dump()
    data["security"]["shell"]["whitelist"] = [
        {"platform": ["windows", "posix"], "executable": "python", "args": ["--version"], "match": "exact"}
    ]
    shell_config = AppConfig.model_validate(data).security.shell

    matched = prepare_shell("PYTHON.EXE --version", config=shell_config, workspace_root=tmp_path)
    missed = prepare_shell("python3 --version", config=shell_config, workspace_root=tmp_path)

    assert matched.allowed and not matched.requires_approval, "白名单命中的命令免审"
    assert not missed.allowed and missed.requires_approval, "不同可执行名不能因前缀相同而命中"


def test_shell_platform_filter_is_respected(app_config: AppConfig, tmp_path: Path) -> None:
    data = app_config.model_dump()
    data["security"]["shell"]["whitelist"] = [
        {"platform": ["posix"], "executable": "python", "args": ["--version"], "match": "exact"}
    ]
    config = AppConfig.model_validate(data).security.shell

    on_windows = prepare_shell(
        "python --version", config=config, workspace_root=tmp_path, platform="windows"
    )
    on_posix = prepare_shell(
        "python --version", config=config, workspace_root=tmp_path, platform="posix"
    )

    assert not on_windows.allowed and on_windows.requires_approval
    assert on_posix.allowed


def test_shell_cwd_must_stay_inside_workspace(app_config: AppConfig, tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    policy = SecurityPolicy.from_config(app_config, workspace_root=workspace, environ={})

    outside = policy.classify_tool("run_shell", {"command": "python --version", "cwd": ".."})
    assert not outside.allowed
    assert "工作目录不合法" in outside.reason

    missing = policy.classify_tool("run_shell", {"command": "python --version", "cwd": "nope"})
    assert not missing.allowed
    assert "不存在" in missing.reason

    inside = policy.classify_tool("run_shell", {"command": "python --version", "cwd": "."})
    assert inside.allowed


def test_shell_env_keys_block_secrets(app_config: AppConfig, tmp_path: Path) -> None:
    policy = SecurityPolicy.from_config(app_config, workspace_root=tmp_path, environ={})

    for key in ("DEEPSEEK_API_KEY", "MY_TOKEN", "APP_SECRET", "db_password"):
        decision = policy.classify_tool(
            "run_shell", {"command": "python --version", "env_keys": [key]}
        )
        assert not decision.allowed, key
        assert "禁止透传" in decision.reason

    ok = policy.classify_tool("run_shell", {"command": "python --version", "env_keys": ["MY_MODE"]})
    assert ok.allowed


def test_shell_timeout_has_no_ceiling_unless_one_is_configured(
    app_config: AppConfig, tmp_path: Path
) -> None:
    """超时**默认不设上限**（2026-10-16）；配了 ``security.shell.max_timeout_seconds`` 才按它收。

    原来上限就是 ``default_timeout_seconds`` **自己**（默认 60 秒），于是模型想把慢命令跑完
    （冷启动浏览器、装依赖、真实构建）得先猜这个数，猜大了直接被拒——用户原话"完全就是限制
    agent 能力"，而且那次拒绝还被渲染成"被安全策略拦截"。现在两截都钉住：
    ① 没配上限时，远超默认值的请求照常放行；② 配了上限才拒，且理由里点名**是哪个键、当前多少**。
    """
    policy = SecurityPolicy.from_config(app_config, workspace_root=tmp_path, environ={})

    # ① 默认（未配 `max_timeout_seconds`）：600 秒也放行。
    generous = policy.classify_tool(
        "run_shell", {"command": "python --version", "timeout_seconds": 600}
    )
    assert generous.allowed, generous.reason

    # ② 配了上限：超一点都拒，理由要能照做（点名键名与当前值）。
    capped = SecurityPolicy.from_config(
        _with_shell_config(app_config, max_timeout_seconds=30),
        workspace_root=tmp_path,
        environ={},
    )
    over = capped.classify_tool("run_shell", {"command": "python --version", "timeout_seconds": 31})
    assert not over.allowed
    assert "max_timeout_seconds" in over.reason and "30" in over.reason

    # 上限之内照常放行。
    assert capped.classify_tool(
        "run_shell", {"command": "python --version", "timeout_seconds": 30}
    ).allowed


# --------------------------------------------------------------------------- T12 跨平台 Shell


def _with_shell_config(app_config: AppConfig, **overrides: object) -> AppConfig:
    """按覆盖项构造一份 ``security.shell`` 变体配置。"""
    data = app_config.model_dump()
    data["security"]["shell"].update(overrides)
    return AppConfig.model_validate(data)


def test_shell_syntax_command_uses_platform_adapter(
    app_config: AppConfig, tmp_path: Path
) -> None:
    """含 Shell 语法的命令：提级审批 + 平台适配器，两个平台各用各的解析规则（架构 §6.6）。"""
    config = app_config.security.shell

    on_windows = prepare_shell(
        "echo hi | findstr hi", config=config, workspace_root=tmp_path, platform="windows"
    )
    on_posix = prepare_shell(
        "echo hi | grep hi", config=config, workspace_root=tmp_path, platform="posix"
    )

    assert not on_windows.allowed and on_windows.requires_approval
    assert on_windows.executor == EXECUTOR_CMD
    assert on_windows.uses_shell_adapter
    assert on_windows.shell_syntax == "|"
    assert on_windows.shell_command == "echo hi | findstr hi"
    windows_invocation = build_shell_invocation(on_windows)
    assert Path(windows_invocation[0]).name.casefold() == "cmd.exe"
    assert windows_invocation[1:] == ["/d", "/s", "/c", "echo hi | findstr hi"]
    assert "cmd.exe /d /s /c" in on_windows.reason

    assert on_posix.executor == EXECUTOR_SH
    # 当前平台没有 sh，解析不到就退回适配器名（执行结果与判定时的环境一致）
    assert build_shell_invocation(on_posix)[1:] == ["-c", "echo hi | grep hi"]
    assert "/bin/sh -c" in on_posix.reason

    # 重定向同样交给适配器，原始命令原样保留（不能被切开重排）
    redirect = prepare_shell(
        "echo hi > out.txt", config=config, workspace_root=tmp_path, platform="windows"
    )
    assert not redirect.allowed and redirect.requires_approval
    assert redirect.shell_syntax == ">"
    assert redirect.shell_command == "echo hi > out.txt"
    assert build_shell_invocation(redirect)[-1] == "echo hi > out.txt"


@pytest.mark.real_resolution
def test_executor_config_can_force_pwsh_adapter(app_config: AppConfig, tmp_path: Path) -> None:
    """``executor`` 可显式指定适配器；与运行平台冲突或本机没装时都失败闭合。"""
    config = _with_shell_config(app_config, executor="pwsh").security.shell

    plan = prepare_shell(
        "Get-ChildItem | Select-String x",
        config=config,
        workspace_root=tmp_path,
        platform="windows",
    )

    assert plan.executor == EXECUTOR_PWSH
    if shutil.which("pwsh") is None:
        # 本机没有 pwsh：判定阶段就拒绝，不先让用户批准、执行时才报错
        assert not plan.allowed and not plan.requires_approval
        assert "找不到" in plan.reason
    else:
        assert not plan.allowed and plan.requires_approval
        assert plan.executable_path is not None
        assert Path(plan.executable_path).is_file()
    invocation = build_shell_invocation(plan)
    assert Path(invocation[0]).name.casefold().startswith("pwsh")
    assert invocation[1:] == [
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        "Get-ChildItem | Select-String x",
    ]

    wrong_platform = prepare_shell(
        "a | b", config=config, workspace_root=tmp_path, platform="posix"
    )
    assert not wrong_platform.allowed and not wrong_platform.requires_approval
    assert "不适用于 posix" in wrong_platform.reason


def test_argv_commands_stay_without_shell_adapter(
    app_config: AppConfig, tmp_path: Path
) -> None:
    """没有 Shell 语法就 argv 直执行，不经任何适配器。"""
    plan = prepare_shell(
        "python --version", config=app_config.security.shell, workspace_root=tmp_path
    )

    assert plan.allowed and plan.executor == EXECUTOR_ARGV
    assert not plan.uses_shell_adapter
    assert plan.shell_command is None
    invocation = build_shell_invocation(plan)
    assert Path(invocation[0]).name.casefold().startswith("python")
    assert invocation[1:] == ["--version"]


def test_absolute_path_executables_are_flagged_by_prepare_shell_then_freed_by_the_tier(
    app_config: AppConfig, tmp_path: Path
) -> None:
    """带路径的可执行文件：`prepare_shell` 照旧标"需要人工审批"（架构 §6.6），判定层则由档位免掉。

    **2026-10-15 权限模型重构**：常规命令一律免审（档位与沙箱负责边界），"带路径"因此不再单独
    触发人工批准。它守的那件事——**判定对象必须是执行对象**（带路径 ⇒ 解析后按该路径执行）——
    降层到 `prepare_shell` 验，与"要不要问用户"是两回事。
    """
    plan = prepare_shell(
        f'"{sys.executable}" --version',
        config=app_config.security.shell,
        workspace_root=tmp_path,
    )
    assert not plan.allowed and plan.requires_approval
    assert "路径" in plan.reason

    # 判定层：同一条命令不再弹审批，理由里如实带出档位与 `prepare_shell` 的原始说明。
    policy = SecurityPolicy.from_config(app_config, workspace_root=tmp_path, environ={})
    decision = policy.classify_tool("run_shell", {"command": f'"{sys.executable}" --version'})
    assert decision.allowed and not decision.requires_approval
    assert "常规命令免审批" in decision.reason


def test_path_qualified_executable_in_workspace_is_flagged_at_prepare_shell(
    app_config: AppConfig, tmp_path: Path
) -> None:
    """工作区内的相对路径可执行文件：`prepare_shell` 提级为"需要审批"并按该路径执行。"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "tool.exe").write_bytes(b"stub")

    plan = prepare_shell(
        "./tool.exe --version", config=app_config.security.shell, workspace_root=workspace
    )

    assert not plan.allowed and plan.requires_approval
    assert "路径" in plan.reason


@pytest.mark.real_resolution
def test_unresolvable_executable_is_rejected_at_judgment(
    app_config: AppConfig, tmp_path: Path
) -> None:
    """解析不到执行对象时在判定阶段就失败闭合，不能先批准、执行时才报错。"""
    policy = SecurityPolicy.from_config(app_config, workspace_root=tmp_path, environ={})

    for command in ("./python --version", "bin/python --version", "no-such-tool --flag"):
        decision = policy.classify_tool("run_shell", {"command": command})
        assert not decision.allowed, command
        assert not decision.requires_approval, command
        assert "找不到可执行文件" in decision.reason, command


def test_executable_aliases_do_not_change_the_tier_judgment(
    app_config: AppConfig, tmp_path: Path
) -> None:
    """判定层对**能解析到的常规命令**一视同仁：大小写、后缀、别名都不改变结果。

    2026-10-15 权限模型重构后判定层不看白名单（常规命令一律免审），所以"别名不共享免审资格"
    这件事降层到 `prepare_shell` 验（见 `test_shell_executable_name_matching_is_normalized`）。
    这里守的是另一半：**解析不到的仍照旧硬拒**——免掉的是审批，不是拒绝。
    """
    policy = SecurityPolicy.from_config(app_config, workspace_root=tmp_path, environ={})

    for command in (
        "PYTHON.EXE --version",
        "python --version",
        "python3 --version",
        "py --version",
        "pip --version",
        "giti status",
    ):
        plan = prepare_shell(command, config=app_config.security.shell, workspace_root=tmp_path)
        decision = policy.classify_tool("run_shell", {"command": command})
        if plan.environment_unavailable:
            # 环境里起不来：与审批无关，任何档位都拒绝。
            assert not decision.allowed and not decision.requires_approval, command
        else:
            assert decision.allowed and not decision.requires_approval, command


def test_whitelist_never_grants_extra_arguments(
    app_config: AppConfig, tmp_path: Path
) -> None:
    """``pytest`` 带任意参数、``git diff`` 带额外参数都不得**命中白名单**（T12 验收）。

    判定层已不看白名单（常规命令一律免审，2026-10-15），所以这条降到 `prepare_shell` 那一层验：
    它守的是"白名单只认一字不差的调用"这件事本身，与"命令要不要审批"是两回事。
    """
    shell_config = app_config.security.shell

    for command in (
        "pytest",
        "pytest -q",
        "pytest tests/test_security.py",
        "git diff --stat",
        "git diff HEAD~1",
        "git status --short",
        "ruff check --fix",
    ):
        plan = prepare_shell(command, config=shell_config, workspace_root=tmp_path)
        assert not plan.allowed, command
        assert plan.requires_approval, command

    # 参数完全一致的那两条仍然命中
    for command in ("pytest --version", "git diff"):
        plan = prepare_shell(command, config=shell_config, workspace_root=tmp_path)
        assert plan.allowed and not plan.requires_approval, command


def test_whitelist_prefix_matches_extra_arguments(app_config: AppConfig, tmp_path: Path) -> None:
    """``match: prefix``：条目参数是实际参数的**前缀**，允许后面跟任意参数。

    这解决的正是"`npm run build` 明明写在白名单里、却仍然被逐条问"——``exact`` 只认一字不差的调用。
    前缀只作用在**参数**上，可执行名照旧逐项比较。
    """
    config = _with_shell_config(
        app_config,
        whitelist=[
            {
                "platform": ["windows", "posix"],
                "executable": "python",
                "args": ["--version"],
                "match": "prefix",
            }
        ],
    )
    shell_config = config.security.shell

    hit = prepare_shell("python --version --help", config=shell_config, workspace_root=tmp_path)
    assert hit.allowed and not hit.requires_approval
    assert "参数前缀匹配" in hit.reason

    # 第一个参数就不对 → 照旧要问（前缀不是"这条可执行文件的任意调用"）。
    miss = prepare_shell("python -c print(1)", config=shell_config, workspace_root=tmp_path)
    assert not miss.allowed and miss.requires_approval

    # 可执行名不受前缀影响。
    other = prepare_shell("python3 --version", config=shell_config, workspace_root=tmp_path)
    assert not other.allowed and other.requires_approval


def test_prefix_whitelist_entry_needs_at_least_one_argument(app_config: AppConfig) -> None:
    """``prefix`` 空参数 = 放行整个可执行文件（`python -c …` 就是任意代码）→ 配置层直接拒绝。"""
    from pydantic import ValidationError

    data = app_config.model_dump()
    data["security"]["shell"]["whitelist"] = [
        {"platform": ["windows", "posix"], "executable": "python", "args": [], "match": "prefix"}
    ]

    with pytest.raises(ValidationError, match="至少要写一个参数"):
        AppConfig.model_validate(data)


def test_whitelist_entry_can_limit_cwd(app_config: AppConfig, tmp_path: Path) -> None:
    """白名单条目可用 ``cwd`` 限定生效目录（架构 §6.6 的匹配维度之一）。"""
    workspace = tmp_path / "ws"
    (workspace / "sub").mkdir(parents=True)
    config = _with_shell_config(
        app_config,
        whitelist=[
            {
                "platform": ["windows", "posix"],
                "executable": "python",
                "args": ["--version"],
                "match": "exact",
                "cwd": "sub",
            }
        ],
    )
    # 白名单的 cwd 维度在 `prepare_shell` 那一层验（判定层不再看白名单，见上一条的理由）。
    shell_config = config.security.shell

    inside = prepare_shell(
        "python --version", config=shell_config, workspace_root=workspace, cwd="sub"
    )
    assert inside.allowed

    outside = prepare_shell(
        "python --version", config=shell_config, workspace_root=workspace, cwd="."
    )
    assert not outside.allowed and outside.requires_approval


@pytest.mark.real_resolution
def test_blocked_patterns_match_parsed_structure(
    app_config: AppConfig, tmp_path: Path
) -> None:
    """黑名单按解析后的结构匹配：大小写、Windows 后缀与绝对路径一样拦得住（架构 §6.6）。

    命中破坏性模式后：执行对象可解析 → 进入审批；不可解析 → 直接拒绝（两者都要报危险原因）。
    """
    policy = SecurityPolicy.from_config(app_config, workspace_root=tmp_path, environ={})

    for command, executable in (
        ("rm -rf build", "rm"),
        ("format C:", "format"),
        (r"C:\Windows\System32\FORMAT.COM C:", "format"),
        ("shutdown /s /t 0", "shutdown"),
        ("del /s /q dist", "del"),
        ("chmod 777 script.sh", "chmod"),
    ):
        decision = policy.classify_tool("run_shell", {"command": command})
        assert not decision.allowed, command
        assert "破坏性命令模式" in decision.reason, command
        if shutil.which(executable) is None and not shell_guard.is_platform_builtin(
            executable, shell_guard.resolve_platform(policy.security.shell.platform)
        ):
            # 环境里没有这个可执行文件、**且它不是平台内建命令**（例如 Windows 上的 rm / chmod）
            # → 直接拒绝，不给审批入口（批准了也起不来）。
            # 内建命令（`del` / `rd`…）不同：它们经适配器**真能跑**，所以照旧进审批——
            # 2026-10-14 修掉的正是"既命中破坏性模式、又永远执行不了"这一格。
            assert not decision.requires_approval, command
            assert "找不到可执行文件" in decision.reason, command
        else:
            assert decision.requires_approval, command


@pytest.mark.real_resolution
def test_shell_plan_resolves_executable_path(
    app_config: AppConfig, tmp_path: Path
) -> None:
    """判定阶段解析出可执行文件绝对路径，执行阶段固定用它（架构 §6.6）。"""
    plan = prepare_shell(
        "python --version", config=app_config.security.shell, workspace_root=tmp_path
    )

    assert plan.executable_path is not None
    assert Path(plan.executable_path).is_file()
    assert build_shell_invocation(plan)[0] == plan.executable_path


@pytest.mark.real_resolution
def test_shell_adapter_invocation_uses_resolved_path(
    app_config: AppConfig, tmp_path: Path
) -> None:
    """Shell 适配器同样按解析出的绝对路径启动，不靠 PATH 二次搜索。"""
    plan = prepare_shell(
        "echo hi | findstr hi",
        config=app_config.security.shell,
        workspace_root=tmp_path,
        platform="windows",
    )

    assert plan.executable_path is not None
    assert Path(plan.executable_path).is_file()
    invocation = build_shell_invocation(plan)
    assert invocation[0] == plan.executable_path
    assert invocation[1:4] == ["/d", "/s", "/c"]


def test_shell_fingerprint_includes_resolved_executable(
    app_config: AppConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """可执行文件解析结果变了指纹就得变：批准不能跨执行对象生效（架构 §5.3）。"""
    policy = SecurityPolicy.from_config(app_config, workspace_root=tmp_path, environ={})
    arguments = {"command": "python --version", "cwd": ".", "env_keys": []}
    before = action_fingerprint("run_shell", arguments, policy=policy)

    monkeypatch.setattr(
        shell_guard.shutil, "which", lambda name: str(tmp_path / "python.exe")
    )
    after = action_fingerprint("run_shell", arguments, policy=policy)

    assert before != after


def test_fingerprint_separates_replace_all(
    app_config: AppConfig, tmp_path: Path
) -> None:
    """``replace_all`` 影响执行语义（改一处 vs 全部），必须体现在指纹里（架构 §5.3）。"""
    policy = SecurityPolicy.from_config(app_config, workspace_root=tmp_path, environ={})
    base = {"path": "AGENTS.md", "old_text": "a", "new_text": "b"}

    once = action_fingerprint("edit_file", {**base, "replace_all": False}, policy=policy)
    every = action_fingerprint("edit_file", {**base, "replace_all": True}, policy=policy)

    assert once != every
    # 省略参数与显式传默认值必须等价，否则同一个动作的批准会莫名其妙失效
    assert once == action_fingerprint("edit_file", base, policy=policy)


def test_classify_edit_patch_mode_needs_no_approval_when_small(
    app_config: AppConfig, tmp_path: Path
) -> None:
    """patch 模式共用同一套免审上限：小补丁直接放行，理由写明是哪种输入模式（架构 §6.3）。"""
    policy = SecurityPolicy.from_config(app_config, workspace_root=tmp_path, environ={})

    decision = policy.classify_tool(
        "edit_file", {"path": "a.py", "patch": "@@ -1 +1 @@\n-a\n+b\n"}
    )

    assert decision.allowed
    assert not decision.requires_approval
    assert "unified diff" in decision.reason


def test_classify_edit_patch_over_the_file_limit_requires_approval(
    app_config: AppConfig, tmp_path: Path
) -> None:
    """大范围编辑按"本轮触及的文件数"判，patch 模式同样走这条（2026-10-15）。

    原来判的是"改动行数 > 200"，而那正是"重写一个函数"的常态——正常重构被逐次拦下。
    """
    app_config.security.require_approval.large_batch_edit = True
    policy = SecurityPolicy.from_config(app_config, workspace_root=tmp_path, environ={})
    limit = app_config.security.large_edit.max_files_without_approval
    for index in range(limit):  # 先把免审额度用掉
        policy.note_edited(tmp_path / f"touched{index}.py")
    patch = "@@ -1 +1 @@\n-a\n+b\n"

    decision = policy.classify_tool("edit_file", {"path": "a.py", "patch": patch})

    assert not decision.allowed
    assert decision.requires_approval
    assert decision.large_edit is True
    assert "大范围修改" in decision.reason
    # 额度用完只挡**新**文件：已经改过的文件不再反复问。
    assert policy.classify_tool("edit_file", {"path": "touched0.py", "patch": patch}).allowed


def test_classify_edit_patch_structure_failure_is_a_hard_reject(
    app_config: AppConfig, tmp_path: Path
) -> None:
    """结构非法的 patch 永远不可能执行：硬拒绝，不推进审批（架构 §6.3）。"""
    policy = SecurityPolicy.from_config(app_config, workspace_root=tmp_path, environ={})

    decision = policy.classify_tool(
        "edit_file", {"path": "a.py", "patch": "@@ -1,5 +1,5 @@\n-a\n+b\n"}
    )

    assert not decision.allowed
    assert not decision.requires_approval
    assert "计数与内容不符" in decision.reason


def test_classify_edit_rejects_mixed_input_modes(app_config: AppConfig, tmp_path: Path) -> None:
    """patch 与 old_text/new_text 同时给出：硬拒绝（安全判定与工具执行同一套规则）。"""
    policy = SecurityPolicy.from_config(app_config, workspace_root=tmp_path, environ={})

    decision = policy.classify_tool(
        "edit_file",
        {"path": "a.py", "old_text": "a", "new_text": "b", "patch": "@@ -1 +1 @@\n-a\n+b\n"},
    )

    assert not decision.allowed
    assert not decision.requires_approval
    assert "不能同时使用" in decision.reason


def test_fingerprint_separates_patch_mode(app_config: AppConfig, tmp_path: Path) -> None:
    """patch 是第二输入模式：不同补丁必须算出不同指纹，省略 patch 与空串等价（架构 §5.3）。"""
    policy = SecurityPolicy.from_config(app_config, workspace_root=tmp_path, environ={})
    first = {"path": "a.py", "patch": "@@ -1 +1 @@\n-a\n+b\n"}
    second = {"path": "a.py", "patch": "@@ -1 +1 @@\n-a\n+c\n"}
    replace = {"path": "a.py", "old_text": "a", "new_text": "b"}

    fp_first = action_fingerprint("edit_file", first, policy=policy)
    assert fp_first != action_fingerprint("edit_file", second, policy=policy)
    # 精确替换与补丁模式不能撞指纹：否则批准一个补丁会放行另一次完全不同的修改
    assert fp_first != action_fingerprint("edit_file", replace, policy=policy)
    # 省略 patch 与显式空串必须等价，否则同一个动作的批准会莫名失效
    assert action_fingerprint("edit_file", replace, policy=policy) == action_fingerprint(
        "edit_file", {**replace, "patch": ""}, policy=policy
    )


def test_fingerprint_includes_timeout(app_config: AppConfig, tmp_path: Path) -> None:
    """超时是执行计划的一部分：不同超时的指纹必须不同，省略值与**配置默认值**必须相同。"""
    policy = SecurityPolicy.from_config(app_config, workspace_root=tmp_path, environ={})
    command = {"command": "python --version"}
    default_timeout = app_config.security.shell.default_timeout_seconds

    short = action_fingerprint("run_shell", {**command, "timeout_seconds": 1}, policy=policy)
    longer = action_fingerprint("run_shell", {**command, "timeout_seconds": 30}, policy=policy)

    assert short != longer
    assert action_fingerprint("run_shell", command, policy=policy) == action_fingerprint(
        "run_shell", {**command, "timeout_seconds": default_timeout}, policy=policy
    )


def test_shell_timeout_default_follows_config(
    app_config: AppConfig, tmp_path: Path
) -> None:
    """省略超时时有效默认值取**配置值**，不是配置的出厂常量 60。

    配置调小到 30 秒时：判定按 30 秒放行；指纹与显式 30 秒一致、与显式 60 秒不同。
    """
    config = _with_shell_config(app_config, default_timeout_seconds=30)
    policy = SecurityPolicy.from_config(config, workspace_root=tmp_path, environ={})
    command = {"command": "python --version"}

    assert policy.classify_tool("run_shell", command).allowed

    omitted = action_fingerprint("run_shell", command, policy=policy)
    assert omitted == action_fingerprint(
        "run_shell", {**command, "timeout_seconds": 30}, policy=policy
    )
    assert omitted != action_fingerprint(
        "run_shell", {**command, "timeout_seconds": 60}, policy=policy
    )


def test_fingerprint_keeps_shell_operators(
    app_config: AppConfig, tmp_path: Path
) -> None:
    """Shell 运算符决定命令语义，不能被 ``split_command`` 丢掉后再算指纹。"""
    policy = SecurityPolicy.from_config(app_config, workspace_root=tmp_path, environ={})

    greater = action_fingerprint(
        "run_shell", {"command": "echo hi > target.txt"}, policy=policy
    )
    less = action_fingerprint("run_shell", {"command": "echo hi < target.txt"}, policy=policy)

    assert greater != less


def test_blocked_patterns_keep_substring_fallback() -> None:
    """pattern 无法做结构匹配时退回子串兜底，且不能被相似名字误触发。"""
    # 结构匹配负责子串匹配抓不到的形态：绝对路径 + Windows 后缀 + 大写
    assert (
        match_blocked_patterns(
            [r"C:\Windows\System32\FORMAT.COM", "C:"],
            r"C:\Windows\System32\FORMAT.COM C:",
            ["format "],
        )
        == "format "
    )
    # pattern 不是合法命令形态（这里含子串但整串匹配才成立）时退回子串兜底
    assert (
        match_blocked_patterns(
            ["python", "-c", "print('x')"], 'python -c "print(\'rm -rf\')"', ["rm -rf"]
        )
        == "rm -rf"
    )
    # 不误伤：可执行名与参数结构都对不上、也没有原样子串
    assert (
        match_blocked_patterns(["python", "scripts/format.py"], "python scripts/format.py", ["format "])
        is None
    )
    assert match_blocked_patterns(["echo", "hi"], "echo hi", ["rm -rf"]) is None


def test_registry_exposes_only_read_tools_in_chat_mode(app_config: AppConfig, tmp_path: Path) -> None:
    policy = SecurityPolicy.from_config(app_config, workspace_root=tmp_path, environ={})
    registry = ToolRegistry.build(policy)

    # T50：`tools_for_mode` → `tools_for(mode, capabilities)`；默认 Profile 等价于 v0.4 的 tool 模式。
    # 2026-10-14：闲聊也带联网工具——闲聊与 Agent 的差别是"要不要动手改东西"，不是"能不能查资料"。
    assert [tool.name for tool in registry.tools_for("chat")] == [
        "list_dir",
        "read_file",
        "web_search",
        "web_fetch",
    ]
    assert [tool.name for tool in registry.tools_for("tool")] == [
        "task_control",
        "update_plan",
        "list_dir",
        "read_file",
        # 2026-09-30 新增检索工具：在此之前工具集**没有任何检索能力**，而 Shell 兜底也不可用
        # （白名单要求参数完全匹配，搜索类命令一律落到"需人工审批"）。
        "grep",
        "glob",
        # 2026-10-23：`search_files` 按文件名全项目搜索（右栏文件面板在用），排在 glob 之后。
        "search_files",
        "edit_file",
        "write_file",
        "delete_file",
        "download_file",
        # 2026-10-27：常驻记忆（§10.7）在注册表里一定有；两个 store 都关时才不绑给模型。
        "memory",
        "run_shell",
        # 2026-10-23：命令会话化之后的三个工具，注册表顺序如此。
        "read_command",
        "stop_command",
        "execute_code",
        # 2026-10-14：联网常态化（`web` 出厂即开、不可关闭），排在 `CAPABILITY_TOOLS` 的位置上——
        # 即 Git / 技能两段之前。
        "web_search",
        "web_fetch",
        # v0.5 T54：新增能力一律**追加在末尾**（前面的字节不变，前缀缓存的前半段仍可复用）。
        # 注意这里问的是**注册表**（不看配置开关），所以 git 工具会出现；真正绑定给模型时还要过
        # `capabilities.effective_capabilities`——默认 `git.enabled=false`，于是"默认档与 v0.4 一致"。
        "git_status",
        "git_diff",
        "git_log",
        "git_commit",
        # v0.5 T55：技能工具同样追加在末尾，且是**只读**的（登记在 READ_ONLY_TOOLS，不需要审批）。
        "skill_list",
        "skill_load",
        # 2026-10-16：看图（`view_image`）——只读，追加在末尾。注意这里问的是**注册表**，
        # 所以它一定会出现；真正绑不绑给模型还要过 `enabled_capabilities` 的 `vision_available`
        # （连接没声明支持图像输入时 `image_read` 不进有效能力集）。
        "view_image",
        "start_shell", "read_shell", "write_shell", "stop_shell",
        "list_shell_sessions", "inspect_system", "browser",
    ]
    assert registry.get("nope") is None


def test_git_commit_needs_no_approval_and_says_what_gets_committed(
    app_config: AppConfig, tmp_path: Path
) -> None:
    """提交改的是**仓库历史**：始终需要人工审批，且理由里带出路径与提交信息（架构 §16.4）。

    审批卡片靠 ``redacted_arguments`` 展示参数，而"要不要审批"由安全门判定；这里钉住后者，
    顺便证明只读三件套**不**需要审批（它们不写盘、不改索引）。
    """
    policy = SecurityPolicy.from_config(app_config, workspace_root=tmp_path, environ={})

    decision = policy.classify_tool(
        "git_commit", {"paths": ["notes.md", "docs/a.md"], "message": "补一段说明"}
    )
    assert decision.requires_approval is False
    assert decision.allowed is True  # "不自动放行"，与 delete_file 同一口径
    assert "notes.md" in decision.reason and "补一段说明" in decision.reason

    for name in ("git_status", "git_diff", "git_log"):
        read = policy.classify_tool(name, {})
        assert read.allowed is True and read.requires_approval is False


# --------------------------------------------------------------------------- 工具分支


def test_chat_branch_produces_no_tool_events(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    model = model_factory(decision="chat", reply="你好")

    result = builder.build_chat_graph().invoke(
        {"user_input": "你好"}, context=_context(tmp_path, model, app_config)
    )

    assert result["mode"] == "chat"
    assert not result.get("pending_tool_calls")
    assert not result.get("tool_events")
    assert not result.get("tool_results")
    assert not result.get("final_evidence")


def test_chat_mode_binds_no_tools(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """闲聊模式不把任何工具交给模型（架构 §1.3-3）。"""
    model = model_factory(decision="chat", reply="你好")
    context = _context(tmp_path, model, app_config)
    model.bound_tools = []  # 忽略能力探测阶段的绑定记录

    builder.build_chat_graph().invoke({"user_input": "你好"}, context=context)

    assert model.bound_tools == []


def test_tool_mode_binds_registered_tools(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """工具模式绑定全部已实现工具；能不能执行由安全门决定，不是"不告诉模型"。"""
    model = model_factory(decision="tool", reply="好的")

    builder.build_chat_graph().invoke(
        {"user_input": "列出当前目录"}, context=_context(tmp_path, model, app_config)
    )

    assert model.bound_tools == [
        "task_control",
        "update_plan",
        "list_dir",
        "read_file",
        "grep",
        "glob",
        # 2026-10-23：`search_files`（按文件名全项目搜索）排在 glob 之后。
        "search_files",
        "edit_file",
        "write_file",
        "delete_file",
        "download_file",
        "run_shell",
        # 2026-10-23：命令会话化之后的三个工具，与注册表顺序一致。
        "read_command",
        "stop_command",
        "execute_code",
        # 联网常态化（2026-10-14）：默认档现在也绑这两个（出厂即开、不可关闭）。
        "web_search",
        "web_fetch",
        "start_shell", "read_shell", "write_shell", "stop_shell",
        "list_shell_sessions", "inspect_system", "browser",
    ]


def test_tool_call_reads_explicit_external_path_without_approval(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """默认工作区可写档可读取明确的外部绝对路径，结果回到模型且不改变文件。"""
    outside = tmp_path / "outside.txt"
    outside.write_text(OUTSIDE_SECRET, encoding="utf-8")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    before = _snapshot(workspace)

    model = model_factory(
        decision="tool",
        reply="已读取该文件",
        tool_calls=[
            {"name": "read_file", "args": {"path": outside.as_posix()}, "id": "call_1"}
        ],
    )
    result = builder.build_chat_graph().invoke(
        {"user_input": "读取工作区外的文件"}, context=_context(workspace, model, app_config)
    )

    assert not result.get("pending_approval")
    assert result["tool_results"][0]["status"] == "ok"
    assert OUTSIDE_SECRET in json.dumps(result["tool_results"], ensure_ascii=False, default=str)
    assert OUTSIDE_SECRET in "\n".join(model.seen_prompts)
    assert _snapshot(workspace) == before
    assert result["final_response"] == "已读取该文件"


def test_secret_file_tool_call_is_denied(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    (tmp_path / ".env").write_text("DEEPSEEK_API_KEY=sk-real-secret", encoding="utf-8")
    model = model_factory(
        decision="tool",
        reply="无法读取密钥文件",
        tool_calls=[{"name": "read_file", "args": {"path": ".env"}, "id": "call_1"}],
    )

    result = builder.build_chat_graph().invoke(
        {"user_input": "读一下 .env"}, context=_context(tmp_path, model, app_config)
    )

    assert result["tool_results"][0]["status"] == "denied"
    assert "sk-real-secret" not in json.dumps(result, ensure_ascii=False, default=str)


# --------------------------------------------------------------------------- 外部路径档位（T38）


def _external_setup(tmp_path: Path) -> tuple[Path, Path, Path]:
    """（工作区，工作区外目录，外部文件）三件套。"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    note = outside / "note.txt"
    note.write_text("外部内容", encoding="utf-8", newline="")
    return workspace, outside, note


def _policy(
    app_config: AppConfig,
    workspace: Path,
    *,
    mode: str = "workspace-write",
    grants: Mapping[str, GrantAccess] | None = None,
) -> SecurityPolicy:
    return SecurityPolicy.from_config(
        app_config,
        workspace_root=workspace,
        environ={},
        external_policy=ExternalPathPolicy(mode=mode, grants=dict(grants or {})),  # type: ignore[arg-type]
    )


def _external_config(app_config: AppConfig, **overrides: object) -> AppConfig:
    data = app_config.model_dump()
    data["workspace"].update(overrides)
    return AppConfig.model_validate(data)


def test_external_read_only_tier_downgrades_grants_and_refuses_writes(
    tmp_path: Path, app_config: AppConfig
) -> None:
    """只读档允许明确外部读取，但读写授权也不能带来写权限。"""
    workspace, outside, note = _external_setup(tmp_path)
    policy = _policy(
        app_config, workspace, mode="read-only", grants={normalize_grant_path(outside): "read_write"}
    )

    target = policy.resolve_target(str(note))
    assert target.external and target.access == "read" and not target.writable, "读写授权被降级"

    created = policy.classify_tool(
        "write_file", {"path": str(outside / "new.txt"), "content": "新"}
    )
    assert not created.allowed and not created.requires_approval

    ungranted = tmp_path / "elsewhere" / "note.txt"
    assert policy.resolve_target(str(ungranted)).access == "read"


def test_external_whitelist_read_grant_allows_reading_and_refuses_writes(
    tmp_path: Path, app_config: AppConfig
) -> None:
    """``read`` 档：只读工具照常，写工具**硬拒绝**并明确说这是授权级别的问题。"""
    workspace, outside, note = _external_setup(tmp_path)
    policy = _policy(app_config, workspace, grants={normalize_grant_path(outside): "read"})

    target = policy.resolve_target(str(note))
    assert target.external and target.access == "read" and not target.writable
    assert policy.resolve_readable_file(str(note)).read_text(encoding="utf-8") == "外部内容"

    created = policy.classify_tool(
        "write_file", {"path": str(outside / "new.txt"), "content": "新"}
    )
    assert not created.allowed and not created.requires_approval  # 不是"等批准"，是权限不足
    assert "只允许读" in created.reason and "权限" in created.reason

    edited = policy.classify_tool(
        "edit_file", {"path": str(note), "old_text": "外部内容", "new_text": "改"}
    )
    assert not edited.allowed and not edited.requires_approval
    assert "只允许读" in edited.reason


def test_external_grants_cover_subdirectories_and_read_write_wins(
    tmp_path: Path, app_config: AppConfig
) -> None:
    """父目录授权覆盖子目录；更具体的 ``read_write`` 条目优先于父目录的 ``read``。"""
    workspace, outside, _note = _external_setup(tmp_path)
    sub = outside / "sub"
    sub.mkdir()
    policy = _policy(
        app_config,
        workspace,
        grants={
            normalize_grant_path(outside): "read",
            normalize_grant_path(sub): "read_write",
        },
    )

    assert policy.resolve_target(str(outside / "a.txt")).access == "read"
    assert policy.resolve_target(str(sub / "b.txt")).access == "read_write"
    assert policy.resolve_target(str(tmp_path / "elsewhere" / "c.txt")).access == "read"


def test_external_danger_tier_opens_reads_but_not_writes(
    tmp_path: Path, app_config: AppConfig
) -> None:
    """「完全放行」档只给**读**：任何绝对外部路径可读，**写**仍需逐条授权。

    与 codex 的 ``workspace-write``、dsh 的 ``writableRoots`` 同一条口径："读不受限、写由可写根围栏"。
    """
    workspace, outside, note = _external_setup(tmp_path)
    config = _external_config(app_config, external_paths={"mode": "danger-full-access"})
    policy = SecurityPolicy.from_config(config, workspace_root=workspace, environ={})

    target = policy.resolve_target(str(note))
    assert target.external and target.access == "read" and not target.writable

    created = policy.classify_tool(
        "write_file", {"path": str(outside / "new.txt"), "content": "新"}
    )
    assert not created.allowed and not created.requires_approval, "写外部是硬拒绝，不是等批准"
    assert "只允许读" in created.reason

    # 授权为读写之后才写得进去：**有没有授权**这件事压得过档位，反过来也一样。
    granted = SecurityPolicy.from_config(
        config,
        workspace_root=workspace,
        environ={},
        external_policy=ExternalPathPolicy(
            mode="danger-full-access", grants={normalize_grant_path(outside): "read_write"}
        ),
    )
    assert granted.resolve_target(str(note)).access == "read_write"


def test_external_paths_never_accept_relative_or_tilde_inputs(
    tmp_path: Path, app_config: AppConfig
) -> None:
    """``~`` 与相对路径逃逸不是"显式指定的绝对目标"：任何档位、任何授权都救不了。"""
    workspace, outside, _note = _external_setup(tmp_path)
    policy = _policy(
        app_config,
        workspace,
        mode="danger-full-access",
        grants={normalize_grant_path(outside): "read_write"},
    )

    for raw in ("~/note.txt", "../outside/note.txt"):
        with pytest.raises(WorkspaceBoundaryError) as excinfo:
            policy.resolve_path(raw)
        assert "只能用绝对路径" in str(excinfo.value)


def test_external_read_write_grant_keeps_the_four_hard_constraints(
    tmp_path: Path, app_config: AppConfig
) -> None:
    """``read_write`` 视同工作区内只改变"常规小改动是否逐次询问"，四项硬约束一条不放宽。"""
    workspace, outside, note = _external_setup(tmp_path)
    policy = _policy(app_config, workspace, grants={normalize_grant_path(outside): "read_write"})

    # 免审的那一类：在工作区外**新建**文件
    created = policy.classify_tool(
        "write_file", {"path": str(outside / "new.txt"), "content": "新"}
    )
    assert created.allowed and not created.requires_approval

    # 硬约束一：覆盖已有文件
    overwrite = policy.classify_tool(
        "write_file", {"path": str(note), "content": "改", "overwrite": True}
    )
    assert not overwrite.allowed and overwrite.requires_approval
    assert "覆盖已有文件" in overwrite.reason

    # 硬约束二：删除文件
    removed = policy.classify_tool("delete_file", {"path": str(note)})
    assert not removed.allowed and removed.requires_approval

    # 硬约束三：大范围编辑（本轮改过的文件数超过免审上限后再改新文件）
    for index in range(app_config.security.large_edit.max_files_without_approval):
        policy.note_edited(tmp_path / f"touched{index}.py")
    big = policy.classify_tool(
        "edit_file", {"path": str(note), "patch": "@@ -1 +1 @@\n-a\n+b\n"}
    )
    assert big.allowed and not big.requires_approval

    # 硬约束四：工作区外工作目录（命令本身在白名单里也不放行）
    shell = policy.classify_tool(
        "run_shell", {"command": "python --version", "cwd": str(outside)}
    )
    assert shell.allowed and not shell.requires_approval


def test_external_grants_cannot_authorize_protected_targets(
    tmp_path: Path, app_config: AppConfig
) -> None:
    """受保护目标（主目录等）不是"更高审批"，而是**不可授权**：授权了也不执行副作用。"""
    workspace, _outside, _note = _external_setup(tmp_path)

    for mode in ("workspace-write", "danger-full-access"):
        policy = _policy(
            app_config,
            workspace,
            mode=mode,
            grants={normalize_grant_path(Path.home()): "read_write"},
        )
        decision = policy.classify_tool("delete_file", {"path": str(Path.home())})
        assert not decision.allowed and not decision.requires_approval, mode
        assert "受保护目标" in decision.reason, mode


def test_legacy_allow_external_paths_flag_maps_to_danger_full_access(app_config: AppConfig) -> None:
    """T38 兼容：旧键单独出现（``true``）归一为 ``external_paths.mode: danger-full-access``。

    ``allow`` 是它的旧写法：``normalize_mode`` 把"外部读不受限"这一语义映射到新三档里最宽的一档。
    """
    data = app_config.model_dump()
    data["workspace"].pop("external_paths")  # 老配置里没有这个键
    data["workspace"]["allow_external_paths"] = True

    config = AppConfig.model_validate(data)

    assert config.workspace.external_paths.mode == "danger-full-access"


def test_legacy_flag_conflicting_with_an_explicit_mode_is_rejected(app_config: AppConfig) -> None:
    """旧键 ``true`` + 新键显式写了别的档位：说不清谁生效，直接拒绝。"""
    data = app_config.model_dump()
    data["workspace"]["allow_external_paths"] = True
    data["workspace"]["external_paths"] = {"mode": "read-only"}

    with pytest.raises(ValidationError) as excinfo:
        AppConfig.model_validate(data)

    assert "语义冲突" in str(excinfo.value)


def test_safe_read_only_commands_are_listed_by_code_not_by_user_config() -> None:
    """只读安全命令的**代码层**清单（2026-10-14）：`time /t`、`whoami`、`date` 一类不该每次弹审批。

    实证来源：用户问"现在几点"，模型调 `time /T` 撞上审批、被拒绝后**此后每次都失败**
    （另一半原因在 :func:`find_rejection` 的作用域，见下条用例）。因为用户白名单是"用户自己配"的，
    出厂只含几条开发命令，所以这批读命令必须由**代码**兜住。
    """
    from local_agent.security.shell_guard import is_safe_read_only

    # 大小写不固定（用户写的就是 `time /T`）
    for argv in (["time", "/T"], ["time", "/t"], ["date", "/t"], ["whoami"], ["hostname"]):
        assert is_safe_read_only(argv, "windows") is True, argv
    for argv in (["date"], ["pwd"], ["uname"], ["id"], ["whoami"]):
        assert is_safe_read_only(argv, "posix") is True, argv
    # `echo` 带任意参数都只输出
    assert is_safe_read_only(["echo", "hi"], "windows") is True
    assert is_safe_read_only(["echo", "hi"], "posix") is True

    # **不收**的几类，各有理由：
    assert is_safe_read_only(["time"], "windows") is False, "裸 time 会询问并改系统时钟"
    assert is_safe_read_only(["date"], "windows") is False, "裸 date 同上"
    assert is_safe_read_only(["time", "/t", "extra"], "windows") is False, "参数不完全一致"
    assert is_safe_read_only([r"C:\Windows\System32\whoami.exe"], "windows") is False, "带路径"
    assert is_safe_read_only(["whoami"], "posix") is True and is_safe_read_only(["ver"], "posix") is False, (
        "平台各自的清单不能互串"
    )


def test_rejection_scope_is_the_current_run_only() -> None:
    """拒绝只在**本轮**拦：跨轮不再永久封禁（2026-10-14 修正）。

    原实现"故意不按轮次隔离"，代价是用户拒绝过一次 `time /t` 之后，"现在几点"此后每次都直接失败、
    连审批卡都不再弹——实证里用户反复撞上这件事。
    """
    from local_agent.security.approval import find_rejection

    fingerprint = "fp-1"
    history = [
        {
            "decision": "reject",
            "action_fingerprint": fingerprint,
            "run_id": "run-a",
            "tool_call_id": "call-1",
        }
    ]

    # 同一轮内：换个 tool_call_id 也绕不过（`find_rejection` 只看指纹，不看调用 ID）。
    assert find_rejection(history, fingerprint, run_id="run-a") is not None
    # 换一轮：可以再问一次（这正是本次修正的目的）。
    assert find_rejection(history, fingerprint, run_id="run-b") is None


def test_credential_directories_stay_unwritable_inside_an_authorised_grant(
    tmp_path: Path, app_config: AppConfig
) -> None:
    """授权一个工作区外目录 ≠ 能改写它里面的**凭据目录**（照 codex 的强制只读子路径做）。

    codex 在可写根里把 `.git` / `.git/hooks` / `.codex` / `.aws` 一类列为**始终只读**；dsh 没有这一层。
    我们跟 codex：`.aws/credentials`、`.ssh/id_rsa` 被改写，后果远超"改错一个文件"，所以它们
    **不可审批**——授权与「放行」档都不放行，工作区**内**同样拒（这不是越界问题，是"不可写"问题）。
    """
    workspace, outside, _note = _external_setup(tmp_path)
    policy = _policy(app_config, workspace, grants={normalize_grant_path(outside): "read_write"})

    # 授权目录本身可写——没有这一步，下面的拒绝就证明不了是"凭据目录"在起作用。
    assert policy.resolve_writable_path(str(outside / "ok.txt")) == (outside / "ok.txt").resolve()

    # 这些路径**都写不出去**，但拒绝理由可以不同：文件名本身疑似密钥的（`credentials`、`id_rsa`）
    # 由敏感文件规则先拦，目录级的由"受保护目标"拦。判据是"写不出去"，不是"具体哪句话"。
    for relative in (".aws/credentials", ".ssh/id_rsa", ".kube/config", ".git/config"):
        with pytest.raises(SecurityError):
            policy.resolve_writable_path(str(outside / relative))

    # 目录级保护的**独立证据**：文件名本身毫不敏感，仍然因为落在凭据目录里被拒。
    for directory in (".aws", ".ssh", ".codex", ".kube", ".gnupg", ".docker"):
        with pytest.raises(SecurityError, match="受保护目标"):
            policy.resolve_writable_path(str(outside / directory / "notes.txt"))

    # 工作区**内**同样拒：这不是"越界"问题，是"不可写"问题。
    with pytest.raises(SecurityError, match="受保护目标"):
        policy.resolve_writable_path(str(workspace / ".ssh" / "notes.txt"))


def test_external_paths_config_survives_a_dump_and_validate_round_trip(
    app_config: AppConfig,
) -> None:
    """往返必须可行：``model_dump()`` 会把新旧两个键都写出来，那不是冲突。"""
    for mode in ("read-only", "workspace-write", "danger-full-access"):
        config = _external_config(app_config, external_paths={"mode": mode})
        again = AppConfig.model_validate(config.model_dump())
        assert again.workspace.external_paths.mode == mode, mode
