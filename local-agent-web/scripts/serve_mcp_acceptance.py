"""MCP 页面验收专用服务：真实 API/数据库/stdio，隔离数据，不构造真实模型。"""

from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

import uvicorn

from local_agent.api.app import create_app
from local_agent.api.container import open_runtime
from local_agent.config import AppConfig


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8139)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    data = root / "data"
    data.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="mcp-browser-", dir=data) as directory:
        base = Path(directory)
        (base / "workspace").mkdir()
        config = AppConfig.model_validate({
            "workspace": {"root": str(base / "workspace")},
            "model_settings": {"credential_store": "memory"},
            "web": {"port": args.port},
        })
        runtime = open_runtime(config, base_dir=base, static_dir=root / "frontend" / "dist",
                               instance_lock=False)
        try:
            app = create_app(runtime)
            server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=args.port,
                                                   log_level="warning"))

            @app.post("/__test__/shutdown")
            def shutdown() -> dict[str, bool]:
                server.should_exit = True
                return {"stopping": True}

            server.run()
        finally:
            runtime.close()


if __name__ == "__main__":
    main()
