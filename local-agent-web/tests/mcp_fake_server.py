"""测试用的最小 MCP Server（stdio）。

**为什么用 SDK 自己的 server 端而不是手写 JSON-RPC**：协议版本、握手字段、分页格式都由
同一个版本产出，测试盯的是**我方客户端**的行为，不该同时替协议实现兜底。

行为由 ``FAKE_MCP_MODE`` 控制（测试通过 ``env`` 注入，不用改文件）：

- 缺省 / ``ok``：正常提供工具。
- ``crash``：启动即退出（模拟命令存在但立刻死掉）。
- ``slow``：握手前睡 10 秒（模拟 ``npx`` 首次下载包）。

直接运行：``python tests/mcp_fake_server.py``。
"""

from __future__ import annotations

import os
import sys
import time

from mcp.server.mcpserver import MCPServer

app = MCPServer("fake")


@app.tool()
def echo(text: str) -> str:
    """原样回显给定文本。"""
    if text == os.environ.get("FAKE_MCP_CRASH_TEXT"):
        os._exit(7)
    return os.environ.get("FAKE_MCP_PREFIX", "") + text


@app.tool()
def sleep_for(seconds: float) -> str:
    """睡指定秒数后返回。用于验证调用超时。"""
    time.sleep(seconds)
    return f"slept {seconds}"


@app.tool()
def boom() -> str:
    """总是失败。用于验证 ``is_error`` 与错误文本。"""
    raise RuntimeError("intentional failure")


def main() -> None:
    mode = os.environ.get("FAKE_MCP_MODE", "ok")
    if mode == "crash":
        sys.exit(3)
    if mode == "slow":
        time.sleep(10)
    if mode == "tree":
        import subprocess
        from pathlib import Path
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        Path(os.environ["FAKE_MCP_PIDS"]).write_text(f"{os.getpid()},{child.pid}")
    app.run("stdio")


if __name__ == "__main__":
    main()
