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
  CapabilitiesView,
  CapabilityStatusView,
  CapabilityView,
  ConnectionStatus,
  ExternalPathGrantView,
  ModelConnectionView,
  ModelMenuItemView,
  PermissionsView,
  SettingsView,
  SkillEntryView,
  SkillRootView,
  SkillsView,
  TaskRowView,
  TaskState,
  TasksView,
} from '../fixtures/types'
import type {
  CapabilitiesDto,
  HealthDto,
  ModelConnectionDto,
  PermissionsDto,
  SkillsDto,
  TasksDto,
} from './client'

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
    // 用户声明的"支持图片输入"（2026-10-16）：与实测能力分列，页面各自表述。
    supportsVision: dto.supports_vision ?? false,
    confirmDelete: options.confirmDelete ?? false,
    pendingApply: options.pendingApply ?? false,
  }
}

/**
 * 「设置 → 权限」分区：DTO 说什么就是什么。
 *
 * 档位、访问级别这两个枚举在契约与视图里**字面相同**（不像 transport 那样下划线↔连字符），
 * 所以这里直接透传，不再多摆一张一一对应的映射表。`store_unavailable` 原样带过去，
 * 界面据此说明"这里改了也存不下来"——这是事实，不是错误。
 */
export function toPermissionsView(dto: PermissionsDto): PermissionsView {
  return {
    mode: dto.mode,
    defaultMode: dto.default_mode,
    grants: (dto.grants ?? []).map(
      (grant): ExternalPathGrantView => ({ path: grant.path, access: grant.access }),
    ),
    storeUnavailable: dto.store_unavailable,
  }
}

/**
 * 「能力开关与能力状态」（v0.5 T56；§16.1、§16.6）：DTO 说什么就是什么。
 *
 * `switches` 是**解析后的完整开关表**（出厂默认 + 用户改过的项），页面据此画每一项的开关状态；
 * `switchable` 是可开关集，页面照它渲染。`reason` 与 `capability` 都是稳定键，页面按 `reason`
 * 分支说"下一步怎么做"；`hint` 原样带过去——不可用时服务端保证它有话，**页面不替后端编一句
 * "为什么用不了"**（那正是这个面板要消灭的东西）。
 */
export function toCapabilitiesView(dto: CapabilitiesDto): CapabilitiesView {
  return {
    switches: dto.switches ?? {},
    switchable: dto.switchable ?? [],
    storeUnavailable: dto.store_unavailable ?? false,
    capabilities: (dto.capabilities ?? []).map(
      (row): CapabilityStatusView => ({
        capability: row.capability,
        label: row.label,
        available: row.available,
        reason: row.reason,
        detail: row.detail,
        hint: row.hint ?? null,
        locked: row.locked ?? false,
      }),
    ),
  }
}

/**
 * 「技能」管理页（2026-10-16；§16.5）：DTO 说什么就是什么。
 *
 * `enabled` 是**总闸**（`skill` 能力开关），逐条 `enabled` 是**用户**的选择，`modelInvocable` /
 * `userInvocable` 是**技能作者**的声明——三件事都在视图里，页面不许把它们合并成一句"这个技能开着"。
 * `diagnostics` 原样带过去：被跳过的条目必须让用户看见（否则就是"我的技能怎么不见了"）。
 */
export function toSkillsView(dto: SkillsDto): SkillsView {
  return {
    enabled: dto.enabled ?? false,
    storeUnavailable: dto.store_unavailable ?? false,
    skills: (dto.skills ?? []).map(
      (row): SkillEntryView => ({
        name: row.name,
        description: row.description,
        source: row.source,
        enabled: row.enabled ?? true,
        modelInvocable: row.model_invocable ?? true,
        userInvocable: row.user_invocable ?? true,
        declaredCapabilities: row.declared_capabilities ?? [],
      }),
    ),
    // 发现根（2026-10-20）：路径原样带回——这一页就是要让用户看清"在扫哪些目录"。
    roots: (dto.roots ?? []).map(
      (row): SkillRootView => ({
        path: row.path,
        source: row.source,
        count: row.count ?? 0,
        removable: row.removable ?? false,
        readable: row.readable ?? true,
      }),
    ),
    diagnostics: dto.diagnostics ?? [],
  }
}

/**
 * 任务中心（v0.6 T62；§17.6）：DTO 说什么就是什么。
 *
 * `counts` 在这里补齐成**完整闭集**：契约给的是一个字典，页面要的是 `Record<TaskState, number>`。
 * 缺键会让筛选栏显示成 `undefined`——那看起来像"这个状态不存在"，比不显示更糟。多出来的键
 * （后端先加了新状态）**忽略**：页面不认识的状态本来就不该被当成已知状态渲染。
 *
 * **不派生"在等什么"**：它是 `state` 的函数，由展示层按闭集推导（契约就是这么定的），
 * 这里只搬事实。
 */
export function toTasksView(dto: TasksDto): TasksView {
  const counts: Record<TaskState, number> = {
    queued: 0,
    running: 0,
    waiting_user: 0,
    completed: 0,
    failed: 0,
    cancelled: 0,
    needs_review: 0,
  }
  for (const [state, count] of Object.entries(dto.counts ?? {})) {
    if (state in counts) counts[state as TaskState] = count
  }
  return {
    tasks: (dto.tasks ?? []).map(
      (row): TaskRowView => ({
        taskId: row.task_id,
        sessionId: row.session_id,
        sessionTitle: row.session_title,
        state: row.state,
        detail: row.detail ?? null,
        createdAt: row.created_at,
        startedAt: row.started_at ?? null,
        finishedAt: row.finished_at ?? null,
      }),
    ),
    counts,
    pendingCount: dto.pending_count,
  }
}

/** 设置页整体：连接列表 + 凭据存储的持久性（后者来自健康检查，还没有连接时也能说清）。 */
export function toSettingsView(
  connections: readonly ModelConnectionDto[],
  health: HealthDto | null,
  /** 每条连接的页面本地态（确认删除 / 正在探测 / 刚保存待下一轮生效）。 */
  optionsFor?: (dto: ModelConnectionDto) => ConnectionViewOptions,
  /** 权限分区；还没读到时传 `null`——页面宁可显示"读取中"，也不编造一个档位。 */
  permissions: PermissionsDto | null = null,
): SettingsView {
  return {
    connections: connections.map((dto) => toConnectionView(dto, optionsFor?.(dto) ?? {})),
    // 健康检查还没回来时不作任何声称：两种 kind 在 note 为空时都不显示提示，
    // 等它回来再如实说"密钥重启后还在不在"（§6.8 不允许猜）。
    credentialStore: {
      kind: health?.credential_store === 'memory' ? 'unavailable' : 'os',
      note: health?.credential_note ?? null,
    },
    permissions: permissions === null ? null : toPermissionsView(permissions),
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
