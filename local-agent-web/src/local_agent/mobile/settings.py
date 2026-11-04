"""移动访问配置的生效值（2026-11-03；架构 §24）。

**为什么网关不直接读设置库**：§24 有一条边界——"网关不写任何会话、记忆、设置库数据"。读也按同一条
办更省事：网关只认 ``config.yaml`` + 一份由主服务写出的**快照**（``data_root/mobile/effective.json``），
不需要懂 sqlite、不会因为主服务正在写库而卡住、也不会在库被删掉时莫名其妙地起不来。

**为什么快照由主服务写而不是网关自己维护**：配置的唯一编辑入口是设置页（主服务），网关是消费方。
把"写"留在主服务，就不会出现两个进程各写一半、谁也说不清当前配置是什么的局面。

**快照缺失时用 config.yaml**：只跑网关（没起主服务）的人仍然能用配置文件，行为与引入快照之前一致。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

from local_agent import app_paths
from local_agent.config import AppConfig, MobileConfig

#: 快照文件名，落在 ``data_root/mobile/`` 下（与配对记录、证书同目录）。
SNAPSHOT_NAME: Final[str] = "effective.json"

#: 移动访问状态目录，与 `local_agent.mobile.__main__` 同一口径。
STATE_DIR_NAME: Final[str] = "mobile"


def snapshot_path(data_root: Path) -> Path:
    return data_root / STATE_DIR_NAME / SNAPSHOT_NAME


def write_snapshot(config: AppConfig, mobile: MobileConfig) -> Path:
    """把生效的 ``mobile`` 配置写进快照；返回路径。

    写失败**不抛**：配置已经在设置库里存好了，快照只是给网关的传递通道。为一次失败的写，
    让「保存」在页面上报错，会让用户以为配置没存上——那是错的。
    """
    path = snapshot_path(app_paths.data_root_of(config))
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = mobile.model_dump()
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    # 原子替换：网关可能正好在读，读到半截 JSON 会被当成"没有快照"而退回 config.yaml，
    # 表现为"刚保存的配置偶尔不生效"。replace 在同一分区上是原子的。
    temp.replace(path)
    return path


def read_snapshot(data_root: Path) -> dict[str, Any] | None:
    """读快照；缺失或读不出来返回 ``None``（调用方退回 config.yaml）。"""
    path = snapshot_path(data_root)
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def effective_mobile(config: AppConfig) -> MobileConfig:
    """``config.yaml`` 的 mobile 段 + 快照覆盖。

    合并而不是替换：快照里可能缺新增的字段（老版本写的），缺的那部分继续用配置文件的默认值。
    """
    stored = read_snapshot(app_paths.data_root_of(config))
    if not stored:
        return config.mobile
    try:
        return MobileConfig.model_validate({**config.mobile.model_dump(), **stored})
    except ValueError:
        # 快照坏了就退回配置文件：一条坏记录不该让网关起不来。
        return config.mobile


__all__ = ["effective_mobile", "read_snapshot", "snapshot_path", "write_snapshot"]
