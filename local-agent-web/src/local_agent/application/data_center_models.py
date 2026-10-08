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
