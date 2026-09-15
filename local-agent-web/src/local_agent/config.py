"""配置加载与校验（开发文档 §8、§8.1）。

设计约定：

- 配置文件只存非秘密配置；API Key 由 ``llm.api_key_env`` 指定的环境变量提供，
  Key 的值放在项目根目录的 ``.env``（已被 .gitignore 排除）。
- 校验失败一律抛出 :class:`ConfigError`，消息面向用户且**不含密钥值**。
- 未列出的配置键会被拒绝（``extra="forbid"``），避免“写了但不生效”的静默错误。
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

DEFAULT_CONFIG_PATH = Path("config.yaml")
DEFAULT_ENV_PATH = Path(".env")


class ConfigError(Exception):
    """配置缺失、语法错误或校验失败；消息可直接展示给用户。"""


class _StrictModel(BaseModel):
    """拒绝未知字段，避免拼错的配置键被静默忽略。"""

    model_config = ConfigDict(extra="forbid")


class LlmConfig(_StrictModel):
    """OpenAI 兼容模型的连接参数。"""

    provider: Literal["openai_compatible"] = "openai_compatible"
    base_url: str = Field(min_length=1)
    api_key_env: str = Field(min_length=1)
    model: str = Field(min_length=1)
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    max_tokens: int = Field(default=4096, gt=0)
    timeout_seconds: int = Field(default=120, gt=0)
    retry_count: int = Field(default=2, ge=0, le=10)


class EmbeddingConfig(_StrictModel):
    """中期记忆使用的 embedding；v0.1a 保持关闭。"""

    enabled: bool = False
    provider: Literal["openai_compatible"] = "openai_compatible"
    base_url: str = ""
    api_key_env: str = ""
    model: str = ""

    @model_validator(mode="after")
    def _require_fields_when_enabled(self) -> EmbeddingConfig:
        if not self.enabled:
            return self
        missing = [
            name
            for name in ("base_url", "api_key_env", "model")
            if not getattr(self, name).strip()
        ]
        if missing:
            raise ValueError("embedding.enabled 为 true 时必须填写：" + "、".join(missing))
        return self


class WorkspaceConfig(_StrictModel):
    """工作区边界；相对路径以启动进程的当前目录为准。"""

    root: str = "."
    allow_external_paths: bool = False
    max_file_size_bytes: int = Field(default=2_000_000, gt=0)


class AgentsPrecedenceConfig(_StrictModel):
    """AGENTS 文件合并优先级（开发文档 §7.1）。"""

    closer_directory_wins: bool = True
    override_wins: bool = True


class AgentsConfig(_StrictModel):
    """AGENTS.md / AGENTS.override.md 加载配置。"""

    enabled: bool = True
    filenames: list[str] = Field(default_factory=lambda: ["AGENTS.md", "AGENTS.override.md"])
    max_file_chars: int = Field(default=20_000, gt=0)
    max_total_chars: int = Field(default=60_000, gt=0)
    precedence: AgentsPrecedenceConfig = Field(default_factory=AgentsPrecedenceConfig)


class ShortTermMemoryConfig(_StrictModel):
    """短期记忆：LangGraph Checkpoint（T03 启用）。"""

    enabled: bool = True
    backend: Literal["sqlite_checkpoint"] = "sqlite_checkpoint"
    path: str = "data/checkpoints.sqlite"
    preserve_checkpoint_messages: bool = True
    prompt_window_messages: int = Field(default=100, gt=0)


class MediumTermMemoryConfig(_StrictModel):
    """中期记忆：Chroma 向量库（T14 启用）。"""

    enabled: bool = False
    backend: Literal["chroma"] = "chroma"
    path: str = "data/chroma"
    collection: str = "agent_memory"
    top_k: int = Field(default=8, gt=0)
    min_score: float = Field(default=0.72, ge=0.0, le=1.0)
    decay_half_life_days: int = Field(default=30, gt=0)
    write_on: list[str] = Field(default_factory=list)


class LongTermMemoryConfig(_StrictModel):
    """长期记忆：SQLite 实体库（T15 启用）。"""

    enabled: bool = False
    backend: Literal["sqlite"] = "sqlite"
    path: str = "data/memories.sqlite"
    confidence_threshold: float = Field(default=0.75, ge=0.0, le=1.0)
    importance_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    aging_enabled: bool = True
    forget_after_days: int = Field(default=180, gt=0)


class CompressionConfig(_StrictModel):
    """Prompt 窗口压缩（T16 启用）。"""

    enabled: bool = False
    trigger_ratio: float = Field(default=0.8, gt=0.0, le=1.0)
    preserve_recent_turns: int = Field(default=8, ge=1)
    summary_max_chars: int = Field(default=12_000, gt=0)


class MemoryConfig(_StrictModel):
    """三层记忆与压缩的开关集合。"""

    short_term: ShortTermMemoryConfig = Field(default_factory=ShortTermMemoryConfig)
    medium_term: MediumTermMemoryConfig = Field(default_factory=MediumTermMemoryConfig)
    long_term: LongTermMemoryConfig = Field(default_factory=LongTermMemoryConfig)
    compression: CompressionConfig = Field(default_factory=CompressionConfig)


class RequireApprovalConfig(_StrictModel):
    """代码层强制审批项（开发文档 §8.1：四项必须始终为 true）。"""

    delete_file: bool = True
    destructive_shell: bool = True
    large_batch_edit: bool = True
    overwrite_existing_file: bool = True

    @model_validator(mode="after")
    def _must_all_be_enabled(self) -> RequireApprovalConfig:
        disabled = [name for name in type(self).model_fields if not getattr(self, name)]
        if disabled:
            raise ValueError(
                "以下审批项不能关闭（属于代码层强制拦截，不由配置放开）：" + "、".join(disabled)
            )
        return self


class LargeEditConfig(_StrictModel):
    """大范围编辑进入审批的阈值。"""

    max_files_without_approval: int = Field(default=3, ge=0)
    max_changed_lines_without_approval: int = Field(default=200, ge=0)


class ShellWhitelistEntry(_StrictModel):
    """结构化 Shell 白名单条目（T12 按平台、可执行名、参数结构与工作目录匹配）。"""

    platform: list[str] = Field(default_factory=list)
    executable: str = Field(min_length=1)
    args: list[str] = Field(default_factory=list)
    match: Literal["exact"] = "exact"
    #: 限定该条目只在指定的工作目录（相对 workspace.root）下免审；None 表示不限目录。
    cwd: str | None = None


#: ``security.shell.default_timeout_seconds`` 的**出厂默认值**。
#: 它只是配置的初始值：命令省略 ``timeout_seconds`` 时的有效默认值永远取配置值本身，
#: 因此把配置调小（例如 30 秒）会同时改变安全判定、动作指纹与工具执行采用的有效默认值；
#: 工具 Schema 本身不随配置变化（`timeout_seconds` 始终是"可省略/None"）。
DEFAULT_SHELL_TIMEOUT_SECONDS = 60


class ShellSecurityConfig(_StrictModel):
    """Shell 执行策略。"""

    platform: Literal["auto", "windows", "posix"] = "auto"
    #: 执行器偏好：``argv_preferred`` 能 argv 就 argv，遇到 Shell 语法时用平台默认适配器
    #: （Windows→cmd.exe、POSIX→/bin/sh）；``cmd`` / ``pwsh`` / ``sh`` 显式指定 Shell 语法
    #: 命令交给哪个适配器执行（架构 §6.6）。
    executor: Literal["argv_preferred", "cmd", "pwsh", "sh"] = "argv_preferred"
    default_timeout_seconds: int = Field(default=DEFAULT_SHELL_TIMEOUT_SECONDS, gt=0)
    max_output_chars: int = Field(default=30_000, gt=0)
    whitelist: list[ShellWhitelistEntry] = Field(default_factory=list)
    blocked_patterns: list[str] = Field(default_factory=list)


class SecretsConfig(_StrictModel):
    """密钥与敏感文件的处理策略。"""

    redact_environment_values: bool = True
    block_known_secret_files: bool = True


class SecurityConfig(_StrictModel):
    """路径、Shell、diff 与审批策略的配置入口。"""

    require_approval: RequireApprovalConfig = Field(default_factory=RequireApprovalConfig)
    large_edit: LargeEditConfig = Field(default_factory=LargeEditConfig)
    shell: ShellSecurityConfig = Field(default_factory=ShellSecurityConfig)
    secrets: SecretsConfig = Field(default_factory=SecretsConfig)


class RagConfig(_StrictModel):
    """可选代码检索（T21 启用）。"""

    enabled: bool = False
    backend: Literal["tree_sitter"] = "tree_sitter"
    index_path: str = "data/code_index"
    max_results: int = Field(default=8, gt=0)


class AgentConfig(_StrictModel):
    """Agent 循环与证据要求。"""

    max_tool_loops: int = Field(default=20, gt=0)
    max_retries_per_step: int = Field(default=2, ge=0)
    require_evidence_for_completion: bool = True


class AppConfig(_StrictModel):
    """全部配置的根对象。"""

    llm: LlmConfig
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    workspace: WorkspaceConfig = Field(default_factory=WorkspaceConfig)
    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    rag: RagConfig = Field(default_factory=RagConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)


def load_dotenv_file(env_path: Path = DEFAULT_ENV_PATH) -> bool:
    """加载 ``.env``；已存在的环境变量优先，不被文件覆盖。

    Returns:
        是否找到并加载了文件。
    """
    return load_dotenv(dotenv_path=env_path, override=False)


def load_config(config_path: Path = DEFAULT_CONFIG_PATH) -> AppConfig:
    """读取并校验配置文件。

    Args:
        config_path: 配置文件路径，默认 ``config.yaml``。

    Returns:
        校验通过的完整配置。

    Raises:
        ConfigError: 文件缺失、YAML 语法错误或字段校验失败。
    """
    display_path = config_path.as_posix()
    if not config_path.is_file():
        raise ConfigError(
            f"未找到配置文件 {display_path}（可复制 {config_path.name}.example 后按需修改）"
        )

    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"无法读取 {display_path}：{exc.strerror or exc}") from exc

    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{display_path} 不是合法 YAML：{exc}") from exc

    if raw is None:
        raise ConfigError(f"{display_path} 是空文件，至少需要 llm 段")
    if not isinstance(raw, Mapping):
        raise ConfigError(f"{display_path} 的顶层必须是 key: value 映射")

    try:
        return AppConfig.model_validate(dict(raw))
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(display_path, exc)) from exc


def resolve_api_key(config: LlmConfig, environ: Mapping[str, str] | None = None) -> str:
    """从环境变量取出 API Key。

    Args:
        config: ``llm`` 配置节，提供 ``api_key_env``。
        environ: 环境变量映射，默认取 ``os.environ``；调用方可注入以便测试。

    Returns:
        非空的 Key 值。

    Raises:
        ConfigError: 环境变量缺失或为空。错误消息只含变量名，不含 Key 值。
    """
    env = os.environ if environ is None else environ
    value = env.get(config.api_key_env, "").strip()
    if not value:
        raise ConfigError(
            f"环境变量 {config.api_key_env} 未设置或为空"
            f"（写进项目根目录的 .env，可参考 {DEFAULT_ENV_PATH.name}.example）"
        )
    return value


def redact_secrets(text: str, secrets: Mapping[str, str] | Sequence[str]) -> str:
    """把文本中的密钥值替换成 ``***``，用于任何可能回显给用户的错误消息。

    Args:
        text: 原始文本。
        secrets: 需要抹除的密钥值（映射取其值，序列直接取用）。

    Returns:
        抹除后的文本；空密钥会被忽略，避免把整段文本替换掉。
    """
    values = list(secrets.values()) if isinstance(secrets, Mapping) else list(secrets)
    for value in values:
        if value and value in text:
            text = text.replace(value, "***")
    return text


def _format_validation_error(display_path: str, exc: ValidationError) -> str:
    details: list[str] = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"])
        details.append(f"{location}: {error['msg']}")
    return f"{display_path} 配置校验失败：" + "；".join(details)
