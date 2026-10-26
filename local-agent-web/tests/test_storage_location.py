"""数据位置（2026-10-26）：指针文件、优先级，以及"页面改不生效"的那一档。

为什么数据根不能存在设置库里：设置库自己就在数据根下。写进去等于让一个东西定义自己的位置——
改到新目录后新库是空的，那条偏好丢了，启动又回到旧位置。所以它走一个**固定放在平台默认目录**的
指针文件，与 ``CODEX_HOME`` / ``HERMES_HOME`` 同一性质。

这一组用例盯三件事：指针真的改了根、非法目标被拒、环境变量在顶层时页面不假装能改。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from local_agent import app_paths
from local_agent.api.app import create_app
from local_agent.api.container import open_runtime
from local_agent.config import AppConfig


def _config(tmp_path: Path) -> AppConfig:
    """三个库都钉到临时目录：本文件验的是"根"，不是库的默认位置。"""
    config = AppConfig()
    config.memory.short_term.path = str(tmp_path / "checkpoints.sqlite")
    config.model_settings.path = str(tmp_path / "model_settings.sqlite")
    return config


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """指针文件落在平台默认目录：先把它隔离到临时目录，再摘掉 CHAIAI_HOME。"""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-app-data"))
    monkeypatch.delenv("CHAIAI_HOME", raising=False)
    runtime = open_runtime(_config(tmp_path), base_dir=tmp_path, instance_lock=False)
    try:
        yield TestClient(create_app(runtime), base_url="http://127.0.0.1")
    finally:
        runtime.close()


def test_pointer_moves_the_root_and_says_it_needs_a_restart(client: TestClient, tmp_path: Path) -> None:
    initial = client.get("/api/storage").json()
    assert initial["source"] == "default"
    assert initial["restart_required"] is False
    assert Path(initial["data_root"]) == Path(initial["default_root"])

    target = tmp_path / "moved" / "chaiai"
    saved = client.put("/api/storage", json={"data_root": str(target)}).json()

    assert saved["source"] == "settings"
    assert saved["restart_required"] is True
    assert Path(saved["data_root"]) == target
    assert "旧数据不会自动搬" in (saved["note"] or "")
    # 探针文件用完就删：它只是"这个目录能写"的证据，不该留在用户目录里
    assert not (target / ".chaiai-write-probe").exists()
    # 下一次解析（= 重启后）读到的是新位置
    assert app_paths.data_root() == target

    reset = client.delete("/api/storage").json()
    assert reset["source"] == "default"
    assert Path(reset["data_root"]) == Path(initial["default_root"])
    assert app_paths.data_root() == Path(initial["default_root"])


def test_reports_usage_and_free_space(client: TestClient) -> None:
    """占用与所在盘剩余（2026-10-26）。

    C 盘小的用户需要的是"为什么该改位置"的证据，而不只是一个输入框；所以这两个数字
    要和路径一起给出。算不出来时允许为 `None`（统计失败不该让整页打不开）。
    """
    # 本用例配的三个库都钉在 tmp_path（绝对路径），数据根本身是空的；
    # 放一个探针文件，才算真的验证了"统计到根下的文件"而不是恰好返回 0。
    root = app_paths.data_root()
    root.mkdir(parents=True, exist_ok=True)
    (root / "probe.bin").write_bytes(b"x" * 1024)

    view = client.get("/api/storage").json()

    assert view["used_bytes"] is not None and view["used_bytes"] >= 1024
    assert view["disk_free_bytes"] is not None and view["disk_free_bytes"] > 0


@pytest.mark.parametrize("target", ["relative/path", ""])
def test_relative_or_empty_targets_are_rejected(client: TestClient, target: str) -> None:
    """相对路径会让"数据在哪"重新取决于启动目录，正是这个功能要消灭的东西。"""
    response = client.put("/api/storage", json={"data_root": target})

    # 空串在传输层就被 422 挡住；相对路径在服务层被拒——两者的错误码一致，文案不同
    assert response.status_code == 422


def test_environment_variable_wins_and_the_page_does_not_pretend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``CHAIAI_HOME`` 在解析链最前面：这一档页面改不生效，必须明说并拒绝写入。"""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-app-data"))
    monkeypatch.setenv("CHAIAI_HOME", str(tmp_path / "env-root"))
    runtime = open_runtime(_config(tmp_path), base_dir=tmp_path, instance_lock=False)
    try:
        client = TestClient(create_app(runtime), base_url="http://127.0.0.1")
        view = client.get("/api/storage").json()

        assert view["source"] == "environment"
        assert Path(view["data_root"]) == tmp_path / "env-root"
        assert "CHAIAI_HOME" in (view["note"] or "")

        blocked = client.put("/api/storage", json={"data_root": str(tmp_path / "other")})
        assert blocked.status_code == 422
        assert "CHAIAI_HOME" in blocked.text
        # 被拒之后不能留下指针文件：否则下次去掉环境变量就会跳到那个目录
        assert app_paths.read_data_root_pointer() is None
    finally:
        runtime.close()
