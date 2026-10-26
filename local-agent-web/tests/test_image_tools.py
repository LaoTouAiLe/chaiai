"""看图工具（`view_image`，2026-10-16）与它进模型的那条管道。

分两段：工具层（格式/上限/路径安全/失败闭合）与管道层（内容块进 `ToolMessage`，预览与指纹只取文本）。
"""

from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

from local_agent.config import SecurityConfig, WorkspaceConfig
from local_agent.exceptions import SecurityError
from local_agent.security.path_guard import ExternalPathPolicy
from local_agent.security.policy import READ_ONLY_TOOLS, SecurityPolicy
from local_agent.tools.image_tools import (
    MAX_IMAGE_BYTES,
    build_image_tools,
    read_image,
)


def _png_head(width: int, height: int) -> bytes:
    """一个**真实形状**的 PNG 头（IHDR 前 8 字节宽高）：尺寸检查读的就是这里。"""
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", width, height)
        + b"\x08\x02\x00\x00\x00"
    )


#: **只验魔数与尺寸，不构造完整 PNG**：本工具认的就是文件头（扩展名一律不信），所以这串够用。
PNG_HEAD_BYTES = _png_head(8, 8)


def _policy(tmp_path: Path, mode: str = "workspace-write") -> SecurityPolicy:
    return SecurityPolicy(
        workspace_root=tmp_path,
        security=SecurityConfig(),
        workspace=WorkspaceConfig(),
        external_policy=ExternalPathPolicy(mode=mode),  # type: ignore[arg-type]
    )


def _tools(policy: SecurityPolicy, *, vision: bool) -> dict[str, object]:
    return {tool.name: tool for tool in build_image_tools(policy, vision_available=vision)}


# ---- 1/2. 工具形状与"只读不需要审批" ----------------------------------------


def test_view_image_is_registered_as_a_read_only_tool(tmp_path: Path) -> None:
    """只读工具：**不需要人工批准**（与 read_file 同级），且名字就是 `view_image`。

    它必须登记在 `READ_ONLY_TOOLS` 里——`KNOWN_TOOLS` 是"可绑定给模型"的白名单，漏登记会被安全门
    直接拒（"未列入放行清单的工具一律拒绝"）。
    """
    assert "view_image" in READ_ONLY_TOOLS
    assert list(_tools(_policy(tmp_path), vision=True)) == ["view_image"]

    decision = _policy(tmp_path).classify_tool("view_image", {"path": "shot.png"})
    assert decision.allowed is True and decision.requires_approval is False


def test_view_image_refuses_without_reading_when_the_model_cannot_see(tmp_path: Path) -> None:
    """连接没声明支持图像时**失败闭合**：给可照做的说明，而且**不读文件**。

    不读是关键：读了也递不出去，只会让用户以为"看到了"。用不存在的路径做判据——真去读就会报
    "文件不存在"，而我们要看到的是那句"没有声明支持图像输入"。
    """
    tools = _tools(_policy(tmp_path), vision=False)

    out = tools["view_image"].invoke({"path": "not-there.png"})  # type: ignore[attr-defined]

    assert isinstance(out, str)
    assert "没有声明支持图像输入" in out
    assert "supports_vision" in out, "要给出可照做的下一步"
    assert "snapshot" in out, "要给出文本替代手段（没图也能自检）"
    assert "不存在" not in out, "不该走到读文件那一步"


# ---- 3. 读图：格式、上限、路径安全 ------------------------------------------


def test_reads_a_supported_image_into_a_data_url(tmp_path: Path) -> None:
    image = tmp_path / "hero.png"
    image.write_bytes(PNG_HEAD_BYTES)

    read = read_image("hero.png", policy=_policy(tmp_path))

    assert read.media_type == "image/png"
    assert read.byte_count == len(PNG_HEAD_BYTES)
    assert read.data_url.startswith("data:image/png;base64,")
    assert read.path == "hero.png"


