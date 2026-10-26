// 「设置 → 权限」分区（T38）：把"Agent 能不能碰工作区外的文件"做成用户点两下就能维护的东西。
//
// 分工与 `SettingsDialog` 一致：展示组件不认识 fetch，写入通过 `actions` 交回容器；
// 只有"添加授权"那个输入框的草稿留在本地（它是页面态，不是数据）。
//
// 这一分区**不许**出现的三种说法：
// 1. 把某一档说成"出厂默认"以外的东西——出厂默认是 `config.yaml` 的事实，由 `defaultMode` 说明；
// 2. 声称可写授权放宽文件工具的敏感操作或保护目标；Shell 模式识别不能保证脚本逐项审批；
// 3. 把服务端拒绝的受保护目标（根目录、主目录、系统目录、Git 元数据）说成可以授权；
// 4. 只讲路径不讲 Shell——每一档都必须同时说清"工作区外能碰哪里"与"哪些命令免批准"（2026-10-14：
//    以前三档的说明只讲路径，用户选了「放行」却每条命令都还在被问，那一页等于没解释到位）。

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
  /**
   * 唤起本机目录选择器并返回绝对路径；取消返回 `null`（2026-10-26）。
   *
   * 由容器注入而不是在这里直接调：本组件按约定不认识 fetch，只有容器才碰 `/api/*`。
   * 省略表示"这个宿主不提供选择器"——那时按钮不渲染，输入框照旧可用。
   */
  onPickDirectory?: () => Promise<string | null>
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

/** 三档的展示名与一句话解释；顺序从最严到最宽，出厂默认是「工作区可写」。
 *
 * 2026-10-15 权限模型重构：档位回答的是**"能写到哪"**（对齐 codex / dsh），不再是"工作区外能不能读"。
 * 写侧沙箱并入档位（没有独立开关），所以每一档除了说路径，还必须说清 **Shell 命令是否进受限令牌**、
 * 哪些命令仍要批准——缺了后半句，用户就会以为档位只跟文件有关。
 */
const MODE_OPTIONS: readonly { mode: ExternalPathsMode; label: string; note: string }[] = [
  {
    mode: 'read-only',
    label: '只读',
    note:
      '本机外部路径也可读取、列目录和搜索，敏感文件仍受保护；新建、修改、删除文件一律拒绝。' +
      'Windows Shell 使用无可写 SID 的受限令牌限制写入。',
  },
  {
    mode: 'workspace-write',
    label: '工作区可写',
    note:
      '工作区内正常读写；本机外部路径默认只读，下面授权的可写目录可读写。' +
      '常规 Shell 命令免批准，破坏性命令与越界请求仍要你点过。',
  },
  {
    mode: 'danger-full-access',
    label: '常规免审',
    note:
      '文件工具可读取显式绝对路径的工作区外目标；文件工具向外写仍需在下面授权为读写。' +
      'Windows Shell 同样限制为工作区、专属临时目录与已授权目录可写；常规命令免批准。',
  },
]

const MODE_LABEL: Record<ExternalPathsMode, string> = {
  'read-only': '只读',
  'workspace-write': '工作区可写',
  'danger-full-access': '常规免审',
}

const ACCESS_LABEL: Record<ExternalPathAccess, string> = {
  read: '旧只读记录（读取已默认允许）',
  read_write: '可写授权',
}

/**
 * 权限分区。
 *
 * 可写目录在三档下都显示；只读档保留授权记录但不允许写入。
 * 旧只读记录继续展示和移除，不再控制外部读取；新建授权统一为 read_write。
 */
