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

from local_agent.cancellation import CancellationToken
from local_agent.config import (
    AppConfig,
    SecurityConfig,
    WorkspaceConfig,
    redact_secrets,
)
from local_agent.exceptions import SecurityError, WorkspaceBoundaryError
from local_agent.security.diff_guard import count_changed_lines, edit_input_mode, parse_patch
from local_agent.security.path_guard import (
    ExternalPathPolicy,
    ResolvedTarget,
    assert_not_protected_target,
    is_sensitive_path,
    is_within,
    protected_target_reason,
    resolve_target,
)
from local_agent.security.process_guard import ProcessRegistry
from local_agent.security.shell_guard import prepare_shell

#: 只读工具。
#:
#: ``grep`` / ``glob``（2026-09-30 新增）也是只读：它们只返回"哪个文件哪一行命中了什么"，
#: 不写盘、不改状态，因此与 ``list_dir`` / ``read_file`` 同级，**不需要人工审批**。
#: 但它们必须登记在这里——``KNOWN_TOOLS`` 是"可绑定给模型"的白名单，没登记的会被安全门拒。
#: ``web_search`` / ``web_fetch``（v0.5 T53）同样登记在这里：它们只读不写，且**只在本机或内网之外**
#: （SSRF 校验，见 §6.10），不需要人工审批；真正的开关是 `web_access.enabled`（默认关闭，显式 opt-in）。
READ_ONLY_TOOLS: tuple[str, ...] = (
    "list_dir",
    "read_file",
    "grep",
    "glob",
    "web_search",
    "web_fetch",
    # v0.5 T54：Git 只读三件套——只报告状态与差异，不写盘、不改索引，不需要人工审批。
    # 能改仓库的是 ``git_commit``，它在下面单独按"始终需要审批"处理（§16.4）。
    "git_status",
    "git_diff",
    "git_log",
    # v0.5 T55：技能清单与正文——只读用户机器上已有的技能说明，不写盘、不执行其中的脚本
    # （脚本只作为文本资源；要执行得走 shell 能力与人工审批，见 §16.5）。
    "skill_list",
    "skill_load",
)
CONTROL_TOOLS: tuple[str, ...] = ("update_plan",)

#: 写入类工具（本版本只放行其中的安全子集）。
WRITE_TOOLS: tuple[str, ...] = ("edit_file", "write_file")

#: Shell 工具（按结构化白名单判定）。
SHELL_TOOLS: tuple[str, ...] = ("run_shell",)

#: 始终需要人工审批的工具。
ALWAYS_APPROVAL_TOOLS: tuple[str, ...] = ("delete_file",)

#: Git 写类工具（v0.5 T54）：它改的是**仓库历史**，不是工作区某个文件，因此不套用写文件的
#: 大小/覆盖判定，而是**始终需要人工审批**（§16.4）。
GIT_WRITE_TOOLS: tuple[str, ...] = ("git_commit",)

