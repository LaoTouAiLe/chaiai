import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { KeyboardEvent, ReactNode } from 'react'

import { Check, Close, Cross } from '../../components/icons'
import { useBodyScrollLock } from '../../components/useBodyScrollLock'
import type {
  CapabilityView,
  ConnectionStatus,
  ModelConnectionView,
  SettingsView,
} from '../../fixtures/types'
import { ScrollToBottom } from '../../components/ScrollToBottom'
import { PermissionSection } from './PermissionSection'
import type { PermissionActions } from './PermissionSection'
import { SETTINGS_SEARCH_INDEX, filterSettings, settingElementId } from './settingsSearch'
import type { SettingsSearchHit } from './settingsSearch'
import styles from './SettingsDialog.module.css'

/** 真实模式下的动作；**不传就是原型宿主**（fixtures 直出、按钮惰性）。 */
export interface SettingsDialogActions {
  /** 正在提交/探测的那条连接（按钮据此禁用，防双击）。 */
  busyId: string | null
  /** 正在**新增**（还没有 id，所以不能只看 `busyId`——否则"新增中"与"空闲"同值）。 */
  creating: boolean
  onAdd: () => void
  /** 打开编辑：表单必须带上这条连接的**当前值**（端点、参数、模型），密钥除外。 */
  onEdit: (connectionId: string) => void
  onTest: (connectionId: string) => void
  onSelectDefault: (connectionId: string) => void
  onRequestDelete: (connectionId: string) => void
  onCancelDelete: (connectionId: string) => void
  onConfirmDelete: (connectionId: string) => void
  onClose: () => void
}

interface SettingsDialogProps {
  settings: SettingsView
  actions?: SettingsDialogActions
  /** 权限分区的动作；不传就是原型宿主（fixtures 直出、按钮惰性）。 */
  permissionActions?: PermissionActions
  /** 权限分区正在提交（按钮据此禁用，防双击）。 */
  permissionBusy?: boolean
  /** 权限分区最近一次失败的展示文本（服务端给的用户可读文本，不是响应原文）。 */
  permissionError?: string | null
  /** 新增/编辑表单：由容器放进来，对话框只负责摆位置（表单自己的状态不在这里）。 */
  form?: ReactNode
  extraSections?: Partial<Record<SettingsSection, ReactNode>>
}

export type SettingsSection = 'models' | 'permissions' | 'skills' | 'capabilities' | 'general' | 'storage' | 'about' | 'execution' | 'agents' | 'retrieval' | 'web_access' | 'mcp' | 'attachments' | 'memory' | 'medium_term' | 'curated' | 'compression' | 'embedding' | 'advanced'

/** 原型保留连接/权限；真实宿主注入应用设置各组。 */
/**
 * 设置分区清单。
 *
 * 2026-10-22：**技能 / 能力 / MCP 三项已移出设置**，改由侧栏「能力中心」整页提供。
 * 原因是它们属于高频日常操作（今天能不能联网、能不能提交），埋在 14 个分区里每次要多两步；
 * 2026-10-19 曾反向下过"并进设置"的决定，那次是为了消除重复入口，这次有明确的新事实推翻了它。
 * 类型里的 `skills` / `capabilities` / `mcp` 三个 id 保留，避免其它调用方（测试、深链）类型断裂。
 */
const SECTIONS: readonly { id: SettingsSection; label: string }[] = [
  { id: 'models', label: '模型连接' },
  { id: 'permissions', label: '权限' },
  { id: 'general', label: '通用与外观' },
  { id: 'storage', label: '数据位置' },
  // 关于（2026-10-27）：版本与更新、卸载。与"数据位置"同组——都是"这台机器怎么跑"，
  // 而且它读的也是宿主形态（装在哪、能不能卸），不是发给模型的偏好。
  { id: 'about', label: '关于' },
  { id: 'execution', label: '任务执行' },
  { id: 'agents', label: '项目规则' },
  { id: 'retrieval', label: '自动代码检索' },
  { id: 'web_access', label: '联网检索' },
  { id: 'attachments', label: '图片与附件' },
  { id: 'memory', label: '长期记忆' },
  { id: 'medium_term', label: '中期记忆' },
  { id: 'curated', label: '常驻记忆' },
  { id: 'compression', label: '上下文压缩' },
  { id: 'embedding', label: '向量库与 Embedding' },
  { id: 'advanced', label: '高级配置' },
]

