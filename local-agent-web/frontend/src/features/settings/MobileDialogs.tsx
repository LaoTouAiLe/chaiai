// 移动访问的三个确认框（2026-11-03）。
//
// 全部走项目自己的 `EditorDialog`，**不用 `window.prompt` / `window.confirm`**：原生框在这个界面里
// 长得像另一个程序，而且 prompt 没法做长度限制、不能取消后恢复焦点、在 PWA 里还会被系统样式打断。
// 这三个动作一个比一个重（改名 → 撤销 → 紧急关闭），越重越该用正式对话框把话说清楚。

import { useState } from 'react'

import { EditorDialog } from '../../components/ui/EditorDialog'
import type { MobileDeviceDto } from '../../api/client'
import shared from './PreferenceSection.module.css'
import styles from './MobileSection.module.css'

/** 重命名。默认值就是当前名字，选中全部，直接打字即可覆盖。 */
export function RenameDeviceDialog({
  device,
  busy,
  onClose,
  onSubmit,
}: {
  device: MobileDeviceDto
  busy: boolean
  onClose: () => void
  onSubmit: (label: string) => void
}): React.ReactElement {
  const [label, setLabel] = useState(device.label)
  const trimmed = label.trim()

  return (
    <EditorDialog
      title="重命名设备"
      description="只影响显示，不动授权——改完那台手机照常可用。"
      onClose={onClose}
    >
      <form
        onSubmit={(event) => {
          event.preventDefault()
          if (!trimmed || busy) return
          onSubmit(trimmed)
        }}
      >
        <label className={styles.dialogField}>
          <span>设备名</span>
          <input
            // 自动选中全文：用户多半是整段替换，不是插入。
            autoFocus
            maxLength={64}
            value={label}
            onChange={(event) => setLabel(event.target.value)}
            onFocus={(event) => event.currentTarget.select()}
          />
        </label>
        <div className={styles.dialogActions}>
          <button type="button" onClick={onClose}>
            取消
          </button>
          <button type="submit" className={shared.primary} disabled={busy || trimmed === ''}>
            {busy ? '正在保存…' : '保存'}
          </button>
        </div>
      </form>
    </EditorDialog>
  )
}

/** 撤销一台设备。破坏性操作，把后果写全：什么时候失效、要不要重新配对。 */
export function RevokeDeviceDialog({
  device,
  busy,
  onClose,
  onConfirm,
}: {
  device: MobileDeviceDto
  busy: boolean
  onClose: () => void
  onConfirm: () => void
}): React.ReactElement {
  return (
    <EditorDialog
      title="撤销这台设备？"
      description={`「${device.label}」会立刻失去访问权限，下一次请求就会被拒绝。`}
      onClose={onClose}
    >
      <p className={styles.dialogText}>
        撤销是<strong>删除记录</strong>，不是打标记。那台手机要重新扫码配对才能再用。
      </p>
      <div className={styles.dialogActions}>
        <button type="button" onClick={onClose}>
          取消
        </button>
        <button type="button" className={styles.danger} disabled={busy} onClick={onConfirm}>
          {busy ? '正在撤销…' : '撤销这台设备'}
        </button>
      </div>
    </EditorDialog>
  )
}

/**
 * 紧急关闭。
 *
 * 两个决定放在同一个框里：**停网关**与**撤销全部设备**。默认只停网关——那是可逆的；勾上撤销
 * 才会清授权，且必须让用户看到"之后要重新配对"这句话，否则他会以为只是关了一下。
 */
export function EmergencyStopDialog({
  running,
  deviceCount,
  busy,
  onClose,
  onConfirm,
}: {
  running: boolean
  deviceCount: number
  busy: boolean
  onClose: () => void
  onConfirm: (revokeDevices: boolean) => void
}): React.ReactElement {
  const [revoke, setRevoke] = useState(false)

  return (
    <EditorDialog
      title="紧急关闭移动访问"
      description="立刻断开所有手机。这一步不等配置、不等重启。"
      onClose={onClose}
    >
      <p className={styles.dialogText}>
        {running
          ? '网关会立刻停止，正在连接的手机全部断开。'
          : '网关当前没有在运行；这里的动作主要是清理授权。'}
      </p>
      <label className={styles.dialogCheck}>
        <input
          type="checkbox"
          checked={revoke}
          onChange={(event) => setRevoke(event.target.checked)}
        />
        <span>
          同时撤销全部 {deviceCount} 台设备的授权
          <small>
            只停网关的话，重新开启后旧 Token 依然有效。怀疑令牌泄露时必须勾上这个，勾了之后每台手机都要
            重新扫码配对。
          </small>
        </span>
      </label>
      <div className={styles.dialogActions}>
        <button type="button" onClick={onClose}>
          取消
        </button>
        <button
          type="button"
          className={styles.danger}
          disabled={busy}
          onClick={() => onConfirm(revoke)}
        >
          {busy ? '正在关闭…' : revoke ? '关闭并撤销全部设备' : '关闭网关'}
        </button>
      </div>
    </EditorDialog>
  )
}
