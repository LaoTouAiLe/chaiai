import { useMemo, useState, type ReactNode } from 'react'
import styles from './StructuredResult.module.css'

function JsonValue({ label, value, depth = 0 }: { label: string; value: unknown; depth?: number }) {
  const [shown, setShown] = useState(20)
  if (typeof value !== 'object' || value === null) return <div className={styles.value}>
    <span className={styles.key}>{label}</span><span data-value-type={value === null ? 'null' : typeof value}>{typeof value === 'string' ? value : String(value)}</span>
  </div>
  const entries = Object.entries(value)
  return <details className={styles.branch} open={depth === 0 || undefined}>
    <summary><span className={styles.key}>{label}</span><span className={styles.count}>{Array.isArray(value) ? '数组' : '对象'} · {entries.length} 项</span></summary>
    {depth >= 6 ? <pre>{JSON.stringify(value, null, 2)}</pre> : <div className={styles.children}>
      {entries.slice(0, shown).map(([key, item]) => <JsonValue key={key} label={Array.isArray(value) ? `[${key}]` : key} value={item} depth={depth + 1} />)}
      {entries.length > shown ? <button type="button" className={styles.more} onClick={() => setShown(count => count + 20)}>还有 {entries.length - shown} 项，显示更多</button> : null}
    </div>}
  </details>
}

/** Inspect only a complete JSON object/array; malformed or oversized output stays verbatim. */
export function StructuredResult({ text, fallback }: { text: string; fallback: ReactNode }) {
  const parsed: unknown = useMemo(() => {
    if (text.length > 60_000) return null
    const first = text.trimStart()[0]
    if (first !== '[' && first !== '{') return null
    try { return JSON.parse(text) as unknown } catch { return null }
  }, [text])
  return parsed !== null && typeof parsed === 'object'
    ? <div className={styles.root} aria-label="结构化结果"><JsonValue label="结果" value={parsed} /></div>
    : fallback
}