/**
 * 左侧导航的分组（2026-10-19 版式调整）：11 个分区平铺在横向页签里会折成两三排，
 * 分组后每组 1–3 项，纵向排列，当前分区在任何窗口高度下都可见。
 * 分组只影响呈现顺序，`id` 与分区标签不变（测试与深链仍按标签找 tab）。
 */
const SECTION_GROUPS: readonly { label: string; ids: readonly SettingsSection[] }[] = [
  { label: '模型与权限', ids: ['models', 'permissions'] },
  // 项目规则（AGENTS.md）与任务执行并列：它决定"模型在这个工作区里怎么干活"，不是记忆。
  { label: '任务与工具', ids: ['execution', 'agents', 'retrieval', 'web_access', 'attachments'] },
  // 压缩挂在"记忆"这一组：它管的是上下文要不要摘、摘完留多少，与三层记忆同一类事。
  // 常驻记忆（2026-10-27）：放在中期之后、"上下文压缩"之前——按"必到 → 按需 → 压缩"的阅读顺序。
  { label: '记忆与上下文', ids: ['memory', 'medium_term', 'curated', 'compression', 'embedding'] },
  // 分组标题只做归类；`通用与外观` 与只读的 `高级配置` 都是"改页面本身怎么跑"，同放一组。
  // 「数据位置」与通用同组：两者都是"这台机器怎么跑"，改完要重启或立即生效的本地设定。
  { label: '通用', ids: ['general', 'storage', 'about', 'advanced'] },
]

const STATUS_LABEL: Record<ConnectionStatus, string> = {
  verified: '已验证',
  unverified: '未验证',
  testing: '检测中',
  failed: '检测失败',
  unsupported: '能力不支持',
}

const STATUS_CLASS: Record<ConnectionStatus, string> = {
  verified: styles.statusVerified ?? '',
  unverified: styles.statusUnverified ?? '',
  testing: styles.statusTesting ?? '',
  failed: styles.statusFailed ?? '',
  unsupported: styles.statusUnsupported ?? '',
}

const KEY_LABEL: Record<ModelConnectionView['keyStatus'], string> = {
  configured: '已配置（只能覆盖写入，不回读）',
  missing: '未配置',
  'session-only': '仅本次进程内 · 重启后需重新录入',
}

const CAPABILITY_LABEL: Record<CapabilityView['result'], string> = {
  passed: '通过',
  failed: '失败',
  unknown: '未知',
}

function CapabilityChip({ capability }: { capability: CapabilityView }) {
  const tone =
    capability.result === 'passed'
      ? styles.capPassed
      : capability.result === 'failed'
        ? styles.capFailed
        : styles.capUnknown
  return (
    <span className={`${styles.capability} ${tone}`}>
      <span aria-hidden="true">
        {capability.result === 'passed' ? <Check size={10} /> : capability.result === 'failed' ? <Cross size={10} /> : null}
      </span>
      {capability.name} {CAPABILITY_LABEL[capability.result]}
    </span>
  )
}

/**
 * One chat connection.
 *
 * What the card must never do: show a key value (only whether one exists), claim
 * an untested connection works, or silently drop a connection — deactivation is
 * confirmed in place and keeps the history (ARCHITECTURE §15.9).
 *
 * A non-loopback HTTP endpoint additionally carries a visible plaintext-transport
 * mark (plan §6.8); the acknowledgement itself belongs to the save flow in T29A.
 */
