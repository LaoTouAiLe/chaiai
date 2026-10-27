"""安全策略服务：路径、敏感文件与工具放行的统一入口（架构 §6、§11.1）。

设计原则（架构 §1.3-4、§11.1）：危险动作在代码层拦截，不依赖 Prompt；
未实现、未放行或需要审批而本版本没有审批能力的动作，一律失败闭合（deny）。

工作区文件编辑、新建、覆盖、删除默认免审批；可选审批开关仍可配置。
常规命令和外部 cwd 免审批，Windows 各档位均强制写边界；外部写入仍需目录授权。
破坏性 Shell 分类（不含 taskkill）、退出沙箱与受保护目标保留对应策略。
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

if TYPE_CHECKING:
    # 只在类型检查期导入：shell_guard 依赖本模块，顶层导入会成环。
    from local_agent.security.shell_guard import ShellPlan

from local_agent.cancellation import CancellationToken
from local_agent.capabilities import EXTERNAL_TOOL_PREFIX
from local_agent.config import (
    AppConfig,
    SecurityConfig,
    WorkspaceConfig,
    redact_secrets,
)
from local_agent.exceptions import FileOperationError, SecurityError, WorkspaceBoundaryError
from local_agent.security import win_sandbox
from local_agent.security.command_registry import CommandRegistry
from local_agent.security.diff_guard import count_changed_lines, edit_input_mode, parse_patch
from local_agent.security.file_system import FileSystemSandbox
from local_agent.security.path_guard import (
    ExternalPathPolicy,
    ResolvedTarget,
    assert_not_protected_target,
    is_sensitive_path,
    is_within,
    protected_target_reason,
    resolve_target,
)
from local_agent.security.process_guard import ProcessRegistry, SandboxRequest
from local_agent.security.shell_guard import describe_adapter, prepare_shell

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
    # 合并入口（2026-10-22）：一次调用可"按内容搜"或"按名字找"，只读，归同一组工具。
    "search_files",
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
    # 2026-10-13：自主委派（§18.7）。它自己不改工作区，派生出的子 Agent 也**只有只读能力**
    # （`readonly_child_capabilities` 的收窄 + 子策略重建），所以与上面几项同级、不需要人工审批。
    # 真正的闸门是页面上的「自主委派调查」开关（默认关，显式 opt-in）与 `workflows` 段的资源上限——
    # 它是**花钱**的能力，不是改文件的能力，所以不套用写类的审批判定。
    "delegate_research",
    # 2026-10-16：看图（`view_image`）——读本机的一张图片交给模型看，不写盘、不改状态。
    # 它只读，所以与 `read_file` 同级**不需要人工审批**；"能不能用"由 `image_read` 能力
    # （取决于 `llm.supports_vision` 声明）与工具内部的失败闭合决定，不由审批决定。
    "view_image",
    "inspect_system",
    # 2026-10-20：MCP 的**检索工具**是只读的——它只查本机已经拿到的目录，不发外部请求、
    # 不改任何状态，所以与上面几项同级、不需要审批。注意它**不是**外部工具：外部工具名是
    # `mcp__<server>__<tool>`（双下划线），由下面的 `mcp__` 前缀分支处理（一律审批）。
    "mcp_tool_search",
)
CONTROL_TOOLS: tuple[str, ...] = ("update_plan", "task_control")

#: 写入类工具（本版本只放行其中的安全子集）。
WRITE_TOOLS: tuple[str, ...] = ("edit_file", "write_file", "download_file")

#: Shell 工具（按结构化白名单判定）。
SHELL_TOOLS: tuple[str, ...] = ("run_shell",)

#: 始终需要人工审批的工具。
ALWAYS_APPROVAL_TOOLS: tuple[str, ...] = ("delete_file",)

#: Git 写类工具（v0.5 T54）：它改的是**仓库历史**，不是工作区某个文件，因此不套用写文件的
#: 大小/覆盖判定，而是**始终需要人工审批**（§16.4）。
GIT_WRITE_TOOLS: tuple[str, ...] = ("git_commit",)

#: 常驻记忆（2026-10-27，架构 §10.7）：写在 agent 自己的存储里（数据根 `memories/`），
#: 既不是用户文件、也没有外部副作用，因此**免审批**——这是用户明确接受的取舍
#: （"模型可直写记忆文件"）。但它仍然是**写**：只读档一律拒绝，见 :meth:`SecurityPolicy.classify_tool`。
MEMORY_TOOLS: tuple[str, ...] = ("memory",)

#: 交互终端与浏览器（2026-10-27 从 ``classify_tool`` 里提出来，判定不变）：
#: 它们属于"当前会话自己的进程/页面"，判定比写文件细，单独一条方法。
INTERACTIVE_TOOLS: tuple[str, ...] = (
    "start_shell", "read_shell", "write_shell", "stop_shell", "list_shell_sessions", "browser",
)

#: 本版本已实现、可绑定给模型的全部工具。
KNOWN_TOOLS: tuple[str, ...] = (
    READ_ONLY_TOOLS
    + CONTROL_TOOLS
    + WRITE_TOOLS
    + SHELL_TOOLS
    + ALWAYS_APPROVAL_TOOLS
    + GIT_WRITE_TOOLS
    + MEMORY_TOOLS
    + INTERACTIVE_TOOLS
)


def _path_key(workspace_root: Path, path: str) -> str:
    """文件计数的比较键：相对路径按工作区展开，再统一大小写与符号链接。

    为什么要有这一步：判定时模型给的是**相对路径**（``site/index.html``），记账时执行侧给的是
    **解析后的绝对路径**；两边直接比字符串永远不相等，于是"改过的文件"每次都算新文件、
    免审额度形同虚设。用同一个键归一后，"同一轮改同一个文件"才是同一件事。
    """
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = workspace_root / candidate
    return os.path.normcase(os.path.realpath(str(candidate)))


@dataclass(frozen=True)
class SafetyDecision:
    """一次工具调用的安全判定结果。"""

    allowed: bool
    requires_approval: bool
    reason: str
    #: 拒绝的**性质**：``policy`` = 安全策略不放行（要改请求，或走人工审批）；``environment`` = 当前
    #: 环境里**根本起不来**（找不到可执行文件或 Shell 适配器）；``input`` = 文件参数或状态不符。
    #: 上层据此分流（2026-10-14：`time /t` 曾被显示成"被安全策略拦截"）。
    reason_kind: Literal["policy", "environment", "input"] = "policy"
    #: 本次审批请求属于**大范围编辑**（本轮触及的文件数超上限）。执行侧据此在批准落地后记住
    #: "本轮已批过一次批量修改"，后续文件不再逐个问（2026-10-15）。不是审批原因的工具不该看它。
    large_edit: bool = False


class FileChangeRecorder(Protocol):
    """文件变更账本的写入口（实现在 ``persistence/file_changes.py``）。

    策略层只持有这个最小协议：``security`` 不该依赖 ``persistence``，而账本又必须知道
    "哪个文件在什么时候被谁改了多少行"。应用层每轮把一个实现了它的对象挂上来即可。
    """

    def capture(self, resolved: Path) -> None:
        """在写盘**之前**留一份原文（同一路径只留第一次）。"""

    def record(
        self,
        *,
        tool: str,
        resolved: Path,
        kind: Literal["create", "modify", "delete"],
        additions: int = 0,
        deletions: int = 0,
        before_sha256: str | None = None,
        after_sha256: str | None = None,
    ) -> None:
        """记录一次已完成的文件写操作。"""


#: 可豁免的「单目标删除」形态（2026-10-23，§6.6）：删除动词 + 可选标志 + 恰好一个目标。
#: 只用于**放行判断**；执行永远用原 argv，绝不经过这里。
_SINGLE_DELETE_SHAPE = re.compile(
    r"""^(?:del|erase|rm|rd|rmdir)(?:\s+/[A-Za-z]+)*\s+(?:"([^"]+)"|'([^']+)'|(\S+))$""",
    re.IGNORECASE,
)

