"""大文件与检索续页的真实 I/O、完整性、版本及权限边界。"""

from __future__ import annotations

import hashlib
import json

import pytest

from local_agent.cancellation import CancellationToken
from local_agent.exceptions import FileOperationError, SecurityError
from local_agent.memory.tool_output import project_file_tool_output
from local_agent.security.path_guard import ExternalPathPolicy
from local_agent.security.policy import SecurityPolicy
from local_agent.tools import search_tools
from local_agent.tools.file_tools import build_file_tools, read_file_content
from local_agent.tools.search_tools import build_search_tools, glob_files, grep_content
from local_agent.tools.text_stream import text_lines, text_parts


@pytest.fixture
def policy(tmp_path, app_config):
    return SecurityPolicy.from_config(app_config, workspace_root=tmp_path, environ={})


def test_read_and_search_reach_tail_past_old_byte_limits(tmp_path, policy):
    data = b"0123456789\n" * 200000 + b"tail-marker\n"
    (tmp_path / "large.txt").write_bytes(data)
    policy.workspace.max_file_size_bytes = 4
    read = read_file_content("large.txt", policy=policy, start_line=200001)
    assert read.content == "tail-marker\n" and read.line_count == 200001
    assert read.sha256 == hashlib.sha256(data).hexdigest()
    found = grep_content("tail-marker", path="large.txt", policy=policy)
    assert found.matches[0].line_number == 200001 and not found.truncated


@pytest.mark.parametrize("text", ["甲" * 70000 + "\r\n结束\n", "a" * 65535 + "\r\nb\r尾", ""],
                         ids=["utf8-chunks", "crlf-boundary", "empty"])
def test_character_pages_reassemble_exact_original_across_chunks(tmp_path, policy, text):
    (tmp_path / "long.txt").write_bytes(text.encode("utf-8-sig"))
    page = read_file_content("long.txt", policy=policy, max_chars=17003)
    pieces = [page.content]
    while page.has_more:
        offset = page.next_start_char
        assert offset is not None
        page = read_file_content("long.txt", policy=policy, max_chars=17003,
                                 start_char=offset, expected_sha256=page.sha256)
        assert page.start_char == offset and (not page.has_more or page.next_start_char > offset)
        pieces.append(page.content)
    assert "".join(pieces) == text
    assert page.line_count == len(text.splitlines())


def test_line_range_and_midline_page_keep_honest_coordinates(tmp_path, policy):
    (tmp_path / "long.txt").write_bytes(b"first\n" + b"x" * 140000 + b"\nlast\n")
    page = read_file_content("long.txt", policy=policy, start_char=70000, max_chars=80000)
    assert page.first_line == 2 and page.last_line == 3 and page.partial_lines == [2]
    assert page.content.endswith("\nlast\n") and not page.has_more
    selected = read_file_content("long.txt", policy=policy, start_line=3, end_line=3)
    assert selected.content == "last\n" and selected.start_char == 140007
    assert not read_file_content("long.txt", policy=policy, start_line=99).content


@pytest.mark.parametrize("encoding", ["gbk", "utf-16", "utf-32"])
def test_explicit_encoding_is_supported_by_read_and_search(tmp_path, policy, encoding):
    (tmp_path / "encoded.txt").write_bytes("开始\r\n尾部中文\n".encode(encoding))
    page = read_file_content("encoded.txt", policy=policy, encoding=encoding, start_line=2)
    assert page.content == "尾部中文\n" and page.line_count == 2
    found = grep_content("中文", policy=policy, encoding=encoding)
    assert found.matches[0].line_number == 2 and not found.skipped_files


def test_read_hash_rejects_changed_file_and_stream_changes(tmp_path, policy, monkeypatch):
    path = tmp_path / "version.txt"
    path.write_bytes(b"first\nsecond\n")
    page = read_file_content(path.name, policy=policy, max_chars=6)
    path.write_bytes(b"new-content\n")
    with pytest.raises(FileOperationError, match="已变化"):
        read_file_content(path.name, policy=policy, start_char=page.next_start_char,
                          expected_sha256=page.sha256)
    original = policy.files.iter_bytes

    def changing(target):
        yield from original(target)
        path.write_bytes(b"changed-again\n")

    monkeypatch.setattr(policy.files, "iter_bytes", changing)
    with pytest.raises(FileOperationError, match="期间文件已变化"):
        read_file_content(path.name, policy=policy)


@pytest.mark.parametrize("arguments", [{"max_chars": 0}, {"start_char": -1},
                                      {"start_line": 2, "start_char": 3}, {"end_line": 0},
                                      {"encoding": "not-a-codec"}])
