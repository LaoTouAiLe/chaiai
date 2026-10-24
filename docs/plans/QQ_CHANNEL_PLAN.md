# QQ 通道接入方案（草案 v0.1）

> 状态：**草案，未实施、未验证**。2026-10-23 起稿。
> 参考实现（只读、未运行）：`TomoyoNatsume/dsh-qq-bridge`（MIT，双模式）、`tencent-connect/dsh-qqbot`（官方 QQ Bot 通道）。
> 本文只描述设计，不改任何代码。所有对 chaiai 的引用都带 `文件:行号`，与当时源码一致。

## 1. 目标与范围

一个独立进程把 QQ 消息接到 chaiai 会话上，**两种模式可切换**：

| 模式 | 通道 | 身份单位 | 主动消息 | 平台标识 |
| --- | --- | --- | --- | --- |
| `official` | 腾讯 QQ 开放平台机器人（WebSocket） | openid | 受额度限制 | 平台会标机器人/AI |
| `onebot` | 本机 NapCat + OneBot 11 | QQ 号 | 无限制（个人号身份） | 普通账号 |

**第一版只做私聊 + 白名单本人**，能力：多轮对话、看本机文件、开浏览器、审批回传。
**不做**：群聊（见 §13 冲突 3）、阻塞式提问（冲突 2）、桌面自动化、任何以"不被平台识别"为目的的拟人化。

## 2. 架构

```
QQ 客户端 ⇄ QQ 平台
              ⇅ (WebSocket，两种模式各一套)
      ┌──────────────────────────────────────┐
      │ 桥接进程（独立，非 FastAPI 内）        │
      │  Transport ──► InboundQueue           │
      │                 │                     │
      │  SessionBinder ◄┘                     │
      │    │  POST /api/sessions（首次）      │
      │    │  POST /api/sessions/{id}/messages │
      │    ▼                                  │
      │  EventFanout  ← GET /api/sessions/{id}/events（SSE）
      │    │                                  │
      │    ├─► Outbox（分片/节流/发图）        │
      │    └─► InteractionHub（审批/超时）     │
      └──────────────────────────────────────┘
```

**为什么独立进程**：chaiai 的本机边界是 `Host` 白名单 + 同源 `Origin` 校验（`local-agent-web/src/local_agent/api/security.py:115-123`），本机脚本不带 `Origin` 时按非浏览器处理放行——桥接进程正好落在这一条口径里；塞进 FastAPI 会改现有契约，收益为零。

## 3. 通道抽象

### 3.1 统一类型

```ts
type Scope = 'private' | 'group'

interface InboundMessage {
  channel: 'official' | 'onebot'
  accountId: string          // official: appId；onebot: 登录的 QQ 号
  scope: Scope
  peerId: string             // official: openid；onebot: QQ 号 / 群号
  senderId: string
  text: string
  messageId: string          // 平台消息 id（用于去重与被动回复）
  replyToken?: string        // official: 被动回复 msgId；onebot: 无
}

interface OutboundTarget { channel: ...; accountId: string; scope: Scope; peerId: string; replyToken?: string }

interface Capabilities {
  proactive: boolean         // 能否主动发起会话
  markdown: boolean
  inlineButtons: boolean     // 官方支持键盘按钮；onebot 只能发文本
  maxTextLength: number
  media: { image: boolean; file: boolean }
}

interface ChannelTransport {
  readonly id: 'official' | 'onebot'
  capabilities(): Capabilities
  connect(): Promise<void>
  disconnect(): Promise<void>
  onMessage(cb: (msg: InboundMessage) => void): () => void
  send(target: OutboundTarget, content: OutboundContent): Promise<void>
}
```

**与参考实现的差异（有意为之）**：它把官方消息压成 OneBot 形状、上层只认一种；好处是少写一套路由，代价是官方能力被抹平（它的审批只能用"回复 1/2"，不能用按钮）。我们保留 `capabilities()`，让上层按能力降级——有按钮发按钮，没有发编号。代价是渲染路径两条都要测。

