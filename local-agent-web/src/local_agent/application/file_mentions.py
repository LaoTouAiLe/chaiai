"""`@路径` 引用（2026-10-30）：把用户在提问里点名的文件读进本轮上下文。

**为什么要有**：此前用户想说"看下 `src/app.py`"只能手打路径，模型还得再调一次
`read_file` 才知道内容——多一轮工具调用，多文件时还容易读错对象。`@` 引用把"点名"
变成一次性注入：本轮第一个 token 之前，文件内容已经在上下文里。

**走哪条通道**：拼进 graph 的 `context_prefix`（一条独立 SystemMessage），
**不拼进 `user_input`**。后者会被存成 `conversation_turns.question`，用户就会在气泡里
看到几千字文件内容——这正是侧聊首轮踩过的坑（见 `service._side_chat_context` 的注释）。

**预算与降级**（2026-10-30 参照 hermes 的 `agent/context_references.py` 改进）：
- 上限按**模型窗口**的比例算：软限 25%、硬限 50%。拿不到窗口时退回一组保守的 token 数。
- 超预算的文件**不截断**，改成"只给路径 + 让模型自己用工具读"。截断是最坏的选择：
  模型会看到半截代码却以为完整，然后基于残缺内容下结论。
- 所有失败（不存在、越界、二进制、超额）都汇总成一段 `--- 引用提示 ---` 交给模型，
  而不是静默跳过——模型要知道"没读到"和"没有这个文件"是两件事。

**边界**（刻意保守）：
- 只认工作区内的**普通文件**。目录、越界路径一律拒绝，且**不报错**给用户：
  路径打错一个字不该让整条消息发不出去。
- 路径含空格时用反引号/双引号/单引号包裹（与 hermes 的 `REFERENCE_PATTERN` 同一套写法）。
"""

from __future__ import annotations

import mimetypes
import re
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from local_agent.cancellation import CancellationToken
from local_agent.context.token_budget import estimate_tokens
from local_agent.security.path_guard import is_sensitive_path
from local_agent.security.process_guard import ProcessCancelledError, ProcessRegistry

if TYPE_CHECKING:
    from local_agent.config import AppConfig
    from local_agent.persistence.session_store import Session

#: 引号包裹的路径：反引号 / 双引号 / 单引号三选一。
#: 顺序与写入侧（前端 `quoteMentionPath`）一致，避免"写进去能识别、发出去识别不了"。
_QUOTED = r"(?:`[^`\n]+`|\"[^\"\n]+\"|'[^'\n]+')"

#: `@` 后面跟引号路径，或跟到空白/中英文标点为止。
#: 中文里 `@src/app.py，这个` 很常见，所以标点也算终止符。
MENTION_PATTERN = re.compile(
    rf"@({_QUOTED}|[^\s@,;:!?\"'()<>\[\]{{}}，。；：！？、（）【】「」]+)"
)

#: 裸词引用：`@diff` = 工作区未提交改动，`@staged` = 暂存区。
#: 用法与 hermes 的 `(?P<simple>diff|staged)\b` 一致；值表示是否要 `--cached`。
GIT_SCOPES: dict[str, bool] = {"diff": False, "staged": True}

#: 单次 patch 的字符上限。git 输出可能很大（格式化整个仓库），先截再估 token。
MAX_PATCH_CHARS = 60_000

#: 一条消息里最多展开多少个引用。再多就不是"点名"而是"批量灌库"，那种事该交给工具。
MAX_REFERENCES = 16

#: 软限：超过只提醒，不阻断。
SOFT_RATIO = 0.25
#: 硬限：超过的文件退化成"只给路径"。
HARD_RATIO = 0.50

#: 拿不到模型窗口时的兜底预算（token）。约等于 20 KB 英文，中文会低一些——
#: 宁可保守：注入多了会把真正要回答的问题挤到窗口外。
FALLBACK_SOFT_TOKENS = 3_000
FALLBACK_HARD_TOKENS = 6_000


def mentions_in(text: str) -> list[str]:
    """按出现顺序去重取出候选**文件**路径（已去掉包裹的引号）。

    `@diff` / `@staged` 不在这里返回：它们是 git 作用域，不是文件（见 `git_scopes_in`）。
    否则会去磁盘上找一个叫 `diff` 的文件，然后报"文件不存在"。
    """
    seen: dict[str, None] = {}
    for match in MENTION_PATTERN.finditer(text or ""):
        candidate = match.group(1).strip()
        if candidate[:1] in {'`', '"', "'"} and len(candidate) >= 2 and candidate[-1] == candidate[0]:
            candidate = candidate[1:-1].strip()
        # 结尾的点几乎总是句号，但 `.py` 要保住：只在末尾多于一个点时裁一次。
        if candidate.endswith(".") and not candidate.endswith(".."):
            candidate = candidate[:-1]
        if not candidate or candidate in GIT_SCOPES or candidate in seen:
            continue
        seen[candidate] = None
    return list(seen)


