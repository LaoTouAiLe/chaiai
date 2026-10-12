// 「能力与档位」面板（v0.5 T56；契约 §16.6）。
//
// 这个面板要回答三件事：**当前是哪个档位**、**每个能力能不能用**、**不能用时怎么办**。
// 三条硬规矩（写进契约的，不是这里的自由发挥）：
//   1. **只讲能力，不讲工具**——用户要判断的是"现在能不能联网、能不能提交"，不是内部绑了哪几个工具；
//   2. **不可用必须解释 + 给办法**——只灰掉不解释会让人以为功能坏了，而多数情况只是档位或开关；
//   3. **原因取自闭集**（`profile` / `disabled` / `missing_dependency`）——页面不猜、不合并、不美化。
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
  /** 闭合动作（档位切换）：不传就是纯展示（测试与原型用）——档位那一行因此退回纯文本。 */
  actions?: {
    onSelectProfile: (profile: string) => void
    /** 正在提交：选择器据此禁用，防双击。 */
    busy?: boolean
  }
  /** 关闭面板；不传就是纯展示（测试与原型用）。 */
  onClose?: () => void
}

/** 四种原因的展示名。**逐一对上后端的闭集**，不在这里加第五种。 */
const REASON_LABEL: Record<CapabilityReason, string> = {
  available: '可用',
  profile: '当前档位没有',
  disabled: '开关已关闭',
  missing_dependency: '本机缺少依赖',
}

export function CapabilitiesPanel({
  view,
  errorText = null,
  actions,
  onClose,
}: CapabilitiesPanelProps) {
  // 能切换的条件：① 有人接线（真宿主）；② 设置库可用（否则改动存不下来，§16.6 的降级口径）；
  // ③ 服务端给了可选集（页面不自己拼档位名）。三条缺一条就退回纯文本，不给点不动的控件。
  const switchable =
    actions !== undefined && view !== null && !view.storeUnavailable && view.profiles.length > 0
  return (
    <div className={styles.overlay} role="presentation">
      <section className={styles.dialog} role="dialog" aria-modal="true" aria-label="能力与档位">
        <header className={styles.head}>
          <div>
            <h2 className={styles.title}>能力与档位</h2>
            <p className={styles.note}>
              这里只讲能力，不讲内部工具。每一项都会说明"能不能用、为什么、怎么才能用"。
              {switchable ? (
                <>
                  <strong>档位可以在下面直接切换</strong>（下一次提交生效，不用重启）；config.yaml 里的
                  开关与自定义档位仍要改完重启服务才生效。
                </>
              ) : (
                // 没有可写的设置库（或原型宿主没接线）时**不承诺**一个不存在的控件：
                // 这时档位只来自 config.yaml，改完必须重启服务（配置只在启动时读一次）。
                <>装好依赖（例如 git）后重开这个面板就是最新结论；config.yaml 里的档位与开关要改完重启服务才生效。</>
              )}
            </p>
          </div>
          {onClose === undefined ? null : (
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
          <>
            <p className={styles.profile}>
              当前档位：
              {switchable ? (
                <select
                  className={styles.profileSelect}
                  aria-label="切换当前档位"
                  value={view.profile}
                  disabled={actions.busy === true}
                  onChange={(event) => actions.onSelectProfile(event.target.value)}
                >
                  {view.profiles.map((name) => (
                    <option key={name} value={name}>
                      {name}
                    </option>
                  ))}
                </select>
              ) : (
                <span className={styles.profileValue}>{view.profile}</span>
              )}
              {/* 档位来自配置文件（设置库不可用）时如实说明：控件已经退回纯文本，别让用户以为面板坏了。 */}
              {view.storeUnavailable ? (
                <span className={styles.profileNote}>设置库不可用，档位来自配置文件</span>
              ) : null}
            </p>
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
                </li>
              ))}
            </ul>
          </>
        )}
      </section>
    </div>
  )
}
