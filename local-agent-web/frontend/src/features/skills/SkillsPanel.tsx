// 「技能」分区（技能管理页，2026-10-16；契约 §16.5）。
//
// 这一页要回答三件事：**这台机器上发现了哪些技能**、**哪些被关掉了**、**关掉意味着什么**。三条硬规矩：
//   1. **只列发现到的技能**，逐条给"用户开关"；作者的声明（`disable-model-invocation`）另起一行说明
//      ——它是技能自己的写法，不是用户的开关，两者混在一起会让用户以为"我明明开着"；
//   2. **总闸与逐条开关分开说**：`skill` 能力关着时整页仍可读（用户要先看清再决定），但必须明说
//      "现在技能对模型完全不可见"；
//   3. **被跳过的条目如实列出**（`diagnostics`）——不说的话，用户只会看到"我的技能怎么不见了"。
//
// 展示组件不认识 fetch：数据由 `SkillsSection` 取好、映射成视图模型后传进来（与 `CapabilitiesPanel`
// 同一分工）。

import { useState } from 'react'

import type { SkillEntryView, SkillsView } from '../../fixtures/types'
import styles from './SkillsPanel.module.css'

interface SkillsPanelProps {
  /** `null` = 还没读到（读失败也走这里，错误单独挂在 `errorText` 上）。 */
  view: SkillsView | null
  /** 最近一次读/写失败的用户可读文本；没有就不显示。 */
  errorText?: string | null
  /** 改逐条开关：不传就是纯展示（测试与原型用）——每一条因此退回纯文本说明。 */
  actions?: {
    onToggle: (name: string, enabled: boolean) => void
    /** 添加一个发现根（2026-10-20）；不传就是纯展示。 */
    onAddRoot?: (path: string) => void
    /** 移除一个自定义发现根；只有 `removable` 的根会显示这个按钮。 */
    onRemoveRoot?: (path: string) => void
    /** 正在提交：开关据此禁用，防双击。 */
    busy?: boolean
  }
}

function SkillRow({
  skill,
  onToggle,
  busy,
}: {
  skill: SkillEntryView
  onToggle: ((name: string, enabled: boolean) => void) | null
  busy: boolean
}) {
  return (
    <li className={styles.row}>
      <div className={styles.rowHead}>
        <span className={styles.name}>{skill.name}</span>
        <span className={styles.source}>{skill.source}</span>
        <span className={`${styles.chip} ${skill.enabled ? styles.ok : styles.off}`}>
          {skill.enabled ? '可用' : '已禁用'}
        </span>
      </div>
      <p className={styles.description}>{skill.description}</p>
      {skill.declaredCapabilities.length === 0 ? null : (
        <p className={styles.meta}>
          技能自己声明只用到：{skill.declaredCapabilities.join('、')}（**只能收窄**本轮能力，不会扩权）
        </p>
      )}
      {skill.modelInvocable ? null : (
        // 作者的声明与用户的开关是两件事：这一条即使开着，模型也看不到它。
        <p className={styles.meta}>
          技能作者声明「只允许用户手动调用」（`disable-model-invocation`）——模型不会看到它，
          与这里的开关无关。
        </p>
      )}
      {onToggle === null ? null : (
        <label className={styles.toggle}>
          <input
            type="checkbox"
            checked={skill.enabled}
            disabled={busy}
            aria-label={`${skill.name}开关`}
            onChange={(event) => onToggle(skill.name, event.target.checked)}
          />
          <span>{skill.enabled ? '允许使用' : '已禁用（不列给模型，也取不到正文）'}</span>
        </label>
      )}
    </li>
  )
}

