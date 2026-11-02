"""文件改动还原（2026-10-29）：两个粒度、冲突跳过、重建被删文件、配额与保留深度。

覆盖的是**场景清单**里必须成立的那些结论，不是实现细节：

- 整轮还原回到"本轮开始前"，不是"会话开始前"（这是 v15 存在的原因）；
- 单文件还原回到"会话开始前"，与右栏 diff 同口径；
- AI 写完后被人改过的文件默认跳过，``force`` 才覆盖；
- 用户删掉的文件按改前内容**重建**；AI 新建的文件被删掉则保持不存在；
- 没有改前内容（超限/二进制/配额/清理）要进 ``skipped`` 并给出原因，不能静默当成功；
- 还原前把当时的内容留一份（``undo_ref``），误还原还能找回。
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from pathlib import Path

import pytest

from local_agent.persistence.file_changes import (
    MAX_BASELINE_BYTES,
    FileChangeLedger,
)
from local_agent.persistence.file_restore import RestoreError, restore
from local_agent.persistence.migrations import (
    _CHAT_V11_FILE_CHANGES_SQL,
    _CHAT_V15_FILE_SNAPSHOTS_SQL,
)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_ledger(tmp_path: Path, **options: int) -> tuple[FileChangeLedger, Path]:
    connection = sqlite3.connect(":memory:")
    connection.executescript(_CHAT_V11_FILE_CHANGES_SQL)
    connection.executescript(_CHAT_V15_FILE_SNAPSHOTS_SQL)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    ledger = FileChangeLedger(
        connection,
        lock=threading.Lock(),
        session_id="s1",
        baseline_dir=tmp_path / "baselines",
        workspace_root=workspace,
        **options,
    )
    return ledger, workspace


def edit(
    ledger: FileChangeLedger, target: Path, content: bytes, *, kind: str = "modify"
) -> None:
    """模拟一次工具写盘：**先 capture 再写**（与真实调用顺序一致），写成功后记账。"""
    ledger.capture(target)
    if kind == "delete":
        target.unlink()
    else:
        target.write_bytes(content)
    ledger.record(
        tool="write_file", resolved=target, kind=kind,
        before_sha256=None, after_sha256=None if kind == "delete" else sha(content),
    )


def test_turn_restore_returns_to_the_start_of_that_turn(tmp_path: Path) -> None:
    """两轮各改一次：还原第 2 轮要停在"第 2 轮开始前"（= 第 1 轮的结果）。"""
    ledger, workspace = make_ledger(tmp_path)
    target = workspace / "a.py"
    target.write_bytes(b"v1\n")

    ledger.bind_run("r1")
    edit(ledger, target, b"v2\n")
    ledger.bind_run("r2")
    edit(ledger, target, b"v3\n")

    result = restore(ledger, workspace_root=workspace, scope="turn", run_id="r2")
    assert [(item.path, item.action, item.basis) for item in result.restored] == [
        ("a.py", "written", "turn")
    ]
    assert target.read_bytes() == b"v2\n"
    assert (result.skipped, result.failed) == ((), ())


def test_file_restore_returns_to_the_start_of_the_session(tmp_path: Path) -> None:
    """单文件粒度与右栏「净变化」同一口径：退到会话开始前，而不是上一轮之前。"""
    ledger, workspace = make_ledger(tmp_path)
    target = workspace / "a.py"
    target.write_bytes(b"v1\n")

    ledger.bind_run("r1")
    edit(ledger, target, b"v2\n")
    ledger.bind_run("r2")
    edit(ledger, target, b"v3\n")

    result = restore(ledger, workspace_root=workspace, scope="file", path="a.py")
    assert [item.basis for item in result.restored] == ["session"]
    assert target.read_bytes() == b"v1\n"


def test_conflict_is_skipped_unless_forced(tmp_path: Path) -> None:
    """AI 写完之后被用户改过：默认跳过（不覆盖用户的手工修改），force 才覆盖。"""
    ledger, workspace = make_ledger(tmp_path)
    target = workspace / "a.py"
    target.write_bytes(b"v1\n")
    ledger.bind_run("r1")
    edit(ledger, target, b"ai\n")

    target.write_bytes(b"user\n")  # 用户手改，账本不知道

    skipped = restore(ledger, workspace_root=workspace, scope="turn", run_id="r1")
    assert [item.path for item in skipped.skipped] == ["a.py"]
    assert skipped.restored == ()
    assert target.read_bytes() == b"user\n"

    forced = restore(ledger, workspace_root=workspace, scope="turn", run_id="r1", force=True)
    assert [item.action for item in forced.restored] == ["written"]
    assert target.read_bytes() == b"v1\n"


def test_deleted_file_is_rebuilt_from_the_before_content(tmp_path: Path) -> None:
    """用户删掉了原本存在的文件：还原用改前内容重建（2026-10-29 用户拍板）。"""
    ledger, workspace = make_ledger(tmp_path)
    target = workspace / "a.py"
    target.write_bytes(b"v1\n")
    ledger.bind_run("r1")
    edit(ledger, target, b"v2\n")

    target.unlink()

    result = restore(ledger, workspace_root=workspace, scope="turn", run_id="r1")
    assert [(item.path, item.action) for item in result.restored] == [("a.py", "rebuilt")]
    assert target.read_bytes() == b"v1\n"


def test_deleted_agent_created_file_stays_deleted(tmp_path: Path) -> None:
    """AI 新建的文件被删掉：它的"改前"是空串，还原时保持不存在，不能凭空造一个空文件。"""
    ledger, workspace = make_ledger(tmp_path)
    target = workspace / "new.py"
    ledger.bind_run("r1")
    edit(ledger, target, b"a\nb\n", kind="create")

    target.unlink()

    result = restore(ledger, workspace_root=workspace, scope="turn", run_id="r1")
    assert [(item.path, item.action) for item in result.restored] == [("new.py", "unchanged")]
    assert target.exists() is False


@pytest.mark.parametrize("scope", ["file", "turn"])
def test_restore_removes_a_file_created_after_the_baseline(tmp_path: Path, scope: str) -> None:
    ledger, workspace = make_ledger(tmp_path)
    target = workspace / "new.py"
    ledger.bind_run("r1")
    edit(ledger, target, b"new content", kind="create")
    result = restore(ledger, workspace_root=workspace, scope=scope, path="new.py", run_id="r1")
    assert not target.exists()
    assert result.restored[0].action == "removed"
    assert ledger.load_undo_snapshot(result.restored[0].undo_ref) == b"new content"
    again = restore(ledger, workspace_root=workspace, scope=scope, path="new.py", run_id="r1")
    assert again.restored[0].action == "unchanged"


@pytest.mark.parametrize("deleted", [True, False])
def test_restore_preserves_or_rebuilds_an_originally_empty_file(tmp_path: Path, deleted: bool) -> None:
    ledger, workspace = make_ledger(tmp_path)
    target = workspace / "empty.py"
    target.write_bytes(b"")
    ledger.bind_run("r1")
    edit(ledger, target, b"changed")
    if deleted:
        target.unlink()
    result = restore(ledger, workspace_root=workspace, scope="turn", run_id="r1")
    assert target.is_file()
    assert target.read_bytes() == b""
    assert result.restored[0].action == ("rebuilt" if deleted else "written")


def test_session_restore_keeps_original_when_later_recreated(tmp_path: Path) -> None:
    ledger, workspace = make_ledger(tmp_path)
    target = workspace / "original.py"
    target.write_bytes(b"original")
    ledger.bind_run("r1")
    edit(ledger, target, b"", kind="delete")
    ledger.bind_run("r2")
    edit(ledger, target, b"recreated", kind="create")
    result = restore(ledger, workspace_root=workspace, scope="file", path="original.py")
    assert result.restored[0].action == "written"
    assert target.read_bytes() == b"original"


def test_created_file_with_manual_edits_is_only_removed_with_force(tmp_path: Path) -> None:
    ledger, workspace = make_ledger(tmp_path)
    target = workspace / "new.py"
    ledger.bind_run("r1")
    edit(ledger, target, b"created", kind="create")
    target.write_bytes(b"manual")
    result = restore(ledger, workspace_root=workspace, scope="turn", run_id="r1")
    assert result.skipped and target.read_bytes() == b"manual"
    forced = restore(ledger, workspace_root=workspace, scope="turn", run_id="r1", force=True)
    assert not target.exists()
    assert ledger.load_undo_snapshot(forced.restored[0].undo_ref) == b"manual"


def test_file_without_before_content_is_reported_not_silently_restored(tmp_path: Path) -> None:
    """超过单文件上限时没有改前内容：进 skipped 并说明原因，绝不能报成功。"""
    ledger, workspace = make_ledger(tmp_path)
    target = workspace / "big.txt"
    target.write_bytes(b"a" * (MAX_BASELINE_BYTES + 1))
    ledger.bind_run("r1")
    edit(ledger, target, b"b" * (MAX_BASELINE_BYTES + 1))

    result = restore(ledger, workspace_root=workspace, scope="turn", run_id="r1")
    assert result.restored == ()
    assert len(result.skipped) == 1
    assert "KB" in result.skipped[0].reason


def test_restore_keeps_a_copy_of_the_current_content(tmp_path: Path) -> None:
    """覆盖前把当时的内容存一份：误还原之后还能把被覆盖的版本找回来。"""
    ledger, workspace = make_ledger(tmp_path)
    target = workspace / "a.py"
    target.write_bytes(b"v1\n")
    ledger.bind_run("r1")
    edit(ledger, target, b"v2\n")

    result = restore(ledger, workspace_root=workspace, scope="turn", run_id="r1")
    undo_ref = result.restored[0].undo_ref
    assert undo_ref is not None
    assert ledger.load_undo_snapshot(undo_ref) == b"v2\n"


def test_turn_restore_rejects_unknown_run(tmp_path: Path) -> None:
    ledger, workspace = make_ledger(tmp_path)
    ledger.bind_run("r1")
    edit(ledger, workspace / "a.py", b"x\n", kind="create")

    with pytest.raises(RestoreError):
        restore(ledger, workspace_root=workspace, scope="turn", run_id="nope")


def test_blobs_are_content_addressed_across_turns(tmp_path: Path) -> None:
    """同样的改前内容只存一份：两个文件、两轮，内容相同就是同一个 blob。"""
    ledger, workspace = make_ledger(tmp_path)
    same = b"same content\n"
    first = workspace / "a.py"
    second = workspace / "b.py"
    first.write_bytes(same)
    second.write_bytes(same)

    ledger.bind_run("r1")
    edit(ledger, first, b"changed\n")
    edit(ledger, second, b"changed\n")

    usage = ledger.storage_usage()
    assert usage["blobs"] == 1
    assert usage["size_bytes"] == len(same)


def test_prune_drops_before_content_beyond_keep_turns(tmp_path: Path) -> None:
    """超出保留轮数后：旧轮仍能查 diff，但不能再还原，且原因写明"已清理"。"""
    ledger, workspace = make_ledger(tmp_path, keep_turns=1)
    target = workspace / "a.py"
    target.write_bytes(b"v1\n")
    ledger.bind_run("r1")
    edit(ledger, target, b"v2\n")
    ledger.bind_run("r2")
    edit(ledger, target, b"v3\n")

    assert ledger.prune() >= 0  # 已由 capture 自动跑过一次，这里幂等
    ops = {op.run_id: op for op in ledger.ops()}
    assert ops["r1"].before_ref is None
    assert ops["r1"].before_reason == "已清理（超出保留轮数）"

    result = restore(ledger, workspace_root=workspace, scope="turn", run_id="r1")
    assert result.restored == ()
    assert len(result.skipped) == 1


def test_capture_does_not_prune_the_snapshot_it_just_stored(tmp_path: Path) -> None:
    """回归（2026-10-29 审查）：prune 的孤儿清理不能删掉**本轮刚存**的那一份。

    旧顺序是"先存 blob，再 prune"，而孤儿判定只认已 record 的 ``before_ref``——刚存的这份
    还没被引用，于是每轮第一个被改文件的改前内容都会被当场删掉，整轮还原对它永远失效。
    用 ``keep_turns=1`` 跑三轮即可复现（第三轮 capture 时才有超期轮次可清）。
    """
    ledger, workspace = make_ledger(tmp_path, keep_turns=1)
    target = workspace / "a.py"
    target.write_bytes(b"v1\n")
    for index, content in enumerate([b"v2\n", b"v3\n", b"v4\n"], start=1):
        ledger.bind_run(f"r{index}")
        edit(ledger, target, content)

    last = ledger.ops()[-1]
    assert last.run_id == "r3"
    assert last.before_ref is not None
    assert ledger.load_blob(last.before_ref) == b"v3\n"


def test_undo_snapshot_survives_pruning(tmp_path: Path) -> None:
    """还原前的那份内容不随保留轮数被清掉：它不进 blob 账目表，也不参与配额。"""
    ledger, workspace = make_ledger(tmp_path, keep_turns=1)
    target = workspace / "a.py"
    target.write_bytes(b"v1\n")
    ledger.bind_run("r1")
    edit(ledger, target, b"v2\n")

    result = restore(ledger, workspace_root=workspace, scope="turn", run_id="r1")
    undo_ref = result.restored[0].undo_ref
    assert undo_ref is not None

    ledger.bind_run("r2")
    edit(ledger, target, b"v3\n")
    ledger.bind_run("r3")
    edit(ledger, target, b"v4\n")

    # 确认 prune 真的跑过（r1 的改前内容已被清理），而救援副本还在。
    assert ledger.ops()[0].before_ref is None
    assert ledger.load_undo_snapshot(undo_ref) == b"v2\n"


def test_file_restore_rejects_a_file_without_history(tmp_path: Path) -> None:
    """没有改动记录时要说清楚，不能回一个空结果（页面会读成"0 个处理成功"）。"""
    ledger, workspace = make_ledger(tmp_path)
    ledger.bind_run("r1")
    edit(ledger, workspace / "a.py", b"x\n", kind="create")

    with pytest.raises(RestoreError):
        restore(ledger, workspace_root=workspace, scope="file", path="never.py")


def test_file_rebuilt_after_deletion_is_skipped_without_force(tmp_path: Path) -> None:
    """AI 删除后用户又重建了文件：默认跳过，不能把用户重建的内容覆盖掉。"""
    ledger, workspace = make_ledger(tmp_path)
    target = workspace / "b.py"
    target.write_bytes(b"orig\n")
    ledger.bind_run("r1")
    edit(ledger, target, b"", kind="delete")
    target.write_bytes(b"user-made\n")

    result = restore(ledger, workspace_root=workspace, scope="turn", run_id="r1")
    assert [item.path for item in result.skipped] == ["b.py"]
    assert target.read_bytes() == b"user-made\n"

    forced = restore(ledger, workspace_root=workspace, scope="turn", run_id="r1", force=True)
    assert [item.action for item in forced.restored] == ["written"]
    assert target.read_bytes() == b"orig\n"


def test_quota_stops_storing_new_content(tmp_path: Path) -> None:
    """会话配额用尽：新的改前内容不再存，但已存的照常可用，且原因写清楚。"""
    ledger, workspace = make_ledger(tmp_path, max_session_bytes=1)
    target = workspace / "a.py"
    target.write_bytes(b"v1\n")
    ledger.bind_run("r1")
    edit(ledger, target, b"v2\n")

    op = ledger.ops()[-1]
    assert op.before_ref is None
    assert op.before_reason is not None
    assert "上限" in op.before_reason

    result = restore(ledger, workspace_root=workspace, scope="turn", run_id="r1")
    assert result.restored == ()
    assert len(result.skipped) == 1


def test_restoring_old_turn_checks_the_latest_session_write(tmp_path: Path) -> None:
    ledger, workspace = make_ledger(tmp_path)
    target = workspace / "a.py"
    target.write_bytes(b"original")
    ledger.bind_run("r1")
    edit(ledger, target, b"first")
    ledger.bind_run("r2")
    edit(ledger, target, b"second")

    target.write_bytes(b"manual")
    conflict = restore(ledger, workspace_root=workspace, scope="turn", run_id="r1")
    assert len(conflict.skipped) == 1
    assert target.read_bytes() == b"manual"

    target.write_bytes(b"second")
    result = restore(ledger, workspace_root=workspace, scope="turn", run_id="r1")
    assert len(result.restored) == 1
    assert target.read_bytes() == b"original"


def test_first_unavailable_snapshot_is_not_replaced_by_an_intermediate(
    tmp_path: Path,
) -> None:
    ledger, workspace = make_ledger(tmp_path, max_baseline_bytes=64 * 1024)
    target = workspace / "a.py"
    target.write_bytes(b"x" * (64 * 1024 + 1))
    ledger.bind_run("r1")
    edit(ledger, target, b"intermediate")
    edit(ledger, target, b"final")

    result = restore(ledger, workspace_root=workspace, scope="turn", run_id="r1")
    assert not result.restored
    assert len(result.skipped) == 1
    assert "超过" in result.skipped[0].reason
    assert target.read_bytes() == b"final"
    assert all(op.before_ref is None for op in ledger.ops())


def test_resuming_the_same_turn_keeps_its_first_snapshot(tmp_path: Path) -> None:
    ledger, workspace = make_ledger(tmp_path)
    target = workspace / "a.py"
    target.write_bytes(b"original")
    ledger.bind_run("r1")
    edit(ledger, target, b"first")
    ledger.bind_run("r1")
    edit(ledger, target, b"second")

    assert sum(op.before_ref is not None for op in ledger.ops()) == 1
    result = restore(ledger, workspace_root=workspace, scope="turn", run_id="r1")
    assert len(result.restored) == 1
    assert target.read_bytes() == b"original"


def test_baseline_cleanup_propagates_failure_and_can_be_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from local_agent.persistence import file_changes

    ledger, workspace = make_ledger(tmp_path)
    target = workspace / "a.py"
    target.write_bytes(b"original")
    ledger.bind_run("r1")
    edit(ledger, target, b"changed")
    ref, _, _ = ledger.session_baseline("a.py")
    assert ref is not None

    with monkeypatch.context() as injected:
        def fail_cleanup(path):
            raise PermissionError("injected baseline lock")

        injected.setattr(file_changes.shutil, "rmtree", fail_cleanup)
        with pytest.raises(PermissionError):
            ledger.delete_session()

    assert ledger.load_session_baseline(ref) == b"original"
    ledger.delete_session()
    ledger.delete_session()  # 已不存在的目录不妨碍重试。
    assert ledger.load_session_baseline(ref) is None
