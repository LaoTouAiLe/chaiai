"""数据路径的基准：**数据根**（绝对路径），与进程当前目录无关（架构 §8；2026-10-26 修订）。

两段历史，都要保住：

1. 2026-10-09：`graph/nodes.py` 直接拿 `memory.long_term.path` 去 `open_store`，而装配层只解析了
   Checkpoint——换一个目录启动，**会话还在原地、长期记忆却换成了另一个库**（T40 的"解压双击"）。
   修法是在装配/入口统一展开。
2. 2026-10-26：展开的基准从"配置文件所在目录"改成**数据根**。理由不是漂移（那条 2026-10-09 就修了），
   而是"数据不能跟着代码走"：源码目录会被 `git clean`、重新 clone、换分支，数据留在代码树里就等于
   换一份检出换一套数据。基准改成绝对路径后，两条历史要求同时成立。

另一半同样重要：展开只能在**装配/入口**做，**不能**在 `load_config` 里做——加载期绝对化会架空
"从 A 加载配置、把数据放到 B"的用法（测试与工具全靠它把数据放进临时目录）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from local_agent.api.container import open_runtime, resolve_data_paths, resolve_skill_paths
from local_agent.config import AppConfig, SkillsConfig, load_config

#: 一份刚好通过校验、且把三条记忆库路径都写成相对形式的配置。
#: 相对谁 = ``data_root``（2026-10-26 起），所以这里直接写文件名，不再写 ``data/...``。
CONFIG_TEXT = """\
memory:
  short_term:
    path: checkpoints.sqlite
  medium_term:
    path: chroma
  long_term:
    path: memories.sqlite
model_settings:
  path: model_settings.sqlite
"""


def _write_config(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "config.yaml"
    path.write_text(CONFIG_TEXT, encoding="utf-8")
    return path


def test_resolve_skill_paths_pins_relative_dirs_to_base_dir(tmp_path: Path) -> None:
    """`skills.user_dirs` 的相对路径按 **base_dir** 展开（2026-10-16，与数据路径不同基准）。

    这条是"**技能随包发布**"能成立的前提：包里那份配置写的是 `user_dirs: ["skills"]`，展开后才指向
    **包所在目录**下的 `skills/`——换盘、换机器、换解压位置都不用改配置。
    绝对路径**原样保留**（与 `resolve_under` 逐字一致），没写 `user_dirs` 时原样返回。
    """
    base = tmp_path / "unpacked"
    keep = tmp_path / "keep-skills"
    config = load_config(_write_config(base)).model_copy(
        update={
            "skills": SkillsConfig(
                enabled=True, user_dirs=["skills", str(keep)]
            )
        }
    )

    resolved = resolve_skill_paths(config, base)

    assert resolved.skills.user_dirs == [str(base / "skills"), str(keep)]
    assert resolve_skill_paths(
        config.model_copy(update={"skills": SkillsConfig()}), base
    ).skills.user_dirs == []


def test_load_config_keeps_relative_paths(tmp_path: Path) -> None:
    """加载期不展开：基准由装配/入口给出（见模块 docstring 里那次失败的尝试）。"""
    config = load_config(_write_config(tmp_path / "cfg"))

    assert config.memory.short_term.path == "checkpoints.sqlite"
    assert config.memory.long_term.path == "memories.sqlite"


def test_data_paths_are_anchored_to_data_root_and_ignore_the_process_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同一个配置 + 同一个数据根，从任意目录解析都必须得到同一套绝对路径。"""
    root = tmp_path / "data-root"
    config = load_config(_write_config(tmp_path / "cfg")).model_copy(
        update={"data_root": str(root)}
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    monkeypatch.chdir(tmp_path)
    first = resolve_data_paths(config)
    monkeypatch.chdir(elsewhere)
    second = resolve_data_paths(config)

    for field in ("short_term", "medium_term", "long_term"):
        assert Path(getattr(first.memory, field).path) == root / {
            "short_term": "checkpoints.sqlite",
            "medium_term": "chroma",
            "long_term": "memories.sqlite",
        }[field], field
        assert getattr(second.memory, field).path == getattr(first.memory, field).path, field
    # 设置库是同类的应用状态，一起跟数据根走（2026-10-26 扩的范围）。
    assert Path(first.model_settings.path) == root / "model_settings.sqlite"


def test_default_data_root_is_a_platform_directory() -> None:
    """不配 ``data_root`` 时用平台标准目录，**不是**相对路径。"""
    from local_agent import app_paths

    resolved = resolve_data_paths(AppConfig())

    assert Path(resolved.memory.short_term.path).is_absolute()
    assert Path(resolved.memory.short_term.path).parent == app_paths.data_root()


def test_relative_data_root_is_rejected() -> None:
    """相对路径会让"数据在哪"重新取决于启动目录，正是这个字段要消灭的东西。"""
    with pytest.raises(ValueError, match="必须是绝对路径"):
        AppConfig(data_root="data/here")


def test_absolute_memory_paths_are_left_untouched(tmp_path: Path) -> None:
    """已经是绝对路径的键不拼接、不改写。"""
    target = tmp_path / "outside" / "m.sqlite"
    config = load_config(_write_config(tmp_path / "c")).model_copy(
        update={"data_root": str(tmp_path / "root")}
    )
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

    resolved = resolve_data_paths(config)

    assert Path(resolved.memory.long_term.path) == target


def test_runtime_creates_databases_in_the_data_root_not_the_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """端到端：换一个目录启动，数据库仍然建在数据根下。"""
    root = tmp_path / "data-root"
    config = load_config(_write_config(tmp_path / "cfg")).model_copy(
        update={"data_root": str(root)}
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    runtime = open_runtime(config, base_dir=tmp_path / "cfg", instance_lock=False)
    try:
        assert (root / "checkpoints.sqlite").is_file()
        assert not (elsewhere / "data").exists()
        # 附件同属应用状态，必须跟数据根，而不是配置文件目录（目录本身懒建，所以查解析结果）。
        store = runtime.service._attachments
        assert store is not None and store.root == root / "attachments"
    finally:
        runtime.close()


def test_resolve_skill_paths_appends_the_bundled_skills_when_frozen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """冻结形态把**包内**的 `skills/` 追加成发现根（2026-10-20）。

    包里的技能就在 exe 同级，但数据目录里既没有 config.yaml 也没有 skills；不追加这一条，
    设置页与模型清单永远是空的（用户看到的就是"识别不到技能"）。
    """
    from local_agent import app_paths

    bundled = tmp_path / "chaiai" / "skills"
    bundled.mkdir(parents=True)
    monkeypatch.setattr(app_paths, "bundled_skills_dir", lambda: bundled)
    config = load_config(_write_config(tmp_path / "cfg")).model_copy(
        update={"skills": SkillsConfig(enabled=True, user_dirs=[])}
    )

    resolved = resolve_skill_paths(config, tmp_path / "cfg")

    assert resolved.skills.user_dirs == [str(bundled)]