function ConnectionCard({
  connection,
  actions,
}: {
  connection: ModelConnectionView
  actions?: SettingsDialogActions
}) {
  // 真实模式下按钮必须真有行为（§15.9：不得沿用无行为按钮）；原型模式没有 actions，保持惰性。
  const busy = actions !== undefined && actions.busyId === connection.id
  // 收起 / 展开（2026-10-22 V4）：一屏能看 6+ 行，而不是 3 张全展开的卡。
  // 默认收起——但要显示"是否需要关注"的信息（状态、是否有风险）仍在收起态可见。
  const [expanded, setExpanded] = useState(false)
  return (
    <article className={styles.card} aria-label={`连接 ${connection.name}`}>
      <header className={styles.cardHead}>
        <h3 className={styles.cardName}>{connection.name}</h3>
        <span className={styles.cardModel}>{connection.modelId}</span>
        {connection.isDefault ? <span className={styles.defaultTag}>默认</span> : null}
        <span className={`${styles.status} ${STATUS_CLASS[connection.status]}`}>
          {STATUS_LABEL[connection.status]}
        </span>
        {connection.transport === 'plaintext-http' ? (
          <span className={styles.riskTagInHead}>明文传输</span>
        ) : null}
        <button
          type="button"
          className={styles.cardToggle}
          aria-expanded={expanded}
          onClick={() => setExpanded((open) => !open)}
        >
          {expanded ? '收起' : '详情'}
        </button>
        <span className={styles.cardId}>{connection.id}</span>
      </header>

      {expanded ? <>
        <dl className={styles.cardFacts}>
          <div className={styles.fact}>
            <dt>端点</dt>
            <dd className={styles.mono}>{connection.baseUrl}</dd>
          </div>
          <div className={styles.fact}>
            <dt>模型</dt>
            <dd className={styles.mono}>{connection.modelId}</dd>
          </div>
          <div className={styles.fact}>
            <dt>上下文</dt>
            <dd>{connection.contextWindowLabel}</dd>
          </div>
          <div className={styles.fact}>
            <dt>密钥</dt>
            <dd>{KEY_LABEL[connection.keyStatus]}</dd>
          </div>
        </dl>

        {connection.transport === 'plaintext-http' ? (
          <p className={styles.risk}>
            <span className={styles.riskTag}>明文传输</span>
            局域网 HTTP 端点：请求内容在传输中不加密。保存时已由你确认该风险，可改为 HTTPS 或删除该连接。
          </p>
        ) : null}

        <div className={styles.capabilities}>
          {connection.capabilities.map((capability) => (
            <CapabilityChip key={capability.name} capability={capability} />
          ))}
        </div>

        {connection.statusDetail === null ? null : (
          <p className={styles.cardDetail}>{connection.statusDetail}</p>
        )}
      </> : null}

      {connection.pendingApply ? (
        <p className={styles.pendingApply}>编辑已保存为新版本 · 从下一轮请求开始生效，正在运行的一轮不受影响</p>
      ) : null}

      {connection.confirmDelete ? (
        <div className={styles.confirm}>
          <span className={styles.confirmText}>
            删除即停用：新请求不再使用该连接，历史模型标识与用量记录保留；正在运行的一轮继续使用它的固定版本与凭据。
            之后可以重新添加。
          </span>
          <button
            type="button"
            className={styles.cancel}
            disabled={busy}
            onClick={() => actions?.onCancelDelete(connection.id)}
          >
            取消
          </button>
          <button
            type="button"
            className={styles.danger}
            disabled={busy}
            onClick={() => actions?.onConfirmDelete(connection.id)}
          >
            确认删除
          </button>
        </div>
      ) : (
        <div className={styles.cardActions}>
          <button
            type="button"
            className={styles.action}
            disabled={busy || actions?.creating === true}
            onClick={() => actions?.onEdit(connection.id)}
          >
            编辑
          </button>
          <button
            type="button"
            className={styles.action}
            disabled={busy}
            onClick={() => actions?.onTest(connection.id)}
          >
            测试连接
          </button>
          {connection.isDefault ? null : (
            <button
              type="button"
              className={styles.action}
              disabled={busy}
              onClick={() => actions?.onSelectDefault(connection.id)}
            >
              设为默认
            </button>
          )}
          <button
            type="button"
            className={styles.actionDanger}
            disabled={busy}
            onClick={() => actions?.onRequestDelete(connection.id)}
          >
            删除
          </button>
        </div>
      )}
    </article>
  )
}

/**
 * Settings dialog (v0.4 scope: chat connections and permissions).
 *
 * The section switcher is real: both sections are fully usable (T38 added the
 * permissions one), so a nav item now leads somewhere instead of promising an
 * interaction the dialog does not have. The default section is the model
 * connections — opening settings is overwhelmingly about "which model", and the
 * permission section is only entered deliberately.
 */
