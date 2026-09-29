// DTO → 展示视图模型（T29A）。
//
// 为什么需要这一层：展示组件吃的是 `fixtures/types.ts` 里的**视图模型**，契约给的是 DTO。
// 把映射集中在这里，组件就不必知道 `session_only` ↔ `session-only` 这类拼写差异，也不会
// "顺手"把 DTO 里没有的字段补出来。`fixtures/types.ts` 头部记的四处分叉在这里被真正消掉：
// 状态/密钥/传输枚举一律以生成类型为准，映射方向是**单向**的（DTO → 视图）。
//
// 三条规则：
// - **不编造状态**：`testing`（"已发出探测、还没回来"）只由页面设置，适配器从不产出它；
// - **不假装测过**：能力固定按 聊天/工具/流式 三项列出，DTO 里没有的那项记 `unknown`；
// - **密钥只报存在性**：`key_status` 三态直接映射——DTO 里本来就没有承载密钥值的字段。

import type {
  CapabilityView,
  ConnectionStatus,
  ModelConnectionView,
  ModelMenuItemView,
  SettingsView,
} from '../fixtures/types'
import type { HealthDto, ModelConnectionDto } from './client'

const CAPABILITY_ORDER = ['chat', 'tools', 'streaming'] as const

const CAPABILITY_NAME: Record<(typeof CAPABILITY_ORDER)[number], string> = {
  chat: '聊天',
  tools: '工具调用',
  streaming: '流式',
}

// 传输枚举在契约里是下划线、在视图里是连字符。**显式映射而不是 `as` 转换**：
// 后者会把这种差异藏起来，等它某天真的变了才在别处炸掉（这条就是被测试抓出来的）。
const TRANSPORT_BY_CONTRACT: Record<string, ModelConnectionView['transport']> = {
  https: 'https',
  loopback_http: 'loopback-http',
  plaintext_http: 'plaintext-http',
}

// 契约的四态映射到视图的四态；视图多出的 `testing` 由页面在探测飞行中设置。
const STATUS_BY_CONTRACT: Record<string, ConnectionStatus> = {
  verified: 'verified',
  unverified: 'unverified',
  failed: 'failed',
  unsupported: 'unsupported',
}

/** 上下文窗口的展示写法：32000 → `32k`，非整千保留原值。 */
export function contextWindowLabel(tokens: number): string {
  if (tokens >= 1000 && tokens % 1000 === 0) return `${tokens / 1000}k`
  return String(tokens)
}

/** 契约里的能力名 → 视图顺序名。`tool_calling` 是契约的写法，页面说"工具调用"。 */
const CAPABILITY_KEY: Record<(typeof CAPABILITY_ORDER)[number], string> = {
  chat: 'chat',
  tools: 'tool_calling',
  streaming: 'streaming',
}

function toCapabilities(dto: ModelConnectionDto): CapabilityView[] {
  const checks = dto.capabilities ?? []
  return CAPABILITY_ORDER.map((name) => {
    const found = checks.find((item) => item.capability === CAPABILITY_KEY[name])
    return {
      name: CAPABILITY_NAME[name],
      // 缺项就是"不知道"，不是"通过"（§15.9）。
      result: found?.result ?? 'unknown',
    }
  })
}

export interface ConnectionViewOptions {
  /** 卡片正在等用户确认停用（页面本地状态）。 */
  confirmDelete?: boolean
  /** 刚保存过、改动只对下一轮生效（页面本地状态）。 */
  pendingApply?: boolean
  /** **页面正在等这次探测返回**（页面本地态）：契约不会产出 `testing`，只有这里能给出它。 */
  testing?: boolean
}

/** 一条连接：DTO 说什么就是什么，页面本地态只从这里注入。 */
export function toConnectionView(
  dto: ModelConnectionDto,
  options: ConnectionViewOptions = {},
): ModelConnectionView {
  return {
    id: dto.connection_id,
    name: dto.display_name,
    baseUrl: dto.base_url,
    transport: TRANSPORT_BY_CONTRACT[dto.transport] ?? 'https',
    modelId: dto.model_id,
    contextWindowLabel: contextWindowLabel(dto.context_window_tokens),
    isDefault: dto.is_default,
    // `session_only`（契约，下划线）→ `session-only`（视图）。
    keyStatus: dto.key_status === 'session_only' ? 'session-only' : dto.key_status,
    // 页面在等探测返回时就地显示"检测中"（这是页面本地态，不是契约状态）。
    status: options.testing ? 'testing' : (STATUS_BY_CONTRACT[dto.status] ?? 'unverified'),
    // 契约目前不带"检测说明"文本：这里**不臆造**一句话，页面按能力分项自己措辞。
    statusDetail: null,
    capabilities: toCapabilities(dto),
    confirmDelete: options.confirmDelete ?? false,
    pendingApply: options.pendingApply ?? false,
  }
}

/** 设置页整体：连接列表 + 凭据存储的持久性（后者来自健康检查，还没有连接时也能说清）。 */
export function toSettingsView(
  connections: readonly ModelConnectionDto[],
  health: HealthDto | null,
  /** 每条连接的页面本地态（确认删除 / 正在探测 / 刚保存待下一轮生效）。 */
  optionsFor?: (dto: ModelConnectionDto) => ConnectionViewOptions,
): SettingsView {
  return {
    connections: connections.map((dto) => toConnectionView(dto, optionsFor?.(dto) ?? {})),
    // 健康检查还没回来时不作任何声称：两种 kind 在 note 为空时都不显示提示，
    // 等它回来再如实说"密钥重启后还在不在"（§6.8 不允许猜）。
    credentialStore: {
      kind: health?.credential_store === 'memory' ? 'unavailable' : 'os',
      note: health?.credential_note ?? null,
    },
  }
}

/** 输入区模型菜单里的一行。 */
export function toModelMenuItem(dto: ModelConnectionDto): ModelMenuItemView {
  return {
    id: dto.connection_id,
    name: dto.display_name,
    modelId: dto.model_id,
    status: STATUS_BY_CONTRACT[dto.status] ?? 'unverified',
  }
}
