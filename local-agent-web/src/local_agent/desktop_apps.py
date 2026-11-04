"""本机「打开方式」：把工作区里的文件交给用户装好的编辑器 / 终端 / 资源管理器。

**定位**与 :mod:`local_agent.directory_picker` / :mod:`local_agent.browser_open` 相同：这是
"宿主进程能不能做某件事"的能力，不是应用服务用例。应用服务只负责把路径校验到工作区内，
这里负责"用哪个程序打开、那个程序真的存在吗"。

**为什么是一张编译期目录，而不是枚举系统里所有应用**（照 dsh 的裁决）：操作系统能告诉你
"哪些程序注册了关联"，但给不出"哪个程序接受一个目录/文件参数"。所以目录固定，每条应用声明
一串**按序尝试的定位器**，只有定位到**本机真实存在的启动器**才算可用——绝不拿一条安装记录
当证据。探测结果每进程只算一次；启动器消失时只重解析那一条并重试一次。

**为什么必须这样探测**（2026-11-03 实机）：本机的 Cursor（`D:\\cursor`）、IntelliJ IDEA
（`E:\\IntelliJ IDEA 2024.1.4`）、PyCharm（`D:\\PyCharm 2026.2.1`）**都不在 `App Paths` 里**，
只有卸载记录的 `InstallLocation` 指向它们；安装盘符跨 D:/E: 且目录名带空格，所以启动一律走
argv、`shell=False`，绝不拼命令行字符串。

**与子进程出口的关系**（§20.1）：启动经 :func:`local_agent.security.process_guard.launch_detached`
——同一个模块出口，但**不登记、不挂 Job、不套沙箱**：用户关掉服务不该顺手杀掉他打开的 IDE。
"""

from __future__ import annotations

import base64
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal

if os.name == "nt":  # pragma: no cover - 非 Windows 上根本没有这个模块
    import winreg
else:  # pragma: no cover
    winreg = None  # type: ignore[assignment]

from local_agent.security.process_guard import launch_detached, run_process
from local_agent.security.shell_guard import build_environment, resolve_terminal_shell

_LOGGER = logging.getLogger(__name__)

#: 启动参数里的目标占位符；没有它就**追加到 argv 末尾**（`idea64.exe <文件>` 就是这么来的）。
PATH_TOKEN: Final[str] = "{path}"

#: GUI 程序可继承的环境变量：基础集合（PATH / APPDATA / LOCALAPPDATA / USERPROFILE 等）
#: 之外再补几个 Electron 与 JetBrains 会读的**非密钥**名字。
ENV_KEYS: Final[tuple[str, ...]] = (
    "ProgramData",
    "ProgramFiles",
    "ProgramFiles(x86)",
    "CommonProgramFiles",
    "PROCESSOR_ARCHITECTURE",
    "USERNAME",
    "USERDOMAIN",
    "SESSIONNAME",
)

#: 早期失败看护窗口：GUI 程序在 1 秒内自己退出，基本等于"起不来"。
LAUNCH_WATCH_SECONDS: Final[float] = 1.0

#: 抽图标的子进程超时。PowerShell 冷启动 + System.Drawing 大约 0.5~1.5 秒。
ICON_TIMEOUT_SECONDS: Final[float] = 20.0

#: 图标内存缓存上限（每个应用一份 PNG，几 KB）。
ICON_CACHE_MAX: Final[int] = 32

OpenTarget = Literal["file", "dir"]


# ---- 定位器：一条应用怎么在本机被找到 ------------------------------------


@dataclass(frozen=True)
class AppPaths:
    """Windows `App Paths` 注册表（HKLM/HKCU 的 64 位与 32 位视图）。"""

    exe: str
    args: tuple[str, ...] = ()


@dataclass(frozen=True)
class InstallRecord:
    """Windows 卸载记录：用 `InstallLocation` 拼出启动器，再验证它真的存在。"""

    display_prefix: str
    relative_launcher: str | None = None
    args: tuple[str, ...] = ()


@dataclass(frozen=True)
class KnownFile:
    """一组候选路径，取第一个存在的（可含 `${LOCALAPPDATA}` 这类变量）。"""

    candidates: tuple[str, ...]
    args: tuple[str, ...] = ()


@dataclass(frozen=True)
class VersionedScan:
    """在某个根下按"产品名前缀"找版本化安装目录，取最新且**真有启动器**的那个。"""

    root: str
    name_prefix: str
    relative_launcher: str
    args: tuple[str, ...] = ()