@pytest.mark.parametrize(
    ("head", "expected"),
    [
        (b"\xff\xd8\xff\xe0junk", "image/jpeg"),
        # GIF 头带真实宽高（8×8）：尺寸检查会读它，随便填字符会被判成"超大图"。
        (b"GIF89a" + struct.pack("<HH", 8, 8), "image/gif"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "image/webp"),
    ],
)
def test_recognises_supported_formats_by_magic_bytes(tmp_path: Path, head: bytes, expected: str) -> None:
    """按**魔数**认格式——扩展名一律不信（`hero.png` 完全可能是个 HTML 错误页）。"""
    (tmp_path / "whatever.bin").write_bytes(head)

    assert read_image("whatever.bin", policy=_policy(tmp_path)).media_type == expected


def test_refuses_a_file_that_is_not_an_image(tmp_path: Path) -> None:
    # 典型现场：站点返回的错误页被存成了 .png。
    (tmp_path / "hero.png").write_text("<html>404 Not Found</html>", encoding="utf-8")

    with pytest.raises(ValueError, match="不是受支持的图片"):
        read_image("hero.png", policy=_policy(tmp_path))


def test_refuses_an_oversized_image_instead_of_truncating(tmp_path: Path) -> None:
    """超限**如实拒绝**，不截断——半个图交给模型只会得到错误的视觉结论。"""
    (tmp_path / "big.png").write_bytes(PNG_HEAD_BYTES + b"x" * 4096)

    with pytest.raises(ValueError, match="图片太大"):
        read_image("big.png", policy=_policy(tmp_path), max_bytes=64)

    assert MAX_IMAGE_BYTES is None, "默认不设大小配额；显式调用参数仍可限制"


def test_refuses_a_sensitive_file(tmp_path: Path) -> None:
    """疑似密钥的文件一律拒（与 read_file 同一条安全口径）。"""
    (tmp_path / "id_rsa").write_bytes(PNG_HEAD_BYTES)

    with pytest.raises(SecurityError):
        read_image("id_rsa", policy=_policy(tmp_path))


# ---- 4. 管道：内容块要进 ToolMessage，预览/指纹只取文本 ----------------------


def test_returns_content_blocks_with_the_image_attached(tmp_path: Path) -> None:
    (tmp_path / "hero.png").write_bytes(PNG_HEAD_BYTES)
    tools = _tools(_policy(tmp_path), vision=True)

    message = tools["view_image"].invoke({"path": "hero.png"})  # type: ignore[attr-defined]

    # 工具返回的是**带内容块的 ToolMessage**：LangChain 的 `_format_output` 对 ToolOutputMixin
    # 原样透传，执行节点再取 `.content` 重建带 tool_call_id 的那一条。
    assert message.type == "tool"
    blocks = message.content
    assert isinstance(blocks, list) and len(blocks) == 2
    assert blocks[0]["type"] == "text" and "hero.png" in blocks[0]["text"]
    assert blocks[1]["type"] == "image_url"
    assert blocks[1]["image_url"]["url"].startswith("data:image/png;base64,")


@pytest.mark.parametrize(
    ("data", "size"),
    [
        (_png_head(8, 8), (8, 8)),
        (b"GIF89a" + struct.pack("<HH", 320, 200) + b"\x00\x00\x00", (320, 200)),
        (
            b"\xff\xd8"
            + b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
            + b"\xff\xc0" + struct.pack(">H", 17) + b"\x08" + struct.pack(">HH", 64, 48) + b"\x03"
            + b"\x01\x11\x00\x02\x11\x01\x03\x11\x01",
            (48, 64),
        ),
        (
            b"RIFF" + struct.pack("<I", 30) + b"WEBP" + b"VP8X"
            + b"\x0a\x00\x00\x00" + b"\x00" + b"\x00\x00\x00"
            + (1599).to_bytes(3, "little") + (899).to_bytes(3, "little"),
            (1600, 900),
        ),
    ],
)
def test_image_dimensions_read_the_header(data: bytes, size: tuple[int, int]) -> None:
    """四种受支持格式都要能取到尺寸：超限拦截依赖它（不依赖 Pillow）。"""
    from local_agent.media import image_dimensions

    assert image_dimensions(data) == size


