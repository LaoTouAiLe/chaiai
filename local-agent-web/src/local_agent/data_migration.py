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


def _tree_stats(path: Path) -> tuple[int, int]:
    """``(总字节, 文件数)``，一次遍历算完。

    只在复制阶段算一次，校验阶段拿它比对目标——两次全树 rglob 在 6 GB 数据上要几十秒，
    而这段时间用户只是在看进度条（2026-11-06 真机实测：校验占了总时长的一半）。
    """
    if path.is_file():
        return path.stat().st_size, 1
    size = 0
    files = 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                size += item.stat().st_size
                files += 1
        except OSError:
            continue
    return size, files


def _size_of(path: Path) -> int:
    return _tree_stats(path)[0]


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
        # 相对路径按数据根解析（与 `container.resolve_data_paths` 同一条规则）。
        # **不依赖调用方先 resolve**：线上链路传的是解析过的配置，但脚本、新的调用点未必——
        # 漏掉的表现是"迁移成功、数据库一个没搬"，这是最难发现的一种失败（2026-11-06 真机验证抓到）。
        if not path.is_absolute():
            path = root / path
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


def _copy_sqlite(
    src: Path,
    dst: Path,
    advance: Callable[[int], None] | None = None,
    *,
    total_bytes: int | None = None,
    pages: int = 2000,
) -> None:
    """用 SQLite 自己的备份接口复制：连同 ``-wal`` 里未落盘的事务一起，不需要先关服务。

    ``advance`` 收"已复制字节"：6 GB 的会话库单靠"整项复制完再报进度"会让界面停在 0%
    好几十秒（2026-11-06 端到端实测），用户会以为卡死。这里按页回调折算成字节。
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    with (
        closing(sqlite3.connect(src.resolve().as_uri() + "?mode=ro", uri=True)) as source,
        closing(sqlite3.connect(dst)) as target,
    ):
        # 用调用方给的真实字节数折算，不再自己查 page_count：WAL 模式下的只读连接未必可靠，
        # 而一旦算成 0，界面上就会一直显示 0%（2026-11-06 端到端实测撞到）。
        expected = total_bytes if total_bytes else (src.stat().st_size or 1)

        def progress(_status: int, remaining: int, total: int) -> None:
            if advance is not None and total:
                advance(int(expected * (total - remaining) / total))

        source.backup(target, pages=pages, progress=progress)


def _verify_sqlite(src: Path, dst: Path) -> None:
    """比对结构规模，并对**副本**做完整自检。

    ``integrity_check`` 要读完整个库：6 GB 的会话库一次约 55 秒（2026-11-06 真机实测），
    跑两遍纯属浪费。只查副本就够了——源库要是本来就坏，副本这一查同样会失败。
    """

    def probe(path: Path, *, integrity: bool) -> tuple[int, int]:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            if integrity:
                verdict = conn.execute("PRAGMA integrity_check").fetchone()
                if not verdict or verdict[0] != "ok":
                    raise MigrationError(f"复制后的数据库自检失败：{path}")
            objects = conn.execute("SELECT count(*) FROM sqlite_master").fetchone()[0]
            pages = conn.execute("PRAGMA page_count").fetchone()[0]
        return objects, pages

    if probe(src, integrity=False) != probe(dst, integrity=True):
        raise MigrationError(f"复制后的库与原库不一致：{dst}")


def _verify_dir(dst: Path, expected: tuple[int, int]) -> None:
    """目标目录的 ``(字节, 文件数)`` 必须与复制源时记下的完全一致。"""
    actual = _tree_stats(dst)
    if actual != expected:
        raise MigrationError(
            f"复制后的目录与原始不一致（目标 {actual[1]} 个文件 / {actual[0]} 字节，"
            f"原始 {expected[1]} 个文件 / {expected[0]} 字节）：{dst}"
        )


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
    #: 复制源时记下 (字节, 文件数)，校验阶段直接比对，不再重算源侧。
    expected: dict[str, tuple[int, int]] = {}

    def report(stage: str, done: int) -> None:
        if progress is not None:
            progress(stage, done)

    try:
        report("copying", 0)
        for item in plan.items:
            src = plan.source_root / item.relative
            dst = plan.target_root / item.relative
            stats = _tree_stats(src)
            if item.kind == "sqlite":
                _copy_sqlite(
                    src, dst,
                    # 用默认参数绑定当前进度：lambda 直接闭包循环变量会被 ruff B023 判为隐患
                    advance=lambda done, base=copied: report("copying", base + done),
                    total_bytes=stats[0],
                )
            else:
                shutil.copytree(src, dst)
            expected[item.relative] = stats
            created.append(dst)
            copied += stats[0]
            report("copying", copied)

        report("verifying", copied)
        for item in plan.items:
            src = plan.source_root / item.relative
            dst = plan.target_root / item.relative
            if item.kind == "sqlite":
                _verify_sqlite(src, dst)
            else:
                _verify_dir(dst, expected[item.relative])
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
        stuck: list[str] = []
        for item in plan.items:
            try:
                move_to_trash(plan.source_root / item.relative)
            except FileOperationError:
                stuck.append(f"{item.name}（{item.relative}）")
        if stuck:
            # 一条汇总而不是 N 条零散错误：回收站对大文件会直接拒绝（实测 6 GB 的会话库报
            # OLE error 0x80270027），这时唯一可执行的指引是"确认新位置能用后手工删"。
            warnings.append(
                f"以下旧数据没能移入回收站，仍在 {plan.source_root}：{'、'.join(stuck)}。"
                "系统回收站对超过一定体积的文件会拒绝；确认新位置正常后，手工删掉这几项即可。"
            )
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
