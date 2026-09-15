"""项目自定义异常。

第三方库的异常（``sqlite3.Error``、``pydantic.ValidationError`` 等）保持原样使用；
这里只放本项目自己的错误类型，便于调用方精确捕获。
"""

from __future__ import annotations


class LocalAgentError(Exception):
    """本项目所有自定义异常的基类。"""


class SecurityError(LocalAgentError):
    """安全策略拒绝：路径越界、敏感文件、未放行的工具等。"""


class WorkspaceBoundaryError(SecurityError):
    """路径解析后位于工作区之外。"""


class SessionStoreError(LocalAgentError):
    """会话索引操作被拒绝：会话不存在、名称为空或超长等。"""
