"""网关进程的运行时句柄（2026-11-03；架构 §24）。

**为什么主服务要能即时启停网关**：用户的原话是"出问题之后可以操控关闭"。一个"改配置 → 重启服务"
才生效的开关，在"疑似被入侵"的场景下等于没有——那时最需要的是**立刻断掉**，而不是让人去翻配置文件
再等一次重启。所以这里给的是一个命令式入口：调用即刻执行，不等配置、不等重启。

**为什么是模块级单例而不是依赖注入**：网关进程的生命周期属于**进程**，不属于**请求**。把它塞进
FastAPI 的依赖树，会让"谁持有这个句柄"变得含糊；这里只由 `api.__main__` 在启动时 attach 一次，
HTTP 端点通过 :func:`gateway_runtime` 拿到它。

**没有 attach 时所有操作都是 no-op 并如实报错**：测试、或用 `uvicorn` 直接起的服务并不管理网关
（没有子进程可管）。那时说"本进程不负责网关"比假装关掉了更安全——用户需要知道"我关的是不是真关了"。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

from local_agent.config import AppConfig
from local_agent.mobile.supervisor import GatewayProcess, is_listening, start_gateway


@dataclass(frozen=True, slots=True)
class RuntimeOutcome:
    """一次启停的结果。``ok=False`` 时 ``detail`` 是可以直接给用户看的句子。"""

    ok: bool
    detail: str
    running: bool


class GatewayRuntime:
    """主服务持有的网关子进程句柄。

    所有方法都加锁：uvicorn 单 worker 下 HTTP 处理仍可能是多线程（同步端点跑在线程池里），
    而"先判断再启动"这种序列不加锁时两次并发点击会拉起两个进程。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._process: GatewayProcess | None = None
        self._config_path: Path | None = None
        self._data_root: Path | None = None

    # ---- 装配 ---------------------------------------------------------------

    def attach(
        self,
        *,
        config_path: Path,
        data_root: Path,
        process: GatewayProcess | None,
    ) -> None:
        """由入口在启动时调用一次；``process`` 为 ``None`` 表示"配置了但没起来"。"""
        with self._lock:
            self._config_path = config_path
            self._data_root = data_root
            self._process = process

    def detach(self) -> GatewayProcess | None:
        """交出当前进程并清空句柄（入口退出时用，由调用方负责 stop）。"""
        with self._lock:
            process, self._process = self._process, None
            return process

    @property
    def attached(self) -> bool:
        """本进程是否负责管理网关（只有入口拉起的服务才为真）。"""
        with self._lock:
            return self._config_path is not None and self._data_root is not None

    def _alive(self) -> bool:
        return self._process is not None and self._process.process.poll() is None

    # ---- 操作 ---------------------------------------------------------------

    def start(self, config: AppConfig) -> RuntimeOutcome:
        """确保网关在跑。已在跑就如实说"已在运行"，不重复拉起。"""
        with self._lock:
            if self._config_path is None or self._data_root is None:
                return RuntimeOutcome(False, "本进程不负责管理移动网关（不是由主服务入口启动的）。", False)
            if self._alive():
                return RuntimeOutcome(True, "网关已在运行。", True)
            if is_listening(config.mobile.gateway_host, config.mobile.gateway_port):
                # 端口被别的进程占着（用户手动起的、或上一次没退干净）。**不接管**它：
                # 替别人的进程做主（之后去 stop 它）比"说明情况"危险得多。
                return RuntimeOutcome(
                    True,
                    f"端口 {config.mobile.gateway_port} 上已有一个网关在运行（不是本进程拉起的），"
                    "已经沿用它。",
                    True,
                )
            if not config.mobile.enabled:
                return RuntimeOutcome(
                    False,
                    "配置里 mobile.enabled 是关闭的：先在配置里打开，再启动网关。",
                    False,
                )
            process = start_gateway(
                config, config_path=self._config_path, data_root=self._data_root
            )
            if process is None:
                return RuntimeOutcome(False, "网关没能启动起来，原因见 data_root/mobile/gateway.log。", False)
            self._process = process
            return RuntimeOutcome(True, "网关已启动。", True)

    def stop(self) -> RuntimeOutcome:
        """立刻停掉网关；没在跑也算成功（幂等，方便"多按几次确保关掉"）。"""
        with self._lock:
            process, self._process = self._process, None
            if process is None:
                return RuntimeOutcome(True, "网关本来就没有在运行。", False)
            process.stop()
            return RuntimeOutcome(True, "网关已停止，所有已连接的手机立即断开。", False)


_RUNTIME = GatewayRuntime()


def gateway_runtime() -> GatewayRuntime:
    """全局句柄。**只有入口会 attach 它**，其余调用方只读。"""
    return _RUNTIME


__all__ = ["GatewayRuntime", "RuntimeOutcome", "gateway_runtime"]
