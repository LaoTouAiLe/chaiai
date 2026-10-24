"""产物汇总（§15.11.1，2026-10-22）的契约测试。

只测应用层函数，不启 HTTP、不读真实数据库：`artifacts()` 只依赖 service 上的
`_sessions` / `_conversations` / `_change_ledger` / `_attachments` / `_sqlite_access`。
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from local_agent.application import artifacts as artifacts_module
from local_agent.application.artifacts import artifacts, preview


@pytest.fixture(autouse=True)
def _clear_artifact_cache():
    """每个用例前后清缓存：`id(service)` 会被复用，不清就会串到上一个用例的数据。"""
    artifacts_module._CACHE.clear()
    yield
    artifacts_module._CACHE.clear()


def _session(session_id: str, *, title: str = "", kind: str = "interactive",
             workspace_root: str = "/workspace", hours_ago: int = 1) -> SimpleNamespace:
    stamp = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()
    return SimpleNamespace(
        session_id=session_id,
        thread_id=f"t-{session_id}",
        title=title or session_id,
        workspace_root=workspace_root,
        project_id="p",
        created_at=stamp,
        updated_at=stamp,
        last_checkpoint_id=None,
        status="idle",
        mode="agent",
        archived=False,
        kind=kind,
    )


class _Sessions:
    def __init__(self, sessions: list[SimpleNamespace]) -> None:
        self._all = sessions

    def list_sessions(self, *, workspace_root=None, archived: bool = False):
        return [item for item in self._all if item.archived is archived]


class _Conversations:
    def __init__(self, turns: dict[str, list[SimpleNamespace]]) -> None:
        self._turns = turns

    def turns(self, session_id: str):
        return self._turns.get(session_id, [])

    def turn_texts(self, session_ids):
        """批量接口：与生产实现同形，返回 (session_id, question, answer, submitted_at)。"""
        rows = []
        for session_id in session_ids:
            for turn in self._turns.get(session_id, []):
                rows.append((session_id, turn.question or "", turn.answer_text or "",
                             turn.submitted_at))
        return rows


def _turn(question: str, answer: str) -> SimpleNamespace:
    return SimpleNamespace(
        question=question,
        answer_text=answer,
        state="completed",
        submitted_at=datetime.now(timezone.utc).isoformat(),
        checkpoint_id=None,
        ordinal=1,
    )


class _Ledger:
    def __init__(self, ops: list[SimpleNamespace]) -> None:
        self._ops = ops

    def ops(self):
        return self._ops


def _service(*, sessions, turns=None, ledgers=None, attachments=None, root: Path | None = None):
    workspace = root if root is not None else Path.cwd()
    service = SimpleNamespace(
        _sessions=_Sessions(sessions),
        _conversations=_Conversations(turns or {}),
        _secrets=(),
        _workspace_root=workspace,
        _attachments=attachments,
        _sqlite_access=lambda fn: fn(),
        # 预览按这个解析工作区根；需要读真实文件的用例显式传 root。
        _path_for_workspace_id=lambda workspace_id: workspace,
    )
    service._change_ledger = lambda session: (ledgers or {}).get(session.session_id)
    return service


def test_empty_database_returns_empty_page(tmp_path: Path) -> None:
    page = artifacts(_service(sessions=[], root=tmp_path))
    assert page.entries == []
    assert page.total == 0
    assert page.scanned_sessions == 0
    assert page.incomplete is False


def test_extracts_markdown_image_and_link_from_answer_text() -> None:
    session = _session("s1", title="作图")
    service = _service(
        sessions=[session],
        turns={"s1": [_turn("生成一张图", "好的：![封面](images/cover.png)\n参考 https://example.com/a?b=1")]},
    )
    page = artifacts(service)
    kinds = {entry.kind for entry in page.entries}
    assert "image" in kinds
    assert "link" in kinds
    image = next(entry for entry in page.entries if entry.kind == "image")
    assert image.location == "images/cover.png"
    assert image.source == "message"
    assert image.session_id == "s1"


def test_file_change_keeps_create_and_modify_but_not_delete() -> None:
    session = _session("s2")
    ops = [
        SimpleNamespace(kind="create", path="src/app.py", created_at="2026-10-22T00:00:00+00:00"),
        SimpleNamespace(kind="modify", path="README.md", created_at="2026-10-22T00:00:00+00:00"),
        SimpleNamespace(kind="delete", path="old.txt", created_at="2026-10-22T00:00:00+00:00"),
    ]
    page = artifacts(_service(sessions=[session], ledgers={"s2": _Ledger(ops)}))
    locations = {entry.location for entry in page.entries if entry.source == "file_change"}
    assert locations == {"src/app.py", "README.md"}


def test_internal_sessions_are_excluded() -> None:
    service = _service(sessions=[
        _session("root", title="父会话"),
        _session("sub", title="子 Agent", kind="subagent"),
        _session("beat", title="心跳", kind="heartbeat"),
    ])
    page = artifacts(service)
    assert page.scanned_sessions == 1


def test_archived_sessions_are_scanned() -> None:
    archived = _session("old")
    archived.archived = True
    page = artifacts(_service(sessions=[archived]))
    assert page.scanned_sessions == 1


def test_kind_filter_and_pagination() -> None:
    session = _session("s3")
    turns = {"s3": [_turn("", "https://a.example/1 https://a.example/2 https://a.example/3")]}
    service = _service(sessions=[session], turns=turns)

    links = artifacts(service, kind="link")
    assert links.total == 3
    assert all(entry.kind == "link" for entry in links.entries)

    first = artifacts(service, offset=0, limit=2)
    assert len(first.entries) == 2
    assert first.total == 3
    second = artifacts(service, offset=2, limit=2)
    assert len(second.entries) == 1


def test_absolute_path_inside_workspace_becomes_relative(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    target = workspace / "dist" / "app.js"
    target.parent.mkdir(parents=True)
    target.write_text("x", encoding="utf-8")

    session = _session("s4", workspace_root=str(workspace))
    turns = {"s4": [_turn("", f"产物在 {target}")]}
    page = artifacts(_service(sessions=[session], turns=turns))
    entry = next(item for item in page.entries if item.kind == "file")
    assert entry.location == "dist/app.js"
    assert entry.external is False


def test_absolute_path_outside_workspace_is_marked_external(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "elsewhere" / "notes.txt"
    outside.parent.mkdir()
    outside.write_text("x", encoding="utf-8")

    session = _session("s5", workspace_root=str(workspace))
    turns = {"s5": [_turn("", f"我读了这个文件 {outside}")]}
    page = artifacts(_service(sessions=[session], turns=turns))
    entry = next(item for item in page.entries if item.kind == "file")
    assert entry.external is True


def test_sessions_older_than_window_are_skipped() -> None:
    service = _service(sessions=[
        _session("fresh", hours_ago=1),
        _session("stale", hours_ago=24 * 60),
    ])
    page = artifacts(service, days=30)
    assert page.scanned_sessions == 1


# ---- 内容预览（§15.11.3）---------------------------------------------------
# 这四条守的是同一条硬边界：**只读工作区内**。放宽任何一条，
# 这个接口就变成"任意文件读取"。


def test_preview_reads_text_inside_workspace(tmp_path: Path) -> None:
    target = tmp_path / "src" / "app.py"
    target.parent.mkdir(parents=True)
    target.write_text("print('你好')\n", encoding="utf-8")
    service = _service(sessions=[], root=tmp_path)

    view = preview(service, path="src/app.py")
    assert view.kind == "text"
    assert "print" in (view.text or "")
    assert view.truncated is False


def test_preview_rejects_paths_outside_workspace(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-secret.txt"
    outside.write_text("不能读我", encoding="utf-8")
    service = _service(sessions=[], root=tmp_path)

    with pytest.raises(PermissionError):
        preview(service, path="../outside-secret.txt")
    with pytest.raises(PermissionError):
        preview(service, path=str(outside))


def test_preview_refuses_external_urls(tmp_path: Path) -> None:
    """外链交给浏览器打开；服务端替用户发请求就是 SSRF。"""
    service = _service(sessions=[], root=tmp_path)
    with pytest.raises(ValueError, match="浏览器"):
        preview(service, path="https://example.com/a.png")


def test_oversize_text_is_truncated_not_rejected(tmp_path: Path) -> None:
    """文本超限要**截断返回**：整份拒绝等于什么都没给（2026-10-22 实机反馈）。"""
    big = tmp_path / "big.log"
    big.write_bytes(b"x" * (300 * 1024))
    service = _service(sessions=[], root=tmp_path)

    view = preview(service, path="big.log")
    assert view.kind == "text"
    assert view.truncated is True
    assert view.text is not None and len(view.text) > 0
    assert view.size_bytes >= 300 * 1024


def test_oversize_image_is_still_rejected(tmp_path: Path) -> None:
    """图片超限仍然拒绝：整份转 data URL 会把页面拖死，这是唯一该硬拒的情形。

    上限是 2 MB（截图常见 300–800 KB，256 KB 会把大部分截图挡在门外）。
    """
    huge = tmp_path / "huge.png"
    huge.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * (2 * 1024 * 1024 + 1024))
    service = _service(sessions=[], root=tmp_path)

    view = preview(service, path="huge.png")
    assert view.kind == "too_large"
    assert view.url is None


def test_medium_image_under_limit_is_previewed(tmp_path: Path) -> None:
    """300 KB 的截图必须能看——这正是常见尺寸。"""
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * (300 * 1024))
    service = _service(sessions=[], root=tmp_path)

    view = preview(service, path="shot.png")
    assert view.kind == "image"
    assert (view.url or "").startswith("data:image/png;base64,")


def test_preview_detects_image_by_magic_bytes(tmp_path: Path) -> None:
    png = tmp_path / "cover.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    service = _service(sessions=[], root=tmp_path)

    view = preview(service, path="cover.png")
    assert view.kind == "image"
    assert (view.url or "").startswith("data:image/png;base64,")


def test_preview_missing_file_is_lookup_error(tmp_path: Path) -> None:
    service = _service(sessions=[], root=tmp_path)
    with pytest.raises(LookupError):
        preview(service, path="nowhere.txt")


def test_preview_resolves_attachments_scope(tmp_path: Path) -> None:
    """附件路径相对附件根，不是工作区根（2026-10-22）。"""
    store_root = tmp_path / "data" / "attachments"
    target = store_root / "2026" / "10" / "22" / "sess-1" / "a.txt"
    target.parent.mkdir(parents=True)
    target.write_text("附件内容", encoding="utf-8")
    service = _service(sessions=[], root=tmp_path)
    service._attachments = SimpleNamespace(root=store_root)

    view = preview(service, path="2026/10/22/sess-1/a.txt", scope="attachments")
    assert view.kind == "text"
    assert "附件内容" in (view.text or "")
    # 同一路径按工作区解析应当越界，证明两个 scope 真的不同
    with pytest.raises(PermissionError):
        preview(service, path="../outside.txt")


def test_posix_path_regex_is_fast_on_realistic_text() -> None:
    """回归（2026-10-22）：`(?:[^...]+/)+` 的段内允许 `/`，会造成指数级回溯。

    实测那条正则单独就把 140 轮正文拖到 44 秒；修好后同样的量级应在毫秒级。
    这里用真实形态的文本（URL + 路径 + 中文）压测。
    """
    payload = (
        "参考 https://example.com/a/b?x=1 和 `frontend/src/app/main.tsx`，"
        "以及 C:\\Users\\me\\project\\a.py ；另见 /usr/local/bin/python3 与 /a/b/c 这类无扩展名的串。\n"
    ) * 200
    start = time.perf_counter()
    artifacts_module._POSIX_PATH.findall(payload)
    # 阈值故意宽松：这条守的是"不要退回到 44 秒量级"，不是性能基准。
    assert time.perf_counter() - start < 1.0


def test_refresh_bypasses_the_60s_cache(tmp_path: Path) -> None:
    """`refresh=True` 必须真的重扫（§15.11.1，2026-10-23）。

    页面上的「刷新」按钮就靠它：普通 refetch 只会再拿一次缓存结果，
    用户看到"点了还是旧数据"，会以为产物没生成。
    """
    first = _service(sessions=[_session("s1", hours_ago=1)], turns={
        "s1": [_turn("第一次提问", "见 https://example.com/a")],
    }, root=tmp_path)
    page = artifacts(first, days=30)
    assert page.total >= 1
    assert page.scanned_sessions == 1

    # 同一实例第二次走缓存：数据没变
    assert artifacts(first, days=30).total == page.total

    # 换了数据但同 key：缓存仍是旧值
    first._sessions = _Sessions([_session("s1", hours_ago=1), _session("s2", hours_ago=1)])
    assert artifacts(first, days=30).scanned_sessions == 1

    # refresh 跳过缓存：看到新会话
    assert artifacts(first, days=30, refresh=True).scanned_sessions == 2


def test_binary_files_are_not_decoded_as_text(tmp_path: Path) -> None:
    """docx 等二进制不能当文本读（2026-10-23 实机反馈：界面一屏乱码）。

    docx 是 zip 容器（魔数 `PK\x03\x04`），按 `decode(errors="replace")` 出来全是乱码。
    这里明确回 `binary`，让界面说"不支持预览"，而不是展示垃圾字符。
    """
    doc = tmp_path / "报告.docx"
    doc.write_bytes(b"PK\x03\x04" + b"\x00" * 512)
    service = _service(sessions=[], root=tmp_path)

    view = preview(service, path="报告.docx")
    assert view.kind == "binary"
    assert view.suffix == "docx"
    assert view.text is None, "二进制不得返回任何文本内容"


def test_plain_text_is_still_text(tmp_path: Path) -> None:
    """不能因为拦二进制把普通文本也拦掉。"""
    (tmp_path / "notes.md").write_text("# 标题", encoding="utf-8")
    service = _service(sessions=[], root=tmp_path)

    view = preview(service, path="notes.md")
    assert view.kind == "text"
    assert "标题" in (view.text or "")
