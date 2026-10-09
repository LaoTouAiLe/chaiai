"""T34 真实基线采集：固定场景跑一遍真实模型，逐次调用保存供应商实际上报的用量。

为什么需要这个脚本（而不是"跑一次 pytest 就算数"）：T34 的"真实基线"要求的是一份**可重跑**的
证据——固定模型、端点、工作区和工具集，跑一条四步多轮场景，逐调用保存 uncached / cache-read /
output、端点与工具 Schema 指纹，以及发生压缩的位置。T35 的真实验收要"重跑 T34 场景"并给出
优化前后对照，所以场景本身必须留在仓库里，不能是一次性脚本。

隔离口径（都不该碰用户的东西）：

- 数据目录默认是临时目录，配置由 ``config.yaml.example`` 派生并**覆盖 llm 段为真实 ``config.yaml``
  的那一份**（模型、端点以真实配置为准，不在这里另写一套）；
- ``model_settings.credential_store`` 强制 ``memory``：绝不写系统凭据存储；
- ``instance_lock=False``：它不占用户单实例的位置；
- 密钥只从 ``.env`` 读进本进程环境变量，**不打印、不落盘**。

用法::

    uv run python scripts/measure_usage_baseline.py --data E:/tmp/t34-baseline
    uv run python scripts/measure_usage_baseline.py            # 用临时目录
    uv run python scripts/measure_usage_baseline.py --profile researcher   # T51：按 Profile 各跑一遍

T51 起多了 ``--profile``：同一份固定场景在 ``coder`` / ``researcher`` / ``reviewer`` 上各跑一遍，
再用 ``scripts/report_prefix_cache.py`` 看逐调用缓存回落与工具 Schema 指纹的变化。**换 Profile 就是换
工具集、也就是换前缀**，所以跨 Profile 的两次运行之间不做增量比较，各自看自己那一份。

T56 多了 ``--task-set profile``：跑 `local_agent.profile_tasks` 里三档各自的**固定任务集**，
逐条按有效能力集决定跑或跳过（默认 `web_access.enabled=false` 时，联网那条会被跳过并写明原因），
并把"这一轮有没有改工作区"记进结果——只读档改了工作区（或写类档没改）会以退出码 4 拦下。
``--task-set baseline``（默认）仍是 T34 的那四轮，历史基线靠它可比。

``--list`` 只打印选定任务集与逐轮判定，不联网、不调用模型（用来在跑真模型之前核对）。

逐轮记进结果的是：状态与错误、审批次数、**本轮新增的截断轨迹**（模型说了什么——只读档"如实说
做不到"这类判据只能从这里看出来）、用量与缓存回落，以及工作区有没有被改动。

退出码：0 成功；2 缺密钥 / 缺配置 / Profile 名不存在；3 场景里出现了未预期的终态；4 固定任务集的
写入预期未达成（见 stderr）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]

#: 固定场景：四步多轮。第 1 轮期望工具（读文件）、第 2 轮纯聊天、第 3 轮写新文件、第 4 轮纯聊天。
#: 文本改掉就等于换了基线，**改前先想清楚**：T35 的对照要求重跑的就是这一份。
TURNS: tuple[str, ...] = (
    "请读取 notes.md，然后用一句话说明这份笔记的主题。",
    "它第一条内容是什么？只回答那一条，不要读文件。",
    "把 notes.md 原样复制一份为 notes_copy.md。",
    "用一句话总结你在这几轮里做了哪三件事。",
)

#: 工作区种子文件。内容固定（含固定的 AGENTS.md），这样系统前缀在轮次之间不会因为种子变化而变。
NOTES_MD = """# T34 基线笔记

1. 这份笔记用于 T34 的真实用量基线。
2. 模型、端点、工作区与工具集在四轮之间保持不变。
3. 观测点是供应商在 usage 里上报的实际 token 与缓存命中。
4. 没有命中也要如实记录，不省略、不改写。
"""

AGENTS_MD = """# 工作区约定