@dataclass(frozen=True)
class PathCli:
    """PATH 上的命令（`where` 语义，不经过 shell）。"""

    name: str
    args: tuple[str, ...] = ()


@dataclass(frozen=True)
class ShellOpen:
    """交给操作系统自己的 open verb（资源管理器 / Finder / xdg-open）。"""


Locator = AppPaths | InstallRecord | KnownFile | VersionedScan | PathCli | ShellOpen


@dataclass(frozen=True)
class AppSpec:
    id: str
    label: str
    locators: tuple[Locator, ...]
    #: 菜单顺序，小的在前。
    order: int
    #: 交给程序的是文件本身还是它所在目录（终端类要目录）。
    opens: OpenTarget = "file"


def _jetbrains(
    app_id: str, label: str, product: str, cli_name: str, exe: str, order: int,
) -> AppSpec:
    """JetBrains 系：先扫 `%ProgramFiles%\\JetBrains` 的最新版本目录，再退到卸载记录。"""
    return AppSpec(
        app_id, label,
        (
            VersionedScan("${ProgramFiles}/JetBrains", product, f"bin/{exe}"),
            InstallRecord(product, f"bin/{exe}"),
            PathCli(cli_name),
        ),
        order,
    )


#: 目录**按菜单顺序**排列；`explorer` 永远排第一（它就是"默认打开方式"）。
OPEN_IN_APP_CATALOG: Final[tuple[AppSpec, ...]] = (
    AppSpec("explorer", "文件资源管理器", (ShellOpen(),), 0, opens="dir"),
    AppSpec(
        "cursor", "Cursor",
        (
            AppPaths("Cursor.exe"),
            InstallRecord("Cursor", "Cursor.exe"),
            KnownFile(("${LOCALAPPDATA}/Programs/cursor/Cursor.exe",)),
            PathCli("cursor"),
        ),
        10,
    ),
    AppSpec(
        "vscode", "VS Code",
        (
            AppPaths("Code.exe"),
            InstallRecord("Microsoft Visual Studio Code", "Code.exe"),
            KnownFile((
                "${LOCALAPPDATA}/Programs/Microsoft VS Code/Code.exe",
                "${ProgramFiles}/Microsoft VS Code/Code.exe",
            )),
            PathCli("code"),
        ),
        20,
    ),
    _jetbrains("intellij", "IntelliJ IDEA", "IntelliJ IDEA", "idea", "idea64.exe", 30),
    _jetbrains("pycharm", "PyCharm", "PyCharm", "pycharm", "pycharm64.exe", 40),
    AppSpec(
        "gitbash", "Git Bash",
        (
            InstallRecord("Git version", "git-bash.exe", ("--cd={path}",)),
            KnownFile(("${ProgramFiles}/Git/git-bash.exe",), ("--cd={path}",)),
        ),
        50,
        opens="dir",
    ),
    AppSpec(
        "windowsterminal", "Windows Terminal",
        (
            AppPaths("wt.exe", ("-d", PATH_TOKEN)),
            PathCli("wt", ("-d", PATH_TOKEN)),
        ),
        60,
        opens="dir",
    ),
)


# ---- 探测：把定位器落到本机 ----------------------------------------------


@dataclass(frozen=True)
class UninstallEntry:
    display_name: str
    install_location: str
    display_icon: str


@dataclass(frozen=True)
class RegistrySnapshot:
    """两处注册表的一次性快照（`App Paths` 与卸载记录）。"""

    app_paths: Mapping[str, str] = field(default_factory=dict)
    uninstall: tuple[UninstallEntry, ...] = ()


def _registry_roots() -> tuple[tuple[int, int], ...]:
    """注册表视图：64 位与 32 位各读一次（32 位安装的程序只出现在 WOW6432Node 里）。"""
    if winreg is None:  # pragma: no cover - 非 Windows
        return ()
    return (
        (winreg.HKEY_CURRENT_USER, winreg.KEY_WOW64_64KEY),
        (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY),
        (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY),
    )


def read_registry() -> RegistrySnapshot:
    """读 `App Paths` 与卸载记录。读不到（非 Windows、权限不足）就返回空快照。

    一律吞掉异常：探测失败只意味着"菜单里少一项"，不该让整个请求 500。
    """
    roots = _registry_roots()
    if not roots:
        return RegistrySnapshot()
    app_paths: dict[str, str] = {}
    uninstall: list[UninstallEntry] = []
    for hive, view in roots:
        _read_app_paths(hive, view, app_paths)
        _read_uninstall(hive, view, uninstall)
    return RegistrySnapshot(app_paths=app_paths, uninstall=tuple(uninstall))