### 3.2 两个实现

- `official`：官方 SDK（`@tencent-connect/qqbot-nodejs` 一类）建 WS 长连接，`appId/appSecret`，`sandbox` 切换 baseUrl。出站分两种：有 `replyToken` 走被动回复，没有则尝试主动消息接口（官方额度限制在此暴露）。
- `onebot`：`ws://127.0.0.1:3001` + `Authorization: Bearer <token>`，指数退避重连（500ms 起、上限 15s、连通即重置）。出站走 `send_private_msg` / `send_group_msg`，消息段支持 `text` / `image`（base64 或本机文件）。**token 为空字符串/"undefined"/"null" 一律视为无 token**（参考实现的坑，照抄）。

## 4. 会话映射（与 dsh 的关键差异）

```
peerKey = `${channel}:${accountId}:${scope}:${peerId}`
```

本地表（桥接进程自己的 SQLite，不碰 chaiai 的库）：

```sql
CREATE TABLE channel_peers (
  peer_key   TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  last_at    TEXT NOT NULL,
  version    INTEGER NOT NULL DEFAULT 0   -- /new 时 +1
);
```

**为什么必须持久化**：chaiai 的 `session_id` 由服务端生成（`POST /api/sessions`，`routes.py:652-658`），调用方**不能指定**——这点和 dsh 相反（dsh 允许 `agents.create({ sessionId })`，所以它可以用 `hash(sessionKey)+bootSuffix` 派生）。我们没有派生权，只能"建一次、存下来、以后复用"。丢了这张表 = 每次都开新会话 = 记忆全断。

规则：
- 首次收到该 peer 消息 → `POST /api/sessions` → 落表
- 之后一律复用表里的 `session_id`
- `/new` → 再建一个会话、更新表、`version+1`；旧会话留在网页里可查
- 同一 peer 的提交串行排队（参考实现用 Promise 链），避免并发驱动同一会话

## 5. 入站（提交）

```http
POST /api/sessions/{session_id}/messages
{ "text": "...", "client_request_id": "..." }
```

三个必须注意的点：

1. **先订阅 SSE，再提交**。反序会丢掉本轮最早的事件。
2. **这个接口是同步的**——`routes.py:708` 的说明写着"同步跑完这一轮"，一直挂到本轮结束才返回。桥接层必须把它丢到后台任务里，立刻回"收到"，结果全靠 SSE；同时给这个 HTTP 请求设**大于一轮最长耗时**的读超时。
3. **幂等键**：同一条 QQ 消息重投时 `client_request_id` 必须复用；但"用户又发了一条相同文字"是另一条消息，要换键（与 `frontend/src/api/queries.ts` 里已有的纪律一致）。

> 需要后端配合的（第二版）：给该接口加 `async=true`，立即返回 `run_id`，彻底摆脱"长连接挂到结束"。

## 6. 出站（订阅 SSE）

`GET /api/sessions/{session_id}/events`（`sse.py:129-131`），事件名与载荷在 `application/events.py`：

| 事件 | 用途 | 备注 |
| --- | --- | --- |
| `message_delta` | 流式转发 | `payload.channel` 区分 `text` / `reasoning`；只转 `text`，否则聊天框刷屏（`events.py:232-237`） |
| `message_completed` | 收尾 | `payload.text`，若与已转发增量一致则跳过（`events.py:240-245`） |
| `approval_required` | 推审批 | `payload.approval` 是 `ApprovalView`（`application/models.py:691-708`） |
| `approval_resolved` | 关掉待处理项 | `state: approved/rejected/expired`（`events.py:291-295`） |
| `task_completed` / `task_failed` / `task_cancelled` | 结束本轮 | 清活跃态、允许下一条 |
| `tool_started` | 可选提示 | 默认不发，避免噪音 |
| `stream_overrun` / `stream_idle_timeout` | 重连 | 见 `sse.py:38-39` |

