"""数据根搬迁：把整个数据目录搬到新盘，而不是让用户手工复制（2026-11-06）。

**为什么系统要做这件事**：换数据根的入口本来就在设置页，但过去只写指针——用户重启后看到的是
空白界面（会话、模型 Key、记忆、附件全在新位置"消失"）。把"复制 → 校验 → 切指针 → 清旧"做成
一次动作，用户才真的能换盘。

**四条硬约束**（每一条都对应一种会丢东西的做法）：

1. SQLite **必须**用 ``backup()`` 复制，不能直接 copy 文件：``-wal`` 里可能有尚未 checkpoint 的
   数据，拷主文件等于丢掉最近的写入。
2. 目标与源**不能互相包含**：``D:/data`` 搬到 ``D:/data/new`` 时，后面的"清旧"会把自己刚搬过去的
   东西一起删掉。
3. "清旧"只删**清单内的项**，绝不 ``rmtree`` 整个旧根——旧根里可能有用户自己放的文件。
4. 任何一步失败都要**零残留**：清掉目标里本模块建的东西、指针不动、旧数据不删。

清旧走系统回收站（:func:`local_agent.trash.move_to_trash`）：删错了还能还原，代价只是多一个确认。
"""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from local_agent import app_paths
from local_agent.config import AppConfig
from local_agent.exceptions import FileOperationError
from local_agent.trash import move_to_trash

#: 目标盘至少要多留的余量比例：复制过程中还有临时文件与目录开销。
SPACE_MARGIN = 1.2


class MigrationError(RuntimeError):
    """搬迁无法开始或中途失败。消息会直接展示给用户，必须写成可执行的提示。"""


@dataclass(frozen=True)
class MigrationItem:
    """清单里的一项。``relative`` 是相对数据根的路径（用 ``/`` 分隔）。"""

    name: str
    relative: str
    kind: str  # "sqlite" | "dir"


@dataclass(frozen=True)
class MigrationPlan:
    """搬迁计划：来源、目标、要搬的项，以及不搬的那些（配置里指到数据根之外的库）。"""

    source_root: Path
    target_root: Path
    items: tuple[MigrationItem, ...]
    external: tuple[str, ...]
    total_bytes: int


#: 数据根下的固定目录/文件（相对于根）。顺序即展示顺序。
_FIXED_ITEMS: tuple[MigrationItem, ...] = (
    MigrationItem(name="附件", relative="attachments", kind="dir"),
    MigrationItem(name="文件改动基线", relative="file-changes", kind="dir"),
    MigrationItem(name="移动设备授权", relative="mobile", kind="dir"),
    MigrationItem(name="日志", relative="logs", kind="dir"),
    MigrationItem(name="轮次耗时", relative="metrics", kind="dir"),
    MigrationItem(name="窗口缓存", relative="webview", kind="dir"),
    MigrationItem(name="MCP 目录缓存", relative="mcp_catalog.sqlite", kind="sqlite"),
)


def _is_within(path: Path, base: Path) -> bool:
    try:
        path.resolve().relative_to(base.resolve())
    except (OSError, ValueError):
        return False
    return True


