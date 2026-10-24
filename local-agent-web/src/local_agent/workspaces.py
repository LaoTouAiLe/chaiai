"""工作区路径校验（架构 §1.5、§10.6）。

这些规则**必须只有一份**：Streamlit 入口与 HTTP API 都要在跑图之前校验工作区（目录被删、
不是目录、没有读/进入权限、或用户输入的不是绝对路径）。放在这里而不是 ``web.py``：后者
导入 Streamlit，API 层不得依赖它（架构 §15.1 的依赖方向）。

判定口径（与 v0.1–v0.3 完全一致）：

- **必须原始输入即绝对路径**：判定在展开 ``~`` 之前做，所以 ``~`` 与相对路径都会被拒；
- 必须存在、是目录、当前进程可访问（POSIX 上还要 ``X_OK``，否则进不去也遍历不了）。
"""

from __future__ import annotations

import os
from pathlib import Path

from local_agent.exceptions import SessionStoreError

__all__ = ["validate_workspace", "verify_workspace"]


def verify_workspace(path: Path) -> Path:
    """校验工作区目录**仍然**存在、是目录且可访问（架构 §1.5、§10.6）。

    每一轮运行前都要过这里：只检查 ``is_dir()`` 挡不住直接调用服务/图的路径。

    Raises:
        SessionStoreError: 目录不存在、不是目录或当前进程不可访问。
    """
    try:
        resolved = Path(path).resolve(strict=True)
    except OSError as exc:
        raise SessionStoreError(f"当前工作区不存在或无法访问：{Path(path).as_posix()}") from exc
    if not resolved.is_dir():
        raise SessionStoreError(f"当前工作区不是目录：{resolved.as_posix()}")
    required = os.R_OK | (os.X_OK if os.name != "nt" else 0)
    if not os.access(resolved, required):
        raise SessionStoreError(f"当前工作区不可访问（需要读取与进入权限）：{resolved.as_posix()}")
    return resolved


def validate_workspace(raw_path: str) -> Path:
    """校验用户输入的工作区路径：必须是**原始输入即绝对路径**、存在、是目录、可访问。

    非法输入只提示，**不切换会话**——不能把当前会话弄丢（架构 §1.5）。

    Raises:
        SessionStoreError: 路径为空、非绝对路径，或 :func:`verify_workspace` 的任一条件不满足。
    """
    text = raw_path.strip().strip('"').strip("'")
    if not text:
        # 示例按**平台**给（2026-10-16）：原来写死的是开发机的盘符路径，
        # 换台机器（尤其 macOS/Linux）照抄会得到一条根本不存在的路径。
        example = "C:/projects/demo" if os.name == "nt" else "/home/you/projects/demo"
        raise SessionStoreError(f"请填写工作区绝对路径，例如 {example}")
    if not Path(text).is_absolute():
        raise SessionStoreError(f"工作区必须是绝对路径：{text}")
    return verify_workspace(Path(text))
