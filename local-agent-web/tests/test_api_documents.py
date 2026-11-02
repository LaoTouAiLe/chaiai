"""文档上传的端到端（2026-10-29）：HTTP 上传 → 提交消息 → **模型真的看到了路径**。

为什么单独一个文件：这里要拿到假模型实例才能断言"出站请求里到底有什么"，
而 `test_api.py` 的 `api` fixture 把实例藏在工厂调用里。自己建一套运行时更直接。
"""

from __future__ import annotations

import io
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import docx
import pytest
from fastapi.testclient import TestClient

from local_agent.api.app import create_app
from local_agent.api.container import ApiRuntime, open_runtime
from local_agent.config import AppConfig

LOCAL_BASE_URL = "http://127.0.0.1"


def _config(tmp_path: Path, app_config: AppConfig) -> AppConfig:
    """与 `test_api._config` 同一口径：临时工作区 + 临时短期记忆库。"""
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    return app_config.model_copy(
        update={
            "workspace": app_config.workspace.model_copy(update={"root": str(workspace)}),
            "memory": app_config.memory.model_copy(
                update={
                    "short_term": app_config.memory.short_term.model_copy(
                        update={
                            "enabled": True,
                            "path": str(tmp_path / "data" / "checkpoints.sqlite"),
                        }
                    )
                }
            ),
        }
    )


def _docx_bytes() -> bytes:
    buffer = io.BytesIO()
    document = docx.Document()
    document.add_paragraph("招新表")
    document.save(buffer)
    return buffer.getvalue()


@pytest.fixture
def live_api(
    tmp_path: Path, app_config: AppConfig, model_factory: Callable[..., Any]
) -> Iterator[tuple[TestClient, ApiRuntime, Any]]:
    """返回 (client, runtime, 假模型)：模型实例是断言"出站提示"的唯一入口。"""
    model = model_factory(decision="chat", reply="假回答")
    runtime = open_runtime(_config(tmp_path, app_config), base_dir=tmp_path, model=model,
                           static_dir=None)
    try:
        yield TestClient(create_app(runtime), base_url=LOCAL_BASE_URL), runtime, model
    finally:
        runtime.close()


