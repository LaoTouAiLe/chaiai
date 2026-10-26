"""安全 Shell 工具：``run_shell``（架构 §6.6、开发文档 T07/T12）。

执行方式固定为 ``shell=False`` + argv 数组，工作目录与环境变量都由安全策略给出：

- 常规命令按权限档位执行；破坏性命令、外部工作目录与退出沙箱请求仍需人工批准；
- 含 Shell 语法的命令由平台适配器执行（Windows ``cmd.exe /d /s /c`` 或
  ``pwsh -NoProfile -NonInteractive -Command``、POSIX ``/bin/sh -c``），适配器本身
  仍以 argv 数组启动，不交给 Python 的 ``shell=True``；
- 输出按 ``security.shell.max_output_chars`` 截断；超时**与取消**都按**进程树**终止并如实记录
  （架构 §17.4：``taskkill /F /T`` / ``killpg``，手段与系统返回进 ``note``，退出码只写真实值）；
- **运行中提权**（escalation，2026-10-14）：``escalate_sandbox=True`` 表示"这一次退出写侧沙箱"，
  任何档位都要人工批准、批准只对这一次生效（对齐 codex 的 ``RequireEscalated`` 与 dsh 的
  ``escalate()``）；沙箱没开时该参数被直接拒绝，不静默忽略；
- 子进程环境只含基础变量与显式放行的变量名，**不含任何密钥**；
- 退出码、标准输出、错误输出、超时状态、执行方式与耗时都会返回，供反思节点机械校验。
"""

from __future__ import annotations

import json
import locale
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from langchain_core.tools import BaseTool, tool

from local_agent.exceptions import SecurityError
from local_agent.security.approval import action_fingerprint
from local_agent.security.path_guard import display_path
from local_agent.security.policy import SecurityPolicy
from local_agent.security.process_guard import ProcessCancelledError
from local_agent.security.shell_guard import (
    build_environment,
    build_shell_invocation,
    prepare_shell,
)
from local_agent.security.win_sandbox import SkippedDirectory
from local_agent.tools.output_decoding import OutputDecoder
from local_agent.tools.progress import current_call_id, output_reporter
from local_agent.tools.result_types import ShellResult

#: 说明里最多逐个列出几个被跳过的目录（其余只报数量）：note 是给模型与用户看的判断依据，
#: 不是一份目录清单。
_SKIPPED_DIRS_SHOWN = 3


def _skipped_grant_note(skipped: tuple[SkippedDirectory, ...], workspace_root: Path) -> str:
    """写侧沙箱跳过了哪些目录的授权，以及**怎么办**——如实说出去，不静默（2026-10-15）。

    为什么必须有这句：`grant_write` 现在会跳过权限不足的目录（原来是把整批物化打死），
    未枚举目录的实际写权限及内部保护范围不能从“跳过”推断；不说出去，用户会误以为边界已核实。

    后半句是**可照做的下一步**（2026-10-15 用户裁定"B1"）：只说"会被拒绝"没有指路。
    指的两条路都真实存在——`escalate_sandbox=true` 是产品里现成的单次出口（要人工批准、
    只对这一次生效），而"取得目录的修改权限"才是真正解决权限问题的那一步。
    **不提"把沙箱关掉"**：那等于把整轮命令的写边界一起去掉，代价与该目录的收益不成比例；
    沙箱随档位生效，不再存在独立设置开关。
    """
    shown = "、".join(
        f"{display_path(Path(item.path), workspace_root)}（{item.reason}）"
        for item in skipped[:_SKIPPED_DIRS_SHOWN]
    )
    extra = "" if len(skipped) <= _SKIPPED_DIRS_SHOWN else f" 等共 {len(skipped)} 个"
    return (
        f"写侧沙箱：{len(skipped)} 个目录没能授权或完成检查，不能确认其中的实际写入与保护范围"
        f"（命令本身照常执行）——{shown}{extra}。"
        "要写这些目录：用 escalate_sandbox=true 单独跑那一条命令（需人工批准，只对这一次生效）；"
        "若是权限问题，先让当前账户取得修改该目录权限的权限。"
    )