**连接会被主动关掉**：`web.sse_idle_timeout_seconds` 默认 300s 无事件即断，`sse_max_pending_events` 默认 200 超过即判落后（`config.py:599-602`）。桥接层必须：断线重连 → 用重连后的 `session_snapshot` 对齐状态，而不是假设事件能补发（`sse.py:6` 明确不做游标补发）。

分片与节流：单条上限 4500 字（参考实现同值），按 200ms 节流合并增量；超长按行/代码块边界切，不从句子中间断。

## 7. 审批回传

待处理表：`approval_id → { peerKey, toolName, risk, stage, outsideWorkspace, createdAt }`。

回传：

```http
POST /api/sessions/{session_id}/approvals/{approval_id}
{ "decision": "approve|reject", "client_request_id": "...", "reason": "" }
```

`decision` 只有三值且 `edit` 必须带参数（`api/schemas.py:147-158`）——QQ 里不提供 `edit`（改写参数属于精细操作，回网页做）。

**分级规则（比参考实现严，这里是安全边界）**：

- 可在聊天内批准：`risk === "normal"` 且 `confirmation_stage === 1` 且 `outside_workspace === false`
- 其余（高危、二次确认、工作区外）→ 只提示"需要到网页确认"，**不提供批准选项**。理由：聊天里的一次误触不该等于"在你的机器上写工作区外的文件"。
- 解析不出、发送者不在白名单、该审批已被处理 → **不提交任何决定**（失败即拒绝，与 §15.8 的 fail-closed 一致）。
- 超时 5 分钟：**不自动拒绝**（拒绝会替用户终结这一轮），只把该条降级为"请去网页处理"并停止等待。
- 幂等：`client_request_id` 固定为 `approval_id + decision`，重试复用。

## 8. 访问控制

- 白名单字段 `allowedUsers: string[]`（official 填 openid，onebot 填 QQ 号），**默认空 = 全部拒绝**；不提供 `open` 模式。
- 非白名单消息：静默丢弃，不回"无权限"（避免被用来探测）。
- 前缀可选：`requirePrefix` 非空时，消息必须以其开头才进入路由；空则白名单消息全部进入。
- **群聊第一版不做**，原因见 §13 冲突 3。

## 9. 主动消息（P2）

- 触发源复用现有定时任务：`POST /api/cron/jobs`（`routes.py:387-405`）跑一个 prompt。
- 出口：桥接层订阅该会话 SSE，`task_completed.payload.final_result` 取文本 → 发给绑定的 peer。
- 约束（默认关闭）：时段窗口 09:00–22:00、每日 4–6 次、最小间隔 90 分钟、对方 30 分钟内说过话就跳过、连续 2 次没回当天停。
- 风险：主动消息是最容易被平台判异常的行为，`official` 还会撞额度（参考实现遇到 `40034122` 召回上限）。

## 10. 发图（P2）

- 图片来源不用新增工具：`browser` 的 `screenshot` 已经把 PNG 落到工作区（`tools/browser_tools.py:252,265`）。
- 出站：`onebot` 走 `image` 段（base64），`official` 走官方上传。单张上限与允许根目录都进配置；路径越界直接拒发。

## 11. 配置草案

```yaml
channel:
  enabled: false
  platform: official            # official | onebot
  official:
    appId: ""
    appSecret: ""               # 建议走环境变量，不落明文
    adminOpenId: ""
    sandbox: false
  onebot:
    wsUrl: "ws://127.0.0.1:3001"
    token: ""                   # 空串/undefined/null 视为无 token
    loginQq: 0
  access:
    allowedUsers: []            # 空 = 全部拒绝
    requirePrefix: ""
  output:
    maxChars: 4500
    stream: true
    throttleMs: 200
  approval:
    timeoutSeconds: 300
    allowInChat: true           # false = 一律回网页批
  proactive:
    enabled: false
    window: "09:00-22:00"
    perDay: [4, 6]
    minGapMinutes: 90
  api:
    baseUrl: "http://127.0.0.1:8000"
```

