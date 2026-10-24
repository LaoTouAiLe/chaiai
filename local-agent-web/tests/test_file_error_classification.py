"""F4：文件参数/状态错误与真实策略拒绝分开，失败不得改写文件。"""

import hashlib

import pytest
from langchain_core.messages import AIMessage
from test_t43_main_loop import LoopModel, _call, _tools

from local_agent.exceptions import FileOperationError
from local_agent.graph import builder, nodes
from local_agent.security.policy import SecurityPolicy
from local_agent.tools.tool_registry import ToolRegistry


@pytest.mark.parametrize("name,args,hint", [
    ("edit_file", {"old_text": "missing", "new_text": "x"}, "没有精确匹配"),
    ("edit_file", {"path": "repeat.txt", "old_text": "alpha", "new_text": "x"}, "匹配到 2 处"),
    ("edit_file", {"old_text": "a = 1", "new_text": "a = 1"}, "没有需要修改"),
    ("edit_file", {"old_text": "a = 1", "new_text": "a = 2", "expected_sha256": "0" * 64}, "重新读取"),
    ("edit_file", {"path": "binary.bin", "old_text": "a", "new_text": "b"}, "二进制"),
    ("edit_file", {"path": "unknown.txt", "old_text": "a", "new_text": "b"}, "无法按utf-8-sig解码"),
    ("read_file", {"path": "missing.txt"}, "不存在"),
    ("read_file", {"path": "folder"}, "不是文件"),
    ("edit_file", {"path": "missing.txt", "old_text": "a", "new_text": "b"}, "不存在"),
    ("list_dir", {"path": "missing-folder"}, "不是目录或不存在"),
    ("write_file", {"content": "replacement"}, "文件已存在"),
    ("write_file", {"path": "folder", "content": "replacement"}, "目录"),
    ("edit_file", {"patch": "invalid patch"}, "patch"),
    ("edit_file", {"patch": "@@ -1 +1 @@\n-wrong\n+new\n"}, "不匹配"),
    ("edit_file", {}, "需要提供"),
])
def test_invalid_file_operations_are_errors_without_policy_denials(
    tmp_path, app_config, name, args, hint,
) -> None:
    (tmp_path / "note.txt").write_bytes(b"a = 1\nb = 2\n")
    (tmp_path / "repeat.txt").write_bytes(b"alpha\nbeta\nalpha\n")
    (tmp_path / "binary.bin").write_bytes(b"\x00\x01")
    (tmp_path / "unknown.txt").write_bytes(b"\xff\xfe")
    (tmp_path / "folder").mkdir()
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
    policy = SecurityPolicy.from_config(app_config, workspace_root=tmp_path, environ={})
    arguments = {"path": "note.txt", **args}
    if name == "edit_file" and "expected_sha256" not in arguments:
        arguments["expected_sha256"] = hashlib.sha256(before.get(arguments["path"], b"")).hexdigest()
    tool = ToolRegistry.build(policy).get(name)
    content, status = nodes._invoke_tool(tool, {
        "name": name, "id": "invalid", "args": arguments, "type": "tool_call",
    }, policy)
    assert status == "error", content
    assert hint in content
    assert "被安全策略拒绝" not in content
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()} == before


def test_invalid_patch_is_an_error_at_the_gate_and_can_recover(tmp_path, app_config) -> None:
    target = tmp_path / "note.txt"
    target.write_bytes(b"a = 1\nb = 2\n")
    sha = hashlib.sha256(target.read_bytes()).hexdigest()
    model = LoopModel(messages=iter([]), script=[
        _tools(_call("edit_file", "bad", path="note.txt", patch="invalid patch", expected_sha256=sha)),
        _tools(_call("edit_file", "good", path="note.txt", old_text="a = 1", new_text="a = 2",
                     expected_sha256=sha)),
        AIMessage(content="修正编辑参数后已成功。"),
    ])
    app_config.memory.long_term.enabled = False
    app_config.memory.medium_term.enabled = False
    context = builder.build_runtime_context(app_config=app_config, model_adapter=model,
                                             workspace_root=tmp_path, current_cwd=tmp_path)
    result = builder.build_chat_graph().invoke(
        {"user_input": "修改第一行", "session_mode": "agent"}, context=context,
    )
    assert [event.status for event in result["tool_events"] if event.tool_name != "task_control"] == ["error", "ok"]
    assert result["task_status"] == "completed"
    assert target.read_bytes() == b"a = 2\nb = 2\n"
    assert not result.get("pending_approval")
    assert all(error["stage"] != "safety_gate" for error in result["error_events"])


@pytest.mark.parametrize("mode,path", [
    ("read-only", "note.txt"), ("workspace-write", ".env"),
    ("workspace-write", ".git/config"), ("workspace-write", "../outside.txt"),
])
def test_real_file_policy_refusals_remain_denied(tmp_path, app_config, mode, path) -> None:
    app_config.workspace.external_paths.mode = mode
    policy = SecurityPolicy.from_config(app_config, workspace_root=tmp_path, environ={})
    tool = ToolRegistry.build(policy).get("edit_file")
    content, status = nodes._invoke_tool(tool, {
        "name": "edit_file", "id": "blocked", "type": "tool_call",
        "args": {"path": path, "old_text": "a", "new_text": "b", "expected_sha256": "0" * 64},
    }, policy)
    assert status == "denied"
    assert "被安全策略拒绝" in content


def test_file_operation_error_details_are_redacted(tmp_path, app_config) -> None:
    class BrokenTool:
        def invoke(self, _call):
            raise FileOperationError("文件内容含 sk-file-operation-secret")

    policy = SecurityPolicy.from_config(
        app_config, workspace_root=tmp_path, environ={"DEEPSEEK_API_KEY": "sk-file-operation-secret"},
    )
    content, status = nodes._invoke_tool(BrokenTool(), {
        "name": "edit_file", "id": "secret", "args": {}, "type": "tool_call",
    }, policy)
    assert status == "error"
    assert "sk-file-operation-secret" not in content
