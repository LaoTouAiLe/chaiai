import { useEffect, useMemo, useRef, useState } from 'react'

import { createSideChat } from '../../api/client'
import { useSnapshot, useSubmitMessage } from '../../api/queries'
import { useSessionStream } from '../../api/useSessionStream'
import { Button } from '../../components/ui/Button'
import type { ConversationScenario } from '../../fixtures/types'
import { mapMessages, mapSession } from '../conversation/adapters'
import { Conversation } from '../conversation/Conversation'
import styles from './SideChatPane.module.css'

/**
 * 侧边对话面板（§21.7）。
 *
 * **消息渲染直接复用 `Conversation`**（2026-10-23 第二次返工）：之前自己写了一版
 * 「气泡 + Markdown」，结果思考块、检索与记忆卡片、工具调用行、每轮统计全都没有——
 * 和主对话放在一起就是两套东西。现在把快照映射成同一个 `ConversationScenario`，
 * 主对话长什么样，侧聊就长什么样。
 *
 * 输入框仍单独实现：主对话底部那排（闲聊/Agent 切换、模型、附件）在侧聊里用不上。
 */
export function SideChatPane({
  parentSessionId,
  sessionId,
  onSession,
}: {
  /** 主会话 id：开侧聊要挂在它下面。 */
  parentSessionId: string | null
  /** 这个页签对应的侧聊会话 id；`null` = 还没开。 */
  sessionId: string | null
  onSession: (id: string) => void
}) {
  const snapshot = useSnapshot(sessionId)
  // 与主对话同一个流：回答结束、审批、状态变化都会推过来。
  useSessionStream(sessionId)
  const submit = useSubmitMessage(sessionId)
  const [draft, setDraft] = useState('')
  const [starting, setStarting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const bottom = useRef<HTMLDivElement | null>(null)

  const data = snapshot.data
  const running = data?.current_answer_streaming === true || submit.isPending

  const scenario = useMemo<ConversationScenario>(() => ({
    id: `side-${sessionId ?? 'none'}`,
    purpose: '',
    sessionId,
    session: data === undefined
      ? { title: '侧边对话', workspace: null, status: 'idle', modelLabel: '' }
      : mapSession(data, ''),
    messages: data === undefined ? [] : mapMessages(data),
    plan: null,
    stats: null,
    turnStats: [],
    context: { usedTokens: null, windowTokens: null, scope: 'unknown' },
    takeover: { kind: 'none' },
    settings: null,
    modelMenu: null,
  }), [data, sessionId])

  const start = () => {
    if (parentSessionId === null) return
    setStarting(true)
    setError(null)
    void createSideChat(parentSessionId)
      .then((created) => onSession(created.session_id))
      .catch(() => setError('没能开始侧边对话，请重试。'))
      .finally(() => setStarting(false))
  }

  // 挂载即建：宿主传进来的还是空 id 时直接开一个侧聊，不要用户再点一次。
  // `autoStarted` 防 StrictMode 下重复创建（那会多出几条空会话）。
  const autoStarted = useRef(false)
  useEffect(() => {
    if (sessionId !== null || parentSessionId === null || autoStarted.current) return
    autoStarted.current = true
    start()
    // eslint-disable-next-line react-hooks/exhaustive-deps -- 只在挂载/父会话变化时触发
  }, [sessionId, parentSessionId])

  // 新消息进来就滚到底（`Conversation` 自己也会跟随，这里兜一层输入框的滚动）。
  useEffect(() => {
    bottom.current?.scrollIntoView({ block: 'end' })
  }, [scenario.messages.length, running])

  const send = () => {
    const text = draft.trim()
    if (sessionId === null || text === '') return
    submit.mutate({ text, context: {} })
    setDraft('')
  }

  if (sessionId === null) {
    return (
      <div className={styles.empty}>
        {error === null ? (
          <p className={styles.emptyHint}>正在准备侧边对话…</p>
        ) : (
          <>
            <p className={styles.err}>{error}</p>
            <Button size="sm" onClick={start} disabled={starting}>重试</Button>
          </>
        )}
      </div>
    )
  }

  return (
    <div className={styles.wrap}>
      {/* 与主对话同一个组件：思考块、检索卡片、工具行、每轮统计都在。 */}
      <Conversation scenario={scenario} autoFollow />
      <div ref={bottom} />
      <div className={styles.composer}>
        <textarea
          className={styles.input}
          value={draft}
          rows={2}
          placeholder="在这里追问，不影响主对话…"
          aria-label="侧边对话输入框"
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter' && !event.shiftKey) {
              event.preventDefault()
              send()
            }
          }}
        />
        <Button size="sm" onClick={send} disabled={draft.trim() === '' || running}>
          发送
        </Button>
      </div>
    </div>
  )
}
