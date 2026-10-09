"""装配：配置 + 模型 + 库/图 + 应用服务 = 一套 `ApiRuntime`（T29）。

入口（``python -m local_agent.api``）与测试**共用这里**，所以不存在第二套装配逻辑。

单进程、单 worker（§15.1）不是靠文档要求，而是靠**排他文件锁**强制：第二个进程/worker 启动
就会失败并说明原因。理由不是洁癖——进程内广播、每会话互斥与幂等台账都以为"只有一个进程在写
这个库"；两个进程同时跑，同一会话的两次执行可能都认为自己拿到了锁。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from filelock import FileLock, Timeout
from langchain_core.language_models import BaseChatModel

from local_agent import config as config_module
from local_agent.application.service import ApplicationService
from local_agent.config import AppConfig, ConfigError, LlmConfig
from local_agent.graph import builder
from local_agent.llm import factory
from local_agent.model_settings import (
    SettingsDatabase,
    import_legacy_llm,
    open_credential_store,
    open_settings_database,
)
from local_agent.persistence import checkpoint as checkpoint_module
from local_agent.persistence.checkpoint import ChatDatabase
from local_agent.persistence.request_ledger import RequestLedger

#: 单实例锁的后缀：与聊天库同目录，既不在库内也不进 Git。
INSTANCE_LOCK_SUFFIX = ".api.lock"


@dataclass
class ApiRuntime:
    """一套可服务的运行时；由入口持有到进程退出。

    `lock` 的生命周期必须覆盖整个进程：它被释放（或被 GC 回收）就意味着"可以再起一个服务"。
    """

    service: ApplicationService
    database: ChatDatabase
    #: 调用方注入的模型客户端（诊断与测试要看它，**不进任何 DTO**）。
    #: `None` 表示"这个进程没有注入模型"：运行期按连接构造客户端（§6.8）。
    model: BaseChatModel | None
    web: config_module.WebConfig
    static_dir: Path | None
    model_name: str
    #: 模型连接设置库；`None` = 打不开（已经降级为"按旧配置运行"，页面会看到原因）。
    settings: SettingsDatabase | None = None
    lock: FileLock | None = field(default=None, repr=False)

    def close(self) -> None:
        """释放单实例锁并关闭数据库连接（测试与一次性脚本用；服务进程不需要调用）。"""
        if self.lock is not None:
            self.lock.release()
            self.lock = None
        self.database.connection.close()
        if self.settings is not None:
            self.settings.connection.close()


def instance_lock_path(database_path: Path) -> Path:
    """单实例锁文件的位置（与聊天库同目录）。"""
    return Path(database_path).with_name(Path(database_path).name + INSTANCE_LOCK_SUFFIX)


def acquire_instance_lock(database_path: Path) -> FileLock:
    """排他地占住"这个库只有一个服务进程"（§15.1：多 worker 属于不受支持配置）。

    Raises:
        RuntimeError: 已有进程占着同一把锁（第二个 worker 或重复启动）。
    """
    path = instance_lock_path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(path), timeout=0)
    try:
        lock.acquire()
    except Timeout as exc:
        msg = (
            f"已经有一个 local-agent-web 服务在跑（{path.name} 被占用）："
            "v0.4 只支持单进程单 worker，请先停掉它，或换一个 memory.short_term.path。"
        )
        raise RuntimeError(msg) from exc
    return lock


def resolve_under(base_dir: Path, value: str | Path) -> Path:
    """把配置里的相对路径按**配置文件所在目录**展开（与进程当前目录无关）。"""
    candidate = Path(value)
    return candidate if candidate.is_absolute() else (Path(base_dir) / candidate)


def resolve_memory_paths(config: AppConfig, base_dir: Path) -> AppConfig:
    """把三层记忆库的路径也钉到 ``base_dir`` 上（架构 §8）。

    为什么这三条要单独收口：它们的消费方是**图内节点**（`graph/nodes.py` 直接拿
    ``context.app_config.memory.*.path`` 去 ``open_store``），节点拿不到 base_dir，于是它们成了
    全项目唯一"按进程当前目录解析"的路径。2026-10-09 实测后果：换一个目录启动，聊天库还在原地
    （它在这里解析过），**长期记忆却换成了另一个库**——正是 T40 的"解压双击"场景。

    语义与 :func:`resolve_under` 逐字一致（绝对路径照用、相对路径拼在基准目录上、不展开 ``~``）。
    其余键仍由各自的消费方调用 :func:`resolve_under`，本次不改。
    """
    memory = config.memory
    resolved = memory.model_copy(
        update={
            "short_term": memory.short_term.model_copy(
                update={"path": str(resolve_under(base_dir, memory.short_term.path))}
            ),
            "medium_term": memory.medium_term.model_copy(
                update={"path": str(resolve_under(base_dir, memory.medium_term.path))}
            ),
            "long_term": memory.long_term.model_copy(
                update={"path": str(resolve_under(base_dir, memory.long_term.path))}
            ),
        }
    )
    return config.model_copy(update={"memory": resolved})


def open_runtime(
    app_config: AppConfig,
    *,
    base_dir: Path,
    model: BaseChatModel | None = None,
    model_factory: Callable[[LlmConfig, str], BaseChatModel] | None = None,
    secrets: Iterable[str] = (),
    static_dir: Path | None = None,
    instance_lock: bool = True,
) -> ApiRuntime:
    """打开聊天库与设置库、装好图与应用服务。

    `model` 由调用方注入（测试、评测、脚本）：**注入的模型代表这个进程"构造客户端"的能力**，
    运行期按连接构造时就返回它——这样注入假模型的测试仍然覆盖"按提交边界构造"这条路径。
    入口不再预构造客户端（§15.9：完成首个有效连接后立即建立，不需要伪造 YAML 模型段）。

    本函数**不读** `.env`、不构造真实客户端、不联网——装配只做装配的事。

    Args:
        app_config: 已校验的配置。
        base_dir: 相对路径的解析基准（配置文件所在目录）。
        model: 注入的聊天模型客户端；``None`` 表示运行期按连接构造。
        model_factory: 显式指定"按连接构造客户端"的可调用对象；省略时按 `model` 决定。
        secrets: 仅用于展示脱敏的密钥值。
        static_dir: 显式指定前端产物目录；缺省按 `web.static_dir` 解析。
        instance_lock: 是否占住单实例锁（测试若要并发建多套运行时可关掉）。

    Returns:
        可直接交给 :func:`local_agent.api.app.create_app` 的运行时。

    Raises:
        ConfigError: 未启用短期记忆（会话与幂等台账都在那个库里）。
        RuntimeError: 已有另一个服务进程占着同一个库。
    """
    if not app_config.memory.short_term.enabled:
        msg = (
            "v0.4 的 Web 服务需要 memory.short_term.enabled=true："
            "会话索引、Checkpoint 与幂等台账都在那个库里，没有它就没有会话可服务"
        )
        raise ConfigError(msg)

    database_path = resolve_under(base_dir, app_config.memory.short_term.path)
    lock = acquire_instance_lock(database_path) if instance_lock else None
    settings: SettingsDatabase | None = None
    try:
        workspace_root = resolve_under(base_dir, app_config.workspace.root)
        # 图内节点直接按配置里的路径开记忆库，所以在这里先钉到 base_dir 上（§8）。
        app_config = resolve_memory_paths(app_config, base_dir)
        database = checkpoint_module.open_database(database_path, workspace_root=workspace_root)
        settings = _open_settings(app_config, base_dir=base_dir)
        service = ApplicationService(
            graph=builder.build_chat_graph(database.checkpointer),
            app_config=app_config,
            model_adapter=model,
            model_factory=_factory_for(model=model, model_factory=model_factory),
            workspace_root=workspace_root,
            sessions=database.sessions,
            ledger=RequestLedger(database.connection),
            # 没显式给脱敏密钥时，从设置库里取（含旧配置导入的那条的环境变量路径）。
            secrets=tuple(secrets) or _settings_secrets(settings),
            connections=None if settings is None else settings.connections,
            permissions=None if settings is None else settings.permissions,
            preferences=None if settings is None else settings.preferences,
        )
    except BaseException:
        # 装配失败不能留下"锁被一个不存在的服务占着"的状态。
        if lock is not None:
            lock.release()
        if settings is not None:
            settings.connection.close()
        raise

    resolved_static = (
        resolve_under(base_dir, app_config.web.static_dir) if static_dir is None else Path(static_dir)
    )
    return ApiRuntime(
        service=service,
        database=database,
        model=model,
        web=app_config.web,
        static_dir=resolved_static if resolved_static.is_dir() else None,
        # 旧配置里的模型名（初始设置模式下没有它，报告为空字符串）。
        model_name="" if app_config.llm is None else app_config.llm.model,
        settings=settings,
        lock=lock,
    )


def _open_settings(app_config: AppConfig, *, base_dir: Path) -> SettingsDatabase | None:
    """打开模型连接设置库并做一次幂等的旧配置导入；打不开就返回 ``None``（降级）。

    打不开**不是**致命错误（§6.8）：服务继续按 `config.yaml` 运行，页面会看到"设置不可用"的说明。
    但导入失败要暴露：那说明旧配置与设置库不一致，静默吞掉会让用户以为迁移完成了。
    """
    path = resolve_under(base_dir, app_config.model_settings.path)
    credentials = open_credential_store(app_config.model_settings.credential_store)
    try:
        settings = open_settings_database(
            path,
            credentials=credentials,
            # config.yaml 里的档位只是**出厂默认**：库里没有用户选择时用它（T38）。
            default_external_paths_mode=app_config.workspace.external_paths.mode,
        )
    except (sqlite3.Error, OSError):
        return None
    import_legacy_llm(settings.connections, app_config.llm)
    return settings


def _settings_secrets(settings: SettingsDatabase | None) -> tuple[str, ...]:
    """从设置库收集要脱敏的密钥（只用于抹除，不进任何 DTO）。"""
    if settings is None:
        return ()
    return tuple(settings.connections.secret_values())


def _factory_for(
    *,
    model: BaseChatModel | None,
    model_factory: Callable[[LlmConfig, str], BaseChatModel] | None,
) -> Callable[[LlmConfig, str], BaseChatModel]:
    """决定"按连接构造客户端"用哪个构造器。

    优先级：显式 ``model_factory`` → 注入的 ``model``（固定返回它）→ 真实构造函数。
    """
    if model_factory is not None:
        return model_factory
    if model is not None:
        return lambda _config, _api_key: model
    return factory.build_chat_model