export function SkillsPanel({ view, errorText = null, actions }: SkillsPanelProps) {
  const onToggle = actions?.onToggle ?? null
  const onAddRoot = actions?.onAddRoot ?? null
  const onRemoveRoot = actions?.onRemoveRoot ?? null
  const [rootDraft, setRootDraft] = useState('')
  // 能改开关的条件：① 有人接线（真宿主）；② 设置库可用（否则改动存不下来）。缺一条就退回纯文本，
  // 不给一个点不动的控件。
  const canToggle = onToggle !== null && view !== null && !view.storeUnavailable
  const canEditRoots = onAddRoot !== null && view !== null && !view.storeUnavailable
  const busy = actions?.busy === true

  return (
    <div className={styles.root}>
      <div className={styles.head}>
        <div>
          <h3 className={styles.title}>技能</h3>
          <p className={styles.note}>
            技能是本机已有的工作手册（发现根里带 frontmatter 的 <code>SKILL.md</code>）：清单常驻、
            正文由模型按需读取。这里只决定「哪个技能可用」；关掉**不会删除任何文件**。
            {canToggle
              ? ' 改完下一次提交生效，不用重启服务。'
              : ' 没有可用的设置库时改动存不下来，这里只做展示。'}
          </p>
        </div>
      </div>

      {errorText === null ? null : (
        <p className={styles.error} role="alert">
          {errorText}
        </p>
      )}

      {view === null ? (
        <p className={styles.empty}>还没有读到技能清单——读到之前不显示任何一项。</p>
      ) : (
        <>
          {view.enabled ? null : (
            <p className={styles.masterOff}>
              技能能力当前<strong>关闭</strong>：这些技能对模型完全不可见（列在这里只是让你先看清再决定）。
              到「能力」面板打开「技能」后即可用。
            </p>
          )}

          {view.skills.length === 0 ? (
            <p className={styles.empty}>
              没有发现任何技能：可能是发现根没配、目录里缺少合法的 <code>SKILL.md</code>，
              或者都被禁用了。
            </p>
          ) : (
            <ul className={styles.list}>
              {view.skills.map((skill) => (
                <SkillRow
                  key={skill.name}
                  skill={skill}
                  onToggle={canToggle ? onToggle : null}
                  busy={busy}
                />
              ))}
            </ul>
          )}

          <section className={styles.roots} aria-label="技能发现根">
            <h4 className={styles.rootsTitle}>发现根</h4>
            <p className={styles.note}>
              技能在这些目录里被发现：**只引用目录，不复制文件**，作者更新技能后下一轮就生效。
              随包与个人级根不在这里移除；自己添加的可以随时移除（不删任何文件）。
            </p>
            <ul className={styles.rootList}>
              {view.roots.map((root) => (
                <li key={root.path} className={styles.rootRow}>
                  <span className={styles.source}>{root.source}</span>
                  <code className={styles.rootPath}>{root.path}</code>
                  <span className={styles.meta}>
                    {root.readable ? `发现 ${root.count} 个技能` : '目录不可读（不存在或没有权限）'}
                  </span>
                  {root.removable && onRemoveRoot !== null ? (
                    <button type="button" disabled={busy} onClick={() => onRemoveRoot(root.path)}>
                      移除
                    </button>
                  ) : null}
                </li>
              ))}
            </ul>
            {canEditRoots ? (
              <form
                className={styles.rootAdd}
                onSubmit={(event) => {
                  event.preventDefault()
                  const value = rootDraft.trim()
                  if (value === '') return
                  onAddRoot(value)
                  setRootDraft('')
                }}
              >
                <input
                  type="text"
                  aria-label="技能目录绝对路径"
                  placeholder={'C:\\Users\\你\\下载\\skills（绝对路径）'}
                  disabled={busy}
                  value={rootDraft}
                  onChange={(event) => setRootDraft(event.target.value)}
                />
                <button type="submit" disabled={busy || rootDraft.trim() === ''}>
                  添加目录
                </button>
              </form>
            ) : null}
          </section>

          {view.diagnostics.length === 0 ? null : (
            <details className={styles.diagnostics}>
              <summary>有 {view.diagnostics.length} 条技能被跳过（格式不合规或越界）</summary>
              <ul>
                {view.diagnostics.map((line) => (
                  <li key={line}>{line}</li>
                ))}
              </ul>
            </details>
          )}
        </>
      )}
    </div>
  )
}
