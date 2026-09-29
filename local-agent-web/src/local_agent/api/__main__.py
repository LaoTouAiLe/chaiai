"""官方入口：``python -m local_agent.api``（T29；§15.1 单进程、单 worker）。

启动前只做三件事：读 `.env` 与配置 → 构造模型客户端 → 装配运行时。装配失败一律给**可执行的
提示**并以 1 退出，不做半启动（与 Streamlit 入口同一口径）。

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

from local_agent import config as config_module
from local_agent.api.app import create_app
from local_agent.api.container import open_runtime
from local_agent.config import ConfigError
from local_agent.exceptions import SessionStoreError


def main(argv: Sequence[str] | None = None) -> int:
    """启动本机服务；返回进程退出码（0 = 正常结束，1 = 装配失败）。"""
    parser = argparse.ArgumentParser(
        prog="python -m local_agent.api",
        description="启动 local-agent-web 的本机 Web 服务（v0.4，单进程单 worker）",
    )
    parser.add_argument(
        "--config",
        default=str(config_module.DEFAULT_CONFIG_PATH),
        help="配置文件路径（默认 config.yaml，相对当前目录）",
    )
    parser.add_argument(
        "--env",
        default=str(config_module.DEFAULT_ENV_PATH),
        help=".env 路径（默认 .env，只用来取 API Key）",
    )
    args = parser.parse_args(argv)
    config_path = Path(args.config)

    try:
        config_module.load_dotenv_file(Path(args.env))
        # 没有 config.yaml 也照常起来（§15.9 的初始设置模式）：设置页可用，提交会给出可执行提示。
        app_config = config_module.load_config(config_path, allow_missing=True)
        # **启动不再要求模型 Key**（§15.9）：客户端在提交边界按连接构造，缺连接或缺 Key 时
        # 设置页仍然可用，提交会得到 `MODEL_CONNECTION_REQUIRED` / `MODEL_AUTH_FAILED` 这类
        # 可执行提示——而不是"服务起不来，只能去翻终端"。
        # 相对路径按**配置文件所在目录**解析：从哪个目录启动都得到同一个库。
        runtime = open_runtime(app_config, base_dir=config_path.resolve().parent)
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
    print(
        f"local-agent-web v0.4 已启动：http://{web.host}:{web.port}（单进程、单 worker）"
        f"\n{frontend}{setup_hint}",
        flush=True,
    )
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
