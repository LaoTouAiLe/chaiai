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
from typing import Literal, cast

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
    #: 模型上下文窗口（token）。**必填且没有默认值**：T16 的压缩触发比 ``trigger_ratio``
    #: 以 ``context_window_tokens - max_tokens`` 为分母，给一个固定缺省值会让"达到多少比例"
    #: 这句话随模型变化而失真（换模型必须同步改这里，不能靠默认值蒙对）。
    context_window_tokens: int = Field(gt=0)
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    #: 输出预留：回答长度上限。输入 Prompt 的可用预算 = 上下文窗口 − 这里的值。
    max_tokens: int = Field(default=4096, gt=0)
    timeout_seconds: int = Field(default=120, gt=0)
    retry_count: int = Field(default=2, ge=0, le=10)
    #: 网页是否按增量流式展示**最终回答**（T23）。默认关闭：流式只是展示层优化，关闭时行为与
    #: 之前完全一致；打开后它同时作用于模型构造（``streaming``）与页面 sink——只在"最终回答"
    #: 那两个调用点流式（见 ``local_agent.ui.streaming``），其它调用一律照常一次性拿全。
    streaming: bool = False

    @model_validator(mode="after")
    def _window_must_exceed_output_reserve(self) -> LlmConfig:
        if self.context_window_tokens <= self.max_tokens:
            raise ValueError(
                f"llm.context_window_tokens（{self.context_window_tokens}）必须大于 "
                f"llm.max_tokens（{self.max_tokens}）：max_tokens 是为回答预留的输出额度，"
                "输入 Prompt 的可用预算 = 上下文窗口 − 输出预留，必须为正数"
            )
        return self


#: Embedding provider（架构 §10.2）：本地 ONNX / OpenAI 兼容 API。
EmbeddingProvider = Literal["onnx_local", "openai_compatible"]

#: 只有 ``onnx_local`` 使用的字段；``openai_compatible`` 里出现即视为配置错误。
_ONNX_LOCAL_ONLY_FIELDS = ("cache_dir", "local_files_only")
#: 只有 ``openai_compatible`` 使用的字段；``onnx_local`` 里出现即视为配置错误。
_OPENAI_COMPATIBLE_ONLY_FIELDS = ("base_url", "api_key_env")


