"""MCP Server 配置的写入口（2026-11-02 从 `service.py` 拆出）。

为什么单独一个模块：`service.py` 有 **4,834 行的硬上限**（`scripts/check_size.py` 的棘轮），
而这段逻辑与它其余部分只共享四个接缝（配置锁、设置库、状态库、连接管理器），
放在这里不比放在那里更难读。

**读写分工**：读（`mcp_view`）仍在 service——它要拼连接状态与工具清单；
写只碰 `mcp.json` + 设置库，搬出来不影响调用方（`Service` 上留同名薄封装）。

**两条与安全相关的行为**（改动时别弄丢）：

1. **换传输要整份重写**：`stdio` 的 ``command``/``args`` 与 ``http`` 的 ``url``/``headers``
   互斥，保留旧字段会让模型校验直接拒绝。
2. **只有"换了是谁"才重置开关**：传输 / 程序 / 端点变了要重新表态（旧信任不能直接执行新命令、
   也不能直接连新远端）；**凭据类改动（env / headers）不重置**——轮换 token 是常态，
   而每次 MCP 调用本来都要弹审批卡。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from local_agent.application import errors as app_errors
from local_agent.application.models import (
    McpServerView,
    McpSummaryView,
    McpToolView,
    McpView,
)
from local_agent.config import ConfigError
from local_agent.mcp.cache import CatalogCache
from local_agent.mcp.config import (
    McpServersFile,
    McpServerSpec,
    check_server_name,
    load_mcp_servers,
    save_mcp_servers,
    spec_to_body,
)
from local_agent.mcp.state import ServerState
from local_agent.model_settings.store import McpServerStore

if TYPE_CHECKING:
    from local_agent.application.service import ApplicationService


def view(service: ApplicationService) -> McpView:
    """配置始终可管理；总开关仅控制连接与模型可见性。"""
    from local_agent.config import ConfigError
    from local_agent.mcp.config import McpServersFile, load_mcp_servers

    with service._mcp_config_lock:
        enabled = bool(service.capability_switches().get("mcp"))
        path = service._mcp_config_path
        config_error = None
        specs = McpServersFile()
        if path is not None:
            try:
                specs = load_mcp_servers(path)
            except ConfigError as exc:
                config_error = str(exc)
        manager = ensure_manager(service)
        snapshot = None if manager is None else manager.snapshot()
        current_states = states(service)
        rows_by_server: dict[str, list[Any]] = {}
        if snapshot is None and path is not None:
            from local_agent.mcp.cache import CatalogCache
            from local_agent.mcp.catalog import ExposureRow, ToolExposure
            cache = CatalogCache(catalog_path(service))
            for name, spec in specs.mcp_servers.items():
                cached = cache.load(name, spec)
                if cached is not None:
                    rows_by_server[name] = [ExposureRow(entry, ToolExposure.HIDDEN, "server_disabled")
                                            for entry in cached.tools]
        views = {} if snapshot is None else {item.name: item for item in snapshot.servers}
        for row in (() if snapshot is None else snapshot.rows):
            rows_by_server.setdefault(row.entry.server, []).append(row)
        servers = []
        for name, spec in specs.mcp_servers.items():
            state = current_states.get(name, ServerState(
                enabled_tools=spec.enabled_tools, disabled_tools=spec.disabled_tools))
            view = views.get(name)
            rows = rows_by_server.get(name, [])
            servers.append(McpServerView(
                name=name, transport=spec.type, command=spec.command or "",
                args=list(spec.args), url=spec.url, header_keys=list(spec.headers),
                env_keys=list(spec.env), enabled=state.enabled,
                enabled_tools=None if state.enabled_tools is None else list(state.enabled_tools),
                disabled_tools=list(state.disabled_tools),
                status=("disabled" if not enabled or view is None else view.status),
                tool_count=len(rows),
                tools=[McpToolView(
                    name=row.entry.canonical_name, tool=row.entry.name,
                    description=row.entry.description, exposure=row.exposure.value,
                    reason=row.reason, disabled=row.entry.name in state.disabled_tools,
                ) for row in rows],
                failure_kind=None if view is None else view.failure_kind,
                failure_message=None if view is None else view.failure_message,
            ))
        return McpView(
            enabled=enabled, config_path=None if path is None else path.as_posix(),
            config_error=config_error, store_unavailable=service._mcp is None,
            servers=servers, summary=McpSummaryView(
                configured=len(servers), enabled=sum(item.enabled for item in servers),
                connected=sum(item.status == "connected" for item in servers),
                failed=sum(item.status == "failed" for item in servers),
                tools=sum(item.tool_count for item in servers),
                direct=sum(tool.exposure == "direct" for item in servers for tool in item.tools),
                deferred=sum(tool.exposure == "deferred" for item in servers for tool in item.tools),
            ),
        )



def add(service: ApplicationService, payload: Mapping[str, Any]) -> McpView:
    return _change(service, str(payload.get("name") or "").strip(), payload, action="add")


def edit(service: ApplicationService, name: str, payload: Mapping[str, Any]) -> McpView:
    return _change(service, name, payload, action="edit")


def delete(service: ApplicationService, name: str) -> McpView:
    return _change(service, name, {}, action="delete")


def _change(
    service: ApplicationService, name: str, payload: Mapping[str, Any], *, action: str
) -> McpView:
    with service._mcp_config_lock:
        store = require_store(service)
        path = require_path(service)
        try:
            check_server_name(name)
            current = load_mcp_servers(path)
            bodies = {key: spec_to_body(value) for key, value in current.mcp_servers.items()}
            if action == "add":
                if name in bodies:
                    raise ValueError(f"已经存在名为 {name} 的 server")
                body = {key: payload[key] for key in
                        ("type", "command", "args", "env", "url", "headers") if key in payload}
            else:
                if name not in bodies:
                    raise ValueError(f"mcp.json 里没有名为 {name} 的 server")
                body = dict(bodies[name])
                if action == "edit":
                    body = _edited_body(body, payload)
            if action == "delete":
                del bodies[name]
            else:
                bodies[name] = spec_to_body(McpServerSpec.model_validate(body))
            merged = McpServersFile.model_validate({"mcpServers": bodies})
            save_mcp_servers(path, merged)
        except ValidationError as exc:
            # str(ValidationError) 会附带 input_value，其中可能是整个 env。
            detail = "；".join(".".join(map(str, e["loc"])) + "：字段不合法"
                              for e in exc.errors())
            raise app_errors.model_settings_invalid(detail=detail, occurred_at=_now()) from exc
        except (ValueError, ConfigError) as exc:
            raise app_errors.model_settings_invalid(detail=str(exc), occurred_at=_now()) from exc
        except OSError as exc:
            raise app_errors.model_settings_invalid(
                detail=f"写入 {path.name} 失败：{exc.strerror}", occurred_at=_now()) from exc
        if action in ("delete", "add"):
            service._sqlite_access(partial(store.forget, name))
        elif action == "edit":
            _reset_switch_if_identity_changed(service, store, name,
                                              current.mcp_servers[name], merged.mcp_servers[name])
        if action == "delete":
            CatalogCache(catalog_path(service)).forget(name)
        manager = ensure_manager(service)
        if manager is not None:
            manager.replace_servers(merged)
    return service.mcp_view()


def set_server(service: ApplicationService, name: str, changes: Mapping[str, Any]) -> McpView:
    from pydantic import ValidationError

    from local_agent.mcp.config import McpServerSpec, load_mcp_servers

    with service._mcp_config_lock:
        store = require_store(service)
        path = require_path(service)
        try:
            specs = load_mcp_servers(path)
            spec = specs.mcp_servers.get(name)
            if spec is None:
                raise ValueError(f"mcp.json 里没有名为 {name} 的 server")
            current = states(service).get(name, ServerState(
                enabled_tools=spec.enabled_tools, disabled_tools=spec.disabled_tools))
            if "enabled" in changes and not isinstance(changes["enabled"], bool):
                raise ValueError("enabled 必须是布尔值")
            if "disabled_tools" in changes and changes["disabled_tools"] is None:
                raise ValueError("disabled_tools 必须是工具名数组")
            lists = McpServerSpec.model_validate({
                "command": "validation-only",
                "enabled_tools": changes.get("enabled_tools", current.enabled_tools),
                "disabled_tools": changes.get("disabled_tools", current.disabled_tools),
            })
        except ValidationError as exc:
            raise app_errors.model_settings_invalid(
                detail="工具名单不合法：" + "; ".join(e["msg"] for e in exc.errors()),
                occurred_at=_now()) from exc
        except (ValueError, ConfigError) as exc:
            raise app_errors.model_settings_invalid(detail=str(exc), occurred_at=_now()) from exc
        service._sqlite_access(partial(store.put, name, ServerState(
            enabled=changes.get("enabled", current.enabled),
            enabled_tools=lists.enabled_tools, disabled_tools=lists.disabled_tools)))
        if service._mcp_manager is not None:
            service._mcp_manager.warm()
    return service.mcp_view()



def reload(service: ApplicationService) -> McpView:
    from local_agent.mcp.config import load_mcp_servers

    with service._mcp_config_lock:
        try:
            servers = load_mcp_servers(require_path(service))
        except ConfigError as exc:
            raise app_errors.model_settings_invalid(detail=str(exc), occurred_at=_now()) from exc
        if service._mcp is not None:
            for name in states(service):
                if name not in servers.mcp_servers:
                    service._sqlite_access(partial(service._mcp.forget, name))
        manager = ensure_manager(service)
        if manager is not None:
            manager.replace_servers(servers, refresh=True)
    return service.mcp_view()

# 三个写入口的实现搬到 `application/mcp_admin.py`（2026-11-02，本文件有行数硬上限）：
# 它们只碰 `mcp.json` 与设置库，不参与读路径的状态拼装。


def close(service: ApplicationService) -> None:
    with service._mcp_config_lock:
        manager = service._mcp_manager
        service._mcp_manager = None
        if manager is not None:
            manager.close()



def known_servers(service: ApplicationService) -> tuple[str, ...]:
    """``mcp.json`` 里声明的 server 名（读不到就是空）。"""
    from local_agent.config import ConfigError
    from local_agent.mcp.config import load_mcp_servers

    path = service._mcp_config_path
    if path is None:
        return ()
    try:
        return load_mcp_servers(path).names()
    except ConfigError:
        return ()



def catalog_path(service: ApplicationService) -> Path:
    """MCP 工具目录缓存的位置（2026-10-26）。

    与 ``mcp.json`` 刻意分开：配置是**用户手写的文件**，跟 ``config.yaml`` 同目录；而这份缓存
    是应用自己产生的状态（可删、可重建、会随 server 变化而失效），跟数据根走。

    之前三处各写一次 ``path.parent / "data" / "mcp_catalog.sqlite"``，且它隐含"mcp.json 与
    ``data/`` 同目录"这个巧合——两处任一搬家，缓存就会静默落到别处。
    """
    from local_agent import app_paths

    return app_paths.data_root_of(service._app_config) / "mcp_catalog.sqlite"



def ensure_manager(service: ApplicationService) -> Any:
    """按需建连接管理器：**开关打开的那一刻**才付建连与导入 SDK 的代价。

    为什么必须懒建：``mcp`` 是能力开关里的一项，用户点开之后"保存即生效、作用于下一轮"是既有
    承诺。若管理器只在进程启动时按当时的开关决定，点开开关就得重启——那是"看起来改了、实际
    没生效"，正是能力面板要消灭的那类问题。

    代价只在第一次发生；之后 ``_mcp_manager`` 一直复用，进程退出时由容器关掉。
    """
    if not service.capability_switches().get("mcp"):
        return None
    if service._mcp_manager is not None:
        return service._mcp_manager
    path = service._mcp_config_path
    if path is None:
        return None
    from local_agent.config import ConfigError
    from local_agent.mcp.config import load_mcp_servers
    from local_agent.mcp.manager import McpManager

    with service._mcp_config_lock:
        if service._mcp_manager is not None:
            return service._mcp_manager
        try:
            servers = load_mcp_servers(path)
        except ConfigError:
            # 配置坏了就不建：错误由 `mcp_view().config_error` 展示给人看。
            return None
        manager = McpManager(
            servers,
            config=service._app_config.mcp,
            state_provider=lambda: states(service),
            cache_path=catalog_path(service),
        )
        manager.start()
        service._mcp_manager = manager
        return manager



def states(service: ApplicationService) -> Mapping[str, ServerState]:
    """每台 MCP Server 的开关与工具名单；没有设置库时返回空（= 全部按关）。"""
    store = service._mcp
    if store is None:
        return {}
    return service._sqlite_access(store.states)



def require_store(service: ApplicationService) -> McpServerStore:
    if service._mcp is None:
        raise app_errors.settings_store_unavailable(
            detail="设置库没有打开", occurred_at=_now()
        )
    return service._mcp

# ---- 联网检索的服务凭据（Tavily / DeepSeek）------------------------------
# 数值型偏好（条数上限、域名策略…）走 `RuntimePreferences.web_access`；这里只管**密钥**一件事。
# 密钥与模型连接同一条口径（§6.8）：只进设置库或进程内存，读取接口只报"有没有"。
# 2026-10-26：两个 API 后端各占一条密钥，页面按当前选中的后端读写对应那条——换后端不覆盖另一个。



def require_path(service: ApplicationService) -> Path:
    if service._mcp_config_path is None:
        raise app_errors.model_settings_invalid(
            detail="当前运行没有 MCP 配置路径（未接线）。", occurred_at=_now())
    return service._mcp_config_path



def _edited_body(body: dict[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    """把一次编辑应用到已有的 server 定义上（换传输时整份重来）。"""
    if "type" in payload and payload["type"] != body.get("type"):
        body = {"type": payload["type"]}
    for key in ("command", "args", "url"):
        if key in payload:
            if payload[key] is None:
                raise ValueError(f"{key} 不能为 null")
            body[key] = payload[key]
    env = dict(body.get("env", {}))
    env.update(payload.get("env") or {})
    for key in payload.get("remove_env", ()):
        env.pop(key, None)
    body["env"] = env
    headers = dict(body.get("headers", {}))
    headers.update(payload.get("headers") or {})
    for key in payload.get("remove_headers", ()):
        headers.pop(key, None)
    body["headers"] = headers
    return body


def _reset_switch_if_identity_changed(
    service: ApplicationService, store: Any, name: str, old_spec: McpServerSpec,
    new_spec: McpServerSpec,
) -> None:
    """传输 / 程序 / 端点变了 → 关掉开关（凭据改动不重置）。"""
    if (old_spec.type, old_spec.command, tuple(old_spec.args), old_spec.url) == (
        new_spec.type, new_spec.command, tuple(new_spec.args), new_spec.url,
    ):
        return
    state = states(service).get(
        name,
        ServerState(enabled_tools=old_spec.enabled_tools, disabled_tools=old_spec.disabled_tools),
    )
    service._sqlite_access(partial(
        store.put,
        name,
        ServerState(enabled=False, enabled_tools=state.enabled_tools,
                    disabled_tools=state.disabled_tools),
    ))


def _now() -> datetime:
    return datetime.now(timezone.utc)
