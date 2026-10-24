"""按需代码检索的词项提取与候选发现（架构 §4.3、§5.3；开发文档 T21）。

**不建持久索引**：每次调用直接读当前工作区的磁盘现状，因此不存在"陈旧索引"，也没有
增量维护任务。检索是**字面匹配**，词项由 :func:`extract_terms` 从用户请求里提取（路径、
文件名、标识符；只有纯中文请求才退回中文片段做字面词项），不对整句话做模糊匹配。

候选来源有两条，职责划分如下：

1. **文件/路径命中**：始终由有界目录遍历给出（:func:`_walk_files`）。这条路径与"有没有
   ``rg``"无关，因此"按文件名/路径找得到"在两种环境下行为一致。
2. **正文命中**：优先用 ``rg``（外部可选加速器，``uv sync`` 不会安装它）——以 **argv 数组
   直执行**、``shell=False``、不拼字符串、不经 Shell，用 ``--json`` 拿结构化结果；``rg``
   不可用或执行失败时，退化为对**同一批**已遍历文件逐行做字面匹配。

两条路径共用同一套限额，并且都在**文件数、单文件字节数与墙钟时间**上有上限。
``rg`` 正常返回"没有命中"不会触发退化（那是结果，不是故障）。
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from local_agent import app_paths
from local_agent.config import RagConfig
from local_agent.rag.walk import iter_candidate_files
from local_agent.security.path_guard import is_sensitive_path, is_within
from local_agent.security.process_guard import (
    ProcessCancelledError,
    ProcessRegistry,
    run_process,
)
from local_agent.security.shell_guard import build_environment, resolve_executable

#: 命中来源：正文、文件名、路径。
MatchType = Literal["path", "filename", "text"]

#: 候选发现使用的路径。
SearchSource = Literal["rg", "python_scan"]

#: 匹配类型优先级：文件名/路径命中比正文命中更能说明"该看哪个文件"。
_MATCH_TYPE_ORDER: dict[str, int] = {"filename": 0, "path": 1, "text": 2}

#: 单个词项的长度上限：更长的多半是整句话，不是可检索的标识符。
MAX_TERM_CHARS = 64

#: 词项切分：保留标识符、文件名、路径与中文片段，其余（标点、空白、emoji）当分隔符。
_TOKEN_SPLIT = re.compile(r"[^0-9A-Za-z_\u4e00-\u9fff./\\-]+")

#: 提取词项时丢掉的常见英文虚词（命中率极高、几乎不携带信息）。
_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "that",
        "this",
        "what",
        "how",
        "why",
        "where",
        "when",
        "which",
        "from",
        "into",
        "about",
        "please",
        "code",
        "file",
        "files",
        "line",
        "lines",
        "use",
        "using",
        "you",
        "your",
        "our",
        "are",
        "does",
        "did",
        "can",
        "could",
        "should",
        "would",
        "not",
        "but",
        "all",
        "any",
        "get",
        "set",
        "run",
    }
)

#: 遍历与"跳过什么"都在 :mod:`local_agent.rag.walk`（实现）与 :mod:`local_agent.rag.ignore`
#: （静态依赖目录清单 :data:`~local_agent.rag.ignore.SKIPPED_DIRS` + `.gitignore` 规则）——
#: v0.6 T63 第三片搬到那里，`grep` / `glob` / 本模块共用同一份，不再各留一套。

#: 判断"二进制文件"的 NUL 探测窗口（与 ``read_file`` 同一口径）。
_BINARY_PROBE_BYTES = 4096

#: 传给 ``rg`` 的固定参数：结构化输出、字面匹配、不读用户配置、与终端无关。
_RG_BASE_ARGV: tuple[str, ...] = (
    "--json",
    "--no-config",
    "--color=never",
    "--fixed-strings",
)


class RgFailed(Exception):
    """``rg`` 这一次没能完成检索（超时、启动失败或返回错误码）。"""


@dataclass(frozen=True)
class SearchMatch:
    """一条候选命中（尚未读取片段）。"""

    path: Path
    #: 相对工作区的 posix 路径：展示、去重与 Prompt 都用它，绝不对外暴露绝对路径。
    relative_path: str
    match_type: MatchType
    term: str
    source: SearchSource
    #: 正文命中为 1 基行号；文件名/路径命中没有具体行，为 ``None``。
    line_number: int | None = None

    @property
    def order_key(self) -> tuple[int, str, int]:
        return (
            _MATCH_TYPE_ORDER[self.match_type],
            self.relative_path,
            self.line_number or 0,
        )


@dataclass(frozen=True)
class SearchOutcome:
    """一次候选发现的结果与降级说明。"""

    matches: tuple[SearchMatch, ...]
    #: 实际完成检索的路径；``none`` 表示连遍历都没做（没有可用词项）。
    source: str
    notes: tuple[str, ...] = ()
    #: 两条路径都没能完成检索（需要写可见错误，且不得伪称已检索）。
    failed: bool = False
    #: 命中被上限截断（结果不完整，已如实标注）。
    truncated: bool = False


def extract_terms(query: str, *, limit: int) -> list[str]:
    """把用户请求转成候选检索词项（字面匹配用）。

    切分规则：按非标识符字符切分，保留长度 ``2..MAX_TERM_CHARS`` 的片段，丢掉常见英文虚词，
    去重保序，最后截到 ``limit`` 个。

    **有 ASCII 词项时只用 ASCII 词项**（文件名、路径、标识符），中文片段一律不再加入：
    中文问句里混着的 ``routers.py``、``decide_mode`` 才是能定位到代码的东西，而"里的""怎么判断"
    这类片段按字面匹配只会命中中文散文。实测：一句「routers.py 里的 decide_mode 怎么判断模式？」
    里的"里的"在 README 里出现 7 次，把 8 个结果位吃掉 7 个，真正要找的函数被挤出结果——
    检索等于白做。**纯中文请求**（没有任何 ASCII 词项）仍然用中文片段做字面词项：那是这种请求
    唯一可用的信号，宁可召回弱，也不要什么都不搜。

    Args:
        query: 用户请求原文（通常是 ``normalized_request``）。
        limit: 最多返回多少个词项（``rag.max_terms``）。

    Returns:
        去重保序的词项列表；没有任何可用词项时返回空列表。
    """
    ascii_terms: list[str] = []
    chinese_terms: list[str] = []
    seen: set[str] = set()
    for raw in _TOKEN_SPLIT.split(query or ""):
        term = raw.strip("./\\-_")
        if len(term) < 2 or len(term) > MAX_TERM_CHARS:
            continue
        key = term.casefold()
        if key in _STOPWORDS or key in seen:
            continue
        seen.add(key)
        (ascii_terms if term.isascii() else chinese_terms).append(term)
    if ascii_terms:
        return ascii_terms[: max(limit, 0)]
    return chinese_terms[: max(limit, 0)]


def discover_matches(
    terms: Sequence[str],
    *,
    workspace_root: Path,
    config: RagConfig,
    resolve: Callable[..., str | None] = resolve_executable,
    registry: ProcessRegistry | None = None,
) -> SearchOutcome:
    """按词项在当前工作区内发现候选（正文优先 ``rg``，缺失退化逐行扫描）。

    Args:
        terms: 已经提取好的词项；为空时直接返回"没有可检索词项"，**不碰文件系统**。
        workspace_root: 工作区根目录；遍历、解析与展示都以它为界。
        config: ``rag.*`` 限额（超时、文件数、字节数、命中数）。
        resolve: 可执行文件解析函数（默认 :func:`shell_guard.resolve_executable`）；
            测试用它注入"有 ``rg`` / 没有 ``rg``"两种情形。
        registry: **本轮**的进程登记表（§20.1）：``rg`` 从它出去，于是超时与取消都按进程树
            终止。省略时用一次性登记表（无令牌）——行为仍是"统一出口"，只是不参与取消。

    Returns:
        候选命中、实际来源与降级说明。
    """
    if not terms:
        return SearchOutcome(
            matches=(),
            source="none",
            notes=("本轮请求里没有可检索的词项（路径、文件名或标识符），未搜索工作区",),
        )

    deadline = time.monotonic() + max(config.timeout_seconds, 1)
    try:
        files, walk_notes, walk_truncated = _walk_files(
            workspace_root, config=config, deadline=deadline
        )
    except OSError as exc:
        # 连遍历都起不来（工作区不存在、无权限）：如实上报"没检索成"，不装作"没有相关代码"。
        return SearchOutcome(
            matches=(),
            source="python_scan",
            notes=(f"工作区遍历失败：{exc}",),
            failed=True,
        )
    matches: list[SearchMatch] = []
    notes: list[str] = list(walk_notes)
    truncated = walk_truncated

    # 1) 文件/路径命中：与"有没有 rg"无关，始终由遍历结果给出。
    per_term: dict[str, int] = {term: 0 for term in terms}
    for path in files:
        relative = _relative(path, workspace_root)
        match = _path_match(path, relative, terms)
        if match is None or per_term[match.term] >= config.max_matches_per_term:
            continue
        per_term[match.term] += 1
        matches.append(match)

    # 2) 正文命中：优先 rg（包内自带的优先于 PATH）；不可用或失败才退化到逐行扫描。
    executable: str | None = None
    bundled = app_paths.bundled_binary("rg")
    if bundled is not None:
        executable = str(bundled)
    else:
        try:
            executable = resolve("rg", cwd=workspace_root)
        except OSError:  # pragma: no cover - 解析器自身异常按"没有 rg"处理
            executable = None

    source = "python_scan"
    if executable:
        try:
            content, rg_notes, rg_truncated = _rg_matches(
                executable,
                terms,
                cwd=workspace_root,
                config=config,
                deadline=deadline,
                registry=registry,
            )
        except RgFailed as exc:
            # 加速器坏了就说清楚，然后退化；不能因为 rg 失败就宣称"工作区里没有相关代码"。
            notes.append(f"rg 检索未完成（{exc}），已改用有界 Python 扫描")
        else:
            matches.extend(content)
            notes.extend(rg_notes)
            truncated = truncated or rg_truncated
            source = "rg"
            return _outcome(matches, source=source, notes=notes, truncated=truncated)
    else:
        notes.append("未找到可执行文件 rg（可选加速器），正文命中改用有界 Python 扫描")

    try:
        content, scan_notes, scan_truncated = _scan_matches(
            terms,
            files=files,
            workspace_root=workspace_root,
            config=config,
            deadline=deadline,
            per_term=per_term,
        )
    except OSError as exc:
        return SearchOutcome(
            matches=(),
            source=source,
            notes=(*notes, f"工作区遍历失败：{exc}"),
            failed=True,
        )
    matches.extend(content)
    notes.extend(scan_notes)
    return _outcome(
        matches, source=source, notes=notes, truncated=truncated or scan_truncated
    )


def _outcome(
    matches: Sequence[SearchMatch], *, source: str, notes: Sequence[str], truncated: bool
) -> SearchOutcome:
    """去重、按优先级排序后打包结果。"""
    unique: dict[tuple[str, int], SearchMatch] = {}
    for match in matches:
        unique.setdefault((match.relative_path, match.line_number or 0), match)
    ordered = tuple(sorted(unique.values(), key=lambda item: item.order_key))
    return SearchOutcome(
        matches=ordered, source=source, notes=tuple(notes), truncated=truncated
    )


def _rg_matches(
    executable: str,
    terms: Sequence[str],
    *,
    cwd: Path,
    config: RagConfig,
    deadline: float,
    registry: ProcessRegistry | None = None,
) -> tuple[list[SearchMatch], list[str], bool]:
    """调用 ``rg --json`` 做正文候选发现。

    走**统一执行出口**（§20.1）：超时与取消都按**进程树**终止（``rg`` 自己派生的子进程也一并收掉），
    而不是只杀直接子进程。**退化口径一字不变**：超时仍是 ``RgFailed("超过 N 秒")``、起不来仍是
    ``RgFailed("无法启动（…）")``、退出码非 0/1 仍是 ``RgFailed("退出码 N…")``，调用方据此退化到
    有界 Python 扫描——"rg 失败"永远不会被说成"工作区里没有相关代码"。

    Raises:
        RgFailed: 超时、无法启动或返回错误码（调用方据此退化到逐行扫描）。
    """
    # 每个词项都显式用 `-e` 传入，最后单独给搜索路径：不依赖 rg 对"哪个参数是路径"的启发式判断。
    argv = [
        executable,
        *_RG_BASE_ARGV,
        "--max-count",
        str(config.max_matches_per_term),
    ]
    for term in terms:
        argv += ["-e", term]
    argv.append(".")
    remaining = max(deadline - time.monotonic(), 0.1)
    try:
        outcome = run_process(
            argv,
            cwd=str(cwd),
            env=build_environment(()),
            timeout_seconds=remaining,
            registry=registry,
        )
    except ProcessCancelledError as exc:
        # 门已通过、取消同时发生（§20.1）：**不启动**，按既有的"检索未完成"如实说明并退化。
        raise RgFailed(str(exc)) from exc
    except OSError as exc:
        raise RgFailed(f"无法启动（{type(exc).__name__}）") from exc

    if outcome.timed_out:
        raise RgFailed(f"超过 {config.timeout_seconds} 秒")

    # rg 退出码：0 = 有命中，1 = 没有命中，2 = 出错。
    code = outcome.exit_code
    if code is None:
        # 只可能出现在"终止后仍读不到输出"这种极端情形：如实说没取到，不拿 0 顶替。
        raise RgFailed("未取到退出码（进程被终止后仍未读完输出）")
    if code not in (0, 1):
        detail = outcome.stderr.decode("utf-8", errors="replace").strip()[:200]
        raise RgFailed(f"退出码 {code}{f'：{detail}' if detail else ''}")

    matches: list[SearchMatch] = []
    truncated = False
    root = cwd.resolve()
    limit = config.max_files * config.max_matches_per_term
    for line in outcome.stdout.decode("utf-8", errors="replace").splitlines():
        if len(matches) >= limit:
            truncated = True
            break
        try:
            payload = json.loads(line)
        except ValueError:
            # 输出不完整（例如被截断）时丢掉这一行并如实标注，不猜内容。
            truncated = True
            continue
        if payload.get("type") != "match":
            continue
        data = payload.get("data") or {}
        raw_path = str((data.get("path") or {}).get("text") or "")
        line_number = data.get("line_number")
        if not raw_path or not isinstance(line_number, int):
            continue
        path = Path(raw_path)
        absolute = (root / path).resolve()
        # 不信任外部工具的筛选结果：工作区外、解析不了或敏感文件一律丢弃（架构 §5.3）。
        if not is_within(absolute, root) or is_sensitive_path(absolute):
            continue
        match = _text_match(
            absolute,
            _relative(absolute, root),
            line_number,
            str((data.get("lines") or {}).get("text") or ""),
            terms,
        )
        if match is not None:
            matches.append(match)

    notes: list[str] = []
    if truncated:
        notes.append("rg 输出超过解析上限，已截断（结果不完整）")
    return matches, notes, truncated


def _text_match(
    path: Path,
    relative_path: str,
    line_number: int,
    line_text: str,
    terms: Sequence[str],
) -> SearchMatch | None:
    """把一条正文命中归属到具体词项；行里找不到任何词项时返回 ``None``。

    ``path`` 始终是**已验证位于工作区内**的绝对路径（来源可以是 ``rg`` 的输出或目录遍历），
    读取片段时再经安全策略复核一次。
    """
    for term in terms:
        if term in line_text:
            return SearchMatch(
                path=path,
                relative_path=relative_path,
                match_type="text",
                term=term,
                source="rg",
                line_number=line_number,
            )
    return None


def _scan_matches(
    terms: Sequence[str],
    *,
    files: Sequence[Path],
    workspace_root: Path,
    config: RagConfig,
    deadline: float,
    per_term: dict[str, int],
) -> tuple[list[SearchMatch], list[str], bool]:
    """对**已遍历出的**文件逐行做字面匹配（``rg`` 不可用时的正文路径）。

    只在单文件字节数、每词项命中数与墙钟时间上有上限；文件数与遍历边界由
    :func:`_walk_files` 负责。
    """
    matches: list[SearchMatch] = []
    notes: list[str] = []
    truncated = False
    skipped_big = 0
    skipped_binary = 0
    skipped_undecodable = 0

    for path in files:
        if time.monotonic() > deadline:
            truncated = True
            notes.append(f"已达时间上限 {config.timeout_seconds} 秒，未继续扫描剩余文件")
            break
        if all(count >= config.max_matches_per_term for count in per_term.values()):
            truncated = True
            notes.append(f"每个词项都已达命中上限 {config.max_matches_per_term}，未继续扫描")
            break
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > config.max_file_bytes:
            skipped_big += 1
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if b"\x00" in data[:_BINARY_PROBE_BYTES]:
            skipped_binary += 1
            continue
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            skipped_undecodable += 1
            continue

        relative = _relative(path, workspace_root)
        for index, line in enumerate(text.splitlines(), start=1):
            for term in terms:
                if per_term[term] >= config.max_matches_per_term or term not in line:
                    continue
                per_term[term] += 1
                matches.append(
                    SearchMatch(
                        path=path,
                        relative_path=relative,
                        match_type="text",
                        term=term,
                        source="python_scan",
                        line_number=index,
                    )
                )

    if skipped_big:
        notes.append(f"已跳过 {skipped_big} 个超过 {config.max_file_bytes} 字节的文件")
    if skipped_binary:
        notes.append(f"已跳过 {skipped_binary} 个二进制文件")
    if skipped_undecodable:
        notes.append(f"已跳过 {skipped_undecodable} 个不是 UTF-8 文本的文件")
    return matches, notes, truncated


def _path_match(path: Path, relative: str, terms: Sequence[str]) -> SearchMatch | None:
    """文件名或相对路径命中词项时产出一条候选（排序优先级高于正文命中）。"""
    name = path.name
    for term in terms:
        if term in name:
            return SearchMatch(
                path=path,
                relative_path=relative,
                match_type="filename",
                term=term,
                source="python_scan",
            )
        if term in relative:
            return SearchMatch(
                path=path,
                relative_path=relative,
                match_type="path",
                term=term,
                source="python_scan",
            )
    return None


def _walk_files(
    workspace_root: Path, *, config: RagConfig, deadline: float
) -> tuple[list[Path], list[str], bool]:
    """有界、可复现地遍历工作区里的候选文件。

    遍历与跳过口径都在 :func:`local_agent.rag.walk.iter_candidate_files`：隐藏目录/文件、依赖目录、
    `.gitignore`、符号链接（可能指向工作区外）与敏感文件（密钥不进结果），并**同级轮转**产出
    （超限时结果分布在多个目录，而不是被名字最靠前的子树吃光——与 `grep` / `glob` 同一份实现）。
    达到文件数上限或时间上限即停止，并如实标注"结果不完整"。
    """
    root = workspace_root.resolve()
    files: list[Path] = []
    notes: list[str] = []
    truncated = False
    # 遍历与跳过口径走 :func:`local_agent.rag.walk.iter_candidate_files`——与 `grep` / `glob`
    # **共用同一份实现**（§6.7：静态清单 + `.gitignore` + 同级轮转），这里只叠加限额与如实标注。
    for path in iter_candidate_files(root, workspace_root=root):
        if len(files) >= config.max_files:
            truncated = True
            notes.append(f"已达文件数上限 {config.max_files}，未继续遍历剩余目录")
            return files, notes, truncated
        if time.monotonic() > deadline:
            truncated = True
            notes.append(f"已达时间上限 {config.timeout_seconds} 秒，未继续遍历剩余目录")
            return files, notes, truncated
        files.append(path)
    return files, notes, truncated


def _relative(path: Path, workspace_root: Path) -> str:
    """相对工作区的 posix 路径；解析失败时退回文件名（绝不对外暴露绝对路径）。"""
    try:
        return path.resolve().relative_to(workspace_root.resolve()).as_posix()
    except (OSError, ValueError):  # pragma: no cover - 理论不可达（路径来自遍历）
        return path.name