#: 本版本已实现、可绑定给模型的全部工具。
KNOWN_TOOLS: tuple[str, ...] = (
    READ_ONLY_TOOLS
    + CONTROL_TOOLS
    + WRITE_TOOLS
    + SHELL_TOOLS
    + ALWAYS_APPROVAL_TOOLS
    + GIT_WRITE_TOOLS
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
        external_policy: ExternalPathPolicy | None = None,
        cancellation: CancellationToken | None = None,
        allow_result_spill: bool = True,
    ) -> None:
        self.workspace_root = workspace_root
        self.security = security
        self.workspace = workspace
        #: 本轮执行的取消信号（§17.4）：它让本方法成为"取消后不再启动工具调用"的**第二道门**。
        #: 缺省 `None` 表示这次运行不参与取消（CLI / 测试的轻量路径、以及 v0.5 之前的行为）。
        self.cancellation = cancellation
        #: T67 子策略禁止只读检索工具的显式落盘；不是模型可变参数或用户配置。
        self.allow_result_spill = allow_result_spill
        #: 本轮在跑的子进程登记表（§17.4 第二片）：取消时按**进程树**终止（Windows `taskkill /F /T`、
        #: POSIX `killpg`）。它与令牌绑定，因此"取消"与"已启动进程的终止"是同一次动作的两半。
        self.processes = ProcessRegistry(cancellation)
        #: 工作区外路径的档位与逐条授权（T38；架构 §1.5）。界面上的档位存在设置库里，
        #: 由应用层解析后传进来；缺省取 ``config.yaml`` 的出厂默认。放开不等于关闭安全校验：
        #: 越界判定、受保护目标、外部 cwd 强制审批与四项硬约束都照旧生效。
        self.external_policy = external_policy or ExternalPathPolicy(
            mode=workspace.external_paths.mode
        )
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
        external_policy: ExternalPathPolicy | None = None,
        cancellation: CancellationToken | None = None,
        allow_result_spill: bool = True,
    ) -> SecurityPolicy:
        """按配置构造策略；密钥值从环境变量读取，仅用于输出脱敏。

        Args:
            app_config: 完整配置。
            workspace_root: 工作区根目录。
            environ: 环境变量映射，默认取 ``os.environ``。
            external_policy: 外部路径档位与授权。界面上的档位可在设置库里被改，应用层解析后
                从这里传进来；给 ``None`` 时取 ``config.yaml`` 的出厂默认。
            cancellation: 本轮执行的取消信号（§17.4）；``None`` 表示这次运行不参与取消。

        Returns:
            构造好的策略对象。外部档位只是放开"显式绝对路径的受控外部访问"（架构 §1.5），
            不放松越界判定本身，也不放宽四项硬约束。
        """
        env = os.environ if environ is None else environ
        secret_names = [
            name
            for name in (
                # `llm` 段可以为空（Web 初始设置模式），这里只做"把环境变量值纳入脱敏"的收集。
                "" if app_config.llm is None else app_config.llm.api_key_env,
                app_config.embedding.api_key_env,
            )
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
            external_policy=external_policy,
            cancellation=cancellation,
            allow_result_spill=allow_result_spill,
        )

    # ------------------------------------------------------------------ 路径

    def require_result_spill(self) -> None:
        """检索工具自己的第二道门：子策略不能借 save_full 写文件。"""
        if not self.allow_result_spill:
            raise SecurityError("当前策略禁止保存检索结果文件")

    def resolve_target(self, path: str) -> ResolvedTarget:
        """解析路径并给出访问级别（目标可以不存在）。判定与执行共用这一个入口。

        工作区外的目标仅在**原始输入是绝对路径**且当前档位认可时放行（架构 §1.5）；
        其余越界输入一律失败。

        Raises:
            WorkspaceBoundaryError: 空路径，或路径在工作区之外且未被授权。
        """
        return resolve_target(
            path,
            base_dir=self.workspace_root,
            workspace_root=self.workspace_root,
            external_policy=self.external_policy,
        )

    def resolve_path(self, path: str) -> Path:
        """把路径解析为规范化绝对路径（目标可以不存在）；越界且未授权时失败。"""
        return self.resolve_target(path).path

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
        if self.cancellation is not None and self.cancellation.cancelled:
            # 取消是安全 / 审批门的**第一条判定**（§17.4）：用的就是本方法本来就有的"拒绝"出口，
            # 不新增旁路。已经开始的工具调用不在这里处理（终止属 T60 的第二片，§17.4 明写不承诺）。
            return SafetyDecision(
                allowed=False,
                requires_approval=False,
                reason=self.cancellation.refusal_reason(),
            )
        if tool_name in CONTROL_TOOLS:
            return SafetyDecision(allowed=True, requires_approval=False, reason="当前任务计划更新")
        if tool_name in READ_ONLY_TOOLS:
            if tool_name in {"grep", "glob"} and args.get("save_full") and not self.allow_result_spill:
                return SafetyDecision(False, False, "当前策略禁止保存检索结果文件")
            return SafetyDecision(allowed=True, requires_approval=False, reason="只读工具")
        if tool_name == "edit_file":
            return self._classify_edit(args)
        if tool_name == "write_file":
            return self._classify_write(args)
        if tool_name in SHELL_TOOLS:
            return self._classify_shell(args)
        if tool_name in GIT_WRITE_TOOLS:
            return self._classify_git_commit(args)
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
        - 越界且未被授权 → **硬拒绝**（失败闭合），提示指向「设置 → 权限」；
        - 只有**只读**授权的外部目标 → 拒绝写操作，并说明该授权只覆盖读取；
        - **读写**授权的外部目标 → ``None``：视同工作区内，交给各工具自己的风险规则
          （四项硬约束仍然生效）；
        - 工作区内目标 → ``None``。
        """
        if not path.strip():
            # 没给路径（或空串）：交给工具自身的解析去报错，这里不抢先判定，
            # 否则"只按文本判定"的调用（例如只传 old_text/new_text）会被误判为缺路径。
            return None
        try:
            target = self.resolve_target(path)
        except WorkspaceBoundaryError as exc:
            return SafetyDecision(allowed=False, requires_approval=False, reason=str(exc))
        reason = protected_target_reason(target.path)
        if reason:
            return SafetyDecision(
                allowed=False,
                requires_approval=False,
                reason=f"禁止{verb}受保护目标（{reason}）：{target.path.as_posix()}",
            )
        if target.access == "read":
            return SafetyDecision(
                allowed=False,
                requires_approval=False,
                reason=(
                    f"该路径只有只读授权，不能{verb}：{target.path.as_posix()}。"
                    "请在「设置 → 权限」中把该目录改为「可读写」，或改到工作区内操作"
                ),
            )
        return None

    def _classify_git_commit(self, args: Mapping[str, Any]) -> SafetyDecision:
        """提交到 Git 仓库：**始终需要人工审批**（架构 §16.4）。

        它改的是**仓库历史**而不是工作区某个文件，所以不走写文件那套"大小/覆盖"判定。理由里必须带出
        "哪些路径、什么信息"——审批卡片据此展示（投影层会把参数展平给用户看）；分支由 `git_status`
        给出，且默认分支上 `git_commit` 会先返回"需要额外确认"（见 `tools.git_tools`）。
        """
        raw_paths = args.get("paths")
        paths = [str(item) for item in raw_paths] if isinstance(raw_paths, (list, tuple)) else []
        listed = "、".join(paths) if paths else "未列出（必须显式给出）"
        message = str(args.get("message") or "").strip()
        return SafetyDecision(
            allowed=False,
            requires_approval=True,
            reason=(
                f"提交到当前 Git 分支：路径 {listed}；提交信息「{message[:80]}」。"
                "这改动仓库历史而不是单个文件，必须人工批准后才执行"
            ),
        )

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
            external_policy=self.external_policy,
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
