import { useState, type ComponentPropsWithoutRef } from 'react'
import type { ExtraProps } from 'react-markdown'
import { copyText } from './clipboard'
import styles from './MarkdownTable.module.css'

/** Keep the table semantic; the surrounding region owns horizontal scrolling. */
export function MarkdownTable({ node, children, ...props }: ComponentPropsWithoutRef<'table'> & ExtraProps) {
  const [copied, setCopied] = useState<boolean | null>(null)
  const columns = Number(node?.properties['data-table-columns'] ?? 0)
  const source = String(node?.properties['data-table-tsv'] ?? '')
  return <div className={styles.frame} data-answer-table data-columns={columns}>
    <div className={styles.toolbar}>
      <span>表格 · {columns} 列</span>
      <button type="button" aria-label="复制表格" onClick={() => { void copyText(source).then(setCopied) }}>
        {copied === null ? '复制表格' : copied ? '已复制' : '复制失败'}
      </button>
    </div>
    <div className={styles.scroll} tabIndex={0} role="region" aria-label="表格内容，可横向滚动" data-wide={columns >= 4}>
      <table {...props}>{children}</table>
    </div>
  </div>
}
