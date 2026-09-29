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


class MemoryStoreError(LocalAgentError):
    """记忆操作被拒绝：作用域无法确定、记忆层未启用、可选依赖缺失等。"""


class ModelSettingsError(LocalAgentError):
    """模型连接设置被拒绝：连接不存在、版本冲突、字段非法、设置库不可用等。"""


class ConnectionValidationError(ModelSettingsError):
    """一条待保存的连接不合法；消息是给用户看的可执行说明。"""


class ConnectionVersionConflict(ModelSettingsError):
    """写入携带的版本号与当前版本不一致（两个页面同时编辑同一条连接）。"""


class SelectionConflict(ModelSettingsError):
    """写入选择时，库里的**当前选择**已不是调用方看到的那一个（两个页面互相覆盖，§6.1）。"""


class CredentialStoreError(ModelSettingsError):
    """系统凭据存储不可用或读写失败（调用方应降级为进程内临时模式并告知用户）。"""


class MemoryDisabledError(MemoryStoreError):
    """配置里该层记忆为关闭状态，调用方应改走透传分支而不是打开存储。"""


class MemoryUnavailableError(MemoryStoreError):
    """该层记忆已启用，但运行环境缺少必需的依赖（例如未安装 ``chromadb``）。"""


class EmbeddingError(LocalAgentError):
    """Embedding 适配层的失败基类（架构 §10.2）。"""


class EmbeddingUnavailableError(EmbeddingError):
    """Embedding 未启用或缺少可选依赖，调用方应改走关闭分支而不是继续。"""


class EmbeddingLoadError(EmbeddingError):
    """模型下载、缓存或推理失败；消息必须带上 ``cache_dir`` 与可执行的下一步。"""
