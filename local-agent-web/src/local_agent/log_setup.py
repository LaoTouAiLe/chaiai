"""把日志落到数据根下的文件（2026-11-06）。

**为什么需要**：服务默认把日志写到 stdout。双击 exe 的人没有终端，`--no-open-browser` 在后台跑
的人也看不到 stderr——报障时手里一条记录都没有。落到 ``<data_root>/logs/app.log``（5 MB × 3 个
滚动文件）之后，"服务在我这儿起不来"至少能附上一段真实输出。

**为什么连 uvicorn 那三个 logger 一起挂**：``uvicorn`` / ``uvicorn.error`` / ``uvicorn.access``
都设了 ``propagate = False``，只挂 root 的话启动过程与访问日志一条都进不了文件，而那正是排障最想
要的部分。挂同一个 handler 实例即可，不会重复写。
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

#: 单个日志文件的字节上限。
MAX_BYTES = 5 * 1024 * 1024

#: 滚动保留的旧文件个数（``app.log.1`` ~ ``app.log.3``）。
BACKUP_COUNT = 3

#: 文件名。诊断与启动横幅都从这里取，避免两处写死不一致。
LOG_NAME = "app.log"

_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

#: uvicorn 自己建的 logger。它们的 ``propagate`` 是 False，必须单独挂。
_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


def log_path_of(log_dir: Path) -> Path:
    """日志文件路径（不创建目录）。诊断只用它报告位置。"""
    return log_dir / LOG_NAME


def _normalized(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _same_file(handler: logging.Handler, target: str) -> bool:
    """这个 handler 是否已经在写同一个文件。"""
    if not isinstance(handler, RotatingFileHandler):
        return False
    return _normalized(handler.baseFilename) == target


def configure_logging(log_dir: Path, *, level: int = logging.INFO) -> Path:
    """给 root 与 uvicorn 的 logger 挂上滚动文件，返回日志文件路径。

    幂等：同一个目录反复调用只会有一个 handler（否则每行日志会被写多遍）。
    目录不存在会被创建；创建失败往上抛 ``OSError``，由调用方决定是否降级继续启动。
    """
    path = log_path_of(log_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    target = _normalized(str(path))

    handler: logging.Handler | None = None
    for logger in [logging.getLogger(), *(logging.getLogger(name) for name in _UVICORN_LOGGERS)]:
        for existing in logger.handlers:
            if _same_file(existing, target):
                handler = existing
                break
        if handler is not None:
            break
    if handler is None:
        handler = RotatingFileHandler(
            path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter(_FORMAT))

    for name in (None, *_UVICORN_LOGGERS):
        logger = logging.getLogger() if name is None else logging.getLogger(name)
        if not any(_same_file(item, target) for item in logger.handlers):
            logger.addHandler(handler)
        if logger.getEffectiveLevel() > level:
            logger.setLevel(level)
    return path
