"""Isolated real Graph/SSE service for long-task reading acceptance; no real model."""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests"), str(ROOT / "scripts")]

import uvicorn
from conftest import RoutingFakeModel
from langchain_core.messages import HumanMessage
from serve_fake import FAKE_KEY_ENV, _write_config

from local_agent.api.app import create_app
from local_agent.api.container import open_runtime
from local_agent.config import load_config
from local_agent.model_settings import CapabilityReport

STREAM_REPLY = "第一句回答已经开始。这里是持续生成的补充说明，会在后续块到来之前先展示给用户。第二句回答继续显示。这里继续提供更多可读内容，方便观察增量过程。第三句回答会在最后才出现。"
BACKGROUND_REPLY = "第一句后台回答已经开始。第二句后台回答继续生成。第三句后台回答在最后完成。"
LONG_REPLY = "## 长任务阅读验证\n\n" + "\n\n".join(
    f"第 {index:02d} 段：这是持续生成中的正文，用来核对段落、换行和滚动位置。阅读历史时，后续输出应继续保存，当前阅读位置应保持稳定。"
    for index in range(1, 61)
) + "\n\n长回答结束标记。"


class ReadingModel(RoutingFakeModel):
    """One controlled partial-stream error; recovery changes only the fake endpoint."""

    allow_recovery: bool = False
    stream_interrupted: bool = False

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        latest = next((item.text for item in reversed(messages) if isinstance(item, HumanMessage)), "")
        if latest == "FAIL_PERSISTENT" and self.stream_interrupted and not self.allow_recovery:
            raise RuntimeError("验收故障：普通调用重试也失败")
        return super()._generate(messages, stop, run_manager, **kwargs)

    def _stream_chunks(self, messages, stop=None, run_manager=None, **kwargs):
        latest = next((item.text for item in reversed(messages) if isinstance(item, HumanMessage)), "")
        for index, chunk in enumerate(super()._stream_chunks(messages, stop, run_manager, **kwargs)):
            if latest == "TOOL_LONG" and any(call.get("name") == "task_control" for call in chunk.message.tool_calls):
                time.sleep(2)
            if latest in {"FAIL_STREAM", "FAIL_PERSISTENT"} and not self.allow_recovery and index == 5:
                self.stream_interrupted = True
                raise RuntimeError("验收故障：流式输出中断")
            yield chunk


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--data", required=True)
    parser.add_argument("--scenario", choices=["streaming", "background", "reading"], required=True)
    args = parser.parse_args()
    data = Path(args.data).resolve()
    data.mkdir(parents=True, exist_ok=True)
    os.environ["CHAIAI_HOME"] = str(data / "home")
    os.environ[FAKE_KEY_ENV] = "fake-reading-acceptance"
    dist = ROOT / "frontend/dist"
    config = load_config(_write_config(data, dist=dist, streaming=True))
    config.jobs.background = args.scenario != "streaming"
    config.memory.long_term.enabled = False
    config.memory.compression.enabled = False
    config.memory.curated.enabled = False
    config.terminal.load_profile = False
    (data / "reading-seed.txt").write_text("隔离验收文件，只用于 read_file。\n" * 80, encoding="utf-8")
    (data / "command_progress_demo.py").write_text(
        "import time\nfor index in range(1, 6):\n"
        "    print(f'step-{index}', flush=True)\n    time.sleep(2)\n", encoding="utf-8",
    )
    tools = {
        "COMMAND_STREAM": [{"name": "run_shell", "id": "command-stream", "args": {
            "command": f'"{sys.executable}" -u command_progress_demo.py', "timeout_seconds": 30,
        }}],
        "TOOL_LONG": [{"name": "read_file", "id": "reading-file", "args": {"path": "reading-seed.txt"}}],
    }
    reply = STREAM_REPLY if args.scenario == "streaming" else BACKGROUND_REPLY
    model = ReadingModel(
        messages=iter(["fake"]), decision="chat", reply=reply,
        stream_chunks=60 if args.scenario == "reading" else 3,
        stream_delay_seconds=0.16 if args.scenario == "reading" else 0.7,
        explicit_task_control=True,
        tool_calls_by_input=tools,
        response_by_input={name: LONG_REPLY for name in ["LONG_STREAM", "HISTORY_STREAM", "TOOL_LONG", "FAIL_STREAM", "FAIL_PERSISTENT"]},
    )
    runtime = open_runtime(config, base_dir=data, model=model, static_dir=dist, instance_lock=False)
    if runtime.settings is None:
        raise RuntimeError("Isolated acceptance needs a settings store")
    for connection in runtime.settings.connections.list_connections():
        runtime.settings.connections.record_check(
            connection.connection_id,
            CapabilityReport(chat="pass", streaming="pass", checked_at=datetime.now(timezone.utc)),
            expected_version=connection.config_version,
        )
    app = create_app(runtime)

    @app.post("/__acceptance/reset")
    def reset():
        # The test adapter mutates its binding in place; real providers return a runnable.
        model.bound_tools = []
        model.scripted_inputs.clear()
        model.decision_calls = 0
        model.allow_recovery = False
        model.stream_interrupted = False
        return {"reset": True}

    @app.post("/__acceptance/recover")
    def recover():
        model.allow_recovery = True
        return {"recovered": True}

    try:
        uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    finally:
        runtime.close()


if __name__ == "__main__":
    main()
