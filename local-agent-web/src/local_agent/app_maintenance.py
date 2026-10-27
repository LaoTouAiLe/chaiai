"""应用身份与发行维护：版本、安装形态、检查更新、卸载交接（设置页「关于」）。

**边界**：只回答"这个程序是什么版本、装在哪、能不能卸、远端有没有新版"。
不读业务状态、不进模型上下文、不是工具——设置页是唯一消费者。版本号的真源是
:data:`local_agent.__version__`（`release_contracts.py` 也读它）；`pyproject.toml`
里的同名字段由 `tests/test_app_maintenance.py` 断言一致，避免两份事实漂移。

三件容易搞错的事，先写在这里：

1. **三种形态的"卸载"不是一回事**。安装版有 Inno 卸载器；绿色包没有，卸载＝删目录；
   源码形态根本没有可卸的东西。判断集中在 :func:`app_paths.uninstaller_path`。
2. **卸载时程序必须真的退出**。Windows 不允许删除正在运行的 exe，而 Inno 卸载器
   （`unins000.exe`）就在 ``chaiai.exe`` 同级。所以这里不自己删任何**程序**文件，
   而是交出一个延迟执行的批处理：等本进程退出 → 调卸载器 → 按勾选删数据。
3. **删数据是唯一会碰用户数据的地方**，护栏在 :func:`removal_block_reason`：
   盘符根、用户主目录、过浅路径一律拒绝。宁可让用户手动删，也不能 `rmdir /s /q` 错一个目录。

联网口径：检查更新是**用户显式点按**的应用自身动作，不归 `web_access` 那套"给模型的工具"
开关管辖（那套开关控制模型能不能上网，不是控制应用能不能问版本）。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

from local_agent import __version__, app_paths

logger = logging.getLogger(__name__)

#: 发布仓库：与 `README.md` 的 Releases 链接、`.github/workflows/release.yml` 同一个。
RELEASE_REPO: Final[str] = "LaoTouAiLe/chaiai"
LATEST_RELEASE_API: Final[str] = f"https://api.github.com/repos/{RELEASE_REPO}/releases/latest"
RELEASE_PAGE_URL: Final[str] = f"https://github.com/{RELEASE_REPO}/releases"

#: 检查更新的墙钟上限（秒）。设置页打开时不该为它卡住，超时按"无法检查"如实报告。
UPDATE_TIMEOUT_SECONDS: Final[float] = 6.0

#: 响应体读取上限：版本清单只有几 KB，读超这个数说明对面不是我们以为的接口。
MAX_RESPONSE_BYTES: Final[int] = 256 * 1024

#: 安装形态：安装版（有 Inno 卸载器）/ 绿色包（解压即用）/ 源码。
InstallKind = Literal["installed", "portable", "source"]

#: 检查结果：有新版 / 已是最新 / 查不了（网络、限流、格式变化都归这一类）。
UpdateStatus = Literal["update_available", "up_to_date", "unavailable"]


@dataclass(frozen=True)
class BuildInfo:
    """``BUILD_INFO.txt`` 里**对用户有意义**的几项（构建脚本写入，绿色包与安装包都有）。"""

    built_at: str
    revision: str
    #: 打包机上的未提交改动条数；"未知"表示当时读不到 git。
    dirty: str
    python: str


@dataclass(frozen=True)
class AppInfo:
    """设置页「关于」要显示的全部事实；路径都是绝对路径。"""

    version: str
    install_kind: InstallKind
    install_dir: str | None
    data_root: str
    data_root_source: str
    build: BuildInfo | None
    #: 卸载可用性与不可用原因**成对出现**：按钮置灰时必须能说出为什么
    #: （"没有安装程序"和"这是绿色版"对用户是两种不同的下一步）。
    uninstall_available: bool
    uninstall_note: str | None
    #: 数据目录能不能被安全删除；不能时给出原因，勾选框据此禁用。
    can_remove_data: bool
    remove_data_note: str | None


@dataclass(frozen=True)
class UpdateCheck:
    """一次检查更新的结果。**失败不是异常**：设置页要能显示"查不了"而不是报错。"""

    status: UpdateStatus
    current_version: str
    latest_version: str | None = None
    release_url: str | None = None
    published_at: str | None = None
    note: str | None = None


def install_kind() -> InstallKind:
    """安装形态。判据只有"冻结没有"和"卸载器在不在"两条（不猜路径）。"""
    if not app_paths.is_frozen():
        return "source"
    return "installed" if app_paths.uninstaller_path() is not None else "portable"


def read_build_info(install_dir: Path | None = None) -> BuildInfo | None:
    """读 ``BUILD_INFO.txt``；源码形态或文件缺失返回 ``None``。

    格式是构建脚本写的 ``键：值``（全角冒号）。这里**只认四个键**：
    其余行是给排障看的，进页面只会变成噪声。键名对不上就返回 ``None``——
    宁可页面不显示构建信息，也不要把半截数据当成事实展示。
    """
    if install_dir is None:
        return None
    path = install_dir / "BUILD_INFO.txt"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    values: dict[str, str] = {}
    for line in text.splitlines():
        key, separator, value = line.partition("：")
        if separator:
            values[key.strip()] = value.strip()
    built_at = values.get("构建时间(UTC)")
    revision = values.get("源树 HEAD")
    if built_at is None or revision is None:
        return None
    return BuildInfo(
        built_at=built_at,
        revision=revision,
        dirty=values.get("未提交改动条数", "未知"),
        python=values.get("Python", "未知"),
    )


def removal_block_reason(path: Path) -> str | None:
    """``path`` 能不能被"卸载时删除"；不能时返回原因，能则返回 ``None``。

    为什么这么保守：批处理里是 ``rmdir /s /q``，没有回收站。一个拼错的路径就是用户
    整个盘或整个用户目录。规则只放行"看起来确实是一个应用数据目录"的目标：

    - 必须是**绝对路径**、已存在、是目录；
    - 不能是盘符根（``C:\\``）或只有一段的路径；
    - 不能是用户主目录本身或它的**祖先**（``C:\\Users`` 这种）；
    - 不能在 ``Program Files`` / Windows 目录下（数据不该在那儿，出现就是配置错了）。
    """
    if not path.is_absolute():
        return "数据目录不是绝对路径，拒绝删除"
    try:
        if not path.is_dir():
            return "数据目录不存在或不是目录，没有可删的内容"
    except OSError:
        return "读不到数据目录，拒绝删除"
    parts = path.parts
    if len(parts) <= 1:
        return "数据目录过浅（盘符根或单层目录），拒绝删除"
    home = Path.home()
    try:
        resolved = path.resolve()
        home_resolved = home.resolve()
    except OSError:
        return "解析不到数据目录的真实路径，拒绝删除"
    if resolved == home_resolved or home_resolved.is_relative_to(resolved):
        return "数据目录是用户主目录或它的上级，拒绝删除"
    lowered = str(resolved).lower()
    for blocked in ("\\program files", "\\program files (x86)", "\\windows\\"):
        if blocked in lowered:
            return "数据目录在系统目录下，拒绝删除"
    return None


def uninstall_note(kind: InstallKind) -> str | None:
    """卸载不可用时给用户的一句话；安装版返回 ``None``。

    文案只在这里写一份：`POST /api/app/uninstall` 的拒绝理由也用它，
    否则"按钮为什么是灰的"和"点了为什么被拒"会慢慢说成两件事。
    """
    if kind == "portable":
        return "这是绿色版（解压即用），没有卸载程序：关闭程序后删除解压出来的文件夹即可。"
    if kind == "source":
        return "当前是源码运行形态，没有可卸载的安装。"
    return None


def app_info(
    *, data_root: Path, data_root_source: str, version: str | None = None
) -> AppInfo:
    """汇总「关于」页的事实。数据根由调用方传入（它的解析属于应用状态，不在这里重复一套）。"""
    kind = install_kind()
    base = app_paths.install_dir()
    uninstaller = app_paths.uninstaller_path()
    note = None if uninstaller is not None else uninstall_note(kind)
    block = removal_block_reason(data_root)
    return AppInfo(
        version=version or __version__,
        install_kind=kind,
        install_dir=str(base) if base is not None else None,
        data_root=str(data_root),
        data_root_source=data_root_source,
        build=read_build_info(base),
        uninstall_available=uninstaller is not None,
        uninstall_note=note,
        can_remove_data=block is None,
        remove_data_note=block,
    )


def _version_tuple(text: str) -> tuple[int, int, int] | None:
    """把 ``v0.2.0`` / ``0.2.0-rc1`` 压成可比较的三元组；解析不了返回 ``None``。"""
    core = text.strip().lstrip("vV").split("-", 1)[0].split("+", 1)[0]
    parts = core.split(".")
    if len(parts) < 2 or len(parts) > 3:
        return None
    numbers: list[int] = []
    for part in parts:
        if not part.isdigit():
            return None
        numbers.append(int(part))
    while len(numbers) < 3:
        numbers.append(0)
    return (numbers[0], numbers[1], numbers[2])


def _fetch_latest_release(url: str, timeout: float) -> Mapping[str, Any]:
    """取 GitHub 的 latest release。失败抛异常，由 :func:`check_update` 兜住转成 note。"""
    request = urllib.request.Request(
        url,
        headers={
            # GitHub 的 API 没有 User-Agent 会直接 403；带上版本也便于对面看出是谁在问。
            "User-Agent": f"chaiai/{__version__}",
            "Accept": "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read(MAX_RESPONSE_BYTES)
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("版本清单不是对象")
    return payload


def check_update(
    *,
    current: str | None = None,
    timeout: float = UPDATE_TIMEOUT_SECONDS,
    url: str = LATEST_RELEASE_API,
    fetch: Callable[[str, float], Mapping[str, Any]] = _fetch_latest_release,
) -> UpdateCheck:
    """查远端最新版本并与当前版本比较。

    **永不抛异常**：网络不通、被限流、返回格式变了，一律落到 ``status="unavailable"``
    并给一句用户能照做的 note。设置页是只读提醒，不该因为查不了而变成一个错误页。
    """
    version = current or __version__

    def unavailable(note: str, page: str | None = None) -> UpdateCheck:
        # 查不了的时候**总是**给出一个发布页：那是用户此刻唯一能自己走完的路
        # （"连不上"不等于"没新版"，页面必须留这个出口）。
        return UpdateCheck(
            status="unavailable",
            current_version=version,
            release_url=page or RELEASE_PAGE_URL,
            note=note,
        )

    try:
        payload = fetch(url, timeout)
    except urllib.error.HTTPError as exc:
        # 403 多半是 GitHub 的匿名限流（每小时 60 次）；说清"过会儿再试"比抛原文有用。
        note = "GitHub 拒绝了这次检查（可能是访问太频繁），稍后再试。"
        if exc.code != 403:
            note = f"检查更新失败：GitHub 返回 HTTP {exc.code}。"
        return unavailable(note)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        logger.info("检查更新失败：%s", exc)
        return unavailable("连不上 GitHub，检查更新失败。可以稍后再试，或直接打开发布页下载。")
    except (TypeError, ValueError, UnicodeDecodeError) as exc:
        logger.info("检查更新响应无法解析：%s", exc)
        return unavailable("版本清单的格式和预期不一致，检查更新失败。可以打开发布页手动查看。")

    tag = payload.get("tag_name")
    if not isinstance(tag, str) or not tag.strip():
        return unavailable("版本清单里没有版本号，检查更新失败。可以打开发布页手动查看。")
    published = payload.get("published_at")
    page = payload.get("html_url")
    latest = tag.strip().lstrip("vV")
    newest = _version_tuple(latest)
    mine = _version_tuple(version)
    if newest is None or mine is None:
        return unavailable(
            f"读到的版本号（{latest}）无法比较，请到发布页确认。",
            page if isinstance(page, str) else None,
        )
    available = newest > mine
    return UpdateCheck(
        status="update_available" if available else "up_to_date",
        current_version=version,
        latest_version=latest,
        release_url=str(page) if isinstance(page, str) else RELEASE_PAGE_URL,
        published_at=str(published) if isinstance(published, str) else None,
        note=None if available else "已经是最新版本。",
    )


def _bat_quote(value: str) -> str:
    """把路径放进批处理的 ``set "VAR=..."`` 里。

    只处理 ``%``（cmd 会展开变量）。含 ``"`` 或换行的路径**直接拒绝**——那种路径本来就
    不该被写进批处理，硬转义只会在别处出问题。
    """
    if '"' in value or "\n" in value or "\r" in value:
        raise ValueError("路径里含有不能写进批处理的字符")
    return value.replace("%", "%%")


def _uninstall_script(*, pid: int, uninstaller: Path, data_root: Path,
                      pointer: Path, remove_data: bool) -> str:
    """生成卸载批处理。

    顺序是硬要求：**本进程必须先退出**，否则卸载器删不掉 ``chaiai.exe``（Windows 不允许
    覆盖运行中的文件）。所以脚本第一件事是等两秒再 ``taskkill`` 自己；数据删除放在
    卸载器**之后**——卸载失败时数据还在，用户可以重来。
    """
    lines = [
        "@echo off",
        "setlocal",
        f'set "UNINS={_bat_quote(str(uninstaller))}"',
        f'set "DATA={_bat_quote(str(data_root))}"',
        f'set "POINTER={_bat_quote(str(pointer))}"',
        f'set "REMOVE={"1" if remove_data else "0"}"',
        # 给接口留出把 202 写回前端的时间；ping 比 timeout 可靠（后者在没有控制台时会报错）。
        "ping -n 3 127.0.0.1 >nul",
        f"taskkill /F /PID {pid} >nul 2>&1",
        "ping -n 2 127.0.0.1 >nul",
        # /SILENT 而不是 /VERYSILENT：留进度窗口，出错时用户看得见。
        'start "" /wait "%UNINS%" /SILENT /SUPPRESSMSGBOXES /NORESTART',
        'if "%REMOVE%"=="1" (',
        '  rmdir /s /q "%DATA%" >nul 2>&1',
        # 指针文件在平台默认目录里，不随数据根移动：只删数据不删它，下次启动还会指回旧路径。
        '  del /q "%POINTER%" >nul 2>&1',
        ")",
        'del "%~f0" >nul 2>&1',
        "",
    ]
    return "\r\n".join(lines)


def schedule_uninstall(
    *,
    uninstaller: Path,
    data_root: Path,
    pointer: Path,
    remove_data: bool,
    pid: int | None = None,
    popen: Callable[..., Any] = subprocess.Popen,
) -> Path:
    """写好批处理并**分离启动**它，返回脚本路径（测试与日志用）。

    只做"交出去"这一步：本进程不在函数里退出——调用方还要把 202 响应写回浏览器，
    退出由脚本里的 ``taskkill`` 完成。批处理落在系统临时目录，执行完自己删自己。

    Raises:
        ValueError: 路径含不可写进批处理的字符（拒绝而不是猜测怎么转义）。
        RuntimeError: 不在 Windows 上——这套交接只对 Inno 安装版成立。
    """
    if sys.platform != "win32":
        raise RuntimeError("卸载交接只在 Windows 安装版上有意义")
    process_id = pid if pid is not None else os.getpid()
    script = Path(tempfile.gettempdir()) / f"chaiai-uninstall-{process_id}.bat"
    script.write_text(
        _uninstall_script(
            pid=process_id,
            uninstaller=uninstaller,
            data_root=data_root,
            pointer=pointer,
            remove_data=remove_data,
        ),
        encoding="utf-8",
        errors="strict",
    )
    creationflags = 0
    if sys.platform == "win32":
        # `CREATE_NO_WINDOW` 而不是 `DETACHED_PROCESS`（2026-10-27 修）：后者只是"不与父进程
        # 共享控制台"，`cmd.exe` 自己**仍会新建一个控制台窗口**——用户点卸载先看到一个黑框，
        # 看起来像出了故障。两者互斥，取"不建窗口"。
        # 与父进程解耦由 `CREATE_NEW_PROCESS_GROUP` 保证：父进程被 `taskkill` 时脚本照常跑完。
        creationflags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    popen(
        [os.environ.get("COMSPEC", "cmd.exe"), "/c", str(script)],
        creationflags=creationflags,
        close_fds=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=str(script.parent),
    )
    return script
