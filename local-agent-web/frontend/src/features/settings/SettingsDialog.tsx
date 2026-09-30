import { useState } from 'react'
import type { ReactNode } from 'react'

import { Check, Close, Cross } from '../../components/icons'
import type {
  CapabilityView,
  ConnectionStatus,
  ModelConnectionView,
  SettingsView,
} from '../../fixtures/types'
import { PermissionSection } from './PermissionSection'
import type { PermissionActions } from './PermissionSection'
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
}

type SettingsSection = 'models' | 'permissions'

/** 分节切换器：两个分区都是真的（模型连接、权限），不是摆着好看的空壳（T38）。 */
const SECTIONS: readonly { id: SettingsSection; label: string }[] = [
  { id: 'models', label: '模型连接' },
  { id: 'permissions', label: '权限' },
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
  return (
    <article className={styles.card} aria-label={`连接 ${connection.name}`}>
      <header className={styles.cardHead}>
        <h3 className={styles.cardName}>{connection.name}</h3>
        {connection.isDefault ? <span className={styles.defaultTag}>默认</span> : null}
        <span className={`${styles.status} ${STATUS_CLASS[connection.status]}`}>
          {STATUS_LABEL[connection.status]}
        </span>
        <span className={styles.cardId}>{connection.id}</span>
      </header>

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
}: SettingsDialogProps) {
  const [section, setSection] = useState<SettingsSection>('models')
  const empty = settings.connections.length === 0
  return (
    <div className={styles.overlay}>
      <div className={styles.dialog} role="dialog" aria-modal="true" aria-label="设置">
        <header className={styles.header}>
          <div>
            <h2 className={styles.title}>设置</h2>
            <p className={styles.subtitle}>模型连接与权限 · 密钥只写入不回读</p>
          </div>
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
          <div className={styles.tabs} role="tablist" aria-label="设置分区">
            {SECTIONS.map((item) => (
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

          {section === 'permissions' ? (
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
          ) : (
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
          )}
        </div>
      </div>
    </div>
  )
}
