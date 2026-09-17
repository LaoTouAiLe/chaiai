"""路径安全：工作区边界、外部路径策略与敏感文件判定（架构 §6.1、§1.5、§11.1）。

规则：

1. 相对路径按给定的基准目录解析，不依赖进程当前目录，行为可预期。
2. ``..``、``~`` 与符号链接都会被 ``resolve()`` 展开，**展开之后再校验一次**边界：
   这样“工作区内指向工作区外的软链接”也会被拦住。
3. 目标不存在时同样可以校验（``resolve(strict=False)``），因此“读一个不存在的外部路径”
   也会先被边界拦截，而不是先报“文件不存在”。
4. 敏感文件（``.env``、私钥等）由 :func:`is_sensitive_path` 单独判定，供调用方决定
   拦截或脱敏。
5. 工作区外路径（T14C，架构 §1.5）：只有 ``workspace.allow_external_paths=true`` 且
   **原始输入本身**是绝对路径时才放行；``~``、相对路径逃逸与 Windows 盘符相对路径一律
   拒绝。放行只针对这一次显式操作，不改变作用域。此外另有一组"不可审批目标"
   （根目录、主目录、Git 元数据、系统关键目录）即使人工批准也不执行副作用。
"""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass
from pathlib import Path

from local_agent.exceptions import SecurityError, WorkspaceBoundaryError

#: Windows 上代表操作系统关键目录的环境变量（未设置的跳过）。
_SYSTEM_DIR_ENV_WINDOWS: tuple[str, ...] = (
    "SystemRoot",
    "windir",
    "ProgramFiles",
    "ProgramFiles(x86)",
    "ProgramData",
)

#: POSIX 上的系统关键目录。
_SYSTEM_DIRS_POSIX: tuple[str, ...] = (
    "/etc",
    "/usr",
    "/bin",
    "/sbin",
    "/boot",
    "/dev",
    "/proc",
    "/sys",
    "/var",
    "/Library",
    "/System",
)

#: 默认的敏感文件名模式；命中时不允许把内容读进上下文。
SENSITIVE_NAME_PATTERNS: tuple[str, ...] = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.pfx",
    "*.keystore",
    "id_rsa*",
    "id_ed25519*",
    "id_ecdsa*",
    "credentials",
    "credentials.*",
)


def resolve_within_workspace(path: str, *, base_dir: Path, workspace_root: Path) -> Path:
    """把路径解析为绝对路径，并确认它仍在工作区内。

    Args:
        path: 用户或模型给出的路径，可相对可绝对。
        base_dir: 相对路径的解析基准。
        workspace_root: 工作区根目录，边界。

    Returns:
        解析并展开符号链接后的绝对路径（目标可以不存在）。

    Raises:
        WorkspaceBoundaryError: 解析后的路径位于工作区之外。
    """
    if not path or not path.strip():
        raise WorkspaceBoundaryError("路径为空")

    candidate = Path(path.strip()).expanduser()
    if not candidate.is_absolute():
        candidate = base_dir / candidate

    root = _resolve(workspace_root)
    resolved = _resolve(candidate)
    if not is_within(resolved, root):
        raise WorkspaceBoundaryError(
            f"路径在工作区之外，已拒绝：{resolved.as_posix()}（工作区：{root.as_posix()}）"
        )
    return resolved


def is_within(path: Path, root: Path) -> bool:
    """``path`` 是否等于 ``root`` 或位于 ``root`` 之下。"""
    resolved_path = _resolve(path)
    resolved_root = _resolve(root)
    return resolved_path == resolved_root or resolved_root in resolved_path.parents


@dataclass(frozen=True)
class ResolvedTarget:
    """一次路径解析的结果：规范化绝对路径 + 是否落在当前工作区之外（架构 §1.5）。"""

    path: Path
    external: bool


def look_absolute(raw: str) -> bool:
    """**按原始输入**判断是否绝对路径（架构 §1.5，T14C）。

    必须在展开 ``~``、拼接工作区、解析 ``..`` **之前**判定：``~``、相对路径和 Windows
    盘符相对路径（``C:foo``）都不是绝对路径，因此不能作为"工作区外目标"被放行。
    """
    text = raw.strip()
    return bool(text) and Path(text).is_absolute()


