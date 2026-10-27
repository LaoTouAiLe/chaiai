"""非秘密应用偏好；运行参数在提交时复制进 Checkpoint。"""

from __future__ import annotations

import sqlite3
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from local_agent.config import (
    AppConfig,
    EmbeddingConfig,
    FetchProvider,
    WebAccessProvider,
    clean_domain_list,
)
from local_agent.exceptions import ConnectionValidationError
from local_agent.message_flags import DEFAULT_IMAGE_CONTEXT_TURNS
from local_agent.model_settings.models import _validate_base_url, transport_of


class _Preference(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GeneralPreferences(_Preference):
    #: 默认跟随系统外观（2026-10-22，页面规范 §12 决策 2）。前端 generalDefaults 同步。
    theme: Literal["light", "dark", "system"] = "system"
    font_size: Literal["small", "standard", "large"] = "standard"
    animations: bool = True
    send_shortcut: Literal["enter", "ctrl_enter"] = "enter"
    reasoning_expanded: bool = False


class ExecutionPreferences(_Preference):
    max_tool_loops: int | None = Field(default=None, gt=0)
    max_retries_per_step: int = Field(ge=0)
    shell_timeout_seconds: int | None = Field(default=None, gt=0)
    shell_max_output_chars: int = Field(gt=0)
    #: **安全删除**（2026-10-20）：`delete_file` 把目标移进回收站而不是直接抹掉。
    #: 只管文件工具；`run_shell` 里的删除命令不受它管辖（那条路径无法可靠拦截）。
    safe_delete: bool = True


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


class WebAccessPreferences(_Preference):
    """联网检索的运行期偏好（"设置 → 联网检索"页签）。

    只放**能安全回灌**的字段：``enabled`` 不在这里——它是**能力开关**的地盘（2026-10-14 起
    `web` 是锁定能力，运行期由 `apply_capability_gates` 回灌，见 ``WebAccessConfig`` 的说明）；
    ``tavily_api_key_env`` / ``deepseek_api_key_env`` 与 ``user_agent`` 也不在这里：前者们是旧配置的
    密钥**变量名**（密钥本身走凭据存储，见 ``model_settings.web_search``），后者不是用户要调的旋钮。

    上下界与 ``WebAccessConfig`` **逐项一致**：偏好值经 ``model_copy(update=...)`` 落到运行期配置，
    那条路径不做二次校验，所以写在模型上的约束就是最后一道。
    """

    provider: WebAccessProvider = "auto"
    #: 正文从哪来（2026-10-26）：``local`` 本机抓取 / ``deepseek`` 交给 DeepSeek fetch API。
    #: 与 ``provider`` 分开：搜索和抓取可以各选一个，默认 ``local`` 不改变任何已有行为。
    fetch_provider: FetchProvider = "local"
    max_results: int = Field(default=5, ge=1, le=20)
    max_chars: int = Field(default=8_000, ge=500, le=100_000)
    #: 搜索后自动抓取前 N 条的正文首段（2026-10-23）；上下界与 ``WebAccessConfig`` 一致。
    prefetch_pages: int = Field(default=2, ge=0, le=5)
    max_search_calls: int | None = Field(default=None, ge=1)
    max_read_pages: int | None = Field(default=None, ge=1)
    max_reads_per_page: int | None = Field(default=None, ge=1)
    timeout_seconds: int = Field(default=15, gt=0, le=120)
    domain_policy: Literal["allow", "allowlist"] = "allow"
    denied_domains: tuple[str, ...] = ()
    allowed_domains: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _clean_domains(self) -> WebAccessPreferences:
        """与 ``WebAccessConfig`` 共用 :func:`clean_domain_list`：库里存的就是策略要比对的形式。"""
        self.denied_domains = clean_domain_list(self.denied_domains, "denied_domains")
        self.allowed_domains = clean_domain_list(self.allowed_domains, "allowed_domains")
        return self


class AttachmentsPreferences(_Preference):
    """图片附件偏好（2026-10-20）。

    - ``image_context_turns``：出站请求保留图片的**最近轮数**（含当前轮，1~10）。
      1 = 只发当前轮的图，下一轮就换占位；值越大，历史图重发得越多（每图固定 1024 token）。
    - ``backend``：``local``（本机文件，默认）或 ``oss``（S3 兼容对象存储，公开读）。
    - ``oss_*``：选 ``oss`` 时必填端点、桶名与公开前缀。**密钥不在这里**——它走凭据存储
      （`model_settings.attachments_oss`），偏好会随 Checkpoint 走，不能放密钥。
    """

    backend: Literal["local", "oss"] = "local"
    image_context_turns: int = Field(default=DEFAULT_IMAGE_CONTEXT_TURNS, ge=1, le=10)
    oss_endpoint: str = ""
    oss_region: str = "auto"
    oss_bucket: str = ""
    oss_public_base_url: str = ""
    oss_key_prefix: str = "chaiai/"
    oss_path_style: bool = True

    @model_validator(mode="after")
    def _require_oss_settings(self) -> AttachmentsPreferences:
        """选 ``oss`` 就把必填项一次性说清：不要出现"保存成功但发图才报错"。"""
        if self.backend != "oss":
            return self
        missing = [
            label
            for label, value in (
                ("端点", self.oss_endpoint),
                ("桶名", self.oss_bucket),
                ("公开访问前缀", self.oss_public_base_url),
            )
            if not str(value).strip()
        ]
        if missing:
            raise ValueError(f"选择对象存储后，这些配置不能为空：{'、'.join(missing)}")
        endpoint = self.oss_endpoint.strip().lower()
        public = self.oss_public_base_url.strip().lower()
        if not public.startswith("https://"):
            raise ValueError("公开访问前缀必须是 https:// 地址（模型要从公网拉图）")
        if not endpoint.startswith("https://") and not endpoint.startswith(
            ("http://127.0.0.1", "http://localhost")
        ):
            raise ValueError("对象存储端点必须是 https://（本机调试可用 http://127.0.0.1）")
        return self


class CuratedMemoryPreferences(_Preference):
    """常驻记忆（``MEMORY.md`` / ``USER.md``，2026-10-27；架构 §10.7）。

    与参照实现的设置页一一对应：两个开关 + 两个预算。三条说明写在这里，因为页面 note 只是转述：

    - 预算是**字符**不是 token，默认 2200 / 1375；
    - 改小**不会删除**已有内容（加载时只告警、不截断），只影响后续能否再添加；
    - 两块内容每轮进系统提示，是固定成本——这与中期/长期的"按需召回"不是一回事。
    """

    enabled: bool = True
    user_profile_enabled: bool = True
    memory_char_limit: int = Field(default=2200, gt=0)
    user_char_limit: int = Field(default=1375, gt=0)


class CompressionPreferences(_Preference):
    """上下文压缩（2026-10-26：从"高级只读概览"搬进可改）。

    只放**用户能凭结果判断**的四项：开不开、什么时候压、压完保留多少。

    其余预算项（工具步字符预算、单条工具结果阈值、摘要上限）刻意留在只读：它们改错的症状是
    "某段输出莫名其妙被截断"，用户既看不出原因、也不知道该调哪个，只会把界面变成一堆旋钮。

    上下界与 ``CompressionConfig`` 一致（偏好值经 ``model_copy(update=…)`` 回灌，那条路径不再
    二次校验，所以写在这里的约束就是最后一道）。
    """

    enabled: bool = True
    #: 已用输入额度占"窗口 − 输出预留"的比例，超过即触发压缩。
    trigger_ratio: float = Field(default=0.8, gt=0.0, le=1.0)
    #: 压缩后原样保留的最近轮数。
    preserve_recent_turns: int = Field(default=8, ge=1)
    #: 摘要目标占原内容的比例。
    retain_ratio: float = Field(default=0.16, gt=0.0, le=1.0)

    @model_validator(mode="after")
    def _retain_must_be_below_trigger(self) -> CompressionPreferences:
        """保留比例 ≥ 触发比例等于"压完立刻再触发"——直接拒绝，不做静默修正。"""
        if self.retain_ratio >= self.trigger_ratio:
            raise ValueError("保留比例必须小于触发比例，否则压缩后立刻再次触发")
        return self


class AgentsPreferences(_Preference):
    """项目规则（``AGENTS.md``）的读取口径（2026-10-26：从"高级只读"搬进可改）。

    只搬三项**用户能判断**的：读不读、单份上限、总上限。

    不搬 ``filenames`` 与 ``precedence``：前者决定"哪些文件算规则"，改错就再也读不到自己写的
    那份；后者是"近的目录覆盖远的"这条语义，不是旋钮。

    症状锚点：规则写在 AGENTS.md 里却没生效，最常见的原因就是**被上限截断了**——这时用户
    需要一个能看到并调高的入口，而不是去翻 YAML。
    """

    enabled: bool = True
    #: 单份规则文件的字符上限；超出部分不注入。
    max_file_chars: int = Field(default=20_000, gt=0)
    #: 所有规则文件合计的字符上限；超出部分同样不注入。
    max_total_chars: int = Field(default=60_000, gt=0)

    @model_validator(mode="after")
    def _total_must_cover_one_file(self) -> AgentsPreferences:
        """总上限小于单份上限等于"一份都读不全"：直接拒绝，不给静默生效一半的配置。"""
        if self.max_total_chars < self.max_file_chars:
            raise ValueError("规则总字符上限不能小于单份上限，否则连一份规则都读不全")
        return self


class RuntimePreferences(_Preference):
    execution: ExecutionPreferences
    retrieval: RetrievalPreferences
    memory: LongTermPreferences
    #: 常驻记忆（2026-10-27）：``None`` = 这个字段出现之前保存的设置，按启动配置补齐。
    curated: CuratedMemoryPreferences | None = None
    # Missing fields in old settings/checkpoints keep the startup configuration.
    medium_term: MediumTermPreferences | None = None
    embedding: EmbeddingPreferences | None = None
    web_access: WebAccessPreferences | None = None
    attachments: AttachmentsPreferences | None = None
    #: 上下文压缩（2026-10-26）。``None`` = 这个字段出现之前保存的设置，按启动配置补齐。
    compression: CompressionPreferences | None = None
    #: 项目规则（2026-10-26），口径同上。
    agents: AgentsPreferences | None = None


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
            safe_delete=config.security.safe_delete,
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
        web_access=WebAccessPreferences(
            **config.web_access.model_dump(include={
                "provider", "fetch_provider", "max_results", "max_chars", "prefetch_pages",
                "max_search_calls", "max_read_pages", "max_reads_per_page",
                "timeout_seconds", "domain_policy", "denied_domains", "allowed_domains",
            }),
        ),
        attachments=AttachmentsPreferences(),
        compression=CompressionPreferences(**config.memory.compression.model_dump(include={
            "enabled", "trigger_ratio", "preserve_recent_turns", "retain_ratio",
        })),
        agents=AgentsPreferences(**config.agents.model_dump(include={
            "enabled", "max_file_chars", "max_total_chars",
        })),
        curated=CuratedMemoryPreferences(**config.memory.curated.model_dump(include={
            "enabled", "user_profile_enabled", "memory_char_limit", "user_char_limit",
        })),
    )


