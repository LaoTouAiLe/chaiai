import type { RefObject } from 'react'
import { Check, Close } from '../../components/icons'
import { AnchoredPopover } from '../../components/ui/AnchoredPopover'
import type { ConnectionStatus, ModelMenuView } from '../../fixtures/types'
import styles from './ModelMenu.module.css'

interface ModelMenuProps {
  menu: ModelMenuView
  onClose?: () => void
  anchorRef?: RefObject<HTMLElement | null>
  actions?: {
    busy: boolean
    onSelect: (connectionId: string) => void
    onFollowDefault: () => void
    onManage: () => void
  }
}

const STATUS_LABEL: Record<ConnectionStatus, string> = {
  verified: '已验证',
  unverified: '未验证',
  testing: '检测中',
  failed: '检测失败',
  unsupported: '不支持工具',
}

/**
 * Session model menu.
 *
 * The switch only affects the next submit: the running turn keeps the connection
 * it was pinned to, and the menu says so instead of implying an immediate change
 * (ARCHITECTURE §15.9).
 *
 * Anchored to the model trigger; the list scrolls without resizing the conversation.
 */
export function ModelMenu({ menu, actions, onClose, anchorRef }: ModelMenuProps) {
  return (
    <AnchoredPopover label="模型选择" anchorRef={anchorRef} onClose={onClose}>
    <section className={styles.menu}>
      <div className={styles.head}>
        <span className={styles.headTitle}>会话模型</span>
        <span className={styles.headHint}>切换只影响下一次提交</span>
        <button type="button" className={styles.close} aria-label="关闭模型选择"
          disabled={onClose === undefined} onClick={onClose}><Close /></button>
      </div>

      <ul className={styles.list} aria-label="模型连接">
        {menu.connections.map((connection) => {
          const selected = connection.id === menu.selectedId
          return (
            <li key={connection.id}>
              <button
                type="button"
                className={selected ? `${styles.item} ${styles.itemSelected}` : styles.item}
                aria-pressed={selected}
                disabled={actions?.busy === true}
                onClick={actions === undefined ? undefined : () => actions.onSelect(connection.id)}
              >
                <span className={styles.check} aria-hidden="true">
                  {selected ? <Check /> : null}
                </span>
                <span className={styles.name}>{connection.name}</span>
                <span className={styles.model}>{connection.modelId}</span>
                <span className={styles.status}>{STATUS_LABEL[connection.status]}</span>
              </button>
            </li>
          )
        })}
      </ul>

      {menu.nextTurnNotice === null ? null : <p className={styles.notice}>{menu.nextTurnNotice}</p>}
      {actions !== undefined && menu.followsDefault === false ? (
        <button type="button" className={styles.manage} disabled={actions.busy} onClick={actions.onFollowDefault}>
          恢复跟随默认模型
        </button>
      ) : null}
      {menu.pinnedLabel === null ? null : <p className={styles.pinned}>正在运行的一轮：{menu.pinnedLabel}</p>}

      <button type="button" className={styles.manage} disabled={actions?.busy === true} onClick={actions?.onManage}>
        管理模型连接…
      </button>
    </section>
    </AnchoredPopover>
  )
}
