"""配置里的相对路径基准：由**装配/入口**按 ``base_dir`` 展开，与进程当前目录无关（架构 §8）。

回归的是 2026-10-09 实测到的缺陷：`container.open_runtime` 用 `resolve_under(base_dir, …)` 解析
Checkpoint、工作区与设置库，而 `graph/nodes.py` 直接拿 `memory.long_term.path` 去 `open_store`
——换一个目录启动，**会话还在原地、长期记忆却换成了另一个库**（T40 的"解压双击"场景）。

另一半同样重要：展开只能在**装配/入口**做，**不能**在 `load_config` 里做。加载期绝对化架空的正是
``base_dir`` 这个参数（测试与工具靠它把数据放进临时目录），也废掉"从 A 加载配置、把数据放到 B"的用法。
2026-10-09 试过那一版：12 个用例立刻读到了仓库里**真实的**设置库（出现用户自己的连接名），
所以这里用一条用例把"`load_config` 保持相对"钉住，避免以后又走回去。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from local_agent.api.container import open_runtime, resolve_memory_paths
from local_agent.config import load_config

#: 一份刚好通过校验、且把三条记忆库路径都写成相对形式的配置。
CONFIG_TEXT = """\
memory:
  short_term:
    path: data/checkpoints.sqlite
  medium_term:
    path: data/chroma
  long_term:
    path: data/memories.sqlite
"""


def _write_config(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "config.yaml"
    path.write_text(CONFIG_TEXT, encoding="utf-8")
    return path


def test_load_config_keeps_relative_paths(tmp_path: Path) -> None:
    """加载期不展开：基准由装配/入口给出（见模块 docstring 里那次失败的尝试）。"""
    config = load_config(_write_config(tmp_path / "cfg"))

    assert config.memory.short_term.path == "data/checkpoints.sqlite"
    assert config.memory.long_term.path == "data/memories.sqlite"


def test_memory_paths_follow_base_dir_and_ignore_the_process_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同一个配置 + 同一个 base_dir，从任意目录解析都必须得到同一套库路径。"""
    config_dir = tmp_path / "cfg"
    config = load_config(_write_config(config_dir))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    monkeypatch.chdir(config_dir)
    first = resolve_memory_paths(config, config_dir)
    monkeypatch.chdir(elsewhere)
    second = resolve_memory_paths(config, config_dir)

    for field in ("short_term", "medium_term", "long_term"):
        resolved = Path(getattr(first.memory, field).path)
        assert resolved == config_dir / "data" / {  # 基准是 base_dir，与 CWD 无关
            "short_term": "checkpoints.sqlite",
            "medium_term": "chroma",
            "long_term": "memories.sqlite",
        }[field], field
        assert getattr(second.memory, field).path == getattr(first.memory, field).path, field


def test_absolute_memory_paths_are_left_untouched(tmp_path: Path) -> None:
    """已经是绝对路径的键不拼接、不改写。"""
    target = tmp_path / "outside" / "m.sqlite"
    config = load_config(_write_config(tmp_path / "c"))  # 只借用它的其它字段
    config = config.model_copy(
        update={
            "memory": config.memory.model_copy(
                update={
                    "long_term": config.memory.long_term.model_copy(
                        update={"path": str(target)}
                    )
                }
            )
        }
    )

    resolved = resolve_memory_paths(config, tmp_path / "base")

    assert Path(resolved.memory.long_term.path) == target


def test_runtime_creates_databases_next_to_the_config_not_the_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """端到端：换一个目录启动，数据库仍然建在 ``base_dir`` 下。"""
    config_dir = tmp_path / "cfg"
    config = load_config(_write_config(config_dir))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    runtime = open_runtime(config, base_dir=config_dir, instance_lock=False)
    try:
        assert (config_dir / "data" / "checkpoints.sqlite").is_file()
        assert not (elsewhere / "data").exists()
    finally:
        runtime.close()
