"""设置页「关于」的回归（2026-10-27）：版本、安装形态、检查更新、卸载交接。

三条要守住的口径：

1. **三种形态不混**：安装版（有 Inno 卸载器）/ 绿色包（解压即用）/ 源码。判断只看
   "冻结没有"与"卸载器在不在"，不猜路径。
2. **检查更新永不把设置页变成错误页**：网络、限流、格式变化一律 ``unavailable`` + note，
   且**总是**给出发布页——那是用户此刻唯一能自己走完的路。
3. **卸载顺序是硬要求**：本进程必须先退出，否则 Windows 删不掉运行中的 exe；数据删除
   放在卸载器**之后**，卸载失败时数据还在。

这些用例**不真的卸载、不真的联网**：``fetch`` 与 ``popen`` 都是注入点。
"""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.error
from pathlib import Path
from typing import Any

import pytest
import tomllib
from fastapi.testclient import TestClient

from local_agent import __version__, app_maintenance, app_paths
from local_agent.api.app import create_app
from local_agent.api.container import open_runtime
from local_agent.application.errors import UNINSTALL_UNAVAILABLE
from local_agent.config import AppConfig

LOCAL_BASE_URL = "http://127.0.0.1"
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _fake_project_version() -> str:
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as handle:
        return str(tomllib.load(handle)["project"]["version"])


def test_package_version_matches_pyproject() -> None:
    """运行期版本号只有一个真源（``__init__.__version__``），pyproject 必须与它一致。

    两处漂移的症状很隐蔽：设置页说 0.1.0、安装包叫 0.2.0、GitHub 上又是另一个 tag。
    所以把"两份事实"降级成"一条断言"，漂移直接变红。
    """
    assert _fake_project_version() == __version__


# ---- 安装形态 ---------------------------------------------------------------


def test_source_checkout_has_no_install_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    assert app_maintenance.install_kind() == "source"
    assert app_paths.install_dir() is None
    assert app_paths.uninstaller_path() is None


def test_frozen_bundle_with_the_innosetup_uninstaller_is_installed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    exe_dir = tmp_path / "Programs" / "chaiai"
    exe_dir.mkdir(parents=True)
    (exe_dir / app_paths.UNINSTALLER_NAME).write_bytes(b"MZ")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe_dir / "chaiai.exe"), raising=False)

    assert app_maintenance.install_kind() == "installed"
    assert app_paths.install_dir() == exe_dir
    assert app_paths.uninstaller_path() == exe_dir / app_paths.UNINSTALLER_NAME


def test_frozen_bundle_without_the_uninstaller_is_portable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """绿色包没有卸载器：那种形态的"卸载"是删目录，页面必须说清，而不是给个点不动的按钮。"""
    exe_dir = tmp_path / "chaiai"
    exe_dir.mkdir()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe_dir / "chaiai.exe"), raising=False)

    assert app_maintenance.install_kind() == "portable"
    assert app_maintenance.uninstall_note("portable") is not None
    assert app_maintenance.uninstall_note("installed") is None


# ---- 构建信息 ---------------------------------------------------------------


def test_build_info_reads_the_four_known_keys(tmp_path: Path) -> None:
    (tmp_path / "BUILD_INFO.txt").write_text(
        "构建时间(UTC)：2026-10-27T01:02:03+00:00\n"
        "Python：3.13.1\n"
        "PyInstaller：6.11.0\n"
        "Node：v22.5.0\n"
        "npm：10.9.0\n"
        "源树 HEAD：abcdef1234567890\n"
        "未提交改动条数：2\n"
        "构建方式：scripts/build_windows_package.py（spec：scripts/chaiai.spec，onedir）\n"
        "数据目录口径：%LOCALAPPDATA%\\chaiai（只读资源在 _internal）\n",
        encoding="utf-8",
    )

    info = app_maintenance.read_build_info(tmp_path)

    assert info is not None
    assert info.built_at == "2026-10-27T01:02:03+00:00"
    assert info.revision == "abcdef1234567890"
    assert info.dirty == "2"
    assert info.python == "3.13.1"


def test_build_info_is_absent_rather_than_half_true(tmp_path: Path) -> None:
    """缺关键键时返回 None：页面宁可**不显示**构建信息，也不把半截数据当事实。"""
    (tmp_path / "BUILD_INFO.txt").write_text("Python：3.13.1\n", encoding="utf-8")

    assert app_maintenance.read_build_info(tmp_path) is None
    assert app_maintenance.read_build_info(None) is None


# ---- 删数据的护栏 -----------------------------------------------------------


def test_removal_allows_a_normal_app_data_dir(tmp_path: Path) -> None:
    target = tmp_path / "chaiai-data"
    target.mkdir()

    assert app_maintenance.removal_block_reason(target) is None