def _size_of(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def _item_of(name: str, path: Path, root: Path) -> MigrationItem | None:
    """把配置里的绝对路径转成清单项；不在数据根下、或不存在时返回 ``None``。"""
    if not path.exists() or not _is_within(path, root):
        return None
    relative = path.resolve().relative_to(root.resolve()).as_posix()
    return MigrationItem(name=name, relative=relative, kind="dir" if path.is_dir() else "sqlite")


def build_plan(config: AppConfig, target: Path) -> MigrationPlan:
    """按**已解析**的配置列出要搬的东西（:func:`local_agent.api.container.resolve_data_paths`
    之后调用，那时记忆与设置库的路径都已是绝对路径）。

    配置里指到数据根之外的自定义库（例如把长期记忆放到另一个盘）**不搬**：那是用户自己指定的
    位置，搬数据根不该顺手把它挪走。它们会出现在 ``external`` 里，由界面如实告知。
    """
    root = app_paths.data_root_of(config)
    items: list[MigrationItem] = []
    external: list[str] = []
    candidates = (
        ("会话与检查点库", config.memory.short_term.path),
        ("中期记忆", config.memory.medium_term.path),
        ("长期记忆", config.memory.long_term.path),
        ("常驻记忆", config.memory.curated.path),
        ("设置库（含模型连接）", config.model_settings.path),
    )
    seen: set[str] = set()
    for name, raw in candidates:
        path = Path(raw)
        item = _item_of(name, path, root)
        if item is None:
            if str(raw).strip() and Path(raw).is_absolute():
                external.append(f"{name}：{raw}")
            continue
        if item.relative in seen:
            continue
        seen.add(item.relative)
        items.append(item)
    for fixed in _FIXED_ITEMS:
        path = root / fixed.relative
        if not path.exists():
            continue
        if fixed.relative in seen:
            continue
        seen.add(fixed.relative)
        items.append(fixed)
    total = sum(_size_of(root / item.relative) for item in items)
    return MigrationPlan(
        source_root=root, target_root=target, items=tuple(items),
        external=tuple(external), total_bytes=total,
    )


def _nearest_existing(path: Path) -> Path:
    current = path
    while not current.exists() and current.parent != current:
        current = current.parent
    return current


def preflight(plan: MigrationPlan) -> None:
    """所有"不能开始"的理由集中在这里，失败抛 :class:`MigrationError`。

    顺序是按"用户最容易搞错"排的：先判同一处、再判嵌套、再判目标冲突，最后判空间。
    """
    source, target = plan.source_root, plan.target_root
    if not source.is_dir():
        raise MigrationError(f"当前数据目录不存在：{source}")
    if target.resolve() == source.resolve():
        raise MigrationError("新位置就是当前数据目录，不需要搬迁。")
    if _is_within(target, source) or _is_within(source, target):
        raise MigrationError(
            "新位置不能与当前数据目录互相包含：否则「迁移后清理旧数据」会把刚搬过去的东西一起删掉。"
        )
    if not plan.items:
        raise MigrationError("当前数据目录里没有可搬迁的内容。")
    conflicts = [item.relative for item in plan.items if (target / item.relative).exists()]
    if conflicts:
        raise MigrationError(
            "新位置已经有同名数据：" + "、".join(conflicts) + "；请换一个空目录，或先自行备份后移走。"
        )
    try:
        target.mkdir(parents=True, exist_ok=True)
        probe = target / ".chaiai-write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise MigrationError(f"新位置不可写（{type(exc).__name__}）：{target}") from exc
    free = shutil.disk_usage(_nearest_existing(target)).free
    required = int(plan.total_bytes * SPACE_MARGIN)
    if free < required:
        raise MigrationError(
            f"新位置所在磁盘空间不足：需要约 {required // (1024 * 1024)} MB，"
            f"剩余 {free // (1024 * 1024)} MB。"
        )


def _copy_sqlite(src: Path, dst: Path) -> None:
    """用 SQLite 自己的备份接口复制：连同 ``-wal`` 里未落盘的事务一起，不需要先关服务。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    with (
        closing(sqlite3.connect(src.resolve().as_uri() + "?mode=ro", uri=True)) as source,
        closing(sqlite3.connect(dst)) as target,
    ):
        source.backup(target)


def _verify_sqlite(src: Path, dst: Path) -> None:
    def probe(path: Path) -> tuple[int, int]:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            verdict = conn.execute("PRAGMA integrity_check").fetchone()
            if not verdict or verdict[0] != "ok":
                raise MigrationError(f"数据库自检失败：{path}")
            objects = conn.execute("SELECT count(*) FROM sqlite_master").fetchone()[0]
            pages = conn.execute("PRAGMA page_count").fetchone()[0]
        return objects, pages

    if probe(src) != probe(dst):
        raise MigrationError(f"复制后的库与原库不一致：{dst}")


def _verify_dir(src: Path, dst: Path) -> None:
    if _size_of(src) != _size_of(dst):
        raise MigrationError(f"复制后的目录与原始大小不一致：{dst}")
    src_files = sum(1 for item in src.rglob("*") if item.is_file())
    dst_files = sum(1 for item in dst.rglob("*") if item.is_file())
    if src_files != dst_files:
        raise MigrationError(f"复制后的文件数不一致（{dst_files} / {src_files}）：{dst}")


def run(
    plan: MigrationPlan,
    *,
    progress: Callable[[str, int], None] | None = None,
    delete_source: bool = True,
) -> list[str]:
    """执行搬迁，返回"已成功但需要告知用户"的警告（例如旧数据删不掉）。

    成功后指针已经指向新位置；**必须重启**才会用新根启动。失败时抛 :class:`MigrationError`，
    目标里本模块建的东西会被清掉，指针与旧数据都不动。

    Args:
        delete_source: 搬完是否清旧（走系统回收站）。关掉只用于测试与排障。
    """
    preflight(plan)
    created: list[Path] = []
    copied = 0

    def report(stage: str, done: int) -> None:
        if progress is not None:
            progress(stage, done)

    try:
        report("copying", 0)
        for item in plan.items:
            src = plan.source_root / item.relative
            dst = plan.target_root / item.relative
            before = copied
            if item.kind == "sqlite":
                _copy_sqlite(src, dst)
            else:
                shutil.copytree(src, dst)
            created.append(dst)
            copied = before + _size_of(src)
            report("copying", copied)

        report("verifying", copied)
        for item in plan.items:
            src = plan.source_root / item.relative
            dst = plan.target_root / item.relative
            if item.kind == "sqlite":
                _verify_sqlite(src, dst)
            else:
                _verify_dir(src, dst)
    except (OSError, sqlite3.Error, MigrationError) as exc:
        for path in reversed(created):
            _remove_quietly(path)
        if isinstance(exc, MigrationError):
            raise
        raise MigrationError(f"搬运失败：{type(exc).__name__}：{exc}") from exc

    report("switching", copied)
    app_paths.set_data_root_pointer(plan.target_root)

    warnings: list[str] = []
    if delete_source:
        report("cleaning", copied)
        for item in plan.items:
            try:
                move_to_trash(plan.source_root / item.relative)
            except FileOperationError as exc:
                warnings.append(f"{item.name}：{exc}")
    report("done", copied)
    return warnings


def _remove_quietly(path: Path) -> None:
    """清掉本模块刚写到目标里的东西（这是自己建的，用永久删，不进回收站）。"""
    try:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
    except OSError:
        pass
