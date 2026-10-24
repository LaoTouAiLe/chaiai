import type { InputHTMLAttributes } from 'react'

import styles from './primitives.module.css'

interface SearchFieldProps extends Omit<InputHTMLAttributes<HTMLInputElement>, 'type' | 'onChange' | 'value'> {
  value: string
  onChange: (value: string) => void
  /** 有内容时是否显示清除按钮。默认显示。 */
  clearable?: boolean
}

/**
 * 唯一的搜索框实现（§7.3）。
 *
 * 无边框、白底、聚焦才出现强调色描边。**不要**再手写带边框的 `<input type="search">`：
 * 仓库里现在每个页面的搜索框高度、圆角、底色都不一样。
 *
 * 白底（`--c-raised`）而不是底槽色，是因为多数搜索框落在 `--c-panel` 上，
 * 两者明度太接近会看不出输入区域（2026-10-22 截图复核，见 §9.2）。
 */
export function SearchField({ value, onChange, clearable = true, placeholder, ...rest }: SearchFieldProps) {
  const showClear = clearable && value.length > 0
  return (
    <span className={showClear ? `${styles.search} ${styles.searchWithClear}` : styles.search}>
      <input
        type="search"
        value={value}
        placeholder={placeholder}
        onChange={(event) => onChange(event.target.value)}
        {...rest}
      />
      {showClear ? (
        <button
          type="button"
          className={styles.clear}
          aria-label="清除搜索"
          onClick={() => onChange('')}
        >
          ×
        </button>
      ) : null}
    </span>
  )
}
