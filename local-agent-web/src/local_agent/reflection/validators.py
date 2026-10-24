"""工具结果的机械校验（架构 §11.2）。

设计要点：**证据是否存在由代码判定，不由模型自述决定**。``evidence_level`` 只有
机械校验通过才是 ``verified``，否则保持 ``uncertain`` 或 ``failed``。

T05 覆盖只读工具；``exit_code_ok``、``file_hash_changed_as_expected``、
``diff_non_empty`` 由 T06/T07 的写操作与 Shell 填上（当前保持 ``None``）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from local_agent.graph.state import ToolEvent
from local_agent.tools.result_types import ToolResultRecord

EvidenceLevel = Literal["verified", "uncertain", "failed"]


@dataclass(frozen=True)
class ToolEvidence:
    """单个工具事件的机械校验结果（架构 §11.2）。"""

    status_ok: bool
    has_output: bool
    evidence_level: EvidenceLevel
    exit_code_ok: bool | None = None
    file_hash_changed_as_expected: bool | None = None
    diff_non_empty: bool | None = None
    reasons: tuple[str, ...] = field(default_factory=tuple)


def validate_tool_event(
    event: ToolEvent,
    result: ToolResultRecord,
    *,
    payload: Mapping[str, Any] | None = None,
) -> ToolEvidence:
    """校验一次工具调用是否产生了可用证据。

    Args:
        event: State 中的工具事件。
        result: 对应的工具结果记录。
        payload: 工具返回的完整结构化内容（能解析出 JSON 时提供）。编辑类工具靠它判断
            "文件 Hash 是否真的变了""diff 是否非空"（架构 §11.2）。

    Returns:
        校验结果；``reasons`` 说明判定依据，供反思节点与用户界面使用。
    """
    status_ok = event.status == "ok" and result["status"] == "ok"
    content = result["output_preview"].strip()
    has_output = bool(content) and content not in {"{}", "[]"}

    exit_code_ok: bool | None = None
    file_hash_changed: bool | None = None
    diff_non_empty: bool | None = None
    if payload:
        if "exit_code" in payload:
            exit_code = payload.get("exit_code")
            if isinstance(exit_code, int):
                exit_code_ok = exit_code == 0
            # Shell 的“有输出”看真实 stdout/stderr，而不是 JSON 外壳
            stdout = payload.get("stdout")
            stderr = payload.get("stderr")
            if isinstance(stdout, str) or isinstance(stderr, str):
                has_output = bool(str(stdout or "").strip() or str(stderr or "").strip())
        if payload.get("is_binary") is True:
            # 二进制文件没有可用内容，不能当作证据
            has_output = False
        old_hash = payload.get("old_sha256")
        new_hash = payload.get("new_sha256")
        if isinstance(old_hash, str) and isinstance(new_hash, str):
            file_hash_changed = old_hash != new_hash
        diff = payload.get("diff")
        if isinstance(diff, str):
            diff_non_empty = bool(diff.strip())

    reasons: list[str] = []
    if not status_ok:
        # 区分"**被拒绝、没有执行**"与"执行了却拿不到产出"：前者根本不是执行失败（2026-10-14）。
        # 汇总处（`graph/nodes.py` 的机械校验）据此把被拒事件排除在证据之外，不再污染整轮可信度。
        if result["status"] == "denied":
            reasons.append("调用被拒绝、没有执行（不是执行失败），没有可验证产出")
        else:
            reasons.append(f"工具状态为 {result['status']}，没有可验证的产出")
        return ToolEvidence(
            status_ok=False,
            has_output=has_output,
            evidence_level="failed",
            exit_code_ok=exit_code_ok,
            file_hash_changed_as_expected=file_hash_changed,
            diff_non_empty=diff_non_empty,
            reasons=tuple(reasons),
        )

    if payload and (payload.get("timed_out") is True or payload.get("cancelled") is True):
        return ToolEvidence(
            status_ok=status_ok, has_output=has_output, evidence_level="failed",
            exit_code_ok=exit_code_ok, reasons=("命令超时或被取消，不能作为成功执行的依据",),
        )

    if exit_code_ok is False:
        reasons.append(f"命令退出码为 {payload.get('exit_code') if payload else '未知'}，执行失败")
        return ToolEvidence(
            status_ok=True,
            has_output=has_output,
            evidence_level="failed",
            exit_code_ok=False,
            file_hash_changed_as_expected=file_hash_changed,
            diff_non_empty=diff_non_empty,
            reasons=tuple(reasons),
        )

    if exit_code_ok is None and payload is not None and "exit_code" in payload:
        reasons.append("命令没有退出码（超时或被终止），结果不确定")
        return ToolEvidence(
            status_ok=True,
            has_output=has_output,
            evidence_level="failed",
            exit_code_ok=None,
            file_hash_changed_as_expected=file_hash_changed,
            diff_non_empty=diff_non_empty,
            reasons=tuple(reasons),
        )

    if not has_output and not (event.tool_name == "run_shell" and exit_code_ok is True):
        reasons.append("工具返回为空，无法确认结果")
        return ToolEvidence(
            status_ok=True,
            has_output=False,
            evidence_level="uncertain",
            exit_code_ok=exit_code_ok,
            file_hash_changed_as_expected=file_hash_changed,
            diff_non_empty=diff_non_empty,
            reasons=tuple(reasons),
        )

    if file_hash_changed is False:
        reasons.append("修改前后文件 Hash 未变化，改动可能没有生效")
        return ToolEvidence(
            status_ok=True,
            has_output=True,
            evidence_level="failed",
            exit_code_ok=exit_code_ok,
            file_hash_changed_as_expected=False,
            diff_non_empty=diff_non_empty,
            reasons=tuple(reasons),
        )

    if diff_non_empty is False:
        reasons.append("diff 为空，没有实际改动")
        return ToolEvidence(
            status_ok=True,
            has_output=True,
            evidence_level="failed",
            exit_code_ok=exit_code_ok,
            file_hash_changed_as_expected=file_hash_changed,
            diff_non_empty=False,
            reasons=tuple(reasons),
        )

    if file_hash_changed:
        reasons.append("文件 Hash 已变化")
    if diff_non_empty:
        reasons.append("diff 非空")
    reasons.append("命令正常退出（无输出）" if not has_output else "工具执行成功且返回了内容")
    return ToolEvidence(
        status_ok=True,
        has_output=has_output,
        evidence_level="verified",
        exit_code_ok=exit_code_ok,
        file_hash_changed_as_expected=file_hash_changed,
        diff_non_empty=diff_non_empty,
        reasons=tuple(reasons),
    )


def summarize_trust(evidences: list[ToolEvidence]) -> EvidenceLevel:
    """把多个证据的等级汇总成一个整体可信度。

    - 全部 ``verified`` → ``verified``
    - 全部 ``failed``（且至少一个） → ``failed``
    - 其余 → ``uncertain``
    """
    if not evidences:
        return "uncertain"
    levels = {evidence.evidence_level for evidence in evidences}
    if levels == {"verified"}:
        return "verified"
    if levels == {"failed"}:
        return "failed"
    return "uncertain"
