"""冻结形态路径口径的回归（T40）。

这些用例**不真的打包**：``is_frozen()`` 与 ``LOCALAPPDATA`` 是本模块仅有的两个输入，直接替换
它们就能覆盖"资源在包里、数据在用户目录"的全部组合，以及取不到环境变量时的回落。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import uvicorn

from local_agent import app_paths
from local_agent.api import __main__ as api_main


def test_source_mode_uses_the_project_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """源码配置固定在项目目录，不随 CWD 或 LOCALAPPDATA 漂移。"""
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    assert app_paths.is_frozen() is False
    assert app_paths.resource_dir() is None
    assert app_paths.data_dir() is None
    assert app_paths.bundled_static_dir() is None
    assert app_paths.default_config_path() == Path(__file__).resolve().parents[1] / "config.yaml"
    assert app_paths.default_env_path() == Path(__file__).resolve().parents[1] / ".env"


def test_frozen_mode_puts_user_data_under_local_appdata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """冻结形态的配置与 .env 都落在数据目录（计划 §T40：覆盖升级后数据仍在）。"""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    assert app_paths.data_dir() == tmp_path / "chaiai"
    assert app_paths.default_config_path() == tmp_path / "chaiai" / "config.yaml"
    assert app_paths.default_env_path() == tmp_path / "chaiai" / ".env"


def test_frozen_mode_reads_the_frontend_from_the_bundle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """前端产物是**只读资源**，从 ``_MEIPASS`` 取，不落进数据目录。"""
    resources = tmp_path / "bundle"
    dist = resources / "frontend" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<title>chaiai</title>", encoding="utf-8")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(resources), raising=False)

    assert app_paths.resource_dir() == resources
    assert app_paths.bundled_static_dir() == dist


def test_frozen_without_a_bundled_frontend_returns_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """包内没有前端产物时返回 None：交回 ``web.static_dir`` 那条老路径（服务仍能起，只提供 API）。"""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)

    assert app_paths.bundled_static_dir() is None


def test_bundled_binary_prefers_the_package_copy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """随包的外部可执行文件（如 rg.exe）在 ``_internal/bin``；源码形态一律返回 None。"""
    resources = tmp_path / "bundle"
    (resources / "bin").mkdir(parents=True)
    rg = resources / "bin" / "rg.exe"
    rg.write_bytes(b"MZ")

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(resources), raising=False)

    assert app_paths.bundled_binary("rg") == rg
    assert app_paths.bundled_binary("rg.exe") == rg
    assert app_paths.bundled_binary("missing") is None

    monkeypatch.setattr(sys, "frozen", False, raising=False)
    assert app_paths.bundled_binary("rg") is None


def test_frozen_without_local_appdata_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """异常环境（非 Windows、环境变量被裁剪）：回落相对默认值，**不凭空造**写不进去的目录。"""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)

    assert app_paths.data_dir() is None
    assert app_paths.default_config_path() == Path("config.yaml")
    assert app_paths.default_env_path() == Path(".env")


def test_entry_point_writes_everything_into_the_data_dir_when_frozen(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """不给 ``--config`` 时，冻结形态的库与解析基准都在数据目录——不随当前工作目录漂移。"""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: None)

    assert api_main.main(["--no-open-browser"]) == 0

    data_dir = tmp_path / "chaiai"
    assert (data_dir / "data" / "checkpoints.sqlite").is_file()
    assert (data_dir / "data" / "model_settings.sqlite").is_file()

def test_frozen_mode_finds_the_bundled_skills(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """技能目录与 ``chaiai.exe`` 同级（2026-10-20）。

    冻结形态的配置在数据目录，那里没有 config.yaml、也没有 skills；不把包内这份认出来，
    界面上就永远"识别不到技能"（包里的目录存在，只是没有任何发现根指向它）。
    """
    exe_dir = tmp_path / "chaiai"
    skills = exe_dir / "skills"
    skills.mkdir(parents=True)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe_dir / "chaiai.exe"), raising=False)

    assert app_paths.bundled_skills_dir() == skills

    monkeypatch.setattr(sys, "frozen", False, raising=False)
    assert app_paths.bundled_skills_dir() is None, "源码形态不引入第二套路径"


def test_source_mcp_path_is_stable_after_changing_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    expected = app_paths.default_config_path().with_name("mcp.json")
    monkeypatch.chdir(tmp_path)
    assert app_paths.default_config_path().with_name("mcp.json") == expected