def git_scopes_in(text: str) -> list[str]:
    """认出 `@diff` / `@staged`。要求前面是行首或空白，避免把 `foo@diff` 当成引用。"""
    return [
        scope
        for scope in GIT_SCOPES
        if re.search(rf"(?:^|\s)@{scope}(?=$|[\s，。；！？、])", text or "")
    ]


def _git_output(args: list[str], registry: ProcessRegistry) -> bytes | None:
    try:
        completed = registry.run(args, timeout_seconds=8)
    except (OSError, ProcessCancelledError):
        return None
    return completed.stdout if completed.exit_code == 0 and not completed.timed_out else None


def _allowed_path(root: Path, relative: str, block_known_secret_files: bool) -> Path | None:
    try:
        base = root.resolve()
        target = (base / relative).resolve()
    except OSError:
        return None
    if target != base and base not in target.parents:
        return None
    if block_known_secret_files and is_sensitive_path(target):
        return None
    return target


def git_patch(
    workspace_root: Path, *, staged: bool, block_known_secret_files: bool = True,
    cancellation: CancellationToken | None = None,
) -> tuple[str, bool] | None:
    """只读地取一段 patch：`(文本, 是否截断)`；取不到返回 `None`。

    **刻意不走 `SecurityPolicy`**：那条路要求本轮已经在跑（策略对象在运行时才建立），
    而 `@diff` 恰恰在**提交之前**就要用。这里只读、argv 固定、cwd 锁在工作区、
    有超时与输出上限——与"执行模型给的任意命令"不是一回事。
    """
    args = ["git", "-C", str(workspace_root), "diff", "--no-color", "--no-ext-diff",
            "--no-textconv", "--no-renames", "--relative"]
    registry = ProcessRegistry(cancellation if cancellation is not None else CancellationToken())
    if staged:
        args.append("--cached")
    # cwd 不限制 diff 的范围；先按工作区列出路径，再过滤敏感文件和越界链接。
    names = _git_output([*args, "--name-only", "-z", "--", "."], registry)
    if names is None:
        return None
    paths = [name.decode("utf-8", errors="surrogateescape") for name in names.split(b"\0") if name]
    allowed = [f":(literal){path}" for path in paths
               if _allowed_path(workspace_root, path, block_known_secret_files) is not None]
    if not allowed:
        return "", False
    output = _git_output([*args, "--", *allowed], registry)
    if output is None:
        return None
    text = output.decode("utf-8", errors="replace")
    if len(text) > MAX_PATCH_CHARS:
        return text[:MAX_PATCH_CHARS], True
    return text, False


def _budget(context_window: int | None) -> tuple[int, int]:
    """`(软限, 硬限)`，单位 token。窗口未知时用兜底值。"""
    if context_window is None or context_window <= 0:
        return FALLBACK_SOFT_TOKENS, FALLBACK_HARD_TOKENS
    return (
        max(1, int(context_window * SOFT_RATIO)),
        max(1, int(context_window * HARD_RATIO)),
    )


def _read_within(
    root: Path, relative: str, *, block_known_secret_files: bool = True
) -> tuple[str | None, str]:
    """读工作区内的一个文件：`(文本, 给模型看的原因)`。文本为 `None` 时表示没读到。

    越界判定用 resolve 后的前缀比较，而不是字符串里查 `..`——符号链接与 Windows
    短名都能让字符串检查失效。
    """
    try:
        base = root.resolve()
        target = (root / relative).resolve()
    except OSError:
        return None, "路径无法解析"
    if target != base and base not in target.parents:
        return None, "在工作区之外，不能读取"
    if block_known_secret_files and is_sensitive_path(target):
        return None, "敏感文件，不能读取内容"
    if not target.exists():
        return None, "文件不存在"
    if not target.is_file():
        return None, "不是一个文件"
    try:
        raw = target.read_bytes()
    except OSError:
        return None, "读取失败（权限或占用）"
    if b"\x00" in raw[:4096]:
        mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        return None, f"这是二进制文件（{mime}），没有按文本注入；需要的话用工具自行处理"
    return raw.decode("utf-8", errors="replace"), ""


def _path_only_block(relative: str, reason: str) -> str:
    """超预算时的降级块：只给路径与原因，让模型自己决定要不要读。"""
    return (
        f"### @{relative}\n"
        f"（{reason}。文件在磁盘上，需要时用读取工具按需查看。）"
    )


