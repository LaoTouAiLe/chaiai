// 文件改动还原入口（2026-10-29）。
//
// 两个入口共用这一个控件：对话流卡片上的「还原本轮」与右栏抽屉里的「还原此文件」。
// 共用不是为了省代码，而是为了**只有一套说法**——"对话不会回退""哪些被跳过""为什么跳过"
// 这些文案如果两处各写一遍，迟早出现一个入口说 A、另一个说 B。
//
// 2026-10-29 二次确认（用户拍板）：**基准点由用户显式选**，不再由代码隐式定。
// 依据是 hermes 的做法——它只有"恢复到某个 checkpoint"一个概念，单文件只是把范围缩小，
// 由用户用 `/rollback <N> <file>` 指定 N。我们把它落成"一个动作 + 基准单选"。
//
// 状态机：idle → confirming → （请求中）→ 结果区；结果区可再触发一次「仍然覆盖」。
// 刻意没有乐观更新：还原是真实副作用，必须等服务端结果再改界面。

import { useId, useState } from 'react'

import { ApiError } from '../../api/client'
import { useRestoreChanges } from '../../api/queries'
import type { components } from '../../api/schema.gen'
import styles from './RestoreControl.module.css'

type RestoreResult = components['schemas']['RestoreResultView']

/** 一个可选的还原基准。多个时在确认区里以单选呈现，默认第一个。 */
export interface RestoreAction {
  /** 基准的名字，如「会话开始前」。 */
  label: string
  /** 一句话说明会退成什么样。 */
  note: string
  scope: 'file' | 'turn'
  path?: string
  runId?: string
  /** 整轮里只还原这些文件（单文件"撤销本轮改动"就传 `[path]`）。 */
  paths?: string[]
}

interface RestoreControlProps {
  sessionId: string
  /** 触发按钮的文案，如「还原」「还原此文件」。 */
  label: string
  actions: RestoreAction[]
  /** 紧凑模式：右栏抽屉头那一行用，按钮更小。 */
  compact?: boolean
  /** 确认与结果独占父卡片的一行，避免挤压标题。 */
  className?: string
}

const ACTION_TEXT: Record<string, string> = {
  written: '已写回',
  rebuilt: '已重建',
  removed: '已删除本次新建的文件',
  unchanged: '本来就一致，未改动',
}

const BASIS_TEXT: Record<string, string> = {
  turn: '本轮开始前',
  session: '会话开始前',
}

/** 服务端用这句话表示"文件在 AI 写完之后被改过"；只有这种情况才给「仍然覆盖」。 */
const CONFLICT_HINT = '又被改动过'

