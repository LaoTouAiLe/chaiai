import type { CSSProperties } from 'react'
import type { ContextMeterView } from '../fixtures/types'
import { Close } from './icons'
import styles from './ContextMeter.module.css'

interface ContextMeterProps {
  meter: ContextMeterView
  /** The detail is open as a panel elsewhere, so the hover popover is suppressed. */
  open?: boolean
  /**
   * 点击圆环展开/收起详情面板。省略即**原型态**：圆环保持惰性、也不声明 `aria-expanded`。
   *
   * 悬停与聚焦本来就能打开浮层，所以省略它不会让数字读不到；但只有它存在时，
   * `aria-expanded` 与"展开上下文详情"这个动作型标签才是真的——**键盘用户走的就是这一步**
   * （matrix 22/23 要求"没有指针也能拿到同一份详情"）。
   */
  onToggle?: () => void
}

const SCOPE_LABEL: Record<ContextMeterView['scope'], string> = {
  current: '当前请求',
  previous: '上一请求',
  unknown: '尚未组装',
}

/** What each scope actually describes — the number must not be read as "this turn". */
const SCOPE_MEANING: Record<ContextMeterView['scope'], string> = {
  current: '正在运行的那一轮请求',
  previous: '装配器上一次构造的请求',
  unknown: '还没有组装出可计量的请求',
}

/** Compact token counts, always in `k`: `371k`, `1000k`, `842`. */
function formatTokens(value: number): string {
  if (value >= 1_000) return `${String(Math.round(value / 1000))}k`
  return String(value)
}

/** Exact counts for the hover/assistive text: `371,000`. */
function formatExact(value: number): string {
  return value.toLocaleString('en-US')
}

/**
 * The 口径 shared by the hover popover and the open panel.
 *
 * It states the two rules the numbers depend on — the window is confirmed, never
 * guessed, and cache baselines only compare within one connection version — so a
 * user cannot read the ring as an estimate of something else.
 */
function MeterDetail({ meter }: { meter: ContextMeterView }) {
  const known = meter.usedTokens !== null && meter.windowTokens !== null && meter.windowTokens > 0
  const percent =
    known && meter.usedTokens !== null && meter.windowTokens !== null
      ? Math.round(Math.min(meter.usedTokens / meter.windowTokens, 1) * 100)
      : null

  return (
    <>
      <dl className={styles.facts}>
        <div className={styles.fact}>
          <dt>占用</dt>
          <dd>
            {known
              ? `${formatExact(meter.usedTokens ?? 0)} / ${formatExact(meter.windowTokens ?? 0)} tokens`
              : '未知'}
          </dd>
        </div>
        <div className={styles.fact}>
          <dt>比例</dt>
          <dd>{percent === null ? '未知' : `${String(percent)}%`}</dd>
        </div>
        <div className={styles.fact}>
          <dt>口径</dt>
          <dd>
            {SCOPE_LABEL[meter.scope]} · {SCOPE_MEANING[meter.scope]}
          </dd>
        </div>
      </dl>
      <p className={styles.basis}>
        窗口必须由配置或可信的能力检测确认，不猜测；窗口未知时不显示比例。缓存命中只在相同连接版本与工具 Schema
        下比较；切换模型后本计量器按新窗口重新标记，旧轮次仍显示当时的窗口。
      </p>
    </>
  )
}

/**
 * Circular context meter.
 *
 * Shows the estimated input occupancy of the request the assembler last built,
 * next to the window confirmed by configuration or capability detection. When
 * either side is unknown the meter says so instead of inventing a ratio
 * (ARCHITECTURE §15.6).
 *
 * Hovering (or focusing) the ring opens the 口径 popover; the same detail is
 * available as a pinned panel through `MeterPanel`, so the numbers are reachable
 * without a pointer (matrix 22/23).
 */
export function ContextMeter({ meter, open = false, onToggle }: ContextMeterProps) {
  const known = meter.usedTokens !== null && meter.windowTokens !== null && meter.windowTokens > 0
  const ratio = known && meter.usedTokens !== null && meter.windowTokens !== null
    ? Math.min(meter.usedTokens / meter.windowTokens, 1)
    : 0
  const percent = Math.round(ratio * 100)
  const label = known
    ? `上下文已用 ${String(percent)}%（${formatTokens(meter.usedTokens ?? 0)} / ${formatTokens(meter.windowTokens ?? 0)}，${SCOPE_LABEL[meter.scope]}）`
    : `上下文用量未知（${SCOPE_LABEL[meter.scope]}）`

  // Hover reports the exact counts; the visible readout stays compact.
  const detail = known
    ? `上下文已用 ${String(percent)}%（${formatExact(meter.usedTokens ?? 0)} / ${formatExact(meter.windowTokens ?? 0)} tokens，${SCOPE_LABEL[meter.scope]}）`
    : `上下文用量未知（${SCOPE_LABEL[meter.scope]}）`

  const nearLimit = known && ratio >= 0.8

  return (
    <div className={styles.meter} title={detail}>
      <button
        type="button"
        className={styles.trigger}
        // 惰性按钮不谎称展开态：没有 handler 时连这个属性都不渲染（基线不受影响）。
        aria-expanded={onToggle === undefined ? undefined : open}
        aria-label={open ? '收起上下文详情' : '展开上下文详情'}
        aria-describedby={open ? undefined : 'context-meter-popover'}
        onClick={onToggle === undefined ? undefined : () => onToggle()}
      >
        <span
          className={known ? (nearLimit ? `${styles.ring} ${styles.ringWarn}` : styles.ring) : `${styles.ring} ${styles.ringUnknown}`}
          style={{ '--law-meter-ratio': `${String(ratio)}` } as CSSProperties}
          role="img"
          aria-label={label}
        >
          <span className={styles.hole} aria-hidden="true" />
        </span>
      </button>
      <span className={styles.readout}>
        {known ? `${formatTokens(meter.usedTokens ?? 0)} / ${formatTokens(meter.windowTokens ?? 0)}` : '未知'}
      </span>

      {open ? null : (
        <div className={styles.popover} role="tooltip" id="context-meter-popover">
          <MeterDetail meter={meter} />
        </div>
      )}
    </div>
  )
}

/** The same detail as a block panel: the ring's click target, pinned open. */
export function MeterPanel({ meter, onClose }: { meter: ContextMeterView; onClose?: () => void }) {
  return (
    <section className={styles.panel} aria-label="上下文详情">
      <header className={styles.panelHead}>
        <h2 className={styles.panelTitle}>上下文详情</h2>
        <button
          type="button"
          className={styles.panelClose}
          aria-label="收起上下文详情"
          onClick={onClose === undefined ? undefined : () => onClose()}
        >
          <Close />
        </button>
      </header>
      <MeterDetail meter={meter} />
    </section>
  )
}