def test_removal_refuses_a_drive_root() -> None:
    """``rmdir /s /q`` 没有回收站：一个拼错的路径就是用户的整个盘。"""
    root = Path(Path.cwd().anchor)
    if not root.is_dir():  # pragma: no cover - 只在异常环境
        pytest.skip("当前平台没有可校验的盘符根")

    assert app_maintenance.removal_block_reason(root) is not None


def test_removal_refuses_the_user_home_and_its_ancestors() -> None:
    home = Path.home()
    assert app_maintenance.removal_block_reason(home) is not None
    assert app_maintenance.removal_block_reason(home.parent) is not None


def test_removal_refuses_a_relative_or_missing_path(tmp_path: Path) -> None:
    assert app_maintenance.removal_block_reason(Path("data")) is not None
    assert app_maintenance.removal_block_reason(tmp_path / "not-there") is not None


# ---- 检查更新 ---------------------------------------------------------------


def _release(tag: str) -> dict[str, Any]:
    return {
        "tag_name": tag,
        "html_url": "https://github.com/LaoTouAiLe/chaiai/releases/tag/" + tag,
        "published_at": "2026-10-26T12:00:00Z",
    }


def test_check_update_reports_a_newer_release() -> None:
    result = app_maintenance.check_update(
        current="0.1.0", fetch=lambda _url, _timeout: _release("v0.2.0")
    )

    assert result.status == "update_available"
    assert result.latest_version == "0.2.0"
    assert result.release_url is not None and result.release_url.endswith("v0.2.0")


def test_check_update_says_up_to_date() -> None:
    result = app_maintenance.check_update(
        current="0.2.0", fetch=lambda _url, _timeout: _release("v0.2.0")
    )

    assert result.status == "up_to_date"
    assert result.note is not None


def test_check_update_treats_rate_limiting_as_unavailable_with_a_release_page() -> None:
    def forbidden(_url: str, _timeout: float) -> Any:
        raise urllib.error.HTTPError(_url, 403, "Forbidden", {}, None)

    result = app_maintenance.check_update(current="0.1.0", fetch=forbidden)

    assert result.status == "unavailable"
    assert result.release_url == app_maintenance.RELEASE_PAGE_URL
    assert "频繁" in (result.note or "")


def test_check_update_survives_network_errors() -> None:
    def offline(_url: str, _timeout: float) -> Any:
        raise urllib.error.URLError("no route")

    result = app_maintenance.check_update(current="0.1.0", fetch=offline)

    assert result.status == "unavailable"
    assert result.release_url == app_maintenance.RELEASE_PAGE_URL


def test_check_update_rejects_a_release_without_a_tag() -> None:
    result = app_maintenance.check_update(current="0.1.0", fetch=lambda _u, _t: {"name": "x"})

    assert result.status == "unavailable"
    assert result.latest_version is None


def test_check_update_refuses_to_compare_unparseable_versions() -> None:
    result = app_maintenance.check_update(
        current="0.1.0", fetch=lambda _u, _t: _release("nightly")
    )

    assert result.status == "unavailable"
    assert "无法比较" in (result.note or "")


# ---- 卸载脚本 ---------------------------------------------------------------


def test_uninstall_script_kills_this_process_before_the_uninstaller() -> None:
    """顺序错了就永远卸不掉：运行中的 exe 不能被删除或替换。"""
    script = app_maintenance._uninstall_script(
        pid=4321,
        uninstaller=Path(r"C:\Programs\chaiai\unins000.exe"),
        data_root=Path(r"D:\chaiai-data"),
        pointer=Path(r"C:\Users\me\AppData\Local\chaiai\data-root.txt"),
        remove_data=True,
    )

    assert script.index("taskkill /F /PID 4321") < script.index('start "" /wait "%UNINS%"')
    # 数据删除必须在卸载器之后：卸载失败时数据还在，用户可以重来。
    assert script.index('start "" /wait "%UNINS%"') < script.index('rmdir /s /q "%DATA%"')
    assert 'set "REMOVE=1"' in script
    assert 'del /q "%POINTER%"' in script


def test_uninstall_script_without_data_removal_leaves_the_root_alone() -> None:
    script = app_maintenance._uninstall_script(
        pid=1,
        uninstaller=Path(r"C:\a\unins000.exe"),
        data_root=Path(r"D:\data"),
        pointer=Path(r"C:\p\data-root.txt"),
        remove_data=False,
    )

    assert 'set "REMOVE=0"' in script
    # 分支还在，但条件不成立：`if "%REMOVE%"=="1"` 挡住两个删除动作。
    assert 'if "%REMOVE%"=="1"' in script