def test_upload_multipart_stores_a_document(live_api) -> None:
    """multipart 上传走得通，返回的引用按魔数命名（不是按文件名）。"""
    client, _, _ = live_api
    session_id = client.post("/api/sessions", json={"title": "会话"}).json()["session_id"]

    response = client.post(
        f"/api/sessions/{session_id}/files",
        files={"file": ("报告.xls", _docx_bytes(), "application/octet-stream")},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["attachment_id"].endswith(".docx"), "扩展名必须按魔数改，不能信文件名"
    assert body["byte_count"] > 0


def test_upload_rejects_non_documents(live_api) -> None:
    client, _, _ = live_api
    session_id = client.post("/api/sessions", json={"title": "会话"}).json()["session_id"]

    response = client.post(
        f"/api/sessions/{session_id}/files",
        files={"file": ("图.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 32, "image/png")},
    )

    assert response.status_code == 422
    assert "受支持的文档" in response.text


def test_document_path_reaches_the_model(live_api) -> None:
    """完整链路：上传 → 提交 → **假模型收到的提示里出现绝对路径**。

    这一条是"没把握的地方"里最该验的：前面各段都有单测，但"服务端真的把路径
    交给模型了"只有把请求截下来才能确认。
    """
    client, _, model = live_api
    session_id = client.post("/api/sessions", json={"title": "会话"}).json()["session_id"]
    uploaded = client.post(
        f"/api/sessions/{session_id}/files",
        files={"file": ("报告.docx", _docx_bytes(), "application/octet-stream")},
    )
    assert uploaded.status_code == 200, uploaded.text
    attachment_id = uploaded.json()["attachment_id"]

    submitted = client.post(
        f"/api/sessions/{session_id}/messages",
        json={
            "text": "这份文件里写了什么",
            "files": [{"id": attachment_id, "name": "季度报告.docx"}],
            "client_request_id": "doc-1",
        },
    )
    assert submitted.status_code == 200, submitted.text

    prompts = "\n".join(getattr(model, "seen_prompts", []))
    assert "用户上传了以下文件" in prompts, "出站提示没有进模型请求"
    assert attachment_id.split("/")[-1] in prompts or "attachments" in prompts.lower()
    assert "read_file" in prompts
    # 展示名（用户上传时的名字）必须出现在提示里：模型靠它对上"用户说的那份文件"。
    assert "季度报告.docx" in prompts


def test_snapshot_carries_the_file_for_the_page(live_api) -> None:
    """页面要能画出附件块：快照里那条用户消息必须带 `files`（名字 + 体积，没有路径）。"""
    client, _, _ = live_api
    session_id = client.post("/api/sessions", json={"title": "会话"}).json()["session_id"]
    attachment_id = client.post(
        f"/api/sessions/{session_id}/files",
        files={"file": ("报告.docx", _docx_bytes(), "application/octet-stream")},
    ).json()["attachment_id"]

    client.post(
        f"/api/sessions/{session_id}/messages",
        json={
            "text": "看看",
            "files": [{"id": attachment_id, "name": "季度报告.docx"}],
            "client_request_id": "doc-2",
        },
    )
    snapshot = client.get(f"/api/sessions/{session_id}").json()

    users = [item for item in snapshot["messages"] if item["kind"] == "user"]
    assert users, "应当有一条用户消息"
    files = users[-1]["files"]
    assert len(files) == 1
    assert files[0]["attachment_id"] == attachment_id
    assert files[0]["size_bytes"] > 0
    # 展示名是用户上传时的名字，不是落盘的内容哈希（2026-10-29）。
    assert files[0]["name"] == "季度报告.docx"
    assert "path" not in files[0] and "\\" not in files[0]["name"], "页面不该拿到磁盘路径"


def test_display_name_is_sanitized(live_api) -> None:
    """客户端声明的文件名不可信：路径段与控制字符都要被剔掉，超长要截断。"""
    client, _, _ = live_api
    session_id = client.post("/api/sessions", json={"title": "会话"}).json()["session_id"]
    attachment_id = client.post(
        f"/api/sessions/{session_id}/files",
        files={"file": ("报告.docx", _docx_bytes(), "application/octet-stream")},
    ).json()["attachment_id"]

    client.post(
        f"/api/sessions/{session_id}/messages",
        json={
            "text": "看看",
            "files": [{"id": attachment_id, "name": "../../../etc/passwd\u0000.docx"}],
            "client_request_id": "doc-4",
        },
    )
    snapshot = client.get(f"/api/sessions/{session_id}").json()
    users = [item for item in snapshot["messages"] if item["kind"] == "user"]

    name = users[-1]["files"][0]["name"]
    assert "/" not in name and "\\" not in name
    assert "\u0000" not in name
    assert name.endswith(".docx")


def test_attachment_preview_returns_document_text(live_api) -> None:
    """点气泡里的文件块走的就是这个接口（2026-10-30）：`scope=attachments` + 文档提取。

    附件不在工作区里，所以必须用附件根解析；预览与 `read_file` 共用同一份提取实现。
    """
    client, _, _ = live_api
    session_id = client.post("/api/sessions", json={"title": "会话"}).json()["session_id"]
    attachment_id = client.post(
        f"/api/sessions/{session_id}/files",
        files={"file": ("报告.docx", _docx_bytes(), "application/octet-stream")},
    ).json()["attachment_id"]

    response = client.get(
        "/api/data-center/artifacts/preview",
        params={"path": attachment_id, "scope": "attachments"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["kind"] == "text", "文档附件要提取成文本，不能回 binary"
    assert "招新表" in (body["text"] or "")


def test_unknown_attachment_reference_is_rejected(live_api) -> None:
    """引用形状对但文件不存在：必须报错，不能静默当"没带文件"发出去。"""
    client, _, _ = live_api
    session_id = client.post("/api/sessions", json={"title": "会话"}).json()["session_id"]

    response = client.post(
        f"/api/sessions/{session_id}/messages",
        json={
            "text": "读一下",
            "files": [{"id": f"2026/10/29/{session_id}/0000000000000000.docx", "name": ""}],
            "client_request_id": "doc-3",
        },
    )

    assert response.status_code >= 400
