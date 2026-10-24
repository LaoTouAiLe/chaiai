import styles from './primitives.module.css'

export interface SegmentedOption<T extends string> {
  value: T
  label: string
}

/**
 * 2–4 项互斥选择的唯一实现（§7.6）。
 *
 * 用来替代"一排小胶囊按钮"：容器下沉、选中项上浮，用明度差表达当前位置。
 * 超过 4 项不要用它——那是导航，不是分段选择。
 */
export function SegmentedControl<T extends string>({
  options,
  value,
  onChange,
  ariaLabel,
  size = 'default',
}: {
  options: readonly SegmentedOption<T>[]
  value: T
  /** 省略即只读：原型/静态基线下页签只显示当前态，不产生跳转。 */
  onChange?: (value: T) => void
  ariaLabel: string
  size?: 'sm' | 'default'
}) {
  const sizeClass = size === 'sm' ? styles.segmentSm : undefined
  return (
    <div className={styles.segmented} role="tablist" aria-label={ariaLabel}>
      {options.map((option) => {
        const active = option.value === value
        return (
          <button
            key={option.value}
            type="button"
            role="tab"
            aria-selected={active}
            className={[styles.segment, sizeClass, active ? styles.segmentActive : null]
              .filter(Boolean)
              .join(' ')}
            onClick={onChange === undefined ? undefined : () => onChange(option.value)}
          >
            {option.label}
          </button>
        )
      })}
    </div>
  )
}
