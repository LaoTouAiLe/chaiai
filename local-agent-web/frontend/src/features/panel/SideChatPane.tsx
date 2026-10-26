import { useEffect, useMemo, useRef, useState } from 'react'

import type { Dispatch, SetStateAction } from 'react'
import { ApiError } from '../../api/client'
import { useSnapshot, useSubmitMessage } from '../../api/queries'
import { useSessionStream } from '../../api/useSessionStream'
import { Button } from '../../components/ui/Button'
import type { ConversationScenario } from '../../fixtures/types'
import { useStreamTail } from '../../stores/stream'
import { useGeneralPreferences } from '../settings/preferencesContext'
import { mapMessages, mapSession, mapTurnStats } from '../conversation/adapters'
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
  onStart,
  onSession,
  draft,
  onDraftChange,
}: {
  /** 主会话 id：开侧聊要挂在它下面。 */
  parentSessionId: string | null
  /** 这个页签对应的侧聊会话 id；`null` = 还没开。 */
  sessionId: string | null
  onStart: () => Promise<string>
  onSession: (id: string) => void
  draft: string
  onDraftChange: Dispatch<SetStateAction<string>>
}) {
  const snapshot = useSnapshot(sessionId)
  // 与主对话同一个流：回答结束、审批、状态变化都会推过来。
  const stream = useSessionStream(sessionId)
  const tail = useStreamTail(sessionId)
  const preferences = useGeneralPreferences()
  // 后端在本机：系统断网也要实际尝试请求，失败就提示，不能排队到联网后悄悄发送。
  const submit = useSubmitMessage(sessionId, {}, 'always')
  const [starting, setStarting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [sendError, setSendError] = useState<string | null>(null)

  const data = snapshot.data
  const running = data?.session_state === 'running' || data?.current_answer_streaming === true || submit.isPending
  const canSend = sessionId !== null && data !== undefined && !snapshot.isError && !running

  const scenario = useMemo<ConversationScenario>(() => {
    const messages = data === undefined ? [] : mapMessages(data, data.current_answer_streaming === true ? tail : null)
    return {
      id: `side-${sessionId ?? 'none'}`,
      purpose: '',
      sessionId,
      session: data === undefined
        ? { title: '侧边对话', workspace: null, status: 'idle', modelLabel: '' }
        : mapSession(data, ''),
      messages,
      activity: running ? {
        id: `side-running-${sessionId ?? ''}`, active: true, startedAt: null,
        label: data?.current_answer_streaming === true ? '正在接收回答' : '正在处理请求',
        detail: data?.current_answer_streaming === false ? '回答会在完成时一次性显示。' : '可以继续编辑下一条消息的草稿。',
      } : null,
      plan: null,
      stats: null,
      turnStats: data === undefined ? [] : mapTurnStats(data, messages, running),
      context: { usedTokens: null, windowTokens: null, scope: 'unknown' },
      takeover: { kind: 'none' },
      settings: null,
      modelMenu: null,
    }
  }, [data, sessionId, tail, running])

  const start = () => {
    if (parentSessionId === null) return
    setStarting(true)
    setError(null)
    void onStart()
      .then(onSession)
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

  const send = () => {
    const text = draft.trim()
    if (!canSend || text === '') return
    const submittedDraft = draft
    setSendError(null)
    onDraftChange('')
    void submit.mutateAsync({ text, context: {} }).catch((cause: unknown) => {
      // 用户已经写了下一条草稿时保留新内容，不让迟到的失败覆盖它。
      onDraftChange((current) => current === '' ? submittedDraft : current)
      setSendError(cause instanceof ApiError ? cause.message : '网络连接失败，请检查网络后重试。')
    })
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
      {snapshot.isPending ? <div className={styles.empty} role="status"><p className={styles.emptyHint}>正在读取侧边对话…</p></div>
        : snapshot.isError ? <div className={styles.empty} role="alert">
          <p className={styles.err}>侧边对话读取失败，请重试。</p>
          <Button size="sm" onClick={() => void snapshot.refetch()}>重试</Button>
        </div> : scenario.messages.length === 0 && !running ? <div className={styles.empty}>
          <p className={styles.emptyTitle}>从主会话继续追问</p>
          <p className={styles.emptyHint}>在下方输入问题，回答会留在这个标签里。</p>
        </div> : <Conversation scenario={scenario} autoFollow />}
      {stream.reconnecting ? <p className={styles.connection} role="status">连接中断，正在重新连接…</p> : null}
      {sendError === null ? null : <p className={styles.sendError} role="alert">发送失败：{sendError} 未发出的内容已保留。</p>}
      <div className={styles.composer}>
        <textarea
          className={styles.input}
          value={draft}
          rows={2}
          placeholder="在这里追问，不影响主对话…"
          aria-label="侧边对话输入框"
          onChange={(event) => onDraftChange(event.target.value)}
          onKeyDown={(event) => {
            const sendKey = preferences.send_shortcut === 'ctrl_enter' ? event.ctrlKey || event.metaKey : !event.ctrlKey && !event.metaKey
            if (event.key === 'Enter' && sendKey && !event.shiftKey && !event.nativeEvent.isComposing && event.keyCode !== 229) {
              event.preventDefault()
              send()
            }
          }}
        />
        <Button size="sm" onClick={send} disabled={draft.trim() === '' || !canSend}>
          {running ? '处理中' : '发送'}
        </Button>
      </div>
    </div>
  )
}
