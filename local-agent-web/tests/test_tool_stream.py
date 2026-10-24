"""命令开始、运行输出、断线重读与完成结果的展示边界。"""

from datetime import datetime, timezone

from local_agent.application.models import (
    SessionSnapshot,
    TaskRef,
    ToolMessageView,
    ToolUseView,
    WorkspaceRef,
)
from local_agent.application.tool_stream import ToolStream
from local_agent.tools.progress import ToolProgress


def _snapshot(**updates):
    snapshot = SessionSnapshot(
        snapshot_id="snapshot", session_id="session", title="test", session_state="running",
        current_task=TaskRef(run_id="run"), workspace=WorkspaceRef(workspace_id="ws", display_name="ws"),
        created_at=datetime.now(timezone.utc),
    )
    return snapshot.model_copy(update=updates)


def test_running_command_survives_snapshot_refresh_and_terminal_result_wins():
    events = []
    stream = ToolStream("session", events.extend, ())
    stream.notify(ToolProgress("run", "command", {"command": "python example.py", "cwd": "."}))
    stream.notify(ToolProgress("run", "command", stream="stdout", text="first line\n"))
    message = stream.merge(_snapshot()).messages[0]
    assert message.tool.status == "running"
    assert message.tool.outcome.stdout == "first line\n"
    assert [event.event_type for event in events] == ["tool_started", "tool_output"]
    completed = ToolMessageView(message_id=message.message_id, tool=ToolUseView(
        tool_call_id="command", tool_name="run_shell", status="ok", output_preview="final result",
    ))
    snapshot = stream.merge(_snapshot(messages=[completed]))
    assert snapshot.messages == [completed]
    assert not stream.merge(_snapshot()).messages


def test_stream_redacts_secrets_split_across_chunks_and_bounds_output():
    events = []
    secret = "credential with spaces"
    stream = ToolStream("session", events.extend, (secret,), limit=100)
    stream.notify(ToolProgress("run", "command", {"command": "python example.py"}))
    stream.notify(ToolProgress("run", "command", stream="stdout", text="ready " + secret[:12]))
    first = stream.merge(_snapshot()).messages[0].tool.outcome.stdout
    assert "credential" not in first
    stream.notify(ToolProgress("run", "command", stream="stdout",
                               text=secret[12:] + "\n" + "ordinary output\n" * 1000))
    message = stream.merge(_snapshot()).messages[0]
    assert secret not in message.tool.outcome.stdout
    assert len(message.tool.outcome.stdout) <= 100
    assert message.tool.outcome.truncated
    assert all(secret not in event.model_dump_json() for event in events)
    assert not stream.merge(_snapshot(current_task=TaskRef(run_id="other"))).messages


def test_short_lines_are_not_delayed_by_unrelated_long_credentials():
    stream = ToolStream("session", lambda _: None, ("long-credential-" + "x" * 100,))
    stream.notify(ToolProgress("run", "command", {"command": "python example.py"}))
    stream.notify(ToolProgress("run", "command", stream="stdout", text="1\n"))
    assert stream.merge(_snapshot()).messages[0].tool.outcome.stdout == "1\n"


def test_paths_survive_command_and_output_streams_with_split_secrets():
    events = []
    secret = "configured secret"
    path = "C:/Users/Some Name/demo.txt"
    stream = ToolStream("session", events.extend, (secret,))
    stream.notify(ToolProgress("run", "command", {"command": f"echo {path}", "cwd": path}))
    for piece in ("C:/Us", "ers/Some Name/demo.txt configured ", "secret\n"):
        stream.notify(ToolProgress("run", "command", stream="stdout", text=piece))
    tool = stream.merge(_snapshot()).messages[0].tool
    assert tool.redacted_arguments["command"] == f"echo {path}"
    assert tool.outcome.target == path
    assert tool.outcome.stdout == path + " ***\n"
    assert all(secret not in event.model_dump_json() for event in events)
