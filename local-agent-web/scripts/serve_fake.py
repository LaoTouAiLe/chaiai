"""确定性假模型服务（T30 端到端专用；**不是**产品入口）。

为什么需要它：计划要求「Playwright 使用确定性假模型」，而正式入口（`python -m local_agent.api`）
只会按连接构造**真实**客户端（§15.9 的口径：完成首个有效连接后立即建立），仓库里没有任何"假模型开关"。
这里用测试里那套 `RoutingFakeModel`（与 pytest 共用同一份确定性实现）、起一个真 uvicorn，并且
**同源托管前端产物**——生产就是这么跑的（§6.2），所以端到端只有一个地址，不需要代理配置。

隔离口径（都不该碰用户的东西）：
- 数据目录默认是临时目录；配置从 `config.yaml.example` 复制一份过去，相对路径因此全部落在那里
  （聊天库、记忆库、设置库）；
- `model_settings.credential_store` 强制 `memory`：**绝不写系统凭据存储**；
- `instance_lock=False`：它服务的是测试，不该占住用户单实例的位置。

用法：
    uv run python scripts/serve_fake.py --port 8123 --dist frontend/dist --reply "固定回答"
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import uvicorn

ROOT = Path(__file__).resolve().parents[1]
#: 复用测试里的确定性假模型（同一份实现，避免"端到端看的是另一套行为"）。
sys.path.insert(0, str(ROOT / "tests"))

from conftest import make_model

from local_agent.api.app import create_app
from local_agent.api.container import open_runtime
from local_agent.config import load_config
from local_agent.model_settings import CapabilityReport

#: 假密钥走一个专用变量名，避免和真实 Key 的环境变量混在一起。
FAKE_KEY_ENV = "LOCAL_AGENT_FAKE_KEY"

#: 审批端到端（T32）用的两条脚本化输入；页面上按这两句话分别触发"批准"与"拒绝"。
APPROVAL_INPUTS = ("T32_APPROVE", "T32_REJECT")
#: 种子文件的行数，以及这一轮会被替换掉的行数。
APPROVAL_SEED_LINES = 300
APPROVAL_REPLACED_LINES = 201
APPROVAL_NEW_TEXT = "已批准的新内容"


def _approval_tool_calls(workspace: Path) -> dict[str, list[dict[str, Any]]]:
    """种下一份文件，并为两条输入各准备一次"必须人工审批"的大范围编辑。

    为什么是 201 行替换成 1 行：``security.large_edit.max_changed_lines_without_approval`` 的出厂值
    是 200，改动行数是 202，**真实地**越过免审上限——阈值本身仍然由产品代码裁决，这里只是照着它
    构造一个确定性的输入，不在端到端里放宽任何安全配置。

    ``expected_sha256`` 必须与磁盘上的字节逐字节一致（``edit_file`` 会校验），所以这里自己写字节、
    自己算 Hash；用 ``write_bytes`` 而不是 ``write_text`` 也是为了不让 Windows 把 ``\\n`` 改写成
    ``\\r\\n``（那会让 ``old_text`` 匹配不上真实内容）。
    """
    lines = [f"line {index}" for index in range(1, APPROVAL_SEED_LINES + 1)]
    payload = ("\n".join(lines) + "\n").encode("utf-8")
    seed = workspace / "notes.txt"
    seed.write_bytes(payload)

    arguments = {
        "path": seed.name,
        "old_text": "\n".join(lines[:APPROVAL_REPLACED_LINES]),
        "new_text": APPROVAL_NEW_TEXT,
        "expected_sha256": hashlib.sha256(payload).hexdigest(),
        "reason": "T32 端到端：大范围修改需要人工审批",
    }
    return {
        text: [{"name": "edit_file", "args": dict(arguments), "id": f"call-{text.lower()}"}]
        for text in APPROVAL_INPUTS
    }


def _write_config(data_dir: Path, *, dist: Path, streaming: bool) -> Path:
    """把示例配置复制到数据目录，再改四处：密钥变量名、静态目录、凭据存储、是否流式。"""
    example = (ROOT / "config.yaml.example").read_text(encoding="utf-8")
    example = example.replace('api_key_env: "DEEPSEEK_API_KEY"', f'api_key_env: "{FAKE_KEY_ENV}"')
    example = example.replace('static_dir: "frontend/dist"', f'static_dir: "{dist.as_posix()}"')
    example = example.replace('credential_store: "auto"', 'credential_store: "memory"')
    if streaming:
        # 端到端要覆盖两条路径（增量与非流式降级），所以由旗标决定，不在脚本里写死。
        example = example.replace(
            '  streaming: false',
            '  streaming: true',
        )
    config_path = data_dir / "config.yaml"
    config_path.write_text(example, encoding="utf-8")
    return config_path


def main() -> int:
    parser = argparse.ArgumentParser(description="确定性假模型服务（T30 端到端专用）")
    parser.add_argument("--port", type=int, default=8123)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--dist", default="frontend/dist", help="前端产物目录（同源托管）")
    parser.add_argument("--data", default=None, help="数据目录；默认用临时目录")
    parser.add_argument("--reply", default="这是假模型的固定回答。", help="普通回答的文本")
    parser.add_argument("--decision", default="chat", help='路由分类结果：chat / tool / ...')
    parser.add_argument("--chunk-delay", type=float, default=0.0, help="假模型每个流式块的测试延迟（秒）")
    parser.add_argument("--variants", action="store_true", help="启用长/空/故障回答的页面验收输入")
    parser.add_argument(
        "--approval-edit",
        action="store_true",
        help=(
            "审批端到端（T32）：在工作区种一份 notes.txt，并让假模型按输入"
            f"（{' / '.join(APPROVAL_INPUTS)}）发出一次需要人工审批的大范围编辑"
        ),
    )
    parser.add_argument("--data-dir-name", default="local-agent-fake", help="临时数据目录前缀")
    parser.add_argument(
        "--streaming",
        action="store_true",
        help="打开最终回答的流式展示（覆盖增量路径；默认关，走非流式那条）",
    )
    args = parser.parse_args()

    dist = (ROOT / args.dist).resolve() if not Path(args.dist).is_absolute() else Path(args.dist)
    if not dist.is_dir():
        print(f"前端产物不存在：{dist}（先跑 npm run build）", file=sys.stderr)
        return 2

    data_dir = Path(args.data) if args.data is not None else Path(tempfile.mkdtemp(prefix=args.data_dir_name))
    data_dir.mkdir(parents=True, exist_ok=True)
    config_path = _write_config(data_dir, dist=dist, streaming=args.streaming)

    # 连接是从 llm 段幂等导入的，取密钥仍走它指向的环境变量：这里给个占位值，
    # 否则提交会正确地被 `MODEL_AUTH_FAILED` 拒绝（那是产品行为，但端到端要的是对话本身）。
    os.environ[FAKE_KEY_ENV] = "fake-key-for-e2e"

    config = load_config(config_path)
    # 数据目录就是工作区根（示例配置里 `workspace.root: "."`），种子文件因此落在工作区内。
    tool_calls_by_input = _approval_tool_calls(data_dir) if args.approval_edit else {}
    model: Any = make_model(
        reply=args.reply,
        decision=args.decision,
        stream_delay_seconds=args.chunk_delay,
        tool_calls_by_input=tool_calls_by_input,
        response_by_input={
            "T30_LONG": "这是长回答。" * 1500,
            "T30_EMPTY": "",
        } if args.variants else {},
        fail_on_input=["T30_ERROR"] if args.variants else [],
    )
    runtime = open_runtime(
        config,
        base_dir=data_dir,
        model=model,
        static_dir=dist,
        instance_lock=False,
    )
    if args.streaming:
        # 真实服务只有能力检测明确通过才会开流；假端点不联网，测试夹具在启动时给它
        # 一份确定性的流式通过记录。否则 config.yaml 的 streaming=true 仍会被未知能力门禁降级。
        if runtime.settings is None:
            raise RuntimeError("假模型测试需要设置库")
        for connection in runtime.settings.connections.list_connections():
            runtime.settings.connections.record_check(
                connection.connection_id,
                CapabilityReport(chat="pass", streaming="pass", checked_at=datetime.now(timezone.utc)),
                expected_version=connection.config_version,
            )

    print(f"数据目录：{data_dir}")
    print(f"前端目录：{dist}")
    print(f"监听：http://{args.host}:{args.port}")
    uvicorn.run(
        create_app(runtime),
        host=args.host,
        port=args.port,
        log_level="warning",
        timeout_keep_alive=config.web.keepalive_seconds,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