- 只在本工作区内读写文件。
- 回答保持简短。
"""


def _load_env(path: Path) -> None:
    """把 ``.env`` 里的键值放进当前进程环境（只补缺，不覆盖已有值）。"""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def _write_config(data_dir: Path, profile: str) -> Path:
    """由示例配置派生一份：llm 段换成真实 ``config.yaml`` 的，凭据存储改内存态，Profile 按参数固定。"""
    example_path = ROOT / "config.yaml.example"
    real_path = ROOT / "config.yaml"
    if not real_path.is_file():
        print(f"缺少真实配置：{real_path}", file=sys.stderr)
        raise SystemExit(2)
    text = yaml.safe_load(example_path.read_text(encoding="utf-8"))
    real = yaml.safe_load(real_path.read_text(encoding="utf-8"))
    text["llm"] = real["llm"]
    text["model_settings"] = dict(text.get("model_settings") or {})
    text["model_settings"]["credential_store"] = "memory"
    # T51：Profile 与密钥无关，显式写进派生配置，跑出来的基线才有"这是哪一档"的出处。
    text["profiles"] = {"active": profile}
    config_path = data_dir / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(text, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return config_path


def _seed_workspace(data_dir: Path, extra: Mapping[str, str] | None = None) -> None:
    """种下固定的种子文件（工作区根就是数据目录，示例配置里 ``workspace.root: "."``）。

    ``extra`` 是任务集自带的额外种子（例如 `researcher` 的 `reference.md`）：内容随任务集固定，
    因此跨次运行的基线仍然可比。
    """
    (data_dir / "notes.md").write_text(NOTES_MD, encoding="utf-8")
    (data_dir / "AGENTS.md").write_text(AGENTS_MD, encoding="utf-8")
    for name, text in (extra or {}).items():
        path = data_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _fingerprint(data_dir: Path, names: Sequence[str]) -> dict[str, str | None]:
    """逐轮核对的工作区指纹：**只算被点名的文件**。

    为什么不整个目录一起哈希：数据目录里同时住着 Checkpoint、会话索引与各记忆库，它们本来就每轮都在
    变——混进来之后"这一轮有没有改工作区"这个判定就没意义了（永远为真）。
    """
    out: dict[str, str | None] = {}
    for name in names:
        path = data_dir / name
        out[name] = None if not path.is_file() else hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    return out


#: 回答与工具输出的截断长度。验收要看"模型说了什么"，但不该把整段对话抄进仓库。
TRANSCRIPT_TEXT_LIMIT = 600
TRANSCRIPT_PREVIEW_LIMIT = 300


def _trim(value: Any, limit: int) -> Any:
    if not isinstance(value, str) or len(value) <= limit:
        return value
    return value[:limit] + f"…（已截断，原长 {len(value)}）"


def _message_row(message: Any) -> dict[str, Any]:
    """把一条展示消息裁成一行记录（不含推理正文，工具输出只留预览）。"""
    row = message.model_dump(mode="json")
    kind = row.get("kind")
    entry: dict[str, Any] = {"kind": kind, "message_id": row.get("message_id")}
    if kind in ("user", "assistant"):
        entry["text"] = _trim(row.get("text"), TRANSCRIPT_TEXT_LIMIT)
    elif kind == "tool":
        tool = row.get("tool") or {}
        entry["tool"] = {
            key: _trim(tool.get(key), TRANSCRIPT_PREVIEW_LIMIT)
            for key in ("tool_name", "status", "title", "output_preview")
            if key in tool
        }
    else:
        # 其余类型（记忆/错误行）不单独建模：留一份截断后的原文，够核对即可。
        entry["detail"] = _trim(json.dumps(row, ensure_ascii=False), TRANSCRIPT_PREVIEW_LIMIT)
    return entry


def _transcript(messages: Sequence[Any], seen: set[str]) -> list[dict[str, Any]]:
    """**本轮新增**的消息轨迹（截断）。

    为什么必须记：固定任务集的验收点常常就在"模型自己怎么说的"——只读档被要求改文件时，正确结果
    是**如实说做不到**，而状态码只说得出"这一轮没失败"。只按 `seen` 收新增，避免每轮把整段会话
    重复抄一遍。
    """
    return [
        _message_row(message) for message in messages if str(message.message_id) not in seen
    ]


def _dump(snapshot: Any) -> dict[str, Any]:
    """把快照裁成记录用得上的那几段（不把整份 messages 抄进基线）。"""
    usage = snapshot.usage
    return {
        "session_state": snapshot.session_state,
        "approval": None
        if snapshot.approval is None
        else {"state": snapshot.approval.state, "tool_name": snapshot.approval.tool_name},
        "context": snapshot.context.model_dump(mode="json"),
        "usage": None if usage is None else usage.model_dump(mode="json"),
        "model_calls": [call.model_dump(mode="json") for call in snapshot.model_calls],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="T34 真实基线采集（真实模型）")
    parser.add_argument("--data", default=None, help="数据/工作区目录；默认用临时目录")
    parser.add_argument(
        "--out",
        default=None,
        help="结果 JSON 的落盘路径；默认写到数据目录下的 baseline_result.json",
    )
    parser.add_argument(
        "--profile",
        default=None,
        help="Agent Profile 名（coder/researcher/reviewer）；默认用产品默认档",
    )
    parser.add_argument(
        "--no-probe",
        action="store_true",
        help=(
            "跳过启动前的能力检测。默认**做**检测：产品只在'流式能力已确认'时才流式，"
            "不检测就测不到首 Token 时间（ttft），等于少覆盖 T34 改动过的流式链路"
        ),
    )
    parser.add_argument(
        "--task-set",
        choices=("baseline", "profile"),
        default="baseline",
        help=(
            "跑哪一套任务：baseline = T34 的固定四轮（默认，历史基线靠它可比）；"
            "profile = v0.5 T56 的三档固定任务集（逐条按有效能力集决定跑或跳过）"
        ),
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="只打印选定任务集与逐轮判定（是否需要跳过），不联网、不调用模型",
    )
    args = parser.parse_args()

    _load_env(ROOT / ".env")
    llm_env = "DEEPSEEK_API_KEY"
    if not os.environ.get(llm_env, "").strip():
        print(f"环境里没有 {llm_env}：先确认 .env 存在且已填值", file=sys.stderr)
        return 2

    data_dir = (
        Path(args.data) if args.data is not None else Path(tempfile.mkdtemp(prefix="t34-baseline"))
    )
    data_dir.mkdir(parents=True, exist_ok=True)

    # 放在这里 import：路径与依赖都就绪后再导入，`--help` 不需要装齐环境。
    from local_agent.api.container import open_runtime
    from local_agent.capabilities import (
        DEFAULT_PROFILE,
        PROFILES,
        effective_capabilities,
        profile_names,
        tool_names_for,
    )
    from local_agent.config import load_config
    from local_agent.profile_tasks import PROFILE_TASK_SETS, ProfileTaskSet, Task, select_tasks
    from local_agent.tools.git_tools import git_availability

    profile = args.profile or DEFAULT_PROFILE
    if profile not in PROFILES:
        print(f"未知 Profile：{profile}（可用：{'、'.join(profile_names())}）", file=sys.stderr)
        return 2
    tool_names = tool_names_for("tool", PROFILES[profile])
    # 两套任务走**同一条执行路径**：T34 的四轮包成同一套 `Task` 结构，否则就会有两处会漂移的判定。
    baseline_set = ProfileTaskSet(
        profile="baseline",
        purpose="T34/T35 的固定四轮（文本冻结：改了就等于换了基线）",
        tasks=tuple(Task(text=text) for text in TURNS),
        # 第 3 轮会新建 notes_copy.md，所以两个文件都要在核对清单里（否则那一轮的"有没有改工作区"
        # 会报 False，记录的是不实的信息）。
        watched_files=("notes.md", "notes_copy.md"),
    )
    task_set = PROFILE_TASK_SETS[profile] if args.task_set == "profile" else baseline_set
    session_title = "T56 固定任务集" if args.task_set == "profile" else "T34 真实基线"
    # request id 必须带上任务集：同一个数据目录里换一套任务却沿用同一批 id，等于"同一请求不同内容"，
    # 产品会（正确地）按幂等冲突拒掉——2026-10-09 实测过一次，别再把产品行为当成跑器故障。
    request_prefix = "t56-profile" if args.task_set == "profile" else "t34-baseline"
    config_path = _write_config(data_dir, profile)
    _seed_workspace(data_dir, extra=task_set.seed_files)
    config = load_config(config_path)
    print(f"[profile] {profile} 可见工具：{'、'.join(tool_names)}", flush=True)

    # 能力门与运行时**同源**（同一个 `effective_capabilities`）：开关关着（默认的 web / git）时，
    # 那条注定做不到的任务会被跳过，并在结果里写清缺的是哪个能力——而不是跑出一次误导人的失败。
    plan = select_tasks(
        task_set,
        effective_capabilities(
            PROFILES[profile],
            web_access_enabled=config.web_access.enabled,
            git_enabled=config.git.enabled and git_availability().available,
        ),
    )
    print(f"[task-set] {args.task_set}：{task_set.purpose}", flush=True)
    for index, item in enumerate(plan, start=1):
        print(
            f"  第 {index} 轮[{'执行' if item.runs else '跳过'}]：{item.task.text}",
            flush=True,
        )
        if item.skip_reason is not None:
            print(f"          原因：{item.skip_reason}", flush=True)
    if args.list:
        return 0
    print(f"[task-set] 待执行 {sum(1 for item in plan if item.runs)} 轮 / 共 {len(plan)} 轮", flush=True)
    runtime = open_runtime(config, base_dir=data_dir, instance_lock=False)
    try:
        service = runtime.service
        capability: dict[str, Any] | None = None
        if not args.no_probe:
            # 真实用户的第一件事就是"测试连接"。能力检测的结果决定这一轮走不走流式，
            # 所以基线必须把它记下来（不记的话 ttft 全空，无法解释是"没流"还是"丢了数据"）。
            connection_id = service.default_model_connection()
            if connection_id:
                view = service.test_model_connection(connection_id)
                capability = {
                    "connection_id": view.connection_id,
                    "capabilities": [item.model_dump(mode="json") for item in view.capabilities],
                }
                print(f"[probe] {capability}", flush=True)
        summary = service.create_session(workspace_root=data_dir, title=session_title)
        session_id = summary.session_id
        turns: list[dict[str, Any]] = []
        watched = task_set.watched_files
        # 已记过的消息 id：每轮只收**新增**的那几条（快照带的是整条会话的展示轨迹）。
        seen_ids: set[str] = set()
        for index, item in enumerate(plan, start=1):
            if not item.runs:
                # 跳过也要留痕：否则"少跑了一轮"在结果里看不出来，会被当成"测过了且没问题"。
                turns.append(
                    {
                        "turn": index,
                        "input": item.task.text,
                        "skipped": True,
                        "skip_reason": item.skip_reason,
                        "task_note": item.task.note,
                    }
                )
                continue
            before = _fingerprint(data_dir, watched)
            try:
                result = service.submit_message(session_id, item.task.text, f"{request_prefix}-{index}")
            except Exception as exc:  # noqa: BLE001 - 产品级拒绝也要**记成一条结果**，不要只留 traceback
                # 例如幂等冲突：跑器沿用旧 id 提交了不同内容。这种时候最该留下的信息是"哪一轮、因为什么"，
                # 而不是一屏栈——栈会让读结果的人以为是产品坏了。
                print(f"[turn {index}] 提交被拒绝：{type(exc).__name__}: {exc}", file=sys.stderr)
                turns.append(
                    {
                        "turn": index,
                        "input": item.task.text,
                        "skipped": False,
                        "task_note": item.task.note,
                        "result": {
                            "status": "failed",
                            "error": {"type": type(exc).__name__, "message": str(exc)},
                        },
                        "approvals": 0,
                        "workspace_changed": before != _fingerprint(data_dir, watched),
                        "write_expectation_met": None,
                        "transcript": [],
                    }
                )
                continue
            # 需要人工审批的操作按"允许一次"放行；这本身也是基线的一部分（审批等待耗时）。
            approves = 0
            while result.status == "awaiting_approval" and approves < 5:
                snapshot = service.get_session_snapshot(session_id)
                if snapshot.approval is None:
                    break
                approves += 1
                result = service.resolve_approval(
                    session_id,
                    snapshot.approval.approval_id,
                    "approve",
                    f"{request_prefix}-{index}-approve-{approves}",
                )
            snapshot = service.get_session_snapshot(session_id)
            transcript = _transcript(snapshot.messages, seen_ids)
            seen_ids.update(str(message.message_id) for message in snapshot.messages)
            changed = before != _fingerprint(data_dir, watched)
            turns.append(
                {
                    "turn": index,
                    "input": item.task.text,
                    "skipped": False,
                    "task_note": item.task.note,
                    # 本轮的截断轨迹：验收要看模型**说了什么**（例如只读档有没有如实说做不到）。
                    "transcript": transcript,
                    "result": {
                        "status": result.status,
                        "run_id": result.run_id,
                        "snapshot_id": result.snapshot_id,
                        "replayed": result.replayed,
                        "error": None
                        if result.error is None
                        else result.error.model_dump(mode="json"),
                    },
                    "approvals": approves,
                    # 固定任务集的机械验收点：这一轮**是否**改了工作区（只读档必须为 False）。
                    # 只算被点名的文件（`watched`）——数据目录里的 Checkpoint 与各库本来就每轮都在变。
                    "workspace_changed": changed,
                    "write_expectation_met": None
                    if args.task_set != "profile"
                    else changed is item.task.expects_write,
                    **_dump(snapshot),
                }
            )
            print(
                f"[turn {index}] status={result.status} "
                f"calls={len(snapshot.model_calls)} approvals={approves}",
                flush=True,
            )

        payload = {
            "model": config.llm.model if config.llm is not None else None,
            "base_url": config.llm.base_url if config.llm is not None else None,
            "streaming": config.llm.streaming if config.llm is not None else None,
            "workspace": str(data_dir),
            # T51：Profile 与它实际绑给模型的工具名——报告要能解释"工具 Schema 指纹为什么变了"。
            "profile": profile,
            "profile_tool_names": list(tool_names),
            "capability": capability,
            "turns": turns,
            "turns_count": len(turns),
            # T56：跑的是哪一套任务、跳过了哪几轮（跳过必须留痕，否则"少跑一轮"看不出来）。
            "task_set": args.task_set,
            "task_set_purpose": task_set.purpose,
            "turns_skipped": [turn["turn"] for turn in turns if turn.get("skipped") is True],
            "lines": "notes.md / AGENTS.md",
            "no_compaction_expected": True,
        }
        out_path = Path(args.out) if args.out is not None else data_dir / "baseline_result.json"
        out_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"数据目录：{data_dir}")
        print(f"结果：{out_path}")
        failed = [
            turn["turn"] for turn in turns if (turn.get("result") or {}).get("status") == "failed"
        ]
        if failed:
            print(f"有轮次失败：{failed}", file=sys.stderr)
            return 3
        # T56 的机械验收点：只读档**不得**改动工作区，写类任务**必须**真的改动。
        # 达不到就是档位边界或提示词出了问题（T51 的 reviewer 事件正是这一类）。
        deviations = [turn["turn"] for turn in turns if turn.get("write_expectation_met") is False]
        if deviations:
            print(
                f"固定任务集的写入预期未达成：{deviations}"
                "（只读档改了工作区，或写类档没改——按 §16.1 的能力边界核对）",
                file=sys.stderr,
            )
            return 4
        return 0
    finally:
        runtime.close()


if __name__ == "__main__":
    raise SystemExit(main())