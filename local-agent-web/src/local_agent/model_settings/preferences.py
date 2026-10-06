"""非秘密应用偏好；运行参数在提交时复制进 Checkpoint。"""

from __future__ import annotations

import sqlite3
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from local_agent.config import AppConfig, EmbeddingConfig
from local_agent.exceptions import ConnectionValidationError
from local_agent.model_settings.models import _validate_base_url, transport_of


class _Preference(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GeneralPreferences(_Preference):
    theme: Literal["light", "dark", "system"] = "light"
    font_size: Literal["small", "standard", "large"] = "standard"
    animations: bool = True
    send_shortcut: Literal["enter", "ctrl_enter"] = "enter"
    reasoning_expanded: bool = False


class ExecutionPreferences(_Preference):
    max_tool_loops: int = Field(gt=0)
    max_retries_per_step: int = Field(ge=0)
    shell_timeout_seconds: int = Field(gt=0)
    shell_max_output_chars: int = Field(gt=0)


class RetrievalPreferences(_Preference):
    enabled: bool
    max_files: int = Field(gt=0)
    max_results: int = Field(gt=0)
    timeout_seconds: int = Field(gt=0)
    context_lines: int = Field(ge=0)


class LongTermPreferences(_Preference):
    enabled: bool
    confidence_threshold: float = Field(ge=0, le=1)
    importance_threshold: float = Field(ge=0, le=1)
    aging_enabled: bool
    forget_after_days: int = Field(gt=0)


class MediumTermPreferences(_Preference):
    enabled: bool
    top_k: int = Field(gt=0)
    min_score: float = Field(ge=0, le=1)
    decay_half_life_days: int = Field(gt=0)


class EmbeddingPreferences(EmbeddingConfig):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    api_key_env: str = Field(default="", pattern=r"^(?:[A-Za-z_][A-Za-z0-9_]*|)$")
    allow_plaintext_http: bool = False

    @model_validator(mode="after")
    def check_endpoint(self) -> EmbeddingPreferences:
        if self.provider == "openai_compatible" and self.base_url:
            try:
                _validate_base_url(self.base_url, allow_plaintext_http=self.allow_plaintext_http)
            except ConnectionValidationError as exc:
                raise ValueError(str(exc)) from exc
        return self

    def as_config(self) -> EmbeddingConfig:
        return EmbeddingConfig.model_validate(self.model_dump(exclude={"allow_plaintext_http"}))


class RuntimePreferences(_Preference):
    execution: ExecutionPreferences
    retrieval: RetrievalPreferences
    memory: LongTermPreferences
    # Missing fields in old settings/checkpoints keep the startup configuration.
    medium_term: MediumTermPreferences | None = None
    embedding: EmbeddingPreferences | None = None


class PreferenceValues(RuntimePreferences):
    general: GeneralPreferences = Field(default_factory=GeneralPreferences)


class RuntimePreferenceBinding(_Preference):
    version: int = Field(ge=0)
    values: RuntimePreferences


class PreferencesView(_Preference):
    version: int
    writable: bool
    values: PreferenceValues
    defaults: PreferenceValues
    advanced: dict[str, int | float | bool]
    resources: MemoryResourcesView | None = None


class MemoryResourcesView(_Preference):
    backend: str
    path: str
    collection: str
    vector_available: bool
    vector_initialized: bool
    embedding_state: Literal["disabled", "unavailable", "not_ready", "ready"]
    embedding_detail: str
    key_configured: bool
    restart_required: bool = False


class MemoryProbeRequest(_Preference):
    expected_version: int = Field(ge=0)
    values: PreferenceValues


class MemoryProbeResult(_Preference):
    state: Literal["passed", "disabled", "not_ready", "incompatible", "failed"]
    detail: str
    dimension: int | None = None
    records: int | None = None
    error_category: Literal[
        "restart_required", "timeout", "network", "authentication", "request",
        "model_or_endpoint", "rate_limit", "provider", "local_load", "unknown",
    ] | None = None
    http_status: int | None = Field(default=None, ge=400, le=599)


class PreferencesRequest(_Preference):
    expected_version: int = Field(ge=0)
    values: PreferenceValues


def initial_preferences(config: AppConfig) -> PreferenceValues:
    """只从实际加载的配置取初值，不重复维护 YAML 默认常量。"""
    return PreferenceValues(
        execution=ExecutionPreferences(
            max_tool_loops=config.agent.max_tool_loops,
            max_retries_per_step=config.agent.max_retries_per_step,
            shell_timeout_seconds=config.security.shell.default_timeout_seconds,
            shell_max_output_chars=config.security.shell.max_output_chars,
        ),
        retrieval=RetrievalPreferences(**config.rag.model_dump(include={
            "enabled", "max_files", "max_results", "timeout_seconds", "context_lines",
        })),
        memory=LongTermPreferences(**config.memory.long_term.model_dump(include={
            "enabled", "confidence_threshold", "importance_threshold", "aging_enabled",
            "forget_after_days",
        })),
        medium_term=MediumTermPreferences(**config.memory.medium_term.model_dump(include={
            "enabled", "top_k", "min_score", "decay_half_life_days",
        })),
        embedding=EmbeddingPreferences(
            **config.embedding.model_dump(),
            allow_plaintext_http=(
                config.embedding.provider == "openai_compatible" and
                bool(config.embedding.base_url) and
                transport_of(config.embedding.base_url) == "plaintext-http"
            ),
        ),
    )


def complete_preferences(values: PreferenceValues, config: AppConfig) -> PreferenceValues:
    defaults = initial_preferences(config)
    return values.model_copy(update={
        "medium_term": values.medium_term or defaults.medium_term,
        "embedding": values.embedding or defaults.embedding,
    })


def apply_runtime_preferences(config: AppConfig, values: RuntimePreferences) -> AppConfig:
    """创建独立的配置；不修改进程初值，也不触及压缩与模型参数。"""
    execution = values.execution
    shell = config.security.shell.model_copy(update={
        "default_timeout_seconds": execution.shell_timeout_seconds,
        "max_output_chars": execution.shell_max_output_chars,
    })
    embedding = config.embedding if values.embedding is None else values.embedding.as_config()
    medium = config.memory.medium_term if values.medium_term is None else (
        config.memory.medium_term.model_copy(update=values.medium_term.model_dump())
    )
    if medium.enabled and not embedding.enabled:
        raise ValueError("启用中期记忆前，请先启用并配置 Embedding。")
    return config.model_copy(update={
        "embedding": embedding,
        "agent": config.agent.model_copy(update={
            "max_tool_loops": execution.max_tool_loops,
            "max_retries_per_step": execution.max_retries_per_step,
        }),
        "security": config.security.model_copy(update={"shell": shell}),
        "rag": config.rag.model_copy(update=values.retrieval.model_dump()),
        "memory": config.memory.model_copy(update={
            "medium_term": medium,
            "long_term": config.memory.long_term.model_copy(update=values.memory.model_dump()),
        }),
    })


def advanced_preferences(config: AppConfig) -> dict[str, int | float | bool]:
    """显式白名单，禁止向页面倾倒配置（含密钥、路径、命令规则）。"""
    c = config.memory.compression
    return {
        "compression_enabled": c.enabled,
        "compression_trigger_ratio": c.trigger_ratio,
        "preserve_recent_turns": c.preserve_recent_turns,
        "preserve_recent_tool_steps": c.preserve_recent_tool_steps,
        "tool_step_budget_chars": c.tool_step_budget_chars,
        "tool_result_keep_chars": c.tool_result_keep_chars,
        "summary_max_chars": c.summary_max_chars,
        "short_term_enabled": config.memory.short_term.enabled,
        "agents_enabled": config.agents.enabled,
        "agents_max_file_chars": config.agents.max_file_chars,
        "agents_max_total_chars": config.agents.max_total_chars,
    }


class PreferenceConflict(Exception):
    """拒绝基于旧版本覆盖已保存的设置。"""


class PreferenceStore:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def read(self) -> tuple[int, PreferenceValues] | None:
        row = self.connection.execute(
            "SELECT version, values_json FROM app_preferences WHERE id=1"
        ).fetchone()
        return None if row is None else (int(row[0]), PreferenceValues.model_validate_json(row[1]))

    def write(self, values: PreferenceValues, *, expected_version: int) -> int:
        with self.connection:
            if expected_version == 0:
                try:
                    self.connection.execute(
                        "INSERT INTO app_preferences(id, version, values_json) VALUES(1, 1, ?)",
                        (values.model_dump_json(),),
                    )
                except sqlite3.IntegrityError as exc:
                    raise PreferenceConflict from exc
            else:
                cursor = self.connection.execute(
                    "UPDATE app_preferences SET version=version+1, values_json=? "
                    "WHERE id=1 AND version=?",
                    (values.model_dump_json(), expected_version),
                )
                if cursor.rowcount != 1:
                    raise PreferenceConflict
        return expected_version + 1

    def binding(self, config: AppConfig) -> RuntimePreferenceBinding:
        saved = self.read()
        version, values = saved if saved is not None else (0, initial_preferences(config))
        values = complete_preferences(values, config)
        return RuntimePreferenceBinding(
            version=version,
            values=RuntimePreferences.model_validate(values.model_dump(exclude={"general"})),
        )