def test_read_rejects_invalid_parameters(tmp_path, policy, arguments):
    (tmp_path / "note.txt").write_bytes(b"x\n")
    with pytest.raises(ValueError):
        read_file_content("note.txt", policy=policy, **arguments)


def test_long_line_coverage_is_explicit_and_can_expand(tmp_path, policy):
    (tmp_path / "minified.json").write_text("x" * 160000 + "needle", encoding="utf-8")
    limited = grep_content("needle", policy=policy)
    assert not limited.matches and limited.limited_lines == 1 and limited.truncated
    full = grep_content("needle", policy=policy, max_line_chars=None)
    assert full.total_matches == 1 and not full.truncated
    assert "needle" in full.matches[0].text and len(full.matches[0].text) < 310


@pytest.mark.parametrize("mode", ["content", "count", "files_with_matches"])
def test_grep_pages_cover_each_matching_line_once(tmp_path, policy, mode):
    for name in ("a.txt", "dir/b.txt", "z.txt"):
        path = tmp_path / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b"hit\nmiss\nhit\nhit\n")
    cursor = None
    seen = []
    counts = {}
    for _ in range(10):
        page = grep_content("hit", policy=policy, max_matches=2, cursor=cursor, output_mode=mode)
        seen.extend((m.path, m.line_number) for m in page.matches)
        for entry in page.hits:
            counts[entry.path] = counts.get(entry.path, 0) + entry.count
        if not page.has_more:
            break
        assert page.next_cursor and page.next_cursor != cursor
        cursor = page.next_cursor
    else:
        pytest.fail("游标没有终止")
    assert counts == {"a.txt": 3, "dir/b.txt": 3, "z.txt": 3}
    assert len(seen) == len(set(seen)) == (9 if mode == "content" else 0)


def test_scan_pages_reach_files_after_budget_and_can_increase_it(tmp_path, policy):
    for i in range(7):
        (tmp_path / f"{i}.txt").write_bytes(b"hit\n" if i == 6 else b"miss\n")
    page = grep_content("hit", policy=policy, max_scan_files=2)
    assert page.files_scanned == 2 and page.has_more and not page.matches
    next_page = grep_content("hit", policy=policy, cursor=page.next_cursor, max_scan_files=None)
    assert next_page.files_scanned == 5 and next_page.matches[0].path == "6.txt"


def test_glob_pages_and_expanded_page_have_no_old_hard_cap(tmp_path, policy):
    for i in range(2003):
        (tmp_path / f"f{i:04}.txt").touch()
    all_files = glob_files("*.txt", policy=policy, max_files=3000)
    assert len(all_files.files) == 2003 and not all_files.has_more
    first = glob_files("*.txt", policy=policy, max_files=2)
    rest = glob_files("*.txt", policy=policy, max_files=3000, cursor=first.next_cursor)
    assert first.files + rest.files == all_files.files and not rest.has_more


def test_matches_can_exceed_old_hard_cap(tmp_path, policy):
    (tmp_path / "hits.txt").write_bytes(b"hit\n" * 601)
    page = grep_content("hit", policy=policy, max_matches=700)
    assert len(page.matches) == page.total_matches == 601 and not page.has_more


@pytest.mark.parametrize("kind", ["grep", "glob"])
@pytest.mark.parametrize("change", ["query", "missing", "version", "malformed", "root"])
def test_invalid_or_stale_search_cursor_is_rejected(tmp_path, policy, kind, change):
    for name in ("a.txt", "b.txt", "c.txt"):
        (tmp_path / name).write_bytes(b"hit\nhit\n")
    tool = grep_content if kind == "grep" else glob_files
    pattern = "hit" if kind == "grep" else "*.txt"
    budget = {"max_matches": 1} if kind == "grep" else {"max_files": 1}
    first = tool(pattern, policy=policy, **budget)
    cursor = first.next_cursor
    args = {}
    if change == "query":
        pattern = "changed"
    elif change == "root":
        args["path"] = "a.txt"
    elif change == "malformed":
        cursor = "%%invalid"
    else:
        anchor = "a.txt" if kind == "grep" else "b.txt"
        if change == "missing":
            (tmp_path / anchor).unlink()
        else:
            (tmp_path / anchor).write_bytes(b"new version\n")
    with pytest.raises(ValueError, match="游标|变化|消失"):
        tool(pattern, policy=policy, cursor=cursor, **args, **budget)


