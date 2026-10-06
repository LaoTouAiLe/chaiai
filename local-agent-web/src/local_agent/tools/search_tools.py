"""检索类工具（``grep`` / ``glob``）：让模型先**定位**、再定点读（架构 §3、§6.1）。

为什么需要它们
--------------
在这两个工具存在之前，``tool`` 模式绑定的 6 个工具里**没有任何检索能力**，而 Shell 兜底也不
可用——``security.shell_guard`` 的安全白名单要求**参数完全匹配**（``list(argv[1:]) != entry.args``
即落到"需人工审批"），``rg`` / ``Select-String`` / ``git grep`` 这类搜索命令一律会触发审批，模型
实际上搜不了。于是它只能 ``list_dir`` 枚举 + ``read_file`` 通读：实测一次"梳理架构"的提问做了
30 次 ``list_dir`` + 26 次 ``read_file``，单个 ``read_file`` 动辄撞 2 万字符上限，模型既定位不准
也读不全，还会把"证据被截断"误判成"任务未完成"而空转收尾。

两条工具的分工与主流编码 Agent 一致：

- ``glob``：按**路径模式**找文件（``**/*.py``、``src/**/test_*.py``）；
- ``grep``：按**正则**找内容，返回 ``路径:行号:内容``，并支持 ``files_with_matches`` / ``count``
  两种汇总模式——先看"哪些文件命中"，再定点读，而不是把整个仓库读进上下文。

安全
----
两个工具都是**只读**（已登记进 ``security.policy.READ_ONLY_TOOLS``），因此不需要人工审批；
但边界与 ``read_file`` 完全相同：

- 检索根目录必须位于工作区内（``resolve_within_workspace``），越界直接拒绝；
- 跳过符号链接（可能指向工作区外）与敏感文件（``is_sensitive_path``，密钥不进入检索结果）；
- 跳过隐藏目录/文件与依赖目录（复用 ``rag.search.SKIPPED_DIRS``，与按需 RAG 同一份清单）；
- 跳过二进制、非 UTF-8 与超大文件；
- 文件数、命中数、单行长度与墙钟时间都有上限，达到时**如实标注结果不完整**，不静默截断。
"""

from __future__ import annotations

import fnmatch
import os
import re
import time
from collections.abc import Iterator
from pathlib import Path

from langchain_core.tools import BaseTool, tool

from local_agent.rag.search import SKIPPED_DIRS
from local_agent.security.path_guard import (
    is_sensitive_path,
    resolve_within_workspace,
)
from local_agent.security.policy import SecurityPolicy
from local_agent.tools.result_types import (
    GlobResult,
    GrepFileHits,
    GrepMatch,
    GrepOutputMode,
    GrepResult,
)

#: 单次检索最多扫描的文件数；超出即停止并如实标注。
MAX_SCAN_FILES = 2000
#: ``glob`` 默认最多返回的文件数。
DEFAULT_GLOB_FILES = 500
#: 单文件字节上限：更大的文件跳过（与按需 RAG 扫描同一口径）。
MAX_SCAN_FILE_BYTES = 1_000_000
#: 单次检索的墙钟上限（秒）。
SCAN_TIMEOUT_SECONDS = 20.0
#: ``grep`` 默认返回的最大命中条数。
DEFAULT_MAX_MATCHES = 100
#: ``grep`` 命中条数的**硬上限**：模型传更大的值也不会超过它（防止一次把上下文灌满）。
HARD_MAX_MATCHES = 500
#: 内容命中单行展示的长度上限：超长行（压缩产物、单行 JSON）不该把上下文撑爆。
MAX_MATCH_LINE_CHARS = 300
#: 参与正则匹配的一行**最多看前多少字符**：既限制展示，也压低病态正则的回溯面。
MAX_SEARCHED_LINE_CHARS = 4_000
#: 模式串长度上限（``grep`` 与 ``glob`` 共用）。
MAX_PATTERN_CHARS = 200
#: 判定二进制文件的 NUL 探测窗口（与 ``read_file`` / 按需 RAG 同一口径）。
_BINARY_PROBE_BYTES = 4_096


