"""装配：配置 + 模型 + 库/图 + 应用服务 = 一套 `ApiRuntime`（T29）。

入口（``python -m local_agent.api``）与测试**共用这里**，所以不存在第二套装配逻辑。

单进程、单 worker（§15.1）不是靠文档要求，而是靠**排他文件锁**强制：第二个进程/worker 启动
就会失败并说明原因。理由不是洁癖——进程内广播、每会话互斥与幂等台账都以为"只有一个进程在写
这个库"；两个进程同时跑，同一会话的两次执行可能都认为自己拿到了锁。
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Iterable
from contextlib import nullcontext
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout
from langchain_core.language_models import BaseChatModel

from local_agent import app_paths
from local_agent import config as config_module
from local_agent.application.jobs import JobRuntime
from local_agent.application.service import ApplicationService
from local_agent.attachments import AttachmentStore
from local_agent.capabilities import switchable_capabilities
from local_agent.config import AppConfig, ConfigError, LlmConfig
from local_agent.graph import builder
from local_agent.llm import factory
from local_agent.mcp.config import DEFAULT_MCP_PATH
from local_agent.model_settings import (
    SettingsDatabase,
    import_legacy_llm,
    open_credential_store,
    open_settings_database,
)
from local_agent.model_settings.attachments_oss import OssKeyStore
from local_agent.persistence import checkpoint as checkpoint_module
from local_agent.persistence.checkpoint import ChatDatabase
from local_agent.persistence.file_changes import FileChangeStore
from local_agent.persistence.job_store import JobStore
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
    #: MCP 连接管理器（2026-10-20）：关掉运行时必须一并收掉它的后台线程与子进程。
    mcp_manager: Any = field(default=None, repr=False)

    def close(self) -> None:
        """释放单实例锁并关闭数据库连接（测试与一次性脚本用；服务进程不需要调用）。"""
        self.service.close_tool_sessions()
        self.service.close_user_terminals()
        self.service.close_mcp()
        self.mcp_manager = None
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


def resolve_data_paths(config: AppConfig) -> AppConfig:
    """把**应用数据**的路径钉到数据根上（2026-10-26）。

    与旧 ``resolve_memory_paths`` 的两点不同：

    1. 基准从"配置文件所在目录"换成 **数据根**（绝对路径）。数据不能跟着代码走——源码目录
       会被 ``git clean``、重新 clone、换分支；
    2. 覆盖范围从三层记忆库扩到**设置库**，因为它是同一类东西（都是应用自己产生的状态）。

    **只管应用数据**：``workspace.root``（用户的代码目录）、``skills.user_dirs``（用户的技能
    目录）、``web.static_dir``（产物）仍按配置文件目录解析——把用户的项目搬进数据目录是另一回事。

    为什么这几条要单独收口：消费方是**图内节点**（`graph/nodes.py` 直接拿
    ``context.app_config.memory.*.path`` 去 ``open_store``），节点拿不到基准目录。2026-10-09
    实测后果：换一个目录启动，聊天库还在原地、**长期记忆却换成了另一个库**。

    语义与 :func:`resolve_under` 一致：绝对路径照用，相对路径拼在数据根上。
    """
    root = app_paths.data_root_of(config)
    memory = config.memory
    resolved_memory = memory.model_copy(
        update={
            "short_term": memory.short_term.model_copy(
                update={"path": str(resolve_under(root, memory.short_term.path))}
            ),
            "medium_term": memory.medium_term.model_copy(
                update={"path": str(resolve_under(root, memory.medium_term.path))}
            ),
            "long_term": memory.long_term.model_copy(
                update={"path": str(resolve_under(root, memory.long_term.path))}
            ),
            # 常驻记忆（§10.7）：与三层库同一基准——它也是应用自己产生的状态，不跟着代码目录走。
            "curated": memory.curated.model_copy(
                update={"path": str(resolve_under(root, memory.curated.path))}
            ),
        }
    )
    settings = config.model_settings.model_copy(
        update={"path": str(resolve_under(root, config.model_settings.path))}
    )
    return config.model_copy(update={"memory": resolved_memory, "model_settings": settings})


def resolve_skill_paths(config: AppConfig, base_dir: Path) -> AppConfig:
    """把 ``skills.user_dirs`` 里的**相对路径**钉到 ``base_dir`` 上（2026-10-16）。

    为什么要有这一步：技能的**内容**是文件（`SKILL.md` + 资源目录），发布时只能随包走；而发现根原来
    **只接受绝对路径**，于是"包里带技能"就退化成"让朋友手改配置写自己机器上的绝对路径"——正是 T40
    "解压双击"那条路上最容易卡住的一步。

    语义与 :func:`resolve_under` **逐字一致**（绝对路径照用、相对路径拼在基准目录上、不展开 ``~``）：
    相对 = **相对 config.yaml 所在目录**，与进程当前目录无关——原来拒绝相对路径的理由（cwd 漂移）
    在这里不成立。

    与 :func:`resolve_data_paths` 并列，同样在**三个入口**各调用一次（``open_runtime`` / ``cli`` /
    ``web``），保证消费方（清单节点、技能工具、设置页）看到的是同一份路径。
    """
    skills = config.skills
    values = [str(resolve_under(base_dir, value)) for value in skills.user_dirs]
    # 包内技能目录（2026-10-20）：冻结形态的配置在 `%LOCALAPPDATA%/chaiai`，那里既没有 config.yaml
    # 也没有 skills；包里的 `skills/` 就在 exe 同级。不追加这一条，界面上永远"识别不到技能"。
    bundled = app_paths.bundled_skills_dir()
    if bundled is not None and str(bundled) not in values:
        values.append(str(bundled))
    if not values:
        return config
    resolved = skills.model_copy(update={"user_dirs": values})
    return config.model_copy(update={"skills": resolved})


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

    # 数据根先算出来：会话库的路径、锁文件、后面的附件与日志都从它派生。
    data_root = app_paths.data_root_of(app_config)
    database_path = resolve_under(data_root, app_config.memory.short_term.path)
    lock = acquire_instance_lock(database_path) if instance_lock else None
    settings: SettingsDatabase | None = None
    #: MCP 连接管理器（2026-10-20）。装配失败时也要关掉：它已经起了后台线程。
    mcp_manager: Any = None
    try:
        # 工作区是**用户内容**，仍按配置文件目录解析（不搬进数据目录）。
        workspace_root = resolve_under(base_dir, app_config.workspace.root)
        # 图内节点直接按配置里的路径开记忆库，所以在这里先钉到数据根上（§8）。
        app_config = resolve_data_paths(app_config)
        # 技能发现根里的相对路径同样钉到 base_dir（2026-10-16）：解压到哪个盘都能用。
        app_config = resolve_skill_paths(app_config, base_dir)
        database = checkpoint_module.open_database(database_path, workspace_root=workspace_root)
        settings = _open_settings(app_config, base_dir=base_dir)
        # MCP（2026-10-20；契约 §6.13）：开关关着时返回 None，`mcp` 能力因此不进能力集。
        mcp_manager = build_mcp_manager(app_config, base_dir=base_dir, data_root=data_root,
                                        settings=settings,
                                        state_lock=database.checkpointer.lock)
        service = ApplicationService(
            timing_path=data_root / "metrics" / "turn-timings.jsonl",
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
            # 能力开关（§16.1）：与上面三个同源，都是设置库里的可调项；没有库时整组为 None。
            capabilities=None if settings is None else settings.capabilities,
            # 联网检索的服务凭据（Tavily）：与模型连接共用同一个凭据存储，没有库时只剩环境变量。
            web_search=None if settings is None else settings.web_search,
            # 技能的用户级禁用表（技能管理页，2026-10-16）：与能力开关同源，没有库时整组为 None。
            skills=None if settings is None else settings.skills,
            # MCP 的开关存储与连接管理器（2026-10-20）：两者都可能为 None（没库 / 总开关关着）。
            mcp=None if settings is None else settings.mcp,
            mcp_manager=mcp_manager,
            # 重载与错误回报都要读这份文件；位置与 config.yaml 同目录。
            mcp_config_path=base_dir / DEFAULT_MCP_PATH,
            # 附件存储（2026-10-16）：内容落 `<data_root>/attachments/年/月/日/会话/`——
            # 与三个库同处（2026-10-26 起统一从数据根派生，不再各自拼 `base_dir/data`）。
            attachments=AttachmentStore.under(data_root),
            # 对象存储密钥（2026-10-20）：与模型连接共用设置库里的凭据存储。
            oss_keys=None if settings is None else OssKeyStore(settings.credentials),
            # 任务索引与**会话索引、Checkpoint 同库同连接**（§17.1）；worker 只在
            # `jobs.background=true` 时才起，默认路径与 v0.5 一致。
            # 文件变更账本（2026-10-21）：与 Checkpoint 同库同锁（工具在 executor 线程里写，
            # 与快照必须串行），基线原文落在 `<data_root>/file-changes/<会话>/`。
            file_changes=partial(
                FileChangeStore.under(data_root).ledger,
                database.connection,
                lock=database.checkpointer.lock,
            ),
            jobs=JobRuntime(
                # 与 Checkpoint **同一把锁**：worker 线程与会话索引、Checkpoint 共用一条连接，
                # 跨线程不串行会踩掉彼此的事务（见 `persistence/job_store.py` 的模块 docstring）。
                JobStore(database.connection, lock=database.checkpointer.lock),
                background=app_config.jobs.background,
                max_concurrent_jobs=app_config.jobs.max_concurrent_jobs,
            ),
        )
        if lock is not None:
            # 重启后的任务收口（架构 §17.5）：**只在持单实例锁时做**——拿到锁就等于"本进程是这个
            # 库唯一的服务进程"，索引里剩下的非终态任务必然是上一次进程留下的。拿不到锁说明另一个
            # 进程正占着这个库（`open_runtime` 会直接抛错），那些任务可能是它正在跑的活。
            service.recover_interrupted_jobs()
        if mcp_manager is not None:
            mcp_manager.start()
    except BaseException:
        # 装配失败不能留下"锁被一个不存在的服务占着"的状态。
        if lock is not None:
            lock.release()
        if mcp_manager is not None:
            # 管理器已经起了预热/回收线程与子进程；装配在这里失败时必须收掉。
            mcp_manager.close()
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
        mcp_manager=mcp_manager,
    )


def _mcp_switch_on(app_config: AppConfig, settings: SettingsDatabase | None) -> bool:
    """MCP 总开关：**设置库优先**，``config.yaml`` 只是出厂默认（与 §16.1 同一口径）。"""
    if settings is not None:
        return bool(settings.capabilities.switches().get("mcp"))
    return bool(app_config.mcp.enabled)


def build_mcp_manager(
    app_config: AppConfig,
    *,
    base_dir: Path,
    data_root: Path,
    settings: SettingsDatabase | None,
    state_lock: Any = None,
) -> Any:
    """按 ``mcp.json`` 建连接管理器；容器装配完成后再启动后台预热。

    三个刻意的选择：

    - **开关关着时完全不建**：``mcp`` SDK 的导入要 0.5–1 秒，默认配置下不该付这笔钱。
    - **配置**文件与 ``config.yaml`` 同目录（``base_dir``）：源码形态就是仓库里的 ``mcp.json``，
      冻结形态是 ``%LOCALAPPDATA%/chaiai/mcp.json``。它是**用户手写的配置**，所以跟配置文件走；
      而工具目录缓存（``data_root/mcp_catalog.sqlite``）是应用自己产生的状态，跟数据根走。
    - 解析失败**不阻断启动**：``mcp.json`` 是手写文件，一个逗号错就让整个应用打不开的话，
      用户连改它的入口都没有。这里记警告并当作"没有 server"；错误详情由读取接口回报
      （那里的报错能直接展示给人看）。

    Returns:
        ``McpManager``；开关关着或配置不可读时返回 ``None``。
    """
    if not _mcp_switch_on(app_config, settings):
        return None
    # 延迟导入：这两条路径（``mcp.config`` 可以，``mcp.manager`` 不行）会拉 SDK。
    from local_agent.mcp.config import DEFAULT_MCP_PATH, load_mcp_servers
    from local_agent.mcp.manager import McpManager

    try:
        servers = load_mcp_servers(base_dir / DEFAULT_MCP_PATH)
    except ConfigError as exc:
        logging.getLogger(__name__).warning("MCP 配置无效，本次不加载任何 server：%s", exc)
        return None
    def provider() -> Any:
        # 与页面的 _sqlite_access 共用锁，不能只锁 MCP store 自己。
        with (state_lock if state_lock is not None else nullcontext()):
            return {} if settings is None else settings.mcp.states()

    manager = McpManager(
        servers,
        config=app_config.mcp,
        state_provider=provider,
        cache_path=data_root / "mcp_catalog.sqlite",
    )
    return manager


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
            # 能力开关同一条口径（§16.1）：config.yaml 解析出的出厂默认 + 服务端枚举的可开关集。
            default_capabilities=app_config.capability_default_layer(),
            switchable_capabilities=switchable_capabilities(),
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
