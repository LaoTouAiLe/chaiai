import { useEffect, useState } from 'react'
import type { QuestionItemView, QuestionView } from '../../fixtures/types'
import styles from './QuestionCard.module.css'

/**
 * 真实接线（2026-10-28）。**省略即原型态**：按钮渲染出来但没有处理器，这是静态基线要的样子。
 */
export interface QuestionActions {
  /** 一次提交整组；空着的题由调用方按"跳过"处理。 */
  onSubmit: (answers: readonly { index: number; answer: string }[]) => void
  /** 整组跳过：一题都不答，直接让这一轮继续。 */
  onSkip: () => void
  /** 提交在飞：所有按钮一起锁住，直到服务端给出结局。 */
  busy: boolean
}

interface QuestionCardProps {
  question: QuestionView
  actions?: QuestionActions
}

const STATE_LABEL: Record<QuestionView['state'], string> = {
  pending: '需要你补充',
  answered: '已回答',
  skipped: '已跳过',
  expired: '提问已失效',
}

/**
 * 题组草稿的本地存储（2026-10-28）。
 *
 * 题组是"考试式"的：翻页只在本地，答完才提交一次。所以草稿**不落服务端**——服务端此刻只
 * 知道"有一组题在等你"，不知道你答到第几题。存 `sessionStorage` 是为了刷新/误关标签页时
 * 不白答一遍；它随标签页关闭而消失，不写盘、不同步，也不参与任何业务判断。
 */
function draftKey(questionId: string): string {
  return `local-agent-web.question-drafts:${questionId}`
}

function readDrafts(questionId: string): Record<number, string> {
  try {
    const raw = globalThis.sessionStorage?.getItem(draftKey(questionId))
    if (raw === null || raw === undefined) return {}
    const parsed: unknown = JSON.parse(raw)
    if (typeof parsed !== 'object' || parsed === null) return {}
    const drafts: Record<number, string> = {}
    for (const [key, value] of Object.entries(parsed as Record<string, unknown>)) {
      const index = Number(key)
      if (Number.isInteger(index) && index >= 0 && typeof value === 'string') drafts[index] = value
    }
    return drafts
  } catch {
    return {}
  }
}

function writeDrafts(questionId: string, drafts: Record<number, string>): void {
  try {
    const kept = Object.fromEntries(Object.entries(drafts).filter(([, text]) => text !== ''))
    globalThis.sessionStorage?.setItem(draftKey(questionId), JSON.stringify(kept))
  } catch {
    // 存储不可用：这次仍然能答，只是刷新后要重填。
  }
}

function clearDrafts(questionId: string): void {
  try {
    globalThis.sessionStorage?.removeItem(draftKey(questionId))
  } catch {
    // 同上：清不掉不影响提交结果。
  }
}

/**
 * 接管输入区的提问卡（ARCHITECTURE §6.14）。
 *
 * 四条规则：
 * - **一次提交整组**：翻页只在本地，点"提交"才把整组答案交给后端（不是答一题挂一次）；
 * - 只有 `pending` 给输入；有结局后只报结果，不再提供第二次机会；
 * - 空着的题按"跳过"提交，提交后按钮立刻锁定、结局以后端为准（与审批卡同一口径，§15.8）；
 * - 文案不出现工具名、节点名或"interrupt"这类内部机制词。
 */
