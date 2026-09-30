"""路径安全：工作区边界、外部路径策略与敏感文件判定（架构 §6.1、§1.5、§11.1）。

规则：

1. 相对路径按给定的基准目录解析，不依赖进程当前目录，行为可预期。
2. ``..``、``~`` 与符号链接都会被 ``resolve()`` 展开，**展开之后再校验一次**边界：
   这样“工作区内指向工作区外的软链接”也会被拦住。
3. 目标不存在时同样可以校验（``resolve(strict=False)``），因此“读一个不存在的外部路径”
   也会先被边界拦截，而不是先报“文件不存在”。
4. 敏感文件（``.env``、私钥等）由 :func:`is_sensitive_path` 单独判定，供调用方决定
   拦截或脱敏。
5. 工作区外路径（T14C/T38，架构 §1.5）：只有 **原始输入本身**是绝对路径、且当前档位
   （``ExternalPathPolicy``）认可该目标时才放行；``~``、相对路径逃逸与 Windows 盘符相对
   路径一律拒绝。放行只针对这一次显式操作，不改变作用域，并带上访问级别
   （``read`` / ``read_write``）。此外另有一组"不可审批目标"（根目录、主目录、Git 元数据、
   系统关键目录）即使人工批准也不执行副作用。
"""

from __future__ import annotations

import fnmatch
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from local_agent.exceptions import SecurityError, WorkspaceBoundaryError

#: 工作区外路径的档位（``workspace.external_paths.mode``），同时是设置库里的取值域。
ExternalPathsMode = Literal["deny", "whitelist", "allow"]

#: 一次路径解析得到的访问级别：工作区内 / 只读授权 / 读写授权。
AccessLevel = Literal["inside", "read", "read_write"]

#: 授权条目允许保存的级别（``inside`` 只属于工作区，不接受保存）。
GrantAccess = Literal["read", "read_write"]

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
    """一次路径解析的结果：规范化绝对路径 + 是否在工作区外 + 访问级别（架构 §1.5）。"""

    path: Path
    external: bool
    #: ``inside``（工作区内）/ ``read``（只读授权）/ ``read_write``（读写授权）。
    access: AccessLevel = "inside"

    @property
    def writable(self) -> bool:
        """是否**视同工作区内**、走常规写规则（读写授权与工作区内为真）。"""
        return self.access in ("inside", "read_write")


def normalize_grant_path(path: str | Path) -> str:
    """授权条目的规范化键：展开 ``~`` 与符号链接，并统一大小写与路径分隔符。

    同一个目录写成 ``E:/data`` 与 ``e:\\data`` 必须落到同一个键上，否则"授权过却仍被拦下"
    会变成只能靠肉眼比对路径才能发现的 bug（架构 §1.5）。
    """
    return os.path.normcase(str(_resolve(Path(str(path)).expanduser())))


def _covers(key: str, grant: str) -> bool:
    """已归一化的 ``key`` 是否等于授权目录 ``grant`` 或位于其下。"""
    if key == grant:
        return True
    prefix = grant if grant.endswith(os.sep) else grant + os.sep
    return key.startswith(prefix)


@dataclass(frozen=True)
class ExternalPathPolicy:
    """工作区外路径的档位与逐条授权（T38；架构 §1.5）。

    - ``deny``：拒绝一切工作区外目标（授权条目也不会被读到）；
    - ``whitelist``：只放行 ``grants`` 覆盖到的目标，级别取该条目的 ``read`` / ``read_write``；
      空白名单等价于 ``deny``——出厂默认因此是"拒绝一切外部路径"；
    - ``allow``：显式绝对路径的外部目标一律按 ``read_write`` 放行。

    档位只决定"常规小改动是否需要逐次询问"，不放宽任何硬约束：删除文件、覆盖已有文件、
    破坏性 Shell 命令、大范围编辑由调用方照旧要求人工审批；受保护目标照旧不可审批。
    """

    mode: ExternalPathsMode = "whitelist"
    #: 规范化路径 → 访问级别；键必须由 :func:`normalize_grant_path` 生成。
    grants: Mapping[str, GrantAccess] = field(default_factory=dict)

    def access_for(self, resolved: Path) -> AccessLevel | None:
        """该外部目标可获得的访问级别；未被授权时返回 ``None``。"""
        if self.mode == "deny":
            return None
        if self.mode == "allow":
            return "read_write"
        key = os.path.normcase(str(resolved))
        best: AccessLevel | None = None
        for grant_path, access in self.grants.items():
            if not _covers(key, grant_path):
                continue
            if access == "read_write":
                return "read_write"
            best = "read"
        return best

    def reaches(self, resolved: Path) -> bool:
        """该外部目标是否**可达**（``allow`` 档或被任一授权条目覆盖）。

        "可达"与"能写"是两回事：外部工作目录虽然可达，仍必须人工审批（架构 §1.5）。
        """
        return self.access_for(resolved) is not None


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
    external_policy: ExternalPathPolicy | None = None,
) -> ResolvedTarget:
    """解析路径并给出访问级别；未被授权的越界输入直接失败。

    Args:
        path: 用户或模型给的原始路径字符串。
        base_dir: 相对路径的基准目录（本项目即当前工作区）。
        workspace_root: 当前工作区根目录。
        external_policy: 外部路径档位与授权；``None`` 等价 ``deny``（拒绝一切外部目标）。

    Returns:
        :class:`ResolvedTarget`：工作区内为 ``inside``，其余取授权给出的级别。

    Raises:
        WorkspaceBoundaryError: 空路径，或目标在工作区外且未被授权。
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
        return ResolvedTarget(path=resolved, external=False, access="inside")
    if not raw_absolute:
        # `~`、相对路径逃逸与 Windows 盘符相对路径：授权也救不了——授权的是**目录**，
        # 而这类输入根本不指向用户显式认可的绝对目标（架构 §1.5）。
        raise WorkspaceBoundaryError(
            f"路径超出工作区：{resolved.as_posix()}（工作区 {root.as_posix()}）；"
            "工作区外路径只能用绝对路径显式指定（`~`、相对路径与 Windows 盘符相对路径不在此列）"
        )
    access = None if external_policy is None else external_policy.access_for(resolved)
    if access is None:
        # 提示必须指向用户真能找到的地方（T38）：这句经工具结果回填后由模型转述给最终用户，
        # 而界面上没有 `workspace.allow_external_paths` 这个开关。
        raise WorkspaceBoundaryError(
            f"该路径在工作区外且未被授权：{resolved.as_posix()}（工作区 {root.as_posix()}）。"
            "请在「设置 → 权限」中添加授权，或改到工作区内操作。"
        )
    return ResolvedTarget(path=resolved, external=True, access=access)


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