def test_uninstall_script_escapes_percent_and_refuses_quotes() -> None:
    script = app_maintenance._uninstall_script(
        pid=1,
        uninstaller=Path(r"C:\a\unins000.exe"),
        data_root=Path(r"D:\100%data"),
        pointer=Path(r"C:\p\data-root.txt"),
        remove_data=True,
    )
    assert r"D:\100%%data" in script

    with pytest.raises(ValueError):
        app_maintenance._uninstall_script(
            pid=1,
            uninstaller=Path(r"C:\a\unins000.exe"),
            data_root=Path('D:\\bad"path'),
            pointer=Path(r"C:\p\data-root.txt"),
            remove_data=True,
        )


def test_schedule_uninstall_hands_a_detached_batch_to_cmd(tmp_path: Path) -> None:
    """交接的动作是"起一个 cmd 跑脚本"，不是自己删文件——函数里不能有删除调用。"""
    calls: list[dict[str, Any]] = []

    def fake_popen(args: list[str], **kwargs: Any) -> Any:
        calls.append({"args": args, **kwargs})
        return object()

    script = app_maintenance.schedule_uninstall(
        uninstaller=tmp_path / "unins000.exe",
        data_root=tmp_path / "chaiai-data",
        pointer=tmp_path / "data-root.txt",
        remove_data=True,
        pid=777,
        popen=fake_popen,
    )

    assert script.is_file()
    assert calls[0]["args"][-1] == str(script)
    assert calls[0]["args"][0].lower().endswith(("cmd.exe", "cmd"))
    # 不建控制台窗口（2026-10-27）：`DETACHED_PROCESS` 会让 cmd 自己新建一个黑框，
    # 用户点卸载先看到它，像出了故障。
    if sys.platform == "win32":
        assert calls[0]["creationflags"] & subprocess.CREATE_NO_WINDOW
    body = script.read_text(encoding="utf-8")
    assert "taskkill /F /PID 777" in body
    script.unlink()


# ---- HTTP 面 ---------------------------------------------------------------


def _config(tmp_path: Path) -> AppConfig:
    config = AppConfig()
    config.memory.short_term.path = str(tmp_path / "checkpoints.sqlite")
    config.model_settings.path = str(tmp_path / "model_settings.sqlite")
    return config


@pytest.fixture()
def client(tmp_path: Path) -> Any:
    runtime = open_runtime(_config(tmp_path), base_dir=tmp_path, instance_lock=False)
    try:
        yield TestClient(create_app(runtime), base_url=LOCAL_BASE_URL)
    finally:
        runtime.close()


def test_app_info_endpoint_describes_this_checkout(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    view = client.get("/api/app/info").json()

    assert view["version"] == __version__
    assert view["install_kind"] == "source"
    assert view["install_dir"] is None
    assert view["uninstall_available"] is False
    assert view["uninstall_note"]
    # 源码形态的数据根是临时目录，可安全删除 —— 是否勾选仍由用户决定。
    assert view["data_root"]
    assert view["data_root_source"] in {"environment", "settings", "config", "default"}


def test_app_info_and_uninstall_require_the_browser_origin(client: TestClient) -> None:
    """这两个接口会**重装/删除本机程序**：只接受本机页面的同源请求。"""
    for headers in ({}, {"origin": "http://evil.example"}, {"origin": "null"}):
        assert client.get("/api/app/info", headers=headers).status_code == 200  # 只读不设限
        assert (
            client.post("/api/app/uninstall", json={"remove_data": False}, headers=headers).status_code
            == 403
        )
        assert client.post("/api/app/update/check", headers=headers).status_code == 403


def test_update_check_reports_unavailable_without_failing_the_request(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """查不了也是 200：设置页照常可用，只有一行说明。"""
    monkeypatch.setattr(
        app_maintenance,
        "check_update",
        lambda **_kwargs: app_maintenance.UpdateCheck(
            status="unavailable",
            current_version=__version__,
            release_url=app_maintenance.RELEASE_PAGE_URL,
            note="连不上 GitHub。",
        ),
    )

    response = client.post(
        "/api/app/update/check", headers={"origin": LOCAL_BASE_URL}
    )

    assert response.status_code == 200
    assert response.json()["status"] == "unavailable"
    assert response.json()["release_url"] == app_maintenance.RELEASE_PAGE_URL


def test_uninstall_is_rejected_for_a_source_checkout(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "frozen", False, raising=False)

    response = client.post(
        "/api/app/uninstall",
        json={"remove_data": False},
        headers={"origin": LOCAL_BASE_URL},
    )

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == UNINSTALL_UNAVAILABLE
    assert error["retryable"] is False
    # 拒绝理由必须是"下一步怎么办"，不能只说"不行"。
    assert json.dumps(error, ensure_ascii=False).count("源码") >= 1
