"""联网工具共用错误；策略拒绝与网络/内容错误分开记录。"""

from local_agent.exceptions import SecurityError


class WebAccessError(Exception):
    """网络或内容处理失败，消息可作为模型可读的工具结果。"""


class WebAccessDenied(WebAccessError, SecurityError):
    """仅表示安全策略拒绝。"""
