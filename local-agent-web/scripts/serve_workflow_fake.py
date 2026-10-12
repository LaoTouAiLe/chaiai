"""T69 浏览器验收服务：临时工作区、确定性模型与真实 API/工具，无供应商调用。"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from test_code_workflow import ProposalModel

from local_agent.api.app import create_app
from local_agent.api.container import open_runtime
from local_agent.config import WorkflowsConfig
from local_agent.graph.code_workflow import _INSTRUCTION
from local_agent.workflow_eval import CODE_CASE, READ_CASE, _config


class WorkflowModel(ProposalModel):
    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.body = json.dumps(
            {"old_text": "    return value\n", "new_text": "    return min(upper, max(lower, value))\n"}
            if messages[0].content == _INSTRUCTION else {"pattern": "*.md", "keywords": []})
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8147)
    args = parser.parse_args()
    os.environ["WORKFLOW_EVAL_UNUSED"] = "offline-placeholder"
    with TemporaryDirectory(prefix="chaiai-v07-t69-browser-") as directory:
        root = Path(directory)
        for path, content in (*READ_CASE.seeds, *CODE_CASE.seeds):
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content.encode())
        config = _config(root, "coder")
        config.workflows = WorkflowsConfig(enabled=True)
        config.security.large_edit.max_changed_lines_without_approval = 1
        config.memory.short_term.enabled = True
        config.memory.short_term.path = str(root / "data" / "checkpoints.sqlite")
        runtime = open_runtime(config, base_dir=root, model=WorkflowModel(messages=iter([])),
                               static_dir=ROOT / "frontend" / "dist", instance_lock=False)
        try:
            uvicorn.run(create_app(runtime), host="127.0.0.1", port=args.port, log_level="warning")
        finally:
            runtime.service._watchdog_stop.set()
            runtime.service._jobs.close()
            runtime.close()


if __name__ == "__main__":
    main()
