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
from typing import Literal, TypeVar, cast

import yaml
from dotenv import load_dotenv
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from local_agent.capabilities import (
    CAPABILITY_TOOLS,
    DEFAULT_PROFILE,
    PROFILES,
    as_switch_layer,
    capability_names,
    duplicated_capabilities,
    profile_names,
    resolve_capabilities,
    unknown_capabilities,
)
from local_agent.security.path_guard import ExternalPathsMode, normalize_mode

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
    #: 之前完全一致；打开后它只作用于"最终回答"这一条路径的调用点（见 ``local_agent.ui.streaming``
    #: 与 ``local_agent.ui.decision_streaming``），其它调用一律照常一次性拿全。
    #:
    #: 2026-10-12（v0.8 T74）修正两处过时描述：① 原注释说"作用于模型构造（``streaming``）"——
    #: 实际**没有**传给 `ChatOpenAI`（``llm/factory.py`` 明确刻意不传，两个取值都会踩坑）；
    #: ② 原注释说"两个调用点"，实际是**三个**（两个"最终回答" + 主循环的工具决策流式）。
    #: 口径与呈现见 §19.4：本开关不改变业务结果，只决定有没有增量。
    streaming: bool = False
    #: 这条连接**是否支持图像输入**（2026-10-16；`view_image` 与 `image_read` 能力的闸门）。
    #:
    #: 为什么是**声明**而不是探测：视觉能力只能靠"发一张图看它收不收"来测，那要花一次真实调用，
    #: 且各家对不支持的返回形态不一；仓库里既有的能力探测（`probe_capabilities`）只覆盖构造层。
    #: 因此这里由用户声明，**默认 false = 失败闭合**：只有声明为真，`image_read` 才进有效能力集、
    #: `view_image` 才绑给模型，运行环境段也才会写"图像输入：可用"。
    #: 按连接分别声明（而不是整个 `llm` 段一处）属于后续增量：连接记录里目前没有这一位。
    supports_vision: bool = False

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
    """**权限档位**的出厂默认（架构 §1.5）。

    界面上的档位存在设置库里（"保存即生效、作用于下一轮"），这里只是新安装与旧配置的起点。
    **2026-10-15 起换成"边界"语义的三档**（对齐 codex / dsh，用户裁定）：

    三档均允许读取显式绝对路径的本机外部目标，敏感文件仍由读取策略拦截。

    - ``read-only``：**工作区只读**——写类工具一律拒绝；命令在 Windows 上经"无写授权"的受限
      令牌运行。想只看不改时用这一档；
    - ``workspace-write``：**出货默认**。工作区可写；外部读取不需目录授权，外部写入只允许
      「设置 → 权限」里添加的可写目录；
    - ``danger-full-access``：工作区可写，工作区外**一律可读**，写仍只按逐条授权。

    旧值 ``deny``/``whitelist``/``allow`` 仍被接受并归一（见
    :data:`~local_agent.security.path_guard.LEGACY_MODES`）：设置库里存着旧值，硬拒绝会让老用户
    开不了机。
    """

    mode: ExternalPathsMode = "workspace-write"

    @field_validator("mode", mode="before")
    @classmethod
    def _normalize(cls, value: object) -> object:
        """旧值归一在**一处**（:func:`normalize_mode`），配置与设置库共用同一条。"""
        return normalize_mode(str(value)) if isinstance(value, str) else value


