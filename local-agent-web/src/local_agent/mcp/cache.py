"""工具目录缓存：独立短连接，不与设置库的跨线程事务共用连接。"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from local_agent.mcp.catalog import ServerCatalog, build_server_catalog
from local_agent.mcp.config import McpServerSpec, spec_to_body

_LOG = logging.getLogger(__name__)


def spec_fingerprint(spec: McpServerSpec) -> str:
    payload = json.dumps(spec_to_body(spec), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


class CatalogCache:
    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def _open(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        try:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS mcp_catalog ("
                "server TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, tools TEXT NOT NULL)"
            )
            with connection:
                yield connection
        finally:
            connection.close()

    def load(self, name: str, spec: McpServerSpec) -> ServerCatalog | None:
        if not self.path.is_file():
            return None
        try:
            with self._open() as connection:
                row = connection.execute(
                    "SELECT tools FROM mcp_catalog WHERE server = ? AND fingerprint = ?",
                    (name, spec_fingerprint(spec)),
                ).fetchone()
            if row is None:
                return None
            raw = json.loads(row[0])
            if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
                return None
            return build_server_catalog(name, raw)
        except (sqlite3.Error, OSError, ValueError, TypeError):
            _LOG.warning("MCP 目录缓存不可读：%s", name)
            return None

    def save(self, catalog: ServerCatalog, spec: McpServerSpec) -> None:
        tools = [
            {"name": tool.name, "description": tool.description, "inputSchema": dict(tool.input_schema)}
            for tool in catalog.tools
        ]
        try:
            with self._open() as connection:
                connection.execute(
                    "INSERT OR REPLACE INTO mcp_catalog VALUES (?, ?, ?)",
                    (catalog.server, spec_fingerprint(spec), json.dumps(tools, ensure_ascii=False)),
                )
        except (sqlite3.Error, OSError, ValueError, TypeError):
            _LOG.warning("MCP 目录缓存未保存：%s", catalog.server)

    def forget(self, name: str) -> None:
        if not self.path.is_file():
            return
        try:
            with self._open() as connection:
                connection.execute("DELETE FROM mcp_catalog WHERE server = ?", (name,))
        except (sqlite3.Error, OSError):
            _LOG.warning("MCP 目录缓存未清理：%s", name)