def _read_app_paths(hive: int, view: int, sink: dict[str, str]) -> None:
    path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"
    try:
        with winreg.OpenKey(hive, path, 0, winreg.KEY_READ | view) as root:
            index = 0
            while True:
                try:
                    name = winreg.EnumKey(root, index)
                except OSError:
                    break
                index += 1
                try:
                    with winreg.OpenKey(root, name) as entry:
                        value = winreg.QueryValueEx(entry, "")[0]
                except OSError:
                    continue
                if isinstance(value, str) and value.strip():
                    sink.setdefault(name.casefold(), value.strip())
    except OSError:
        return


def _read_uninstall(hive: int, view: int, sink: list[UninstallEntry]) -> None:
    path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
    try:
        with winreg.OpenKey(hive, path, 0, winreg.KEY_READ | view) as root:
            index = 0
            while True:
                try:
                    name = winreg.EnumKey(root, index)
                except OSError:
                    break
                index += 1
                try:
                    with winreg.OpenKey(root, name) as entry:
                        display = winreg.QueryValueEx(entry, "DisplayName")[0]
                        if not isinstance(display, str):
                            continue
                        # 值必须在 with 块内读：句柄一关，`_optional_value` 只会拿到空串。
                        sink.append(UninstallEntry(
                            display_name=display,
                            install_location=_optional_value(entry, "InstallLocation"),
                            display_icon=_optional_value(entry, "DisplayIcon"),
                        ))
                except OSError:
                    continue
    except OSError:
        return


def _optional_value(key: Any, name: str) -> str:
    try:
        value = winreg.QueryValueEx(key, name)[0]
    except OSError:
        return ""
    return value.strip() if isinstance(value, str) else ""


@dataclass(frozen=True)
class Probe:
    """解析所需的一切外部事实——测试据此注入假环境，不碰真机。"""

    env: Mapping[str, str]
    is_file: Callable[[str], bool]
    which: Callable[[str], str | None]
    registry: RegistrySnapshot
    #: 列目录（只给版本化扫描用）；给不了就返回空列表，扫描直接落空。
    listdir: Callable[[str], list[str]] = lambda _path: []


def os_probe() -> Probe:
    """真机探测：环境变量、文件存在性、PATH 解析、注册表快照。"""
    def listdir(path: str) -> list[str]:
        try:
            return os.listdir(path)
        except OSError:
            return []

    return Probe(
        env=os.environ,
        is_file=lambda path: Path(path).is_file(),
        which=shutil.which,
        registry=read_registry(),
        listdir=listdir,
    )


@dataclass(frozen=True)
class ResolvedApp:
    """一条**已验证**的应用：要么有真实启动器，要么是 OS 的 open verb。"""

    id: str
    label: str
    #: `None` = 用系统 open verb 打开目录（资源管理器一类）。
    command: str | None
    args: tuple[str, ...]
    #: 抽图标用的可执行文件；取不到就是 `None`（前端退化成首字母块）。
    icon_source: str | None
    opens: OpenTarget


def resolve_apps(
    *, probe: Probe | None = None, specs: Sequence[AppSpec] = OPEN_IN_APP_CATALOG,
) -> tuple[ResolvedApp, ...]:
    """把目录解析成"本机真的能用"的那几项，按菜单顺序返回。"""
    active = probe if probe is not None else os_probe()
    resolved: list[ResolvedApp] = []
    for spec in sorted(specs, key=lambda item: item.order):
        found = _resolve_one(spec, active)
        if found is not None:
            resolved.append(found)
    return tuple(resolved)


def _resolve_one(spec: AppSpec, probe: Probe) -> ResolvedApp | None:
    for locator in spec.locators:
        hit = _resolve_locator(locator, probe)
        if hit is None:
            continue
        command, args = hit
        if command is not None:
            # 注册表和模板里两种分隔符都可能出现，统一成本机形式再交给启动器与抽图标。
            command = os.path.normpath(command)
        icon = command if command is not None else _system_icon_source()
        return ResolvedApp(
            id=spec.id, label=spec.label, command=command, args=args,
            icon_source=icon, opens=spec.opens,
        )
    return None


