"""HTTP 请求与响应形状（T29；契约 §6.2、§15.1）。

请求模型**只描述传输层**：`decision` / `edited_arguments` / `reason` 原样透传给应用服务，
因为"什么算合法决定"的唯一裁定权在服务里（`APPROVAL_DECISIONS`）；本层只挡下**形状**问题
（未知字段、空输入、edit 缺参数），它们返回 422，而不是业务错误 DTO。

响应模型一律是 T27 的 DTO（`application/models.py`），本层不新增业务字段——前端类型从
OpenAPI 生成，多一个私有字段就等于在契约里加了一条 UI 私有通道（§15.1 明令禁止）。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from local_agent.application.errors import ErrorView
from local_agent.application.models import (
    SCHEMA_VERSION,
    ExternalPathAccess,
    ExternalPathsMode,
)
from local_agent.attachments import MAX_ATTACHMENTS_PER_MESSAGE
from local_agent.workflow_contracts import WorkflowRequest

#: 会话名与请求标识的长度上限：够用即可，真正的截断规则在会话库里。
TITLE_MAX_CHARS = 1000
REQUEST_ID_MAX_CHARS = 128
MESSAGE_MAX_CHARS = 100_000
#: 一条消息最多带几份文档（2026-10-29）。图片数量不设配额，文档设一个：
#: 每一份都是一次解析 + 一段提示，塞十几个只会稀释上下文。
MAX_FILES_PER_MESSAGE = 5
PATH_MAX_CHARS = 4096
REASON_MAX_CHARS = 2000
#: 提问答复的长度上限（与拒绝理由同值，2026-10-28）；超出部分由请求校验挡掉，不让它进 State。
QUESTION_ANSWER_MAX_CHARS = 2000
#: 服务凭据（Tavily）的长度上限：常见密钥是几十个字符，512 只用来挡住"粘进来一整篇文档"。
KEY_MAX_CHARS = 512


class _Request(BaseModel):
    """请求基类：未知字段**拒绝**（打错字段名不能静默忽略）。"""

    model_config = ConfigDict(extra="forbid")


class RenameRequest(_Request):
    """会话与工作区的显示名称；不改变目录名或会话绑定。"""

    title: str = Field(min_length=1, max_length=60)

    @field_validator("title")
    @classmethod
    def _nonblank(cls, value: str) -> str:
        title = " ".join(value.split())
        if not title:
            raise ValueError("名称不能为空")
        return title


class SelectWorkspaceRequest(_Request):
    """选择工作区：`workspace_id`（已列出的工作区）与 `path`（新目录）二选一。"""

    workspace_id: str | None = Field(default=None, min_length=1, max_length=REQUEST_ID_MAX_CHARS)
    path: str | None = Field(default=None, min_length=1, max_length=PATH_MAX_CHARS)

    @model_validator(mode="after")
    def _exactly_one(self) -> SelectWorkspaceRequest:
        if (self.workspace_id is None) == (self.path is None):
            msg = "workspace_id 与 path 必须二选一"
            raise ValueError(msg)
        return self


class CreateSessionRequest(_Request):
    """新建会话；两项都可以缺省（沿用库的启动默认工作区、自动命名）。"""

    title: str | None = Field(default=None, max_length=TITLE_MAX_CHARS)
    workspace_id: str | None = Field(default=None, min_length=1, max_length=REQUEST_ID_MAX_CHARS)
    mode: Literal["chat", "agent"] = "agent"


class SessionModeRequest(_Request):
    mode: Literal["chat", "agent"]
    expected_mode: Literal["chat", "agent"]


class RecoveryRequest(_Request):
    request_id: str = Field(min_length=1, max_length=REQUEST_ID_MAX_CHARS)
    expected_snapshot_id: str = Field(min_length=1, max_length=REQUEST_ID_MAX_CHARS)
    confirm_reviewed: Literal[True]


class CancelTaskRequest(_Request):
    """取消当前会话在跑的任务（架构 §15.2、§17.4）。**幂等**：没有可取消的任务时是空操作。

    刻意**不带** `client_request_id`：取消不进幂等台账（重复调用本来就是空操作），
    要求一个用不上的幂等键只会让调用方以为"取消也会被去重"。
    """

    reason: str = Field(default="", max_length=200)


class SubmittedFileRef(_Request):
    """随消息提交的一份文档引用（2026-10-29）。

    `name` 是**用户上传时的文件名**，只用于展示（气泡与出站提示）；服务端会净化它
    （去路径段、去控制字符、限长），为空时回退到落盘名。落盘名永远是内容哈希，
    不能由客户端指定——那是路径穿越与覆盖写入的入口。
    """

    id: str = Field(min_length=1, max_length=300)
    name: str = Field(default="", max_length=300)


class SubmitMessageRequest(_Request):
    """提交消息。`client_request_id` 是幂等键（§6.1），由前端生成并复用。"""

    text: str = Field(default="", max_length=MESSAGE_MAX_CHARS)
    client_request_id: str = Field(min_length=1, max_length=REQUEST_ID_MAX_CHARS)
    #: 随这条消息一起交给模型的图片（2026-10-16）：**引用**（上传接口返回的 `attachment_id`），
    #: 不是字节——字节留在 `data/attachments/` 里，消息里只放能定位它的相对路径。
    #: 默认空 ⇒ 老客户端照常工作；上限与存储层同一条口径（`MAX_ATTACHMENTS_PER_MESSAGE`）。
    attachments: list[str] = Field(
        default_factory=list, max_length=MAX_ATTACHMENTS_PER_MESSAGE
    )
    #: 随这条消息一起交给模型的**文档**（2026-10-29）：同样是引用，不是字节。
    #: 与图片分成两个字段而不是混在一个列表里，因为下游处理完全不同——图片要被物化成
    #: `data:` URL 进内容块，文档只把绝对路径写进提示、由模型自己 `read_file` 读。
    files: list[SubmittedFileRef] = Field(default_factory=list, max_length=MAX_FILES_PER_MESSAGE)
    workflow: WorkflowRequest | None = None
    parent_turn_id: str | None = Field(default=None, min_length=1, max_length=REQUEST_ID_MAX_CHARS)
    expected_branch_id: str | None = Field(default=None, min_length=1, max_length=REQUEST_ID_MAX_CHARS)
    expected_snapshot_id: str | None = Field(default=None, min_length=1, max_length=REQUEST_ID_MAX_CHARS)

    @model_validator(mode="after")
    def _text_or_attachments(self) -> SubmitMessageRequest:
        """文字与图片**至少有一个**（2026-10-16）。

        `text` 因此允许为空：只贴一张图、一个字不打也是合法提问（"看看这是什么"）。但不能两者都空：
        那样提交的是一条**什么都不含**的消息，模型没有可回应的内容，用户也看不出自己漏了什么。
        """
        if not self.text.strip() and not self.attachments and not self.files:
            msg = "text、attachments 与 files 至少要有一个"
            raise ValueError(msg)
        return self


class ArrangeBranchRequest(_Request):
    title: str = Field(min_length=1, max_length=80)
    position: int = Field(ge=0)


class CreateSideChatRequest(_Request):
    """开侧聊（§21.7）：只传`从哪一轮开出`，上下文由服务端读。"""

    #: 缺省表示取父会话的最新一轮。
    origin_turn_id: str | None = Field(default=None, max_length=64)


class ResolveTaskReviewRequest(_Request):
    """人工核对结案（§17.1 唯一例外）。"""

    #: `completed`（核对后视为已完成）或 `cancelled`（确认没做完 / 放弃）。
    outcome: Literal["completed", "cancelled"]


class QuestionAnswerItem(_Request):
    """题组里某一题的答复（2026-10-28）：`index` 对齐 `QuestionView.items`，空 `answer` = 该题跳过。"""

    index: int = Field(ge=0, le=4)
    answer: str = Field(default="", max_length=QUESTION_ANSWER_MAX_CHARS)


class QuestionAnswerRequest(_Request):
    """提问答复（2026-10-28，架构 §15.3）。

    **一次提交整组**：`answers` 是逐题结果，用户翻页答完点一次"提交"。`answer` 保留给单题写法
    （只问了一题时）。空答复、越界 `index` 与未知 `action` 一律按"跳过"处理（失败闭合），与
    图内节点的判定同一口径——两处各判一次会造出两个真相。
    """

    action: Literal["answer", "skip"] = "answer"
    answer: str | None = Field(default=None, max_length=QUESTION_ANSWER_MAX_CHARS)
    answers: list[QuestionAnswerItem] | None = None
    client_request_id: str = Field(min_length=1, max_length=REQUEST_ID_MAX_CHARS)


class ApprovalDecisionRequest(_Request):
    """审批决定。

    `edit` 必须带 `edited_arguments`（否则服务不知道该执行什么）；反过来，非 `edit` 带参数
    会被拒绝——那会让"用户改了参数"与"实际执行的参数"出现两份不一致的真相（§15.7）。
    """

    decision: Literal["approve", "edit", "reject"]
    client_request_id: str = Field(min_length=1, max_length=REQUEST_ID_MAX_CHARS)
    edited_arguments: dict[str, Any] | None = None
    reason: str = Field(default="", max_length=REASON_MAX_CHARS)
    confirm_recursive: bool = False

    @model_validator(mode="after")
    def _edit_arguments_match_the_decision(self) -> ApprovalDecisionRequest:
        if self.decision == "edit" and not self.edited_arguments:
            msg = "decision=edit 必须给出 edited_arguments"
            raise ValueError(msg)
        if self.decision != "edit" and self.edited_arguments:
            msg = "只有 decision=edit 才能带 edited_arguments"
            raise ValueError(msg)
        return self


CONNECTION_NAME_MAX_CHARS = 60
MODEL_ID_MAX_CHARS = 200
SECRET_MAX_CHARS = 4096


class AttachmentUploadRequest(_Request):
    """上传一张图片（2026-10-16）。

    为什么是 **base64 的 JSON** 而不是 multipart：后者要给运行时（以及打包）多加一个
    `python-multipart` 依赖，而 4 MB 上限下 base64 的 33% 开销无所谓；契约与生成类型也干净。

    **没有 media_type 字段**是刻意的：客户端声明与扩展名一样不可信（`报告.png` 完全可能是 HTML），
    媒体类型一律由服务端按**魔数**判定。
    """

    #: 图片字节的 base64（标准编码，带 padding）。上限按"解码后 ≤ 4 MB"卡，
    #: 这里给一个略大的字符上限，免得把超大请求体先读进内存再拒绝。
    data_base64: str = Field(min_length=1)


class OssCredentialRequest(_Request):
    """对象存储的访问密钥（2026-10-20）。

    形状只挡空值与超长；内容不做任何校验（与 Tavily 同一条口径）。写入即整体覆盖，
    页面**不回读**：只回答"已配置 / 未配置"。
    """

    access_key: str = Field(min_length=1, max_length=KEY_MAX_CHARS)
    secret_key: str = Field(min_length=1, max_length=KEY_MAX_CHARS)


class OssCredentialView(BaseModel):
    """对象存储密钥状态：只报有没有，不回读任何一位密钥。"""

    configured: bool
    store: Literal["os", "local", "memory"] | None = None
    note: str | None = None


class OssTestResult(BaseModel):
    """「测试对象存储」的结果：能否写入、公开地址能否访问。"""

    ok: bool
    detail: str


class ModelConnectionRequest(_Request):
    """新增 / 编辑一条模型连接（§6.8）。

    请求体**只描述连接字段**：没有 `credential_ref`（引用由服务端生成）、没有连接 id
    （路径里给）、也没有任何能改安全策略、工具权限或 Graph 配置的入口——模型设置不是提权通道。
    `api_key` 是"本次录入的密钥"，不填表示不改密钥（编辑时不重填不会把已保存的 Key 抹掉）。
    """

    display_name: str = Field(min_length=1, max_length=CONNECTION_NAME_MAX_CHARS)
    base_url: str = Field(min_length=1, max_length=PATH_MAX_CHARS)
    model: str = Field(min_length=1, max_length=MODEL_ID_MAX_CHARS)
    #: 上下文窗口**必填**：§6.8 要求由用户填写或由可信检测确认，不能猜。
    context_window_tokens: int = Field(gt=0)
    max_tokens: int = Field(default=4096, gt=0)
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    timeout_seconds: int = Field(default=120, gt=0)
    retry_count: int = Field(default=2, ge=0, le=10)
    streaming: bool = False
    #: 用户声明：**这条连接支持图片输入**（2026-10-16）。默认关（失败闭合）——勾错会让每次带图的
    #: 请求失败，所以页面上必须写清"不确定就别勾"。它是**声明**，与 `capabilities` 那组**实测报告**
    #: 分开：收不收图只能靠发图试，测不出来与不支持是两回事。
    supports_vision: bool = False
    auth_mode: Literal["api_key", "none"] = "api_key"
    #: 用户对"非回环明文 HTTP"的明确确认（§6.8 第 5 条）。
    allow_plaintext_http: bool = False
    api_key: str | None = Field(default=None, max_length=SECRET_MAX_CHARS)
    #: 客户端手上的版本号：**编辑必填**（防两个页面互相覆盖，§6.1）。
    expected_version: int | None = Field(default=None, ge=1)


class SelectionExpectation(_Request):
    """调用方**看到的当前选择**，用于选择写入的乐观校验（§6.1）。

    `connection_id=None` 表示"我当时看到这个作用域没有选择"（全新安装还没有默认，
    或某个会话跟随默认）。两个字段要么都给、要么都给 `None`——只给一半说明调用方自己没想清楚。
    """

    connection_id: str | None = Field(min_length=1, max_length=REQUEST_ID_MAX_CHARS)
    config_version: int | None = Field(ge=1)

    @model_validator(mode="after")
    def matching_fields(self) -> SelectionExpectation:
        if (self.connection_id is None) != (self.config_version is None):
            raise ValueError("connection_id 与 config_version 要么都给值、要么都给 null")
        return self


class SelectDefaultConnectionRequest(_Request):
    """设置新会话继承的默认连接。

    `expected_selection` **必填**：不填就等于"我不管别人改了什么，覆盖掉"，
    那正是 §6.1 要避免的两个页面互相覆盖。
    """

    connection_id: str = Field(min_length=1, max_length=REQUEST_ID_MAX_CHARS)
    expected_selection: SelectionExpectation
    #: 目标连接你看到的版本；不符说明这条连接在你选之前已被改过。
    expected_version: int = Field(ge=1)


class SelectSessionModelRequest(_Request):
    """为某个会话选择**下一轮**用的连接；`connection_id=None` 表示恢复为跟随默认。

    `expected_selection` 对"恢复跟随默认"同样是必填的（那也是一次写入）。
    """

    connection_id: str | None = Field(default=None, min_length=1, max_length=REQUEST_ID_MAX_CHARS)
    expected_selection: SelectionExpectation
    expected_version: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def target_version_matches_connection(self) -> SelectSessionModelRequest:
        if (self.connection_id is None) != (self.expected_version is None):
            raise ValueError("选中连接时必须给目标版本；恢复跟随默认时不应给目标版本")
        return self


class ErrorResponse(BaseModel):
    """统一错误信封：业务错误一律 `{"error": ErrorView}`（§15.5）。

    响应体刻意**不是** `{"detail": ...}`：那会和 FastAPI 自己的 422/403 撞形状，前端就分不清
    "业务错误"和"传输层拒绝"了。
    """

    error: ErrorView


class HealthView(BaseModel):
    """健康检查：只说可用性，不含路径、密钥或内部标识（§15.8）。

    - `model_configured=false` 是初始设置模式的正常状态（§15.9）；
    - `credential_store` 让**设置页**在还没有任何连接时也能说清"密钥重启后还在不在"
      （§6.8 要求凭据存储不可用时必须明确提示，而不是等用户录完密钥才发现）。
    """

    status: Literal["ok"] = "ok"
    schema_version: int = SCHEMA_VERSION
    model_configured: bool
    model_name: str
    #: `local` = 持久，密钥在**本地设置库**里（2026-10-13 起的默认，§6.8）；
    #: `os` = 历史值（系统凭据存储，v1.0 之前默认，现无代码路径产出）；
    #: `memory` = 仅本进程内有效（重启后需重新录入）。
    credential_store: Literal["os", "local", "memory"] = "memory"
    #: 降级原因；持久存储（`local` / `os`）时为 `null`。
    credential_note: str | None = None
    #: 只说到"本地库"这一层：具体的后端标识（配置里的 `sqlite_checkpoint`）属内部细节，
    #: 不该固化进公开契约（前端也不需要）。
    storage: Literal["sqlite"] = "sqlite"
    static_frontend: bool


# ---- 应用身份与维护（2026-10-27，设置页「关于」）--------------------------


class BuildInfoView(BaseModel):
    """`BUILD_INFO.txt` 的摘要：打包机写入，让"这个包对应哪份代码"可以复核。"""

    built_at: str
    revision: str
    dirty: str
    python: str


class AppInfoView(BaseModel):
    """「关于」页要显示的**本机事实**。

    路径是绝对路径（与 `PickDirectoryView` 同一口径）：这一页就是给用户核对"装在哪儿、
    数据在哪儿"用的，隐藏路径等于把最有用的信息删掉。注意它与 `/health` 的约束不同——
    那里要公共可用性摘要，这里只给本机设置页看。
    """

    version: str
    #: `installed` 安装版 / `portable` 绿色包 / `source` 源码。
    install_kind: Literal["installed", "portable", "source"]
    #: 源码形态为 `null`（没有"安装目录"这回事）。
    install_dir: str | None = None
    data_root: str
    data_root_source: Literal["environment", "settings", "config", "default"]
    build: BuildInfoView | None = None
    #: 卸载入口可不可点；不可点时 `uninstall_note` 必须说明下一步怎么做。
    uninstall_available: bool
    uninstall_note: str | None = None
    #: 「同时删除数据」勾选框能不能勾；不能时 `remove_data_note` 说明原因。
    can_remove_data: bool
    remove_data_note: str | None = None


# ---- 委派实时进度（2026-11-04）-------------------------------------------


class DelegationLiveStepView(BaseModel):
    """子任务已经发起过的一次工具调用。

    **不是审计记录**：它只回答"这一路此刻在查什么"，跑完就被 `DelegationChildView` 里的
    统计取代。所以字段刻意少（没有 token、没有证据编号）。
    """

    name: str
    #: 参数里最能说明"在查哪"的那一个（路径 / 命令 / 模式串）。取不到就是空串，不编造。
    target: str
    status: str


class DelegationLiveLaneView(BaseModel):
    """一路正在跑（或刚跑完、还在保留期内）的子任务。"""

    child_id: str
    scope: str
    #: 这一路要查的问题（截断）。块头显示它，比目录路径有信息量——见 `DelegationChildView.goal`。
    goal: str = ""
    status: str
    steps: list[DelegationLiveStepView] = Field(default_factory=list)
    updated_at: float


class DelegationLiveView(BaseModel):
    lanes: list[DelegationLiveLaneView] = Field(default_factory=list)


# ---- 移动访问（2026-11-03，设置页「移动访问」，架构 §24）-------------------


class MobileInfoView(BaseModel):
    """移动网关的**只读**状态：只回答"开着没、跑着没、配了几台、去哪儿管"。

    **刻意不含任何操作**：生成配对码与撤销设备都在网关自己的管理页（`admin_url`）上。
    主服务不代它转发——§24 的"主服务不为网关开例外"因此仍然成立，跨源请求也一步都不用放。
    """

    #: 配置里 `mobile.enabled` 的当前值。`false` 时其余字段都是"本来会是什么"。
    enabled: bool
    #: 网关进程此刻是否真的在监听。**与 `enabled` 分开**：配了但没启动是最常见的一态，
    #: 两者合成一个布尔会让用户看到"开着"却连不上。
    running: bool
    #: 管理页地址（回环）。它只在本机能打开。
    admin_url: str
    #: 已配对设备数。读的是网关落盘的状态，网关没跑时也能显示——那时它才是用户关心的数字。
    device_count: int


class MobileDeviceView(BaseModel):
    """一台已配对设备。**不含 Token 摘要**：页面不需要它，泄露了也没用。"""

    id: str
    label: str
    #: 配对时走的通道名（`pair` / `lan` …）。
    channel: str
    created_at: str


class MobileDevicesView(BaseModel):
    devices: list[MobileDeviceView]


class MobilePairUrlView(BaseModel):
    """一个手机可达地址。`label` 是给人看的（"同一 Wi-Fi" / "远程（Tailscale）"）。"""

    label: str
    url: str


class MobilePairingCodeView(BaseModel):
    """一次配对邀请：给手机扫的链接与它的二维码。

    ``qr_svg`` 由服务端生成并内联返回——**不要把 code 拼进图片 URL**，那样这个一次性凭据会
    出现在访问日志里。
    """

    code: str
    pair_url: str
    qr_svg: str
    #: 剩余有效秒数。页面据此做倒计时，过期后提示重新生成。
    expires_in: int
    #: 备选地址（不含 `pair_url` 自己）。某条通道"配了但没生效"时，第一个链接会打不开——那时
    #: 用户需要一个能点的退路，而不是只看到"扫了没反应"。
    alternates: list[MobilePairUrlView] = []


class MobileRenameRequest(BaseModel):
    """改设备显示名（UA 解析出来的型号代号用户认不出，允许自己起名）。"""

    label: str


class MobileRevokeView(BaseModel):
    #: 是否真的删掉了一条记录（找不到时 `false`）。
    revoked: bool


class MobileChannelSettingView(BaseModel):
    """页面上的一行通道配置。**与 `MobileChannelConfig` 同字段**——它就是那份配置的可编辑形态。"""

    type: str
    enabled: bool
    mode: Literal["named", "quick"]
    hostname: str
    expose: Literal["serve", "funnel"]
    https_port: int
    port: int
    tls: bool


class MobileSettingsView(BaseModel):
    """移动访问的**生效值**：设置库里有就用库里的，否则用 `config.yaml`。

    `source` 与 `restart_required` 是给界面看的，不参与计算：前者说明"你看到的这份来自哪儿"，
    后者说明"改完之后网关进程还没重启，此刻还没生效"——这两句不写清楚，用户会以为保存没成功。
    """

    enabled: bool
    gateway_host: str
    gateway_port: int
    pairing_ttl_seconds: int
    ws_enabled: bool
    channels: list[MobileChannelSettingView]
    #: `settings` = 页面保存过的值；`config` = 来自 config.yaml。
    source: Literal["settings", "config"]
    #: 网关进程是否还在用旧配置（保存后恒为 true，重启网关才清）。
    restart_required: bool


class MobileGatewayActionView(BaseModel):
    """启停网关的结果。`detail` 是**可以直接展示给用户**的句子。

    `ok=False` 时界面要如实显示失败原因，而不是给一个"已完成"的提示——"我关的是不是真关了"
    这个问题，在紧急场景下比任何别的信息都重要。
    """

    ok: bool
    detail: str
    running: bool
    device_count: int


class MobileEmergencyStopRequest(BaseModel):
    """紧急关闭。

    `revoke_devices` 单独给：**停止网关只断开连接，设备 Token 仍然有效**——重新开启后那台手机
    照样能用。怀疑令牌泄露时必须连设备一起清掉，否则"关过一次"只是心理安慰。
    """

    revoke_devices: bool = False


class MobileSettingsRequest(BaseModel):
    """保存整段移动访问配置。

    **整段提交而不是逐项**：页面就是一张表单，逐项保存会让"改端口 + 关通道"变成多次写入，
    中间失败一次就留下一份自相矛盾的配置。
    """

    enabled: bool
    gateway_port: int
    pairing_ttl_seconds: int
    ws_enabled: bool
    channels: list[MobileChannelSettingView]


class UpdateCheckView(BaseModel):
    """一次检查更新的结果。**查不到不算错误**：`unavailable` + `note` 就是完整答复。"""

    status: Literal["update_available", "up_to_date", "unavailable"]
    current_version: str
    latest_version: str | None = None
    #: GitHub Release 页面（不是 API 地址）：前端直接交给系统浏览器打开。
    release_url: str | None = None
    published_at: str | None = None
    note: str | None = None


class UninstallRequest(_Request):
    """卸载请求。

    `remove_data=False` 是默认，也是**老约定**（`packaging/README.md` 的"用户数据不归安装包管"）：
    传 `true` 是用户在弹窗里显式勾选过"同时删除数据"，不是服务端替他决定。
    """

    remove_data: bool = False


class UninstallView(BaseModel):
    """卸载已交出去（202 + `scheduled=true`）：进程随后会被脚本结束，前端不该再发请求。"""

    scheduled: bool
    remove_data: bool
    data_dir: str
    message: str


class PickDirectoryView(BaseModel):
    """本机目录选择器的结果：`path` 是选中的**绝对路径**，用户取消时为 `null`。

    刻意**不做成业务 DTO**（不放 `application/models.py`）：弹一个原生窗口是宿主进程的能力，
    不是应用服务的用例——应用服务只认"按路径登记工作区"，对"路径怎么来的"没有意见。

    取消用 `200 + null` 表达而不是错误：取消是正常操作，不该被读成失败、也不该改变任何状态。
    """

    path: str | None = None


# ---- 能力开关（§16.1、§16.6）------------------------------------------------


class CapabilitiesRequest(_Request):
    """改能力开关（§16.1、§16.6）。

    只报告**这次动过的那几项**（合并式）：页面点一下开关就发一项，其余保持原样。取值是否合法由领域层
    裁定（未登记的能力名给 422，理由里带上可开关集）——传输层不另立一套能力名，与
    `PermissionsModeRequest` 同一条口径。
    """

    changes: dict[str, bool] = Field(min_length=1)


# ---- 打开方式（2026-11-03，架构 §15.11.4）----------------------------------


class OpenInAppOpenRequest(_Request):
    """用本机的某个应用打开工作区里的一份文件。

    ``app_id`` 只能是服务端目录里的 id（探测结果），**不是**可执行文件路径——客户端传什么
    命令过来都一律拒绝；启动参数也全部来自服务端目录。``path`` 与预览同一条边界。
    """

    app_id: str = Field(min_length=1, max_length=40, pattern=r"^[a-z0-9_-]+$")
    path: str = Field(min_length=1, max_length=PATH_MAX_CHARS)
    workspace_id: str | None = Field(default=None, max_length=REQUEST_ID_MAX_CHARS)


# ---- 文件改动还原（2026-10-29）---------------------------------------------


class RestoreChangesRequest(_Request):
    """把文件改动退回某个时间点。

    这里只校验**形状**：``scope=file`` 要 ``path``，``scope=turn`` 要 ``run_id``。至于
    "这一轮到底有没有改动可还原""账本有没有接线"，属于业务不成立，由服务返回带错误码的信封
    （``CHANGES_RESTORE_UNAVAILABLE``）——那时用户需要的是原因和下一步，不是"字段不合法"。
    """

    scope: Literal["file", "turn"]
    path: str | None = Field(default=None, max_length=PATH_MAX_CHARS)
    run_id: str | None = Field(default=None, max_length=REQUEST_ID_MAX_CHARS)
    #: 整轮里只还原这部分文件；``None`` = 该轮全部。
    paths: list[str] | None = Field(default=None, max_length=500)
    #: 文件在 AI 写完之后被人改过时，是否仍然覆盖。默认不覆盖（保留了别处的手工修改）。
    force: bool = False

    @model_validator(mode="after")
    def _required_by_scope(self) -> RestoreChangesRequest:
        if self.scope == "file" and not (self.path or "").strip():
            raise ValueError("还原单个文件需要提供 path")
        if self.scope == "turn" and not (self.run_id or "").strip():
            raise ValueError("还原本轮需要提供 run_id")
        return self


# ---- MCP（2026-10-20；契约 §6.13）------------------------------------------


class McpServerRequest(_Request):
    """改一台 MCP Server 的开关或工具名单。

    只带**本次动过的键**：省略 ``enabled_tools`` 表示"不改它"，显式传 ``null`` 表示"取消白名单"
    （两条语义不同，所以服务层按"有没有这个键"判断，不靠默认值）。
    """

    enabled: bool | None = None
    enabled_tools: list[str] | None = None
    disabled_tools: list[str] | None = None


class McpServerCreateRequest(_Request):
    """新增一台 MCP Server（``stdio`` 或 ``http``，2026-11-02）。

    ``env`` / ``headers`` 允许传——否则需要凭据的 server（cookie / token）根本配不出来。
    但它们**只写不回读**：响应里只有键名，日志与错误文案里也会抹掉值（与模型连接密钥同一条口径）。

    两个必填字段按 ``type`` 二选一（``command`` 或 ``url``），由服务层用同一个模型校验——
    这里不重复写规则，避免"接口说可以、落盘被拒"。
    """

    name: str = Field(min_length=1)
    type: Literal["stdio", "http"] = "stdio"
    command: str | None = Field(default=None, min_length=1)
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    url: str | None = Field(default=None, min_length=1)
    headers: dict[str, str] = Field(default_factory=dict)


class McpServerSpecRequest(_Request):
    """替换连接方式；省略字段保持原值，env / headers 按键合并且不回读。

    ``type`` 与已有值不同时，服务层会**丢掉旧传输那组字段**再应用本次改动
    （stdio 的 command/args 与 http 的 url/headers 互斥）。
    """

    type: Literal["stdio", "http"] | None = None
    command: str | None = Field(default=None, min_length=1)
    args: list[str] | None = None
    env: dict[str, str] = Field(default_factory=dict)
    remove_env: list[str] = Field(default_factory=list)
    url: str | None = Field(default=None, min_length=1)
    headers: dict[str, str] = Field(default_factory=dict)
    remove_headers: list[str] = Field(default_factory=list)


# ---- 技能（技能管理页，2026-10-16；§16.5）----------------------------------


class SkillRootRequest(_Request):
    """添加一个技能发现根（2026-10-20）。

    只收**绝对路径**：相对路径的基准在冻结形态是数据目录、在源码形态是配置文件目录，
    让用户在这里猜基准只会得到"路径明明对却什么都扫不到"。`~` 由页面展开后再提交。
    """

    path: str = Field(min_length=1, max_length=TITLE_MAX_CHARS)


class SkillToggleRequest(_Request):
    """启用/禁用一个技能（技能管理页，2026-10-16；§16.5）。

    **一次一个**：界面就是一个开关，报一批既没有对应操作，也会让"哪个名字错了"变含混。
    名字是否真有这个技能由领域层裁定（422，理由里带上当前可用的名字）。
    """

    name: str = Field(min_length=1)
    enabled: bool


# ---- 权限（T38；§1.5）------------------------------------------------------


class PermissionsModeRequest(_Request):
    """切换"工作区外路径"档位（T38）。

    档位取值与领域层同源（`ExternalPathsMode`），传输层不另立一套字符串——两处各写一份迟早会
    漂移。这里的 `extra="forbid"` 保证打错档位名得到 422，而不是静默保存成别的档位。
    """

    mode: ExternalPathsMode


class StorageLocationRequest(_Request):
    """把数据根改到指定目录（2026-10-26）。

    这里只挡空值：绝对路径、可创建、可写这些**文件系统层面**的判断在服务里做（要真的去建目录
    并写探针），放在传输层会把 I/O 塞进校验器。非法路径返回 422，文案由服务给出。
    """

    data_root: str = Field(min_length=1, max_length=1024)


class WebSearchKeyRequest(_Request):
    """录入 / 覆盖联网检索的服务凭据（哪一条由 query 的 ``provider`` 决定：Tavily / DeepSeek）。

    形状只挡空值：**内容不在这里做任何校验**（长度上限只是防"把整篇文档粘进来"），
    密钥对不对只有发起一次真实检索才知道——那件事由用户自己决定何时做。
    """

    api_key: str = Field(min_length=1, max_length=KEY_MAX_CHARS)


class ExternalPathGrantRequest(_Request):
    """新增 / 改写一条外部路径授权（T38）。

    只校验**形状**（非空、长度上限）：路径的规范化（大小写、分隔符、符号链接）与"是不是受保护
    位置"都在服务层裁定——那需要真实文件系统，不是传输层能替它做主的。
    `access` 只允许 `read` / `read_write`："不给访问"用撤销表达，不另立一个级别。
    """

    path: str = Field(min_length=1, max_length=PATH_MAX_CHARS)
    access: ExternalPathAccess