#: 硬拦截词（2026-10-23）：出现即不豁免，保留原审批。对齐 hermes 的 hardline 清单。
_HARDLINE_WORDS: tuple[str, ...] = (
    "format", "mkfs", "diskpart", "vssadmin", "clear-disk", "cipher",
    "shutdown", "reboot", "icacls", "chmod", "chown",
    "taskkill", "stop-process", "/dev/sd",
)


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
        session_scope: str | None = None,
        #: 安全删除（2026-10-20）：开启时 `delete_file` 把目标**移进系统回收站**而不是抹掉。
        #: 策略层默认关（直接构造策略的测试/CLI 保持旧行为）；生产由 `from_config` 按配置传入。
        safe_delete: bool = False,
    ) -> None:
        self.workspace_root = workspace_root
        # 不同会话不共享终端或浏览器；CLI 未提供会话键时只在当前策略内复用。
        from uuid import uuid4

        self.session_scope = session_scope or uuid4().hex
        self.security = security
        self.workspace = workspace
        #: 删除是否进**系统回收站**（2026-10-20）。见 `FileSystemSandbox.delete`。
        self.safe_delete = bool(safe_delete)
        #: 本轮执行的取消信号（§17.4）：它让本方法成为"取消后不再启动工具调用"的**第二道门**。
        #: 缺省 `None` 表示这次运行不参与取消（CLI / 测试的轻量路径、以及 v0.5 之前的行为）。
        self.cancellation = cancellation
        #: T67 子策略禁止只读检索工具的显式落盘；不是模型可变参数或用户配置。
        self.allow_result_spill = allow_result_spill
        #: `read_file` 的轮级去重表（2026-10-22）：`(路径, sha, 范围, max_chars, 编码) -> 上次结果的
        #: 行区间/续读位置`。同一轮里重复读同一段、且文件没变时不再返回原文——模型重复读同一段
        #: 是常见空转，一次就是一次模型往返。策略每轮新建，所以天然只在轮内生效；文件一变 sha
        #: 就不同，不会命中。
        self.read_cache: dict[tuple, tuple[int, int, bool, int | None, int | None, int]] = {}
        #: 验证类命令的轮内结果缓存（2026-10-22）：同命令 + 文件未变 → 直接回上次结果。
        #: 只缓存"跑测试/类型检查"这类**无副作用**命令；key 带 ``file_change_version``，
        #: 一旦有写盘就整体失效（改了代码还拿旧测试结果，比不缓存更糟）。
        self.command_cache: dict[tuple[str, str, int], str] = {}
        self.file_change_version = 0
        #: 同一文件连续编辑失败次数（2026-10-22，抄 hermes 的 patch 失败计数）：
        #: 到第 3 次时错误信息升级为"不要再改写法重试，换策略"，而不是让模型无限微调锚点。
        self.edit_failures: dict[str, int] = {}
        #: 本轮工具结果的字符预算（2026-10-22，对齐 hermes 的 turn budget 20 万字符）。
        #: 只统计**进入模型上下文**的文本（图像块不算）：`read_file` 的上限提到 10 万字符后，
        #: 一轮里几次大读取就可能把上下文推到压缩阈值。这里只记账并置标记，**不截断、不落盘**
        #: ——落盘是下一步，先有计数才谈得上"什么时候算超"。
        self.result_budget_chars = 200_000
        self.result_budget_used = 0
        self.result_budget_exceeded = False
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
        #: 本轮**已经改过内容**的文件（`edit_file` 判定用；`write_file` 新建不计——见 `_classify_edit`）。
        #: 策略是每轮重建的，所以这个集合天然就是"本轮"的作用域，不需要另开状态通道。
        self._edited_paths: set[str] = set()
        #: 本轮是否已经为"大范围编辑"批准过一次：批准后本轮内后续文件不再逐个问（2026-10-15）。
        self._large_edit_approved = False
        #: 当前实例是否已物化过写授权（根上一条可继承 ACE，系统传播到整棵子树）。
        self._sandbox_materialized = False
        #: 文件变更账本（2026-10-21）：由应用层每轮注入，``None`` = 不记账（CLI / 测试）。
        #: 它是**可观测性设施**，不是安全门：记账失败只写日志，绝不阻断写盘。
        self.file_change_recorder: FileChangeRecorder | None = None
        #: 命令级后台运行表（2026-10-21）：用户点"转后台"后由它接管进程与输出。
        #: 与 `processes` 同一生命周期——取消/归档时一并按进程树终止，不留孤儿。
        self.commands = CommandRegistry(processes=self.processes)
        self.files = FileSystemSandbox(self)

    # ---- 文件变更账本（只做转发，实现见 persistence/file_changes.py） ----------

    def capture_file_baseline(self, resolved: Path) -> None:
        """写盘前留一份原文；没有接线或失败都不影响写盘。"""
        recorder = self.file_change_recorder
        if recorder is None:
            return

        try:
            recorder.capture(resolved)
        except Exception:
            logging.getLogger(__name__).warning("记录文件基线失败：%s", resolved, exc_info=True)

    def note_file_change(
        self,
        *,
        tool: str,
        resolved: Path,
        kind: Literal["create", "modify", "delete"],
        additions: int = 0,
        deletions: int = 0,
        before_sha256: str | None = None,
        after_sha256: str | None = None,
    ) -> None:
        """记一次已完成的文件写操作；同样吞掉账本异常。"""
        # 缓存失效与账本是否接线无关：文件变了就必须让旧的验证结果作废。
        self.file_change_version += 1
        recorder = self.file_change_recorder
        if recorder is None:
            return

        try:
            recorder.record(
                tool=tool, resolved=resolved, kind=kind, additions=additions,
                deletions=deletions, before_sha256=before_sha256, after_sha256=after_sha256,
            )
        except Exception:
            logging.getLogger(__name__).warning("记录文件变更失败：%s", resolved, exc_info=True)

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
            external_policy: 权限档位与授权。界面上的档位可在设置库里被改，应用层解析后
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
                app_config.web_access.tavily_api_key_env,
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
            # 安全删除来自配置（默认开）：删除进回收站，可从数据目录找回。
            safe_delete=app_config.security.safe_delete,
        )

    # -------------------------------------------------------------- 写侧沙箱

    def shell_sandbox_active(self) -> bool:
        """写侧沙箱这一轮是否生效。**无副作用**：只回答"要不要沙箱"，不物化授权。

        **2026-10-15 起它由档位决定，不再有独立开关**（用户裁定"那个写侧沙箱开关可以去掉了"）：

        - ``read-only``：生效，但**不带任何写授权**——命令读得到、写不了（这是该档的实现手段）；
        - ``workspace-write``：生效，可写范围 = 工作区 + 专属临时目录 + 「读写」授权目录；
        - ``danger-full-access``：**不生效**——这一档的语义就是"不设 OS 边界"（对齐 codex 的
          ``danger-full-access``）。

        与 :meth:`shell_sandbox_request` 的分工：这个用于**判定**（例如回答"`escalate_sandbox`
        这个请求有没有意义"），那个用于**执行**（会真的去授权目录）。判定阶段不能调后者——
        它会给工作区与临时目录写 ACL，判一次写一次是不可接受的副作用。
        """
        # 非 Windows 没有 OS 级写边界：如实返回 False，由 `sandbox_boundary_note()` 说明。
        return win_sandbox.is_supported()

    def sandbox_boundary_note(self) -> str | None:
        """这一轮"OS 级写边界到底有没有"，没有时给一句如实说明（给用户与模型看）。

        为什么需要它（2026-10-15 用户裁定）：非 Windows 上不再失败闭合，改为**照常跑**。静默跑
        掉就等于假装有边界——那是 dsh 移植笔记里点名的 fail-open 坑。所以改成"跑，但说清楚"。
        """
        if win_sandbox.is_supported():
            return None
        return (
            "当前平台没有 OS 级写边界（写侧沙箱只在 Windows 上实现）："
            "路径工具受档位策略限制；命令内部的文件访问不能由路径工具约束，不是内核强制"
        )

    def shell_sandbox_request(self, *, escalate: bool = False, cwd: Path | None = None) -> SandboxRequest | None:
        """``run_shell`` 该不该在**写侧沙箱**里跑；不该就是 ``None``（默认行为）。

        由档位派生（没有独立开关了）：

        - ``read-only`` → **无写授权**的受限令牌：命令能跑、写不进去；
        - ``workspace-write`` → 可写范围 = **工作区根 + 该工作区专用临时目录 + 用户授权的「读写」
          目录**；每个根独立 SID，ACL 边界见 win_sandbox 模块说明。临时目录会把 ``TMP``/``TEMP``/
          ``TMPDIR`` 指过去——很多命令要写临时文件，没有这一格它们会成片（如实）失败；
        - ``danger-full-access`` → 与工作区可写相同的写边界，外部写入仍需目录授权；
        - 非 Windows → ``None``（照常跑，由 :meth:`sandbox_boundary_note` 如实说明）。
        根上一条可继承 ACE；新目录**立即继承可写**（2026-10-15 起，不再逐目录扫描）。

        Args:
            escalate: 本次请求**退出写侧沙箱**（escalation，2026-10-14）。为真时直接返回 ``None``
                （这一条命令不进沙箱）。**审批不在这里**：调用方必须先拿到人工批准（指纹里含
                ``escalate_sandbox=True``），执行侧 :func:`~local_agent.tools.shell_tools.run_shell_command`
                还会再查一次批准——两处都必须有，缺一不可。

        Raises:
            SecurityError: 授权物化失败（可写根授权不了）。**这里仍然失败闭合**——边界说"工作区
                可写"却给不出可写授权时，命令会在一个有边界的错觉下跑起来，比拒绝更糟。
        """
        if escalate:
            # 只回答"这次要不要沙箱"；审批与"批准是否真的存在"由调用方与执行侧各自负责。
            return None
        mode = self.external_policy.mode
        if not win_sandbox.is_supported():
            return None
        if cwd is not None and not is_within(cwd, self.workspace_root):
            # 外部 cwd 免审批不等于可写；LUA 令牌需显式用户读 ACE 才能读取管理员所有的目录。
            try:
                win_sandbox.grant_read(str(cwd))
            except (OSError, win_sandbox.SandboxUnavailable) as exc:
                raise SecurityError(f"命令工作目录无法物化只读访问（{exc}）") from exc
        if mode == "read-only":
            # LUA 令牌不能依赖 OWNER RIGHTS。只补普通用户读 ACE，不带入任何可写 SID。
            roots = [self.workspace_root, *(Path(path) for path in self.external_policy.grants)]
            try:
                for root in dict.fromkeys(roots):
                    if root.is_dir():
                        win_sandbox.grant_read(str(root))
            except (OSError, win_sandbox.SandboxUnavailable) as exc:
                raise SecurityError(f"只读沙箱：无法物化读授权（{exc}）") from exc
            return SandboxRequest(writable_sids=())
        temp_dir = win_sandbox.sandbox_temp_dir(self.workspace_root)
        # **用户授权的「读写」目录也纳入可写范围**（2026-10-14，吸收 codex `--add-dir` 的做法）：
        # 否则组合是自相矛盾的——档位说"这个目录你可以读写"，沙箱却把它挡在外面。
        # 只收 `read_write`（只读授权本来就意味着"能读不能写"，沙箱不该替它放宽），
        # 不存在与受保护的目标跳过。
        writable_dirs = self._writable_grant_dirs()
        directories = tuple(dict.fromkeys((str(self.workspace_root), str(temp_dir), *writable_dirs)))
        # 每个可写根一个 SID：撤销或降为只读后，下一轮令牌不再携带该目录的 SID。
        # 每进程每根扫描一次；新目录继承可写，扫描后新增的保护名目录要到下次进程启动才保护。
        sids = tuple(win_sandbox.workspace_sid_text(directory) for directory in directories)
        try:
            temp_dir.mkdir(parents=True, exist_ok=True)
            reports = [win_sandbox.grant_write(directory, sid) for directory, sid in zip(directories, sids)]
            # 包括其他授权根的 SID；缓存命中也复核，防止后续 ACL/授权变化让文件重新可写。
            win_sandbox.verify_read_only_files(
                tuple(path for report in reports for path in report.read_only_files), writable_sids=sids,
            )
        except (OSError, win_sandbox.SandboxUnavailable) as exc:
            # 可写根授权或受保护子树拒绝项失败均阻止执行；普通子树枚举失败单独报告。
            # 不把不完整的物化结果当作可用边界，失败后绝不裸跑。
            raise SecurityError(f"写侧沙箱：无法物化写授权（{exc}）") from exc
        self._sandbox_materialized = True
        return SandboxRequest(
            writable_sids=sids,
            extra_env=(("TMP", str(temp_dir)), ("TEMP", str(temp_dir)), ("TMPDIR", str(temp_dir))),
            # 跳过的目录随请求带走：这些目录的写权限与内部保护范围未完成核实。
            skipped=tuple(item for report in reports for item in report.skipped),
        )

    def _writable_grant_dirs(self) -> tuple[str, ...]:
        """「读写」授权里可以纳入沙箱可写范围的目录（不存在与受保护目标跳过）。

        「只读」档直接返回空：那一档的语义是"哪儿都不许写"，授权条目只能给到只读
        （旧值 ``deny`` 的守卫已随权限模型重构作废——`shell_sandbox_request` 在只读档更早
        就返回了无写授权的请求，这里是第二道保险）。
        """
        result: list[str] = []
        if self.external_policy.mode == "read-only":
            return ()
        for grant_path, access in self.external_policy.grants.items():
            if access != "read_write":
                continue
            directory = Path(grant_path)
            if not directory.is_dir() or protected_target_reason(directory):
                continue
            result.append(str(directory))
        return tuple(result)

    # ------------------------------------------------------------------ 路径

    def note_tool_result(self, chars: int) -> None:
        """累计一条工具结果进入上下文的字符数（2026-10-22）。

        只记账与置标记，**不在这里截断**：截断/落盘要带上"怎么取回全文"的说明，
        属于结果处理层；策略层只回答"这一轮用了多少、超没超"。
        """
        if chars <= 0:
            return
        self.result_budget_used += chars
        if self.result_budget_used >= self.result_budget_chars:
            self.result_budget_exceeded = True

    def require_result_spill(self) -> None:
        """检索工具自己的第二道门：子策略不能借 save_full 写文件。"""
        if not self.allow_result_spill:
            raise SecurityError("当前策略禁止保存检索结果文件")
        self.files.check_cancelled()
        self.require_workspace_writable("保存检索结果文件")

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
            SecurityError: 路径越界或命中敏感文件模式。
            FileOperationError: 目标不是文件或不存在。
        """
        resolved = self.resolve_path(path)
        self._reject_sensitive(resolved, verb="读取")
        if not resolved.is_file():
            raise FileOperationError(f"目标不是文件或不存在：{resolved.as_posix()}")
        return resolved

    def require_workspace_writable(self, verb: str) -> None:
        """当前档位是否允许写工作区；``read-only`` 档下一律拒绝（2026-10-15）。

        为什么放在**解析路径**这一层而不是只进判定：判定挡住的是模型直接发起的工具调用，
        而写还有别的入口（执行侧复核、别的工具内部复用）。边界要落在"解析出一个可写目标"这件事上
        ——codex 的 ``read-only`` 是只读挂载，语义就是"解析不出可写目标"。

        Raises:
            SecurityError: 当前是只读档。
        """
        if self.external_policy.allows_workspace_write():
            return
        raise SecurityError(
            f"当前是「只读」档，不能{verb}：要改文件请先在「设置 → 权限」切到「工作区可写」"
            "（这一步是产品设计上的边界，不是可以逐次批准的例外）"
        )

    def resolve_writable_file(self, path: str) -> Path:
        """解析待修改的已有文件（架构 §6.3）。

        Raises:
            SecurityError: 路径越界、命中敏感文件模式或当前是只读档。
            FileOperationError: 目标不是文件或不存在。
        """
        resolved = self.resolve_writable_path(path, verb="修改")
        if not resolved.is_file():
            raise FileOperationError(f"目标文件不存在或不是普通文件：{resolved.as_posix()}")
        return resolved

    def _is_benign_workspace_delete(self, plan: ShellPlan) -> bool:
        """工作区内、单目标、非递归的删除 → 与 `delete_file` 同口径免审（§6.6，2026-10-23）。

        放宽的动因：放宽前「命令删工作区单文件要审批、`delete_file` 删同一文件却免审」两套规则
        并存，模型据系统提示「工作区删除免审批」推断，回答里断言「没有出现审批卡」——与事实相反。

        **任何不确定都按"要审批"处理**：递归标志、通配符、命令串联、多目标、工作区外路径、
        硬拦截词、解析不出单一命令，一个都不豁免。宁可多问一次，不可放宽边界。
        """
        parts = [str(part) for part in plan.argv if str(part).strip()]
        if not parts:
            return False
        # Windows 内建命令经适配器执行时，整条命令是最后一个 argv 元素
        # （`cmd.exe /d /s /c del /q x`）——cmd 自己的 `/s` 不是递归标志，只能看这一段。
        command = parts[-1]
        if any(char in command for char in "|&;<>$`\n"):
            return False
        lowered = command.lower()
        if any(word in lowered for word in _HARDLINE_WORDS):
            return False
        match = _SINGLE_DELETE_SHAPE.match(command)
        if match is None:
            return False
        target = next((group for group in match.groups() if group), "")
        if not target or any(char in target for char in "*?$"):
            return False
        # `del /s`、`rm -r` 是递归：一律审批
        if any(flag in lowered.split() for flag in ("/s", "-r", "-rf", "--recursive")):
            return False
        base = plan.cwd
        if base is None:
            return False
        candidate = Path(target)
        if not candidate.is_absolute():
            candidate = base / candidate
        try:
            self.resolve_writable_path(str(candidate), verb="删除")
        except (SecurityError, OSError, ValueError):
            return False
        return True

    def resolve_writable_path(self, path: str, *, verb: str = "写入") -> Path:
        """解析写入目标路径；目标可以不存在（架构 §6.4）。

        Raises:
            SecurityError: 路径越界、命中敏感文件模式、或当前是只读档。
        """
        target = self.resolve_target(path)
        resolved = target.path
        self._reject_sensitive(resolved, verb=verb)
        self.require_side_effect_target(resolved)
        self.require_workspace_writable("写入文件")
        if target.access == "read":
            raise SecurityError(
                "该路径当前只允许读，不能写入：请在「设置 → 权限」中把这个目录授权为「可读写」"
            )
        return resolved

    # ------------------------------------------------------------------ 工具分类

    def _classify_interactive(
        self, tool_name: str, args: dict[str, Any]
    ) -> SafetyDecision:
        """交互终端 / 浏览器的判定（原样从 :meth:`classify_tool` 搬出来，行为不变）。"""
        if not self.allow_result_spill:
            return SafetyDecision(False, False, "只读子任务不能创建或控制持久会话")
        if tool_name in {"read_shell", "stop_shell", "list_shell_sessions"}:
            return SafetyDecision(True, False, "读取或关闭当前会话拥有的进程")
        if not self.external_policy.allows_workspace_write():
            return SafetyDecision(False, False, "交互终端与浏览器需要工作区可写权限")
        if tool_name == "start_shell":
            return self._classify_shell(args)
        if tool_name == "write_shell":
            from local_agent.security.shell_guard import match_blocked_patterns

            text = str(args.get("text") or "")
            if self.security.require_approval.destructive_shell and match_blocked_patterns(
                [], text, self.security.shell.blocked_patterns
            ):
                return SafetyDecision(False, True, "终端输入含破坏性命令，需要人工批准")
        if tool_name == "browser" and args.get("path"):
            try:
                self.resolve_writable_path(str(args["path"]))
            except SecurityError as exc:
                return SafetyDecision(False, False, str(exc))
        return SafetyDecision(True, False, "当前会话的交互操作，文件输出仍按目录授权检查")

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
        if tool_name in INTERACTIVE_TOOLS:
            return self._classify_interactive(tool_name, args)
        if tool_name in READ_ONLY_TOOLS:
            if tool_name in {"grep", "glob"} and args.get("save_full"):
                try:
                    self.require_result_spill()
                except SecurityError as exc:
                    return SafetyDecision(False, False, str(exc))
            return SafetyDecision(allowed=True, requires_approval=False, reason="只读工具")
        if not self.external_policy.allows_workspace_write() and tool_name in (
            *WRITE_TOOLS,
            *ALWAYS_APPROVAL_TOOLS,
            *GIT_WRITE_TOOLS,
            *MEMORY_TOOLS,
        ):
            # **只读档：写类工具一律拒绝**（2026-10-15）。放在这里而不是各工具的判定里，是为了
            # 让"这个档位下不能写"只有一个出口——漏掉一个工具就是一条静默的写通道。
            # 这不是"需要审批"，所以 `requires_approval=False`：没有批准能把它放行。
            return SafetyDecision(
                allowed=False,
                requires_approval=False,
                reason=(
                    "当前是「只读」档，不能写文件：要改文件请先在「设置 → 权限」切到「工作区可写」"
                ),
            )
        if tool_name in MEMORY_TOOLS:
            # 常驻记忆写在 agent 自己的存储里（数据根 `memories/`）：不进工作区、不碰用户文件，
            # 因此免审批。**放在只读档那道门之后**——它终究是"写"，只读档必须拒绝。
            return SafetyDecision(
                allowed=True, requires_approval=False, reason="常驻记忆（自有存储，免审批）"
            )
        if tool_name == "edit_file":
            return self._classify_edit(args)
        if tool_name == "write_file":
            return self._classify_write(args)
        if tool_name == "download_file":
            if not self.allow_result_spill:
                return SafetyDecision(False, False, "只读子任务不能下载文件到磁盘")
            concern = self._external_side_effect_decision(str(args.get("path") or ""), verb="下载")
            if concern is not None:
                return concern
            return self._classify_write(args)
        if tool_name == "execute_code":
            return self._classify_execute_code(args)
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
            if not self.is_external(self.resolve_path(str(args.get("path") or "."))) and not self.security.require_approval.delete_file:
                return SafetyDecision(True, False, "删除工作区文件由模型执行，无需人工审批")
            return SafetyDecision(
                allowed=False,
                requires_approval=True,
                reason=f"{tool_name} 属于强制审批动作，必须人工批准后才执行",
            )
        if tool_name.startswith(EXTERNAL_TOOL_PREFIX):
            return self._classify_mcp(tool_name)
        return SafetyDecision(
            allowed=False,
            requires_approval=False,
            reason=f"工具 {tool_name} 尚未接入或不在放行清单内",
        )

    def _classify_mcp(self, tool_name: str) -> SafetyDecision:
        """MCP 外部工具（架构 §6.13）。

        **免审批**（2026-10-21 按用户决定改）：一台 server 的 ``command`` 是用户亲手写进
        ``mcp.json`` 的，那本身就是授权；调用它暴露的工具等于使用已经授权的东西，不是一次新的
        决定。早先"一律审批"的实测结果是：模型连着调 3~5 个工具就要点 3~5 次卡，而人很快
        就不再读卡片内容直接点批准——那比不审批更糟，因为它把"审批"变成了仪式。

        免审批**不等于**没有边界，两条仍然在：

        - ``allow_result_spill=False``（只读子任务）直接拒绝：子 Agent 没有人盯着，
          而装配层本来也不给它 MCP 工具，这条是防御性的第二道门；
        - 名字必须来自本轮已知集合（``capabilities.is_visible`` 的 ``external_names``），
          模型幻觉出来的 ``mcp__xxx__yyy`` 依然执行不了。
        """
        if not self.allow_result_spill:
            return SafetyDecision(False, False, "只读子任务不能调用 MCP 外部工具")
        return SafetyDecision(
            allowed=True,
            requires_approval=False,
            reason=f"{tool_name} 是外部系统的操作（MCP），按 server 配置授权执行",
        )

    def _external_side_effect_decision(
        self, path: str, *, verb: str
    ) -> SafetyDecision | None:
        """工作区外副作用与受保护目标的统一判定；``None`` 表示按常规规则继续判。

        - 受保护目标（根目录、主目录、Git 元数据、系统关键目录）→ **硬拒绝**，不可审批；
        - 越界且未被授权 → **硬拒绝**（失败闭合），提示指向「设置 → 权限」；
        - 外部目标**只允许读**（只有只读授权，或「放行」档下未逐条授权的目标）→ **拒绝写操作**
          并说明写需要逐条授权。一律硬拒绝、不经审批：两家在工作区外写上的表现也是"直接失败"，
          不是"问一次"（2026-10-14）；
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
                    f"该路径当前只允许读（只读授权，或「放行」档仍未逐条授权），不能{verb}："
                    f"{target.path.as_posix()}。"
                    "写需要逐条授权：请在「设置 → 权限」中把这个目录授权为「可读写」，"
                    "或改到工作区内操作"
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
            allowed=True,
            requires_approval=False,
            reason=(
                f"提交到当前 Git 分支：路径 {listed}；提交信息「{message[:80]}」。"
                "提交由模型执行，无需人工审批"
            ),
        )

    def _classify_edit(self, args: Mapping[str, Any]) -> SafetyDecision:
        # 输入模式：精确串替换（默认）或 unified diff（T22）。安全判定与工具执行**共用**
        # diff_guard.edit_input_mode，不能两处各写一套互斥规则（架构 §6.3）。
        try:
            mode = edit_input_mode(args.get("old_text"), args.get("new_text"), args.get("patch"))
        except FileOperationError as exc:
            return SafetyDecision(allowed=False, requires_approval=False, reason=str(exc), reason_kind="input")
        concern = self._external_side_effect_decision(str(args.get("path", "")), verb="修改")
        if concern is not None:
            return concern
        if mode == "patch":
            try:
                plan = parse_patch(str(args.get("patch") or ""))
            except FileOperationError as exc:
                # 结构非法的 patch 永远不可能被执行：硬拒绝，不推进审批——不能让用户为一个
                # 必然失败的补丁点"批准"（架构 §6.3：空 Patch/错 hunk 直接拒绝）。
                return SafetyDecision(allowed=False, requires_approval=False, reason=str(exc), reason_kind="input")
            changed_lines = plan.changed_lines
            what = f"unified diff（{len(plan.hunks)} 个 hunk）"
        else:
            changed_lines = count_changed_lines(
                str(args.get("old_text") or ""), str(args.get("new_text") or "")
            )
            what = "精确串替换"
        limit = self.security.large_edit.max_files_without_approval
        path = str(args.get("path") or "")
        # **大范围编辑按"本轮触及的不同文件数"判**（2026-10-15）。
        #
        # 原来判的是"单次改动行数 > 200"，而已改动行数 = 新增 + 删除（`diff_guard.count_changed_lines`），
        # 那个量级恰好是"重写一个函数"的常态：实证里它把正常重构变成了逐次弹卡，用户体感是
        # "稍微多改点就被拦"。dsh 没有这类阈值——它的 `edit` 工具只校验参数合法性
        # （`packages/fs/tool-fs/src/edit.ts`），覆盖前必须读（我们的等价物是 `expected_sha256`）。
        #
        # 只对 `edit_file` 计数：**新建文件不计**。"产出一组文件"（生成站点、脚手架）不该被这条拦住，
        # 而"编辑"才对应四项硬约束里的"大范围编辑"。批准一次后本轮内不再问（见 `note_large_edit_approved`）。
        #
        # 记账与判定必须用**同一个 key**（`_path_key`）：模型传的是相对路径，执行侧记的是解析后的
        # 绝对路径，直接比字符串会导致"改过的文件仍被当成新文件"——那样额度永远用不掉。
        exceeds_batch = (
            self.security.require_approval.large_batch_edit
            and _path_key(self.workspace_root, path) not in self._edited_paths
            and len(self._edited_paths) >= limit
            and not self._large_edit_approved
        )
        if exceeds_batch:
            return SafetyDecision(
                allowed=False,
                requires_approval=True,
                reason=(
                    f"{what}，本轮已经改过 {len(self._edited_paths)} 个文件，这次要改的是新文件"
                    f"（{path or '未知路径'}），属于大范围修改：需要一次人工批准。"
                    f"批准后本轮不再逐个询问（免审上限 {limit} 个文件，可在 security.large_edit 调整）"
                ),
                large_edit=True,
            )
        return SafetyDecision(
            allowed=True,
            requires_approval=False,
            reason=f"{what}，改动 {changed_lines} 行，在免审范围内",
        )

    def note_edited(self, path: Path) -> None:
        """记下"本轮改过这个文件"（在 ``edit_file`` 真正改完之后调用）。

        作用域就是本策略实例 = 本轮：新的一轮会重建策略，计数自然归零。
        """
        self._edited_paths.add(_path_key(self.workspace_root, str(path)))

    def note_large_edit_approved(self) -> None:
        """记下"本轮的大范围修改已获人工批准"：后续文件不再逐个弹卡。"""
        self._large_edit_approved = True

    def edited_file_count(self) -> int:
        """本轮已经改过几个文件（诊断与测试用）。"""
        return len(self._edited_paths)

    def _classify_write(self, args: Mapping[str, Any]) -> SafetyDecision:
        concern = self._external_side_effect_decision(str(args.get("path", "")), verb="写入")
        if concern is not None:
            return concern
        if args.get("overwrite") and (
            self.security.require_approval.overwrite_existing_file
            or self.is_external(self.resolve_path(str(args.get("path") or ".")))
        ):
            return SafetyDecision(
                allowed=False,
                requires_approval=True,
                reason="覆盖已有文件需要人工审批",
            )
        return SafetyDecision(
            allowed=True, requires_approval=False, reason="创建新文件，未覆盖任何已有内容"
        )

    def _classify_execute_code(self, args: Mapping[str, Any]) -> SafetyDecision:
        """`execute_code` 的判定（2026-10-22）。

        它跑的是模型给的任意 Python：**逐条审计代码内容做不到**，假装能审反而更糟
        （例如只匹配 `rm -rf` 却漏掉 `shutil.rmtree`，会给出"已检查"的错觉）。
        因此边界与 `run_shell` 同级——工作区可写才允许，执行侧仍受沙箱、取消、寿命与
        输出上限约束。真正的差别只是"少写一个临时脚本"，不放宽任何门。
        """
        if not self.external_policy.allows_workspace_write():
            return SafetyDecision(False, False, "运行代码需要工作区可写权限")
        code = args.get("code")
        if not isinstance(code, str) or not code.strip():
            return SafetyDecision(False, False, "execute_code 缺少 code 参数")
        return SafetyDecision(
            True, False, "运行 Python 代码，与 run_shell 同一执行通道与沙箱"
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
        # 提权请求（escalation）要在**所有分支之前**定下来：它是一票否决式的参数有效性检查。
        escalate = bool(args.get("escalate_sandbox"))
        if escalate and not self.shell_sandbox_active():
            # 没有沙箱就没有"退出沙箱"这回事：**拒绝**而不是静默忽略——静默忽略会让模型以为
            # 自己拿到了更宽的执行环境（"写了却没生效"的另一种形态）。
            return SafetyDecision(
                allowed=False,
                requires_approval=False,
                reason=(
                    "当前没有启用写侧沙箱，escalate_sandbox 没有意义：去掉这个参数重试"
                    "（提权只在真的有写边界时才存在）"
                ),
            )
        # **档位在这里决定"常规命令问不问"**（2026-10-14）：档位的设计意图本来就是"决定常规小改动
        # 是否需要逐次询问"，但在此之前 Shell 审批完全没读过档位——于是"选了放行还什么都要问"。
        mode = self.external_policy.mode
        # 2026-10-23 放宽（§6.6，用户批准）：**工作区内、单目标、非递归的删除**与
        # `delete_file` 工具同口径免审。放宽前这两条规则不一致，模型据"工作区删除免审批"
        # 推断后断言"没有出现审批卡"，最终回答与事实相反（实机复现三次）。
        # 递归、通配、串联、多目标、工作区外、硬拦截词**一律不豁免**，照旧走审批。
        if (plan.destructive and self.security.require_approval.destructive_shell
                and not plan.environment_unavailable
                and not self._is_benign_workspace_delete(plan)):
            # 环境不可用的除外：那种命令**批准了也起不来**，走拒绝（别退回"点了批准然后启动失败"）。
            return SafetyDecision(
                allowed=False,
                requires_approval=True,
                reason=f"{plan.reason}；破坏性命令任何档位都需要人工批准（硬拦截清单）",
            )
        if escalate and plan.executable_path and not plan.environment_unavailable:
            # **运行中提权**（escalation，2026-10-14）：本次请求退出写侧沙箱 → 不管哪一档、
            # 不管命令本身是否在白名单里，都要人工批准；批准只对**这一条**命令生效
            # （指纹里含 `escalate_sandbox`）。对齐 codex 的 `RequireEscalated` 与 dsh 的 `escalate()`。
            # 起不来的命令不进这条路：那会退化成"点了批准然后启动失败"。
            return SafetyDecision(
                allowed=False,
                requires_approval=True,
                reason=(
                    f"本次请求退出写侧沙箱（仅这一次，工作区外也可写）：{plan.reason}。"
                    "任何档位都需要人工批准——批准后这一条命令不再受写边界约束"
                ),
            )
        if plan.safe_read_only:
            # 只读免审不能覆盖提权或工作区外 cwd 的审批要求。
            return SafetyDecision(allowed=True, requires_approval=False, reason=plan.reason)
        if (
            self.external_policy.mode == "read-only"
            and not win_sandbox.is_supported()
            and plan.requires_approval
        ):
            # **只读档 + 没有 OS 沙箱的平台**：命令里一句重定向就能写盘，于是"只读"是句空话。
            # 这种组合下常规命令仍逐条问——不是不信任命令，而是这句承诺必须由某样东西兜住
            # （Windows 上由"无写授权的受限令牌"兜，其它平台只能靠人看一眼）。
            return SafetyDecision(
                allowed=False,
                requires_approval=True,
                reason=(
                    f"只读档且当前平台没有 OS 级写边界：命令需要人工批准（{plan.reason}）"
                ),
            )
        if plan.requires_approval:
            # **常规命令不再逐条询问**（2026-10-15 权限模型重构，用户裁定）：
            # 档位决定"写到哪"，沙箱/授权条目负责把边界落实，所以"命令本身要不要问"不再是
            # 需要用户逐条回答的问题——workspace-write 档的语义就是"可以跑 powershell"。
            #
            # 放在前面那些判定**之后**，因此以下照旧要人工批准：
            # 破坏性命令（`DESTRUCTIVE_PATTERNS`）、越是界（`escalate_sandbox`）、工作区外 cwd。
            # 也**不动**硬拒绝（`plan.requires_approval=False` 的那类：非法命令结构、带路径的
            # 可执行文件、环境里起不来的命令）——这里只免掉"审批"，不免掉"拒绝"。
            # plan.reason 包含下层的旧审批结论；这里已免审，改由结构字段说明实际执行方式。
            execution = (
                describe_adapter(plan.executor) if plan.uses_shell_adapter
                else "argv（按已解析的可执行文件路径与参数数组直接执行）"
            )
            return SafetyDecision(
                allowed=True,
                requires_approval=False,
                reason=f"权限档位「{mode}」：常规命令免审批；执行方式：{execution}",
            )
        return SafetyDecision(
            allowed=plan.allowed,
            requires_approval=plan.requires_approval,
            reason=plan.reason,
            reason_kind="environment" if plan.environment_unavailable else "policy",
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
