"""F3：字节编码、流式分段和最终结果使用一致的文本。"""

import pytest

from local_agent.security.policy import SecurityPolicy
from local_agent.security.process_guard import ProcessOutcome
from local_agent.tools import output_decoding, shell_tools
from local_agent.tools.progress import shell_progress, use_tool_listener


@pytest.mark.parametrize("encoding", ["utf-8", "gbk"])
@pytest.mark.parametrize("chunk_size", [1, 2, 7, 4096])
def test_live_and_final_chinese_output_match(
    tmp_path, app_config, monkeypatch, encoding, chunk_size,
) -> None:
    monkeypatch.setattr(shell_tools.locale, "getpreferredencoding", lambda _: "gbk")
    config = app_config.model_copy(deep=True)
    config.workspace.external_paths.mode = "danger-full-access"
    policy = SecurityPolicy.from_config(config, workspace_root=tmp_path, environ={})
    texts = {"stdout": "第1秒：心跳正常\n录入完成\n", "stderr": "错误通道：中文正常\n"}
    raw = {name: text.encode(encoding) for name, text in texts.items()}

    def run(*args, on_output, **kwargs):
        for name, data in raw.items():
            for pos in range(0, len(data), chunk_size):
                on_output(name, data[pos:pos + chunk_size])
        return ProcessOutcome(exit_code=0, stdout=raw["stdout"], stderr=raw["stderr"])

    monkeypatch.setattr(policy.processes, "run", run)
    events = []
    with use_tool_listener(events.append), shell_progress("run", "call", {}):
        result = shell_tools.run_shell_command("python --version", policy=policy)

    assert result.stdout == texts["stdout"]
    assert result.stderr == texts["stderr"]
    for name, expected in texts.items():
        assert "".join(event.text for event in events if event.stream == name) == expected


@pytest.mark.parametrize("chunk_size", [1, 5, 4096, 65536])
def test_decoder_handles_bom_line_encoding_changes_and_long_lines(chunk_size) -> None:
    raw = (b"\xef\xbb\xbf" + "UTF8 中文\r\n".encode()
           + "录入完成\r".encode("gbk") + ("长行中" * 2000 + "\n").encode())
    decoder = output_decoding.OutputDecoder("gbk")
    parts = [decoder.feed(raw[pos:pos + chunk_size]) for pos in range(0, len(raw), chunk_size)]
    assert "".join(parts) + decoder.finish() == "UTF8 中文\r\n录入完成\r" + "长行中" * 2000 + "\n"


def test_non_newline_output_flushes_on_idle_and_keeps_partial_character(monkeypatch) -> None:
    clock = [0.0]
    monkeypatch.setattr(output_decoding.time, "monotonic", lambda: clock[0])
    decoder = output_decoding.OutputDecoder("gbk")
    assert decoder.feed("中文进度 ".encode()) == ""
    clock[0] = 0.3
    assert decoder.flush_idle() == "中文进度 "
    raw = "继续".encode()
    assert decoder.feed(raw[:2]) == ""
    assert decoder.feed(raw[2:] + b"\n") == "继续\n"
    assert decoder.finish() == ""


def test_idle_flush_does_not_replace_an_incomplete_utf8_character(monkeypatch) -> None:
    clock = [0.0]
    monkeypatch.setattr(output_decoding.time, "monotonic", lambda: clock[0])
    decoder = output_decoding.OutputDecoder("gbk")
    raw = "中".encode()
    assert decoder.feed(raw[:2]) == ""
    clock[0] = 0.3
    assert decoder.flush_idle() == ""
    assert decoder.feed(raw[2:]) == "中"
    assert decoder.finish() == ""


def test_ascii_partial_lines_are_immediately_available() -> None:
    decoder = output_decoding.OutputDecoder("gbk")
    assert decoder.feed(b"progress: ") == "progress: "
    assert decoder.feed("中文\n".encode()) == "中文\n"
    assert decoder.finish() == ""


def test_ambiguous_bytes_prefer_utf8_and_invalid_bytes_do_not_crash() -> None:
    decoder = output_decoding.OutputDecoder("gbk")
    ambiguous = "录".encode("gbk")  # 同时是合法 UTF-8，无法无损猜出编码。
    assert decoder.feed(ambiguous + b"\n") == ambiguous.decode("utf-8") + "\n"
    assert decoder.feed(b"\xff\n") == "�\n"
    assert decoder.finish() == ""


def test_truncated_result_counts_characters_and_preserves_cancelled_tail(
    tmp_path, app_config, monkeypatch,
) -> None:
    monkeypatch.setattr(shell_tools.locale, "getpreferredencoding", lambda _: "gbk")
    config = app_config.model_copy(deep=True)
    config.workspace.external_paths.mode = "danger-full-access"
    config.security.shell.max_output_chars = 5
    policy = SecurityPolicy.from_config(config, workspace_root=tmp_path, environ={})
    raw = "中文输出超过限制".encode()

    def run(*args, on_output, **kwargs):
        on_output("stdout", raw)
        return ProcessOutcome(exit_code=-1, stdout=raw, stderr=b"", cancelled=True)

    monkeypatch.setattr(policy.processes, "run", run)
    result = shell_tools.run_shell_command("python --version", policy=policy)
    assert result.stdout == "中文输出超"
    assert result.truncated
    assert "已被取消" in result.note
