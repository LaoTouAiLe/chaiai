"""看图（2026-10-16）：把本机图片作为**图像内容**交给模型。

为什么需要它：浏览器自检那条链路缺的最后一环不是"能不能截图"（`run_shell` + `playwright-cli`
够了），而是**模型能不能看见**——截了图却没有把图递给模型的通道。`view_image` 就是那条通道：
读一个本机图片文件，以**内容块**（``text`` + ``image_url``）的形式回填给模型。

四条边界（每条都有对应用例）：

1. **只读**：不改任何文件，登记在 ``security.policy.READ_ONLY_TOOLS``，不需要人工审批；
2. **走既有路径安全**：``policy.resolve_readable_file`` —— 相对路径以工作区为根、工作区外按档位与
   授权放行、敏感文件（疑似密钥）一律拒绝；
3. **按魔数认格式、有界**：不信扩展名（``hero.png`` 完全可能是个 HTML 错误页），只收
   PNG / JPEG / GIF / WebP；超过上限**如实拒绝而不截断**——半个图没有意义；
4. **模型收不了图就失败闭合**：``vision_available=False`` 时直接说明原因（连文件都不读），
   并给出**文本替代手段**——别让用户以为"读到了但看不到"。

结果形态说明（与 `graph.nodes._invoke_tool` 的约定）：本工具返回 ``ToolMessage``（含内容块），
LangChain 的 ``_format_output`` 会原样透传（``ToolOutputMixin`` 分支），执行节点只取它的
``.content`` 重建带 ``tool_call_id`` 的那一条。因此这里给的 ``tool_call_id`` 是占位值。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool
from langchain_core.tools import tool as langchain_tool

from local_agent.exceptions import SecurityError
from local_agent.media import (
    MAX_IMAGE_BYTES,
    MAX_IMAGE_EDGE,
    SUPPORTED_NOTE,
    data_url,
    detect_image_media_type,
    image_dimensions,
)
from local_agent.security.policy import SecurityPolicy

_UNSUPPORTED_NOTE = (
    "这条模型连接**没有声明支持图像输入**，所以我没有去读那张图（读了你也看不到，读它没有意义）。"
    "要打开它：在「设置 → 模型连接」里编辑**这条连接**，勾上「支持图片输入」，重启后端即可"
    "（2026-10-16 起这是**逐连接**的声明；`config.yaml` 的 `llm.supports_vision` 只剩旧单模型路径在用）。"
    "在那之前请用**文本手段**取事实：浏览器 CLI 的 `snapshot` / `text` / `styles` / `box` 输出、"
    "或页面量测（`scrollWidth` 与 `clientWidth`、`getBoundingClientRect`）——它们同样是可核对的证据。"
)


@dataclass(frozen=True)
class ImageRead:
    """一次成功的图片读取。"""

    #: 展示用路径（工作区内给相对路径）。
    path: str
    media_type: str
    byte_count: int
    #: ``data:image/png;base64,…`` —— 直接塞进内容块给模型。
    data_url: str


def _media_type(data: bytes) -> str | None:
    """兼容包装：判定与上限已提到 :mod:`local_agent.media`（上传与看图共用一份）。"""
    return detect_image_media_type(data)


def _display(resolved: Path, workspace_root: Path) -> str:
    """展示路径：在工作区内给相对路径，外部目标给绝对路径（与文件工具同一条口径）。"""
    try:
        return resolved.relative_to(workspace_root).as_posix()
    except ValueError:
        return resolved.as_posix()


def read_image(
    path: str, *, policy: SecurityPolicy, max_bytes: int | None = MAX_IMAGE_BYTES
) -> ImageRead:
    """读一张图片并编码成 data URL（有界、按魔数认格式）。

    Args:
        path: 路径（相对以工作区为根）；越界与敏感文件由策略拒绝。
        policy: 安全策略。
        max_bytes: 字节上限。

    Returns:
        展示路径、媒体类型、字节数与 data URL。

    Raises:
        SecurityError: 路径越界或命中敏感文件（来自 ``resolve_readable_file``）。
        FileOperationError: 目标不是文件或不存在。
        ValueError: 不是受支持的图片，或超过大小上限。
    """
    resolved = policy.resolve_readable_file(path)
    size = resolved.stat().st_size
    if max_bytes is not None and size > max_bytes:
        raise ValueError(
            f"图片太大（{size} 字节 > 上限 {max_bytes} 字节）：先压缩或裁剪，"
            "浏览器截图可以先 `playwright-cli resize 1440 900` 缩小视口再重截。"
        )
    data = resolved.read_bytes()
    if max_bytes is not None and len(data) > max_bytes:
        # 读的瞬间变大了：宁可拒绝，也不把一张不完整的图交给模型。
        raise ValueError(f"图片太大（{len(data)} 字节）：读取期间文件变大了，请重试或先压缩。")
    media_type = detect_image_media_type(data)
    if media_type is None:
        raise ValueError(
            f"这不是受支持的图片（按文件头判断）。{SUPPORTED_NOTE}；"
            "如果它是文本或 HTML，请改用 read_file。"
        )
    size = image_dimensions(data)
    if size is not None and max(size) > MAX_IMAGE_EDGE:
        # 端点对单边有硬上限（8192px）；超了整轮请求会被 400 拒绝，不如在这里就说明白。
        raise ValueError(
            f"这张图是 {size[0]}×{size[1]} 像素，单边超过端点上限 {MAX_IMAGE_EDGE}px："
            "直接交给模型会让整轮请求被拒绝。请先把它缩小（系统画图工具或缩图命令都行，"
            f"长边压到 {MAX_IMAGE_EDGE} 以内），或改看它的截图版本。"
        )
    return ImageRead(
        path=_display(resolved, policy.workspace_root),
        media_type=media_type,
        byte_count=len(data),
        data_url=data_url(media_type, data),
    )


def build_image_tools(policy: SecurityPolicy, *, vision_available: bool) -> list[BaseTool]:
    """构造看图工具。

    与 `git_tools` 同一口径：**工具总是被构造**（注册表的能力覆盖校验要求它在），
    "能不能用"由两处决定——能力层（`enabled_capabilities` 的 `vision_available`）与这里的失败闭合。

    Args:
        policy: 安全策略（路径与敏感文件判定都在它里面）。
        vision_available: 这条连接是否声明支持图像输入（``llm.supports_vision``）。
    """

    @langchain_tool("view_image")
    def view_image(path: str) -> str | ToolMessage:
        """看图：把本机的一张图片（PNG / JPEG / GIF / WebP）作为**图像**交给模型，你能直接看到它。

        什么时候用：截图之后（浏览器自检、UI 走查、看参考图、看图表）。
        **不要**用它读文本或 HTML（那些用 read_file），也不要在没有图片时要求"看一眼"。
        图像太大或格式不支持时会**如实拒绝**，不会给你一张看错的图。

        Args:
            path: 相对路径以当前工作区为根；显式绝对路径按「设置 → 权限」的档位与授权放行，
                敏感文件（疑似密钥）一律拒绝。
        """
        if not vision_available:
            return _UNSUPPORTED_NOTE
        try:
            image = read_image(path, policy=policy)
        except SecurityError as exc:
            return f"看图被拒绝：{exc}"
        except ValueError as exc:
            return f"看图失败：{exc}"
        except OSError as exc:  # pragma: no cover - 磁盘层异常
            return f"看图失败（{type(exc).__name__}）：{exc}"
        return ToolMessage(
            content=[
                {
                    "type": "text",
                    "text": (
                        f"已读取图片 {image.path}（{image.media_type}，{image.byte_count} 字节）。"
                        "图像内容会作为紧随其后的一条用户消息提供，直接看即可。"
                    ),
                },
                {"type": "image_url", "image_url": {"url": image.data_url}},
            ],
            # 占位 id：回填给模型的那一条由工具执行节点重建（它只取 `.content`）。
            tool_call_id="view_image",
            name="view_image",
        )

    return [view_image]
