"""T87：显式本机诊断；不装配运行时，不读取凭据或调用模型。"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any

from local_agent import app_paths
from local_agent.config import AppConfig, ConfigError, load_config
from local_agent.persistence.database_maintenance import verify_connection
from local_agent.persistence.migrations import (
    CHAT_SCHEMA_VERSION,
    LONG_TERM_SCHEMA_VERSION,
    SETTINGS_SCHEMA_VERSION,
    current_version,
)


def _under(base: Path, path: str) -> Path:
    value = Path(path)
    return value if value.is_absolute() else base / value


def diagnose(config_path: Path) -> dict[str, Any]:
    """消息是固定提示，配置异常中的输入值与供应商信息均不回显。"""
    checks: list[dict[str, str]] = []

    def add(name: str, status: str, message: str) -> None:
        checks.append({"name": name, "status": status, "message": message})

    try:
        exists = config_path.is_file()
        config = load_config(config_path, allow_missing=True)
        add("config", "ok" if exists else "warning",
            "YAML 配置校验通过。" if exists else "未提供 YAML；启动进入初始设置模式。")
    except (ConfigError, OSError, UnicodeError):
        add("config", "error", "配置无法读取或校验失败；对照 config.yaml.example 检查编码与字段。")
        return {"ok": False, "checks": checks}
    _inspect(config, config_path.resolve().parent, add)
    return {"ok": all(item["status"] != "error" for item in checks), "checks": checks}


def _inspect(config: AppConfig, base: Path, add: Callable[[str, str, str], None]) -> None:
    static = app_paths.bundled_static_dir() or _under(base, config.web.static_dir)
    add("frontend", "ok" if (static / "index.html").is_file() else "error",
        "前端产物存在。" if (static / "index.html").is_file()
        else "缺少前端 index.html；源码在 frontend/ 执行 npm ci 与 npm run build；分发包重新核对清单。")
    databases = (
        ("chat_database", config.memory.short_term.path, CHAT_SCHEMA_VERSION),
        ("long_term_database", config.memory.long_term.path, LONG_TERM_SCHEMA_VERSION),
        ("settings_database", config.model_settings.path, SETTINGS_SCHEMA_VERSION),
    )
    for name, value, expected in databases:
        source = _under(base, value)
        if not source.exists():
            add(name, "warning", "数据库不存在；首次启动创建，本次诊断未创建。")
            continue
        try:
            with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
                verify_connection(db)
                version = current_version(db)
            if version > expected:
                add(name, "error", "数据库结构比当前程序新；使用匹配版本或恢复升级前备份，不直接降级。")
            elif version < expected:
                add(name, "warning", "数据库需要迁移；先停服务备份，在副本验证升级后再使用。")
            else:
                add(name, "ok", "数据库自检与结构版本通过；不代表业务记录恢复已验证。")
        except (OSError, sqlite3.Error, ValueError):
            add(name, "error", "数据库不可读、自检失败或被锁定；停服务保留原库，检查备份与恢复指南。")
    for module in ("chromadb", "fastembed"):
        try:
            available = importlib.util.find_spec(module) is not None
        except (ImportError, ValueError):
            available = False
        add(module, "ok" if available else "warning",
            "依赖可见，尚未加载或验证模型。" if available
            else "可选记忆依赖缺失；需要此能力时按 README 安装 memory extra。")
    for executable in ("git", "rg"):
        # 包内的 rg 不在 PATH 上：判定必须与运行期解析顺序一致（包内优先）。
        available = app_paths.bundled_binary(executable) is not None or shutil.which(executable) is not None
        add(executable, "ok" if available else "warning",
            "工具可见，尚未执行。" if available else "工具缺失；rg 可退化扫描，Git 能力需安装 git。")
    add("model", "warning", "未检测模型、系统凭据或远端接口；实际连接在网页设置中显式测试。")
    add("scope", "warning", "仅检查 YAML 与环境；不读取网页配置覆盖，不证明沙箱或真实回答质量。")


def print_diagnosis(config_path: Path) -> int:
    report = diagnose(config_path)
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0 if report["ok"] else 1
