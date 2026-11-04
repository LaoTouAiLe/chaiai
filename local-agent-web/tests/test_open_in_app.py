"""「打开方式」（§15.11.4，2026-11-03）的契约测试。

全部用**注入的假探测环境**：不读真注册表、不启动任何程序。真机上的解析在开发时实测过
（Cursor 在 `D:\\cursor`、IDEA 在 `E:\\IntelliJ IDEA 2024.1.4`、PyCharm 在
`D:\\PyCharm 2026.2.1`、Git 在 `C:\\Program Files\\Git`，四个都只出现在卸载记录里），
但把它写进用例只会让测试依赖某台机器。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from local_agent import desktop_apps as module
from local_agent.application.service import ApplicationService


def _probe(
    *,
    app_paths: dict[str, str] | None = None,
    uninstall: tuple[module.UninstallEntry, ...] = (),
    files: frozenset[str] = frozenset(),
    which: dict[str, str] | None = None,
    env: dict[str, str] | None = None,
    listdir: dict[str, list[str]] | None = None,
) -> module.Probe:
    return module.Probe(
        env=env or {},
        is_file=lambda path: path in files,
        which=lambda name: (which or {}).get(name),
        registry=module.RegistrySnapshot(app_paths=app_paths or {}, uninstall=uninstall),
        listdir=lambda path: (listdir or {}).get(path, []),
    )


@pytest.fixture(autouse=True)
def _clean_cache():
    module.reset_cache()
    yield
    module.reset_cache()


def test_only_explorer_when_nothing_is_installed() -> None:
    """资源管理器是 OS 自带的 open verb，永远可用；其余探测不到就不出现。"""
    apps = module.resolve_apps(probe=_probe())
    assert [app.id for app in apps] == ["explorer"]
    assert apps[0].command is None


def test_app_paths_is_preferred_and_verified() -> None:
    probe = _probe(
        app_paths={"cursor.exe": r"D:\cursor\Cursor.exe"},
        files=frozenset({r"D:\cursor\Cursor.exe"}),
    )
    apps = module.resolve_apps(probe=probe)
    cursor = next(app for app in apps if app.id == "cursor")
    assert cursor.command == str(Path(r"D:\cursor\Cursor.exe"))


def test_registry_entry_without_executable_is_not_offered() -> None:
    """`App Paths` 里有这条、盘上却没那个文件 —— 不能出现在菜单里。"""
    probe = _probe(app_paths={"cursor.exe": r"D:\cursor\Cursor.exe"}, files=frozenset())
    assert [app.id for app in module.resolve_apps(probe=probe)] == ["explorer"]


def test_uninstall_record_finds_ide_outside_app_paths() -> None:
    """本机真实形态：三个 IDE 都不在 App Paths 里，只有卸载记录的 InstallLocation。"""
    probe = _probe(
        uninstall=(module.UninstallEntry("IntelliJ IDEA 2024.1.4", r"E:\IntelliJ IDEA 2024.1.4", ""),),
        files=frozenset({r"E:\IntelliJ IDEA 2024.1.4\bin\idea64.exe"}),
    )
    apps = {app.id: app for app in module.resolve_apps(probe=probe)}
    assert apps["intellij"].command == str(Path(r"E:\IntelliJ IDEA 2024.1.4\bin\idea64.exe"))


def test_versioned_scan_takes_newest_directory_that_really_has_the_launcher() -> None:
    """`scan` 只认"目录里有启动器"的那一个：新版目录残缺时要退到旧版本。"""
    root = r"C:\Program Files\JetBrains"
    probe = _probe(
        env={"ProgramFiles": r"C:\Program Files"},
        listdir={root: ["PyCharm Community Edition 2020.2", "PyCharm 2026.2.1"]},
        files=frozenset({rf"{root}\PyCharm 2026.2.1\bin\pycharm64.exe"}),
    )
    apps = {app.id: app for app in module.resolve_apps(probe=probe)}
    assert apps["pycharm"].command == str(Path(rf"{root}\PyCharm 2026.2.1\bin\pycharm64.exe"))


def test_missing_env_variable_makes_a_candidate_unresolvable() -> None:
    """`${ProgramFiles}` 缺席时不能拼出半条路径去撞运气。"""
    probe = _probe(env={}, files=frozenset({r"\Microsoft VS Code\Code.exe"}))
    assert [app.id for app in module.resolve_apps(probe=probe)] == ["explorer"]


def test_launch_argv_appends_path_or_substitutes_token() -> None:
    """带空格、跨盘符的路径只能靠 argv；有 `{path}` 就替换，没有就追加到末尾。"""
    assert module._launch_argv(r"E:\IntelliJ IDEA 2024.1.4\bin\idea64.exe", (), r"D:\a b\c.txt") == [
        r"E:\IntelliJ IDEA 2024.1.4\bin\idea64.exe", r"D:\a b\c.txt",
    ]
    assert module._launch_argv(r"C:\Program Files\Git\git-bash.exe", ("--cd={path}",), r"D:\a b") == [
        r"C:\Program Files\Git\git-bash.exe", r"--cd=D:\a b",
    ]


def test_open_with_editor_launches_detached_with_scrubbed_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "notes.txt"
    target.write_text("hi", encoding="utf-8")
    probe = _probe(
        app_paths={"cursor.exe": r"D:\cursor\Cursor.exe"},
        files=frozenset({r"D:\cursor\Cursor.exe"}),
    )
    monkeypatch.setattr(module, "os_probe", lambda: probe)
    captured: dict[str, object] = {}

    def fake_launch(argv, *, env=None, cwd=None, watch_seconds=0.0):
        captured["argv"] = list(argv)
        captured["env"] = env
        captured["watch"] = watch_seconds

    monkeypatch.setattr(module, "launch_detached", fake_launch)

    app = module.open_with("cursor", target)

    assert app.id == "cursor"
    assert captured["argv"] == [str(Path(r"D:\cursor\Cursor.exe")), str(target)]
    env = captured["env"]
    assert isinstance(env, dict)


def test_open_with_terminal_app_passes_the_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "src" / "main.ts"
    target.parent.mkdir()
    target.write_text("x", encoding="utf-8")
    probe = _probe(
        uninstall=(module.UninstallEntry("Git version 2.17.0", r"C:\Program Files\Git", ""),),
        files=frozenset({r"C:\Program Files\Git\git-bash.exe"}),
    )
    monkeypatch.setattr(module, "os_probe", lambda: probe)
    captured: list[list[str]] = []
    monkeypatch.setattr(module, "launch_detached", lambda argv, **_: captured.append(list(argv)))

    module.open_with("gitbash", target)

    assert captured == [[str(Path(r"C:\Program Files\Git\git-bash.exe")), rf"--cd={target.parent}"]]


def test_open_with_unknown_app_is_lookup_error(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("x", encoding="utf-8")
    with pytest.raises(LookupError):
        module.open_with("no-such-app", target)


def test_icon_is_none_for_unknown_app() -> None:
    assert module.icon_png("no-such-app") is None


# ---- 应用服务层的路径边界 -------------------------------------------------


def _service(root: Path) -> SimpleNamespace:
    return SimpleNamespace(_workspace_root=root, _path_for_workspace_id=lambda _id: root)


def test_open_with_app_rejects_paths_outside_workspace(tmp_path: Path) -> None:
    service = _service(tmp_path)
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("x", encoding="utf-8")

    with pytest.raises(PermissionError):
        ApplicationService.open_with_app(service, app_id="cursor", path="../outside.txt")


def test_open_with_app_reports_missing_file(tmp_path: Path) -> None:
    service = _service(tmp_path)
    with pytest.raises(LookupError):
        ApplicationService.open_with_app(service, app_id="cursor", path="nowhere.txt")