class EmbeddingConfig(_StrictModel):
    """中期记忆使用的 embedding（架构 §10.2、开发文档 §8）。

    校验按 ``provider`` 区分，**不静默忽略**当前 provider 不适用的字段。判定口径是
    "**显式提供且给出了实际内容**"：空字符串与 ``false`` 属于无效占位（``model_dump()`` 后
    再 ``model_validate()`` 就会这样出现），不算使用该字段；填了真实内容才会失败。

    - ``onnx_local``（v0.2 默认）：要求 ``model`` 与 ``cache_dir``（启用时必须是绝对路径，
      模型权重只落在该目录，不进 Git、不进项目源码目录）；填了 ``base_url`` 或
      ``api_key_env`` 会校验失败。
    - ``openai_compatible``：要求 ``model``、``base_url``、``api_key_env``；填了
      ``cache_dir`` 或 ``local_files_only=true`` 会校验失败。

    必填项只在 ``enabled=true`` 时检查（关掉的层不需要填全），但"不适用字段"无论在
    启用与否都会报错，避免关掉开关时把配置错误藏起来。
    """

    enabled: bool = False
    provider: EmbeddingProvider = "onnx_local"
    base_url: str = ""
    api_key_env: str = ""
    model: str = ""
    #: ``onnx_local`` 的模型缓存目录；首次下载与离线加载都用它，必须是本机绝对路径。
    cache_dir: str = ""
    #: ``true`` 时禁止任何网络下载，只从 ``cache_dir`` 读取（缺模型即失败，不静默换源）。
    local_files_only: bool = False

    @model_validator(mode="after")
    def _check_provider_fields(self) -> EmbeddingConfig:
        if self.provider == "onnx_local":
            forbidden = self._foreign_fields(_OPENAI_COMPATIBLE_ONLY_FIELDS)
            if forbidden:
                raise ValueError(
                    "embedding.provider 为 onnx_local 时不能使用：" + "、".join(forbidden)
                )
            missing = [
                name for name in ("model", "cache_dir") if not getattr(self, name).strip()
            ]
            if self.enabled and missing:
                raise ValueError("embedding.enabled 为 true 时必须填写：" + "、".join(missing))
            # 只在真正启用时要求绝对路径：关掉的层不该因为示例里的机器相关路径而在别的
            # 平台（例如 POSIX 上的 "E:/..."）连配置都加载不了
            if self.enabled and self.cache_dir.strip() and not Path(self.cache_dir).is_absolute():
                raise ValueError(
                    f"embedding.cache_dir 必须是绝对路径（当前：{self.cache_dir}）；"
                    "相对路径会随启动目录漂移"
                )
            return self

        forbidden = self._foreign_fields(_ONNX_LOCAL_ONLY_FIELDS)
        if forbidden:
            raise ValueError(
                "embedding.provider 为 openai_compatible 时不能使用：" + "、".join(forbidden)
            )
        missing = [
            name
            for name in ("base_url", "api_key_env", "model")
            if not getattr(self, name).strip()
        ]
        if self.enabled and missing:
            raise ValueError("embedding.enabled 为 true 时必须填写：" + "、".join(missing))
        return self

    def _foreign_fields(self, names: tuple[str, ...]) -> list[str]:
        """找出"属于另一个 provider 且给出了实际内容"的字段。

        只有"显式出现**且有值**"才算冲突：空的 ``base_url=""`` / ``cache_dir=""`` 与默认值
        等价，``model_dump()`` 后再 ``model_validate()`` 就会这样出现——若把空值也判为冲突，
        配置对象就无法无损往返（既有的"dump 再校验"用例会全部失败，用户照着 dump 写 YAML
        也会撞错）。真正会给错结果的是"填了内容却不会被使用"，那才是要拦的。
        """
        found: list[str] = []
        for name in names:
            if name not in self.model_fields_set:
                continue
            value = getattr(self, name)
            if isinstance(value, str):
                if value.strip():
                    found.append(name)
            elif value:
                found.append(name)
        return found


class ExternalPathsConfig(_StrictModel):
    """工作区外路径的**出厂默认档位**（T38；架构 §1.5）。

    界面上的档位存在设置库里（"保存即生效、作用于下一轮"），这里只是新安装与旧配置的起点：

    - ``deny``：拒绝一切工作区外目标；
    - ``whitelist``：只放行「设置 → 权限」里逐条添加的授权目录。**默认值**——空白名单等价于
      ``deny``，因此出厂默认拒绝一切外部路径，分发出去的实例开箱安全；
    - ``allow``：放行显式绝对路径的外部目标（``~``、相对路径逃逸与 Windows 盘符相对路径仍被
      拒绝），删除/覆盖/破坏性 Shell/大范围编辑四类敏感操作照旧必须人工批准。

    档位只决定"常规小改动是否需要逐次询问"，不放宽任何硬约束。
    """

    mode: Literal["deny", "whitelist", "allow"] = "whitelist"


