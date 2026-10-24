// 「能力」面板（v0.5 T56；契约 §16.1、§16.6）。
//
// 这个面板要回答三件事：**每一项开没开**、**开了能不能用**、**不能用时怎么办**。
// 三条硬规矩（写进契约的，不是这里的自由发挥）：
//   1. **只讲能力，不讲工具**——用户要判断的是"现在能不能联网、能不能提交"，不是内部绑了哪几个工具；
//   2. **不可用必须解释 + 给办法**——只灰掉不解释会让人以为功能坏了，而多数情况只是开关没打开；
//   3. **原因取自闭集**（`disabled` / `missing_dependency`）——页面不猜、不合并、不美化。
//
// 展示组件不认识 fetch：数据由 `CapabilitiesSection` 取好、映射成视图模型后传进来（与
// `PermissionSection` 同一分工）。

import type { CapabilityReason, CapabilitiesView } from '../../fixtures/types'
import styles from './CapabilitiesPanel.module.css'

interface CapabilitiesPanelProps {
  /** `null` = 还没读到（读失败也走这里，错误单独挂在 `errorText` 上）。 */
  view: CapabilitiesView | null
  /** 最近一次读取失败的用户可读文本；没有就不显示。 */
  errorText?: string | null
  /** 改开关：不传就是纯展示（测试与原型用）——每一项因此退回纯文本说明。 */
  actions?: {
    onToggle: (capability: string, enabled: boolean) => void
    /** 正在提交：开关据此禁用，防双击。 */
    busy?: boolean
  }
  /** 关闭面板；不传就是纯展示（测试与原型用）。 */
  onClose?: () => void
  /**
   * 嵌进设置分区（2026-10-19）：不再自带遮罩与对话框外壳，标题降为三级。
   * 独立弹窗与内嵌共用同一份行与状态，避免两处各写一份"能力"页面。
   */
  embedded?: boolean
}

/** 三种原因的展示名。**逐一对上后端的闭集**，不在这里加第四种。 */
const REASON_LABEL: Record<CapabilityReason, string> = {
  available: '可用',
  disabled: '开关已关闭',
  missing_dependency: '本机缺少依赖',
}

export function CapabilitiesPanel({
  view,
  errorText = null,
  actions,
  onClose,
  embedded = false,
}: CapabilitiesPanelProps) {
  // 能改开关的条件：① 有人接线（真宿主）；② 设置库可用（否则改动存不下来，§16.1 的降级口径）；
  // ③ 服务端给了可开关集（页面不自己拼能力名）。三条缺一条就退回纯文本，不给点不动的控件。
  const onToggle = actions?.onToggle ?? null
  const canToggle = onToggle !== null && view !== null && !view.storeUnavailable && view.switchable.length > 0
  const busy = actions?.busy === true
  const body = (
    <>
      <header className={styles.head}>
        <div>
          {embedded
            ? <h3 className={styles.embeddedTitle}>能力</h3>
            : <h2 className={styles.title}>能力</h2>}
          <p className={embedded ? styles.embeddedNote : styles.note}>
            这里只讲能力，不讲内部工具。每一项都会说明"开没开、能不能用、为什么、怎么才能用"。
            {canToggle ? (
              <>
                开关改完<strong>下一次提交生效，不用重启服务</strong>；装好依赖（例如 git）后重开这个
                面板就是最新结论。
              </>
            ) : (
              // 没有可写的设置库（或原型宿主没接线）时**不承诺**一个不存在的控件：
              // 这时开关只来自配置文件，改完必须重启服务（配置只在启动时读一次）。
              <>开关来自配置文件时改完要重启服务；装好依赖（例如 git）后重开这个面板就是最新结论。</>
            )}
          </p>
        </div>
        {onClose === undefined || embedded ? null : (
          <button type="button" className={styles.close} onClick={onClose}>
            关闭
          </button>
        )}
      </header>

      {errorText === null ? null : (
        <p className={styles.error} role="alert">
          {errorText}
        </p>
      )}

      {view === null ? (
        <p className={styles.detail}>还没有读到能力状态——读到之前不显示任何一项。</p>
      ) : (
        <ul className={styles.list}>
          {view.capabilities.map((row) => (
            <li key={row.capability} className={styles.row}>
              <div className={styles.rowHead}>
                <span className={styles.label}>{row.label}</span>
                <span
                  className={`${styles.chip} ${row.available ? styles.ok : styles.off}`}
                  data-reason={row.reason}
                >
                  {REASON_LABEL[row.reason]}
                </span>
              </div>
              <p className={styles.detail}>{row.detail}</p>
              {row.hint === null ? null : <p className={styles.hint}>→ {row.hint}</p>}
              {row.locked ? (
                // 基础能力（§16.1）：说明为什么没有开关，而不是给一个点不动的控件。
                <p className={styles.locked}>始终开启</p>
              ) : onToggle !== null && canToggle && view.switchable.includes(row.capability) ? (
                <label className={styles.toggle}>
                  {/* 与设置里的布尔项同一套开关外观：能力页现在也长在设置里，两种复选框并排会很怪。 */}
                  <span className={styles.switch}>
                    <input
                      type="checkbox"
                      aria-label={`${row.label}开关`}
                      checked={view.switches[row.capability] === true}
                      disabled={busy}
                      onChange={(event) => onToggle(row.capability, event.target.checked)}
                    />
                    <span className={styles.switchTrack} aria-hidden="true" />
                  </span>
                  <span>{view.switches[row.capability] === true ? '已开启' : '已关闭'}</span>
                </label>
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </>
  )

  if (embedded) return <div className={styles.embedded}>{body}</div>
  return (
    <div className={styles.overlay} role="presentation">
      <section className={styles.dialog} role="dialog" aria-modal="true" aria-label="能力">
        {body}
      </section>
    </div>
  )
}