class WorkspaceConfig(_StrictModel):
    """工作区边界（架构 §1.5）；相对路径以**当前会话绑定的工作区**为根。

    ``root`` 只是启动默认值：T14C 起网页可以选择工作区并随会话保存，每个会话只绑定一个
    工作区。外部访问由 ``external_paths.mode``（T38）控制；``allow_external_paths`` 是 T14C 的
    旧键，保留为快捷方式（``true`` 等价 ``mode: danger-full-access``），现有配置文件不因 T38 失效。
    无论哪一档，``~``、相对路径逃逸与 Windows 盘符相对路径仍被拒绝，外部副作用按分级判定，
    根目录/主目录/系统目录/ Git 元数据即使用户批准也不执行。
    """

    root: str = "."
    #: 旧键（T14C）：``true`` 等价 ``external_paths.mode: danger-full-access``。只作快捷方式，不再单独生效。
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

        - 旧键 ``allow_external_paths: true`` + 新键显式写了非 ``danger-full-access`` 的档位 → 报错；
        - 只有旧键为 ``true`` → 归一为 ``mode: danger-full-access``，配置照常启动；
        - 其余情况新键优先，旧键保持默认。
        """
        if not self.allow_external_paths:
            return self
        if "external_paths" in self.model_fields_set and self.external_paths.mode != "danger-full-access":
            raise ValueError(
                "workspace.allow_external_paths 与 workspace.external_paths.mode 语义冲突："
                "旧键 true 等价 mode: danger-full-access。请二选一——删除旧键只留 "
                f"external_paths.mode: {self.external_paths.mode}，"
                "或把档位也改成 danger-full-access"
            )
        self.external_paths = ExternalPathsConfig(mode="danger-full-access")
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
    #: 「最近保留轮次」的**窗口比例**（2026-10-09 新增，对齐 dsh 的 ``retainRatio``）。
    #:
    #: ``preserve_recent_turns`` 是硬编码条数：窗口很小时"至少保留 8 轮"会恒等于"整段历史都
    #: 留下"——保留下限大于窗口，压缩因此**触发也压不动**，而且完全没有可见信号（实测：设了很小
    #: 的 ``context_window_tokens``，聊两三句就超，却一直没有任何压缩动作）。
    #: 这一条把保留下限与窗口联动：实际保留轮数 = ``preserve_recent_turns`` 之内、且 token 总量
    #: 不超过 ``retain_ratio × llm.context_window_tokens`` 的**最近**轮数（下限 1 轮）。
    #: 窗口大时按 ``preserve_recent_turns`` 走，行为不变；窗口小时自动收紧，给压缩留出空间。
    retain_ratio: float = Field(default=0.16, gt=0.0, le=1.0)
    #: 窗口里最多保留的**最近完整工具步**数量，更早的整步省略并留一条带摘要的说明占位。
    #: 轮次保底对"一个用户请求内的长工具循环"无效（只有 1 个人类轮次），靠这条才能收缩单个长任务。
    #: ``0`` 表示**所有工具步都被省略**（窗口里只剩人类消息与那条说明占位）。
    preserve_recent_tool_steps: int = Field(default=3, ge=0)
    #: 窗口里"工具步内容"的**字符预算**（2026-09-30 新增）。
    #:
    #: 加这条的原因是实测到的一个成本漏洞：原来只按**条数**省略（``preserve_recent_tool_steps``），
    #: 于是一个 20 步的工具循环从第 4 步起就要每步重写窗口中段——而窗口一旦不是"追加式"，
    #: 供应商的前缀缓存就整段失效。实测那次命中率只有 **19.9%**（对比同类工具 67.6%），
    #: 未命中输入 635,714 token，折合约 ¥1.6/次提问；逐次记录显示同一轮内
    #: ``system_prompt_fingerprint`` 有 17 个不同值，正是"每步重写中段"留下的痕迹。
    #:
    #: 改成预算驱动后，**短循环一个字都不省**：历史保持追加式，前缀稳定因而可缓存；只有真正
    #: 超预算的长循环才从**最早的**步骤开始省。``preserve_recent_tool_steps`` 保留为**下限**
    #: （预算再紧也至少留这么多步），两条一起用。
    #:
    #: **取值规则（2026-10-01 补，回答"设多少最普适"）**：这个数不是手感值，它夹在两个界之间，
    #: **换模型必须按窗口重算**：
    #: - **下界**：必须大于一道普通任务的工具步内容，否则边界每步都移动——而"移动边界"就是缓存杀手。
    #:   实测"梳理架构"这类提问会读到 17~26 条工具结果，按压中段后的体型（头 :data:`~local_agent.memory.short_term.TOOL_RESULT_HEAD_CHARS`
    #:   + 尾 :data:`~local_agent.memory.short_term.TOOL_RESULT_TAIL_CHARS` + 标记）约 4k 字符/条，
    #:   即 **70k~105k 字符**。
    #: - **上界**：token 那一层压缩在 ``trigger_ratio × (context_window_tokens − max_tokens)`` 处接管
    #:   （本机 ``0.8 × 61,440 = 49,152`` token）；减去系统提示/快照/工具定义后，按本仓库内容的换算
    #:   （实测约 1.6~2.2 字符/token，**没有精确测量**）折算。
    #: - 两个界很靠近，所以默认取**"让字符层只做兜底"**（``160_000``）：忘记交给 token 层的轮次摘要
    #:   （摘要保留内容，比整步丢弃损失小）。实测这一组（160k + 3072/768）一轮 17 次调用只在交界处
    #:   回落 **1 次**；而卡在临界点的取法（120k + 4096/1024）一轮回落 **5 次**，单次缓存读从
    #:   48,512 塌到 3,456。
    #: - **验收观测量**：逐调用缓存读的**回落次数**（目标 0~1 次）——不是命中率。
    tool_step_budget_chars: int = Field(default=160_000, gt=0)
    #: 上下文压力达标后才裁剪超过此字符阈值的工具正文（DSH 的压力阶段策略）。
    #: ``0`` 禁止正文裁剪；行号与目录投影仍可用。实际替换保存进 Checkpoint，预算
    #: 下降后不重新展开历史；裁剪后重新测量，仍有压力才摘要完整前缀。
    tool_result_keep_chars: int = Field(default=8_192, ge=0)
    summary_max_chars: int = Field(default=12_000, gt=0)

    @model_validator(mode="after")
    def _retain_must_be_below_trigger(self) -> CompressionConfig:
        """保留比例必须小于触发比例（同 dsh 的 ``retainRatio < thresholdRatio`` 校验）。

        否则"触发压缩时至少保留的比例"已经不小于"触发阈值"，压缩一触发就没有可压缩的空间——
        这正是本文件中 ``retain_ratio`` 想避免的"触发也压不动"，配置阶段就失败闭合比运行时
        静默压不动好。
        """
        if self.retain_ratio >= self.trigger_ratio:
            raise ValueError(
                f"memory.compression.retain_ratio（{self.retain_ratio}）必须小于 "
                f"trigger_ratio（{self.trigger_ratio}）：保留比例不小于触发比例时，"
                "压缩一触发就没有可压缩的空间"
            )
        return self


class MemoryConfig(_StrictModel):
    """三层记忆与压缩的开关集合。"""

    short_term: ShortTermMemoryConfig = Field(default_factory=ShortTermMemoryConfig)
    medium_term: MediumTermMemoryConfig = Field(default_factory=MediumTermMemoryConfig)
    long_term: LongTermMemoryConfig = Field(default_factory=LongTermMemoryConfig)
    compression: CompressionConfig = Field(default_factory=CompressionConfig)


class RequireApprovalConfig(_StrictModel):
    """可配置审批项；工作区文件操作默认免审批。"""

    delete_file: bool = False
    destructive_shell: bool = True
    large_batch_edit: bool = False
    overwrite_existing_file: bool = False

class LargeEditConfig(_StrictModel):
    """大范围编辑进入审批的阈值。

    **生效的判定是"本轮改过的不同文件数"**（2026-10-15 改）：超过 ``max_files_without_approval``
    之后再改**新文件**，要一次人工批准；批准后本轮内不再逐个问。只统计 ``edit_file``
    （新建文件不计——"产出一组文件"不该被这条拦）。作用域就是一轮：策略每轮重建，计数归零。

    为什么不再按行数判：``max_changed_lines_without_approval`` 用的量是"新增 + 删除行数"
    （``diff_guard.count_changed_lines``），默认 200 恰好落在"重写一个函数"的常态上，实证里它把
    正常重构变成了逐次弹卡。dsh 的 ``edit`` 工具根本没有行数阈值
    （``packages/fs/tool-fs/src/edit.ts`` 只校验参数合法性）。
    """

    max_files_without_approval: int = Field(default=3, ge=0)
    #: **已不参与判定**（2026-10-15）。字段保留只为旧 ``config.yaml`` 不报错
    #: （``_StrictModel`` 拒绝未知键），改它不会改变任何行为——不要再把它当旋钮用。
    max_changed_lines_without_approval: int = Field(default=200, ge=0)


class ShellWhitelistEntry(_StrictModel):
    """结构化 Shell 白名单条目（T12 按平台、可执行名、参数结构与工作目录匹配）。"""

    platform: list[str] = Field(default_factory=list)
    executable: str = Field(min_length=1)
    args: list[str] = Field(default_factory=list)
    #: 参数匹配方式：``exact``（默认，最保守）= 参数必须**完全一致**；
    #: ``prefix`` = 条目参数是实际参数的**前缀**，允许后面跟任意参数。
    #:
    #: ``prefix`` 只适合"参数怎么变都不改变副作用范围"的命令（``npm run build``、``git status``）；
    #: 反过来，解释器类（``python`` / ``node`` / ``sh``）**不要**用 ``prefix``——对它们来说
    #: `-c "…"` 这种后续参数就是"任意代码"，一条 prefix 条目等于把任意代码放行。
    #: 所以 ``prefix`` **至少要给一个参数**（空参数前缀 = 放行整个可执行文件），由下面的校验器强制。
    match: Literal["exact", "prefix"] = "exact"
    #: 限定该条目只在指定的工作目录（相对 workspace.root）下免审；None 表示不限目录。
    cwd: str | None = None

    @model_validator(mode="after")
    def _prefix_needs_at_least_one_argument(self) -> ShellWhitelistEntry:
        if self.match == "prefix" and not self.args:
            raise ValueError(
                "match=prefix 至少要写一个参数：空参数的前缀匹配会把该可执行文件的一切调用"
                "都放行（例如 `python -c \"...\"` 就是任意代码）"
            )
        return self


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
    default_timeout_seconds: int | None = Field(default=None, gt=0)
    #: 单条命令的**超时上限**（秒）；``None`` = **不设上限**（2026-10-16）。
    #:
    #: 为什么默认不设：这个上限原来是 ``default_timeout_seconds`` **自己**（默认 60 秒），于是
    #: "省略时用多少"与"最多允许多少"挤在一个键里。后果是模型想把慢命令跑完（冷启动浏览器、
    #: 装依赖、真实构建）得先猜这个数，猜大了直接被拒——用户的原话是"完全就是限制 agent 能力"，
    #: 而且那次拒绝还被渲染成"被安全策略拦截"（参数越界 ≠ 安全边界）。
    #:
    #: 兜底不在这一层：任务总时长由 ``jobs.max_task_seconds`` 管（**配置了才生效**，见 §17.4），
    #: 单条命令随时可取消，超时终止按进程树收干净。需要收紧的部署把这一项配成数字即可。
    max_timeout_seconds: int | None = Field(default=None, gt=0)
    max_output_chars: int = Field(default=30_000, gt=0)
    #: **已不参与放行判定**（2026-10-15 权限模型重构）：命令的审批现在只由"破坏性 / 提权 /
    #: 工作区外 cwd"三件事触发，档位与沙箱负责边界。字段保留只为旧 `config.yaml` 不报错；
    #: `prepare_shell` 仍会算它（命令分类、审计文案用），但改它不会让某条命令变得要审批或免审批。
    whitelist: list[ShellWhitelistEntry] = Field(default_factory=list)
    blocked_patterns: list[str] = Field(default_factory=list)
    #: **已不参与判定**（2026-10-15 权限模型重构）：写侧沙箱不再是独立开关，由权限档位派生
    #: ——``read-only`` / ``workspace-write`` 两档生效，``danger-full-access`` 不套沙箱。
    #: 字段保留只为旧 ``config.yaml`` 不报错（``extra=forbid``），改它不会改变任何行为。
    #: 生效时的边界（可写根 = 工作区 + 专属临时目录 + 显式读写授权目录；受保护子树不授权；
    #: 新目录立即继承可写；读侧/网络不隔离）见 ``security/win_sandbox.py``。
    #: 非 Windows **照常运行**，只是没有 OS 级写边界（不再失败闭合）。
    sandbox: bool = False


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
    #: **安全删除**（2026-10-20）：开启时 `delete_file` 把目标移进回收站（数据目录下的 `trash/`），
    #: 而不是直接抹掉。默认**开**——"删错了还能找回"应当是不用特意去开的默认状态；
    #: 关掉则恢复旧的立即删除。作用范围只有文件工具，`run_shell` 里的删除命令不受它管辖。
    safe_delete: bool = True


class RagConfig(_StrictModel):
    """可选按需代码检索（架构 §4.3、§5.3；T21 启用）。

    **没有持久索引**：每次查询直接读当前工作区的磁盘现状，所以不存在"陈旧索引"，
    也没有 `backend` / `index_path` 这类预留键（T21 已把旧的 tree_sitter 预留收窄掉）。

    所有限额都必须有界：一个常见查询词就能命中整棵树，没有上限会同时吃掉内存与 Prompt。
    """

    #: 是否启用按需代码检索。**默认打开（2026-10-01 由 ``False`` 改为 ``True``）**。
    #:
    #: 关闭时工具集里**没有任何定位代码的手段**：只有 ``list_dir`` / ``read_file`` / ``edit_file`` /
    #: ``write_file`` / ``delete_file`` / ``run_shell``。实测代价是一个"梳理架构"的请求在 **14 次
    #: ``list_dir``** 之后用尽循环预算（既烧钱又没做完），而且计划里写出"先用 grep 搜索定位"这种
    #: 本机并不存在的工具——模型在描述一个自己没有的能力。
    #:
    #: 打开后按需注入**带路径与行号**的片段（不建持久索引，每次直接读磁盘现状，见类文档），
    #: 模型据此定向读取。节点本身在关闭时是纯透传（不访问文件系统、不探测 ``rg``、不记错误事件），
    #: 所以这个开关不存在"打开才有副作用"的问题。
    enabled: bool = True
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

    #: 默认不设决策次数上限；仅用户显式填写数字时按该预算执行。
    max_tool_loops: int | None = Field(default=None, gt=0)
    max_retries_per_step: int = Field(default=2, ge=0)
    #: 旧配置兼容字段；自主循环由主模型决定结束，不执行程序完成度校验。
    require_evidence_for_completion: bool = False


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
    #:
    #: 2026-10-16 从 1 MB 抬到 8 MB：聊天里上传图片走的是 **base64 的 JSON**（见
    #: `api.routes.upload_attachment`），4 MB 的图编码后约 5.3 MB，1 MB 这道闸门会把任何图片上传
    #: 直接挡成 413——而且那条提示与"图片太大"毫无关系，用户根本看不出是这个原因。
    #: 抬高的代价很小：服务默认只绑**回环**（`web.host`），单用户本机服务，这道闸门的作用是
    #: "别把巨型正文读进内存"，不是 DoS 防线。
    max_body_bytes: int = Field(default=8_000_000, gt=0)
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


#: 外部内容访问的 provider 闭集：结构化 API 与 HTML 兼容检索。
#:
#: auto 根据 Tavily 凭据是否存在选后端；无凭据使用 Bing HTML 兼容搜索。显式指定后端时不静默切换。
WebAccessProvider = Literal["auto", "tavily", "bing_html", "duckduckgo_html"]


def clean_domain_list(values: tuple[str, ...], field: str) -> tuple[str, ...]:
    """域名只写主机名：去空白、转小写、去掉前导点；带协议/路径/端口一律拒绝（不猜用户意图）。

    归一的意义在**比较处**：策略按主机名逐段后缀匹配（``a.example.com`` 属于 ``example.com``），
    大小写与结尾点不同会让同一个域名走出不同判定（与 §20.2 "判定要看系统真正打开的目标"同一口径）。

    放在模块级：``WebAccessConfig``（运行期配置）与 ``WebAccessPreferences``（设置库里的偏好）
    必须共用同一套规则——两处各写一份，"设置页里存下的域名"与"策略实际比较的域名"迟早会漂移。
    """

    result: list[str] = []
    for item in values:
        host = str(item).strip().lower().lstrip(".")
        if not host or "/" in host or ":" in host or " " in host:
            raise ValueError(
                f"{field} 只写主机名（例如 example.com），不带协议、路径、端口或空格"
            )
        result.append(host)
    return tuple(dict.fromkeys(result))


class WebAccessConfig(_StrictModel):
    """外部网络访问（v0.5 T53；契约 §16.3、工具定义 §6.10）。

    **默认开启**（2026-10-14 修订；原文是"默认关闭、显式 opt-in"）。联网是本机 Agent 的主流能力，
    "这一次要不要联网"应当由模型按任务判断，而不是让用户先去面板里把总闸拉起来。旧口径的实际后果
    已经实证：面板开关与这里的布尔**各说一套**——用户在「能力」面板打开 `web`（工具因此对模型可见），
    工具执行时却按这里的 `enabled=False` 拒绝，页面于是显示"联网检索被策略挡住了"（2026-10-14，
    会话 `af822c1db538` 的 bilibili 那一轮）。两道闸门从此**只剩一道**：能力开关是唯一真源，
    运行期由 `service` 把它回灌到这里（见 `ApplicationService._with_effective_gates`）。

    安全底座不靠这道闸门，而靠三件事（都在，且各有测试）：SSRF 边界（解析出的**每个**地址都查、
    每次跟随重定向**前后**都校验）、外部内容一律按**不可信数据**处理（不得据此授权、不自动进入记忆）、
    结果有界并**如实说明**截断。

    段名是 `web_access` 而不是 `web`：``web`` 已经是**本机 HTTP 服务**的配置段（§15.3），把"改监听端口"
    和"允许联网"混在一起看，早晚会有人以为自己只改了端口。

    **2026-10-14 再进一步**：``web`` 已是**锁定能力**（常态开启、不进可开关集），因此这里的布尔
    **完全退出决策链**——它只剩"运行期由 `service` 回灌"这一个身份，键留着是为了旧配置能原样加载。
    写 ``false`` 不会再关掉联网：那种"写了却没生效"的静默不一致比"关不掉"更坏，所以这条口径必须明说。
    若哪天要恢复"能关"，改法是**同时**放开 ``capabilities.LOCKED_CAPABILITIES`` 与
    ``AppConfig.capability_default_layer`` 里那一行，两处必须一起动。
    """

    enabled: bool = True
    provider: WebAccessProvider = "auto"
    tavily_api_key_env: str = Field(default="TAVILY_API_KEY", min_length=1)
    max_search_calls: int | None = Field(default=None, ge=1)
    max_read_pages: int | None = Field(default=None, ge=1)
    max_reads_per_page: int | None = Field(default=None, ge=1)
    timeout_seconds: int = Field(default=15, gt=0, le=120)
    max_results: int = Field(default=5, ge=1, le=20)
    #: 单次抓取的正文上限（字符）：超过即截断，并在结果 ``note`` 里写明。
    max_chars: int = Field(default=8_000, ge=500, le=100_000)
    #: 搜索后自动抓取前 N 条的正文首段并填进 ``snippet``（2026-10-23）。
    #:
    #: 为什么要它：搜索引擎给的摘要通常只有 50–200 字，模型常常还要再 ``web_fetch`` 两三次；
    #: Tavily 好用的一大原因就是它直接返回抽取过的正文片段。这里用已有的 ``fetch`` 补上这一环。
    #: 0 = 关闭；默认 2：实测每条 0.3–0.8 秒，两条并发后总耗时增加约 1 秒，换来通常省掉一次抓取。
    #: 失败（JS 页面、反爬、超时）一律保留原摘要，不让整次搜索失败。
    prefetch_pages: int = Field(default=2, ge=0, le=5)
    #: 请求头里的 User-Agent。不伪装成浏览器：被对方按 UA 拒绝时，如实失败比假装成 Chrome 更诚实。
    user_agent: str = "chaiai/0.5 (local agent)"
    #: 域名策略（2026-10-14，联网常态化的配套）。``allow`` = 除 `denied_domains` 外一律放行——这是主流
    #: 做法：检索必然遇到未知域名，"默认拦"会让联网寸步难行，所以默认放行、但每次访问都在结果里留下
    #: 可核对的地址与状态；``allowlist`` = 只放行 `allowed_domains` 及其子域（保守档，自建部署用）。
    #: 两档都**不放松** SSRF 边界：本机、内网、链路本地与云元数据地址永远拒绝。
    domain_policy: Literal["allow", "allowlist"] = "allow"
    #: 永远拒绝的域名（含其子域）。
    denied_domains: tuple[str, ...] = ()
    #: ``domain_policy="allowlist"`` 时唯一放行的域名（含其子域）。
    allowed_domains: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _clean_domains(self) -> WebAccessConfig:
        """两个域名清单按 :func:`clean_domain_list` 归一。"""
        self.denied_domains = clean_domain_list(self.denied_domains, "denied_domains")
        self.allowed_domains = clean_domain_list(self.allowed_domains, "allowed_domains")
        return self


class GitConfig(_StrictModel):
    """Git 能力（v0.5 T54；契约 §16.4、工具定义 §6.11）。

    **默认关闭**：打开它等于让 Agent 能读仓库历史（以及经审批后提交），应当是用户的显式决定。
    关闭时 `git_read` / `git_write` **完全不可见**（工具不绑定、提示也不提），行为与 v0.4 一致。

    这里**只有开关与上限**，没有"允许提交哪些分支"之类的策略键：分支保护是 §16.4 的固定规则
    （默认分支上额外确认、永不推送），不引入第二套能被配置放宽的门。
    """

    enabled: bool = False
    #: 单次 git 调用的超时（秒）。超时按失败如实报告，不重试。
    timeout_seconds: float = Field(default=20.0, gt=0, le=300)
    #: 单次输出上限（字符）：超过即截断，并在结果 ``note`` 里写明"结果不完整"。
    max_output_chars: int = Field(default=20_000, ge=500, le=500_000)


class SkillsConfig(_StrictModel):
    """技能能力（v0.5 T55；契约 §16.5、工具定义 §6.12）。

    **默认关闭**：关闭时 `skill` 能力完全不可见（提示里没有清单、工具不绑给模型），行为与 v0.4 一致。

    ``user_dirs`` 接受**绝对路径或相对路径**（2026-10-16 放宽）：相对路径按 **``config.yaml`` 所在
    目录**展开（与记忆库路径同一条口径，见 ``container.resolve_skill_paths``），因此与进程当前目录无关
    ——"相对路径会随 cwd 漂移"这条原来的拒绝理由不再成立。放宽的目的只有一个：**技能要能随包发布**，
    包里的配置写 ``user_dirs: ["skills"]``，解压到哪个盘都能用。
    `~` 仍然**拒绝**（§8：`~` 属于"相对路径逃逸"，不展开）。

    「个人级默认根」在这里**不出现**：它们是代码里的常量表（§16.5 的 rank 表），`include_user_skills`
    只控制"读不读那一组"。市场缓存目录（本机实测一处 295 个技能）**默认不读**，要读就写进 `user_dirs`。
    """

    enabled: bool = False
    #: 是否读取默认的个人级根（`~/.agents/skills` 等，见 §16.5 的 rank 表）。
    include_user_skills: bool = True
    #: 追加的发现根：**绝对路径**照用；**相对路径**按 `config.yaml` 所在目录展开（随包发布用）。
    user_dirs: list[str] = Field(default_factory=list)
    #: 单个技能正文上限（字符）：超过即截断，并在结果 ``note`` 里写明原文长度。
    max_skill_chars: int = Field(default=20_000, ge=500, le=500_000)
    #: 清单上限（字符）：超过即截断并如实标注（§6.7 口径）。
    max_list_chars: int = Field(default=8_000, ge=200, le=100_000)

    @model_validator(mode="after")
    def _user_dirs_are_portable(self) -> SkillsConfig:
        """相对路径允许（相对 `config.yaml` 所在目录），但 `~` 与空项仍然拒绝。

        空项要拒：它会解析成"配置文件所在目录自己"，于是把整个仓库当成发现根扫一遍——
        那不是用户想表达的意思。`~` 要拒：`resolve_under` 不展开它（§8），放过去会得到一个
        名为 `~` 的目录，静默地什么都没发现。
        """
        for value in self.user_dirs:
            text = str(value).strip()
            if not text:
                raise ValueError("skills.user_dirs 里有空项（§16.5）：删掉它或写成目录名")
            if text.startswith("~"):
                raise ValueError(
                    "skills.user_dirs 不接受 `~`（§16.5：`~` 属于相对路径逃逸，不展开）："
                    f"{value!r}。请写成绝对路径，或写成相对 `config.yaml` 所在的目录名"
                )
        return self


class McpConfig(_StrictModel):
    """MCP 客户端的**运行参数**（架构 §6.13；2026-10-20）。

    这里**没有** server 列表：server 的连接方式在独立的 ``mcp.json``（与 CodeBuddy 同格式，
    可整份粘贴），server 的开关与工具白/黑名单是用户状态、存设置库。三份东西分开的理由是
    "部署配置 / 用户状态 / 运行参数"三者的生命周期不同，混在一起会让升级与备份互相牵连。

    **默认关闭**：``enabled`` 为假时不连任何 server、``mcp`` 能力不出现，行为与接入前一致。
    """

    enabled: bool = False
    #: 直接绑定给模型的工具上限。其余走检索——这是"工具有几千个也不会把提示撑爆"的主闸。
    max_direct_tools: int = Field(default=20, ge=0, le=200)
    #: 同时在连的 server 数上限。50 个 npx+node 常驻会吃掉数 GB 内存，必须排队。
    max_connected_servers: int = Field(default=4, ge=1, le=64)
    #: 空闲多久断开子进程（秒）。**0 表示不回收**：只在内存充裕的机器上这么设，
    #: 否则几十台 server 的常驻进程会拖垮整机。
    idle_timeout_seconds: int = Field(default=600, ge=0, le=86_400)
    #: 单台 server 的启动（握手）超时。``npx -y`` 首次要下载包，给到 30 秒。
    startup_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    #: 单次 ``tools/call`` 超时。超时按失败如实报告，**不重试**。
    call_timeout_seconds: float = Field(default=60.0, gt=0, le=600)
    #: 单条工具定义（描述 + 参数 schema）的字节上限。超限的工具直接不可见（照 Codex 的字节闸：
    #: 字节在生成时就知道，token 要估，前者更早失败、没有估算偏差这种含糊地带）。
    max_tool_schema_bytes: int = Field(default=8_192, ge=256, le=1_000_000)
    #: 单次检索返回的工具条数（照 Codex ``tool_search`` 的默认 8）。
    max_search_results: int = Field(default=8, ge=1, le=50)


class ProfilesConfig(_StrictModel):
    """Agent Profile（v0.5 T50；契约 §16.1）。

    ``active`` 是当前生效的 Profile；``custom`` 用来在不改代码的前提下定义自己的组合
    （名字 → 能力列表）。校验在**配置加载阶段**完成：

    - ``active`` 必须存在（内置或自定义）；
    - 能力名必须在架构 §6.9 的闭集内；
    - 能力列表不能为空、不能重复；
    - ``custom`` 不得覆盖内置名——内置三档是验收与页面的固定参照，被覆盖后"当前是哪个 Profile"
      就说不清了。

    宁可启动失败也不静默忽略：配错名字的结果是"看起来配了、实际用默认档"，而这类问题只在模型
    **该调用某个工具却没调用**时才显形，定位成本极高。
    """

    active: str = DEFAULT_PROFILE
    #: 自定义 Profile：名字 → 能力列表（顺序即工具绑定顺序）。空表示只用内置三档。
    custom: dict[str, list[str]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_profiles(self) -> ProfilesConfig:
        shadowed = sorted(name for name in self.custom if name in PROFILES)
        if shadowed:
            raise ValueError(
                "profiles.custom 不能覆盖内置 Profile："
                + "、".join(shadowed)
                + f"（内置：{'、'.join(profile_names())}）"
            )
        for name, capabilities in self.custom.items():
            if not capabilities:
                raise ValueError(f"profiles.custom.{name} 的能力列表不能为空")
            duplicated = duplicated_capabilities(capabilities)
            if duplicated:
                raise ValueError(f"profiles.custom.{name} 的能力重复：" + "、".join(duplicated))
            unknown = unknown_capabilities(capabilities)
            if unknown:
                raise ValueError(
                    f"profiles.custom.{name} 里有未登记的能力："
                    + "、".join(unknown)
                    + f"（可用能力：{'、'.join(capability_names())}）"
                )
        if self.active not in PROFILES and self.active not in self.custom:
            available = "、".join(profile_names())
            if self.custom:
                available += "；自定义：" + "、".join(sorted(self.custom))
            raise ValueError(f"profiles.active={self.active} 不存在（可用：{available}）")
        return self

    def capabilities(self) -> tuple[str, ...]:
        """当前 Profile 的能力集合（校验已保证非空且全部已登记）。"""
        return resolve_capabilities(self.active, self.custom)


class JobsConfig(_StrictModel):
    """后台任务运行时（v0.6 T58；契约 §17.2）。

    **这不是能力开关**：``background`` 只决定**调度时机**，不决定走哪条代码路径——
    ``false`` 时这次执行在提交请求内跑到终态（与 v0.5 逐字节一致），``true`` 时先登记任务并立即返回，
    由本机 worker 在后台跑到终态或"等待用户"。命名刻意不用能力开关惯用的 ``enabled``，因为那个名字在
    ``web_access`` / ``git`` / ``skills`` 上表示"关掉就不可用"，用在这里会被读成同一个意思。

    只放"调度时机"与"资源上限"：取消的语义见 §17.4，恢复与"需人工核对"属 T61。
    """

    #: 是否把执行交给本机 worker（默认否：保持 v0.5 的请求内执行）。
    background: bool = False
    #: 后台 worker 的并发上限。本机单用户、单进程，默认 1——不引入队列公平性之类的第二套语义。
    max_concurrent_jobs: int = Field(default=1, ge=1, le=8)
    #: 单任务总墙钟上限（秒）；``None`` = 不限（默认）。超限走**与取消完全相同的那一条路径**
    #: （置取消信号 + 任务进 `cancelled`，`detail` 写明原因，见 §17.4），不另写一套中止逻辑。
    max_task_seconds: float | None = Field(default=None, gt=0)


class WorkflowsConfig(_StrictModel):
    """确定性工作流的实际资源上限（v0.7 T65/T66，架构 §18.2）。"""

    enabled: bool = False
    max_tool_calls: int = Field(default=256, ge=1)
    max_steps: int = Field(default=512, ge=1)
    max_source_files: int = Field(default=128, ge=1)
    max_evidence_chars: int = Field(default=256_000, ge=1)
    max_report_chars: int = Field(default=256_000, ge=1)
    max_model_tokens: int = Field(default=1_048_576, ge=1)
    max_model_output_tokens: int = Field(default=16_384, ge=1)
    max_children: int = Field(default=8, ge=1)
    max_concurrency: int = Field(default=4, ge=1)
    child_timeout_seconds: int = Field(default=900, ge=1)


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
    profiles: ProfilesConfig = Field(default_factory=ProfilesConfig)
    web_access: WebAccessConfig = Field(default_factory=WebAccessConfig)
    git: GitConfig = Field(default_factory=GitConfig)
    skills: SkillsConfig = Field(default_factory=SkillsConfig)
    #: MCP 客户端（2026-10-20）。默认关闭；server 列表不在这里，见同目录 ``mcp.json``。
    mcp: McpConfig = Field(default_factory=McpConfig)
    jobs: JobsConfig = Field(default_factory=JobsConfig)
    workflows: WorkflowsConfig = Field(default_factory=WorkflowsConfig)

    def capability_default_layer(self) -> dict[str, bool]:
        """**出厂默认开关表**（架构 §16.1）：用户没在页面改过开关时生效的那一组。

        两个来源合起来，顺序照 :data:`~local_agent.capabilities.CAPABILITY_TOOLS`：

        - 五个基础能力取自 ``profiles.active`` 的声明（``coder`` 全开，``reviewer`` 只有只读）；
        - ``git_read`` / ``git_write`` / ``skill`` 取**各自段的 ``enabled`` 且档位也声明了它**——
          与 v0.5 的"档位 ∩ 配置开关"**逐项等价**，所以在一份什么都没改的配置上，行为与升级前一致；
        - ``web`` **恒定取真**（2026-10-14 起联网常态化，见 :data:`~local_agent.capabilities.LOCKED_CAPABILITIES`）。

        Returns:
            能力名 → 布尔；顺序与闭集一致。**不可关闭能力的强制置真不在这里**，它在
            :func:`~local_agent.capabilities.resolve_switches` 的最后一道，避免两个地方各写一次。
        """
        declared = as_switch_layer(resolve_capabilities(self.profiles.active, self.profiles.custom))
        layer = dict(declared)
        # 联网是**常态化能力**（2026-10-14）：出厂默认一律为真，既不取决于档位声明，也不取决于
        # `web_access.enabled`——`resolve_switches` 最后还会强制置真，这里写真话是为了让这张
        # "出厂默认表"本身不再自相矛盾（否则它写着 false、实际跑着 true）。
        layer["web"] = True
        git_on = declared["git_read"] and self.git.enabled
        layer["git_read"] = git_on
        layer["git_write"] = git_on
        layer["skill"] = declared["skill"] and self.skills.enabled
        # MCP **不在任何内置档位里**（它取决于用户自己配了哪些 server），所以出厂默认只由
        # `mcp.enabled` 决定，不看档位声明。页面上改过开关之后，以设置库里的选择为准（§16.1）。
        layer["mcp"] = self.mcp.enabled
        return {name: layer[name] for name in CAPABILITY_TOOLS}

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


_ConfigT = TypeVar("_ConfigT", bound=AppConfig)


def apply_capability_gates(app_config: _ConfigT, switches: Mapping[str, bool]) -> _ConfigT:
    """所有运行入口共用能力开关的生效值，保留其余网络/安全配置。"""
    return app_config.model_copy(update={
        "web_access": app_config.web_access.model_copy(update={"enabled": bool(switches.get("web"))}),
        "git": app_config.git.model_copy(update={
            "enabled": bool(switches.get("git_read") or switches.get("git_write")),
        }),
        "skills": app_config.skills.model_copy(update={"enabled": bool(switches.get("skill"))}),
        # MCP 的总闸跟能力开关走（与 web/git/skills 同构）：`config.yaml` 的 `mcp.enabled` 只是出厂默认。
        "mcp": app_config.mcp.model_copy(update={"enabled": bool(switches.get("mcp"))}),
    })


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