## 12. 契约冲突清单（要动的地方，按优先级）

| # | 冲突 | 证据 | 影响 | 化解 |
| --- | --- | --- | --- | --- |
| 1 | 提交接口同步 | `routes.py:701-715`、`:708` 注释 | 长任务占住一个 HTTP 连接数分钟 | 第一版桥接层后台化；第二版加 `async=true` 返回 `run_id` |
| 2 | 没有阻塞式"向用户提问"的 seam | `graph/nodes.py:220` 的 `ask_user` 只是反思产出的一段文本 | QQ 里做不了"停下来的选择题"（选项/多选/超时） | 第一版不做；要做需在应用层新增可挂起的提问与回答通道 |
| 3 | 权限档位是全局的 | `model_settings/store.py:142-148`（档位是 `settings_meta` 一行） | 群聊里所有人共用一个会话 = 共用同一个全局档位，无法"群里只读、私聊可写" | 群聊延后；先补"按会话的工具白名单" |
| 4 | SSE 按会话单流、不补发 | `sse.py:6,38-39`，`config.py:599-602` | 多 peer = 多条订阅；断线后只能靠快照对齐 | 桥接层维护订阅池 + 重连后重取快照 |
| 5 | 本机边界只认本机 Host | `security.py:115-123` | 桥接进程必须跑在同一台机器、不带 `Origin` | 明确部署前提；不要尝试远程化 |

## 13. 分期与验收（每条都要能观察）

**P0 骨架（不含真实 QQ）**
- 假 transport + 真 chaiai：注入一条文本 → 网页会话里出现同一条用户消息 → assistant 文本回到假 transport
- 验收：网页 UI 能看到该消息；桥接日志打印回复全文

**P1 单聊可用（official 或 onebot 任选其一先上）**
- 白名单拒绝：非白名单消息无任何回复、会话里不新增消息
- 多轮：连发三句，网页会话里是同一个 `session_id`；重启桥接进程后仍是同一个
- 审批：伪造一条 `risk=normal` 的写操作 → QQ 收到编号提示 → 回 1 → 网页里该审批变 approved
- 高危拦截：`outside_workspace=true` 的审批 → QQ 只提示去网页，回复数字不生效
- 超时：发审批后不回复 5 分钟 → 桥接不再接受该条回复，提示去网页；该审批在网页仍是 pending
- SSE 断线：手动断开网络 30s → 恢复后状态与网页一致（不能出现半截回答重复发送）

**P2 主动与富媒体**
- cron 任务完成后主动发给指定私聊，且频率/时段限制生效（超出则不发送并记日志）
- `browser screenshot` 产物通过 QQ 发出，> 上限的图被拒并回一句说明

**P3 群聊**（前置：按会话工具白名单）

## 14. 明确不做

- 以"让平台检测不出是 AI"为目的的任何设计（打字节奏、随机停顿、分句伪装）
- 桌面自动化/Computer Use（chaiai 目前只有 `browser` 工具，没有桌面输入能力）
- 在 QQ 里批准高危或工作区外写操作
- 群聊（在第一版）

## 15. 未核实 / 待确认

- 官方 SDK 的键盘按钮在当前版本是否可用（未验证，直接影响 `inlineButtons`）
- `official` 模式下的主动消息是否真被额度锁死（只在参考实现文档里见到报错码，未实测）
- NapCat 在当前 QQ 客户端版本上的可用性（第三方协议端随版本漂移）
- 桥接进程落哪个语言栈：TypeScript（能直接复用官方 SDK 与参考实现的思路）还是 Python（和 chaiai 同栈、便于复用其类型）。**倾向 TypeScript**，因为两款 SDK/参考实现都是 TS；但这是可换的，未定。
