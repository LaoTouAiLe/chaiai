"""回收站（2026-10-21：改为**系统回收站**）。

早先的实现是自建的 ``data/trash``（按会话分目录 + manifest 清单）。它的问题是**用户找不到**：
删除时提示"已移入回收站"，人第一反应是去资源管理器的回收站看，而那里什么都没有——文件其实
安全地躺在应用的数据目录里，但没有任何入口能打开它。

现在直接用系统回收站：

- Windows：``SHFileOperationW``（``send2trash`` 封装）；
- macOS / Linux：由 ``send2trash`` 各自走原生实现，跨平台差异不再由我们承担；
- 「还原」也回到用户熟悉的那条路：回收站右键 → 还原，文件回到**原位置**。

仍然保留的硬约束：**失败抛错，绝不回退成直接删除**。用户开着「安全删除」，就等于要求
"宁可失败也别丢"；静默降级成 ``unlink`` 是最糟的结果——看起来成功了，东西没了。
"""

from __future__ import annotations

from pathlib import Path

from send2trash import TrashPermissionError, send2trash

from local_agent.exceptions import FileOperationError


def move_to_trash(target: Path) -> None:
    """把 ``target`` 移入**系统回收站**（文件或目录整体移走，可从回收站还原到原位置）。

    Args:
        target: 要移走的路径。这里会 ``resolve()`` 后再交给 ``send2trash``：
            Windows 的 ``SHFileOperation`` 要求绝对路径，相对路径在不同工作目录下行为不一致。

    Raises:
        FileOperationError: 目标不存在、没有权限、或该位置不支持回收站（网络盘、某些挂载点）。
            **失败即失败**，不会退化成直接删除。
    """
    source = Path(target)
    if not source.exists():
        # send2trash 对不存在的路径在 Windows 上可能静默返回成功，先自己拦一道。
        raise FileOperationError(f"目标不存在：{source.as_posix()}")
    try:
        send2trash(str(source.resolve()))
    except (OSError, TrashPermissionError) as exc:
        raise FileOperationError(
            f"移入系统回收站失败（{type(exc).__name__}）：{exc}；文件没有被删除。"
        ) from exc
