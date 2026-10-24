import { useState, type ReactNode } from 'react'
import styles from './Notice.module.css'

export function Notice({ children, label, scope, role }: {
  children: ReactNode; label: string; scope: unknown; role?: 'status' | 'alert'
}) {
  const [dismissed, setDismissed] = useState<{ scope: unknown } | null>(null)
  if (dismissed !== null && dismissed.scope === scope) return null
  return <div className={styles.notice} role={role}>
    <div className={styles.content}>{children}</div>
    <button type="button" className={styles.close} aria-label={`关闭${label}`}
      onClick={() => setDismissed({ scope })}>×</button>
  </div>
}
