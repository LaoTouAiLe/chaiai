"""User terminal HTTP contracts; contains no runtime dependencies (§23)."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class TerminalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workspace_id: str = Field(min_length=1, max_length=200)


class TerminalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["approve", "reject"]
    cols: int = Field(default=80, ge=20, le=400, strict=True)
    rows: int = Field(default=24, ge=5, le=200, strict=True)


class TerminalView(BaseModel):
    approval_id: str
    terminal_id: str | None = None
    workspace_id: str
    cwd: str
    shell: str
    permission_mode: Literal["read-only", "workspace-write"]
    status: Literal["pending", "rejected", "expired", "running", "exited", "stopped", "failed"]
    expires_at: str
    exit_code: int | None = None
    detail: str | None = None


class TerminalList(BaseModel):
    available: bool
    reason: str | None = None
    items: list[TerminalView]
