"""冻结（打包）形态的路径口径：**只读程序资源**与**可写数据目录**分开（T40）。

职责边界：只回答"资源在哪、用户数据该写哪"，**不读配置、不建目录、不碰业务**。消费者是入口
``python -m local_agent.api``：它把这里的结果用作 ``--config`` / ``--env`` 的默认值，并把资源
目录里的前端产物显式交给 ``open_runtime(static_dir=...)``。

**口径（2026-10-08 由项目负责人拍板）**

```text
只读程序资源   PyInstaller onedir 的 _internal/（sys._MEIPASS）
                 └── frontend/dist/    前端构建产物
可写数据目录   %LOCALAPPDATA%/chaiai
                 ├── config.yaml       （可选；不存在就进初始设置模式，本程序不写 YAML）
                 ├── .env              （可选；旧的"环境变量取密钥"路径）
                 └── data/             SQLite / Chroma，由既有配置默认值派生
```

**为什么数据不放在解压目录里**：计划 §T40 要求"覆盖升级后保留设置、凭据引用、Checkpoint 和
会话"。程序目录恰恰是会被整包替换的东西（PyInstaller onedir 的一个目录），数据放进去就会跟着
被换掉。另外解压位置由用户决定，可能落在 ``Program Files`` 或同步盘上，写权限不保证。

**为什么单独一个模块**：T40 的验收包含"检查分发目录及前端资源中没有密钥、真实数据或开发机
路径"，而"哪些路径算程序、哪些算用户数据"必须先成为可测试的函数，构建脚本才能自动核对。本模块
全部是纯函数加环境读取，冻结与否只取决于 :func:`is_frozen` 与 ``LOCALAPPDATA``，测试直接替换
这两个输入即可，**不需要真的打包**。

**源码形态下必须完全沉默**：非冻结时所有函数回到原口径（``config.yaml``、``.env``、静态页按
配置文件所在目录解析），:func:`data_dir` 与 :func:`bundled_static_dir` 返回 ``None`` 表示
"没有第二套路径"——本模块加入前后源码运行行为一致。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Final

from local_agent.config import DEFAULT_CONFIG_PATH, DEFAULT_ENV_PATH

#: 数据目录名：``%LOCALAPPDATA%/<这个名字>``。与产品名一致，卸载时一眼能认出该删哪个目录。
APP_DIR_NAME: Final[str] = "chaiai"

#: 前端产物在包内的相对位置（与 ``web.static_dir`` 默认值 ``frontend/dist`` 同一个含义）。
STATIC_DIR_PARTS: Final[tuple[str, ...]] = ("frontend", "dist")

#: 随包技能目录的名字（onedir 里与 ``chaiai.exe`` 同级）。
SKILLS_DIR_NAME: Final[str] = "skills"


def is_frozen() -> bool:
    """当前进程是否由打包产物启动（PyInstaller 会设置 ``sys.frozen``）。"""
    return bool(getattr(sys, "frozen", False))


def resource_dir() -> Path | None:
    """只读程序资源的根目录；源码形态返回 ``None``。

    用 ``sys._MEIPASS`` 而不是"exe 所在目录"：那是打包器给出的**唯一权威**位置，
    ``--add-data`` 的内容一定在那里，onefile 与 onedir 都对。
    """
    if not is_frozen():
        return None
    base = getattr(sys, "_MEIPASS", None)
    return None if base is None else Path(base)


def data_dir() -> Path | None:
    """可写的用户数据目录；源码形态或取不到 ``LOCALAPPDATA`` 时返回 ``None``。

    取不到 ``LOCALAPPDATA`` 属于异常环境（非 Windows、环境变量被裁剪）：返回 ``None``，
    让调用方回落到原来的相对路径——凭空造一个写不进去的目录只会把失败推迟到更难诊断的地方。
    """
    if not is_frozen():
        return None
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        return None
    return Path(local_app_data) / APP_DIR_NAME


def default_config_path() -> Path:
    """``--config`` 的默认值。

    冻结时**必须**落在数据目录里：``api/__main__.py`` 把"配置文件所在目录"当作相对路径的解析
    基准（``open_runtime(base_dir=...)``），所以这一条同时决定了聊天库、设置库与工作区默认值
    落在哪。原来的默认值 ``config.yaml`` 是相对**当前工作目录**解析的：双击 exe 时通常是程序
    目录，经快捷方式或从别的目录启动就会漂移，漂移后还会静默进入初始设置模式——冻结形态不能
    接受这种不确定性。
    """
    base = data_dir()
    return DEFAULT_CONFIG_PATH if base is None else base / DEFAULT_CONFIG_PATH


def default_env_path() -> Path:
    """``--env`` 的默认值：与 :func:`default_config_path` 同一目录（旧密钥路径继续可用）。"""
    base = data_dir()
    return DEFAULT_ENV_PATH if base is None else base / DEFAULT_ENV_PATH


def bundled_binary(name: str) -> Path | None:
    """随包的**外部可执行文件**（如 ``rg.exe``）；源码形态或文件不存在时返回 ``None``。

    为什么要有这一条：``shutil.which`` 只看 PATH，朋友的机器上通常没有 ripgrep，
    于是代码检索一直走较慢的 Python 扫描。包里把 ``rg.exe`` 放在 ``_internal/bin/``，
    这里负责把它变成"可见的绝对路径"，调用方仍按"包内优先、PATH 兜底"的顺序解析，
    这样源码形态与装过 rg 的开发机行为不变。
    """
    base = resource_dir()
    if base is None:
        return None
    filename = name if name.lower().endswith(".exe") else f"{name}.exe"
    candidate = base / "bin" / filename
    return candidate if candidate.is_file() else None


def bundled_skills_dir() -> Path | None:
    """随包的技能目录；源码形态或目录不存在时返回 ``None``（2026-10-20）。

    为什么需要它：打包时技能被复制到 **exe 同级**的 ``skills/``，但冻结形态的配置文件在
    ``%LOCALAPPDATA%/chaiai/config.yaml``——不存在的时侯程序走内置默认（``user_dirs`` 为空），
    于是包里的技能**没有任何发现根指向它们**，界面上永远"识别不到技能"。

    约定与前端产物一致：``None`` = "这件事不归我管"，调用方保持原有行为。

    注意它**不在** ``_internal`` 里：技能含 ``SKILL.md`` 与脚本，用户要能直接打开看/改，
    所以构建脚本把整个目录放在程序根（与 ``chaiai.exe`` 并排）。
    """
    if not is_frozen():
        return None
    candidate = Path(sys.executable).parent / SKILLS_DIR_NAME
    return candidate if candidate.is_dir() else None


def bundled_static_dir() -> Path | None:
    """包内的前端产物目录；不存在或非冻结时返回 ``None``。

    ``None`` 表示"这件事不归我管"：``open_runtime`` 会回落到 ``web.static_dir`` 那条按配置文件
    目录解析的路径，因此源码形态行为不变。
    """
    base = resource_dir()
    if base is None:
        return None
    candidate = base.joinpath(*STATIC_DIR_PARTS)
    return candidate if candidate.is_dir() else None