def complete_preferences(values: PreferenceValues, config: AppConfig) -> PreferenceValues:
    defaults = initial_preferences(config)
    return values.model_copy(update={
        "medium_term": values.medium_term or defaults.medium_term,
        "embedding": values.embedding or defaults.embedding,
        "web_access": values.web_access or defaults.web_access,
        "attachments": values.attachments or defaults.attachments,
        "compression": values.compression or defaults.compression,
        "agents": values.agents or defaults.agents,
        "curated": values.curated or defaults.curated,
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
        "security": config.security.model_copy(update={
            "shell": shell,
            # 安全删除（2026-10-20）：保存后从下一次新任务生效（正在跑的那一轮保留当时的策略）。
            "safe_delete": execution.safe_delete,
        }),
        "rag": config.rag.model_copy(update=values.retrieval.model_dump()),
        "memory": config.memory.model_copy(update={
            "medium_term": medium,
            "long_term": config.memory.long_term.model_copy(update=values.memory.model_dump()),
            # 压缩（2026-10-26）：只覆盖可改的四项，其余预算项仍取启动配置。
            "compression": config.memory.compression if values.compression is None else (
                config.memory.compression.model_copy(update=values.compression.model_dump())
            ),
            # 常驻记忆（2026-10-27）：两个开关 + 两个预算；``path`` 仍取启动配置（数据位置不在这里改）。
            "curated": config.memory.curated if values.curated is None else (
                config.memory.curated.model_copy(update=values.curated.model_dump())
            ),
        }),
        # 联网检索：只覆盖设置页管的那几项，``enabled`` / 两个 ``*_api_key_env`` / ``user_agent``
        # 仍取启动配置——``enabled`` 随后由能力开关回灌（``apply_capability_gates``）。
        "web_access": config.web_access if values.web_access is None else (
            config.web_access.model_copy(update=values.web_access.model_dump())
        ),
        # 项目规则（2026-10-26）：只覆盖三项可改的，``filenames`` / ``precedence`` 仍取启动配置。
        "agents": config.agents if values.agents is None else (
            config.agents.model_copy(update=values.agents.model_dump())
        ),
    })


def advanced_preferences(config: AppConfig) -> dict[str, int | float | bool]:
    """显式白名单，禁止向页面倾倒配置（含密钥、路径、命令规则）。

    2026-10-26：压缩的**开关 / 触发比例 / 保留轮数**已搬进可改的 ``CompressionPreferences``，
    这里只留**内部预算**——它们改错的症状是"某段输出莫名被截断"，用户既看不出原因、也无从选择
    该调哪一个，摆出来只会多几个无从判断的旋钮。
    """
    c = config.memory.compression
    return {
        "preserve_recent_tool_steps": c.preserve_recent_tool_steps,
        "tool_step_budget_chars": c.tool_step_budget_chars,
        "tool_result_keep_chars": c.tool_result_keep_chars,
        "summary_max_chars": c.summary_max_chars,
        # 短期记忆关掉就没有会话可读（会话、Checkpoint、幂等台账都在那个库里），所以只展示不给改。
        "short_term_enabled": config.memory.short_term.enabled,
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
