"""路径安全：工作区边界与敏感文件判定（架构 §6.1、§11.1）。

规则：

1. 相对路径按给定的基准目录解析，不依赖进程当前目录，行为可预期。
2. ``..``、``~`` 与符号链接都会被 ``resolve()`` 展开，**展开之后再校验一次**边界：
   这样“工作区内指向工作区外的软链接”也会被拦住。
3. 目标不存在时同样可以校验（``resolve(strict=False)``），因此“读一个不存在的外部路径”
   也会先被边界拦截，而不是先报“文件不存在”。
4. 敏感文件（``.env``、私钥等）由 :func:`is_sensitive_path` 单独判定，供调用方决定
   拦截或脱敏。
"""

from __future__ import annotations

import fnmatch
from pathlib import Path

from local_agent.exceptions import WorkspaceBoundaryError

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


def is_sensitive_path(path: Path, patterns: tuple[str, ...] = SENSITIVE_NAME_PATTERNS) -> bool:
    """文件名是否命中敏感模式（只匹配文件名，不匹配目录名）。"""
    name = path.name.lower()
    return any(fnmatch.fnmatch(name, pattern) for pattern in patterns)


def _resolve(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:  # pragma: no cover - 极少数无法解析的路径
        return path.absolute()
