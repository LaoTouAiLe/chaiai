"""超限检索结果的落盘与 locator（架构 §6.7 的"超限结果可恢复"，v0.6 T63）。

**为什么需要它**：``grep`` / ``glob`` 命中上限触发时只说明"结果不完整"，被截掉的候选取不回来——
模型只能靠换更窄的查询赌运气。dsh 的做法是超限时把完整结果写进 spill 后端、把 locator 交给模型；
本项目不引入外部 spill 服务，而是**落到工作区内的一个自忽略目录**，locator 就是一个普通的工作区
相对路径，因此**既有的 `read_file`（带行区间）与 `grep path=<locator>` 都能读它**——不新增工具、
不新增读取通道、不开安全例外。

三条边界（都写进了 §6.7 契约）：

- **不碰用户的文件**：只写 ``<workspace>/.local-agent/results/``，并在该目录里放一个内容为 ``*`` 的
  ``.gitignore`` 让它**自忽略**（agent 不该在用户仓库里留下待提交的东西）。
- **同一组参数复用同一个文件**：文件名由查询参数哈希决定，重复查询覆盖写，不会无限增长；每次写入后
  只保留最新 :data:`MAX_SPILL_FILES` 个。
- **失败绝不变成错误**：任何写入异常都返回 `None` 并记日志，调用方据此**如实**在 ``note`` 里写
  "完整结果未能保存"——既不抛错，也不谎称已保存（照 dsh 的同一口径）。

本模块不读 Checkpoint、不调模型、不执行工具：它只做文件 I/O，路径校验复用安全层的工作区边界。
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from local_agent.exceptions import SecurityError
from local_agent.security.path_guard import resolve_within_workspace

logger = logging.getLogger(__name__)
_SPILL_LOCK = threading.RLock()

#: 落盘目录（工作区相对路径）：隐藏目录，因此**不会进入后续检索结果**（`_iter_files` 跳过隐藏目录）。
SPILL_DIR = ".local-agent/results"
#: 目录内的自忽略文件：内容为 ``*``，让整个目录对 git 不可见。
_GITIGNORE = "*\n"
#: 保留的最新文件个数（每写一次清理一次）。**这是唯一的生命周期策略**，不做按龄删除。
#: 20 这个数是按"最近几次查询"定的：文件名由查询参数决定 ⇒ 重复查询复用同一个文件，
#: 所以它只随**不同**查询增长；再旧的检索结果，模型用更窄的 `path` / `glob` 重查即可。
MAX_SPILL_FILES = 20
#: 目录总字节上限（第二个封顶）。只按个数封顶不够：单个文件上限 8 MB，20 个就是 160 MB，
#: 而成熟缓存都按**容量**收口（ccache 的 `max_size`、npm cacache 的 LRU-GC 都是这个路子）。
#: 超出时按 mtime 从旧到新删，直到**两个上限都满足**。
MAX_SPILL_DIR_BYTES = 50_000_000
#: 单个落盘文件的字节上限；到限即停止写入并**如实标注**"落盘结果本身也不完整"。
MAX_SPILL_BYTES = 8_000_000
#: 落盘行数上限（与字节上限共同生效，防止极短行把文件撑爆）。
MAX_SPILL_LINES = 20_000


@dataclass(frozen=True)
class SpillRef:
    """一次落盘的结果引用。"""

    #: 模型可用的 locator：**工作区相对**的 posix 路径，可直接交给 ``read_file`` / ``grep path=``。
    locator: str
    #: 实际写入的行数与其字节数（如实记录，不估算）。
    lines: int
    bytes: int
    #: 落盘是否**完整**：收集本身触到字节/行/扫描上限时为 `False`（调用方必须一并写进 ``note``）。
    complete: bool


def spill_key(*parts: object) -> str:
    """由查询参数算出稳定的文件标识（同一组参数 → 同一个文件）。"""
    payload = "\x1f".join("" if part is None else str(part) for part in parts)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def save_result(
    *,
    workspace_root: Path,
    kind: str,
    key: str,
    lines: Sequence[str],
    complete: bool,
) -> SpillRef | None:
    """把完整结果写到 ``<workspace>/.local-agent/results/<kind>-<key>.txt``。

    Args:
        workspace_root: 工作区根；落盘位置固定在它下面（locator 也因此可被既有读取工具读到）。
        kind: ``grep`` / ``glob``——进文件名，便于用户一眼看出这是什么。
        key: :func:`spill_key` 算出的稳定标识。
        lines: **要写的全部行**（调用方已完成截断判定；这里只做字节/行数兜底）。
        complete: 调用方对"收集是否完整"的判定（触到扫描上限时为假）。

    Returns:
        引用信息；**任何写入失败都返回 `None`**（调用方据此如实说明，不得抛错）。
    """
    with _SPILL_LOCK:
        return _save_result(workspace_root, kind, key, lines, complete)


def _save_result(
    workspace_root: Path, kind: str, key: str, lines: Sequence[str], complete: bool
) -> SpillRef | None:
    try:
        if kind not in {"grep", "glob"} or re.fullmatch(r"[A-Za-z0-9_-]+", key) is None:
            raise SecurityError("非法检索结果文件名")
        root = workspace_root.resolve()
        directory = resolve_within_workspace(SPILL_DIR, base_dir=root, workspace_root=root)
        directory.mkdir(parents=True, exist_ok=True)
        ignore = _checked_file(directory, ".gitignore", root)
        if not ignore.exists():
            ignore.write_text(_GITIGNORE, encoding="utf-8")
        target = _checked_file(directory, f"{kind}-{key}.txt", root)
        written_lines = 0
        written_bytes = 0
        truncated = False
        with target.open("w", encoding="utf-8", newline="\n") as handle:
            for line in lines:
                size = len(line.encode("utf-8")) + 1
                if written_lines >= MAX_SPILL_LINES or written_bytes + size > MAX_SPILL_BYTES:
                    truncated = True
                    break
                handle.write(line)
                handle.write("\n")
                written_lines += 1
                written_bytes += size
        prune(directory, keep=target, workspace_root=root)
        if not target.is_file():
            return None
    except (OSError, SecurityError):
        # 失败绝不变成错误：记日志、返回 None，由调用方在 note 里如实说明。
        logger.warning("检索结果落盘失败（工作区 %s）", workspace_root, exc_info=True)
        return None
    return SpillRef(
        locator=f"{SPILL_DIR}/{kind}-{key}.txt",
        lines=written_lines,
        bytes=written_bytes,
        complete=complete and not truncated,
    )


def _checked_file(directory: Path, name: str, root: Path) -> Path:
    target = directory / name
    resolved = resolve_within_workspace(str(target), base_dir=root, workspace_root=root)
    if target.is_symlink() or resolved != target:
        raise SecurityError("检索结果目标不能通过链接覆盖其他文件")
    return resolved


def prune(directory: Path, *, keep: Path | None = None, workspace_root: Path | None = None) -> None:
    """按**个数**与**总字节**双封顶清理（mtime 新的先留）。清理失败只记日志。

    本次文件优先保留；其余按纳秒 mtime、文件名排序；突破任一上限后删除剩余结果文件。
    这样"最新的几条一定在"，且目录占用有确定上界（`min(20 个, 50 MB)`）。
    """
    with _SPILL_LOCK:
        _prune(directory, keep=keep, workspace_root=workspace_root)


def _prune(directory: Path, *, keep: Path | None, workspace_root: Path | None) -> None:
    try:
        root = (workspace_root or directory.parent.parent).resolve()
        directory = resolve_within_workspace(str(directory), base_dir=root, workspace_root=root)
        files = [
            item for item in directory.iterdir()
            if re.fullmatch(r"(?:grep|glob)-[A-Za-z0-9_-]+\.txt", item.name)
            and not item.is_symlink() and item.is_file()
        ]
        if not files:
            return
        files.sort(key=lambda item: (item == keep, item.stat().st_mtime_ns, item.name), reverse=True)
        total = 0
        for kept, item in enumerate(files):
            try:
                size = item.stat().st_size
            except OSError:  # pragma: no cover - 竞态：读不到就当作该删
                size = 0
            if kept >= MAX_SPILL_FILES or total + size > MAX_SPILL_DIR_BYTES:
                for stale in files[kept:]:
                    try:
                        _checked_file(directory, stale.name, root)
                        stale.unlink()
                    except (OSError, SecurityError):  # pragma: no cover - 清理失败不影响检索
                        logger.debug("清理旧落盘结果失败：%s", stale, exc_info=True)
                return
            total += size
    except (OSError, SecurityError):  # pragma: no cover - 目录被删/无权限时不影响调用方
        logger.debug("清理落盘目录失败：%s", directory, exc_info=True)


def spill_note(ref: SpillRef) -> str:
    """落盘成功时写进 ``note`` 的那句话（含 locator 与用法，模型据此取回完整结果）。"""
    tail = "" if ref.complete else "；注意：落盘结果本身也未完整（已达字节/行数上限）"
    return (
        f"完整结果 {ref.lines} 条已保存到 {ref.locator}"
        f"（可用 read_file 读取，或用 grep path={ref.locator} 在其中再查）{tail}"
    )


#: 落盘失败时写进 ``note`` 的那句话：**如实说明，且不改变"结果不完整"这个事实**。
SPILL_FAILED_NOTE = "完整结果未能保存（写盘失败），本次仍是不完整结果"
