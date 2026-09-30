"""人工审批：请求、决策与动作指纹（架构 §5.3、§6.5）。

设计要点：

- **动作指纹**按工具类型分别计算，用来防止"换一个 ``tool_call_id`` 重新提交"绕过拒绝记录；
- 拒绝按**指纹**匹配（换个调用 ID 也拦得住），批准只对**同一个 ``tool_call_id``**生效
  （避免一次批准变成永久放行）；
- 执行前会再次计算当前指纹并与批准时比对，目标状态变了就视为批准失效；
- 递归删除需要二次确认：第一次确认只推进到第二阶段，不构成授权。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, TypedDict
from uuid import uuid4

from local_agent.security.policy import SafetyDecision, SecurityPolicy, as_int
from local_agent.security.shell_guard import prepare_shell

ApprovalChoice = Literal["approve", "edit", "reject"]


class ApprovalRequest(TypedDict):
    """待审批动作（架构 §4.2 的 ``pending_approval``）。

    ``run_id`` 标明是哪一轮提出的请求：**批准**只对"这一轮的这一次调用"生效；
    拒绝记录按动作指纹匹配、有意跨轮保留（见 :func:`find_rejection`），两者不对称。
    """

    approval_id: str
    run_id: str
    tool_call_id: str
    tool_name: str
    action_fingerprint: str
    arguments: dict[str, Any]
    impact_summary: str
    requires_second_confirmation: bool
    created_at: datetime


class ApprovalDecision(TypedDict):
    """人工决策记录（架构 §4.2 的 ``approval_history``）。"""

    approval_id: str
    run_id: str
    tool_call_id: str
    action_fingerprint: str
    decision: ApprovalChoice
    edited_arguments: dict[str, Any] | None
    reason: str
    created_at: datetime
    requested_at: datetime


def build_request(
    *,
    tool_name: str,
    run_id: str,
    tool_call_id: str,
    arguments: Mapping[str, Any],
    decision: SafetyDecision,
    policy: SecurityPolicy,
) -> ApprovalRequest:
    """构造待审批请求。"""
    fingerprint = action_fingerprint(tool_name, arguments, policy=policy)
    return ApprovalRequest(
        approval_id=uuid4().hex,
        run_id=run_id,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        action_fingerprint=fingerprint,
        arguments={str(key): value for key, value in arguments.items()},
        impact_summary=describe_impact(tool_name, arguments, decision=decision, policy=policy),
        requires_second_confirmation=requires_second_confirmation(tool_name, arguments),
        created_at=datetime.now(timezone.utc),
    )


def build_decision(
    request: ApprovalRequest,
    *,
    choice: ApprovalChoice,
    edited_arguments: Mapping[str, Any] | None = None,
    reason: str = "",
) -> ApprovalDecision:
    """构造人工决策记录（沿用请求的轮次作用域）。"""
    return ApprovalDecision(
        approval_id=str(request["approval_id"]),
        run_id=str(request["run_id"]),
        tool_call_id=str(request["tool_call_id"]),
        action_fingerprint=str(request["action_fingerprint"]),
        decision=choice,
        edited_arguments=dict(edited_arguments) if edited_arguments else None,
        reason=reason,
        created_at=datetime.now(timezone.utc),
        requested_at=request["created_at"],
    )


def action_fingerprint(
    tool_name: str,
    arguments: Mapping[str, Any],
    *,
    policy: SecurityPolicy,
) -> str:
    """按架构 §5.3 分工具计算动作指纹。"""
    args = dict(arguments or {})
    # 工作区根目录必须是指纹的一部分：同一个相对路径在不同工作区是两个不同的动作，
    # 否则"在 A 里批准过"的指纹会复用到 B（架构 §1.5、T14C）。
    parts: list[str] = [tool_name, policy.workspace_root.as_posix()]

    if tool_name in {"edit_file", "write_file"}:
        path = _normalized_path(policy, str(args.get("path") or ""))
        # 影响执行语义的参数一个都不能漏：`replace_all` 决定改动一处还是全部，
        # `overwrite` 决定覆盖还是新建，`patch`（T22 的第二输入模式）决定到底改成什么
        # ——漏掉任何一个都会让不同的动作共用同一个指纹（架构 §5.3）。
        # 布尔量统一按 True/False 归一、文本统一按空串归一：调用方省略与显式传默认值
        # 必须得到同一个指纹，否则批准会因为"少写一个默认值"莫名失效。
        change = json.dumps(
            {
                "old_text": _text(args.get("old_text")),
                "new_text": _text(args.get("new_text")),
                "patch": _text(args.get("patch")),
                "replace_all": bool(args.get("replace_all")),
                "content": args.get("content"),
                "overwrite": bool(args.get("overwrite")),
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        parts += [path, _file_digest(path), _hash_text(change)]
    elif tool_name == "delete_file":
        path = _normalized_path(policy, str(args.get("path") or ""))
        parts += [path, str(bool(args.get("recursive"))), _delete_target_summary(path)]
    elif tool_name == "run_shell":
        # 指纹要覆盖"实际会被执行的计划"：命令、工作目录、透传变量、执行方式，
        # 以及判定阶段解析出的可执行文件路径。这样批准后若 PATH 变化导致同一个命令
        # 会落到别的程序上，指纹随之变化、批准自动失效（架构 §5.3 的执行前复核）。
        cwd = _normalized_path(policy, str(args.get("cwd") or "."))
        env_keys = sorted(str(key) for key in (args.get("env_keys") or []))
        raw_command = str(args.get("command") or "")
        # 超时也是执行计划的一部分：批准"1 秒内跑完"与批准"120 秒内跑完"不是同一个动作。
        # 归一规则与 SecurityPolicy.classify_tool 一致；省略时取配置值（与安全判定、
        # 工具执行同一语义），而不是配置的出厂常量 60。
        requested_timeout = as_int(args.get("timeout_seconds"))
        if requested_timeout is None:
            requested_timeout = policy.security.shell.default_timeout_seconds
        plan = prepare_shell(
            raw_command,
            config=policy.security.shell,
            workspace_root=policy.workspace_root,
            cwd=str(args.get("cwd") or "."),
            timeout_seconds=requested_timeout,
            env_keys=[str(key) for key in (args.get("env_keys") or [])],
            # 与安全判定/执行同一套 cwd 语义：档位认可的外部 cwd 才解析得出可执行文件，
            # 否则指纹里的 executable_path 会与执行阶段不一致（架构 §5.3）。
            external_policy=policy.external_policy,
        )
        parts += [
            # 原始命令原文必须进指纹：`split_command` 会丢掉管道/重定向等 Shell 运算符，
            # 只比切出来的 argv 会让 `echo hi > f` 与 `echo hi < f` 撞成同一个指纹。
            raw_command,
            " ".join(plan.argv),
            cwd,
            ",".join(env_keys),
            str(plan.timeout_seconds),
            plan.executor,
            plan.executable_path or "",
        ]
    else:
        parts.append(
            json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)
        )

    return _hash_text("|".join(parts))


def describe_impact(
    tool_name: str,
    arguments: Mapping[str, Any],
    *,
    decision: SafetyDecision,
    policy: SecurityPolicy,
) -> str:
    """给用户看的影响范围说明（架构 §6.5 要求显示绝对路径与影响范围）。

    工作区外目标必须一眼可见（架构 §1.5）：加前缀标记，并在说明里带上规范化绝对路径。
    """
    args = dict(arguments or {})
    path_text = _normalized_path(policy, str(args.get("path") or "")) if "path" in args else ""

    if tool_name == "delete_file":
        recursive = bool(args.get("recursive"))
        marker = _external_marker(policy, path_text)
        return (
            f"{marker}删除 {path_text}（{_delete_target_summary(path_text)}）"
            f"{'，递归删除' if recursive else ''}"
        )
    if tool_name == "run_shell":
        cwd_text = _normalized_path(policy, str(args.get("cwd") or "."))
        marker = _external_marker(policy, cwd_text)
        return f"{marker}执行命令：{args.get('command')}（工作目录 {cwd_text}）"
    if tool_name in {"edit_file", "write_file"}:
        marker = _external_marker(policy, path_text)
        return f"{marker}写入 {path_text}：{decision.reason}"
    return f"{tool_name}：{decision.reason}"


def _external_marker(policy: SecurityPolicy, path_text: str) -> str:
    """工作区外目标的审批标记（空字符串表示在工作区内）。"""
    if not path_text:
        return ""
    return "【工作区外路径】" if policy.is_external(Path(path_text)) else ""


def requires_second_confirmation(tool_name: str, arguments: Mapping[str, Any]) -> bool:
    """递归删除必须二次确认（架构 §6.5）。"""
    return tool_name == "delete_file" and bool(dict(arguments or {}).get("recursive"))


def find_rejection(
    history: Sequence[Mapping[str, Any]], fingerprint: str
) -> Mapping[str, Any] | None:
    """按指纹找拒绝记录：换一个 ``tool_call_id`` 也不能绕过。

    与批准（:func:`find_approval`，按轮次隔离）不同，拒绝**故意不按轮次隔离**：
    被拒绝过的动作指纹在后续轮次仍会被拦下。这是"拒绝后不再重复打扰用户"的兜底，
    对应开发文档 T10 验收与 v0.1b 阶段演示的"拒绝后文件未删且不再次弹窗"。
    代价：用户若在下一轮仍然想执行**完全相同**的动作，不会被静默执行，也不会再次弹窗。
    """
    for record in reversed(list(history)):
        if record.get("decision") == "reject" and record.get("action_fingerprint") == fingerprint:
            return record
    return None


def find_approval(
    history: Sequence[Mapping[str, Any]],
    *,
    run_id: str,
    tool_call_id: str,
    fingerprint: str,
) -> Mapping[str, Any] | None:
    """按**轮次 + 调用 ID + 指纹**找批准记录：批准只对同一次调用生效。

    轮次是作用域的一部分：模型在另一轮复用了同一个 ``tool_call_id`` 时，
    上一轮的批准不能被沿用（架构 §5.3）。
    """
    for record in reversed(list(history)):
        if record.get("decision") != "approve":
            continue
        # 缺 run_id 的历史记录（升级前的 Checkpoint）一律不认，宁可重新审批
        if str(record.get("run_id") or "") != run_id:
            continue
        if record.get("tool_call_id") != tool_call_id:
            continue
        if record.get("action_fingerprint") == fingerprint:
            return record
    return None


#: 删除目标摘要允许扫描的条目上限：超过就停止并标注 ``capped``。
#: 摘要只用于审批展示与指纹，不该对大目录（更不该对用户主目录）做无界递归。
DELETE_SUMMARY_SCAN_LIMIT = 500


def _normalized_path(policy: SecurityPolicy, raw: str) -> str:
    """尽量给出规范化绝对路径；越界或无路径时退回原字符串（指纹仍然稳定）。"""
    try:
        return policy.resolve_path(raw or ".").as_posix()
    except Exception:  # noqa: BLE001 — 指纹计算不因路径非法而失败
        return (raw or "").strip()


def _file_digest(path_text: str) -> str:
    """文件当前内容的 Hash；不存在或不可读时给出稳定标记。"""
    if not path_text:
        return "no-path"
    path = Path(path_text)
    try:
        if path.is_file():
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
            return digest.hexdigest()
    except OSError:
        return "unreadable"
    return "missing"


def _delete_target_summary(path_text: str) -> str:
    """删除目标的状态摘要：类型、大小、修改时间与父目录状态。

    目录只做**一次有上限的扫描**（:data:`DELETE_SUMMARY_SCAN_LIMIT` 条就停，并标注 ``capped``）：
    这个摘要只用于审批展示与动作指纹，对用户主目录这类大目录递归整棵树会让页面在等待审批时
    卡死（T14C 复审）。
    """
    if not path_text:
        return "no-path"
    path = Path(path_text)
    try:
        if not path.exists():
            return "missing"
        stat = path.stat()
        kind = "dir" if path.is_dir() else "file"
        parent = path.parent
        parent_state = f"parent-exists:{parent.is_dir()}"
        count = ""
        if path.is_dir():
            files = 0
            dirs = 0
            for item in path.rglob("*"):
                if item.is_dir():
                    dirs += 1
                elif item.is_file():
                    files += 1
                if files + dirs >= DELETE_SUMMARY_SCAN_LIMIT:
                    count = f"files:{files},dirs:{dirs}+,capped"
                    break
            else:
                count = f"files:{files},dirs:{dirs}"
        return f"{kind}:{stat.st_size}:{int(stat.st_mtime)}:{parent_state}:{count}"
    except OSError:
        return "unreadable"


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _text(value: Any) -> str:
    """把参与指纹的文本参数归一成字符串：省略 / ``None`` 与显式空串必须等价。

    不归一的话，"省略 old_text" 与 "显式传 ``old_text=""``" 会算出两个指纹，
    同一个动作的批准会莫名其妙失效（架构 §5.3）。
    """
    return value if isinstance(value, str) else ""
