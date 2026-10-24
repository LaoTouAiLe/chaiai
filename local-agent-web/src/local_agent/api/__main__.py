"""官方入口：``python -m local_agent.api``（T29；§15.1 单进程、单 worker）。

启动前只做三件事：读 `.env` 与配置 → 构造模型客户端 → 装配运行时。装配失败一律给**可执行的
提示**并以 1 退出，不做半启动（与 Streamlit 入口同一口径）。

本入口同时是"目录选择器"的自我 spawn 目标：带 ``--pick-directory`` 启动时**不做任何装配**，
只弹一个原生选择窗并把结果打到 stdout（见 `local_agent.directory_picker`）。源码模式经
`python -m local_agent.api` 进入；打包后 exe 通过 `sys.executable` 自启动。

打包形态（T40）只换**路径来源**，不换流程：``--config`` / ``--env`` 默认落在用户数据目录
（``%LOCALAPPDATA%\\chaiai``），前端产物从只读资源目录（``sys._MEIPASS``）取。口径与理由见
:mod:`local_agent.app_paths`；源码形态的默认配置固定在源码项目目录，显式路径优先。

服务就绪后默认用本机默认浏览器打开页面（:mod:`local_agent.browser_open`）：没参与开发的人双击
exe 时唯一期望就是"页面自己出来"，而不是先复制一段地址。自动化场景用 ``--no-open-browser`` 关掉。

多 worker 属于**不受支持配置**，所以这里硬编 ``workers=1``；配套的排他文件锁会让第二个进程
在启动时就失败——进程内广播、每会话互斥与幂等台账都以"只有一个进程在写"为前提（§15.1）。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path

import uvicorn

from local_agent import app_paths, browser_open, desktop_window, directory_picker
from local_agent import config as config_module
from local_agent.api.app import create_app
from local_agent.api.container import open_runtime
from local_agent.config import ConfigError
from local_agent.exceptions import SessionStoreError


def main(argv: Sequence[str] | None = None) -> int:
    """启动本机服务；返回进程退出码（0 = 正常结束，1 = 装配失败）。"""
    forwarded = list(sys.argv[1:] if argv is None else argv)
    # 自我 spawn 的对话框分支：必须在 argparse **之前**，它不做任何装配就接走整个进程。
    if directory_picker.PICK_DIRECTORY_FLAG in forwarded:
        return directory_picker.main(forwarded)

    parser = argparse.ArgumentParser(
        prog="python -m local_agent.api",
        description="启动 local-agent-web 的本机 Web 服务（v0.4，单进程单 worker）",
    )
    parser.add_argument(
        "--config",
        default=str(app_paths.default_config_path()),
        help="配置文件路径（源码默认项目目录的 config.yaml；打包后默认用户数据目录里的 config.yaml）",
    )
    parser.add_argument(
        "--env",
        default=None,
        help=".env 路径（只用来取 API Key；默认与 --config 同目录）",
    )
    parser.add_argument(
        "--no-open-browser",
        action="store_true",
        help="启动后不显示任何界面（不开窗口、不开浏览器；自动化与只看日志时用）",
    )
    parser.add_argument(
        "--window",
        action="store_true",
        help="用桌面窗口显示页面（打包形态默认开；源码形态默认用浏览器，便于刷新调试）",
    )
    parser.add_argument("--diagnose", action="store_true", help="诊断配置与环境，不启动服务或读取密钥")
    args = parser.parse_args(argv)
    config_path = Path(args.config)

    if args.diagnose:
        from local_agent.diagnostics import print_diagnosis

        return print_diagnosis(config_path)

    try:
        config_module.load_dotenv_file(
            config_path.with_name(".env") if args.env is None else Path(args.env)
        )
        # 没有 config.yaml 也照常起来（§15.9 的初始设置模式）：设置页可用，提交会给出可执行提示。
        app_config = config_module.load_config(config_path, allow_missing=True)
        # **启动不再要求模型 Key**（§15.9）：客户端在提交边界按连接构造，缺连接或缺 Key 时
        # 设置页仍然可用，提交会得到 `MODEL_CONNECTION_REQUIRED` / `MODEL_AUTH_FAILED` 这类
        # 可执行提示——而不是"服务起不来，只能去翻终端"。
        # 相对路径按**配置文件所在目录**解析：从哪个目录启动都得到同一个库。
        # 打包形态的前端产物在**只读资源目录**里（不在数据目录），显式交给装配；源码形态拿到
        # None，继续按 `web.static_dir` 相对配置文件目录解析——行为不变（§T40）。
        runtime = open_runtime(
            app_config,
            base_dir=config_path.resolve().parent,
            static_dir=app_paths.bundled_static_dir(),
        )
    except (ConfigError, RuntimeError, SessionStoreError, sqlite3.Error) as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        return 1

    web = app_config.web
    if not config_module.is_loopback_host(web.host):
        print(
            f"警告：web.allow_non_loopback=true，服务监听非回环地址 {web.host}；"
            "v0.4 没有鉴权，只为本机使用设计。",
            file=sys.stderr,
        )
    frontend = (
        f"前端产物：{runtime.static_dir}"
        if runtime.static_dir is not None
        else "未找到前端产物（只提供 API）：先在 frontend/ 执行 npm run build"
    )
    setup_hint = (
        ""
        if runtime.service.model_configured()
        else "\n还没有可用的模型连接：打开设置页添加一条（不需要重启服务）。"
    )
    # 打包形态把"用户数据在哪"直接打出来：朋友用不到命令行，但排障和备份都得知道这个目录。
    data_dir = app_paths.data_dir()
    data_hint = "" if data_dir is None else f"\n数据目录：{data_dir}"
    print(
        f"local-agent-web v0.4 已启动：http://{web.host}:{web.port}（单进程、单 worker）"
        f"\n{frontend}{data_hint}{setup_hint}",
        flush=True,
    )
    show_ui = not args.no_open_browser
    # 窗口优先：打包形态（双击 exe 的人）默认开窗口，源码形态默认浏览器（开发时刷新、看 devtools 更方便）。
    # 依赖缺失时 `window_available()` 返回 False，这里会自然落到下面浏览器那条路。
    want_window = show_ui and (args.window or app_paths.is_frozen())
    if want_window and desktop_window.window_available():
        try:
            return desktop_window.run(
                create_app(runtime),
                host=web.host,
                port=web.port,
                keepalive_seconds=web.keepalive_seconds,
            )
        finally:
            # 窗口关掉才算"服务结束"：这里与下面浏览器分支的收尾口径一致。
            runtime.close()

    if show_ui:
        # 后台线程等端口真的可连再打开：抢在服务就绪之前打开会得到一张连接失败的页面。
        probe_host, page_url = browser_open.page_target(web.host, web.port)
        print(f"正在用本机默认浏览器打开：{page_url}", flush=True)
        browser_open.open_when_ready(page_url, host=probe_host, port=web.port)
    try:
        uvicorn.run(
            create_app(runtime),
            host=web.host,
            port=web.port,
            workers=1,
            timeout_keep_alive=web.keepalive_seconds,
            log_level="info",
        )
    finally:
        runtime.close()
    return 0


if __name__ == "__main__":  # pragma: no cover - 由 `python -m` 触发
    raise SystemExit(main())
