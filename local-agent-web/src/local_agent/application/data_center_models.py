"""数据中台的公开只读视图；缺失计量保留 None。"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from local_agent.application.models import ModelCallRecordView

MemoryKind = Literal["medium", "fact", "conclusion"]


class DataView(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MemoryEntryView(DataView):
    id: str
    kind: MemoryKind
    title: str
    content: str
    scope: str
    status: str
    source: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    expires_at: str | None = None
    confidence: float | None = None
    importance: float | None = None
    truncated: bool = False


class MemoryResourceView(DataView):
    kind: Literal["medium", "long"]
    enabled: bool
    state: Literal["available", "missing", "unavailable"]
    note: str


class MemoryPageView(DataView):
    entries: list[MemoryEntryView]
    resources: list[MemoryResourceView]
    total: int = Field(ge=0)
    counts: dict[str, int]
    offset: int = Field(ge=0)
    limit: int = Field(gt=0)
    incomplete: bool = False
    notices: list[str] = Field(default_factory=list)


class UsageTotalsView(DataView):
    calls: int = 0
    failed_calls: int = 0
    missing_usage_calls: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    uncached_input_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_hit_ratio: float | None = None


class UsageBucketView(DataView):
    label: str
    totals: UsageTotalsView


class UsageCallView(DataView):
    session_id: str
    session_title: str
    workspace_id: str
    call: ModelCallRecordView


class UsagePageView(DataView):
    totals: UsageTotalsView
    daily: list[UsageBucketView]
    models: list[UsageBucketView]
    purposes: list[UsageBucketView]
    entries: list[UsageCallView]
    total: int = Field(ge=0)
    offset: int = Field(ge=0)
    limit: int = Field(gt=0)
    scanned_sessions: int = 0
    missing_record_sessions: int = 0
    incomplete: bool = False
    notices: list[str] = Field(default_factory=list)
    generated_at: datetime


ArtifactKind = Literal["image", "file", "link"]


class ArtifactView(DataView):
    """一条产物：图片、文件或链接（2026-10-22，§15.11.1）。"""

    artifact_id: str
    kind: ArtifactKind
    title: str
    #: 工作区内为相对路径；链接为完整 URL；附件为相对附件根的路径。
    location: str
    #: 工作区外路径为真：页面必须标注，不得伪装成工作区内文件。
    external: bool = False
    source: Literal["attachment", "file_change", "message"]
    #: 路径相对谁：`workspace` = 工作区根；`attachments` = 附件根（`data/attachments`）。
    #: 附件的路径是相对附件根的，不带上这个标记，预览会按工作区根解析而必然读不到。
    scope: Literal["workspace", "attachments"] = "workspace"
    session_id: str
    session_title: str
    #: 这条产物所属的工作区稳定标识。前端点开预览时必须带上它——否则会按"启动工作区"
    #: 解析路径，在别的会话工作区里必然读不到文件（2026-10-22 实机反馈的"点开没东西"）。
    workspace_id: str | None = None
    created_at: str | None = None


class ArtifactsPageView(DataView):
    entries: list[ArtifactView]
    total: int = Field(ge=0)
    offset: int = Field(ge=0)
    limit: int = Field(gt=0)
    scanned_sessions: int = 0
    incomplete: bool = False
    notices: list[str] = Field(default_factory=list)


class ArtifactPreviewView(DataView):
    """单条产物的内容预览（§15.11.3）。

    **只允许工作区内、且不超过上限**的文件：这是"看一眼"而不是"文件管理"，
    不提供写入、下载、任意路径读取。
    """

    kind: Literal["text", "image", "document", "spreadsheet", "too_large", "unreadable", "binary"]
    sheets: list[str] = Field(default_factory=list)
    sheet: str | None = None
    rows: list[list[str]] = Field(default_factory=list)
    #: 文本内容（`kind=text` 时）；这一页，最多 `PREVIEW_TEXT_LIMIT` 字符。
    text: str | None = None
    #: 图片的 data URL（`kind=image` 时）；不进缓存、不落盘。
    url: str | None = None
    size_bytes: int = 0
    #: **这一页之后还有内容**（下一页用 `offset` / `page` 取）。
    truncated: bool = False
    #: 本页在**整份文本里**的起始字符偏移（2026-11-03，分页用）。
    offset: int = 0
    #: 还有下一页；前端据此显示「加载更多」。
    has_more: bool = False
    #: 下一面的取法：`next_offset` / `next_page` 原样回传即可。两个都是 `None` = 没有下一页。
    #: 为什么要由服务端算：文档会在"同一文档页里继续切字符"和"换下一段文档页"之间切换，
    #: 让前端自己判断就得把后端的抽取窗口复制到浏览器里。
    next_offset: int | None = None
    next_page: int | None = None
    #: 文档的页码（1 起）与总页数；纯文本页为 `None`。
    page: int | None = None
    total_pages: int | None = None
    #: 文件本身超出可读前缀上限，**后面再也取不到**了（与 `has_more` 不是一回事）。
    beyond_limit: bool = False
    #: 二进制文件的扩展名（`kind=binary` 时，如 `docx`）。
    #:
    #: 为什么不解析：hermes 与 codex **都不做** docx（前者只认 code/html/svg，
    #: 后者是 TUI 根本不碰文件预览），抽纯文本又会丢掉全部格式。
    #: 明确告诉用户"不支持"比给一屏乱码或"能看但排版全乱"更诚实。
    suffix: str | None = None
