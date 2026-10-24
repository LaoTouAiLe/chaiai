// 会话事件流的订阅钩子（T30）：把传输层的事件落到"权威快照"与"临时尾部"两处。
//
// 三条契约决定了这里必须怎么做：
// 1. **重连前先取快照**（§15.6）：事件流不带持久游标，所以断线之后的"我所知道的状态"要从快照重建，
//    而不是接着上一次的位置猜。
// 2. **失败绝不重发用户请求**（T30 停止条件）：这里只重连**订阅**，从不调用提交接口——
//    用户消息的重试只由用户在界面上发起，且复用同一个幂等键（见 `queries.ts`）。
// 3. **快照是权威**：任何一处对不上（缺块、解析失败、重新订阅），一律以快照为准，
//    临时尾部只负责"还没进快照的那一段"（见 `streamTail.ts`）。

import { CancelledError, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import type { components } from './schema.gen'

import { ApiError, getSessionSnapshot } from './client'
import { readSessionEvents, type EventEnvelope } from './events'
import { queryKeys } from './queries'
import { useStreamStore, tailOf } from '../stores/stream'
import {
  applyCompleted,
  applyDelta,
  reconcile,
  startTail,
} from '../features/conversation/streamTail'

export interface SessionStreamState {
  /** 传输层提示（`stream_overrun` / `stream_idle_timeout`）：页面据实说明，不假装一切正常。 */
  notices: string[]
  /** 订阅是否活着（用于呈现"正在接收"）。 */
  connected: boolean
  /**
   * 连接**曾经断过**、现在还在重连（§19.2："断线必须可见"）。
   *
   * 只在**连不上**（取快照/订阅失败）的那条路径上置位：一轮正常收尾后 500ms 重订阅是常规生命周期，
   * 不该让页面闪一下"正在重连"。恢复（成功取到快照）即清零。
   */
  reconnecting: boolean
}

const MAX_BACKOFF_MS = 5_000

/**
 * 事件幂等只保留**最近**这么多个标识（§19.1）：重连时的历史重放是常态，但事件量可能有界地很大，
 * 所以这是"有界集合"而不是"记住全部"。
 */
const MAX_SEEN_EVENT_IDS = 200

/**
 * 连接类提示（§19.1）：连接恢复并成功取到快照后必须清掉，否则横幅会一直挂着，用户以为没恢复。
 *
 * 只按**名字**过滤：4xx 之类"重连也没意义"的提示（会话被拒等）不属于连接类，保留原样。
 */
const CONNECTION_NOTICES = new Set(['stream_overrun', 'snapshot_refresh_failed', 'unparsable_event'])

export function useSessionStream(sessionId: string | null): SessionStreamState {
  const queryClient = useQueryClient()
  const [connected, setConnected] = useState(false)
  const [reconnecting, setReconnecting] = useState(false)
  const [notices, setNotices] = useState<string[]>([])

  useEffect(() => {
    if (sessionId === null) return
    const controller = new AbortController()
    let stopped = false
    let attempt = 0
    let timer: ReturnType<typeof setTimeout> | undefined
    // 事件幂等（§19.1）：重连时服务端会把当前轮的历史事件再投影一次，重复投递不得产生第二次效果。
    // 有界：超出窗口就丢掉最早的一半（`Set` 保序，取尾部即最近）。
    let seenEvents = new Set<string>()
    const firstTimeSeen = (id: string): boolean => {
      if (seenEvents.has(id)) return false
      seenEvents.add(id)
      if (seenEvents.size > MAX_SEEN_EVENT_IDS) {
        seenEvents = new Set([...seenEvents].slice(MAX_SEEN_EVENT_IDS / 2))
      }
      return true
    }

    const refreshSnapshot = async () => {
      // 权威状态：任何"我可能漏了东西"的情况都回到它。
      return queryClient.fetchQuery({
        queryKey: queryKeys.snapshot(sessionId),
        queryFn: () => getSessionSnapshot(sessionId),
        staleTime: 0,
      })
    }

    const reconcileFromServer = () => {
      void refreshSnapshot().then((snapshot) => {
        const store = useStreamStore.getState()
        store.setTail(sessionId, reconcile(tailOf(store.tails, sessionId), snapshot))
      }).catch((error: unknown) => {
        // 取消不是故障：切会话/卸载会让这个查询的最后一个观察者消失，React Query 据此取消在飞的
        // 对账请求（query-core 的既定行为）。把它当失败提示出来，用户会在**看不清来源**的新会话上
        // 看到一条英文错误码，而实际什么都没坏。
        if (error instanceof CancelledError || stopped) return
        setNotices((current) => [...current, 'snapshot_refresh_failed'])
      })
    }

    const handle = (event: EventEnvelope): void => {
      const store = useStreamStore.getState()
      const current = tailOf(store.tails, sessionId)
      switch (event.event_type) {
        case 'session_snapshot':
          // 权威替换：写进缓存，并顺手对账尾部（快照里已有完整消息就丢掉尾部，避免重复显示）。
          queryClient.setQueryData(queryKeys.snapshot(sessionId), event.payload)
          queryClient.setQueriesData<components['schemas']['SessionSummaryView'][]>({ queryKey: ['sessions'] },
            (previous) => previous?.map((item) => item.session_id === sessionId
              ? { ...item, title: event.payload.title } : item))
          store.setTail(sessionId, reconcile(current, event.payload))
          return
        case 'message_started':
          store.setTail(sessionId, startTail(current, event.payload))
          return
        case 'message_delta': {
          const effect = applyDelta(current, event.payload)
          store.setTail(sessionId, effect.tail)
          if (effect.gap) {
            // 缺块：不猜中间内容，用快照补齐（增量仍会继续显示）。
            reconcileFromServer()
          }
          return
        }
        case 'message_completed':
          // 先取权威完整消息，再撤下临时尾部；反过来会让回答在两次请求之间短暂消失。
          // 如果这条完成事件匹配当前尾部，快照到达时 reconcile 会按同一 message_id 对账。
          if (applyCompleted(current, event.payload) === null) reconcileFromServer()
          else void queryClient.invalidateQueries({ queryKey: queryKeys.snapshot(sessionId) })
          return
        case 'approval_required':
        case 'approval_resolved':
          // T30 只展示安全等待态，不作审批决定；但必须读到新快照，否则页面会一直显示“正在生成”。
          reconcileFromServer()
          return
        case 'task_completed':
        case 'task_failed':
          // 本轮结束：尾部该收的收掉，剩下的以快照为准。
          reconcileFromServer()
          void queryClient.invalidateQueries({ queryKey: ['sessions'] })
          // 文件变更账本在这一轮才写完：轮结束时重取一次，"改了哪些文件"立刻是新的。
          // 不在 tool_completed 上做——一轮里几十次工具就是几十次重取，收益不成比例。
          void queryClient.invalidateQueries({ queryKey: ['session-changes'] })
          return
        case 'job_queued':
        case 'job_progress':
        case 'job_waiting':
        case 'job_completed':
        case 'task_cancelled':
          // v0.6 T62（§17.6）：任务状态变了 → 让任务中心**重读索引**，页面不在这里推导状态。
          // 索引是任务状态的唯一出口，"任务到哪一步了"永远以它为准。
          void queryClient.invalidateQueries({ queryKey: ['tasks'] })
          return
        case 'plan_updated':
        case 'step_started':
        case 'step_completed':
        case 'tool_started':
        case 'tool_output':
        case 'tool_completed':
        case 'memory_recalled':
        case 'memory_written':
          // 计划/工具/记忆的**权威**是快照：不在前端重复投影业务逻辑，只把快照重新取一遍。
          reconcileFromServer()
          return
        default:
          // 其余事件（用量、模型调用……）属 T34 的呈现，这里不消费。
          return
      }
    }

    const connect = async (): Promise<void> => {
      try {
        // 每次（重）连都先重建权威状态，再订阅：这是"断线先取快照"的落地位置。
        // （任务列表不在这里重取：它有自己的兜底间隔，见 `queries.ts` 的 `useTasks`。
        //  这里只做一件事，重连路径才不会有第二条"顺手刷新"的逻辑。）
        const snapshot = await refreshSnapshot()
        if (stopped) return
        // 重连先把本地尾部对账到权威快照（§19.1）：重放的历史事件不得把已完成的文本再拼一遍。
        const store = useStreamStore.getState()
        store.setTail(sessionId, reconcile(tailOf(store.tails, sessionId), snapshot))
        // 连接恢复且成功取到快照 ⇒ 连接类提示该消失，不常驻。
        setNotices((current) => current.filter((name) => !CONNECTION_NOTICES.has(name)))
        setConnected(true)
        setReconnecting(false)
        await readSessionEvents(
          sessionId,
          {
            onEvent: (event) => {
              attempt = 0 // 收到事件说明连接是通的，退避从头算
              // 幂等（§19.1）：同一个事件只应用一次。重连后的历史重放是常态，不是新事实。
              if (!firstTimeSeen(event.event_id)) return
              handle(event)
            },
            onNotice: (name) => {
              // 空闲收尾是正常生命周期，下一次订阅会先取快照；不误报为故障。
              if (name !== 'stream_idle_timeout') setNotices((current) => [...current, name])
            },
            onMalformed: () => {
              // 解析不了的事件可能意味着我漏了状态：如实记一笔，并用快照校正。
              setNotices((current) => [...current, 'unparsable_event'])
              reconcileFromServer()
            },
          },
          controller.signal,
        )
        // 空闲超时或服务端正常收尾后也要重订阅：下一轮仍要收到实时增量。
        // 没有持久游标，每次重连都先取快照；绝不重发用户请求。
        if (!stopped) {
          setConnected(false)
          timer = setTimeout(() => void connect(), 500)
        }
      } catch (error) {
        if (stopped || controller.signal.aborted) return
        setConnected(false)
        if (error instanceof ApiError && error.status >= 400 && error.status < 500) {
          // 会话不存在/请求被拒：重连没有意义，把提示留下来就好。
          setNotices((current) => [...current, error.code ?? 'stream_rejected'])
          return
        }
        // 连不上（服务没起来 / 网络断 / 取快照失败）：**如实**标成"正在重连"（§19.2），不静默。
        attempt += 1
        setReconnecting(true)
        timer = setTimeout(() => void connect(), Math.min(500 * attempt, MAX_BACKOFF_MS))
      }
    }

    void connect()

    return () => {
      stopped = true
      controller.abort()
      if (timer !== undefined) clearTimeout(timer)
      setConnected(false)
    }
  }, [queryClient, sessionId])

  return { connected, reconnecting, notices }
}