export function SettingsDialog({
  settings,
  actions,
  permissionActions,
  permissionBusy,
  permissionError,
  form,
  extraSections,
}: SettingsDialogProps) {
  const [section, setSection] = useState<SettingsSection>('models')
  const [query, setQuery] = useState('')
  const [hitIndex, setHitIndex] = useState(0)
  /** 待定位的字段：分区切换后目标行才会渲染出来，交给 effect 轮询定位。 */
  const [pending, setPending] = useState<SettingsSearchHit | null>(null)
  /** 目标字段在当前取值下不渲染时（条件显示字段）的如实解释。 */
  const [notice, setNotice] = useState<string | null>(null)
  const panesRef = useRef<HTMLDivElement | null>(null)

  const visibleSections = SECTIONS.filter(
    item => item.id === 'models' || item.id === 'permissions' || extraSections?.[item.id] !== undefined,
  )
  const groups = SECTION_GROUPS
    .map(group => ({
      label: group.label,
      items: visibleSections.filter(item => group.ids.includes(item.id)),
    }))
    .filter(group => group.items.length > 0)

  // 搜索只覆盖偏好字段：模型连接与权限是手写分区，不在这份字段表里。
  const canSearch = visibleSections.some(item => item.id !== 'models' && item.id !== 'permissions')
  const searching = query.trim().length > 0
  // 可见分区拼成字符串做依赖：visibleSections 每次渲染都是新数组，直接依赖会让 memo 永不命中。
  const availableIds = visibleSections.map(item => item.id).join('|')
  const hits = useMemo(() => {
    if (!searching) return []
    const available = new Set(availableIds.split('|'))
    return filterSettings(SETTINGS_SEARCH_INDEX, query).filter(hit => available.has(hit.group))
  }, [availableIds, query, searching])

  // 切换分区或进出搜索时回到顶部：上一分区滚到底再切过去，新分区会从半截开始。
  useEffect(() => {
    if (panesRef.current !== null) panesRef.current.scrollTop = 0
  }, [section, searching])

  // 弹窗打开期间锁住背后的页面滚动（2026-10-26）。理由与计数语义见 hook 注释：
  // `overscroll-behavior` 挡不住遮罩空白处的滚轮，只能锁 body。
  useBodyScrollLock()

  // 目标行可能晚几帧才挂载（hidden 的祖先也不能 scrollIntoView），所以短暂轮询：
  // 找到就滚到中间并闪一下；始终找不到说明它的显示条件没满足，用文案说明而不是静默失败。
  useEffect(() => {
    if (pending === null) return
    let cancelled = false
    let attempts = 0
    let timer = 0
    const targetId = settingElementId(pending.group, pending.key)
    const attempt = () => {
      if (cancelled) return
      const element = document.getElementById(targetId)
      if (element !== null) {
        element.scrollIntoView({ behavior: 'smooth', block: 'center' })
        element.classList.add('settings-field-flash')
        window.setTimeout(() => element.classList.remove('settings-field-flash'), 1600)
        setPending(null)
        return
      }
      if (attempts < 20) {
        attempts += 1
        timer = window.setTimeout(attempt, 50)
        return
      }
      setNotice(pending.requirement)
      setPending(null)
    }
    timer = window.setTimeout(attempt, 30)
    return () => {
      cancelled = true
      window.clearTimeout(timer)
    }
  }, [pending, section])

  const openHit = useCallback((hit: SettingsSearchHit) => {
    setQuery('')
    setHitIndex(0)
    setNotice(null)
    setSection(hit.group)
    setPending(hit)
  }, [])

  const searchKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === 'Escape') {
      setQuery('')
      setHitIndex(0)
      return
    }
    if (!searching || hits.length === 0) return
    if (event.key === 'ArrowDown') {
      event.preventDefault()
      setHitIndex(index => Math.min(index + 1, hits.length - 1))
      return
    }
    if (event.key === 'ArrowUp') {
      event.preventDefault()
      setHitIndex(index => Math.max(index - 1, 0))
      return
    }
    if (event.key === 'Enter') {
      const hit = hits[Math.min(hitIndex, hits.length - 1)]
      if (hit !== undefined) {
        event.preventDefault()
        openHit(hit)
      }
    }
  }

  const empty = settings.connections.length === 0
  return (
    <div className={styles.overlay}>
      <div className={styles.dialog} role="dialog" aria-modal="true" aria-label="设置">
        <header className={styles.header}>
          <div className={styles.headerText}>
            <h2 className={styles.title}>设置</h2>
            <p className={styles.subtitle}>连接、偏好与运行参数 · 密钥只写入不回读</p>
          </div>
          {canSearch ? (
            <div className={styles.searchWrap}>
              <input
                type="search"
                role="searchbox"
                aria-label="搜索设置项"
                className={styles.searchInput}
                placeholder="搜索设置项（如：域名、超时）"
                value={query}
                onChange={event => {
                  setQuery(event.target.value)
                  setHitIndex(0)
                }}
                onKeyDown={searchKeyDown}
              />
            </div>
          ) : null}
          <button
            type="button"
            className={styles.close}
            aria-label="关闭设置"
            onClick={() => actions?.onClose()}
          >
            <Close />
          </button>
        </header>

        <div className={styles.body}>
          {/* 左侧分组导航：分组标签只是标题，不参与 tab 语义（`role="presentation"`）。 */}
          <nav className={styles.nav} role="tablist" aria-label="设置分区">
            {groups.map(group => (
              <div key={group.label} className={styles.navGroup} role="presentation">
                <p className={styles.navLabel} role="presentation">{group.label}</p>
                {group.items.map(item => (
                  <button
                    key={item.id}
                    type="button"
                    role="tab"
                    id={`settings-tab-${item.id}`}
                    aria-selected={section === item.id}
                    aria-controls={`settings-panel-${item.id}`}
                    className={`${styles.tab} ${section === item.id ? styles.tabSelected : ''}`}
                    onClick={() => setSection(item.id)}
                  >
                    {item.label}
                  </button>
                ))}
              </div>
            ))}
          </nav>

          <div className={styles.paneWrap}>
          <div className={styles.panes} ref={panesRef}>
          {notice === null ? null : <p className={styles.notice} role="status">{notice}</p>}
          {/* 搜索态只把分区切到 hidden：不卸载，未保存的草稿与滚动位置都还在。 */}
          <div hidden={!searching} className={styles.hits} role="listbox" aria-label="设置项搜索结果">
            {hits.length === 0 ? (
              <p className={styles.hitsEmpty} role="status">没有匹配的设置项。换个词试试，例如「域名」「超时」「记忆」。</p>
            ) : hits.map((hit, index) => (
              <div
                key={`${hit.group}-${hit.key}`}
                role="option"
                aria-selected={index === hitIndex}
                className={`${styles.hit} ${index === hitIndex ? styles.hitSelected : ''}`}
                onClick={() => openHit(hit)}
                onMouseEnter={() => setHitIndex(index)}
              >
                <span className={styles.hitMain}>
                  <span className={styles.hitLabel}>{hit.label}</span>
                  <span className={styles.hitSection}>{hit.sectionLabel}{hit.readOnly ? ' · 只读' : ''}</span>
                </span>
                <span className={styles.hitNote}>{hit.note}</span>
                {hit.requirement === null ? null : <span className={styles.hitRequires}>{hit.requirement}</span>}
              </div>
            ))}
          </div>
          {Object.entries(extraSections ?? {}).map(([id, content]) => (
            <div key={id} hidden={searching || section !== id} role="tabpanel" id={`settings-panel-${id}`} aria-labelledby={`settings-tab-${id}`}>
              {content}
            </div>
          ))}
          {searching ? null : section === 'permissions' ? (
            <div
              role="tabpanel"
              id="settings-panel-permissions"
              aria-labelledby="settings-tab-permissions"
            >
              <PermissionSection
                permissions={settings.permissions}
                errorText={permissionError}
                busy={permissionBusy}
                actions={permissionActions}
              />
            </div>
          ) : section === 'models' ? (
            <div
              role="tabpanel"
              id="settings-panel-models"
              aria-labelledby="settings-tab-models"
            >
              <div className={styles.sectionHead}>
                <div>
                  <h3 className={styles.sectionTitle}>模型连接</h3>
                  <p className={styles.sectionNote}>
                    新会话继承默认连接；切换只影响下一次提交，已开始的一轮固定用提交时的连接。
                  </p>
                </div>
                <button
                  type="button"
                  className={styles.primary}
                  // 新增中也要禁用：否则连点两次会建出两条一样的连接（复审实测）。
                  disabled={actions !== undefined && (actions.busyId !== null || actions.creating)}
                  onClick={() => actions?.onAdd()}
                >
                  添加连接
                </button>
              </div>

              {settings.credentialStore.kind === 'unavailable' && settings.credentialStore.note !== null ? (
                <p className={styles.storeWarning}>{settings.credentialStore.note}</p>
              ) : null}

              {form}

              {empty ? (
                <div className={styles.empty}>
                  <p className={styles.emptyTitle}>还没有模型连接</p>
                  <p className={styles.emptyText}>
                    添加一个 OpenAI 兼容连接后即可开始对话。工具能力在检测前只是"尚未确认"——普通对话照常可用；
                    只有**检测明确为不支持**时，需要工具的任务才会被拒绝，而且不会静默换模型。
                  </p>
                </div>
              ) : (
                <div className={styles.cards}>
                  {settings.connections.map((connection) => (
                    <ConnectionCard key={connection.id} connection={connection} actions={actions} />
                  ))}
                </div>
              )}
            </div>
          ) : null}
          </div>
          {/* 内容没到底时才出现的"滑到最底"箭头；搜索结果自己会滚，藏着它免得压在结果上。 */}
          {searching ? null : <ScrollToBottom paneRef={panesRef} resetKey={section} />}
          </div>
        </div>
      </div>
    </div>
  )
}
