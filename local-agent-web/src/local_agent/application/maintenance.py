"""记忆维护的后台定时任务（架构 §10.3 的遗忘任务；2026-11-02）。

为什么需要它：``memory.forgetting.run_forgetting`` 的判定早就写好了，但入口只有
``python -m local_agent.cli memory forget``——**没有任何东西会自己跑**。而长期记忆默认开启，
意味着事实只增不减：显式过期的行一直停在 ``active``，超龄的行也不会被落库淘汰。召回层虽然实时
过滤（过期记忆不会被注入 Prompt），但库会一直变大，管理入口与 CLI 看到的状态也不再反映真实
可用的集合。

**与用户 cron 的分工**（不要混为一谈）：用户 cron 跑的是 **prompt**，走正常提交链路、会调模型、
会产生会话与任务记录；这里跑的是**代码**，不提交消息、不调模型、不产生会话。两者都由 lifespan
启停，但只有本模块能在没有用户动作的前提下动记忆库。

三条约束：

1. **只做"把已经不可召回的行落库改状态"**：不合并、不改写、不删除记忆内容。巩固（合并重复、
   把中期碎片提炼成长期事实）是另一件事，且必须经 ``reflection_node``（架构 §10.4）；
2. **用完即关连接**：频率是小时级，不为它常驻一个 SQLite 句柄，也不与图内节点的 store 抢写锁；
3. **失败要可见、但不许杀线程**：异常记日志并留在 :meth:`MemoryMaintenance.status` 里，下一轮
   继续。维护是兜底，不是执行的必要条件——与超时看守（``service._watchdog_loop``）同一口径。

**配置口径**：每轮现取 ``host.memory_maintenance_config()``，因此设置页改了 ``aging_enabled`` /
``forget_after_days``，下一轮就生效，不必重启（与召回、写入读的是同一份偏好）。

**中期库不在这里清理**：打开它需要 embedding 适配器（要加载模型），后台任务不该为此付一次模型
加载的代价；中期过期清理仍走 CLI。中期记忆默认关闭，这条限制的代价目前为零。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Protocol

from local_agent.config import AppConfig
from local_agent.memory import long_term
from local_agent.memory.forgetting import ForgetReport, run_forgetting

logger = logging.getLogger(__name__)

#: 两次维护之间的间隔。小时级：库里"过期了但状态还是 active"的行不影响召回，只影响体积与显示。
DEFAULT_INTERVAL_SECONDS: Final[float] = 6 * 3600.0

#: 启动后的首次延迟：不与应用启动抢时间（建库、迁移、装配都在那几秒里）。
DEFAULT_INITIAL_DELAY_SECONDS: Final[float] = 60.0

#: 关闭服务时等维护线程收尾的上限（秒）。超时就不等了：它是 daemon 线程。
_STOP_TIMEOUT_SECONDS: Final[float] = 5.0


class MaintenanceHost(Protocol):
    """调度器需要的最小宿主接口（应用服务实现；测试可直接传一个假对象）。

    只读配置，不给调度器任何写业务状态的入口——它要动的东西全部经由 ``run_forgetting``。
    """

    def memory_maintenance_config(self) -> AppConfig:
        """返回**应用了设置库偏好**的配置（与召回/写入同源）。"""
        ...


@dataclass(frozen=True, kw_only=True)
class MaintenanceRun:
    """一次维护的结果（诊断用；将来的"记忆健康度"直接读它）。"""

    started_at: datetime
    finished_at: datetime
    #: 实际执行的报告；``None`` 表示这一轮**跳过**（长期记忆未启用），不是失败。
    report: ForgetReport | None
    #: 失败原因（``类型: 说明``）；成功与跳过都是 ``None``。
    error: str | None
    #: 一次巩固的结果（``memory.consolidation.ConsolidationReport``）；``None`` = 未开启/跳过/失败。
    #: 这里用 ``Any`` 而不是导入那个类型：application 层不该为了一个状态字段拉入模型依赖。
    consolidation: Any = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def skipped(self) -> bool:
        return self.report is None and self.error is None


class MemoryMaintenance:
    """进程内单线程的记忆维护器（启动/停止由 ``api/app.py`` 的 lifespan 负责）。"""

    def __init__(
        self,
        host: MaintenanceHost,
        *,
        interval: float = DEFAULT_INTERVAL_SECONDS,
        initial_delay: float = DEFAULT_INITIAL_DELAY_SECONDS,
    ) -> None:
        self._host = host
        self._interval = interval
        self._initial_delay = initial_delay
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._guard = threading.Lock()
        self._last: MaintenanceRun | None = None

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def status(self) -> MaintenanceRun | None:
        """上一次运行的结果；还没跑过时为 ``None``（不伪造一个"成功"）。"""
        return self._last

    def start(self) -> None:
        """启动后台线程。

        **启动时判定**是否必要：长期记忆未启用就不起线程（中期清理不在本模块，见模块 docstring）。
        运行中改了记忆开关需要重启才会开始维护——这与"记忆库路径改了要重启"的既有口径一致。
        """
        with self._guard:
            if self.running:
                return
            if not self._host.memory_maintenance_config().memory.long_term.enabled:
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, name="memory-maintenance", daemon=True
            )
            self._thread.start()

    def stop(self, *, timeout: float = _STOP_TIMEOUT_SECONDS) -> None:
        with self._guard:
            thread = self._thread
            self._thread = None
            self._stop.set()
        if thread is not None:
            thread.join(timeout=timeout)

    def run_once(self, *, now: datetime | None = None) -> MaintenanceRun:
        """同步执行一次维护（测试与将来的"立即整理"按钮都走这里）。"""
        started = now or datetime.now(timezone.utc)
        config = self._host.memory_maintenance_config()
        long_config = config.memory.long_term
        if not long_config.enabled:
            # 未启用 = 跳过，不是失败：这一轮什么都不该做，也不该在状态里显示成红色。
            return self._record(started, started, report=None, error=None)

        store = None
        report: ForgetReport | None = None
        error: str | None = None
        try:
            store = long_term.open_store(Path(long_config.path))
            report = run_forgetting(config=config, long_store=store, now=started)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            logger.exception("记忆维护失败（下一轮继续）")
        finally:
            if store is not None:
                try:
                    store.close()
                except Exception:
                    logger.exception("关闭长期记忆库失败（连接已不打算复用）")
        # 巩固（2026-11-02）：只在**遗忘成功**之后跑，且宿主没实现就跳过（测试里的最小宿主）。
        # 它自己会判断开关、中期是否启用、有没有新碎片；失败在它内部兜住并返回 None。
        consolidation = None
        if error is None:
            runner = getattr(self._host, "consolidate_memories", None)
            if runner is not None:
                try:
                    consolidation = runner()
                except Exception:
                    logger.exception("记忆巩固失败（本轮遗忘结果不受影响）")
        finished = datetime.now(timezone.utc)
        return self._record(
            started, finished, report=report, error=error, consolidation=consolidation
        )

    # ---- 内部 ----------------------------------------------------------------

    def _record(
        self,
        started: datetime,
        finished: datetime,
        *,
        report: ForgetReport | None,
        error: str | None,
        consolidation: Any = None,
    ) -> MaintenanceRun:
        run = MaintenanceRun(
            started_at=started,
            finished_at=finished,
            report=report,
            error=error,
            consolidation=consolidation,
        )
        self._last = run
        if error is not None:
            logger.warning("记忆维护未完成：%s", error)
        elif report is None:
            logger.debug("记忆维护跳过：长期记忆未启用")
        elif report.total:
            logger.info(
                "记忆维护完成：显式过期 %d 条、自动老化 %d 条、中期删除 %d 条",
                report.expired_facts,
                report.aged_facts,
                report.deleted_medium,
            )
        else:
            # 全 0 是常态（库里没有该淘汰的行），用 debug 免得每 6 小时刷一条 info。
            logger.debug("记忆维护完成：本轮无需淘汰")
        if consolidation is not None and getattr(consolidation, "promoted", 0):
            logger.info(
                "记忆巩固完成：新增长期事实 %d 条（丢弃 %d 条）",
                consolidation.promoted,
                getattr(consolidation, "skipped", 0),
            )
        return run

    def _loop(self) -> None:
        if self._stop.wait(self._initial_delay):
            return
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:
                logger.exception("记忆维护调度失败（下一轮继续）")
            if self._stop.wait(self._interval):
                return
