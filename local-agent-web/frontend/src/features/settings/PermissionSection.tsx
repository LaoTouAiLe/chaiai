// 「设置 → 权限」分区（T38）：把"Agent 能不能碰工作区外的文件"做成用户点两下就能维护的东西。
//
// 分工与 `SettingsDialog` 一致：展示组件不认识 fetch，写入通过 `actions` 交回容器；
// 只有"添加授权"那个输入框的草稿留在本地（它是页面态，不是数据）。
//
// 这一分区**不许**出现的三种说法：
// 1. 把某一档说成"出厂默认"以外的东西——出厂默认是 `config.yaml` 的事实，由 `defaultMode` 说明；
// 2. 声称敏感操作也被放开了——删除文件、覆盖已有文件、破坏性 Shell、大范围编辑在任何档位下
//    都要人工批准（这是本任务最该钉死的一条）；
// 3. 把服务端拒绝的受保护目标（根目录、主目录、系统目录、Git 元数据）说成可以授权。

import { useState } from 'react'

import type {
  ExternalPathAccess,
  ExternalPathsMode,
  PermissionsView,
} from '../../fixtures/types'
import styles from './SettingsDialog.module.css'

export interface PermissionActions {
  onSelectMode: (mode: ExternalPathsMode) => void
  onAddGrant: (path: string, access: ExternalPathAccess) => void
  onRevokeGrant: (path: string) => void
  onReload: () => void
}

interface PermissionSectionProps {
  /** `null` = 还没读到（读失败也走这里，错误单独挂在 `errorText` 上）。 */
  permissions: PermissionsView | null
  /** 最近一次读写失败的用户可读文本；没有就不显示。 */
  errorText?: string | null
  /** 正在提交档位或授权（按钮据此禁用，防双击）。 */
  busy?: boolean
  actions?: PermissionActions
}

/** 三档的展示名与一句话解释；顺序固定，默认停在最保守的那一档。 */
const MODE_OPTIONS: readonly { mode: ExternalPathsMode; label: string; note: string }[] = [
  {
    mode: 'deny',
    label: '手动审批',
    note: '工作区外路径一律不可访问。需要在哪儿干活，就到「自定义」里把那个目录授权出来。',
  },
  {
    mode: 'whitelist',
    label: '自定义',
    note: '只放行下面列出的目录：只读授权只能读，读写授权视同工作区内。',
  },
  {
    mode: 'allow',
    label: '放行',
    note: '显式绝对路径的工作区外目标都可以读写，不需要逐个加目录。',
  },
]

const MODE_LABEL: Record<ExternalPathsMode, string> = {
  deny: '手动审批',
  whitelist: '自定义',
  allow: '放行',
}

const ACCESS_LABEL: Record<ExternalPathAccess, string> = {
  read: '只读',
  read_write: '读写',
}

/**
 * 权限分区。
 *
 * 授权列表**只在「自定义」档出现**：在「放行」档里让用户一条条加目录，与那个档位的名字
 * 自相矛盾（计划 T38 实现要点 5）。
 */
export function PermissionSection({
  permissions,
  errorText = null,
  busy = false,
  actions,
}: PermissionSectionProps) {
  const [draftPath, setDraftPath] = useState('')
  const [draftAccess, setDraftAccess] = useState<ExternalPathAccess>('read')

  const submitGrant = () => {
    const path = draftPath.trim()
    if (path === '') return
    actions?.onAddGrant(path, draftAccess)
    setDraftPath('')
  }

  return (
    <div className={styles.permission}>
      <div className={styles.sectionHead}>
        <div>
          <h3 className={styles.sectionTitle}>权限</h3>
          <p className={styles.sectionNote}>
            工作区外的文件默认不可访问。这里只决定"能不能碰、能怎么碰"；敏感操作在任何档位下都要你点过才执行。
          </p>
        </div>
      </div>

      {errorText === null ? null : (
        <p className={styles.formError} role="alert">
          {errorText}
          <button
            type="button"
            className={styles.ghost}
            disabled={busy}
            onClick={() => actions?.onReload()}
          >
            重新读取权限设置
          </button>
        </p>
      )}

      {permissions === null ? (
        <p className={styles.hint}>还没有读到权限设置——读到之前不显示任何档位。</p>
      ) : (
        <>
          {permissions.storeUnavailable ? (
            <p className={styles.storeWarning}>
              当前没有可用的设置库：这里改动的档位与授权只在本进程内有效，重启后回到出厂默认。
            </p>
          ) : null}

          <div className={styles.modeGroup} role="radiogroup" aria-label="工作区外路径">
            {MODE_OPTIONS.map((option) => {
              const selected = permissions.mode === option.mode
              return (
                <button
                  key={option.mode}
                  type="button"
                  role="radio"
                  aria-checked={selected}
                  aria-label={option.label}
                  disabled={busy}
                  className={`${styles.mode} ${selected ? styles.modeSelected : ''}`}
                  onClick={() => actions?.onSelectMode(option.mode)}
                >
                  <span className={styles.modeLabel}>{option.label}</span>
                  <span className={styles.modeNote}>{option.note}</span>
                </button>
              )
            })}
          </div>

          <p className={styles.hint}>
            出厂默认（config.yaml）：{MODE_LABEL[permissions.defaultMode]}；在设置库里保存过选择之后，以你的选择为准。改动从下一轮请求开始生效。
          </p>

          <p className={styles.hint}>
            删除文件、覆盖已有文件、破坏性 Shell 命令与大范围编辑，在以上任何一档下都必须人工批准；根目录、用户主目录、系统目录与 Git 元数据不能被授权。
          </p>

          {permissions.mode !== 'whitelist' ? null : (
            <div className={styles.grants}>
              {permissions.grants.length === 0 ? (
                <p className={styles.hint}>
                  还没有授权任何目录。空白名单与「手动审批」等效——工作区外路径仍然不可访问。
                </p>
              ) : (
                <ul className={styles.grantList}>
                  {permissions.grants.map((grant) => (
                    <li key={grant.path} className={styles.grantRow}>
                      <span className={styles.mono}>{grant.path}</span>
                      <span className={styles.grantAccess}>{ACCESS_LABEL[grant.access]}</span>
                      <button
                        type="button"
                        className={styles.action}
                        aria-label={`撤销授权 ${grant.path}`}
                        disabled={busy}
                        onClick={() => actions?.onRevokeGrant(grant.path)}
                      >
                        撤销
                      </button>
                    </li>
                  ))}
                </ul>
              )}

              <div className={styles.grantForm}>
                <input
                  className={styles.input}
                  type="text"
                  aria-label="授权目录"
                  placeholder="E:\data\shared（工作区外的绝对路径）"
                  value={draftPath}
                  disabled={busy}
                  onChange={(event) => setDraftPath(event.target.value)}
                />
                <select
                  className={styles.input}
                  aria-label="访问级别"
                  value={draftAccess}
                  disabled={busy}
                  onChange={(event) => setDraftAccess(event.target.value as ExternalPathAccess)}
                >
                  <option value="read">只读</option>
                  <option value="read_write">读写</option>
                </select>
                <button
                  type="button"
                  className={styles.primary}
                  disabled={busy || draftPath.trim() === ''}
                  onClick={submitGrant}
                >
                  添加授权
                </button>
              </div>
              <p className={styles.hint}>
                只能用绝对路径；`~`、相对路径与 Windows 盘符相对路径一律拒绝。路径会展开符号链接，并统一大小写与分隔符后保存。
              </p>
            </div>
          )}
        </>
      )}
    </div>
  )
}