class WorkspaceConfig(_StrictModel):
    """工作区边界（架构 §1.5）；相对路径以**当前会话绑定的工作区**为根。

    ``root`` 只是启动默认值：T14C 起网页可以选择工作区并随会话保存，每个会话只绑定一个
    工作区。外部访问由 ``external_paths.mode``（T38）控制；``allow_external_paths`` 是 T14C 的
    旧键，保留为快捷方式（``true`` 等价 ``mode: allow``），现有配置文件不因 T38 失效。
    无论哪一档，``~``、相对路径逃逸与 Windows 盘符相对路径仍被拒绝，外部副作用按分级判定，
    根目录/主目录/系统目录/ Git 元数据即使用户批准也不执行。
    """

    root: str = "."
    #: 旧键（T14C）：``true`` 等价 ``external_paths.mode: allow``。只作快捷方式，不再单独生效。
    allow_external_paths: bool = False
    external_paths: ExternalPathsConfig = Field(default_factory=ExternalPathsConfig)
    max_file_size_bytes: int = Field(default=2_000_000, gt=0)

    @model_validator(mode="after")
    def _resolve_external_paths(self) -> WorkspaceConfig:
        """旧键归一为新档位；只有**真实的语义冲突**才报错。

        "新旧同时出现就报错"这条看似更严格，实际会把 ``model_dump() → model_validate()`` 的往返
        判成冲突（往返必然把两个键都写出来），现有配置的读回也就跟着坏掉——T29A 的
        ``_provider_conflicts`` 已经为同类问题定过口径："显式出现**且有值**、且会给出不同结果"
        才拦。这里沿用同一口径：

        - 旧键 ``allow_external_paths: true`` + 新键显式写了非 ``allow`` 的档位 → 报错（谁生效说不清）；
        - 只有旧键为 ``true`` → 归一为 ``mode: allow``，配置照常启动；
        - 其余情况新键优先，旧键保持默认。
        """
        if not self.allow_external_paths:
            return self
        if "external_paths" in self.model_fields_set and self.external_paths.mode != "allow":
            raise ValueError(
                "workspace.allow_external_paths 与 workspace.external_paths.mode 语义冲突："
                "旧键 true 等价 mode: allow。请二选一——删除旧键只留 "
                f"external_paths.mode: {self.external_paths.mode}，"
                "或把档位也改成 allow"
            )
        self.external_paths = ExternalPathsConfig(mode="allow")
        return self


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
    """中期记忆：Chroma 向量库（T14B 启用）。"""

    enabled: bool = False
    backend: Literal["chroma"] = "chroma"
    path: str = "data/chroma"
    collection: str = "agent_memory"
    top_k: int = Field(default=8, gt=0)
    #: 召回的最低语义相似度（越大越相关，**真实余弦**）。
    #:
    #: 0.53 是按默认模型 ``BAAI/bge-small-zh-v1.5``（FastEmbed）**实测标定**的：5 组中文查询里
    #: 相关记忆落在 0.560–0.772，无关记忆落在 0.242–0.497，取两者之间。该模型的相关性基线偏高
    #: （无关内容也有 0.3–0.5），所以照 OpenAI 系 embedding 量级写的 0.72 会把结果全部筛掉，
    #: 而"看起来很低"的 0.1 又会把无关记忆放进来。**这组数字只来自 5 组查询，换模型或换成真实
    #: 记忆库必须重新标定**（做法：``medium_term.configured_distance_space`` 确认距离度量是 cosine，
    #: 再打印原始命中的 ``1 - distance`` 分布找间隔）。
    min_score: float = Field(default=0.53, ge=0.0, le=1.0)
    decay_half_life_days: int = Field(default=30, gt=0)
    write_on: list[str] = Field(default_factory=list)


class LongTermMemoryConfig(_StrictModel):
    """长期记忆：SQLite 实体库（T15 启用）。"""

    enabled: bool = True
    backend: Literal["sqlite"] = "sqlite"
    path: str = "data/memories.sqlite"
    confidence_threshold: float = Field(default=0.75, ge=0.0, le=1.0)
    importance_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    aging_enabled: bool = True
    forget_after_days: int = Field(default=180, gt=0)
    #: 是否允许把事实写进**用户级**（``user``）作用域——即"跨项目可见"。
    #:
    #: 默认 ``false``：``entity_fact`` 一律落项目级。原因：判定"这是个人偏好而不是项目事实"依赖
    #: 模型输出的 ``entity_type``，而作用域是跨项目隔离边界——模型一次误分类就会让项目事实被别的
    #: 项目召回。跨项目共享属于显式授权，需要在这里打开（打开后 ``entity_type="user"`` 才落
    #: 用户级，其余仍落项目级）。
    allow_user_scope: bool = False


