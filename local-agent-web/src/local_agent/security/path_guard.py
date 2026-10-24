r"""路径安全：工作区边界、外部路径策略与敏感文件判定（架构 §6.1、§1.5、§11.1）。

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
   5b. **凭据/元数据目录在可写范围内也始终不可写**（2026-10-14，照 codex 的
   ``default_read_only_subpaths_for_writable_root`` 做）：``.aws`` / ``.ssh`` / ``.gnupg`` /
   ``.kube`` / ``.docker`` / ``.codex`` 与 ``.git`` 同级不可审批——"授权一个目录"是"用户认可在那里
   干活"，不等于把里面的身份凭据交出去。工作区**内**同样生效。
6. **路径别名口径**（v0.9 T77，架构 §20.2）：判定只看"系统**真正打开**的那个目标"。
   **能证明的归一、证明不了的拒绝**——Windows 上逐段去掉结尾的点与空格（Win32 API 静默忽略它们，
   而 ``resolve()`` 只对**已存在**的目标展开）；设备命名空间与扩展长度前缀（``\\.\`` / ``\\?\``）
   **拒绝**（它们按设计绕开正常化）；工作区**之外**的远程路径（UNC）**拒绝**（无法证明它别名到
   本机哪个目录，"不可审批目标"因此在它上面判不出来）；大小写沿用 pathlib 与 ``resolve()`` 的
   既有口径（不区分大小写），只有 ``.git`` 分段比较在 Windows 上显式 casefold。
"""

from __future__ import annotations

import fnmatch
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal, cast, get_args

from local_agent.exceptions import SecurityError, WorkspaceBoundaryError

#: 权限档位（``workspace.external_paths.mode``），同时是设置库里的取值域。
#:
#: **2026-10-15 起换成"边界"语义的三档**（对齐 codex / dsh）：档位回答的是"能写到哪"，
#: 不再只是"工作区外能不能读"。
#:
#: 三档均允许本机显式绝对路径的读取；敏感文件由读取后端另行拦截。
#: - ``read-only``：写类工具一律拒绝；命令经"无写授权"的受限令牌运行（Windows）；
#: - ``workspace-write``（**出货默认**）：工作区可写；工作区外的写按逐条授权；
#: - ``danger-full-access``：工作区可写、工作区外**一律可读**，写仍只按逐条授权；Windows 命令同样受写边界约束。
#:
#: 旧值 ``deny`` / ``whitelist`` / ``allow`` 仍在 :data:`LEGACY_MODES` 里接受并归一
#: （``deny``/``whitelist`` → ``workspace-write``，``allow`` → ``danger-full-access``）：
#: 设置库里存着旧值，硬拒绝会让老用户开不了机。
ExternalPathsMode = Literal["read-only", "workspace-write", "danger-full-access"]

#: 旧档位值 → 新档位。迁移与兼容都在这一处，别在两处各写一份（"两处会漂移"是本项目踩过的坑）。
LEGACY_MODES: Mapping[str, ExternalPathsMode] = {
    # `deny` 与 `whitelist` 只差"有没有逐条授权"，工作区都可写 ⇒ 都落在 workspace-write。
    "deny": "workspace-write",
    "whitelist": "workspace-write",
    # `allow` 的语义（外部一律可读、写按授权）正好是 danger-full-access。
    "allow": "danger-full-access",
}


def normalize_mode(value: str) -> ExternalPathsMode:
    """把库里的/配置里的档位取值归一到新三档；无法识别时抛 ``ValueError``。

    调用点：``ExternalPathsConfig`` 的校验、设置库读档位、以及 API 入参。**不静默兜底**：
    认不出来就报错，否则一个拼错的档位会静默变成"默认档"，而默认档恰好是最宽的那一档。
    """
    text = str(value).strip()
    if text in LEGACY_MODES:
        return LEGACY_MODES[text]
    if text in get_args(ExternalPathsMode):
        return cast(ExternalPathsMode, text)
    raise ValueError(f"未知的权限档位：{value}（可选：{'、'.join(get_args(ExternalPathsMode))}）")


