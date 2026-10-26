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

**源码形态**：资源函数返回 ``None``，默认配置与 .env 固定在含 pyproject.toml 的源码项目目录。
显式传入的配置路径仍优先；安装形态找不到源码目录时回落原相对路径。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Final

from local_agent.config import DEFAULT_CONFIG_PATH, DEFAULT_ENV_PATH, AppConfig

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


#: 覆盖数据根的环境变量（2026-10-26）。与 ``CODEX_HOME`` / ``HERMES_HOME`` 同一位置：
#: 它在**配置被读出来之前**生效，所以设置库打不开、或者想在两套数据之间切换时仍然有路。
DATA_ROOT_ENV: Final[str] = "CHAIAI_HOME"

#: 指向自定义数据根的指针文件（2026-10-26）。**固定放在平台默认目录**，不随数据根移动：
#: 跟随移动的话，改过一次就再也找不到它，下次启动又回到旧位置。
DATA_ROOT_POINTER: Final[str] = "data-root.txt"


def data_root() -> Path:
    """用户数据的根目录：**两种形态都给同一个绝对路径**（2026-10-26）。

    与 :func:`data_dir` 的分工：那个只在冻结形态返回、源码形态故意返回 ``None``（让调用方
    回落到相对路径）；数据根不能这样——它必须给一个稳定答案，否则"数据在哪"取决于你怎么
    启动、从哪个目录启动。这正是把它从"相对配置文件目录"改成绝对路径的理由：

    - 数据与代码解耦：源码目录会被 ``git clean``、重新 clone、换分支，数据不能跟着走；
    - 可预测：任何工作目录启动都是同一份数据；
    - 备份/清理有固定锚点，运维脚本不必知道代码放在哪。

    顺序见 :func:`data_root_of`；没有配置上下文时是"环境变量 → 指针文件 → 平台标准目录"。
    **没有**"相对当前目录"这一档。
    """
    override = _env_override()
    if override is not None:
        return override
    pointer = read_data_root_pointer()
    return pointer if pointer is not None else platform_default_root()


def platform_default_root() -> Path:
    """平台标准数据目录（不含任何覆盖）。

    单独一个函数是因为**指针文件也放在这里**：如果指针文件跟着 ``data_root`` 走，改过一次就
    再也找不到它，下一次启动又会回到旧位置——那就成了自举死锁。
    """
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
        if local_app_data:
            return Path(local_app_data) / APP_DIR_NAME
    elif sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_DIR_NAME
    xdg_data_home = os.environ.get("XDG_DATA_HOME", "").strip()
    base = Path(xdg_data_home) if xdg_data_home else Path.home() / ".local" / "share"
    return base / APP_DIR_NAME


def _env_override() -> Path | None:
    """``CHAIAI_HOME``：在**读配置之前**就生效，设置库打不开时仍然有路。"""
    override = os.environ.get(DATA_ROOT_ENV, "").strip()
    return Path(override).expanduser() if override else None


def data_root_pointer() -> Path:
    """指向自定义数据根的指针文件（固定放在平台标准目录里）。"""
    return platform_default_root() / DATA_ROOT_POINTER


def read_data_root_pointer() -> Path | None:
    """读指针文件；不存在、为空、内容不是绝对路径都返回 ``None``（回落到默认，不猜）。"""
    path = data_root_pointer()
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text:
        return None
    candidate = Path(text).expanduser()
    return candidate if candidate.is_absolute() else None


def set_data_root_pointer(target: str | Path) -> Path:
    """把数据根改到 ``target`` 并落盘，返回规范化后的路径。

    **校验在这里做，不在启动时**：启动才失败意味着"服务起不来，只能手改文件救回来"。
    所以保存前就要确认这是绝对路径、能创建、能写（写一个探针文件再删掉）。

    目标等于平台默认目录时不写指针文件，而是**删掉它**——否则"恢复默认"会变成写一条与默认
    相同的记录，日后再改平台默认（换机器、换系统）就跟着错了。

    Raises:
        ValueError: 不是绝对路径，或该目录不可创建/不可写。
    """
    text = str(target).strip()
    if text == "":
        raise ValueError("数据目录不能为空；要恢复默认请用「恢复默认位置」")
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        raise ValueError("数据目录必须是绝对路径：相对路径会随启动目录漂移")
    if _same_path(candidate, platform_default_root()):
        reset_data_root_pointer()
        return platform_default_root()
    try:
        candidate.mkdir(parents=True, exist_ok=True)
        probe = candidate / ".chaiai-write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise ValueError(f"这个目录不可写（{type(exc).__name__}），请换一个位置") from exc
    pointer = data_root_pointer()
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(str(candidate), encoding="utf-8", newline="\n")
    return candidate


