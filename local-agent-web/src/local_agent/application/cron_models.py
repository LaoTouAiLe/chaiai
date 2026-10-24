"""定时任务的传输视图（§21.5，2026-10-22 J2）。"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from local_agent.application.data_center_models import DataView


class CronJobCreateRequest(DataView):
    """新建任务。**没有** `next_run_at` 字段：排期只由服务端算（§21.5）。"""

    name: str = Field(min_length=1, max_length=80)
    prompt: str = Field(min_length=1, max_length=4000)
    schedule: str = Field(min_length=1, max_length=120)
    workspace_id: str | None = Field(default=None, max_length=128)
    mode: Literal["chat", "agent"] = "agent"


class CronJobUpdateRequest(DataView):
    """局部更新：只传要改的字段，未传的保持原值。"""

    name: str | None = Field(default=None, min_length=1, max_length=80)
    prompt: str | None = Field(default=None, min_length=1, max_length=4000)
    schedule: str | None = Field(default=None, min_length=1, max_length=120)
    mode: Literal["chat", "agent"] | None = None
    enabled: bool | None = None


class CronJobView(DataView):
    job_id: str
    name: str
    prompt: str
    schedule: str
    workspace_root: str
    mode: str
    enabled: bool
    #: 只由服务端计算；客户端传了也会被忽略（§21.5）。
    next_run_at: str | None = None
    last_run_at: str | None = None
    last_status: str | None = None
    last_error: str | None = None


class CronRunView(DataView):
    run_id: str
    job_id: str
    #: 关联到那次运行的会话；页面据此"点进去看"。会话被清理后这里仍是空而不是报错。
    session_id: str | None = None
    status: str
    started_at: str
    finished_at: str | None = None
    summary: str | None = None


class CronJobsView(DataView):
    jobs: list[CronJobView]
    #: 调度线程是否在跑——没有它，任务永远不会触发，页面必须能显示这个事实。
    scheduler_running: bool = False


class CronRunsPageView(DataView):
    runs: list[CronRunView]
    total: int = Field(ge=0)
    offset: int = Field(ge=0)
    limit: int = Field(gt=0)
