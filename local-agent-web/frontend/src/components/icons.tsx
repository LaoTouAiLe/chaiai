/**
 * Inline monochrome icons.
 *
 * They inherit `currentColor` and carry no product marks: v0.4 must not reuse
 * the reference's brand or proprietary icon assets. (That rule came from
 * `docs/ui-reference/README.md` §4, removed 2026-10-26; the rule still holds.)
 */

interface IconProps {
  size?: number
}

export function ChevronRight({ size = 12 }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 12 12" aria-hidden="true" focusable="false">
      <path
        d="M4.5 2.5 8 6l-3.5 3.5"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  )
}

export function ChevronLeft({ size = 12 }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 12 12" aria-hidden="true" focusable="false">
      <path
        d="M7.5 2.5 4 6l3.5 3.5"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  )
}

export function ChevronDown({ size = 12 }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 12 12" aria-hidden="true" focusable="false">
      <path
        d="M2.5 4.5 6 8l3.5-3.5"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  )
}

export function Check({ size = 12 }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 12 12" aria-hidden="true" focusable="false">
      <path
        d="M2.5 6.5 5 9l4.5-6"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  )
}

export function Cross({ size = 12 }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 12 12" aria-hidden="true" focusable="false">
      <path
        d="M3 3l6 6M9 3l-6 6"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
      />
    </svg>
  )
}

export function WarningTriangle({ size = 14 }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 14 14" aria-hidden="true" focusable="false">
      <path
        d="M7 1.6 13 12.4H1L7 1.6Z"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.3"
        strokeLinejoin="round"
      />
      <path d="M7 5.6v3.1" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" />
      <circle cx="7" cy="10.6" r="0.8" fill="currentColor" />
    </svg>
  )
}

export function Copy({ size = 12 }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 12 12" aria-hidden="true" focusable="false">
      <rect x="1.5" y="1.5" width="6" height="6" rx="1.2" fill="none" stroke="currentColor" strokeWidth="1.2" />
      <path
        d="M4.5 10.5h4.4a1.6 1.6 0 0 0 1.6-1.6V4.5"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.2"
        strokeLinecap="round"
      />
    </svg>
  )
}

/**
 * 引用一段内容继续追问（2026-10-26）。
 *
 * 用一对引号而不是对话气泡：气泡已经表示"消息"，引号表示"把这段拎出来"，两个概念不能混。
 */
export function Quote({ size = 12 }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 12 12" aria-hidden="true" focusable="false">
      <path
        d="M5.1 2.6C3.4 3.4 2.4 4.7 2.4 6.2c0 1.3.8 2.2 1.9 2.2 1 0 1.7-.6 1.7-1.5 0-.8-.6-1.4-1.3-1.4-.2 0-.4 0-.5.1.3-.7.9-1.3 1.8-1.8l-.9-1.2Z"
        fill="currentColor"
      />
      <path
        d="M9.8 2.6C8.1 3.4 7.1 4.7 7.1 6.2c0 1.3.8 2.2 1.9 2.2 1 0 1.7-.6 1.7-1.5 0-.8-.6-1.4-1.3-1.4-.2 0-.4 0-.5.1.3-.7.9-1.3 1.8-1.8l-.9-1.2Z"
        fill="currentColor"
      />
    </svg>
  )
}

export function Pencil({ size = 12 }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 12 12" aria-hidden="true" focusable="false">
      <path
        d="M8.2 1.9 10.1 3.8 4.6 9.3 2 9.9l.6-2.6 5.6-5.4Z"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.2"
        strokeLinejoin="round"
      />
    </svg>
  )
}

export function Close({ size = 12 }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 12 12" aria-hidden="true" focusable="false">
      <path d="M3 3l6 6M9 3l-6 6" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  )
}

/** 「加图片」入口（2026-10-19）：图片框的轮廓，描述的是"图片"而不是"附件"。 */
export function ImagePlus({ size = 14 }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 14 14" aria-hidden="true" focusable="false">
      <rect x="1.6" y="2.4" width="10.8" height="9.2" rx="2" fill="none" stroke="currentColor" strokeWidth="1.3" />
      <circle cx="5" cy="5.8" r="1.05" fill="currentColor" />
      <path
        d="M2.4 10.4 5.7 7.4l2.5 2.2 1.6-1.4 1.8 1.7"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.3"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  )
}

export function Doc({ size = 12 }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 12 12" aria-hidden="true" focusable="false">
      <path
        d="M3 1.5h4l2.5 2.5v6.5H3V1.5Z"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.2"
        strokeLinejoin="round"
      />
      <path d="M7 1.8V4h2.3" fill="none" stroke="currentColor" strokeWidth="1.2" />
    </svg>
  )
}

/** 「欢迎」卡片的小灯泡：从零开始的想法正在被点亮。 */
export function Bulb({ size = 14 }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 14 14" aria-hidden="true" focusable="false">
      <path
        d="M7 1.6a4.1 4.1 0 0 1 2.5 7.4c-.4.3-.6.7-.6 1.2v.3H5.1v-.3c0-.5-.2-.9-.6-1.2A4.1 4.1 0 0 1 7 1.6Z"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.2"
        strokeLinejoin="round"
      />
      <path d="M5.6 12h2.8" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />
    </svg>
  )
}