class CompressionConfig(_StrictModel):
    """Prompt 窗口压缩（T16 启用）。"""

    enabled: bool = True
    trigger_ratio: float = Field(default=0.8, gt=0.0, le=1.0)
    preserve_recent_turns: int = Field(default=8, ge=1)
    #: 窗口里最多保留的**最近完整工具步**数量，更早的整步省略并留一条带摘要的说明占位。
    #: 轮次保底对"一个用户请求内的长工具循环"无效（只有 1 个人类轮次），靠这条才能收缩单个长任务。
    #: ``0`` 表示**所有工具步都被省略**（窗口里只剩人类消息与那条说明占位）。
    preserve_recent_tool_steps: int = Field(default=3, ge=0)
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
    """可选按需代码检索（架构 §4.3、§5.3；T21 启用）。

    **没有持久索引**：每次查询直接读当前工作区的磁盘现状，所以不存在"陈旧索引"，
    也没有 `backend` / `index_path` 这类预留键（T21 已把旧的 tree_sitter 预留收窄掉）。

    所有限额都必须有界：一个常见查询词就能命中整棵树，没有上限会同时吃掉内存与 Prompt。
    """

    enabled: bool = False
    max_results: int = Field(default=8, gt=0)
    #: 单次检索的超时秒数；`rg` 与 Python 扫描共用同一上限。
    timeout_seconds: int = Field(default=10, gt=0)
    #: 候选发现最多检查的文件数；达到上限即停止并在结果说明里标注"已截断"。
    max_files: int = Field(default=200, gt=0)
    #: 单个文件最多读取的字节数；超过即跳过，并在结果说明里如实标注。
    max_file_bytes: int = Field(default=262_144, gt=0)
    #: 从用户请求里最多提取多少个查询词项。
    max_terms: int = Field(default=4, gt=0)
    #: 单个词项最多接受多少条命中（常见词命中太多时按文件顺序截断）。
    max_matches_per_term: int = Field(default=20, gt=0)
    #: 片段上下文行数：命中行前后各取多少行。
    context_lines: int = Field(default=20, ge=0)
    #: 单个片段的字符上限。
    snippet_chars: int = Field(default=1200, gt=0)
    #: 「检索到的代码片段」段落的总字符上限。
    section_chars: int = Field(default=4000, gt=0)


class AgentConfig(_StrictModel):
    """Agent 循环与证据要求。"""

    max_tool_loops: int = Field(default=20, gt=0)
    max_retries_per_step: int = Field(default=2, ge=0)
    require_evidence_for_completion: bool = True


class WebConfig(_StrictModel):
    """本机 HTTP 服务（T29；契约 §15.3、计划 §6.2）。

    默认只绑定**回环地址**：v0.4 没有鉴权，把监听地址改成非回环必须显式 opt-in
    （``allow_non_loopback``），否则启动就失败闭合，而不是"配了但没人发现"。
    """

    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    #: 允许的 `Host` 头（防 DNS rebinding）。默认只有回环的三种写法。
    allowed_hosts: list[str] = Field(default_factory=lambda: ["127.0.0.1", "localhost", "[::1]"])
    #: 开发期允许的跨源来源；生产同源（FastAPI 托管前端产物），因此默认只有 Vite dev server。
    dev_origins: list[str] = Field(
        default_factory=lambda: ["http://127.0.0.1:5173", "http://localhost:5173"]
    )
    #: 请求体上限（字节）。超限返回 413，不进入业务逻辑。
    max_body_bytes: int = Field(default=1_000_000, gt=0)
    #: 前端构建产物目录（相对配置文件所在目录，或绝对路径）；不存在时只提供 API。
    static_dir: str = "frontend/dist"
    #: SSE 心跳间隔与空闲上限：死连接不能一直占着订阅（§15.3）。
    sse_heartbeat_seconds: float = Field(default=15.0, gt=0)
    sse_idle_timeout_seconds: float = Field(default=300.0, gt=0)
    #: 单次排空的待发事件上限：超过即判定客户端落后，结束这次订阅并要求重连（§6.2 的增量不持久）。
    sse_max_pending_events: int = Field(default=200, gt=0)
    #: HTTP keep-alive 超时（uvicorn 的 `timeout_keep_alive`）。
    keepalive_seconds: int = Field(default=15, gt=0)
    #: 允许把监听地址改成非回环（v0.4 无鉴权，默认禁止）。
    allow_non_loopback: bool = False

    @model_validator(mode="after")
    def _loopback_requires_explicit_opt_in(self) -> WebConfig:
        """非回环监听必须显式声明；顺手要求它出现在 `allowed_hosts` 里，避免自相矛盾。"""
        if self.allow_non_loopback:
            return self
        if not is_loopback_host(self.host):
            raise ValueError(
                f"web.host={self.host} 不是回环地址：v0.4 没有鉴权，"
                "改监听地址必须同时设置 web.allow_non_loopback=true"
            )
        return self


