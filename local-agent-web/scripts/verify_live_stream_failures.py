"""真实供应商链路上的断流 / 超时验收（2026-11-03）。

假模型已经证明"流式失败 → 丢弃投影 → 普通调用补全"的流程；这里换成真实 DeepSeek SSE，
在同一进程里注入供应商侧故障，回答三个具体问题：正文会不会重复、会不会丢、
会不会一直停在"生成中"。

做法：隔离设置库把默认连接的 ``base_url`` 指到本机代理，代理原样转发到
``https://api.deepseek.com``，只在请求体 ``"stream": true`` 时按场景注入：

- ``pass``：不注入（真实链路对照，验证供应商返回格式能被正常解析）；
- ``cut``：收到第一块 SSE 后直接断开上游连接（连接重置，没有结束块）；
- ``stall``：收到第一块后静默，让客户端读超时。

隔离：设置库、Checkpoint、工作区全部在 ``--root`` 下，原库只以只读方式备份。
真实 token 消耗只发生在 ``pass`` 与 ``cut`` 的降级调用上。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

from local_agent.api.app import create_app
from local_agent.api.container import open_runtime
from local_agent.config import AppConfig, load_config, load_dotenv_file

PROJECT = Path(__file__).resolve().parents[1]
UPSTREAM: str = "https://api.deepseek.com"
PROMPT: str = "请只回复两个字：好的"
ANSWER_MARK: str = "好的"
READY_TIMEOUT_SECONDS: float = 90.0


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def save(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build_proxy(control: dict[str, Any]) -> FastAPI:
    """转发代理：只有流式请求会按 ``control["mode"]`` 注入故障。"""
    app = FastAPI()

    @app.post("/{path:path}")
    async def forward(path: str, request: Request) -> StreamingResponse:
        body = await request.body()
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError:
            payload = {}
        streaming = bool(payload.get("stream"))
        requested = str(control.get("mode") or "pass")
        # 断流 / 静默只打在流式请求上；dead 是"供应商整个不可用"，连降级用的普通调用一起断。
        mode = "dead" if requested == "dead" else (requested if streaming else "pass")
        headers = {k: v for k, v in request.headers.items()
                   if k.lower() not in {"host", "content-length", "accept-encoding"}}
        # 必须要求上游不压缩：这里转发的是原始字节，压缩过的 SSE 交给应用只会是乱码。
        headers["accept-encoding"] = "identity"
        control["requests"] = int(control.get("requests") or 0) + 1

        async def gen():
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(180.0, connect=20.0), trust_env=False
            ) as client, client.stream("POST", f"{UPSTREAM}/{path}",
                                     content=body, headers=headers) as response:
                control["last_status"] = response.status_code
                if response.status_code >= 400:
                    yield await response.aread()
                    return
                if mode == "dead":
                    # 拿到响应头就断：不读正文，模拟"供应商不可用/网络断"。
                    await asyncio.sleep(0.2)
                    raise ConnectionAbortedError("注入：供应商不可用（连接中断）")
                seen = 0
                async for chunk in response.aiter_raw():
                    yield chunk
                    seen += 1
                    if seen < int(control.get("after") or 1):
                        continue
                    if mode == "cut":
                        await asyncio.sleep(0.2)
                        raise ConnectionAbortedError("注入断流：关闭上游 SSE 连接")
                    if mode == "stall":
                        await asyncio.sleep(float(control.get("stall_seconds") or 90))

        return StreamingResponse(gen(), media_type="text/event-stream")

    return app


def prepare_root(root: Path, proxy_url: str, timeout_seconds: int) -> tuple[AppConfig, str]:
    """复制设置库并把默认连接指向代理，返回隔离配置与默认连接名。"""
    root.mkdir(parents=True, exist_ok=False)
    workspace = root / "workspaces"
    workspace.mkdir()
    load_dotenv_file(PROJECT / ".env")
    original = load_config(PROJECT / "config.yaml")
    data_root = Path(original.data_root) if original.data_root else PROJECT
    source = Path(original.model_settings.path)
    if not source.is_absolute():
        source = data_root / source
    if not source.exists():
        raise SystemExit(f"找不到设置库：{source}")
    with (sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as src,
          sqlite3.connect(root / "settings.sqlite") as dst):
        src.backup(dst)
        default = dst.execute(
            "SELECT s.connection_id, c.display_name FROM model_selection s"
            " LEFT JOIN model_connections c ON c.connection_id = s.connection_id"
            " WHERE s.scope='default'"
        ).fetchone()
        if default is None:
            raise SystemExit("设置库里没有默认连接，先在本机设置页选一个真实模型")
        connection_id, display_name = default
        in_use = dst.execute(
            "SELECT config_version FROM model_selection WHERE scope='default'"
        ).fetchone()[0]
        dst.execute(
            "UPDATE model_connections SET base_url=?, allow_plaintext_http=1, timeout_seconds=?"
            " WHERE connection_id=?",
            (proxy_url, timeout_seconds, connection_id),
        )
        # 恢复/固定版本路径按 config_version 回查历史版本表：两张都要改，否则通道只换了名字。
        dst.execute(
            "UPDATE model_connection_versions SET base_url=?, timeout_seconds=?"
            " WHERE connection_id=? AND config_version=?",
            (proxy_url, timeout_seconds, connection_id, in_use),
        )
        dst.execute("DELETE FROM model_selection WHERE scope <> 'default'")
        dst.commit()
    data = original.model_dump()
    data["data_root"] = str(root)
    data["workspace"]["root"] = str(workspace)
    data["memory"]["short_term"]["path"] = str(root / "checkpoints.sqlite")
    for layer in ("long_term", "medium_term", "curated"):
        if layer in data["memory"]:
            data["memory"][layer]["enabled"] = False
            data["memory"][layer]["path"] = str(root / layer)
    data["model_settings"]["path"] = str(root / "settings.sqlite")
    data["web"]["host"] = "127.0.0.1"
    data["skills"]["enabled"] = False
    return AppConfig.model_validate(data), str(display_name)


def serve(app: Any, port: int) -> tuple[uvicorn.Server, threading.Thread]:
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    return server, thread


def wait_ready(url: str) -> dict[str, Any]:
    deadline = time.time() + READY_TIMEOUT_SECONDS
    last_error = ""
    while time.time() < deadline:
        try:
            response = httpx.get(f"{url}/api/health", timeout=3.0, trust_env=False)
            if response.status_code == 200:
                return response.json()
            last_error = f"HTTP {response.status_code}"
        except Exception as exc:  # noqa: BLE001 - 启动期连不上就是还没好，记下最后一次
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(0.5)
    raise SystemExit(
        f"服务在 {READY_TIMEOUT_SECONDS:.0f}s 内没有就绪：{url}（最后错误：{last_error}）"
    )


def run_case(client: httpx.Client, url: str, control: dict[str, Any], mode: str,
             out_dir: Path) -> dict[str, Any]:
    control["mode"] = mode
    target = Path(control["workspace"])
    target.mkdir(parents=True, exist_ok=True)
    selection = client.post(f"{url}/api/workspaces/select", json={"path": str(target)})
    selection.raise_for_status()
    session_id = selection.json()["session_id"]

    result: dict[str, Any] = {"mode": mode, "started_at": now(), "session_id": session_id}
    holder: dict[str, Any] = {}

    def submit() -> None:
        started = time.time()
        try:
            response = httpx.post(
                f"{url}/api/sessions/{session_id}/messages",
                json={"text": PROMPT, "client_request_id": f"live-{mode}-{time.time_ns()}"},
                timeout=600.0, trust_env=False,
            )
            holder["http_status"] = response.status_code
            holder["body"] = response.json()
        except Exception as exc:  # noqa: BLE001 - 失败也是结果
            holder["exception"] = f"{type(exc).__name__}: {exc}"
        finally:
            holder["elapsed"] = round(time.time() - started, 2)

    worker = threading.Thread(target=submit, daemon=True)
    worker.start()

    states: list[str] = []
    deadline = time.time() + 420.0
    while time.time() < deadline:
        try:
            snapshot = client.get(f"{url}/api/sessions/{session_id}", timeout=15.0).json()
        except Exception:  # noqa: BLE001 - 提交期间偶尔读不到，下一次再来
            time.sleep(0.3)
            continue
        state = str(snapshot.get("session_state"))
        if not states or states[-1] != state:
            states.append(state)
        if not worker.is_alive():
            break
        time.sleep(0.3)
    worker.join(timeout=30.0)

    final = client.get(f"{url}/api/sessions/{session_id}", timeout=30.0).json()
    answers = [row.get("text") or "" for row in final.get("messages") or []
               if row.get("kind") == "assistant"]
    streaming_rows = [row for row in final.get("messages") or []
                      if row.get("kind") == "assistant" and row.get("streaming")]
    result.update({
        "finished_at": now(),
        "states": states,
        "final_state": str(final.get("session_state")),
        "submit": holder,
        "answer_occurrences": sum(text.count(ANSWER_MARK) for text in answers),
        "assistant_rows": len(answers),
        "answer_texts": answers,
        "streaming_rows_left": len(streaming_rows),
        "errors": [row.get("title") for row in final.get("errors") or []],
        "usage": final.get("usage"),
        "model_calls": [
            {"status": item.get("status"), "retry_of": item.get("retry_of")}
            for item in final.get("model_calls") or []
        ],
        "final_result": (final.get("final_result") or {}).get("state"),
        "proxy": {"requests": control.get("requests"), "last_status": control.get("last_status")},
    })
    save(out_dir / f"{mode}.json", {"result": result, "snapshot": final})
    print(json.dumps({k: result[k] for k in (
        "mode", "states", "final_state", "answer_occurrences", "assistant_rows",
        "streaming_rows_left", "errors", "proxy")}, ensure_ascii=False), flush=True)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8153)
    parser.add_argument("--proxy-port", type=int, default=8154)
    parser.add_argument("--timeout-seconds", type=int, default=12,
                        help="隔离副本里的模型读超时（真实配置是 120s，这里只压缩等待）")
    parser.add_argument("--stall-seconds", type=float, default=90.0)
    parser.add_argument("--only", choices=["pass", "cut", "stall", "dead"], action="append")
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    root = args.root.resolve()
    proxy_url = f"http://127.0.0.1:{args.proxy_port}"
    config, connection_name = prepare_root(root, proxy_url, args.timeout_seconds)
    save(root / "manifest.json", {
        "created_at": now(),
        "connection": connection_name,
        "upstream": UPSTREAM,
        "proxy": proxy_url,
        "prompt": PROMPT,
        "timeout_seconds": args.timeout_seconds,
        "isolation": "设置库/Checkpoint/工作区均为副本；原库只读备份；memory 各层关闭",
    })

    control: dict[str, Any] = {"mode": "pass", "after": 1, "stall_seconds": args.stall_seconds,
                               "workspace": str(root / "workspaces" / "case")}
    proxy_server, _ = serve(build_proxy(control), args.proxy_port)
    runtime = open_runtime(config, base_dir=root, static_dir=PROJECT / "frontend/dist")
    app_server, _ = serve(create_app(runtime), args.port)
    url = f"http://127.0.0.1:{args.port}"
    try:
        health = wait_ready(url)
        save(root / "health.json", health)
        if not health.get("model_configured"):
            raise SystemExit("隔离副本里模型未配置（凭据解不出来？）：先在本机设置页确认连接可用")
        modes = args.only or ["pass", "cut", "stall"]
        results = []
        with httpx.Client(timeout=60.0, trust_env=False) as client:
            for mode in modes:
                results.append(run_case(client, url, control, mode, root / "results"))
        save(root / "summary.json", {
            "created_at": now(),
            "connection": connection_name,
            "results": [{k: item[k] for k in (
                "mode", "states", "final_state", "answer_occurrences", "assistant_rows",
                "streaming_rows_left", "errors", "submit", "usage")} for item in results],
        })
    finally:
        app_server.should_exit = True
        proxy_server.should_exit = True
        time.sleep(1.5)
        runtime.close()


if __name__ == "__main__":
    main()