def reset_data_root_pointer() -> None:
    """删掉指针文件 = 回到平台标准目录（幂等）。"""
    data_root_pointer().unlink(missing_ok=True)


def _same_path(left: Path, right: Path) -> bool:
    """比较两个路径是否指同一处：``resolve()`` 在目标还不存在时会抛错，所以退回字面比较。"""
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return os.path.normcase(str(left)) == os.path.normcase(str(right))


def data_root_source(config: AppConfig) -> str:
    """数据根**从哪来**：``environment`` / ``settings``（页面改的指针）/ ``config`` / ``default``。

    页面要如实告诉用户"这一项现在由谁决定"：环境变量的优先级最高，那时在页面上改不会生效，
    必须说清楚，而不是让他改完重启后发现没变。
    """
    if _env_override() is not None:
        return "environment"
    if read_data_root_pointer() is not None:
        return "settings"
    if str(config.data_root or "").strip():
        return "config"
    return "default"


def data_root_of(config: AppConfig) -> Path:
    """配置解析出的数据根，按优先级：环境变量 → 指针文件 → ``config.data_root`` → 平台默认。

    为什么**指针优先于 config.yaml**：指针是"用户最近一次在设置页里的选择"，而他可能早就忘了
    YAML 里还留着一行旧值。页面改完不生效，比"不能改"更糟。

    为什么要有指针这一档：设置库（存偏好的地方）**本身就在数据根下**，把数据根存进设置库等于
    让一个东西定义它自己的位置——改到新位置后新库是空的，那条偏好丢了，启动又回到旧位置。
    指针文件放在**平台默认目录**，不随数据根移动，所以读得到。

    放在这里而不是装配层：装配、启动横幅、诊断、服务内部都要回答"数据在哪"，各处自己算一次
    迟早会分叉（hermes 就因为 HOME 有多条解析路径踩过"写进错误 profile"的坑）。
    """
    override = _env_override()
    if override is not None:
        return override
    pointer = read_data_root_pointer()
    if pointer is not None:
        return pointer
    configured = str(config.data_root or "").strip()
    return Path(configured).expanduser() if configured else platform_default_root()


def default_config_path() -> Path:
    """``--config`` 的默认值。

    冻结时**必须**落在数据目录里：``api/__main__.py`` 把"配置文件所在目录"当作相对路径的解析
    基准（``open_runtime(base_dir=...)``），所以这一条同时决定了聊天库、设置库与工作区默认值
    落在哪。原来的默认值 ``config.yaml`` 是相对**当前工作目录**解析的：双击 exe 时通常是程序
    目录，经快捷方式或从别的目录启动就会漂移，漂移后还会静默进入初始设置模式——冻结形态不能
    接受这种不确定性。
    """
    base = data_dir()
    if base is not None:
        return base / DEFAULT_CONFIG_PATH
    if is_frozen():
        return DEFAULT_CONFIG_PATH
    source_root = Path(__file__).resolve().parents[2]
    if (source_root / "pyproject.toml").is_file():
        return source_root / DEFAULT_CONFIG_PATH
    return DEFAULT_CONFIG_PATH


def default_env_path() -> Path:
    """``--env`` 的默认值：与 :func:`default_config_path` 同一目录（旧密钥路径继续可用）。"""
    return default_config_path().with_name(DEFAULT_ENV_PATH.name)


def bundled_binary(name: str) -> Path | None:
    """随包的**外部可执行文件**（如 ``rg.exe``）；源码形态或文件不存在时返回 ``None``。

    为什么要有这一条：``shutil.which`` 只看 PATH，朋友的机器上通常没有 ripgrep，
    于是代码检索一直走较慢的 Python 扫描。包里把 ``rg.exe`` 放在 ``_internal/bin/``，
    这里负责把它变成"可见的绝对路径"，调用方仍按"包内优先、PATH 兜底"的顺序解析，
    这样源码形态与装过 rg 的开发机行为不变。
    """
    filename = name if name.lower().endswith(".exe") else f"{name}.exe"
    base = resource_dir()
    if base is None:
        return None
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