def _resolve_search_root(policy: SecurityPolicy, path: str) -> Path:
    """把检索根解析为工作区内的路径；可以是**目录**，也可以是**单个文件**。

    2026-09-30 修：此前要求必须是目录，于是模型最自然的用法——"在这个文件里找"（``path='README.md'``）
    ——被拒，界面上弹出"工具被安全策略拒绝: grep"，文案还把它说成"改为在工作区内操作"（其实路径就在
    工作区内，只是不是目录）。实测一轮里 4 次调用被这样拒掉，模型只好自己改写成
    ``glob='README.md'`` 绕过去。``rg`` 本身就是这个语义（``rg pattern file.py``），没有理由拆开。

    越界仍然拒绝（``resolve_within_workspace`` 抛 ``WorkspaceBoundaryError``）；路径不存在抛
    ``ValueError`` 而不是 ``SecurityError``——前者映射成"工具执行失败／检查工具参数"，后者会显示
    成"被安全策略拦截"，而"不存在"根本不是策略问题。
    """
    raw = (path or "").strip() or "."
    root = policy.workspace_root
    resolved = resolve_within_workspace(raw, base_dir=root, workspace_root=root)
    if not resolved.is_dir() and not resolved.is_file():
        raise ValueError(f"检索路径不存在（既不是目录也不是文件）：{raw}")
    return resolved


def _relative(path: Path, workspace_root: Path) -> str:
    """展示用路径：相对工作区的 posix 形式；绝不对模型暴露绝对路径。"""
    try:
        return path.resolve().relative_to(workspace_root.resolve()).as_posix()
    except (OSError, ValueError):  # pragma: no cover - 调用方保证在工作区内
        return path.name


def _read_text(path: Path) -> str | None:
    """读取可检索的文本；二进制、非 UTF-8、超大或读失败时返回 ``None``（静默跳过）。"""
    try:
        if path.stat().st_size > MAX_SCAN_FILE_BYTES:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in data[:_BINARY_PROBE_BYTES]:
        return None
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None


def match_glob(relative: str, pattern: str) -> bool:
    """路径模式匹配：既匹配**相对路径**也匹配**文件名**。

    单独用 ``fnmatch`` 匹配整条相对路径时 ``src/**/*.py`` 匹配不到 ``src/a.py``（``fnmatch`` 不是
    路径感知的，``**`` 退化成两个 ``*``，要求 ``src/`` 之后至少还有一层目录）。``**/`` 语义是
    "零个或多个目录"，所以额外试一次把它去掉的变体。
    """
    text = (pattern or "").strip().replace("\\", "/")
    if not text:
        return True
    candidates = {text, text.replace("**/", ""), text.lstrip("./")}
    name = Path(relative).name
    return any(
        fnmatch.fnmatch(relative, candidate) or fnmatch.fnmatch(name, candidate)
        for candidate in candidates
    )


def _iter_files(root: Path, policy: SecurityPolicy, *, glob: str | None = None) -> Iterator[Path]:
    """有界、可复现地遍历检索根下的候选文件。

    与按需 RAG 用**同一套**跳过规则：隐藏目录/文件、依赖目录（:data:`SKIPPED_DIRS`）、符号链接
    （可能指向工作区外）与敏感文件（密钥）一律不进结果；目录名与文件名都排序，保证同一工作区上
    的结果稳定可复现。

    ``root`` 是**单个文件**时只产出它自己（``rg pattern file.py`` 的语义），跳过规则照旧适用——
    把 ``.env`` 当检索根同样不会返回内容。
    """
    workspace_root = policy.workspace_root
    if root.is_file():
        relative = _relative(root, workspace_root)
        if (
            not root.is_symlink()
            and not is_sensitive_path(root)
            and (glob is None or match_glob(relative, glob))
        ):
            yield root
        return
    for current, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(
            name
            for name in dirnames
            if not name.startswith(".") and name.casefold() not in SKIPPED_DIRS
        )
        for filename in sorted(filenames):
            if filename.startswith("."):
                continue
            candidate = Path(current) / filename
            if candidate.is_symlink() or is_sensitive_path(candidate):
                continue
            relative = _relative(candidate, workspace_root)
            if glob is not None and not match_glob(relative, glob):
                continue
            yield candidate


def _snippet(line: str, regex: re.Pattern[str]) -> str:
    """命中行的展示文本：有界，且**围绕命中位置**取窗口（长行不该只给开头）。"""
    stripped = line.rstrip("\n\r")
    if len(stripped) <= MAX_MATCH_LINE_CHARS:
        return stripped
    head = stripped[:MAX_SEARCHED_LINE_CHARS]
    found = regex.search(head)
    if found is None:
        return f"{head[:MAX_MATCH_LINE_CHARS]}…"
    window = MAX_MATCH_LINE_CHARS // 3
    start = max(0, found.start() - window)
    end = min(len(stripped), start + MAX_MATCH_LINE_CHARS)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(stripped) else ""
    return f"{prefix}{stripped[start:end]}{suffix}"