export function PermissionSection({
  permissions,
  errorText = null,
  busy = false,
  actions,
}: PermissionSectionProps) {
  const [draftPath, setDraftPath] = useState('')
  const [picking, setPicking] = useState(false)

  const submitGrant = () => {
    const path = draftPath.trim()
    if (path === '') return
    actions?.onAddGrant(path, 'read_write')
    setDraftPath('')
  }

  /** 唤起本机目录选择器（2026-10-26）：取消时保持输入框原样，不报错。 */
  const pickDirectory = async () => {
    if (actions?.onPickDirectory === undefined) return
    setPicking(true)
    try {
      const picked = await actions.onPickDirectory()
      if (picked !== null && picked !== '') setDraftPath(picked)
    } finally {
      setPicking(false)
    }
  }

  return (
    <div className={styles.permission}>
      <div className={styles.sectionHead}>
        <div>
          <h3 className={styles.sectionTitle}>权限</h3>
          <p className={styles.sectionNote}>
            本机外部路径可直接读取、列目录和搜索，敏感文件除外；档位决定写入范围与 Shell 的写侧沙箱。
            外部路径必须明确使用绝对路径，不能用相对路径或 `~` 指定；目录授权用于允许外部写入。
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
              当前没有可用的设置库：这里只显示启动初值，档位与授权修改不能保存。
            </p>
          ) : null}

          <div className={styles.modeGroup} role="radiogroup" aria-label="权限档位">
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
            「只读」档直接拒绝文件工具写入；可写档位下，工作区内编辑、覆盖和删除默认免审批，外部写入需要目录授权。
            已识别的破坏性 Shell 命令在任何档位下都须人工批准；根目录、用户主目录、系统目录与 Git 元数据不能被授权写入。
          </p>

          <div className={styles.sandbox}>
            <p className={styles.modeLabel}>写侧沙箱（run_shell）</p>
            {/* 2026-10-15 权限模型重构：写侧沙箱不再是独立开关，由档位派生，所以这里只有说明没有控件。 */}
            <p className={styles.hint}>
              Windows 上各档位均通过受限令牌约束命令写入。「只读」不授予写权限；其余档位可写工作区、专属临时目录和你授权的读写目录，凭据子目录受保护。
              其它平台没有 OS 级写边界：路径工具受档位限制，获批命令内部的文件访问无法据此约束。
            </p>
            <p className={styles.hint}>
              「工作区可写」档的新建目录立即继承可写（根上一条可继承 ACE）；首次扫描后新建的凭据目录，要到下次进程启动扫描时才加入写保护。
              读侧与网络不隔离；对象已有 Everyone 或登录 SID
              写权限、硬链接及并发改名仍有边界；各机器的命令兼容性需实际验证。
            </p>
            {/* 2026-10-15（裁定 B2）：把"出这种问题时怎么想"写在说明旁边。
                实证：工作区里有一个属主是别的账户的目录时，沙箱改不动它的权限——从前这里会
                让整个 run_shell 不可用；现在跳过它、命令照跑，但该处的实际写权限尚未核实。用户看到
                "写失败"却想不到原因，这一句就是那个原因与两条修法。 */}
            <p className={styles.hint}>
              工作区里如果有当前账户无权修改权限的目录（例如属主是别的账户，或工作区在 U 盘、
              网络盘上），普通目录无法枚举时沙箱会跳过它们，并在每次工具结果里说明；这些目录的实际
              写权限与内部保护范围未完成核实。受保护子树的写拒绝项建立失败时会阻止命令执行。
              需要访问被跳过的普通目录时，可以让那一条命令请求提权（会弹审批、只对这一次生效），
              或先取得该目录的修改权限；提权会退出本条命令的整个写侧沙箱。
            </p>
          </div>

          <div className={styles.grants}>
            <p className={styles.modeLabel}>外部可写目录</p>
            <p className={styles.hint}>
              授权包含目录及其子目录，保存后持续生效、可以撤销；只读档下仍不能写入。撤销写入授权不影响默认读取。
            </p>
            {permissions.grants.length === 0 ? (
              <p className={styles.hint}>
                尚未添加外部可写目录。本机外部读取无需授权；要在外部创建、修改或删除文件，请先添加可写目录。
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
                        aria-label={`${grant.access === 'read' ? '移除只读记录' : '撤销写入授权'} ${grant.path}`}
                        disabled={busy}
                        onClick={() => actions?.onRevokeGrant(grant.path)}
                      >
                        {grant.access === 'read' ? '移除记录' : '撤销写入'}
                      </button>
                    </li>
                  ))}
                </ul>
              )}

              <div className={styles.grantForm}>
                <input
                  className={styles.input}
                  type="text"
                  aria-label="外部可写目录"
                  placeholder="C:\data\shared（工作区外的绝对路径）"
                  value={draftPath}
                  disabled={busy}
                  onChange={(event) => setDraftPath(event.target.value)}
                />
                {/* 目录选择（2026-10-26）：手打绝对路径拼错一个字符只会得到"路径不合法"，
                    而用户根本看不出错在哪。宿主没提供选择器时按钮不渲染，输入框照旧可用。 */}
                {actions?.onPickDirectory === undefined ? null : (
                  <button
                    type="button"
                    className={styles.action}
                    aria-label="选择外部可写目录"
                    disabled={busy || picking}
                    onClick={() => void pickDirectory()}
                  >
                    {picking ? '选择中…' : '选择…'}
                  </button>
                )}
                <button
                  type="button"
                  className={styles.primary}
                  disabled={busy || draftPath.trim() === ''}
                  onClick={submitGrant}
                >
                  添加可写目录
                </button>
              </div>
              <p className={styles.hint}>
                只能用绝对路径；`~`、相对路径与 Windows 盘符相对路径一律拒绝。路径会展开符号链接，并统一大小写与分隔符后保存。
              </p>
            </div>
        </>
      )}
    </div>
  )
}