export function QuestionCard({ question, actions }: QuestionCardProps) {
  const items = question.items
  const [drafts, setDrafts] = useState<Record<number, string>>(() => readDrafts(question.questionId))
  const [cursor, setCursor] = useState(0)
  const actionable = question.state === 'pending'
  const busy = actions?.busy === true
  const total = items.length
  const current: QuestionItemView | undefined = items[Math.min(cursor, Math.max(total - 1, 0))]
  const answeredCount = items.filter((item) => (drafts[item.index] ?? '').trim() !== '').length

  // 题目或会话变了就重置草稿（同一个 questionId 才恢复）。
  useEffect(() => {
    setDrafts(readDrafts(question.questionId))
    setCursor(0)
  }, [question.questionId])
  useEffect(() => {
    if (question.state !== 'pending') clearDrafts(question.questionId)
  }, [question.questionId, question.state])

  const update = (index: number, text: string) => {
    setDrafts((previous) => {
      const next = { ...previous, [index]: text }
      writeDrafts(question.questionId, next)
      return next
    })
  }

  const submit = () => {
    if (actions === undefined) return
    const answers = items.map((item) => ({ index: item.index, answer: (drafts[item.index] ?? '').trim() }))
    actions.onSubmit(answers)
  }

  if (!actionable) {
    // 答完就**不再占位**（2026-10-28 用户实测反馈：答案之后那条一直在输入区上方，很碍事）。
    // 答案已经写进这一轮的回答里，卡片再留着只是长期挤占输入区。
    // 只有 `expired` 例外——那一轮已经结束，这张卡片是唯一能解释"为什么它没再问"的地方。
    if (question.state !== 'expired') return null
    return (
      <section className={`${styles.card} ${styles.settled}`} aria-label="提问">
        <div className={styles.head}>
          <span className={styles.state}>{STATE_LABEL[question.state]}</span>
          <span className={styles.question}>{question.question}</span>
        </div>
        <p className={styles.outcome}>{question.note ?? '该提问已失效：本轮已结束。'}</p>
      </section>
    )
  }

  if (current === undefined) {
    return null
  }

  const isLast = cursor >= total - 1
  const last = tempCurrent(drafts, current)

  return (
    <section className={styles.card} aria-label="提问">
      <div className={styles.head}>
        <span className={styles.state}>{STATE_LABEL[question.state]}</span>
        {total > 1 ? (
          <span className={styles.counter} aria-label={`第 ${cursor + 1} 题，共 ${total} 题`}>
            {cursor + 1}/{total}
          </span>
        ) : null}
      </div>

      <p className={styles.question}>{current.question}</p>

      {current.options.length === 0 ? null : (
        <div className={styles.options} role="group" aria-label="可选答案">
          {current.options.map((option) => (
            <button
              key={option}
              type="button"
              className={styles.option}
              aria-pressed={last === option}
              onClick={() => update(current.index, option)}
              disabled={busy}
            >
              {option}
            </button>
          ))}
        </div>
      )}

      {current.allowFreeText || current.options.length === 0 ? (
        <label className={styles.field}>
          <span className="visually-hidden">第 {cursor + 1} 题的回答</span>
          <textarea
            className={styles.input}
            value={last}
            rows={2}
            placeholder={current.options.length === 0 ? '输入你的答复…' : '也可以自己写…'}
            onChange={(event) => update(current.index, event.target.value)}
            disabled={busy}
          />
        </label>
      ) : null}

      <div className={styles.actions}>
        <span className={styles.hint}>
          {busy
            ? '正在提交，结果以后端为准…'
            : total > 1
              ? `答完一起提交；空着的题按"跳过"提交。已填 ${answeredCount}/${total}。`
              : '答复只用于这一次提问；不想回答可以跳过。'}
        </span>
        {total > 1 ? (
          <button
            type="button"
            className={styles.step}
            onClick={() => setCursor((value) => Math.max(0, value - 1))}
            disabled={busy || cursor === 0}
          >
            上一题
          </button>
        ) : null}
        {total > 1 ? (
          <button
            type="button"
            className={styles.skip}
            onClick={actions === undefined ? undefined : () => actions.onSkip()}
            disabled={busy}
          >
            全部跳过
          </button>
        ) : (
          <button
            type="button"
            className={styles.skip}
            onClick={actions === undefined ? undefined : () => actions.onSkip()}
            disabled={busy}
          >
            跳过
          </button>
        )}
        {isLast ? (
          <button
            type="button"
            className={styles.submit}
            onClick={submit}
            disabled={busy || actions === undefined}
          >
            提交
          </button>
        ) : (
          <button
            type="button"
            className={styles.submit}
            onClick={() => setCursor((value) => Math.min(total - 1, value + 1))}
            disabled={busy}
          >
            下一题
          </button>
        )}
      </div>

      {total > 1 ? (
        <ol className={styles.dots} aria-hidden="true">
          {items.map((item) => (
            <li
              key={item.index}
              className={item.index === cursor ? styles.dotActive : styles.dot}
              data-filled={(drafts[item.index] ?? '').trim() !== '' ? 'true' : undefined}
            />
          ))}
        </ol>
      ) : null}
    </section>
  )
}

function tempCurrent(drafts: Record<number, string>, item: QuestionItemView): string {
  return drafts[item.index] ?? ''
}