def _compile(pattern: str, *, ignore_case: bool, kind: str) -> re.Pattern[str]:
    text = (pattern or "").strip()
    if not text:
        raise ValueError(f"{kind} 的 pattern 不能为空")
    if len(text) > MAX_PATTERN_CHARS:
        raise ValueError(f"pattern 过长（上限 {MAX_PATTERN_CHARS} 字符）：{len(text)}")
    try:
        return re.compile(text, re.IGNORECASE if ignore_case else 0)
    except re.error as exc:
        raise ValueError(f"正则不合法：{exc}") from exc


def grep_content(
    pattern: str,
    *,
    policy: SecurityPolicy,
    path: str = ".",
    glob: str | None = None,
    output_mode: GrepOutputMode = "content",
    ignore_case: bool = False,
    max_matches: int = DEFAULT_MAX_MATCHES,
) -> GrepResult:
    """在工作区里按正则检索文件内容。

    Args:
        pattern: Python 正则（也接受普通字面量）。
        policy: 安全策略；检索根必须在工作区内。
        path: 检索根目录（相对工作区）。省略表示整个工作区。
        glob: 只在这些路径上检索（如 ``*.py``、``src/**``）。
        output_mode: ``content`` 给命中行；``files_with_matches`` 只给文件名；
            ``count`` 只给每个文件的命中数。**先看汇总再定点读**是省上下文的关键。
        ignore_case: 忽略大小写。
        max_matches: 最多返回多少条命中；超过硬上限 :data:`HARD_MAX_MATCHES` 时按硬上限。

    Returns:
        :class:`GrepResult`；``hits`` 三种模式都填，``matches`` 只有 ``content`` 才填。

    Raises:
        ValueError: 正则不合法或过长、``output_mode`` 不合法，或检索路径不存在。
        WorkspaceBoundaryError: 检索根不在工作区内。
    """
    if output_mode not in ("content", "files_with_matches", "count"):
        raise ValueError(f"output_mode 不合法：{output_mode}")
    regex = _compile(pattern, ignore_case=ignore_case, kind="grep")
    root = _resolve_search_root(policy, path)
    limit = max(1, min(int(max_matches), HARD_MAX_MATCHES))

    notes: list[str] = []
    matches: list[GrepMatch] = []
    per_file: dict[str, int] = {}
    scanned = 0
    truncated = False
    deadline = time.monotonic() + SCAN_TIMEOUT_SECONDS

    limit_hit = False
    for file_path in _iter_files(root, policy, glob=glob):
        if scanned >= MAX_SCAN_FILES:
            truncated = True
            notes.append(f"已达文件数上限 {MAX_SCAN_FILES}，未继续扫描剩余目录")
            break
        if time.monotonic() > deadline:
            truncated = True
            notes.append(f"已达时间上限 {SCAN_TIMEOUT_SECONDS:g} 秒，未继续扫描剩余文件")
            break
        if sum(per_file.values()) >= limit:
            limit_hit = True
            break
        scanned += 1
        text = _read_text(file_path)
        if text is None:
            continue
        relative = _relative(file_path, policy.workspace_root)
        for number, line in enumerate(text.splitlines(), start=1):
            if not regex.search(line[:MAX_SEARCHED_LINE_CHARS]):
                continue
            if sum(per_file.values()) >= limit:
                limit_hit = True
                break
            per_file[relative] = per_file.get(relative, 0) + 1
            if output_mode == "content":
                matches.append(
                    GrepMatch(path=relative, line_number=number, text=_snippet(line, regex))
                )
        if limit_hit:
            break

    # 说明必须**在循环外统一补**：上限也可能在**同一个文件的中间**触达（那里的 break 曾在早期实现
    # 里直接跳出而不写说明，于是返回 `truncated: true` 却 `note: null`——模型看到一个"不完整但没有
    # 原因"的结果，最容易把它误判成"没有更多了"。这是新增用例当场抓到的缺陷）。
    if limit_hit:
        truncated = True
        notes.append(f"已达命中上限 {limit} 条，未继续扫描剩余文件")

    if not per_file and not truncated:
        notes.append("没有匹配")

    return GrepResult(
        pattern=str(pattern).strip(),
        path=(path or ".").strip() or ".",
        glob=glob,
        output_mode=output_mode,
        matches=matches,
        hits=[
            GrepFileHits(path=name, count=count)
            for name, count in sorted(per_file.items(), key=lambda item: (-item[1], item[0]))
        ],
        total_matches=sum(per_file.values()),
        files_scanned=scanned,
        truncated=truncated,
        note="；".join(notes) if notes else None,
    )


