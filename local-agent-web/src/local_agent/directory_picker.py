"""本机原生目录选择器（宿主能力，不是应用服务用例）。

**定位**：这是"宿主进程能不能弹一个系统窗口"的能力，不是业务用例——应用服务只认"按路径
登记工作区"（`select_workspace(path)`），并不关心那条路径是选出来的还是打出来的。所以它
住在包顶层、紧挨 `workspaces.py`（同一种定位：页面与入口共用的本机能力），不在
`application/` 里。

**为什么必须另起一个进程**（两条独立理由，任一条都足够）：

1. Tk 不是线程安全的：Tk 调用必须发生在创建实例的**同一个线程**里，而 FastAPI 的
   `run_in_threadpool` 不保证复用线程，每次新建 Tk 会出问题；
2. `askdirectory()` 是**模态阻塞**的：跑在事件循环线程上会让整个服务失去响应。参考实现
   （DSH）对此的裁决是"对话框打开期间 web 宿主必须继续服务 RPC"。

**为什么入口在本模块、而不是 `python -c`**：源码运行时由同一个 Python 解释器通过
`-m local_agent.api --pick-directory` 进入；打包成 exe 后 `sys.executable` 是 exe 自身，
改用 `<exe> --pick-directory` **自我 spawn**。两种路径都由 `api/__main__.py` 在 argparse
之前分发。

**实测事实**（2026-09-29，Python 3.13.12 + Tk 8.6.15 + Windows 10.0.19045）：Tk 用的是
现代 `IFileDialog`（子控件含 `DUIViewWndClassName`、面包屑地址栏与搜索框），**不是**老式
`SHBrowseForFolder`；但进程默认 **DPI-unaware**，在非 100% 缩放下对话框会被系统位图拉伸
而模糊——所以弹窗之前主动设置 DPI 感知。

**另一条实测事实**（2026-09-30，用户报"对话框在浏览器下面、以为卡住"）：`IFileDialog` 的
z 序跟着它的宿主窗口走，而子进程没有前台窗口（`CREATE_NO_WINDOW` 起、由后台服务 spawn），
所以宿主窗口的层级就是对话框的层级——宿主必须可见且置顶，见 :func:`_ask_directory`。
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
from collections.abc import Sequence
from typing import Final

from local_agent.security.process_guard import run_process

#: 自我 spawn 开关：`<exe> --pick-directory` 只做一件事——弹一个窗，把结果写到 stdout。
PICK_DIRECTORY_FLAG: Final[str] = "--pick-directory"

#: 父进程等待上限。系统对话框可以无限期开着（所以**不设**请求级超时），这里只做兜底，
#: 免得用户走开之后留下一个永不结束的 HTTP 请求和一个还活着的子进程。
DEFAULT_TIMEOUT_SECONDS: Final[float] = 600.0

#: `DPI_AWARENESS_CONTEXT` 的预定义值（Win10 1607+）。负数即"上下文句柄"，不是枚举。
_DPI_CONTEXTS: Final[tuple[tuple[int, str], ...]] = (
    (-4, "per-monitor-v2"),
    (-3, "per-monitor"),
    (-2, "system"),
)

_DIALOG_TITLE: Final[str] = "选择工作区目录"

#: 子进程不要弹出控制台黑窗（打包成 `--noconsole` 后不需要，开发时很需要）。非 Windows 为 0。
_NO_WINDOW: Final[int] = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class DirectoryPickerError(Exception):
    """目录选择器不可用或结果不可信。**用户取消不是错误**（那返回 ``None``）。"""


def _set_dpi_awareness() -> str:
    """把**当前线程**设成可用的最高 DPI 感知等级，返回实际得到的等级名。

    级联顺序与参考实现一致（per-monitor-v2 → per-monitor → system），逐级检查返回值。
    全都不接受时返回 ``"unaware"`` 并**继续弹窗**：模糊的对话框仍然可用，比打不开好
    ——DPI 只是外观，不该成为失败理由。
    """
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        setter = user32.SetThreadDpiAwarenessContext
    except (AttributeError, OSError):  # 非 Windows，或 Win10 1607 之前
        return "unavailable"
    setter.argtypes = [ctypes.c_void_p]
    setter.restype = ctypes.c_bool
    for context, name in _DPI_CONTEXTS:
        # 位模式而非数值：句柄是 -4 这样的负数，转成无符号指针才是它在线上的样子。
        if setter(ctypes.c_void_p(context & 0xFFFFFFFFFFFFFFFF)):
            return name
    return "unaware"


def _ask_directory() -> str:
    """在**当前进程**弹出原生目录选择器；返回绝对路径，用户取消时返回空串。

    只该由 :func:`main` 在子进程里调用：`tkinter` 在此处才导入（父进程导入本模块时不该加载
    Tk），而且 Tk 的调用必须与创建实例同线程。

    **宿主窗口必须可见且置顶**（实测 2026-09-30）：对话框是宿主窗口的**被拥有窗口**，z 序跟着
    宿主走。子进程没有前台窗口，宿主若被 ``withdraw()`` 成不可见窗口，它就落在 z 序最底，
    对话框跟着沉到浏览器下面——用户看不见窗，以为卡住了。因此宿主改成一个 1×1、无边框、
    移出屏幕外的**置顶**窗口：对话框随之浮在最上层，又不占位、不进任务栏与 Alt-Tab。
    """
    import tkinter
    from tkinter import filedialog

    root = tkinter.Tk()
    root.overrideredirect(True)
    root.geometry("1x1+-100+-100")
    root.attributes("-topmost", True)
    try:
        # `mustexist`：选一个真实存在的目录。新建目录由系统对话框自带的"新建文件夹"承担，
        # 所以这里不自己造一套建目录流程。
        # `parent=root`：让对话框被上面那个置顶宿主拥有，从而落在最上层。
        return filedialog.askdirectory(title=_DIALOG_TITLE, mustexist=True, parent=root) or ""
    finally:
        root.destroy()


def main(argv: Sequence[str] | None = None) -> int:
    """子进程入口：弹窗 → 把结果写到 stdout。返回进程退出码。

    输出协议：**stdout 第一行**是选中的绝对路径，用户取消时是空行；诊断信息一律走 stderr，
    免得污染这一行。父进程按行解析，两边编码都钉成 UTF-8（中文路径不能靠 locale 猜）。

    Args:
        argv: **不含程序名**的参数；省略时取 `sys.argv[1:]`（与 `api.__main__.main` 一致）。
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if PICK_DIRECTORY_FLAG not in args:
        print(f"用法：{PICK_DIRECTORY_FLAG}", file=sys.stderr)
        return 2
    # 中文路径不能靠 locale 猜：父进程固定按 UTF-8 读，这一端就得固定按 UTF-8 写。
    # 被重定向成非文本流（测试夹具、已关闭的管道）时跳过。
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # pragma: no cover - 已被替换或已关闭
            pass

    level = _set_dpi_awareness()
    try:
        chosen = _ask_directory()
    except Exception as exc:  # noqa: BLE001 — 没有桌面会话 / Tk 缺失 / 模板损坏，一律如实上报
        print(f"目录选择器不可用：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"dpi={level}", file=sys.stderr)
    print(chosen, flush=True)
    return 0


def _decode(data: bytes) -> str:
    """按 **UTF-8** 解码子进程输出。

    父进程与子进程都钉成 UTF-8（中文路径不能靠 locale 猜，见 :func:`main`），所以这里不读
    ``locale.getpreferredencoding()``；解不出来的字节用 ``replace`` 如实标出。
    """
    return data.decode("utf-8", errors="replace")


def pick_directory(*, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> str | None:
    """弹出原生目录选择器，返回选中的绝对路径；用户取消返回 ``None``。

    启动独立子进程并读取 stdout，因此不会占用本服务的事件循环。源码模式必须通过 `-m`
    进入应用入口；只有冻结后的 exe 才能直接以 `sys.executable --pick-directory` 自我启动。

    子进程经**统一执行出口**启动（§20.1）：超时按**进程树**终止（此前 ``subprocess.run``
    只杀直接子进程）。四类错误（起不来 / 超时 / 非零退出 / 输出不可解析）的消息与从前**逐字相同**；
    **用户取消不是错误**——那是一条空行，对应 ``None``。

    Raises:
        DirectoryPickerError: 子进程起不来、超时退出、非零退出或输出不可解析。
    """
    command = (
        [sys.executable, PICK_DIRECTORY_FLAG]
        if getattr(sys, "frozen", False)
        else [sys.executable, "-m", "local_agent.api", PICK_DIRECTORY_FLAG]
    )
    try:
        # 走**统一执行出口**（§20.1）：超时按**进程树**终止，不是只杀直接子进程。
        # **不传 registry**：这是页面的一次本机动作，既不属于任何一轮执行、也没有取消令牌，
        # 因此用一次性登记表——取消不适用于它，但同一套树终止与如实记录照旧。
        outcome = run_process(
            command,
            timeout_seconds=timeout_seconds,
            creationflags=_NO_WINDOW,
        )
    except OSError as exc:
        msg = f"无法启动目录选择器子进程：{type(exc).__name__}"
        raise DirectoryPickerError(msg) from exc

    if outcome.timed_out:
        msg = "目录选择器等待超时（窗口可能还开着）：已放弃这次选择。"
        raise DirectoryPickerError(msg)

    stdout_text = _decode(outcome.stdout)
    stderr_text = _decode(outcome.stderr)
    if outcome.exit_code != 0:
        reason = stderr_text.strip().splitlines()
        detail = reason[-1] if reason else f"退出码 {outcome.exit_code}"
        msg = f"目录选择器未能打开：{detail}"
        raise DirectoryPickerError(msg)

    lines = stdout_text.splitlines()
    if not lines:
        msg = "目录选择器没有返回任何结果。"
        raise DirectoryPickerError(msg)
    return lines[0] or None