def resolve_target(
    path: str,
    *,
    base_dir: Path,
    workspace_root: Path,
    allow_external: bool = False,
) -> ResolvedTarget:
    """解析路径并标记它是否在工作区外；严格模式下越界直接失败。

    Args:
        path: 用户或模型给的原始路径字符串。
        base_dir: 相对路径的基准目录（本项目即当前工作区）。
        workspace_root: 当前工作区根目录。
        allow_external: ``workspace.allow_external_paths``。只有为真、**且原始输入本身**
            是绝对路径时，工作区外的目标才放行。

    Returns:
        :class:`ResolvedTarget`。

    Raises:
        WorkspaceBoundaryError: 空路径，或越界且当前策略不允许。
    """
    text = path.strip()
    if not text:
        raise WorkspaceBoundaryError("路径不能为空")

    raw_absolute = Path(text).is_absolute()
    candidate = Path(text).expanduser()
    if not raw_absolute:
        candidate = base_dir / candidate
    resolved = _resolve(candidate)
    root = _resolve(workspace_root)
    if is_within(resolved, root):
        return ResolvedTarget(path=resolved, external=False)
    if raw_absolute and allow_external:
        return ResolvedTarget(path=resolved, external=True)
    hint = (
        "；只有原始输入为绝对路径的目标才能作为外部路径访问"
        "（`~`、相对路径与 Windows 盘符相对路径不在此列）"
        if allow_external
        else "；如需访问工作区外路径，请显式设置 workspace.allow_external_paths=true"
    )
    raise WorkspaceBoundaryError(
        f"路径超出工作区：{resolved.as_posix()}（工作区 {root.as_posix()}）{hint}"
    )


def protected_target_reason(path: Path) -> str | None:
    """"不可审批目标"的原因；不受保护时返回 ``None``（架构 §1.5，T14C）。

    受保护：文件系统/驱动器根目录、当前用户主目录根、Git 元数据目录、操作系统关键目录
    （及其内部）。这些目标**即使人工批准也不执行**副作用。只约束副作用（写入/修改/删除/
    外部工作目录）；工作区根目录由各工具自己的检查负责（例如删除时禁止删根），不在这里判，
    否则 ``run_shell`` 默认的 ``cwd="."``（= 工作区根）会被误伤。
    """
    resolved = _resolve(path)
    if resolved.parent == resolved:
        return "文件系统或驱动器根目录"
    if resolved == _resolve(Path.home()):
        return "当前用户主目录"
    if ".git" in resolved.parts:
        return "Git 元数据目录"
    for directory in _system_directories():
        if resolved == directory or directory in resolved.parents:
            return f"系统关键目录（{directory.as_posix()}）"
    return None


def assert_not_protected_target(path: Path, *, verb: str) -> None:
    """受保护目标的副作用一律拒绝（不是"更高审批"，而是不可审批）。

    Raises:
        SecurityError: 目标是受保护目录（或位于其中）。
    """
    reason = protected_target_reason(path)
    if reason:
        raise SecurityError(f"禁止{verb}受保护目标（{reason}）：{_resolve(path).as_posix()}")


def _system_directories() -> tuple[Path, ...]:
    """当前平台的操作系统关键目录（取不到的环境变量跳过）。"""
    if os.name == "nt":
        values = [os.environ.get(name, "") for name in _SYSTEM_DIR_ENV_WINDOWS]
        return tuple(_resolve(Path(value)) for value in values if value.strip())
    return tuple(_resolve(Path(item)) for item in _SYSTEM_DIRS_POSIX)


def is_sensitive_path(path: Path, patterns: tuple[str, ...] = SENSITIVE_NAME_PATTERNS) -> bool:
    """文件名是否命中敏感模式（只匹配文件名，不匹配目录名）。"""
    name = path.name.lower()
    return any(fnmatch.fnmatch(name, pattern) for pattern in patterns)


def _resolve(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:  # pragma: no cover - 极少数无法解析的路径
        return path.absolute()