def _resolve_locator(locator: Locator, probe: Probe) -> tuple[str | None, tuple[str, ...]] | None:
    if isinstance(locator, ShellOpen):
        return None, ()
    if isinstance(locator, AppPaths):
        target = probe.registry.app_paths.get(locator.exe.casefold())
        if target is not None and probe.is_file(target):
            return target, locator.args
        return None
    if isinstance(locator, InstallRecord):
        for record in probe.registry.uninstall:
            if not record.display_name.startswith(locator.display_prefix):
                continue
            target = _record_launcher(record, locator.relative_launcher, probe)
            if target is not None:
                return target, locator.args
        return None
    if isinstance(locator, KnownFile):
        for candidate in locator.candidates:
            expanded = _expand(candidate, probe.env)
            if expanded is not None and probe.is_file(expanded):
                return expanded, locator.args
        return None
    if isinstance(locator, VersionedScan):
        expanded = _expand(locator.root, probe.env)
        if expanded is None:
            return None
        root = os.path.normpath(expanded)
        for directory in _versioned_dirs(root, locator.name_prefix, probe.listdir):
            target = os.path.join(directory, *locator.relative_launcher.split("/"))
            if probe.is_file(target):
                return target, locator.args
        return None
    found = probe.which(locator.name)
    if found is None:
        return None
    return found, locator.args


def _record_launcher(record: UninstallEntry, relative: str | None, probe: Probe) -> str | None:
    if record.install_location:
        base = record.install_location.strip().strip('"')
        if relative is None:
            return base if probe.is_file(base) else None
        target = os.path.join(base, *relative.split("/"))
        if probe.is_file(target):
            return target
    # 没有 `InstallLocation` 时退到 `DisplayIcon`（形如 `"C:\\x\\y.exe",0`）。
    icon = record.display_icon.strip().strip('"')
    if icon.casefold().endswith((".exe", ".com")) and probe.is_file(icon):
        return icon
    return None


def _versioned_dirs(root: str, prefix: str,
                    listdir: Callable[[str], list[str]]) -> list[str]:
    """根下名字以 `prefix` 开头的目录，按版本号**从新到旧**。"""
    entries = [entry for entry in listdir(root) if entry.startswith(prefix)]
    if not entries:
        return []

    def sort_key(name: str) -> tuple[int, ...]:
        # 版本号取名字里所有数字段（"2026.2.1" → (2026, 2, 1)）。
        # 不能按整词 `isdigit()` 判："2026.2.1" 带点，永远判不成数字。
        numbers = [int(piece) for piece in re.findall(r"\d+", name)]
        return tuple(numbers) if numbers else (0,)

    entries.sort(key=sort_key, reverse=True)
    return [os.path.join(root, entry) for entry in entries]


def _expand(template: str, env: Mapping[str, str]) -> str | None:
    """展开 `${VAR}`；有一个变量缺席就返回 `None`（不要拼出半条路径）。"""
    text = template
    while "${" in text:
        start = text.index("${")
        end = text.find("}", start)
        if end == -1:
            break
        name = text[start + 2:end]
        value = env.get(name)
        if value is None:
            return None
        text = text[:start] + value + text[end + 1:]
    return text


def _system_icon_source() -> str:
    system_root = os.environ.get("SystemRoot", "")
    return os.path.join(system_root, "explorer.exe") if system_root else ""


# ---- 对外：解析缓存、启动、图标 ------------------------------------------

_STATE_LOCK = threading.Lock()
_RESOLVED: tuple[ResolvedApp, ...] | None = None
_ICONS: dict[str, bytes] = {}


def available_apps(*, refresh: bool = False) -> tuple[ResolvedApp, ...]:
    """本机可用的「打开方式」（每进程解析一次；`refresh=True` 强制重解析）。"""
    global _RESOLVED
    with _STATE_LOCK:
        if _RESOLVED is None or refresh:
            _RESOLVED = resolve_apps()
        return _RESOLVED


def find_app(app_id: str) -> ResolvedApp | None:
    for app in available_apps():
        if app.id == app_id:
            return app
    return None


def reset_cache() -> None:
    """测试与"应用刚卸载"用：丢掉解析结果与图标缓存。"""
    global _RESOLVED
    with _STATE_LOCK:
        _RESOLVED = None
        _ICONS.clear()