def run_shell_command(
    command: str,
    *,
    policy: SecurityPolicy,
    cwd: str = ".",
    timeout_seconds: int | None = None,
    env_keys: list[str] | None = None,
    escalate_sandbox: bool = False,
) -> ShellResult:
    """执行一条通过安全判定的命令。

    Args:
        command: 命令字符串。普通 Shell 语法按本轮档位运行；破坏性命令等仍须有效人工批准。
        policy: 安全策略。
        cwd: 相对工作区根目录的执行目录。
        timeout_seconds: 超时秒数，不得超过配置上限。
        env_keys: 需要额外透传的环境变量名（禁止疑似密钥的名字）。
        escalate_sandbox: 本次请求**退出写侧沙箱**（escalation，2026-10-14）。为真时这一条命令
            不进沙箱，因此**必须持有与它一致的人工批准**（指纹里含 ``escalate_sandbox``）——
            校验在这里**再做一次**，不能只靠判定阶段。

    Returns:
        退出码、输出、超时状态、执行方式与耗时。

    Raises:
        SecurityError: 判定不放行、没有有效批准，或命令无法启动。
    """
    # 先定住有效超时：省略时取**配置值**（`security.shell.default_timeout_seconds`），
    # 与安全判定、动作指纹、工具 Schema 完全同一语义。判定与指纹必须用同一个值，
    # 否则审批节点按默认值算、工具按另一套算，批准会莫名失效。
    requested_timeout = (
        timeout_seconds
        if timeout_seconds is not None
        else policy.security.shell.default_timeout_seconds
    )
    plan = prepare_shell(
        command,
        config=policy.security.shell,
        workspace_root=policy.workspace_root,
        cwd=cwd,
        timeout_seconds=requested_timeout,
        env_keys=env_keys,
        external_policy=policy.external_policy,
    )
    # 验证类命令的轮内去重（2026-10-22）：同命令 + 文件未变 → 直接回上次结果。
    # 实测的浪费正是"测试通过后又跑一遍整份回归"；只对无副作用的检查生效。
    cache_key = (command, str(plan.cwd), policy.file_change_version)
    if _cacheable_command(command):
        cached = policy.command_cache.get(cache_key)
        if cached is not None:
            return ShellResult.model_validate_json(cached)
    if plan.cwd is None:
        # 判定没给出可执行目录（命令为空、cwd 非法、超时超限等）→ 失败闭合
        raise SecurityError(plan.reason)

    # 与审批节点同源的指纹：只有"同一次调用 + 指纹一致"的批准才能放行（架构 §5.3）。
    # `escalate_sandbox` **必须进指纹**：否则"普通调用"的批准会被复用到"退出沙箱"的调用上。
    escalated = bool(escalate_sandbox)
    fingerprint = action_fingerprint(
        "run_shell",
        {
            "command": command,
            "cwd": cwd,
            "timeout_seconds": requested_timeout,
            "env_keys": list(env_keys or []),
            "escalate_sandbox": escalated,
        },
        policy=policy,
    )
    if escalated and not policy.is_granted(fingerprint):
        # 提权是**边界决定**，不是命令风险：白名单里免审的命令带了它也照样要批准。
        # 这一条放在 `plan.allowed` 之前——免审命令 `plan.allowed=True`，否则会从这里溜过去。
        raise SecurityError(
            "本次请求退出写侧沙箱必须人工审批（批准只对这一次生效）：" f"{plan.reason}"
        )
    decision = policy.classify_tool("run_shell", {
        "command": command, "cwd": cwd, "timeout_seconds": requested_timeout,
        "env_keys": list(env_keys or []), "escalate_sandbox": escalated,
    })
    if not decision.allowed and not policy.is_granted(fingerprint):
        raise SecurityError(decision.reason)
    if escalated and not policy.shell_sandbox_active():
        raise SecurityError("当前没有启用写侧沙箱，不能请求退出沙箱")

    encoding = locale.getpreferredencoding(False) or "utf-8"
    # 写侧沙箱（由档位派生，没有独立开关）：生效时命令经受限令牌运行，
    # **只换"怎么起"**。`shell_sandbox_request()` 自己失败闭合（平台不支持、或**可写根**授权
    # 不了时抛 SecurityError），这里不吞它——"以为有沙箱其实没有"是最危险的失效方式。
    # `escalate=True`（已拿到批准）时它返回 None：这一条命令不进沙箱。
    # 先求值一次：它的返回值还要用在下面的 note 里（跳过授权了哪些目录）。
    system_taskkill = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32" / "taskkill.exe"
    process_control = (
        os.name == "nt" and plan.executor == "argv" and plan.executable_path is not None
        and Path(plan.executable_path).resolve() == system_taskkill.resolve()
        and policy.external_policy.mode != "read-only"
    )
    # 系统 taskkill 仅操作进程；LUA 令牌会拒绝其进程控制初始化。
    # 只豁免实际系统程序的 argv 直调用，脚本、同名程序、重定向与管道仍由写边界约束。
    sandbox = None if process_control else policy.shell_sandbox_request(escalate=escalated, cwd=plan.cwd)
    report = output_reporter()
    decoders = {name: OutputDecoder(encoding) for name in ("stdout", "stderr")}
    max_chars = policy.security.shell.max_output_chars
    text_parts: dict[str, list[str]] = {name: [] for name in decoders}
    lengths = {name: 0 for name in decoders}
    received = {name: 0 for name in decoders}
    cuts = {name: False for name in decoders}

    def capture(stream: str, text: str) -> None:
        report(stream, text)
        remaining = max_chars - lengths[stream]
        kept = text[:remaining]
        if kept:
            text_parts[stream].append(kept)
            lengths[stream] += len(kept)
        cuts[stream] = cuts[stream] or len(text) > remaining

    def on_output(stream: str, data: bytes) -> None:
        received[stream] += len(data)
        capture(stream, decoders[stream].feed(data))

    def on_idle() -> None:
        for stream, decoder in decoders.items():
            capture(stream, decoder.flush_idle())

    started = time.perf_counter()
    # 转后台（2026-10-21）：命令 id 用工具调用的 id，前端据此在运行中的行上发"转后台"指令。
    # 拿不到调用 id（例如直接调函数）时自己生成一个，功能不受影响，只是前端没有入口。
    command_id = current_call_id() or f"cmd-{uuid4().hex[:8]}"
    detach_event = policy.commands.open(command_id)

    def _record_detached(process: Any, collected: dict[str, bytes]) -> None:
        """转入后台时登记进程句柄；**不返回** ``attach`` 的结果。

        ``ProcessRegistry.run`` 对 ``on_detached`` 的契约是"回调返回 None"（它丢弃返回值）。
        原先写成 lambda 直接返回 ``DetachedCommand``：运行时无害，但契约不一致，类型检查也会拦。
        """
        policy.commands.attach(command_id, process, collected)

    try:
        # 走**子进程登记表**（§17.4）：它有进程句柄，因此超时与取消都能按**进程树**终止，
        # 而 `subprocess.run` 只能杀直接子进程、且中途无法干预。
        outcome = policy.processes.run(
            build_shell_invocation(plan),
            cwd=plan.cwd,
            env=build_environment(plan.env_keys),
            timeout_seconds=float(plan.timeout_seconds) if plan.timeout_seconds is not None else None,
            sandbox=sandbox,
            on_output=on_output,
            on_idle=on_idle,
            detach_event=detach_event,
            on_detached=_record_detached if detach_event is not None else None,
        )
        for stream, decoder in decoders.items():
            # 同样兼容未提供完整增量回调的执行适配器；常规登记表已逐块送出全部字节。
            raw = getattr(outcome, stream)
            if len(raw) > received[stream]:
                on_output(stream, raw[received[stream]:])
            capture(stream, decoder.finish())
    except ProcessCancelledError as exc:
        # 取消与命令启动撞在一起：**如实**说"命令未执行"，不伪装成"执行失败"。
        raise SecurityError(str(exc)) from exc
    except OSError as exc:
        raise SecurityError(f"命令无法启动（{type(exc).__name__}）：{exc}") from exc
    finally:
        # 正常结束（或异常）时撤销"可转后台"登记；转后台路径里 attach 已经先把它取走了。
        # 不撤销的后果：用户对着一条已经跑完的命令点"转后台"，会命中一个再也唤不醒的信号。
        policy.commands.close(command_id)

    duration_ms = int((time.perf_counter() - started) * 1000)
    stdout = "".join(text_parts["stdout"])
    stderr = "".join(text_parts["stderr"])
    truncated = any(cuts.values())

    notes: list[str] = []
    if outcome.detached:
        # 转后台不等于结束：退出码未知、进程仍在跑。措辞必须让模型知道它还能继续做别的，
        # 也告诉它稍后怎么取结果（否则它会以为命令失败了又重跑一遍）。
        notes.append(
            "已按用户请求转入后台继续运行，命令尚未结束；"
            "可以继续做别的事，稍后用 read_command 查看输出，或用 stop_command 结束"
        )
    if outcome.timed_out:
        notes.append(f"命令在 {plan.timeout_seconds} 秒内未结束，已终止")
    if outcome.cancelled:
        notes.append("本轮已被取消，命令已被终止")
    if outcome.termination:
        # 终止手段与系统返回如实照录（§17.4）：不写"已停止"这种没有证据的话。
        notes.append(outcome.termination)
    if truncated:
        notes.append(f"输出超过 {max_chars} 字符，已截断")
    if sandbox is not None and sandbox.skipped:
        notes.append(_skipped_grant_note(sandbox.skipped, policy.workspace_root))
    # 本机原生对照已复现：写侧受限令牌下 Schannel 获取凭据失败，
    # 同权限的 Python/OpenSSL HTTPS 可用。只提供恢复线索，结果与后续命令仍走原契约。
    diagnostics = f"{stdout}\n{stderr}".casefold()
    if (
        sandbox is not None
        and outcome.exit_code == 35
        and not outcome.timed_out
        and not outcome.cancelled
        and "schannel" in diagnostics
        and "acquirecredentialshandle" in diagnostics
        and ("sec_e_no_credentials" in diagnostics or "0x8009030e" in diagnostics)
    ):
        notes.append(
            "Windows Schannel 获取 TLS 凭据失败（SEC_E_NO_CREDENTIALS），"
            "可能与写侧受限令牌的兼容性有关。若任务是普通公开 HTTPS 下载，"
            "可在当前权限内改用 Python urllib.request（使用默认 SSL 上下文、保持证书验证），"
            "沿用任务中的 URL 和目标文件；需要认证、客户端证书或代理时须保留原请求语义。"
            "替代命令仍按当前策略执行与审批；不要仅为此错误退出沙箱或关闭证书验证。"
            "原命令仍是失败，后续须依据退出码、HTTP 状态及文件内容或哈希确认下载成功。"
        )

    result = ShellResult(
        command=command,
        argv=build_shell_invocation(plan),
        # 展示路径（§6.6 / §15.8）：工作区内给相对路径、工作区外保持绝对。工具结果只喂模型与页面，
        # 而页面此前就此显示工作区绝对路径（v0.8 T73 实证）。
        cwd=display_path(plan.cwd, policy.workspace_root),
        exit_code=outcome.exit_code,
        stdout=stdout,
        stderr=stderr,
        timed_out=outcome.timed_out,
        duration_ms=duration_ms,
        truncated=truncated,
        note="；".join(notes) if notes else None,
        executor=plan.executor,
        status="running" if outcome.detached else "completed",
        command_id=command_id if outcome.detached else None,
    )
    # 只缓存"验证类 + 成功 + 前台结束"的结果；转后台、超时、失败都不缓存。
    if (
        _cacheable_command(command)
        and result.exit_code == 0
        and not result.timed_out
        and result.status == "completed"
    ):
        if len(policy.command_cache) >= 50:
            policy.command_cache.clear()
        policy.command_cache[cache_key] = result.model_dump_json()
    return result