def is_loopback_host(host: str) -> bool:
    """``host`` 是否只在本机可达（回环 IPv4/IPv6 或 ``localhost``）。"""
    text = host.strip().strip("[]").lower()
    if text in {"localhost", "::1"}:
        return True
    return text == "127.0.0.1" or text.startswith("127.")


class ModelSettingsConfig(_StrictModel):
    """可视化模型连接设置（T29A；契约 §6.8）。

    - ``path``：**独立**的设置库（非秘密设置：连接、默认与会话选择、能力检测结果）。与聊天库分开存，
      免得"删会话"和"停用连接"互相牵连；
    - ``credential_store``：``auto`` 在 Windows 用系统凭据存储、其它平台临时；``memory`` 强制临时模式
      （不想把密钥交给系统凭据存储时用，代价是重启后要重新录入）。
      **没有任何一档会把密钥写进文件**——这一点由设置库的表结构保证（没有能放密钥的列）。
    """

    path: str = Field(default="data/model_settings.sqlite", min_length=1)
    credential_store: Literal["auto", "memory"] = "auto"


class AppConfig(_StrictModel):
    """全部配置的根对象。"""

    #: ``llm`` 段**可以缺省**（v0.4 起，§15.9）：Web/API 在缺少它时进入"初始设置模式"，
    #: 用户在设置页里加连接即可，不需要伪造一份 YAML 模型段。CLI 与 Streamlit 仍然需要它，
    #: 它们要显式调 :meth:`require_llm`。
    llm: LlmConfig | None = None
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    workspace: WorkspaceConfig = Field(default_factory=WorkspaceConfig)
    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    rag: RagConfig = Field(default_factory=RagConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    model_settings: ModelSettingsConfig = Field(default_factory=ModelSettingsConfig)

    def as_run_config(self) -> RunConfig:
        """收窄成运行期配置；``llm`` 缺省时抛可执行的 `ConfigError`。

        Raises:
            ConfigError: 没有 ``llm`` 段——说明提交边界还没解析出连接，那是调用方的顺序错了。
        """
        if self.llm is None:
            raise ConfigError("运行期配置缺少 llm 段：提交边界应先解析出一条模型连接")
        return cast(RunConfig, self)

    def require_llm(self) -> LlmConfig:
        """取出 ``llm`` 段；缺省时抛可执行的 `ConfigError`。

        Raises:
            ConfigError: 配置里没有 ``llm`` 段（只有 Web/API 允许缺省）。
        """
        if self.llm is None:
            raise ConfigError(
                "配置里没有 llm 段：Web/API 可以直接进设置页添加连接；"
                f"CLI 与 Streamlit 需要 {DEFAULT_CONFIG_PATH.as_posix()} 里的 llm 段"
                f"（可复制 {DEFAULT_CONFIG_PATH.name}.example 后修改）"
            )
        return self.llm

    @model_validator(mode="after")
    def _require_embedding_for_medium_term(self) -> AppConfig:
        """中期记忆要用向量，没有 embedding 就拿不到向量。

        在配置加载阶段就失败闭合：否则要等到每轮对话触发召回时才报错，而页面（T19 之前）
        根本看不到原因。
        """
        if self.memory.medium_term.enabled and not self.embedding.enabled:
            raise ValueError(
                "memory.medium_term.enabled 为 true 时需要 embedding.enabled 也为 true"
                "（中期记忆必须显式配置 embedding provider）"
            )
        return self


class RunConfig(AppConfig):
    """**运行期**配置：``llm`` 必定存在（提交边界已经解析出这次要用哪条连接）。

    Graph 与安全策略读的都是运行期配置——它们里面到处是 ``config.llm.context_window_tokens``
    之类的访问，每次都要写"如果 llm 存在"的守卫会让真正的业务逻辑被噪声淹没。所以由
    :meth:`AppConfig.as_run_config` 在这一个地方收窄，之后整条链路按"llm 一定有"来读。
    """

    llm: LlmConfig


def load_dotenv_file(env_path: Path = DEFAULT_ENV_PATH) -> bool:
    """加载 ``.env``；已存在的环境变量优先，不被文件覆盖。

    Returns:
        是否找到并加载了文件。
    """
    return load_dotenv(dotenv_path=env_path, override=False)


def load_config(config_path: Path = DEFAULT_CONFIG_PATH, *, allow_missing: bool = False) -> AppConfig:
    """读取并校验配置文件。

    Args:
        config_path: 配置文件路径，默认 ``config.yaml``。
        allow_missing: 文件不存在时是否返回一份**安全默认配置**（全字段走产品默认值、``llm`` 为空）。
            Web/API 入口用它进入"初始设置模式"（§15.9）：全新安装没有任何 YAML 也要能打开设置页。
            CLI 与 Streamlit 保持 ``False``——它们必须显式配置。

    Returns:
        校验通过的完整配置。

    Raises:
        ConfigError: 文件缺失（且未允许缺省）、YAML 语法错误或字段校验失败。
    """
    display_path = config_path.as_posix()
    if not config_path.is_file():
        if allow_missing:
            return AppConfig()
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


def resolve_api_key(
    config: LlmConfig | EmbeddingConfig, environ: Mapping[str, str] | None = None
) -> str:
    """从环境变量取出 API Key。

    Args:
        config: ``llm`` 或 ``embedding`` 配置节；两者都用 ``api_key_env`` 指变量名。
            ``embedding.provider=onnx_local`` 时不读 Key，调用方也不应调用本函数。
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


def optional_api_key(
    config: LlmConfig | EmbeddingConfig, environ: Mapping[str, str] | None = None
) -> str | None:
    """非抛出式取 Key：缺失或为空时返回 ``None``。

    给**状态展示**这类只读场景用：那里要的是"能不能用、缺什么"的说明，而不是一个异常——直接调用
    :func:`resolve_api_key` 会让状态页整块抛错，连"请补 Key"的提示都显示不出来（复审实证：
    ``readiness(..., api_key=None)`` 的 ``not_ready`` 分支根本走不到）。
    """
    env = os.environ if environ is None else environ
    value = env.get(config.api_key_env, "").strip()
    return value or None


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
    """把校验错误转成可读消息：逐条列出现位置，缺必填键时再补一句可操作指引。

    为什么单独处理"缺键"：本项目会随任务新增**必填**配置键（例如 T16 的
    ``llm.context_window_tokens``），而 ``config.yaml`` 被 .gitignore 排除、不会自动更新——
    旧配置会在升级后直接失效。只回一句 "Field required" 用户不知道该补什么、补到哪，
    所以这里明确指向示例文件。
    """
    details: list[str] = []
    missing: list[str] = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"])
        details.append(f"{location}: {error['msg']}")
        if error["type"] == "missing":
            missing.append(location)
    message = f"{display_path} 配置校验失败：" + "；".join(details)
    if missing:
        message += (
            f"。缺少必填键：{'、'.join(missing)}——对照项目里的 {DEFAULT_CONFIG_PATH.name}.example "
            "补齐这几项后重启（示例文件里每个键都有注释）"
        )
    return message
