"""`execute_code`（2026-10-22）：一次调用完成"写脚本 → 执行 → 清理"。"""

from __future__ import annotations

import json
from pathlib import Path

from local_agent.config import AppConfig
from local_agent.security.policy import SecurityPolicy
from local_agent.tools.shell_tools import build_shell_tools


def _tool(tmp_path: Path):
    policy = SecurityPolicy.from_config(AppConfig(), workspace_root=tmp_path, environ={})
    return {tool.name: tool for tool in build_shell_tools(policy)}["execute_code"]


def test_execute_code_runs_python_and_leaves_no_script_behind(tmp_path: Path) -> None:
    result = json.loads(_tool(tmp_path).invoke({"code": "print(1 + 1)"}))
    assert result["exit_code"] == 0
    assert "2" in result["stdout"]
    # 脚本目录执行完就删：`.local-agent/tmp/` 下不留残留。
    assert list((tmp_path / ".local-agent" / "tmp").iterdir()) == []
    # 展示用的命令是可读的，不是临时脚本路径。
    assert "python 代码片段" in result["command"]


def test_execute_code_can_write_into_the_workspace(tmp_path: Path) -> None:
    _tool(tmp_path).invoke(
        {"code": "import pathlib; pathlib.Path('out.txt').write_text('x', encoding='utf-8')"}
    )
    assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "x"