def test_skip_reasons_do_not_claim_complete_no_match(tmp_path, policy, monkeypatch):
    (tmp_path / "binary").write_bytes(b"\0abc")
    (tmp_path / "encoding").write_bytes(b"\xff\xff")
    (tmp_path / "large").write_bytes(b"a" * 100)
    (tmp_path / "denied").write_bytes(b"text")
    original = policy.files.iter_bytes

    def unreadable(path):
        if path.name == "denied":
            raise PermissionError("cannot read")
        yield from original(path)

    monkeypatch.setattr(policy.files, "iter_bytes", unreadable)
    page = grep_content("not-found", policy=policy, max_file_bytes=10)
    assert page.skipped_files == {"binary": 1, "encoding": 1, "size_limit": 1, "unreadable": 1}
    assert page.truncated and not page.has_more and "没有匹配" not in page.note


@pytest.mark.parametrize("kind", ["grep", "glob"])
def test_timeout_has_resumable_position(tmp_path, policy, monkeypatch, kind):
    (tmp_path / "note.txt").write_bytes(b"hit\nhit\n")
    ticks = iter([0.0, 2.0])
    monkeypatch.setattr(search_tools.time, "monotonic", lambda: next(ticks))
    tool = grep_content if kind == "grep" else glob_files
    pattern = "hit" if kind == "grep" else "*.txt"
    page = tool(pattern, policy=policy, timeout_seconds=1)
    assert page.has_more and page.next_cursor
    resumed = tool(pattern, policy=policy, cursor=page.next_cursor, timeout_seconds=None)
    assert not resumed.has_more


def test_readonly_paging_creates_no_spill_files(tmp_path, policy):
    (tmp_path / "note.txt").write_bytes(b"hit\nhit\n")
    policy.external_policy = ExternalPathPolicy(mode="read-only")
    first = grep_content("hit", policy=policy, max_matches=1)
    second = grep_content("hit", policy=policy, cursor=first.next_cursor)
    assert second.total_matches == 1 and not (tmp_path / ".local-agent").exists()
    with pytest.raises(SecurityError):
        grep_content("hit", policy=policy, save_full=True)
    with pytest.raises(SecurityError):
        read_file_content(".env", policy=policy)


def test_stream_chunk_bound_and_cancel(tmp_path, policy):
    (tmp_path / "note.txt").write_bytes(b"x" * 200000)
    parts = list(text_parts(tmp_path / "note.txt", policy, "utf-8"))
    assert max(len(text) for text, _ in parts) <= 65536
    lines = list(text_lines(tmp_path / "note.txt", policy, "utf-8", 10))
    assert lines == [(1, "x" * 10, True)]
    token = policy.cancellation = CancellationToken()
    stream = policy.files.iter_bytes("note.txt")
    next(stream)
    token.cancel("stop")
    with pytest.raises(SecurityError):
        next(stream)


def test_model_tools_and_output_expose_usable_continuation(tmp_path, policy):
    (tmp_path / "note.txt").write_bytes(b"x" * 20)
    tools = {tool.name: tool for tool in build_file_tools(policy) + build_search_tools(policy)}
    first = json.loads(tools["read_file"].invoke({"path": "note.txt", "max_chars": 10}))
    projected = project_file_tool_output(first, tool_name="read_file", threshold_chars=0,
                                         head_chars=100, tail_chars=100)
    assert projected["next_start_char"] == 10 and "next_start_char" in projected["note"]
    second = json.loads(tools["read_file"].invoke({"path": "note.txt", "start_char": 10,
                                                 "expected_sha256": first["sha256"]}))
    assert first["content"] + second["content"] == "x" * 20
    assert {"cursor", "max_scan_files", "max_file_bytes", "max_line_chars", "timeout_seconds", "encoding"} <= tools["grep"].args.keys()
    assert {"cursor", "timeout_seconds"} <= tools["glob"].args.keys()


def test_pressure_clipping_cannot_offer_cursor_past_unseen_text(tmp_path, policy):
    (tmp_path / "long.txt").write_bytes(b"x" * 2000)
    page = read_file_content("long.txt", policy=policy, max_chars=1000)
    projected = project_file_tool_output(page.model_dump(), tool_name="read_file",
                                         threshold_chars=200, head_chars=80, tail_chars=80)
    assert projected["next_start_char"] is None and projected["has_more"]
    assert projected["partial_lines"] == [1]


def test_decoder_and_crlf_work_with_arbitrary_small_byte_chunks(tmp_path, policy, monkeypatch):
    text = "中文\r\n甲\r乙\n\n尾巴"
    data = text.encode("utf-8-sig")
    path = tmp_path / "small.txt"
    path.write_bytes(data)

    def small_chunks(target):
        for i in range(0, len(data), 5):
            yield data[i:i + 5]

    monkeypatch.setattr(policy.files, "iter_bytes", small_chunks)
    result = read_file_content(path.name, policy=policy, start_line=2)
    assert result.content == "甲\r乙\n\n尾巴" and result.line_count == 5
