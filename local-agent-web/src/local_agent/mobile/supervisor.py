"""网关进程托管（架构 §24；2026-11-03 用户选 A）。

**为什么要主服务来拉**：让用户另开一个终端敲命令，网关随时可能不在，而主服务对此一无所知
——设置页于是显示"已配置但没在运行"，用户看到的就是一个不能用的功能。真实发生过一次：
IDE 崩溃把终端里的网关一起带走，设置页只剩那句话。

**为什么它仍然是独立进程**：网关崩了不能把主服务带走（§24 的故障域隔离不变）。主服务只负责
"起"和"退出时收"，**不接管重启**：崩了就如实报告状态，不静默重试——重试会把"配置写错了"
藏成"偶尔连不上"，那是更难查的一类故障。

**输出写日志文件而不是继承终端**：打包形态双击 exe 时根本没有终端，继承输出等于把最需要的
排障信息丢掉。日志在 ``data_root/mobile/gateway.log``。
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Final

from local_agent.config import AppConfig
from local_agent.security.process_guard import run_process

#: 打包形态下启动网关的参数。主服务的入口见到它就把进程整体交给网关（见 ``api/__main__``）。
GATEWAY_FLAG: Final[str] = "--mobile-gateway"

#: 等网关就绪的上限（秒）。首次启动要生成 RSA 证书，实测 1–2 秒；5 秒足够且不至于让用户干等。
STARTUP_TIMEOUT_SECONDS: Final[float] = 5.0

#: 停止时等它自己退出的宽限（秒），超过就强杀进程树。
STOP_GRACE_SECONDS: Final[float] = 5.0

#: 端口探测超时。回环上的 connect 要么立刻成功要么立刻失败。
PROBE_TIMEOUT_SECONDS: Final[float] = 0.25

LOG_NAME: Final[str] = "gateway.log"


def is_listening(host: str, port: int) -> bool:
    """``host:port`` 现在是否可连。

    用"能不能连上"而不是 PID 文件：PID 文件会留陈旧条目，而用户关心的正是"手机能不能连"。
    """
    try:
        with socket.create_connection((host, port), timeout=PROBE_TIMEOUT_SECONDS):
            return True
    except OSError:
        return False


def gateway_argv(config_path: Path) -> list[str]:
    """拉起网关的命令行。

    冻结形态不能 ``-m local_agent.mobile``（exe 没有模块搜索路径），改用入口参数；
    两种形态都由同一个 ``main`` 处理 ``--config``。
    """
    base = [sys.executable]
    if getattr(sys, "frozen", False):
        base.append(GATEWAY_FLAG)
    else:
        base.extend(["-m", "local_agent.mobile"])
    return [*base, "--config", str(config_path)]


class GatewayProcess:
    """一个由主服务拉起的网关子进程。"""

    def __init__(self, process: subprocess.Popen[bytes], *, log_path: Path) -> None:
        self.process = process
        self.log_path = log_path

    @property
    def pid(self) -> int:
        return self.process.pid

    def stop(self) -> None:
        """终止网关及其子进程树（以后接 cloudflared / frpc 时它们都是网关的子进程）。"""
        if self.process.poll() is not None:
            return
        if os.name == "nt":
            # 只 terminate 父进程会留下隧道子进程占着端口——那是"关不掉的服务"这类投诉的来源。
            # 终止命令本身也走统一执行出口（§20.1），只是它不需要（也不该）被登记成本轮进程。
            run_process(
                ["taskkill", "/F", "/T", "/PID", str(self.process.pid)], timeout_seconds=15,
            )
        else:
            killpg = getattr(os, "killpg", None)
            getpgid = getattr(os, "getpgid", None)
            try:
                if killpg is not None and getpgid is not None:
                    killpg(getpgid(self.process.pid), signal.SIGTERM)
                else:  # pragma: no cover - 极少见的平台
                    self.process.terminate()
            except OSError:
                self.process.terminate()
        try:
            self.process.wait(timeout=STOP_GRACE_SECONDS)
        except subprocess.TimeoutExpired:  # pragma: no cover - 强杀兜底
            self.process.kill()


def start_gateway(
    config: AppConfig,
    *,
    config_path: Path,
    data_root: Path,
) -> GatewayProcess | None:
    """必要时拉起网关，返回托管对象；未启用或已在运行时返回 ``None``。

    Args:
        config: 当前配置（只看 ``mobile`` 段）。
        config_path: 传给网关的配置文件路径（与主服务同一份）。
        data_root: 数据根；日志写在它下面的 ``mobile/gateway.log``。

    Returns:
        托管对象；``None`` 表示"没启用"或"已经有人起过了"。

    Raises:
        不抛异常：启动失败只打印原因，**不阻塞主服务启动**。远程访问是附加能力，它坏了不该让
        本机服务起不来。
    """
    mobile = config.mobile
    if not mobile.enabled:
        return None
    if is_listening(mobile.gateway_host, mobile.gateway_port):
        print("移动网关已在运行，沿用现有进程。", flush=True)
        return None

    log_path = data_root / "mobile" / LOG_NAME
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_handle = log_path.open("ab")
    except OSError as exc:
        print(f"移动网关未启动：写不了日志文件（{log_path}）：{exc.strerror or exc}", flush=True)
        return None

    argv = gateway_argv(config_path)
    cwd = str(config_path.parent)
    try:
        if os.name == "nt":  # pragma: no cover - 平台分支
            # `CREATE_NO_WINDOW`：源码形态下不该为网关弹一个黑窗口；
            # `CREATE_NEW_PROCESS_GROUP`：让 taskkill /T 能按组收掉它和它的子进程。
            process = subprocess.Popen(
                argv,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                cwd=cwd,
                creationflags=subprocess.CREATE_NO_WINDOW
                | subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        else:  # pragma: no cover - 平台分支
            process = subprocess.Popen(
                argv,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                cwd=cwd,
                start_new_session=True,
            )
    except OSError as exc:
        log_handle.close()
        print(f"移动网关未启动：{exc}", flush=True)
        return None

    supervisor = GatewayProcess(process, log_path=log_path)
    if os.name == "nt":  # pragma: no cover - 平台分支
        # 装进 Job：主服务被强杀（任务管理器 / IDE 崩溃）时网关也跟着死。没有这一步，用户以为
        # 关掉了远程访问，手机其实还能连进来——那是安全问题，不是不便。
        from local_agent.security.win_sandbox import attach_pid_to_kill_on_close_job

        note = attach_pid_to_kill_on_close_job(process.pid)
        if note:
            print(
                f"移动网关未能加入 Job（{note}）：主服务被强杀时它会留在后台，"
                "下次启动会复用它。",
                flush=True,
            )

    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            log_handle.close()
            print(
                f"移动网关启动失败（退出码 {process.returncode}）。日志：{log_path}",
                flush=True,
            )
            return None
        if is_listening(mobile.gateway_host, mobile.gateway_port):
            log_handle.close()
            print(
                f"移动网关已随主服务启动：{mobile.gateway_host}:{mobile.gateway_port}"
                f"（日志：{log_path}）",
                flush=True,
            )
            return supervisor
        time.sleep(0.2)

    log_handle.close()
    print(
        f"移动网关在 {STARTUP_TIMEOUT_SECONDS:.0f} 秒内没有就绪，已放弃等待（进程仍在后台）。"
        f"日志：{log_path}",
        flush=True,
    )
    # 进程还在但没监听：留着它没有意义，收掉后再报告，避免"后台有个占着端口的僵尸"。
    supervisor.stop()
    return None


__all__ = ["GATEWAY_FLAG", "GatewayProcess", "gateway_argv", "is_listening", "start_gateway"]
