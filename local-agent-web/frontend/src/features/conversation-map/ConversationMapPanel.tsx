import { useMemo, useRef, useState, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import type { components } from '../../api/schema.gen'
import { Notice } from '../../components/Notice'
import { ApiError, arrangeBranch, getConversationMap, getTurnSnapshot, selectBranch } from '../../api/client'
import { queryKeys } from '../../api/queries'
import { useStreamStore } from '../../stores/stream'
import type { ConversationScenario } from '../../fixtures/types'
import { Conversation, type ErrorActions } from '../conversation/Conversation'
import { mapCall, mapError, mapMessages, mapTurnStats } from '../conversation/adapters'
import { TraceView } from '../trace/TraceView'
import { traceFromMessages } from '../trace/adapters'
import { ancestry, mapConversationMap, type MapNode } from './adapters'
import { TreeCanvas } from './TreeCanvas'
import styles from './ConversationMapPanel.module.css'

type Snapshot = components['schemas']['SessionSnapshot']
interface Props {
  sessionId: string
  scenario: ConversationScenario
  busy: boolean
  snapshotId: string | null
  onContinue: (node: MapNode, context: Snapshot) => void
  onLocate: (turnId: string) => void
  onBranchChanged: () => void
  followupTurnId?: string | null
  followupContent?: ReactNode
  onCloseFollowup?: () => void
  errorActions?: ErrorActions
}

export function ConversationMapPanel({ sessionId, scenario, busy, snapshotId, onContinue, onLocate, onBranchChanged,
  followupTurnId = null, followupContent, onCloseFollowup, errorActions }: Props) {
  const client = useQueryClient()
  const [searchDraft, setSearchDraft] = useState('')
  const [query, setQuery] = useState('')
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [detailOpen, setDetailOpen] = useState(false)
  const [focusRequest, setFocusRequest] = useState<{ id: string; version: number } | null>(null)
  const panel = useRef<HTMLElement>(null)
  const [collapsed, setCollapsed] = useState<ReadonlySet<string>>(new Set())
  const [detailTab, setDetailTab] = useState<'answer' | 'trace'>('answer')
  const [editingBranch, setEditingBranch] = useState<string | null>(null)
  const [branchTitle, setBranchTitle] = useState('')
  const [expandedTools, setExpandedTools] = useState<ReadonlySet<string>>(new Set())
  const mapQuery = useQuery({
    queryKey: ['conversation-map', sessionId, query, snapshotId],
    queryFn: () => getConversationMap(sessionId, query),
    // 流式快照推进时保留地图与视野；更换搜索关键词时等待对应的匹配结果。
    placeholderData: (previous, previousQuery) => previousQuery?.queryKey[2] === query ? previous : undefined,
    gcTime: 0,
  })
  const data = useMemo(() => mapQuery.data === undefined ? null : mapConversationMap(mapQuery.data), [mapQuery.data])
  const currentId = selectedId ?? data?.branches.find((b) => b.active)?.tipId ?? null
  const node = data?.nodes.find((n) => n.id === currentId) ?? null
  const detail = useQuery({
    queryKey: ['turn-detail', sessionId, currentId, snapshotId],
    queryFn: () => getTurnSnapshot(sessionId, currentId ?? ''), enabled: detailOpen && currentId !== null,
    gcTime: 0,
  })
  const changeBranch = useMutation({
    mutationFn: async (branchId: string) => {
      await client.cancelQueries({ queryKey: queryKeys.snapshot(sessionId) })
      return selectBranch(sessionId, branchId)
    },
    onSuccess: (snapshot) => {
      useStreamStore.getState().setTail(sessionId, null)
      client.setQueryData(queryKeys.snapshot(sessionId), snapshot)
      void client.invalidateQueries({ queryKey: ['conversation-map', sessionId] })
      setSelectedId(null)
      setDetailOpen(false)
      setFocusRequest(null)
      onBranchChanged()
    },
  })
  const arrange = useMutation({
    mutationFn: (choice: { id: string; title: string; position: number }) => arrangeBranch(sessionId, choice.id, { title: choice.title, position: choice.position }),
    onSuccess: () => {
      setEditingBranch(null)
      void client.invalidateQueries({ queryKey: ['conversation-map', sessionId] })
      void client.invalidateQueries({ queryKey: queryKeys.snapshot(sessionId) })
    },
  })
  const continueTurn = useMutation({
    mutationFn: (chosen: MapNode) => getTurnSnapshot(sessionId, chosen.id, true),
    onSuccess: (context, chosen) => onContinue(chosen, context),
  })
  const locate = useMutation({
    mutationFn: async (chosen: MapNode) => {
      if (data === null) return
      const branch = data.branches.find((b) => b.active && ancestry(data.nodes, b.tipId).has(chosen.id))
        ?? data.branches.find((b) => b.id === chosen.originBranchId)
      if (branch === undefined) return
      if (!branch.active) {
        await client.cancelQueries({ queryKey: queryKeys.snapshot(sessionId) })
        const snapshot = await selectBranch(sessionId, branch.id)
        useStreamStore.getState().setTail(sessionId, null)
        client.setQueryData(queryKeys.snapshot(sessionId), snapshot)
        onBranchChanged()
      }
      onLocate(chosen.id)
    },
  })
  const error = mapQuery.error ?? changeBranch.error ?? arrange.error ?? continueTurn.error ?? locate.error
  const messages = detail.data === undefined ? [] : mapMessages(detail.data).map((message) =>
    message.kind === 'tool' ? { ...message, tool: { ...message.tool, expanded: expandedTools.has(message.id) } } : message)
  const presentErrors = new Set(messages.filter((m) => m.kind === 'error').map((m) => m.notice.diagnosticId))
  if (detail.data !== undefined) for (const error of detail.data.errors ?? []) {
    if (!presentErrors.has(error.diagnostic_id)) messages.push({ kind: 'error', id: error.diagnostic_id, notice: mapError(error) })
  }
  const detailScenario: ConversationScenario = { ...scenario, messages, activity: null,
    plan: null, empty: null, loading: null, stats: null,
    turnStats: detail.data === undefined ? [] : mapTurnStats(detail.data, messages, false),
    takeover: { kind: 'none' } }
  const path = data === null ? new Set<string>() : ancestry(data.nodes, data.branches.find((b) => b.active)?.tipId ?? null)
  const calls = (detail.data?.model_calls ?? []).map(mapCall)
  const locked = busy || changeBranch.isPending || continueTurn.isPending || locate.isPending
  function closeDetail() {
    onCloseFollowup?.()
    setDetailOpen(false)
    // 非模态抽屉关闭后把键盘焦点还给原卡片，保留选中状态。
    Array.from(panel.current?.querySelectorAll<HTMLButtonElement>('button[data-turn-id]') ?? [])
      .find((button) => button.dataset.turnId === currentId)?.focus({ preventScroll: true })
  }

  return <section ref={panel} className={styles.panel} aria-label="会话地图"
    onKeyDown={(event) => { if ((detailOpen || followupTurnId !== null) && event.key === 'Escape' && !event.defaultPrevented) { event.stopPropagation(); closeDetail() } }}>
    <header className={styles.toolbar}>
      <div className={styles.heading}><strong>会话地图</strong><span>{data?.nodes.length ?? 0} 轮问答</span></div>
      <form className={styles.search} onSubmit={(event) => { event.preventDefault(); setQuery(searchDraft.trim()) }}>
        <input aria-label="搜索会话问答" value={searchDraft} placeholder="搜索问题或回答"
          onChange={(event) => setSearchDraft(event.target.value)} />
        <button type="submit">搜索</button>
        {query === '' ? null : <button type="button" onClick={() => { setQuery(''); setSearchDraft('') }}>清除</button>}
      </form>
      {data === null ? null : <div className={styles.branches}>
        <label>当前分支<select aria-label="当前分支" value={data.activeBranchId} disabled={locked}
          onChange={(event) => changeBranch.mutate(event.target.value)}>
          {data.branches.map((branch) => <option key={branch.id} value={branch.id}>{branch.title}</option>)}
        </select></label>
        <details><summary>整理分支</summary>
          {data.branches.map((branch, index) => <div key={branch.id} className={styles.branch}>
            {editingBranch === branch.id ? <form onSubmit={(event) => {
              event.preventDefault(); arrange.mutate({ id: branch.id, title: branchTitle.trim(), position: branch.position })
            }}><input aria-label="分支名称" value={branchTitle} maxLength={80} onChange={(event) => setBranchTitle(event.target.value)} />
              <button disabled={busy || arrange.isPending || branchTitle.trim() === ''}>保存</button>
              <button type="button" onClick={() => setEditingBranch(null)}>取消</button></form>
              : <><span>{branch.title}</span><button type="button" disabled={busy || arrange.isPending}
                onClick={() => { setEditingBranch(branch.id); setBranchTitle(branch.title) }}>改名</button></>}
            <button type="button" aria-label={`${branch.title}上移`} disabled={busy || arrange.isPending || index === 0}
              onClick={() => arrange.mutate({ id: branch.id, title: branch.title, position: index - 1 })}>↑</button>
            <button type="button" aria-label={`${branch.title}下移`} disabled={busy || arrange.isPending || index === data.branches.length - 1}
              onClick={() => arrange.mutate({ id: branch.id, title: branch.title, position: index + 1 })}>↓</button>
          </div>)}
        </details>
        {/* 侧聊已经画进树里（2026-10-23 第三轮）：它是"基于某一轮上下文的分支延续"，
            归到 `originTurnId` 那一轮下面。工具栏里不再重复列一遍。 */}
      </div>}
      {mapQuery.isPending ? <p role="status">正在读取会话地图…</p> : null}
      {error === null ? null : <Notice role="alert" label="地图错误提示" scope={error}>{error instanceof ApiError ? error.message : '操作未完成，请刷新地图重试。'}
        <button type="button" onClick={() => { changeBranch.reset(); arrange.reset(); continueTurn.reset(); locate.reset(); void mapQuery.refetch() }}>刷新地图</button></Notice>}
      {query === '' ? null : <p className={styles.note}>{data?.matchedIds.size ?? 0} 条匹配，保留祖先以显示路径。</p>}
    </header>
    <div className={styles.workspace}>
      {data === null ? <div className={styles.placeholder}>正在读取地图…</div> : <TreeCanvas data={data} collapsed={collapsed}
        searching={query !== ''} path={path} selectedId={selectedId} focusRequest={focusRequest}
        onSelect={(chosen) => {
          onCloseFollowup?.()
          setSelectedId(chosen.id); setDetailOpen(true); setDetailTab('answer')
          setFocusRequest((previous) => ({ id: chosen.id, version: (previous?.version ?? 0) + 1 }))
        }}
        onLocateCurrent={(id) => {
          setQuery(''); setSearchDraft('')
          const ancestors = ancestry(data.nodes, id)
          setCollapsed((previous) => new Set([...previous].filter((value) => !ancestors.has(value))))
          setFocusRequest((previous) => ({ id, version: (previous?.version ?? 0) + 1 }))
        }}
        onToggle={(id) => setCollapsed((previous) => {
          const next = new Set(previous); if (next.has(id)) next.delete(id); else next.add(id); return next
        })} />}
      {followupTurnId !== null ? <aside className={`${styles.detail} ${styles.chatDetail}`} aria-label="地图追问">
        <div className={styles.drawerHeading}><span>地图追问</span><button type="button" aria-label="关闭地图追问" onClick={closeDetail}>×</button></div>
        {followupContent}
      </aside> : detailOpen && node !== null ? <aside className={styles.detail} aria-label="问答详情">
        <div className={styles.drawerHeading}><span>问答详情</span><button type="button" aria-label="关闭问答详情" onClick={closeDetail}>×</button></div>
        <div className={styles.detailHeader}>
          <div><strong>第{node.ordinal}轮问答</strong><p>{node.status} · {path.has(node.id) ? '当前路径' : '其他路径'}</p></div>
          <div className={styles.actions}>
            <button type="button" disabled={locked} onClick={() => locate.mutate(node)}>回到聊天位置</button>
            <button type="button" disabled={locked || !node.canContinue} onClick={() => continueTurn.mutate(node)}>基于此轮继续提问</button>
          </div>
        </div>
        <p className={styles.note}>追问继承会话开头到本轮的对话；工作区文件与共享记忆仍使用当前状态。</p>
        {!node.canContinue ? <p className={styles.note}>该轮尚未结束或历史检查点缺失，暂不能作为追问起点。</p> : null}
        <div className={styles.detailTabs}>
          <button type="button" aria-pressed={detailTab === 'answer'} onClick={() => setDetailTab('answer')}>问题与回答</button>
          <button type="button" aria-pressed={detailTab === 'trace'} onClick={() => setDetailTab('trace')}>执行链路</button>
        </div>
        {detail.isPending ? <p role="status">正在读取本轮详情…</p> : detail.isError ?
          <p role="alert">本轮历史详情暂不可用。<button type="button" onClick={() => { void detail.refetch() }}>重试</button></p> :
          detailTab === 'answer' ? <Conversation key={node.id} scenario={detailScenario} errorActions={errorActions} onToggleTool={(id) => {
            setExpandedTools((previous) => { const next = new Set(previous); if (next.has(id)) next.delete(id); else next.add(id); return next })
          }} /> : <div className={styles.execution}>
            <p className={styles.note}>本轮记录：模型调用 {detail.data?.model_calls?.length ?? 0} 次 · 工具记录 {messages.filter((m) => m.kind === 'tool').length} 条 · {node.status}。问题与回答页按消息顺序展示调用过程。</p>
            <section aria-label="本轮模型调用">
              <strong>模型调用（按记录顺序）</strong>
              {calls.length === 0 ? <p className={styles.note}>本轮没有模型调用记录。</p> :
                <ol className={styles.calls}>{calls.map((call) => <li key={call.id}>
                  <span>{call.purpose} · {call.modelLabel}</span>
                  <span>输入 {call.inputTokens ?? '未知'} · 输出 {call.outputTokens ?? '未知'} · {call.elapsedLabel || '耗时未知'}</span>
                </li>)}</ol>}
            </section>
            {messages.some((m) => m.kind === 'tool') ? <TraceView trace={traceFromMessages(messages, calls)} /> :
              <p className={styles.note}>本轮没有工具操作记录。</p>}
          </div>}
      </aside> : null}
    </div>
  </section>
}
