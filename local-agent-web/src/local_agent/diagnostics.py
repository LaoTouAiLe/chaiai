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


#: 数据根来源的中文名（与 `/api/storage` 的 `source` 同源）。
_SOURCE_LABEL: dict[str, str] = {
    "environment": "环境变量 CHAIAI_HOME",
    "settings": "设置页保存的指针文件",
    "config": "config.yaml 的 data_root",
    "default": "平台标准目录",
}


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
    # 库在**数据根**下（2026-10-26），与 `open_runtime` 同一套解析：诊断要是看错位置，
    # 用户会得到"数据库不存在"这种与事实相反的结论。
    data_root = app_paths.data_root_of(config)
    # 「数据在哪」是排障第一个要回答的问题（2026-10-26）。此前只有启动横幅会打印它，
    # `--diagnose` 反而不说——而它恰恰是"服务起不来时"唯一还能跑的命令。
    # 变量名不能叫 `source`：下面的数据库循环里 `source` 是解析后的 `Path`，
    # 同名会让两处语义糊在一起（mypy 直接报"expression has type Path, variable has type str"）。
    root_source = app_paths.data_root_source(config)
    add("data_root", "ok", f"数据根：{data_root}（来源：{_SOURCE_LABEL.get(root_source, root_source)}）")
    # 旧布局（2026-10-26 之前库在 `<某处>/data/` 下）要分两种说，**不能一律沉默**：
    #
    # - 有配置路径仍落在里面 → 那是"还在用旧布局"，而且是**混用**（另一部分已经到根级了）。
    #   这种状态最坑：用户看到"会话是旧的、设置是新的"，却没有任何提示。
    # - 没有任何配置路径落在里面 → 纯残留，可以告知"当前版本不读它"。
    #
    # 早先的写法是"有一个路径带 data/ 前缀就整体不报"，结果把上面那种混用整个藏住了
    # （实测：本机会话库在 `data/` 下、设置库在根级，诊断却只字不提）。
    configured = [(str(value), _under(data_root, value).resolve()) for _, value, _ in databases]
    stale_dirs = {path.resolve() for path in (data_root / "data", base / "data") if path.is_dir()}
    stale_dirs.discard(data_root.resolve())
    for stale in sorted(stale_dirs):
        # 列**配置原文**而不是解析后的绝对路径：用户要改的就是配置里那一行。
        inside = [raw for raw, path in configured if stale in path.parents]
        if inside:
            add("legacy_data", "warning",
                f"部分数据仍落在旧布局 {stale} 下（{'、'.join(inside)}）；它与根级数据混用，"
                f"建议把这些配置路径改成不带 data/ 前缀，重启后统一到 {data_root}。")
        else:
            add("legacy_data", "warning",
                f"检测到旧布局目录：{stale}。当前版本不读它；需要的话手工搬内容到 {data_root}。")
    for name, value, expected in databases:
        source = _under(data_root, value)
        if not source.exists():
            # 带上**具体文件**：换了数据根之后，"数据库不存在"最需要回答的是"哪个路径不存在"。
            add(name, "warning", f"数据库不存在：{source}；首次启动创建，本次诊断未创建。")
            continue
        try:
            with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
                verify_connection(db)
                version = current_version(db)
            if version > expected:
                add(name, "error", f"数据库结构比当前程序新：{source}；使用匹配版本或恢复升级前备份，不直接降级。")
            elif version < expected:
                add(name, "warning", f"数据库需要迁移：{source}；先停服务备份，在副本验证升级后再使用。")
            else:
                # 成功也要给路径（2026-10-26）：同一台机器上可能同时存在新旧两套库
                # （根级 vs `data/` 子目录），不说是哪个文件就读不出自己在用哪一套。
                add(name, "ok", f"数据库自检与结构版本通过：{source}；不代表业务记录恢复已验证。")
        except (OSError, sqlite3.Error, ValueError):
            add(name, "error", f"数据库不可读、自检失败或被锁定：{source}；停服务保留原库，检查备份与恢复指南。")
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
