"""隔离验证三个真实联网问题；使用已配置模型，须显式 --live，不写用户会话库。

--serve 在验证后托管当前前端产物，便于浏览器检查实际回答和刷新后的来源。
"""
from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

import uvicorn

from local_agent.api.app import create_app
from local_agent.api.container import open_runtime
from local_agent.config import AppConfig, load_config, load_dotenv_file

ROOT = Path(__file__).resolve().parents[1]
CASES = (
    ("百度热榜验收", "请联网查询百度实时热榜，列出前五条（置顶另列），附官方来源和获取时间。"),
    ("哔哩哔哩热榜验收", "请联网查询哔哩哔哩综合视频排行榜前五名，列出标题、UP主和视频链接，附榜单来源和获取时间。"),
    ("MCP联网验收", "MCP 是什么？请联网查阅官方文档，用中文简要解释，附实际查阅的官方来源链接。"),
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--port", type=int, default=8137)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    load_dotenv_file(ROOT / ".env")
    data_dir = Path(tempfile.mkdtemp(prefix="chaiai-web-answers-"))
    data = load_config(ROOT / "config.yaml").model_dump()
    data["workspace"]["root"] = str(data_dir)
    data["memory"]["short_term"]["path"] = str(data_dir / "checkpoints.sqlite")
    for layer in ("medium_term", "long_term"):
        data["memory"][layer]["enabled"] = False
    data["model_settings"].update(path=str(data_dir / "settings.sqlite"), credential_store="memory")
    data["rag"]["enabled"] = False
    data["skills"]["enabled"] = False
    data["jobs"]["background"] = False
    data["llm"].update(streaming=False, max_tokens=2500, timeout_seconds=90, retry_count=0)
    data["agent"].update(max_tool_loops=6, max_retries_per_step=1)
    data["profiles"]["active"] = "reviewer"
    runtime = open_runtime(AppConfig.model_validate(data), base_dir=data_dir,
                           static_dir=ROOT / "frontend/dist", instance_lock=False)
    report: list[dict] = []
    try:
        for index, (title, question) in enumerate(CASES):
            session = runtime.service.create_session(title=title, workspace_root=data_dir, mode="agent")
            print(f"开始真实模型验收：{title}", flush=True)
            result = runtime.service.submit_message(session.session_id, question, f"web-check-{index}")
            snapshot = runtime.service.get_session_snapshot(session.session_id)
            # 仅保存公开快照；不保存配置、密钥、原始模型请求或私有数据库。
            report.append({"case": title, "command_status": result.status,
                           "snapshot": snapshot.model_dump(mode="json")})
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"已记录：{title}，状态 {snapshot.session_state}", flush=True)
        if args.serve:
            print(f"隔离验收页面：http://127.0.0.1:{args.port}", flush=True)
            uvicorn.run(create_app(runtime), host="127.0.0.1", port=args.port, log_level="warning")
    finally:
        runtime.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