def _quote_arg(value: object) -> str:
    """把路径参数变成 argv 里的一项：含空格就加引号（Windows 上 `Program Files` 很常见）。"""
    text = str(value)
    return f'"{text}"' if " " in text else text


#: 允许"同命令直接回上次结果"的验证类命令前缀（2026-10-22）。
#: **只收无副作用的检查**：跑测试、类型检查、看差异。`npm start`、`git commit`、构建产物之类
#: 绝不缓存——跳过它们不是省时间，是改变行为。
_CACHEABLE_PREFIXES: tuple[str, ...] = (
    "pytest", "python -m pytest", "py -m pytest",
    "npx vitest", "npx jest", "npm test", "npm run test",
    "npx tsc", "npm run typecheck", "mypy", "ruff check", "ruff",
    "git status", "git diff", "git log",
)


def _cacheable_command(command: str) -> bool:
    text = " ".join(command.strip().lower().split())
    return any(text == prefix or text.startswith(prefix + " ") for prefix in _CACHEABLE_PREFIXES)


def build_shell_tools(policy: SecurityPolicy) -> list[BaseTool]:
    """按安全策略构造 Shell 工具。"""

    @tool
    def run_shell(
        command: str,
        cwd: str = ".",
        # 省略（None）时用 security.shell.default_timeout_seconds，不写死常量，
        # 否则"配置调小 + 模型省略参数"会在执行阶段被判超限
        timeout_seconds: int | None = None,
        env_keys: list[str] | None = None,
        # 运行中提权（escalation，2026-10-14）：默认 False = 照常按写侧沙箱跑。
        escalate_sandbox: bool = False,
    ) -> str:
        """执行一条命令，返回退出码、标准输出与错误输出。

        **用户可以把长命令转后台**：此时结果里 `status` 是 `running`、带 `command_id`，
        表示命令仍在跑而不是失败。不要重跑它，继续做别的事，用 `read_command` 取输出、
        `stop_command` 结束。

        常规命令不因未命中白名单而审批，管道/重定向由平台适配器（cmd.exe / pwsh / sh）执行。
        Windows 的只读档和工作区可写档进入写侧沙箱；完全放行档不进沙箱。
        破坏性命令、工作区外 cwd、escalate_sandbox 仍需人工批准；不支持的结构仍会拒绝。
        非 Windows 的只读档缺少 OS 写边界，只读免审清单以外的常规命令仍需批准。

        需要人工批准的命令会**弹出审批卡**——那是"等用户点一下"，不是失败：把要做什么讲清楚、
        等用户决定即可，不要为了绕开审批去改写等价命令。用户若拒绝，**本轮**不会再弹卡，
        如实说明即可（下一轮可以再提）。

        Args:
            command: 命令字符串。
            cwd: 相对路径以当前工作区为根；配置允许外部路径时可使用显式绝对路径，工作区外cwd无需单独审批，写边界仍由权限档位和目录授权决定。
            timeout_seconds: 可选超时；默认不限时，用户仍可取消。
                **慢命令请显式传更大的值**（冷启动浏览器、安装依赖、真实构建这类动辄几十秒）：
                上限由 `security.shell.max_timeout_seconds` 决定，**默认不设上限**（没配这一项就是随便传）。
            env_keys: 需要额外透传的环境变量名，禁止密钥类变量名。
            escalate_sandbox: 本次请求退出写侧沙箱，只对这一次生效。仅在"写侧沙箱开着、而这条命令
                的确需要写工作区之外的固定位置"时使用：它会让这条命令不再受写边界约束，因此任何档位
                都必须人工批准（白名单里的命令也一样）。沙箱没开时带这个参数会被直接拒绝，
                去掉它重试即可。
        """
        return run_shell_command(
            command,
            policy=policy,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
            env_keys=env_keys,
            escalate_sandbox=escalate_sandbox,
        ).model_dump_json()

    @tool
    def read_command(command_id: str, cursor: int = 0, max_chars: int = 8000) -> str:
        """读取一条**已转后台**的命令的增量输出。

        `run_shell` 的结果里 `status` 是 `running` 时才有意义：命令还没结束，用户让你先做别的。
        用返回的 `next_cursor` 作为下一次的 `cursor`，就只拿新增的部分，不会重复读同一段。
        命令可能仍在跑（`status=running`）、也可能已经结束（`status=finished`，带退出码）——
        两种都不是失败，按事实告诉用户即可。
        """
        return json.dumps(
            policy.commands.read(command_id, cursor=cursor, max_chars=max_chars),
            ensure_ascii=False,
        )

    @tool
    def stop_command(command_id: str) -> str:
        """结束一条转后台的命令（按进程树终止）。

        只在"这条后台命令已经没必要继续"时用；它不会影响别的命令。终止结果如实返回。
        """
        return json.dumps(
            {"command_id": command_id, "stopped": policy.commands.stop(command_id)},
            ensure_ascii=False,
        )

    @tool
    def execute_code(
        code: str,
        cwd: str = ".",
        timeout_seconds: int | None = None,
    ) -> str:
        """运行一段 Python 代码，返回退出码与输出。

        **什么时候用它**：需要在工作区里做一次性的计算或批量改动（解析文件、生成内容、
        跑一段验证脚本）时，用它**代替**"写临时脚本 → run_shell → 删脚本"三步。
        代码写在临时目录里执行、结束后自动清理，不会在你的项目里留下脚本文件。

        Args:
            code: 完整可运行的 Python 代码。
            cwd: 工作目录（同时加入 import 搜索路径），默认工作区根。
            timeout_seconds: 前台等待上限；省略时沿用 shell 的默认值。

        与 `run_shell` 同一套沙箱、超时与取消语义；长任务同样可以由用户转到后台。
        代码改动工作区文件时，变更面板不会逐条记录——这一点与 shell 写入一致。
        """
        # 脚本必须落在**工作区**里：Windows 写侧沙箱只授权工作区，放系统临时目录会
        # `Permission denied`（2026-10-22 实测）。`.local-agent/` 是自忽略目录，
        # 不会污染仓库；执行完就把这次脚本目录删掉。
        base = policy.workspace_root / ".local-agent"
        base.mkdir(parents=True, exist_ok=True)
        ignore = base / ".gitignore"
        if not ignore.exists():
            ignore.write_text("*\n", encoding="utf-8")
        script_dir = base / "tmp" / uuid4().hex
        script_dir.mkdir(parents=True, exist_ok=True)
        try:
            script = script_dir / "snippet.py"
            # 把 cwd 放进 import 路径：否则 `import 项目模块` 会失败（sys.path[0] 是脚本目录）。
            resolved_cwd = policy.resolve_path(cwd) if cwd else policy.workspace_root
            script.write_text(
                f"import sys\nsys.path.insert(0, {str(resolved_cwd)!r})\n{code}\n",
                encoding="utf-8",
            )
            result = run_shell_command(
                f"{_quote_arg(sys.executable)} {_quote_arg(script)}",
                policy=policy,
                cwd=cwd,
                timeout_seconds=timeout_seconds,
            )
            # 命令里是临时脚本的真实路径，对用户没有意义：换成能读懂的说明。
            return result.model_copy(
                update={"command": f"python 代码片段（{len(code)} 字符）"}
            ).model_dump_json()
        finally:
            shutil.rmtree(script_dir, ignore_errors=True)

    return [run_shell, read_command, stop_command, execute_code]
