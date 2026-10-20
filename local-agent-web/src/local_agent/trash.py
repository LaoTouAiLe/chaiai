"""回收站（2026-10-20）：删除不再直接抹掉，而是移到数据目录下可找回的位置。

为什么不用系统回收站：Windows 需要 `SHFileOperation`、macOS/Linux 各有各的实现，跨平台成本远高于
收益；而"能找回"这件事只需要一个约定好的目录 + 一份清单。

规则：

1. 目标整块移动（文件或目录），**不逐个删**——目录移走后结构原样保留，拖回去就能用；
2. 每次移动落到独立子目录（`年/时间-短 uuid`），不覆盖任何已有文件；
3. 追加一行 ``manifest.jsonl``（原路径、回收路径、时间），方便手动找回；
4. 移动失败**抛错**，绝不回退成"直接删除"——那等于把安全删除静默关掉。
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from local_agent.app_paths import data_dir
from local_agent.exceptions import FileOperationError

#: 清单文件名（放在回收站根下）。
MANIFEST_NAME = "manifest.jsonl"


def default_trash_root() -> Path:
    """回收站根目录：冻结形态在数据目录下，源码/CLI 形态落在当前目录的 `data/trash`。"""
    base = data_dir()
    return (Path(base) if base is not None else Path.cwd() / "data") / "trash"


def move_to_trash(target: Path, *, root: Path | None = None) -> Path:
    """把 ``target`` 整体移进回收站，返回它在回收站里的新路径。

    Raises:
        FileOperationError: 移动失败（权限、跨设备且目标巨大、目标已被别人移走…）。
    """
    source = Path(target)
    if not source.exists():
        raise FileOperationError(f"目标不存在：{source.as_posix()}")
    base = Path(root) if root is not None else default_trash_root()
    moment = datetime.now().astimezone()
    slot = base / f"{moment:%Y%m%d}" / f"{moment:%H%M%S}-{uuid4().hex[:8]}"
    try:
        slot.mkdir(parents=True, exist_ok=True)
        destination = slot / source.name
        shutil.move(str(source), str(destination))
    except OSError as exc:
        # 不静默回退成删除：用户开着"安全删除"，就等于要求"宁可失败也别丢"。
        raise FileOperationError(
            f"移入回收站失败（{type(exc).__name__}）：{exc}；文件没有被删除。"
        ) from exc
    _append_manifest(base, source, destination, moment)
    return destination


def _append_manifest(base: Path, source: Path, destination: Path, moment: datetime) -> None:
    """记一行"原路径 → 回收路径"；写不进去不影响移动本身（文件已经安全落位）。"""
    record = {
        "原路径": str(source),
        "回收路径": str(destination),
        "时间": moment.isoformat(timespec="seconds"),
    }
    try:
        base.mkdir(parents=True, exist_ok=True)
        with (base / MANIFEST_NAME).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        return