def test_image_over_the_edge_limit_is_refused_before_it_reaches_the_model(tmp_path: Path) -> None:
    """单边超过 8192px 的图**在工具层就被拒**（2026-10-20）。

    端点对单边有硬上限；放行它会让下一轮整包请求 400，模型只会看到"模型调用失败"，
    连是哪张图都查不出来。这里给出尺寸与可执行做法，模型可以换个方式（缩小后再看）。
    """
    (tmp_path / "huge.png").write_bytes(_png_head(4753, 12789))
    tools = _tools(_policy(tmp_path), vision=True)

    out = tools["view_image"].invoke({"path": "huge.png"})  # type: ignore[attr-defined]

    assert isinstance(out, str)
    assert "4753" in out and "12789" in out
    assert "8192" in out, "要说明上限是多少"
    assert "缩小" in out, "要给出可照做的下一步"


def test_split_image_blocks_moves_images_out_of_the_tool_message() -> None:
    """图片块必须从 ToolMessage 里拆出来（2026-10-20）。

    端点只允许 **user** 消息带图；tool 消息带图会让下一轮请求直接 400
    （`.messages[12].image[0]: unsupported image`）。纯文本 / 无图列表必须原样返回，默认路径不变。
    """
    from local_agent.graph import nodes

    text = {"type": "text", "text": "已读取图片 hero.png"}
    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}

    tool_content, images = nodes._split_image_blocks([text, image])

    assert tool_content == [text]
    assert images == [image]
    assert nodes._split_image_blocks("plain") == ("plain", [])
    assert nodes._split_image_blocks([text]) == ([text], [])


def test_node_keeps_the_blocks_but_builds_preview_and_digest_from_text(tmp_path: Path) -> None:
    """执行节点：回填给模型的是**内容块**，而预览/指纹/截断判定只用文本部分。

    这条守的是两个具体的坏结果：① 预览里塞进几百 KB 的 base64（事件记录被撑爆）；
    ② 指纹对 base64 取哈希（同一次看图得到毫无意义的摘要）。顺带钉住文本块仍然脱敏。
    """
    from local_agent.graph import nodes

    (tmp_path / "hero.png").write_bytes(PNG_HEAD_BYTES)
    policy = _policy(tmp_path)
    tool = _tools(policy, vision=True)["view_image"]

    call = {
        "name": "view_image",
        "id": "call-1",
        "type": "tool_call",
        "args": {"path": "hero.png"},
    }

    content, status = nodes._invoke_tool(tool, call, policy)  # type: ignore[arg-type]

    assert status == "ok"
    assert isinstance(content, list), "内容块必须原样保留"
    text = nodes._tool_output_text(content)
    assert "hero.png" in text
    assert "base64" not in text, "预览/指纹用的文本里不能出现 base64"
    assert "［图像内容］" in text, "非文本块只留占位说明"
    # 指纹取自文本：同样内容两次得到同一个摘要，且与 base64 无关。
    assert nodes._digest(text) == nodes._digest(nodes._tool_output_text(content))


def test_redaction_still_applies_to_the_text_block(tmp_path: Path) -> None:
    """内容块里的**文本块照样脱敏**（图像块原样）——多模态不能成为密钥回显的旁路。"""
    from local_agent.graph import nodes

    secret = "sk-should-be-redacted"

    class _RedactingPolicy:
        """只提供 `redact` 的桩：本用例验的是"内容块过不过脱敏"，不是脱敏本身怎么实现。"""

        def redact(self, text: str) -> str:
            return text.replace(secret, "***")

    policy = _RedactingPolicy()

    blocks = [
        {"type": "text", "text": f"token={secret}"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]
    redacted = nodes._redact_blocks(blocks, policy)

    assert secret not in json.dumps(redacted)
    assert redacted[1] == blocks[1], "图像块原样保留"
