"""候选文件的遍历：跳过口径 + **超限时怎么选**（架构 §6.7；v0.6 T63 第三片）。

这里把两件此前分散的事定在一处，`grep` / `glob` / 按需代码 RAG 三处共用同一份实现：

**一、跳过口径**（与 §6.7 逐条对应）：隐藏目录/文件、依赖目录（:data:`SKIPPED_DIRS`）、
`.gitignore`（:class:`IgnoreRules`）、符号链接、敏感文件（密钥）。此前 `tools/search_tools.py`
与 `rag/search.py` 各写一遍同样的判断，是两套实现碰巧一致——本模块把它收敛成一套。

**二、超限时怎么选：同级轮转**。同一目录下的条目按名排序后**轮流各出一条**、再走下一轮（逐层递归）：

- 触到文件数 / 命中数上限时，结果**分布在多个条目上**，而不是被名字最靠前的那个子树吃光；
- 这是参照实现 dsh 的 `sampleOverCapGlobResults=true` 的语义（"在顶层条目之间采样，避免只看得到一个
  子树"），也是本项目 B-34 那条"系统性盲区"的解法（2026-10-01 实测：只看得见工作区约 1/3）；
- **未触顶时结果集合与从前逐字相同**：轮转只改变"先看到哪些"，不改变"能扫到哪些"。上限决定看到多少，
  但不再决定"只看得到哪一个子树"。

**顺序是确定的**：同级按名排序、轮转固定 ⇒ 同一个工作区上重复遍历得到同一序列（不发散、不随机）。
"""

from __future__ import annotations

import os
from collections import deque
from collections.abc import Iterator
from pathlib import Path

from local_agent.rag.ignore import SKIPPED_DIRS, IgnoreRules
from local_agent.security.path_guard import is_sensitive_path


def iter_candidate_files(root: Path, *, workspace_root: Path) -> Iterator[Path]:
    """按"同级轮转 + 跳过口径"产出 ``root`` 下的候选文件（惰性、确定）。

    Args:
        root: 遍历起点（目录）。单文件的情形由调用方自己处理（那是"显式点名"，不受跳过口径影响）。
        workspace_root: 工作区根；`.gitignore` 规则从它往下算（子目录里的 `.gitignore` 也要生效）。

    Yields:
        通过全部跳过判断的普通文件路径。
    """
    yield from _walk(root, rules=IgnoreRules(workspace_root))


def _file_stream(path: Path) -> Iterator[Path]:
    """单个文件的"流"：符号链接与敏感文件在这里被挡掉（与目录判断同一口径）。"""
    if path.is_symlink() or is_sensitive_path(path):
        return
    yield path


def _walk(directory: Path, *, rules: IgnoreRules) -> Iterator[Path]:
    """递归遍历一个目录；**同级轮转**产出它的文件。

    为什么可以共用同一个 ``rules``：每个 ``_walk`` 在**自己的第一段**里完成"sync → 读目录 → 判定跳过"
    这一整块（中间没有 ``yield``），之后只做产出与递归，因此轮转交错执行也不会让规则栈串味。
    """
    rules.sync(directory)
    streams: list[Iterator[Path]] = []
    try:
        with os.scandir(directory) as entries:
            ordered = sorted(entries, key=lambda item: item.name)
    except OSError:
        # 读不了的目录**静默跳过**（与"读不了的文件跳过"同一取向）：检索不能因为一个坏目录整体失败。
        return
    # 条目**按名整体排序**（文件与子目录混在一起排），因此轮转的顺序由名字决定，不按"文件先、
    # 子目录后"分组——这正是"同级轮转"该有的样子：谁都不占先。
    for entry in ordered:
        name = entry.name
        if name.startswith("."):
            continue
        child = directory / name
        if entry.is_dir(follow_symlinks=False):
            # 被 `.gitignore` 忽略的目录**整个剪掉**（同 git：父目录被排除后无法再包含其中的文件）。
            if name.casefold() in SKIPPED_DIRS or rules.ignores(child, is_dir=True):
                continue
            streams.append(_walk(child, rules=rules))
        else:
            if rules.ignores(child, is_dir=False):
                continue
            streams.append(_file_stream(child))
    # 同级轮转：每个条目轮流出一条，取完即出队、没取完排到队尾。顺序确定 ⇒ 结果可复现。
    queue: deque[Iterator[Path]] = deque(streams)
    while queue:
        stream = queue.popleft()
        try:
            item = next(stream)
        except StopIteration:
            continue
        yield item
        queue.append(stream)
