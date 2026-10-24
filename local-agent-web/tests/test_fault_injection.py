"""T81：五类**真实的**基础设施故障注入（架构 §20.5）。

为什么要这一片：既有故障用例大多靠替身制造异常，证明的是"我们以为的那条分支会跑"。这里注入
的是**基础设施真的坏掉**时的形态，每条都断言"**有明确的恢复或停止策略**"——而且只回到既有
语义（`executing` / 需人工核对），不新造一套。

五类（各至少一条）：

1. **磁盘满（ENOSPC）**：数据库层用 SQLite 自己的 ``PRAGMA max_page_count`` 把库顶到上限，
   拿到的就是 SQLite 自己产生的 ``SQLITE_FULL``（"database or disk is full"）；文件写路径用
   **系统调用边界**的真实 ``OSError(ENOSPC)``。后者如实写明：本机无法真的把卷写满（那需要
   管理员权限建小卷或设配额），所以文件层是在边界注入**真错误码**，产品路径仍然真跑。
2. **真实 ``database is locked``**：另开一条连接 ``BEGIN IMMEDIATE``，真占住写锁。
3. **进程被 OS 杀**：子进程 ``os.abort()``（Windows ``0xC0000409`` / POSIX ``SIGABRT``）——真的
   异常终止，不是我们杀的、也不是超时。
4. **半写文件**：真把一个 UTF-8 文件截断在多字节字符中间（另有 0 字节形态），检查读取/编辑/
   检索三条路径都如实处理。
5. **Checkpoint 落盘中断**：**真起一个进程跑一轮，在"工具副作用已发生、结果还没落盘"时把它
   ``kill()`` 掉**，再按既有的"重开＝重启"口径核对：库没坏、不自动当成完成、不重放工具。
"""

from __future__ import annotations

import errno
import multiprocessing
import os
import shutil
import signal
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from conftest import make_model
from test_application import ModelFactory, _delete_harness, _harness, _open, _session

from local_agent.application import errors as app_errors
from local_agent.config import AppConfig
from local_agent.exceptions import FileOperationError
from local_agent.graph import builder
from local_agent.persistence.request_ledger import STATUS_EXECUTING
from local_agent.rag import search
from local_agent.reflection.validators import validate_tool_event
from local_agent.security.approval import action_fingerprint
from local_agent.security.policy import SecurityPolicy
from local_agent.tools import result_spill, search_tools
from local_agent.tools.file_tools import edit_file_content, read_file_content
from local_agent.tools.shell_tools import run_shell_command

RUN_ID = "run-t81"


@pytest.fixture
def policy(tmp_path: Path, app_config: AppConfig) -> SecurityPolicy:
    """与 `test_tools.py` 的 `policy` 同一形态（两行，重复一份比跨模块引用更省事）。"""
    return SecurityPolicy.from_config(app_config, workspace_root=tmp_path, environ={})


# ---------------------------------------------------------------- 1. 磁盘满（ENOSPC）


