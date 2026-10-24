"""验证类命令的轮内去重（2026-10-22）：只缓存无副作用的检查。"""

from __future__ import annotations

from local_agent.tools.shell_tools import _cacheable_command


def test_only_verification_commands_are_cacheable() -> None:
    for command in (
        "pytest -q",
        "python -m pytest tests/test_x.py",
        "npx tsc --noEmit",
        "npx vitest run src/app",
        "git status",
        "git diff --stat",
        "ruff check src",
    ):
        assert _cacheable_command(command), command


def test_side_effect_commands_are_never_cached() -> None:
    for command in (
        "npm start",
        "npm run dev",
        "git commit -m x",
        "git push",
        "rm -rf build",
        # 前缀边界：不能因为以 pytest 开头就当验证命令。
        "pytestx",
    ):
        assert not _cacheable_command(command), command