def build(*, workspace_root: Path, text: str, context_window: int | None = None,
          git_patch_provider: Callable[[bool], tuple[str, bool] | None] | None = None,
          block_known_secret_files: bool = True) -> str:
    """把正文里的 `@路径` / `@diff` / `@staged` 变成一段引用上下文；无内容时返回空串。

    `git_patch_provider` 由调用方注入（参数是 `staged`）：这样本模块仍然是纯函数，
    测试里塞一个假 provider 就能覆盖全部 git 分支，不需要真仓库。

    返回空串而不是 `None`：调用方要把它和侧聊前缀拼接，空串在 `join` 里自然消失。
    """
    references = mentions_in(text)
    scopes = git_scopes_in(text)
    if not references and not scopes:
        return ""

    soft, hard = _budget(context_window)
    blocks: list[str] = []
    warnings: list[str] = []
    used = 0

    for scope in scopes:
        if git_patch_provider is None:
            warnings.append(f"@{scope}：当前不可用（应用没有提供 git 读取通道）")
            continue
        patch = git_patch_provider(GIT_SCOPES[scope])
        if patch is None:
            warnings.append(f"@{scope}：读不到改动（不是 git 仓库，或 git 不可用）")
            continue
        patch_text, truncated = patch
        if patch_text.strip() == "":
            blocks.append(f"### @{scope}\n（工作区没有这类改动可供注入（没有改动，或已被读取规则过滤）。）")
            continue
        tokens = estimate_tokens(patch_text)
        if used + tokens > hard:
            blocks.append(
                f"### @{scope}\n（改动约 {tokens} tokens，超出本轮注入预算，未放入上下文；"
                "请用 git_diff 工具按需查看。）"
            )
            warnings.append(f"@{scope}：约 {tokens} tokens，超出预算未注入")
            continue
        used += tokens
        clipped = "\n（改动内容已截断：只注入了前一部分。）" if truncated else ""
        blocks.append(f"### @{scope}{clipped}\n```diff\n{patch_text.rstrip()}\n```")

    for reference in references[:MAX_REFERENCES]:
        body, reason = _read_within(
            workspace_root, reference, block_known_secret_files=block_known_secret_files
        )
        if body is None:
            warnings.append(f"@{reference}：{reason}")
            continue
        tokens = estimate_tokens(body)
        if used + tokens > hard:
            # 不截断：半个文件比没有文件更危险。
            blocks.append(_path_only_block(reference, "内容超出本轮注入预算，未放入上下文"))
            warnings.append(f"@{reference}：约 {tokens} tokens，超出预算未注入")
            continue
        used += tokens
        blocks.append(f"### @{reference}\n```\n{body.rstrip()}\n```")

    if len(references) > MAX_REFERENCES:
        warnings.append(
            f"本条消息共有 {len(references)} 个引用，只处理了前 {MAX_REFERENCES} 个。"
        )
    if used > soft:
        warnings.append(f"本轮引用已占用约 {used} tokens（软限 {soft}）。")

    if not blocks and not warnings:
        return ""

    header = (
        "用户在提问里点名了下面这些文件或改动。它们的内容已经附在这里，"
        "直接据此回答即可；除非确实需要未展示的部分，不必再调用读取工具。"
    )
    parts = [header]
    if blocks:
        parts.extend(blocks)
    if warnings:
        # 失败单独成段：混在正文里模型容易当成用户说的话。
        parts.append("--- 引用提示 ---\n" + "\n".join(f"- {item}" for item in warnings))
    return "\n\n".join(parts)


def context_prefix(service: Any, session: Session, text: str, config: AppConfig) -> str | None:
    """本轮要前置给模型的上下文：侧聊首轮 + 提问里 `@` 点名的文件（2026-10-30）。

    两段都走 `context_prefix`（一条独立 SystemMessage），**都不进用户气泡**——
    拼进 `user_input` 会被存成 `conversation_turns.question`，用户就会看到几千字
    文件内容（侧聊首轮踩过这个坑，见上面那条注释）。

    `@` 注入的预算按**这一轮的模型窗口**算（软限 25% / 硬限 50%）：窗口读不到时
    退回一组保守的 token 数，宁可少注入，也不把要回答的问题挤到窗口外。
    """
    blocks = [
        service._side_chat_context(session) or "",
        build(
            workspace_root=Path(session.workspace_root),
            text=text,
            block_known_secret_files=config.security.secrets.block_known_secret_files,
            context_window=service._session_context_window(session.session_id),
            # `@diff` / `@staged` 的取数通道：只读、cwd 锁在工作区（见 `git_patch` 的注释）。
            git_patch_provider=lambda staged: git_patch(
                Path(session.workspace_root), staged=staged,
                block_known_secret_files=config.security.secrets.block_known_secret_files,
                cancellation=service._token_for(session.session_id),
            ),
        ),
    ]
    merged = "\n\n".join(block for block in blocks if block)
    return merged or None