def glob_files(
    pattern: str,
    *,
    policy: SecurityPolicy,
    path: str = ".",
    max_files: int = DEFAULT_GLOB_FILES,
) -> GlobResult:
    """按路径模式列出工作区里的文件。

    Args:
        pattern: 路径模式（``**/*.py``、``src/**/test_*.py``、``*.md``）。``**/`` 表示"零个或多个
            目录"，因此 ``src/**/*.py`` 也能匹配 ``src/a.py``。
        policy: 安全策略；检索根必须在工作区内。
        path: 检索根目录（相对工作区）。
        max_files: 最多返回多少个文件。

    Returns:
        :class:`GlobResult`。

    Raises:
        ValueError: 模式为空或过长，或检索路径不存在。
        WorkspaceBoundaryError: 检索根不在工作区内。
    """
    text = (pattern or "").strip()
    if not text:
        raise ValueError("glob 的 pattern 不能为空")
    if len(text) > MAX_PATTERN_CHARS:
        raise ValueError(f"pattern 过长（上限 {MAX_PATTERN_CHARS} 字符）：{len(text)}")
    root = _resolve_search_root(policy, path)
    limit = max(1, min(int(max_files), MAX_SCAN_FILES))

    notes: list[str] = []
    found: list[str] = []
    truncated = False
    deadline = time.monotonic() + SCAN_TIMEOUT_SECONDS

    for file_path in _iter_files(root, policy, glob=text):
        if len(found) >= limit:
            truncated = True
            notes.append(f"已达文件数上限 {limit}，未继续遍历剩余目录")
            break
        if time.monotonic() > deadline:
            truncated = True
            notes.append(f"已达时间上限 {SCAN_TIMEOUT_SECONDS:g} 秒，未继续遍历")
            break
        found.append(_relative(file_path, policy.workspace_root))

    if not found and not truncated:
        notes.append("没有匹配的文件")

    return GlobResult(
        pattern=text,
        path=(path or ".").strip() or ".",
        files=found,
        total_files=len(found),
        truncated=truncated,
        note="；".join(notes) if notes else None,
    )


def build_search_tools(policy: SecurityPolicy) -> list[BaseTool]:
    """构造检索工具（``grep`` / ``glob``），闭包持有安全策略。

    与 :func:`local_agent.tools.file_tools.build_file_tools` 同一形态：工具只负责解析参数与
    序列化结果，边界判定全部落在 :class:`SecurityPolicy` 与 ``path_guard`` 上。
    """

    @tool
    def grep(
        pattern: str,
        path: str = ".",
        glob: str | None = None,
        output_mode: str = "content",
        ignore_case: bool = False,
        max_matches: int = DEFAULT_MAX_MATCHES,
    ) -> str:
        """在工作区里按**正则**检索文件内容，返回命中位置；用来**先定位再定点读**。

        **优先用它，不要用 ``read_file`` 通读整篇**：先 ``output_mode=files_with_matches`` 看哪些
        文件命中，再对其中一两个文件用 ``read_file`` 读相关行区间。这样在多大的仓库里都能定位到
        该看的地方，也不会把无关内容读进上下文。

        Args:
            pattern: Python 正则；普通字面量也可以。大小写敏感默认开启，需要时传 ignore_case。
            path: 从哪个目录开始检索（相对工作区）。省略表示整个工作区。
            glob: 只在这些路径上检索，如 "*.py"、"src/**"、"**/*.ts"。
            output_mode: "content" 返回命中行（含行号）；"files_with_matches" 只返回文件名；
                "count" 返回每个文件的命中数。
            ignore_case: 是否忽略大小写。
            max_matches: 最多返回多少条命中（硬上限 500）。
        """
        return grep_content(
            pattern,
            policy=policy,
            path=path,
            glob=glob,
            output_mode=output_mode,  # type: ignore[arg-type]
            ignore_case=ignore_case,
            max_matches=max_matches,
        ).model_dump_json()

    @tool
    def glob(
        pattern: str,
        path: str = ".",
        max_files: int = DEFAULT_GLOB_FILES,
    ) -> str:
        """按**路径模式**列出工作区里的文件；用来先搞清楚有哪些文件，再决定读/搜哪个。

        Args:
            pattern: 路径模式，如 "**/*.py"、"src/**/test_*.py"、"*.md"。
                ``**/`` 表示"零个或多个目录"，所以 "src/**/*.py" 也能匹配 "src/a.py"。
            path: 从哪个目录开始找（相对工作区）。省略表示整个工作区。
            max_files: 最多返回多少个文件。
        """
        return glob_files(
            pattern,
            policy=policy,
            path=path,
            max_files=max_files,
        ).model_dump_json()

    return [grep, glob]
