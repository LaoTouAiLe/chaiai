"""数据位置与数据根搬迁（2026-11-06 从 ``service.py`` 搬出来）。

**为什么单独一个模块**：``service.py`` 已经 4880 行、贴着规模基线的上限，而那条基线是
"只防恶化"的棘轮（``scripts/check_size.py`` 的 ``--update-baseline`` 只允许收紧）。这一组
功能恰好是"围绕数据根的若干一次性动作"——查位置、改指针、搬数据、报进度——与对话主流程没有
交集，整体搬走既让 service 回到基线内，也让职责清楚：**它只管数据根，不碰任何一轮对话**。

搬迁本身的文件操作在 :mod:`local_agent.data_migration`（复制、校验、清旧）；这里负责状态、
互斥、后台线程与面向页面的视图。
"""

from __future__ import annotations

import os
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from local_agent import app_paths, data_migration
from local_agent.application import errors as app_errors
from local_agent.application.models import StorageMigrationView, StorageView

#: 剩余空间低于这个值就提示换盘（2026-10-26）。5 GB 是**估算**：一个会话库实测 3.9 GB /
#: 106 会话，留出一倍余量。它不是精确阈值，只是"该提醒了"的量级判断。
_LOW_DISK_BYTES: Final[int] = 5 * 1024**3

#: 数据根来源对应的说明（2026-10-26）。`environment` 那一档必须点明"页面改不生效"——
#: 否则用户改完、重启、发现没变，会以为是这个功能坏了。
_STORAGE_NOTES: dict[str, str] = {
    "environment": "当前由环境变量 CHAIAI_HOME 决定：在页面上改不会生效，请改环境变量后重启。",
    "settings": "位置由「设置 → 数据位置」保存的指针文件决定。",
    "config": "当前由 config.yaml 的 data_root 决定；在页面上改会覆盖它。",
    "default": "使用平台标准目录。",
}

#: 一次只允许一个搬迁：状态是进程内的，并发启动只会让两份线程抢同一个目标目录。
_LOCK = threading.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _directory_usage(root: Path) -> tuple[int | None, int | None]:
    """数据根占用与所在卷剩余字节数（2026-10-26）。

    两者都**不抛错**：统计失败（权限、路径刚被删）不能让整个设置页打不开——这只是
    给用户一个"要不要换盘"的判断依据，拿不到就不显示。

    遍历用 ``os.walk`` 而不是 ``Path.rglob``：后者要为每个条目建一个 ``Path`` 对象，
    附件目录动辄几万个文件时差别很明显；这里只需要大小。

    注意统计**包含**数据根下的一切（含 ``_archive-*`` 之类的手工归档）——它们确实占着盘，
    把它排除掉会让"占用"这个数字说谎。
    """
    used = 0
    try:
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                try:
                    used += os.stat(os.path.join(dirpath, name)).st_size
                except OSError:
                    continue
    except OSError:
        return None, None
    try:
        free: int | None = shutil.disk_usage(root).free
    except OSError:
        free = None
    return used, free


def storage_location(config: Any) -> StorageView:
    """当前数据根、它的来源，以及"改了这一项要重启"这条事实。"""
    source = app_paths.data_root_source(config)
    root = app_paths.data_root_of(config)
    used, free = _directory_usage(root)
    # 只在"默认位置 + 盘快满"时提示：已经换过盘的人不该再被劝一次（他多半是故意选的）。
    low_disk = source == "default" and free is not None and free < _LOW_DISK_BYTES
    return StorageView(
        data_root=str(root),
        default_root=str(app_paths.platform_default_root()),
        source=source,  # type: ignore[arg-type]
        note=_STORAGE_NOTES.get(source),
        used_bytes=used,
        disk_free_bytes=free,
        low_disk=low_disk,
    )


def save_storage_location(config: Any, data_root: str) -> StorageView:
    """把数据根改到 ``data_root``；**重启后生效**，且不搬数据。

    路径就在这一步校验（绝对、能创建、能写），不等到启动：启动才失败意味着服务起不来，
    用户只能去手改文件才救得回来。

    Raises:
        ApplicationError: 路径不合法/不可写，或当前由环境变量决定（页面改不生效）。
    """
    if app_paths.data_root_source(config) == "environment":
        raise app_errors.model_settings_invalid(
            detail="当前数据位置由环境变量 CHAIAI_HOME 决定：请改那个变量后重启，页面设置不会生效",
            occurred_at=_now(),
        )
    try:
        app_paths.set_data_root_pointer(data_root)
    except ValueError as exc:
        raise app_errors.model_settings_invalid(detail=str(exc), occurred_at=_now()) from exc
    return storage_location(config).model_copy(update={
        "restart_required": True,
        "note": "已保存，重启后端后生效。新位置是空的：旧数据不会自动搬过去。",
    })


