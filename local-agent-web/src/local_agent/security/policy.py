"""安全策略服务：路径、敏感文件与工具放行的统一入口（架构 §6、§11.1）。

设计原则（架构 §1.3-4、§11.1）：危险动作在代码层拦截，不依赖 Prompt；
未实现、未放行或需要审批而本版本没有审批能力的动作，一律失败闭合（deny）。

放行清单：

- ``list_dir`` / ``read_file``：只读，直接放行；
- ``edit_file``：精确串替换或 unified diff（`patch`，架构 §6.3），改动行数在
  ``security.large_edit`` 免审范围内时放行，超过上限视为大范围修改 → 需要审批
  （T10 之前拒绝）；结构非法的 patch 直接硬拒绝，不进审批；
- ``write_file``：只允许创建新文件；覆盖已有文件需要审批（T10 之前拒绝）；
- ``run_shell``：只有命中 ``security.shell.whitelist`` 的命令免审批，其余需要审批；
- ``delete_file``：始终需要审批（T10 之前拒绝）。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from local_agent.config import (
    AppConfig,
    SecurityConfig,
    WorkspaceConfig,
    redact_secrets,
)
from local_agent.exceptions import SecurityError, WorkspaceBoundaryError
from local_agent.security.diff_guard import count_changed_lines, edit_input_mode, parse_patch
from local_agent.security.path_guard import (
    assert_not_protected_target,
    is_sensitive_path,
    is_within,
    protected_target_reason,
    resolve_target,
)
from local_agent.security.shell_guard import prepare_shell

#: 只读工具。
READ_ONLY_TOOLS: tuple[str, ...] = ("list_dir", "read_file")

#: 写入类工具（本版本只放行其中的安全子集）。
WRITE_TOOLS: tuple[str, ...] = ("edit_file", "write_file")

#: Shell 工具（按结构化白名单判定）。
SHELL_TOOLS: tuple[str, ...] = ("run_shell",)

#: 始终需要人工审批的工具。
ALWAYS_APPROVAL_TOOLS: tuple[str, ...] = ("delete_file",)

#: 本版本已实现、可绑定给模型的全部工具。
KNOWN_TOOLS: tuple[str, ...] = (
    READ_ONLY_TOOLS + WRITE_TOOLS + SHELL_TOOLS + ALWAYS_APPROVAL_TOOLS
)


@dataclass(frozen=True)
class SafetyDecision:
    """一次工具调用的安全判定结果。"""

    allowed: bool
    requires_approval: bool
    reason: str


class SecurityPolicy:
    """工作区级的路径与工具策略。"""

    def __init__(
        self,
        *,
        workspace_root: Path,
        security: SecurityConfig,
        workspace: WorkspaceConfig,
        secret_values: tuple[str, ...] = (),
    ) -> None:
        self.workspace_root = workspace_root
        self.security = security
        self.workspace = workspace
        #: ``workspace.allow_external_paths``：是否允许**显式绝对路径**访问工作区外目标。
        #: 为真不等于关闭安全校验：越界判定、受保护目标、外部副作用强制审批都照旧生效。
        self.allow_external_paths = bool(workspace.allow_external_paths)
        self._secret_values = secret_values
        #: 本次执行中被人工批准的动作指纹（一次性授权，执行完立即撤销）。
        self._granted: set[str] = set()

    @classmethod
    def from_config(
        cls,
        app_config: AppConfig,
        *,
        workspace_root: Path,
        environ: Mapping[str, str] | None = None,
    ) -> SecurityPolicy:
        """按配置构造策略；密钥值从环境变量读取，仅用于输出脱敏。

        Args:
            app_config: 完整配置。
            workspace_root: 工作区根目录。
            environ: 环境变量映射，默认取 ``os.environ``。

        Returns:
            构造好的策略对象。``workspace.allow_external_paths`` 只是打开"显式绝对路径的受控
            外部访问"（架构 §1.5），不放松越界判定本身。
        """
        env = os.environ if environ is None else environ
        secret_names = [
            name
            for name in (app_config.llm.api_key_env, app_config.embedding.api_key_env)
            if name
        ]
        secret_values = tuple(
            value for name in secret_names if (value := env.get(name, "").strip())
        )
        return cls(
            workspace_root=workspace_root,
            security=app_config.security,
            workspace=app_config.workspace,
            secret_values=secret_values,
        )

    # ------------------------------------------------------------------ 路径

    def resolve_path(self, path: str) -> Path:
        """把路径解析为规范化绝对路径（目标可以不存在）。

        工作区外的目标仅在 ``allow_external_paths=true`` **且原始输入是绝对路径**时放行
        （架构 §1.5）；其余越界输入一律失败。

        Raises:
            WorkspaceBoundaryError: 空路径，或路径在工作区之外且不被允许。
        """
        return resolve_target(
            path,
            base_dir=self.workspace_root,
            workspace_root=self.workspace_root,
            allow_external=self.allow_external_paths,
        ).path

    def is_external(self, path: Path) -> bool:
        """该规范化路径是否在当前工作区之外（用于外部路径的审批与展示）。"""
        return not is_within(path, self.workspace_root)

    def require_side_effect_target(self, path: Path) -> None:
        """副作用的执行前复核：受保护目标硬拒绝（架构 §1.5）。

        安全判定、动作指纹与执行三处都必须用同一个规范化目标，所以这里再查一次——
        审批通过也不能写/改/删受保护目标。

        Raises:
            SecurityError: 目标是受保护目录（或其内部）。
        """
        assert_not_protected_target(path, verb="修改")

    def resolve_readable_file(self, path: str) -> Path:
        """解析只读文件路径，并拦下敏感文件。

        Raises:
            SecurityError: 路径越界、命中敏感文件模式、或目标不是文件。
        """
        resolved = self.resolve_path(path)
        self._reject_sensitive(resolved, verb="读取")
        if not resolved.is_file():
            raise SecurityError(f"目标不是文件或不存在：{resolved.as_posix()}")
        return resolved

    def resolve_writable_file(self, path: str) -> Path:
        """解析待修改的已有文件（架构 §6.3）。

        Raises:
            SecurityError: 路径越界、命中敏感文件模式、或目标不是文件。
        """
        resolved = self.resolve_path(path)
        self._reject_sensitive(resolved, verb="修改")
        self.require_side_effect_target(resolved)
        if not resolved.is_file():
            raise SecurityError(f"目标文件不存在或不是普通文件：{resolved.as_posix()}")
        return resolved

    def resolve_writable_path(self, path: str) -> Path:
        """解析写入目标路径；目标可以不存在（架构 §6.4）。

        Raises:
            SecurityError: 路径越界或命中敏感文件模式。
        """
        resolved = self.resolve_path(path)
        self._reject_sensitive(resolved, verb="写入")
        self.require_side_effect_target(resolved)
        return resolved

    # ------------------------------------------------------------------ 工具分类

    def classify_tool(
        self, tool_name: str, arguments: Mapping[str, Any] | None = None
    ) -> SafetyDecision:
        """判定某个工具调用是否放行。

        未接入或未列入放行清单的工具一律拒绝（失败闭合），不做"默认放行"。
        """
        args = dict(arguments or {})
        if tool_name in READ_ONLY_TOOLS:
            return SafetyDecision(allowed=True, requires_approval=False, reason="只读工具")
        if tool_name == "edit_file":
            return self._classify_edit(args)
        if tool_name == "write_file":
            return self._classify_write(args)
        if tool_name in SHELL_TOOLS:
            return self._classify_shell(args)
        if tool_name in ALWAYS_APPROVAL_TOOLS:
            concern = self._external_side_effect_decision(
                str(args.get("path", "")), verb="删除"
            )
            if concern is not None:
                return concern
            return SafetyDecision(
                allowed=False,
                requires_approval=True,
                reason=f"{tool_name} 属于强制审批动作，必须人工批准后才执行",
            )
        return SafetyDecision(
            allowed=False,
            requires_approval=False,
            reason=f"工具 {tool_name} 尚未接入或不在放行清单内",
        )

    def _external_side_effect_decision(
        self, path: str, *, verb: str
    ) -> SafetyDecision | None:
        """工作区外副作用与受保护目标的统一判定；``None`` 表示按常规规则继续判。

        - 受保护目标（根目录、主目录、Git 元数据、系统关键目录）→ **硬拒绝**，不可审批；
        - 工作区外的普通目标 → 强制人工审批，且不参与免审白名单；
        - 工作区内目标 → ``None``，交给各工具自己的风险规则。
        """
        if not path.strip():
            # 没给路径（或空串）：交给工具自身的解析去报错，这里不抢先判定，
            # 否则"只按文本判定"的调用（例如只传 old_text/new_text）会被误判为缺路径。
            return None
        try:
            target = self.resolve_path(path)
        except WorkspaceBoundaryError as exc:
            return SafetyDecision(allowed=False, requires_approval=False, reason=str(exc))
        reason = protected_target_reason(target)
        if reason:
            return SafetyDecision(
                allowed=False,
                requires_approval=False,
                reason=f"禁止{verb}受保护目标（{reason}）：{target.as_posix()}",
            )
        if self.is_external(target):
            return SafetyDecision(
                allowed=False,
                requires_approval=True,
                reason=(
                    f"工作区外路径：{target.as_posix()}；{verb}工作区外目标必须人工审批"
                    "（架构 §1.5）"
                ),
            )
        return None

    def _classify_edit(self, args: Mapping[str, Any]) -> SafetyDecision:
        # 输入模式：精确串替换（默认）或 unified diff（T22）。安全判定与工具执行**共用**
        # diff_guard.edit_input_mode，不能两处各写一套互斥规则（架构 §6.3）。
        try:
            mode = edit_input_mode(args.get("old_text"), args.get("new_text"), args.get("patch"))
        except SecurityError as exc:
            return SafetyDecision(allowed=False, requires_approval=False, reason=str(exc))
        concern = self._external_side_effect_decision(str(args.get("path", "")), verb="修改")
        if concern is not None:
            return concern
        if mode == "patch":
            try:
                plan = parse_patch(str(args.get("patch") or ""))
            except SecurityError as exc:
                # 结构非法的 patch 永远不可能被执行：硬拒绝，不推进审批——不能让用户为一个
                # 必然失败的补丁点"批准"（架构 §6.3：空 Patch/错 hunk 直接拒绝）。
                return SafetyDecision(allowed=False, requires_approval=False, reason=str(exc))
            changed_lines = plan.changed_lines
            what = f"unified diff（{len(plan.hunks)} 个 hunk）"
        else:
            changed_lines = count_changed_lines(
                str(args.get("old_text") or ""), str(args.get("new_text") or "")
            )
            what = "精确串替换"
        limit = self.security.large_edit.max_changed_lines_without_approval
        if changed_lines > limit:
            return SafetyDecision(
                allowed=False,
                requires_approval=True,
                reason=(
                    f"{what}，改动约 {changed_lines} 行，超过免审上限 {limit} 行，属于大范围修改，"
                    "需要人工审批（T10）"
                ),
            )
        return SafetyDecision(
            allowed=True,
            requires_approval=False,
            reason=f"{what}，改动 {changed_lines} 行，在免审范围内",
        )

    def _classify_write(self, args: Mapping[str, Any]) -> SafetyDecision:
        concern = self._external_side_effect_decision(str(args.get("path", "")), verb="写入")
        if concern is not None:
            return concern
        if args.get("overwrite"):
            return SafetyDecision(
                allowed=False,
                requires_approval=True,
                reason="覆盖已有文件需要人工审批",
            )
        return SafetyDecision(
            allowed=True, requires_approval=False, reason="创建新文件，未覆盖任何已有内容"
        )

    def _classify_shell(self, args: Mapping[str, Any]) -> SafetyDecision:
        command = args.get("command")
        if not isinstance(command, str) or not command.strip():
            return SafetyDecision(False, False, "run_shell 缺少 command 参数")
        raw_env_keys = args.get("env_keys")
        env_keys = (
            [str(key) for key in raw_env_keys]
            if isinstance(raw_env_keys, (list, tuple))
            else None
        )
        plan = prepare_shell(
            command,
            config=self.security.shell,
            workspace_root=self.workspace_root,
            cwd=str(args.get("cwd") or "."),
            timeout_seconds=as_int(args.get("timeout_seconds")),
            env_keys=env_keys,
            allow_external_cwd=self.allow_external_paths,
        )
        if plan.cwd is not None and self.is_external(plan.cwd):
            # 工作区外 cwd 不受免审白名单影响：必须人工批准（架构 §1.5）
            return SafetyDecision(
                allowed=False,
                requires_approval=True,
                reason=(
                    f"工作区外工作目录（{plan.cwd.as_posix()}）：{plan.reason}；"
                    "必须人工审批"
                ),
            )
        return SafetyDecision(
            allowed=plan.allowed,
            requires_approval=plan.requires_approval,
            reason=plan.reason,
        )

    # ------------------------------------------------------------------ 一次性授权

    def grant(self, fingerprint: str) -> None:
        """登记一次人工批准（仅在 ``tool_execution_node`` 执行已批准动作前调用）。

        批准只对本次执行有效：动作执行完必须立刻 :meth:`revoke`，避免一次批准被复用。
        """
        if fingerprint:
            self._granted.add(fingerprint)

    def revoke(self, fingerprint: str) -> None:
        """撤销一次人工批准。"""
        self._granted.discard(fingerprint)

    def is_granted(self, fingerprint: str) -> bool:
        """该动作指纹是否持有有效的人工批准。"""
        return bool(fingerprint) and fingerprint in self._granted

    # ------------------------------------------------------------------ 脱敏

    def redact(self, text: str) -> str:
        """把输出中的密钥值替换掉（``security.secrets.redact_environment_values``）。"""
        if not self.security.secrets.redact_environment_values:
            return text
        return redact_secrets(text, self._secret_values)

    def _reject_sensitive(self, path: Path, *, verb: str) -> None:
        if self.security.secrets.block_known_secret_files and is_sensitive_path(path):
            raise SecurityError(f"该文件按策略禁止{verb}（疑似密钥文件）：{path.name}")


def as_int(value: Any) -> int | None:
    """把模型给的值转成整数；转换失败返回 ``None``（按未提供处理）。

    安全判定与动作指纹必须用同一套归一规则，否则同一个动作会在两处算出不同的
    计划（例如 ``timeout_seconds`` 传 ``"60"`` 与传 ``60``）。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None
