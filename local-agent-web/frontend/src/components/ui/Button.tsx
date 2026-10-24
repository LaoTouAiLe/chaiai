import type { ButtonHTMLAttributes, ReactNode } from 'react'

import styles from './primitives.module.css'

export type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger' | 'text' | 'icon'
export type ButtonSize = 'sm' | 'default' | 'lg'

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant
  size?: ButtonSize
  /** 撑满容器宽度（空态主按钮、表单底部）。 */
  block?: boolean
  children?: ReactNode
}

const SIZE_CLASS: Record<ButtonSize, string> = {
  sm: styles.sizeSm,
  default: styles.sizeDefault,
  lg: styles.sizeLg,
}

const ICON_SIZE_CLASS: Record<ButtonSize, string> = {
  sm: styles.iconSm,
  default: styles.iconDefault,
  lg: styles.iconLg,
}

const VARIANT_CLASS: Record<Exclude<ButtonVariant, 'icon'>, string> = {
  primary: styles.variantPrimary,
  secondary: styles.variantSecondary,
  ghost: styles.variantGhost,
  danger: styles.variantDanger,
  text: styles.variantText,
}

/**
 * 唯一的按钮实现（§7.1）。
 *
 * 调用点只传 `variant` / `size`。**不要**用 `className` 覆写高度、内边距、圆角或字号：
 * 同一个"次要按钮"在不同页面长得不一样，正是这一批要消除的问题。
 *
 * `variant="icon"` 是正方图标按钮，必须同时给 `aria-label`（图标本身不承载语义）。
 */
export function Button({
  variant = 'secondary',
  size = 'default',
  block = false,
  className,
  type = 'button',
  children,
  ...rest
}: ButtonProps) {
  const variantClass = variant === 'icon' ? undefined : VARIANT_CLASS[variant]
  const sizeClass = variant === 'icon' ? ICON_SIZE_CLASS[size] : SIZE_CLASS[size]
  return (
    <button
      // 默认 button：表单里的"取消"若按 Enter 提交会意外触发保存。
      type={type}
      className={[styles.button, variantClass, sizeClass, block ? styles.block : null, className]
        .filter(Boolean)
        .join(' ')}
      {...rest}
    >
      {children}
    </button>
  )
}
