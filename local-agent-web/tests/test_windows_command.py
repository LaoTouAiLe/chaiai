"""真实执行 Windows 命令，覆盖普通与受限令牌两条启动路径的引号语义。"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from local_agent.config import ExternalPathsConfig, SecurityConfig, WorkspaceConfig
from local_agent.security.policy import SecurityPolicy
from local_agent.security.process_guard import ProcessRegistry, SandboxRequest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="验证 cmd.exe 的 Windows 引号语义")


@pytest.fixture(params=[False, True], ids=["direct", "sandbox"])
def sandbox_request(request, tmp_path: Path) -> SandboxRequest | None:
    policy = SecurityPolicy(
        workspace_root=tmp_path,
        security=SecurityConfig(),
        workspace=WorkspaceConfig(
            external_paths=ExternalPathsConfig(
                mode="workspace-write" if request.param else "danger-full-access"
            )
        ),
    )
    return policy.shell_sandbox_request()


@pytest.mark.parametrize("text", [r"C:\path with spaces\item", "A&B"])
def test_cmd_preserves_quoted_text(tmp_path, sandbox_request, text) -> None:
    result = ProcessRegistry().run(
        [os.environ["COMSPEC"], "/d", "/s", "/c", f'echo "{text}"'],
        cwd=tmp_path,
        sandbox=sandbox_request,
        timeout_seconds=30,
    )
    assert result.exit_code == 0, result.stderr
    assert result.stdout.decode().strip() == f'"{text}"'
    assert result.stderr == b""
    assert not result.timed_out


def test_cmd_runs_quoted_executable_and_literal_argument(tmp_path, sandbox_request) -> None:
    script = tmp_path / "echo argument.py"
    script.write_text("import json, sys; print(json.dumps(sys.argv[1]))", encoding="utf-8")
    text = "value with spaces & literal"
    result = ProcessRegistry().run(
        [os.environ["COMSPEC"], "/d", "/s", "/c", f'"{sys.executable}" "{script}" "{text}"'],
        cwd=tmp_path,
        sandbox=sandbox_request,
        timeout_seconds=30,
    )
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout) == text
    assert result.stderr == b""


def test_cmd_start_accepts_empty_window_title(tmp_path, sandbox_request) -> None:
    # 与 start "" site.html 共用的空标题解析；以确定性的本地子程序代替 GUI。
    script = tmp_path / "browser substitute.py"
    script.write_text(
        "from pathlib import Path; Path('opened marker.txt').write_text('opened')",
        encoding="utf-8",
    )
    result = ProcessRegistry().run(
        [
            os.environ["COMSPEC"], "/d", "/s", "/c",
            f'start "" /b /wait "{sys.executable}" "{script}"',
        ],
        cwd=tmp_path,
        sandbox=sandbox_request,
        timeout_seconds=30,
    )
    assert result.exit_code == 0, result.stderr
    assert (tmp_path / "opened marker.txt").read_text() == "opened"
    assert result.stderr == b""
    assert not result.timed_out