def reset_storage_location(config: Any) -> StorageView:
    """恢复平台标准目录；同样要重启才生效。"""
    app_paths.reset_data_root_pointer()
    return storage_location(config).model_copy(update={
        "restart_required": True,
        "note": "已恢复默认位置，重启后端后生效。",
    })


def maintenance_reason(state: dict[str, Any] | None) -> str | None:
    """正在搬迁数据时返回原因字符串；写入口据此拒绝新回合。

    为什么不等"在跑的那一轮"结束再搬：等的时长不可控（一次工具循环可能几分钟到十几分钟），
    而用户在设置页点完按钮就在等结果。所以策略是**拒绝新回合**、已有回合不追。
    """
    if state is None or state.get("finished"):
        return None
    return "正在搬迁数据目录"


def migration_view(state: dict[str, Any] | None) -> StorageMigrationView:
    """当前/最近一次搬迁的状态；从没搬过时 ``stage="idle"``。"""
    if state is None:
        return StorageMigrationView(stage="idle", target_root="")
    return StorageMigrationView(
        stage=state["stage"],
        copied_bytes=state["copied_bytes"],
        total_bytes=state["total_bytes"],
        target_root=state["target_root"],
        items=tuple(state["items"]),
        warnings=tuple(state["warnings"]),
        error=state["error"],
    )


def start(config: Any, state: dict[str, Any] | None, target: str) -> dict[str, Any]:
    """开始一次搬迁，返回**新的**状态字典（调用方保存它并在之后交给 :func:`migration_view`）。

    搬完即清旧（走系统回收站），并把指针指向新位置——**必须重启**才会用新根启动。

    Raises:
        ApplicationError: 目标不是绝对路径，或已经有一次搬迁在跑。
    """
    path = Path(target)
    if not path.is_absolute():
        raise app_errors.model_settings_invalid(
            detail="新位置必须是绝对路径：相对路径会随启动目录漂移",
            occurred_at=_now(),
        )
    with _LOCK:
        if maintenance_reason(state) is not None:
            raise app_errors.model_settings_invalid(
                detail="已经有一次数据搬迁在进行中，请等它结束或重启后再试",
                occurred_at=_now(),
            )
        fresh: dict[str, Any] = {
            "stage": "preparing",
            "copied_bytes": 0,
            "total_bytes": 0,
            "target_root": str(path),
            "items": [],
            "warnings": [],
            "error": "",
            "finished": False,
        }
    threading.Thread(
        target=_run, args=(config, path, fresh), daemon=True, name="data-migration"
    ).start()
    return fresh


def _run(config: Any, target: Path, state: dict[str, Any]) -> None:
    def on_progress(stage: str, done: int) -> None:
        state["stage"] = stage
        state["copied_bytes"] = done

    try:
        # 建清单要统计总字节（6 GB 数据约 55 秒）：**不能**放在 HTTP 请求里做，否则用户点完
        # 按钮半分钟收不到任何反馈（2026-11-06 端到端实测）。预检也一并挪到这里，失败通过
        # `stage=failed` + `error` 回报。
        plan = data_migration.build_plan(config, target)
        state["total_bytes"] = plan.total_bytes
        state["items"] = [item.name for item in plan.items]
        data_migration.preflight(plan)
        warnings = list(data_migration.run(plan, progress=on_progress, delete_source=True))
        # workspace.root 用相对路径时，它的解析基准是**配置文件所在目录**：换数据根后默认工作区
        # 会跟着挪到新位置，而旧工作区里的文件仍在原处（2026-11-06 用户实际撞到过：以为东西被写到了 C 盘）。
        root_setting = str(config.workspace.root or "").strip()
        if root_setting and not Path(root_setting).is_absolute():
            warnings.append(
                f"workspace.root 仍是相对路径（{root_setting}）：默认工作区会跟着新数据目录一起变，"
                "旧工作区里的文件留在原处。要让它固定在某个盘，把它改成绝对路径。"
            )
        state["warnings"] = warnings
        state["stage"] = "done"
    except (data_migration.MigrationError, OSError) as exc:
        state["stage"] = "failed"
        state["error"] = str(exc)
    finally:
        state["finished"] = True