#: 允许写入工作区的档位。``read-only`` 不在内——它是"连工作区都不许写"。
WRITABLE_MODES: Final[frozenset[str]] = frozenset({"workspace-write", "danger-full-access"})

#: 一次路径解析得到的访问级别：工作区内 / 默认外部只读 / 外部读写授权。
AccessLevel = Literal["inside", "read", "read_write"]

#: 授权条目允许保存的级别（``inside`` 只属于工作区，不接受保存）。
GrantAccess = Literal["read", "read_write"]

#: **即使落在可写范围里也始终不可写**的凭据与工具元数据目录（2026-10-14，照 codex 的
#: `default_read_only_subpaths_for_writable_root` 做）。
#:
#: 为什么需要这一层：授权一个工作区外目录，意思是"用户认可在那里干活"，**不等于**把里面的身份
#: 凭据一起交出去——`.aws/credentials`、`.ssh/id_rsa`、`.kube/config`、`.codex/auth.json` 一旦被改写，
#: 后果远超"改错一个文件"。所以它们是**不可审批**的受保护目标，与 `.git` 同级。
#:
#: `.git` 单列（下面的判断里保留它原本的拒绝理由），这里只列其余目录。
#: 只收**确属凭据**的目录：`.config` / `.npm` / `.cache` 之流不收——它们太常见，收进来会误伤普通文件。
_PROTECTED_DIRECTORY_NAMES: tuple[str, ...] = (
    ".aws",
    ".ssh",
    ".gnupg",
    ".kube",
    ".docker",
    ".codex",
)

# Shell ACL 物化与路径门共用保留名，包含可能是文件的 .git。
PROTECTED_SUBPATH_NAMES = frozenset((".git", *_PROTECTED_DIRECTORY_NAMES))

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

#: Windows 上**不是普通路径**的两种前缀（§20.2 ③）：扩展长度（``\\?\``，含 ``\\?\UNC\``）与
#: 设备命名空间（``\\.\``）。它们按设计绕开正常化，因此一律拒绝而不是尝试归一。
_WINDOWS_DEVICE_PREFIXES: tuple[str, ...] = ("\\\\?\\", "\\\\.\\")