def test_sqlite_full_stops_the_request_without_claiming_an_outcome(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """数据库真写不下时：**如实抛出 SQLite 自己的错误**，台账停在"终态不确定"，会话随后可核对恢复。

    ``PRAGMA max_page_count`` 是 SQLite 自己的容量闸门：超过它，SQLite 返回 ``SQLITE_FULL``
    （错误文案 "database or disk is full"）——真实满盘走的也是这一条。**不**把它翻成"成功"或
    "失败"是本用例的核心：终态不确定时既有的口径是保留 ``executing`` 并要人核对（§15.2）。
    """
    harness = _harness(tmp_path, model_factory, app_config, decision="chat", reply="好的")
    session_id = _session(harness)
    connection = harness.database.connection
    pages = connection.execute("PRAGMA page_count").fetchone()[0]
    connection.execute(f"PRAGMA max_page_count={pages}")

    with pytest.raises(sqlite3.OperationalError, match="full"):
        harness.service.submit_message(session_id, "你好", "req-full")

    rows = connection.execute("SELECT request_id, status FROM request_ledger").fetchall()
    assert rows == [("req-full", STATUS_EXECUTING)], "写不下时台账必须留在「终态不确定」上"
    snapshot = harness.service.get_session_snapshot(session_id)
    assert snapshot.interrupted_request_id == "req-full"

    # 既有策略：还挂着待核对请求时，下一次提交被拒绝（不排队、不自动重试）。
    with pytest.raises(app_errors.ApplicationError):
        harness.service.submit_message(session_id, "再来", "req-full-2")

    # 腾出空间 ⇒ 用户核对后收口 ⇒ 会话恢复可用（这就是"有明确恢复策略"）。
    connection.execute("PRAGMA max_page_count=1000000")
    ended = harness.service.end_interrupted_request(
        session_id, "req-full", snapshot.snapshot_id
    )
    assert ended.interrupted_request_id is None
    assert harness.service.submit_message(session_id, "现在可以了", "req-full-3").status == "completed"
    harness.database.connection.close()


def test_enospc_on_the_spill_write_is_reported_not_raised(
    tmp_path: Path, policy: SecurityPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    """文件写路径拿到 ``ENOSPC``：落盘失败**绝不变成工具错误**，也不留半成品。

    既有口径（§6.7 / §20.3）：落盘失败只记日志、返回 `None`，调用方在 `note` 里**如实**写
    "完整结果未能保存"，工具结果照旧是"不完整结果"——既不抛错，也不谎称已保存。写不进去
    就等于没有这个文件：目录里不能留下一个截断的"完整结果"。

    注入点在**系统调用边界**（真的 ``OSError(ENOSPC)``）：本机没法真把卷写满——那要管理员
    权限建小卷或设配额——所以这是本片唯一一处"在边界注入真错误码"，产品侧（`result_spill`
    → `grep` 的 note）整个真跑。
    """
    for index in range(4):
        (tmp_path / f"f{index}.py").write_text("target line\n" * 5, encoding="utf-8")

    real_open = Path.open

    def refuse(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self.name.startswith("grep-"):
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", refuse)

    result = search_tools.grep_content("target", policy=policy, max_matches=3, save_full=True)

    assert result.note is not None and result_spill.SPILL_FAILED_NOTE in result.note
    assert len(result.matches) == 3 and result.total_matches == 20
    assert not list((tmp_path / result_spill.SPILL_DIR).glob("grep-*.txt"))


# ------------------------------------------------- 2. 真实 `database is locked`


def test_real_database_lock_fails_the_request_without_leaving_residue(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """真被别的连接占着写锁：请求**以真实 sqlite 错误失败**，而且台账零残留、可原样重试。

    为什么这条重要：锁住时如果留下一条"执行中"，用户就会被一个**从未真正开始**的请求挡住，
    得先去做一次无意义的核对。所以这里的断言是"什么都没留下 + 解锁后同 id 直接成功"。
    """
    harness = _harness(tmp_path, model_factory, app_config, decision="chat", reply="好的")
    session_id = _session(harness)
    # 只把**等待时长**调短（默认 5 秒）；判定口径不变，错误仍是 sqlite 自己那条。
    harness.database.connection.execute("PRAGMA busy_timeout=200")

    other = sqlite3.connect(str(harness.database_path))
    other.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            harness.service.submit_message(session_id, "你好", "req-locked")
    finally:
        other.rollback()
        other.close()

    assert harness.database.connection.execute(
        "SELECT COUNT(*) FROM request_ledger").fetchone()[0] == 0
    blocked = harness.service.get_session_snapshot(session_id)
    assert blocked.interrupted_request_id is None
    assert blocked.messages == []

    # 解锁之后：同 id、同参数重试**直接执行**（没有"执行中"残留挡路）。
    assert harness.service.submit_message(session_id, "你好", "req-locked").status == "completed"
    harness.database.connection.close()


# ------------------------------------------------------------ 3. 进程被 OS 杀


def _shell_event() -> Any:
    """一条 `run_shell` 的工具事件（机械校验要的口径与真实投递一致）。"""
    from local_agent.graph.state import ToolEvent

    now = datetime.now(timezone.utc)
    return ToolEvent(
        tool_call_id="call-1",
        tool_name="run_shell",
        redacted_arguments={"command": "…"},
        status="ok",
        output_preview="…",
        output_digest="digest",
        run_id=RUN_ID,
        started_at=now,
        finished_at=now,
    )


def test_os_killed_child_is_reported_as_abnormal_and_never_as_success(
    tmp_path: Path, policy: SecurityPolicy
) -> None:
    """子进程被 OS 异常终止（这里让它自己 `abort`）：退出码原样给出，且**不**冒充成功。

    为什么用 `os.abort()`：它给出的是真正的"被 OS 终止"形态——Windows ``0xC0000409``、POSIX
    ``SIGABRT``（Python 记为 ``-6``）。要钉住三件事：① 退出码是真值；② 不是超时、也不是我们
    终止的（不写"已终止"）；③ 机械校验把它判成失败——**一个崩溃的进程永远不会被读成成功**。
    """
    command = f'"{sys.executable}" -c "import os; os.abort()"'
    fingerprint = action_fingerprint(
        "run_shell",
        {
            "command": command,
            "cwd": ".",
            "timeout_seconds": policy.security.shell.default_timeout_seconds,
            "env_keys": [],
        },
        policy=policy,
    )
    policy.grant(fingerprint)
    try:
        result = run_shell_command(command, policy=policy, cwd=".")
    finally:
        policy.revoke(fingerprint)

    expected = 0xC0000409 if os.name == "nt" else -signal.SIGABRT
    assert result.exit_code == expected, "异常终止码必须原样给出"
    assert result.timed_out is False
    assert result.note is None or "已终止" not in result.note
    assert result.note is None or "超时" not in result.note

    # 机械校验的输入与图里记录的**同一形状**：结果记录 + 已解析的 payload。
    payload = result.model_dump(mode="json")
    record = {
        "tool_call_id": "call-1",
        "tool_name": "run_shell",
        "status": "ok",
        "truncated": result.truncated,
        "output_preview": result.model_dump_json(),
        "output_digest": "digest",
        "run_id": RUN_ID,
    }
    evidence = validate_tool_event(_shell_event(), record, payload=payload)
    assert evidence.exit_code_ok is False
    assert evidence.evidence_level == "failed"
    assert any("退出码" in reason for reason in evidence.reasons)


# ------------------------------------------------------------------ 4. 半写文件


def test_a_half_written_file_is_never_read_or_edited_as_content(
    tmp_path: Path, policy: SecurityPolicy, app_config: AppConfig
) -> None:
    """真的被截断在多字节字符中间的文件：三条读取路径都如实处理，谁都不崩、也不编内容。

    半写文件是"写到一半就没了"的直接产物（进程被杀、磁盘满都会留下它）。既有口径是：
    `read_file` 不给内容并说明"无法按 UTF-8 解码"；`edit_file` 直接拒绝（不能拿一份解码后的
    残骸去改文件）；`grep` 跳过它。0 字节是同一类的另一种形态——它是合法 UTF-8，读出来是空。
    """
    (tmp_path / "half.txt").write_bytes(b"\xe4\xb8")  # "中" 的前两个字节：截在多字节字符中间
    (tmp_path / "empty.txt").write_bytes(b"")
    (tmp_path / "ok.txt").write_text("中文内容\n", encoding="utf-8")

    half = read_file_content("half.txt", policy=policy)
    assert half.content == "" and half.is_binary is False
    assert half.note is not None and "无法按 UTF-8 解码" in half.note

    with pytest.raises(FileOperationError, match="无法按utf-8-sig解码"):
        edit_file_content("half.txt", old_text="中", new_text="国", policy=policy)
    assert (tmp_path / "half.txt").read_bytes() == b"\xe4\xb8"

    grep = search_tools.grep_content("中文", policy=policy)
    assert [match.path for match in grep.matches] == ["ok.txt"], "半写文件不能被当成命中"
    assert grep.note is None or "half.txt" not in grep.note

    outcome = search.discover_matches(
        ["中文"], workspace_root=tmp_path, config=app_config.rag, resolve=lambda *a, **k: None
    )
    assert outcome.source == "python_scan"
    assert [match.relative_path for match in outcome.matches] == ["ok.txt"]

    empty = read_file_content("empty.txt", policy=policy)
    assert empty.content == "" and empty.note is None


# ------------------------------------------------------ 5. Checkpoint 落盘中断


def _run_a_turn_and_hang(
    tmp_dir: str, marker: str, config_json: str
) -> None:  # pragma: no cover - 子进程入口
    """子进程：跑一轮，在"工具副作用已发生、结果还没落盘"时挂住，等父进程把它杀掉。

    这就是"Checkpoint 落盘中断"的真实形态：进程死在**副作用与结果落盘之间**的窗口里。
    配置由父进程原样传进来（`AppConfig` 的 JSON），子进程不重复一遍装配逻辑。
    """
    original = builder.tool_execution_node

    def hang(state: Any, runtime: Any) -> Any:
        original(state, runtime)  # 真的执行工具：副作用发生
        Path(marker).write_text("副作用已发生", encoding="utf-8")
        while True:  # 结果还没落盘，就卡在这里
            time.sleep(0.05)

    builder.tool_execution_node = hang
    harness = _delete_harness(
        Path(tmp_dir), make_model, AppConfig.model_validate_json(config_json)
    )
    session_id = _session(harness)
    harness.service.submit_message(session_id, "删除 note.txt", "delete")
    approval = harness.service.get_session_snapshot(session_id).approval
    assert approval is not None
    harness.service.resolve_approval(session_id, approval.approval_id, "approve", "approve")


def test_kill_between_side_effect_and_checkpoint_is_reconciled_never_replayed(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """真在"副作用已发生、结果还没落盘"时杀掉进程：库没坏、不自动算完成、也不重放工具。

    这是本片最重的一条，也最像真实事故：进程死在那半个窗口里，磁盘上已经少了一个文件，而库里
    还没有任何结果。重启后必须回到既有语义——**终态不确定 ⇒ 要人工核对**，绝不自动补一个
    "完成"，也绝不因为"没记录"就把同一个副作用再执行一遍。
    """
    marker = tmp_path / "side-effect.marker"
    context = multiprocessing.get_context("spawn")
    process = context.Process(
        target=_run_a_turn_and_hang,
        args=(str(tmp_path), str(marker), app_config.model_dump_json()),
    )
    try:
        process.start()
        deadline = time.monotonic() + 120
        while not marker.exists() and time.monotonic() < deadline and process.is_alive():
            time.sleep(0.1)
        assert marker.exists(), "子进程没跑到工具副作用那一步"
        process.kill()  # 真实终止（Windows TerminateProcess / POSIX SIGKILL）
        process.join(timeout=60)
    finally:
        if process.is_alive():  # pragma: no cover - 兜底，避免留下孤儿进程
            process.kill()
            process.join(timeout=30)

    assert not (tmp_path / "ws" / "note.txt").exists(), "副作用真的发生了"

    # 重开＝重启（既有口径）：库必须能打开，而且没有被写坏。
    reopened = _open(tmp_path, model_factory, app_config, tmp_path / "data" / "checkpoints.sqlite",
                     decision="chat", reply="好的")
    try:
        assert reopened.database.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        session_id = reopened.service.list_sessions()[0].session_id

        # 既有的停止策略：终态不确定 ⇒ 挡住下一次提交，并要求人工核对。
        blocked = reopened.service.get_session_snapshot(session_id)
        assert blocked.interrupted_request_id == "approve"
        with pytest.raises(app_errors.ApplicationError):
            reopened.service.submit_message(session_id, "重来一次", "again")

        # 核对后收口：这一轮以"未完整记录"结束，且**没有重放**那个副作用。
        ended = reopened.service.end_interrupted_request(
            session_id, "approve", blocked.snapshot_id
        )
        assert ended.interrupted_request_id is None
        assert reopened.service.submit_message(session_id, "继续", "after").status == "completed"
        assert marker.read_text(encoding="utf-8") == "副作用已发生"
    finally:
        reopened.database.connection.close()
        shutil.rmtree(tmp_path, ignore_errors=True)
