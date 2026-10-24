"""无服务器会话状态的检索位置；游标不赋予任何路径权限。"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from local_agent.security.policy import SecurityPolicy


def query_key(*values: object) -> str:
    """绑定检索根及查询语义，预算可在后续页面调整。"""
    return hashlib.sha256(json.dumps(values, ensure_ascii=True).encode()).hexdigest()


def decode_cursor(cursor: str | None, key: str) -> dict[str, Any] | None:
    """校验查询身份与位置格式；不把输入路径直接用于文件访问。"""
    if cursor is None:
        return None
    try:
        if len(cursor) > 32768:
            raise ValueError
        value = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        if (not isinstance(value, dict) or value.get("key") != key
                or not isinstance(value.get("file"), str)
                or type(value.get("line")) is not int or value["line"] < 1
                or not isinstance(value.get("version"), list)):
            raise ValueError
    except (ValueError, UnicodeError, TypeError) as exc:
        raise ValueError("游标无效或不属于本次查询，请从头检索") from exc
    return value


def encode_cursor(key: str, path: Path, line: int, policy: SecurityPolicy) -> str:
    """记录下一文件/行的位置与文件元数据版本。"""
    info = policy.files.stat(path)
    value = {"key": key, "file": path.as_posix(), "line": line,
             "version": [info.st_size, info.st_mtime_ns]}
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode()


def resume_files(
    files: Iterator[Path], cursor: dict[str, Any] | None, policy: SecurityPolicy,
) -> Iterator[tuple[Path, int]]:
    """在策略允许的候选中定位游标；文件变化或位置消失明确拒绝续查。"""
    found = cursor is None
    for path in files:
        policy.files.check_cancelled()
        if not found:
            assert cursor is not None
            if path.as_posix() != cursor["file"]:
                continue
            info = policy.files.stat(path)
            if [info.st_size, info.st_mtime_ns] != cursor["version"]:
                raise ValueError("续查文件已变化，请从头检索")
            found = True
            yield path, cursor["line"]
        else:
            yield path, 1
    if not found:
        raise ValueError("续查位置已消失或被忽略，请从头检索")