#: 默认的敏感文件名模式；命中时不允许把内容读进上下文。
SENSITIVE_NAME_PATTERNS: tuple[str, ...] = (
    ".env",
    ".env.*",
    # MCP 配置文件（2026-10-20）：本应用、CodeBuddy、Codex、Trae 都用同一份格式，而 `env` 里是
    # **明文凭据**（cookie / token）。模型需要的是工具，不是连接凭据——读它没有任何正当用途。
    # 这条是端到端验证时发现的：真实模型确实用 `read_file` 打开过 `mcp.json`（它只是没把内容
    # 说出去，不能靠这个运气）。
    "mcp.json",
    "mcp.json.*.tmp",
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
        WorkspaceBoundaryError: 路径为空、带设备前缀（§20.2 ③），或解析后位于工作区之外。
    """
    if not path or not path.strip():
        raise WorkspaceBoundaryError("路径为空")
    _reject_aliased_device_path(path)

    candidate = Path(path.strip()).expanduser()
    if not candidate.is_absolute():
        candidate = base_dir / candidate

    root = _resolve(workspace_root)
    resolved = _resolve(candidate)
    # 两者已在本次检查中解析；is_within 会再次做两次 Windows realpath。
    if resolved != root and root not in resolved.parents:
        raise WorkspaceBoundaryError(
            f"路径在工作区之外，已拒绝：{resolved.as_posix()}（工作区：{root.as_posix()}）"
        )
    return resolved


def is_within(path: Path, root: Path) -> bool:
    """``path`` 是否等于 ``root`` 或位于 ``root`` 之下。"""
    resolved_path = _resolve(path)
    resolved_root = _resolve(root)
    return resolved_path == resolved_root or resolved_root in resolved_path.parents


def display_path(path: Path, workspace_root: Path) -> str:
    """展示用路径（架构 §15.8）：**工作区内用相对路径**，工作区外保持绝对。

    规则只有这一处（`tools/file_tools.py` 的 `_display_path` 与 `tools/shell_tools.py` 的
    `ShellResult.cwd` 都走这里），因为"页面/模型看得见的路径长什么样"必须是**一个**口径：
    工作区内写相对路径（工作区根本身是 ``"."``）；工作区外**必须可核对真实目标**，所以保持绝对——
    而不是把所有绝对路径一概抹掉（§15.8 明令不许那么做）。
    """
    try:
        return path.relative_to(workspace_root).as_posix() or "."
    except ValueError:
        return path.as_posix()


@dataclass(frozen=True)
class ResolvedTarget:
    """一次路径解析的结果：规范化绝对路径 + 是否在工作区外 + 访问级别（架构 §1.5）。"""

    path: Path
    external: bool
    #: ``inside``（工作区内）/ ``read``（默认外部只读）/ ``read_write``（目录写授权）。
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


def _reject_aliased_device_path(raw: str) -> None:
    """Windows：设备命名空间与扩展长度前缀一律拒绝（§20.2 ③）。

    为什么拒绝而不是归一：这两种前缀**按设计**绕开正常化（不折叠 ``..``、不去尾点空格），而本模块
    所有判定都建立在 ``resolve()`` 的结果之上——实测 ``\\\\?\\`` 形态的路径 ``resolve()`` 原样保留，
    而 ``\\\\.\\C:\\Windows\\win.ini`` 会被 ``resolve()`` **静默改写成普通路径**。放行它们就等于让
    "判定看的路径"与"系统打开的路径"变成两条。非 Windows 上什么都不做（那里的 ``\\\\`` 只是普通字符）。

    Raises:
        WorkspaceBoundaryError: 输入带这两种前缀之一。
    """
    if os.name != "nt":
        return
    text = raw.strip()
    if text.startswith(_WINDOWS_DEVICE_PREFIXES):
        raise WorkspaceBoundaryError(
            f"不接受设备路径（{text[:4]}…）：请改用普通路径。"
            "这种前缀会绕过路径正常化（不折叠 `..`、不去结尾的点与空格），"
            "于是校验看到的路径与系统真正打开的目标可能是两条。"
        )


def _is_remote(path: Path) -> bool:
    """该绝对路径是否位于**远程共享**（Windows UNC，``\\\\server\\share\\…``）。"""
    return os.name == "nt" and path.drive.startswith("\\\\")


def _covers(key: str, grant: str) -> bool:
    """已归一化的 ``key`` 是否等于授权目录 ``grant`` 或位于其下。"""
    if key == grant:
        return True
    prefix = grant if grant.endswith(os.sep) else grant + os.sep
    return key.startswith(prefix)


@dataclass(frozen=True)
class ExternalPathPolicy:
    """权限档位与逐条授权（架构 §1.5）。

    三档均可读取显式绝对路径的本机外部目标；读取后端保留敏感文件检查。
    ``read-only`` 拒绝一切文件变更；其余档位允许写工作区，外部写仍须 ``read_write`` 授权。
    ``danger-full-access`` 的 Shell 不进入 OS 写侧沙箱，文件工具仍保留授权和保护检查。
    旧 ``read`` 记录可继续保存和移除，但不再决定读取是否可达，也不收窄父级读写授权。
    """

    mode: ExternalPathsMode = "workspace-write"
    #: 规范化路径 → 访问级别；键必须由 :func:`normalize_grant_path` 生成。
    grants: Mapping[str, GrantAccess] = field(default_factory=dict)
    #: 内部委派保留指定 scope 的边界；不作为用户档位、配置或 API 字段公开。
    workspace_only: bool = False

    def allows_workspace_write(self) -> bool:
        """当前档位是否允许**写工作区内的文件**（2026-10-15）。

        ``read-only`` 是唯一的"连工作区也不许写"的档：它必须由**判定层**回答，而不是靠用户自律
        （codex 的 ``read-only`` 是只读挂载，语义就是"写不进去"）。
        """
        return self.mode in WRITABLE_MODES

    def _granted_level(self, resolved: Path) -> AccessLevel | None:
        """逐条授权给出的级别（``read_write`` 优先）；没有条目覆盖到就是 ``None``。"""
        key = os.path.normcase(str(resolved))
        best: AccessLevel | None = None
        for grant_path, access in self.grants.items():
            if not _covers(key, grant_path):
                continue
            if access == "read_write":
                return "read_write"
            best = "read"
        return best

    def access_for(self, resolved: Path) -> AccessLevel | None:
        """本机外部目标默认只读；只读档不因已有可写授权而获得写权限。"""
        if self.workspace_only:
            return None
        if self.mode == "read-only":
            return "read"
        return self._granted_level(resolved) or "read"

    def reaches(self, resolved: Path) -> bool:
        """该外部目标是否可达；本机路径默认可读，路径格式与 UNC 仍由解析层校验。

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
    """解析路径并给出访问级别；外部默认只读，限定 scope 的内部策略仍拒绝越界。

    Args:
        path: 用户或模型给的原始路径字符串。
        base_dir: 相对路径的基准目录（本项目即当前工作区）。
        workspace_root: 当前工作区根目录。
        external_policy: 外部路径档位与授权；``None`` 等价 ``deny``（拒绝一切外部目标）。

    Returns:
        :class:`ResolvedTarget`：工作区内为 ``inside``，外部默认 ``read``，写授权为 ``read_write``。

    Raises:
        WorkspaceBoundaryError: 空路径，或目标在工作区外且未被授权。
    """
    text = path.strip()
    if not text:
        raise WorkspaceBoundaryError("路径不能为空")
    _reject_aliased_device_path(text)

    raw_absolute = Path(text).is_absolute()
    candidate = Path(text).expanduser()
    if not raw_absolute:
        candidate = base_dir / candidate
    resolved = _resolve(candidate)
    root = _resolve(workspace_root)
    # 只比较刚解析出的实际路径，避免 is_within 重复查询同一组路径。
    if resolved == root or root in resolved.parents:
        return ResolvedTarget(path=resolved, external=False, access="inside")
    if not raw_absolute:
        # `~`、相对路径逃逸与 Windows 盘符相对路径：授权也救不了——授权的是**目录**，
        # 而这类输入根本不指向用户显式认可的绝对目标（架构 §1.5）。
        raise WorkspaceBoundaryError(
            f"路径超出工作区：{resolved.as_posix()}（工作区 {root.as_posix()}）；"
            "工作区外路径只能用绝对路径显式指定（`~`、相对路径与 Windows 盘符相对路径不在此列）"
        )
    if _is_remote(resolved):
        # 工作区**之外**的远程路径一律拒绝，档位与授权都不放行（§20.2 ④）：实测
        # `\\localhost\C$\Windows\win.ini` 判不出受保护目标，于是 `allow` 档下的
        # "系统关键目录不可审批"被一个 UNC 别名绕过。这是一次**能力收窄**，契约里如实写明。
        raise WorkspaceBoundaryError(
            f"工作区外的远程路径（UNC）不受支持：{resolved.as_posix()}（工作区 {root.as_posix()}）。"
            "本阶段无法证明一个远程路径别名到本机的哪个目录，"
            "因此「系统关键目录 / 主目录 / Git 元数据不可审批」在它上面判不出来；"
            "请把该共享设为工作区，或改用本地路径。"
        )
    access = None if external_policy is None else external_policy.access_for(resolved)
    if access is None:
        if external_policy is not None and external_policy.workspace_only:
            raise WorkspaceBoundaryError(
                f"路径超出本次委派范围：{resolved.as_posix()}（范围：{root.as_posix()}）；"
                "目录授权不能扩大本次委派 scope，请在父任务中处理该路径。"
            )
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
    return _protected_resolved_reason(resolved, _resolve(Path.home()))


@dataclass(frozen=True)
class ProtectedPathContext:
    """单轮目录扫描的固定保护边界；不跨扫描缓存环境或用户目录。"""

    home: Path
    system_directories: tuple[Path, ...]

    @classmethod
    def current(cls) -> ProtectedPathContext:
        return cls(_resolve(Path.home()), _system_directories())

    def reason_for_resolved(self, path: Path) -> str | None:
        """调用方已解析过实际路径；保留 Windows 别名归一，不重复查询文件系统。"""
        return _protected_resolved_reason(
            _strip_windows_trailing(path), self.home, self.system_directories,
        )


def _protected_resolved_reason(
    resolved: Path, home: Path, system_directories: tuple[Path, ...] | None = None,
) -> str | None:
    if resolved.parent == resolved:
        return "文件系统或驱动器根目录"
    if resolved == home:
        return "当前用户主目录"
    # Windows 比较路径分段必须不区分大小写（§20.2 ②）：`.GIT` 与 `.git` 是同一个目录。
    # 已存在的目标经 `resolve()` 会取磁盘上的真实大小写，所以这里是"目标**尚不存在**"那一半。
    parts = (
        tuple(part.casefold() for part in resolved.parts)
        if os.name == "nt"
        else resolved.parts
    )
    if ".git" in parts:
        return "Git 元数据目录"
    for name in _PROTECTED_DIRECTORY_NAMES:
        if name in parts:
            return f"凭据或工具元数据目录（{name}）"
    for directory in _system_directories() if system_directories is None else system_directories:
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
    """解析成绝对路径，并做**平台别名归一**（§20.2 ①）。

    Windows 上再逐段去掉结尾的点与空格：Win32 API 静默忽略它们（`".env "` 打开的就是 `.env`），
    而 `resolve()` 只在目标**已经存在**时会这样展开。不归一就会出现"判定看的名字"与"系统打开的目标"
    不一致——实证：干净目录里写 `".env "` 真的造出了 `.env`，而判定当时说"不敏感"。
    """
    try:
        resolved = path.resolve()
    except OSError:  # pragma: no cover - 极少数无法解析的路径
        resolved = path.absolute()
    return _strip_windows_trailing(resolved)


def _strip_windows_trailing(path: Path) -> Path:
    """Windows：逐段去掉**结尾的点与空格**（§20.2 ①）；其它平台原样返回。

    为什么必须做：Win32 API 静默忽略分段结尾的点与空格，所以 `".env "` 与 `".env"` 是**同一个文件**。
    `Path.resolve()` 只对**已经存在**的目标展开（它靠打开文件句柄取真实名字），于是"写到还不存在的
    `.env `"会绕过敏感文件判定。归一之后，校验与执行看的是同一个名字。

    仅由点构成的分段（`"..."`）原样保留：`rstrip(". ")` 会把它抹成空段，那是猜。实测这种分段通常
    已被上一步 `resolve()` 按 Windows 语义塌缩（`sub/.../a.txt` ⇒ `sub/a.txt`），这里只是不额外猜一次。
    """
    if os.name != "nt":
        return path
    parts = path.parts
    fixed: list[str] = []
    changed = False
    for part in parts:
        stripped = part.rstrip(". ")
        if not stripped or stripped == part:
            fixed.append(part)
            continue
        fixed.append(stripped)
        changed = True
    return Path(*fixed) if changed else path