def open_with(app_id: str, target: Path) -> ResolvedApp:
    """用 `app_id` 打开 `target`（文件类传文件，终端类传它所在目录）。

    Raises:
        LookupError: 这个 id 不在目录里，或者本机解析不出它。
        PermissionError: 目标不是普通文件/目录。
        OSError: 启动器存在但启动失败。
    """
    app = find_app(app_id)
    if app is None:
        raise LookupError("这个应用在这台机器上不可用。")
    path = target if app.opens == "file" else target.parent
    if not path.exists():
        raise PermissionError("要打开的位置已经不存在了。")
    if app.command is None:
        _shell_open(path)
        return app
    argv = _launch_argv(app.command, app.args, str(path))
    try:
        launch_detached(argv, env=build_environment(ENV_KEYS), watch_seconds=LAUNCH_WATCH_SECONDS)
    except FileNotFoundError:
        # 解析之后被卸载了：只重解析一次，再不行就如实报错。
        app = _refresh_one(app_id)
        if app is None or app.command is None:
            raise LookupError("这个应用已经不在本机了。") from None
        launch_detached(
            _launch_argv(app.command, app.args, str(path)),
            env=build_environment(ENV_KEYS), watch_seconds=LAUNCH_WATCH_SECONDS,
        )
    return app


def _refresh_one(app_id: str) -> ResolvedApp | None:
    available_apps(refresh=True)
    return find_app(app_id)


def _launch_argv(command: str, args: tuple[str, ...], path: str) -> list[str]:
    """占位符替换；没有任何参数带占位符时把目标**追加到末尾**。"""
    if any(PATH_TOKEN in arg for arg in args):
        return [command, *(arg.replace(PATH_TOKEN, path) for arg in args)]
    return [command, *args, path]


def _shell_open(path: Path) -> None:
    """交给 OS 的 open verb：Windows 上是资源管理器，其他平台是 xdg-open / open。"""
    if os.name == "nt":
        # `os.startfile` 走 shell 关联，不经过 shell 字符串，也不产生我们要管的子进程。
        os.startfile(str(path))
        return
    opener = shutil.which("open") or shutil.which("xdg-open")
    if opener is None:
        raise LookupError("这台机器没有可用的文件管理器。")
    outcome = run_process([opener, str(path)], timeout_seconds=10.0)
    if outcome.exit_code not in (0, None):
        raise OSError("文件管理器没有接受这个路径。")


def icon_png(app_id: str) -> bytes | None:
    """取某个应用的 32px 图标（PNG）。取不到返回 `None`——调用方退化成首字母块。

    Windows 上借 PowerShell 的 `ExtractAssociatedIcon`（.NET 标准面能给出的最大尺寸），
    结果按应用 id 缓存在内存里：图标是**静态**的，每页只该抽一次。
    """
    app = find_app(app_id)
    if app is None or not app.icon_source:
        return None
    with _STATE_LOCK:
        cached = _ICONS.get(app_id)
        if cached is not None:
            return cached
    data = _extract_icon(app.icon_source)
    if data is None:
        return None
    with _STATE_LOCK:
        if len(_ICONS) >= ICON_CACHE_MAX:
            _ICONS.clear()
        _ICONS[app_id] = data
    return data


def _extract_icon(source: str) -> bytes | None:
    if os.name != "nt":
        return None
    shell, is_powershell = resolve_terminal_shell("powershell")
    if not is_powershell:
        # 连 PowerShell 都没有：不为了图标去装东西，直接让前端用占位块。
        return None
    script = (
        "$ErrorActionPreference='Stop';"
        "Add-Type -AssemblyName System.Drawing;"
        "$p=$env:CHAIAI_ICON_TARGET;"
        "if(-not (Test-Path -LiteralPath $p)){exit 3};"
        "$i=[System.Drawing.Icon]::ExtractAssociatedIcon($p);"
        "if($i -eq $null){exit 4};"
        "$b=$i.ToBitmap();"
        "$m=New-Object System.IO.MemoryStream;"
        "$b.Save($m,[System.Drawing.Imaging.ImageFormat]::Png);"
        "$m.Close();"
        "[Convert]::ToBase64String($m.ToArray())"
    )
    env = build_environment(ENV_KEYS)
    env["CHAIAI_ICON_TARGET"] = source
    try:
        outcome = run_process(
            [shell, "-NoProfile", "-NonInteractive", "-Command", script],
            env=env,
            timeout_seconds=ICON_TIMEOUT_SECONDS,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError:
        _LOGGER.warning("抽图标失败：%s", source, exc_info=True)
        return None
    if outcome.exit_code != 0 or not outcome.stdout.strip():
        _LOGGER.info("抽图标没有结果（退出码 %s）：%s", outcome.exit_code, source)
        return None
    try:
        return base64.b64decode(outcome.stdout.strip(), validate=True)
    except (ValueError, TypeError):
        _LOGGER.info("抽图标的输出不是合法 base64：%s", source)
        return None


def platform_supported() -> bool:
    """Windows 才有完整的目录与启动语义；其他平台只保留 open verb 与 PATH 命令。"""
    return sys.platform.startswith(("win32", "darwin", "linux"))