export function RestoreControl({
  sessionId, label, actions, compact = false, className,
}: RestoreControlProps) {
  const [stage, setStage] = useState<'idle' | 'confirming'>('idle')
  const [chosen, setChosen] = useState(0)
  const [force, setForce] = useState(false)
  // 单选组名按实例生成：同一个页面上可能同时存在两个还原控件（卡片 + 抽屉），
  // 用文案拼名字的话它们会串成一组（2026-10-29 审查）。
  const groupName = useId()
  const mutation = useRestoreChanges(sessionId)
  const [lastResult, setLastResult] = useState<RestoreResult | null>(null)
  const result = mutation.data ?? lastResult
  // 调用方保证至少一个基准；真给了空数组就整个不渲染，而不是在渲染期崩掉。
  const action = actions.length === 0 ? undefined : actions[Math.min(chosen, actions.length - 1)]
  if (action === undefined) return null

  const run = (nextForce: boolean) => {
    mutation.mutate(
      {
        scope: action.scope,
        path: action.path,
        run_id: action.runId,
        paths: action.paths,
        force: nextForce,
      },
      { onSuccess: (data) => { setLastResult(data); setStage('idle'); setForce(false) } },
    )
  }

  const close = () => {
    mutation.reset()
    setLastResult(null)
    setStage('idle')
    setForce(false)
    setChosen(0)
  }

  const error = mutation.error
  const errorText = error instanceof ApiError
    ? [error.message, error.hint].filter(Boolean).join(' ')
    : error !== null ? '这次还原没有完成，请稍后重试。' : null

  if (result !== null) {
    const conflict = result.skipped.some((item) => item.reason.includes(CONFLICT_HINT))
    return (
      <div className={`${styles.result} ${className ?? ''}`} role="status">
        <p className={styles.summary}>
          还原结果：{result.restored.length} 个处理成功
          {result.skipped.length > 0 ? `，${result.skipped.length} 个跳过` : ''}
          {result.failed.length > 0 ? `，${result.failed.length} 个失败` : ''}
        </p>
        {result.restored.length > 0 ? (
          <ul className={styles.list}>
            {result.restored.map((item) => (
              <li key={item.path}>
                <code>{item.path}</code>
                <span className={styles.note}>
                  {ACTION_TEXT[item.action] ?? item.action} · 基准：{BASIS_TEXT[item.basis] ?? item.basis}
                </span>
              </li>
            ))}
          </ul>
        ) : null}
        {result.skipped.length > 0 ? (
          <>
            <p className={styles.subhead}>这些文件没有动，原因是：</p>
            <ul className={styles.list}>
              {result.skipped.map((item) => (
                <li key={item.path}>
                  <code>{item.path}</code>
                  <span className={styles.note}>{item.reason}</span>
                </li>
              ))}
            </ul>
          </>
        ) : null}
        {result.failed.length > 0 ? (
          <>
            <p className={styles.subhead}>这些文件尝试过但失败了：</p>
            <ul className={styles.list}>
              {result.failed.map((item) => (
                <li key={item.path}>
                  <code>{item.path}</code>
                  <span className={styles.note}>{item.error}</span>
                </li>
              ))}
            </ul>
          </>
        ) : null}
        <div className={styles.actions}>
          {conflict ? (
            <button
              type="button"
              className={styles.danger}
              disabled={mutation.isPending}
              onClick={() => { run(true) }}
              title="连同你自己在那之后做的修改一起覆盖"
            >
              {mutation.isPending ? '正在还原…' : '仍然覆盖被跳过的文件'}
            </button>
          ) : null}
          <button type="button" disabled={mutation.isPending} onClick={close}>关闭</button>
        </div>
        {errorText === null ? null : <p className={styles.error} role="alert">{errorText}</p>}
      </div>
    )
  }

  if (stage === 'confirming') {
    return (
      <div className={`${styles.confirm} ${className ?? ''}`} role="group" aria-label="确认还原">
        {actions.length > 1 ? (
          <ul className={styles.options}>
            {actions.map((item, index) => (
              <li key={item.label}>
                <label className={styles.option}>
                  <input
                    type="radio"
                    name={groupName}
                    checked={index === chosen}
                    onChange={() => { setChosen(index) }}
                  />
                  <span>
                    <strong>{item.label}</strong>
                    <span className={styles.note}>{item.note}</span>
                  </span>
                </label>
              </li>
            ))}
          </ul>
        ) : (
          <p className={styles.summary}>{action.note}。</p>
        )}
        <p className={styles.summary}>
          <strong>对话记录不会回退</strong>，这一轮的消息仍然留在对话里。
        </p>
        <label className={styles.check}>
          <input
            type="checkbox"
            checked={force}
            onChange={(event) => { setForce(event.target.checked) }}
          />
          连同我自己在那之后改过的地方一起覆盖
        </label>
        <div className={styles.actions}>
          <button
            type="button"
            className={styles.danger}
            disabled={mutation.isPending}
            onClick={() => { run(force) }}
          >
            {mutation.isPending ? '正在还原…' : '确认还原'}
          </button>
          <button type="button" disabled={mutation.isPending} onClick={close}>取消</button>
        </div>
        {errorText === null ? null : <p className={styles.error} role="alert">{errorText}</p>}
      </div>
    )
  }

  return (
    <button
      type="button"
      className={compact ? `${styles.trigger} ${styles.compact}` : styles.trigger}
      onClick={() => { setStage('confirming') }}
      title={`${actions.length > 1 ? '可选基准：' : ''}${actions.map((item) => item.label).join(' / ')}；不会回退对话`}
    >
      {label}
    </button>
  )
}
