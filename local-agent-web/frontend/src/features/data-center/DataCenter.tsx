import { useRef, useState } from 'react'
import type { components } from '../../api/schema.gen'
import type { MemoryFilters } from '../../api/client'
import { useMemoryPage, useUsagePage } from '../../api/queries'
import { countLabel, inputTokens, kindLabels, percentLabel, purposeLabel, scopeLabels, statusLabels, timeLabel, utcRange } from './adapters'
import type { MemoryEntry, UsageEntry } from './adapters'
import styles from './DataCenter.module.css'

type Tab = 'overview' | 'medium' | 'fact' | 'conclusion' | 'usage'
const tabs: { id: Tab; label: string }[] = [
  { id: 'overview', label: '概览' }, { id: 'medium', label: '中期记忆' },
  { id: 'fact', label: '长期事实' }, { id: 'conclusion', label: '任务结论' },
  { id: 'usage', label: 'Token 用量' },
]
const PAGE_SIZE = 30
type Workspaces = components['schemas']['WorkspaceRef'][]

export function DataCenter({ workspaces, initialWorkspaceId, running, onClose }: {
  workspaces: Workspaces; initialWorkspaceId: string | null; running: boolean; onClose: () => void
}) {
  const [tab, setTab] = useState<Tab>('overview')
  const [memoryWorkspace, setMemoryWorkspace] = useState(initialWorkspaceId ?? '')
  const [usageWorkspace, setUsageWorkspace] = useState(initialWorkspaceId ?? '')
  const [draft, setDraft] = useState('')
  const [query, setQuery] = useState('')
  const [status, setStatus] = useState<MemoryFilters['status']>('all')
  const [modelDraft, setModelDraft] = useState('')
  const [model, setModel] = useState('')
  const [from, setFrom] = useState('')
  const [to, setTo] = useState('')
  const [page, setPage] = useState(0)
  const [selectedMemory, setSelectedMemory] = useState<MemoryEntry | null>(null)
  const [selectedCall, setSelectedCall] = useState<UsageEntry | null>(null)
  const body = useRef<HTMLDivElement>(null)
  const origin = useRef<HTMLButtonElement | null>(null)
  const validDates = !from || !to || from <= to
  const memory = useMemoryPage({ workspace_id: memoryWorkspace || undefined,
    kind: tab === 'medium' || tab === 'fact' || tab === 'conclusion' ? tab : 'all',
    query, status, offset: tab === 'overview' ? 0 : page * PAGE_SIZE, limit: PAGE_SIZE }, tab !== 'usage')
  const usage = useUsagePage({ workspace_id: usageWorkspace || undefined, model, ...utcRange(from, to),
    offset: tab === 'overview' ? 0 : page * PAGE_SIZE, limit: PAGE_SIZE }, (tab === 'overview' || tab === 'usage') && validDates)
  function closeDetails() {
    setSelectedMemory(null); setSelectedCall(null)
    origin.current?.focus({ preventScroll: true })
  }
  function reset() {
    setPage(0); setSelectedMemory(null); setSelectedCall(null)
    if (body.current) body.current.scrollTop = 0
  }
  function switchTab(value: Tab) { setTab(value); reset() }
  // 显式列举，避免新增 tab 时被 `tab !== 'usage'` 这类否定条件误吞。
  const memoryTab = tab === 'medium' || tab === 'fact' || tab === 'conclusion'
  const activeQuery = tab === 'usage' ? usage : memory
  const total = tab === 'usage' ? usage.data?.total : memory.data?.total
  const updating = (tab !== 'usage' && memory.isFetching)
    || ((tab === 'overview' || tab === 'usage') && usage.isFetching)
  const usageData = validDates ? usage.data : undefined
  const totals = usageData?.totals

  return <section className={styles.page} aria-label="数据中台" onKeyDown={(event) => {
    if (event.key === 'Escape' && (selectedMemory || selectedCall)) { event.stopPropagation(); closeDetails() }
  }}>
    <header className={styles.header}><div><span className={styles.eyebrow}>记忆与模型用量</span><h1>数据中台</h1>
      <p>查看已保存的知识与真实调用记录</p></div><div className={styles.actions}>
      <button type="button" disabled={updating || ((tab === 'overview' || tab === 'usage') && !validDates)} onClick={() => {
        closeDetails()
        if (tab !== 'usage') void memory.refetch()
        if (tab === 'overview' || tab === 'usage') void usage.refetch()
      }}>{updating ? '刷新中…' : '刷新数据'}</button>
      <button type="button" onClick={onClose}>返回会话</button></div></header>
    <nav className={styles.tabs} aria-label="数据分类">{tabs.map((item) => <button key={item.id}
      type="button" aria-current={tab === item.id ? 'page' : undefined} onClick={() => switchTab(item.id)}>{item.label}</button>)}</nav>
    <div className={styles.filters}>
      {tab !== 'usage' ? <label>记忆工作区<select aria-label="记忆工作区" value={memoryWorkspace} onChange={(event) => { setMemoryWorkspace(event.target.value); reset() }}>
        <option value="">启动工作区</option>{workspaces.map((workspace) => <option key={workspace.workspace_id} value={workspace.workspace_id}>{workspace.display_name}</option>)}
      </select></label> : null}
      {tab === 'usage' || tab === 'overview' ? <>
        <label>用量工作区<select aria-label="用量工作区" value={usageWorkspace} onChange={(event) => { setUsageWorkspace(event.target.value); reset() }}>
          <option value="">全部工作区</option>{workspaces.map((workspace) => <option key={workspace.workspace_id} value={workspace.workspace_id}>{workspace.display_name}</option>)}
        </select></label><label>开始日期<input aria-label="开始日期 UTC" type="date" value={from} onChange={(event) => { setFrom(event.target.value); reset() }} /></label>
        <label>结束日期<input aria-label="结束日期 UTC" type="date" value={to} onChange={(event) => { setTo(event.target.value); reset() }} /></label>
        <form onSubmit={(event) => { event.preventDefault(); setModel(modelDraft); reset() }}><label>模型<input aria-label="筛选模型" placeholder="模型名称" value={modelDraft} onChange={(event) => setModelDraft(event.target.value)} maxLength={200} /></label><button type="submit">筛选</button></form>
      </> : null}
      {memoryTab ? <>
        <label>记录状态<select aria-label="记录状态" value={status} onChange={(event) => { setStatus(event.target.value as MemoryFilters['status']); reset() }}>
          <option value="all">全部状态</option>{Object.entries(statusLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select></label><form onSubmit={(event) => { event.preventDefault(); setQuery(draft); reset() }}><label>搜索记忆<input aria-label="搜索记忆" placeholder="名称或正文关键词" value={draft} onChange={(event) => setDraft(event.target.value)} maxLength={500} /></label><button type="submit">搜索</button></form>
      </> : null}
    </div>
    <div ref={body} className={styles.body}>
      {running ? <p className={styles.notice}>当前会话仍在执行或等待审批。此处只查看数据，可返回会话处理。</p> : null}
      {!validDates && (tab === 'usage' || tab === 'overview') ? <p role="alert" className={styles.notice}>开始日期不能晚于结束日期。</p> : null}
      {((tab !== 'usage') && memory.isError) || ((tab === 'usage' || tab === 'overview') && usage.isError) ?
        <p role="alert" className={styles.notice}>数据暂时无法读取，请检查连接后点击“刷新数据”。</p> : null}
      {tab !== 'usage' ? memory.data?.notices?.map((note) => <p key={note} role="status" className={styles.notice}>{note}</p>) : null}
      {tab === 'overview' || tab === 'usage' ? usage.data?.notices?.map((note) => <p key={note} role="status" className={styles.notice}>{note}</p>) : null}
      {tab === 'overview' ? <>
        {validDates ? <div className={styles.metrics}>
          <Metric title="模型调用" value={totals ? countLabel(totals.calls) : usage.isPending ? '读取中…' : '未记录'} note={totals ? `${totals.failed_calls} 次失败 · ${totals.missing_usage_calls} 次用量不完整` : ''} />
          <Metric title="实际输入 Token" value={usage.isPending ? '读取中…' : countLabel(totals?.input_tokens)} note="含未命中、缓存读与缓存写" />
          <Metric title="实际输出 Token" value={usage.isPending ? '读取中…' : countLabel(totals?.output_tokens)} note="采用供应商实际上报量" />
          <Metric title="缓存命中率" value={usage.isPending ? '读取中…' : percentLabel(totals?.cache_hit_ratio)} note="按输入 Token 加权" />
        </div> : null}
        <div className={styles.resources}>{memory.data?.resources.map((resource) => <article key={resource.kind}>
          <span className={styles.badge}>{resource.enabled ? '召回已启用' : '召回已关闭'}</span><h2>{resource.kind === 'medium' ? '中期记忆' : '长期记忆'}</h2>
          <strong>{resource.state === 'available' ? countLabel(resource.kind === 'medium' ? memory.data?.counts.medium ?? 0 : (memory.data?.counts.fact ?? 0) + (memory.data?.counts.conclusion ?? 0)) : resource.state === 'missing' ? '尚无数据库' : '暂不可读'}</strong>
          <p>{resource.note}</p><button type="button" onClick={() => switchTab(resource.kind === 'medium' ? 'medium' : 'fact')}>查看记录 →</button>
        </article>)}</div>
        <div className={styles.chartGrid}><article className={styles.chart}><h2>每日调用趋势 <small>UTC · 最近有记录的 14 天</small></h2>
          {usageData?.daily.slice(-14).map((bucket) => <button key={bucket.label} type="button" className={styles.barRow} aria-label={`${bucket.label.slice(5)} ${bucket.totals.calls} 次`}
            title={`输入 ${countLabel(bucket.totals.input_tokens)} · 输出 ${countLabel(bucket.totals.output_tokens)} · 缓存 ${percentLabel(bucket.totals.cache_hit_ratio)}`} onClick={() => {
            setFrom(bucket.label); setTo(bucket.label); switchTab('usage')
          }}><span>{bucket.label.slice(5)}</span><span className={styles.barTrack}><span style={{ width: `${bucket.totals.calls / Math.max(1, ...usageData!.daily.map((day) => day.totals.calls)) * 100}%` }} /></span><span>{bucket.totals.calls} 次</span></button>)}
          {usageData?.daily.length === 0 ? <p>暂无可用调用记录。</p> : null}</article>
          <article className={styles.chart}><h2>模型分布 <small>调用量前 20 个</small></h2>{usageData?.models.slice().sort((a, b) => b.totals.calls - a.totals.calls).slice(0, 20).map((bucket) => <button key={bucket.label} type="button" className={styles.modelRow} aria-label={`${bucket.label} ${bucket.totals.calls} 次`} onClick={() => {
            setModel(bucket.label); setModelDraft(bucket.label); switchTab('usage')
          }}><span>{bucket.label}</span><strong>{bucket.totals.calls} 次</strong></button>)}
            <h2 className={styles.subheading}>调用用途</h2>{usageData?.purposes.map((bucket) => <p key={bucket.label} className={styles.modelRow}><span>{purposeLabel(bucket.label)}</span><strong>{bucket.totals.calls} 次</strong></p>)}</article></div>
        <p className={styles.footnote}>用量包含归档会话与已有分支，共享历史只计一次；筛选按调用开始时间。未上报指标不按零计算。记忆记录数量是该工作区可见范围，功能关闭仍可查看保留数据。</p>
      </> : null}
      {memoryTab ? <>
        <p className={styles.summary}>{memory.data?.incomplete ? '已读范围内' : '可见范围内'} {countLabel(memory.data?.total)} 条记录 · 全局、用户、项目与工作区作用域</p>
        {memory.data?.resources.filter((resource) => resource.kind === (tab === 'medium' ? 'medium' : 'long')).map((resource) => <p key={resource.kind} className={styles.footnote}>{resource.enabled ? '召回已启用。' : '召回已关闭，以下为保留数据。'}{resource.note}</p>)}
        <div className={styles.tableWrap}><table><thead><tr><th>记忆内容</th><th>作用域</th><th>状态</th><th>记录时间</th></tr></thead><tbody>
          {memory.data?.entries.map((entry) => <tr key={`${entry.kind}:${entry.id}`}><td><button type="button" className={styles.rowLink} aria-label={`${entry.title} ${entry.content.slice(0, 100)}`} onClick={(event) => {
            origin.current = event.currentTarget; setSelectedMemory(entry); setSelectedCall(null)
          }}><strong>{entry.title}</strong><span>{entry.content.slice(0, 100)}</span></button></td><td>{scopeLabels[entry.scope] ?? '未记录'}</td>
            <td><span className={styles.badge}>{statusLabels[entry.status] ?? '未记录'}</span></td><td>{timeLabel(entry.updated_at ?? entry.created_at)}</td></tr>)}</tbody></table></div>
      </> : null}
      {tab === 'usage' && validDates ? <>
        <div className={styles.metrics}><Metric title="调用次数" value={countLabel(totals?.calls)} note={`${totals?.failed_calls ?? 0} 次失败`} />
          <Metric title="未命中输入" value={countLabel(totals?.uncached_input_tokens)} /><Metric title="缓存读取" value={countLabel(totals?.cache_read_tokens)} />
          <Metric title="缓存命中率" value={percentLabel(totals?.cache_hit_ratio)} /></div>
        <p className={styles.summary}>{usage.data?.incomplete ? '已读范围内' : '筛选范围内'} {countLabel(usage.data?.total)} 次调用 · 日期按 UTC · 明细与汇总同一统计来源</p>
        <div className={styles.tableWrap}><table><thead><tr><th>模型 / 会话</th><th>用途</th><th>状态</th><th>输入</th><th>输出</th><th>开始时间</th></tr></thead><tbody>
          {usage.data?.entries.map((entry) => <tr key={`${entry.session_id}:${entry.call.model_call_id}`}><td><button type="button" className={styles.rowLink} aria-label={`${entry.call.model_id} ${entry.session_title}`} onClick={(event) => {
            origin.current = event.currentTarget; setSelectedCall(entry); setSelectedMemory(null)
          }}><strong>{entry.call.model_id}</strong><span>{entry.session_title}</span></button></td><td>{purposeLabel(entry.call.purpose)}</td>
            <td><span className={styles.badge}>{entry.call.status === 'ok' ? '成功' : entry.call.status === 'running' ? '进行中' : '失败'}</span></td>
            <td>{countLabel(inputTokens(entry))}</td><td>{countLabel(entry.call.actual_output_tokens)}</td><td>{timeLabel(entry.call.started_at)}</td></tr>)}</tbody></table></div>
      </> : null}
      {tab !== 'overview' && (tab !== 'usage' || validDates) ? <>
        {activeQuery.isPending ? <p role="status" className={styles.empty}>正在读取记录…</p> : total === 0 ? <p className={styles.empty}>没有匹配记录，试试其他筛选条件。</p> : null}
        <footer className={styles.pagination}><button type="button" disabled={page === 0 || activeQuery.isFetching} onClick={() => { setPage(page - 1); closeDetails() }}>上一页</button>
          <span>第 {page + 1} 页</span><button type="button" disabled={total == null || (page + 1) * PAGE_SIZE >= total || activeQuery.isFetching} onClick={() => { setPage(page + 1); closeDetails() }}>下一页</button></footer>
      </> : null}
    </div>
    {selectedMemory || selectedCall ? <aside className={styles.drawer} aria-label="数据详情"><header><h2>{selectedMemory ? kindLabels[selectedMemory.kind] : '模型调用详情'}</h2><button type="button" aria-label="关闭数据详情" onClick={closeDetails}>×</button></header>
      <div className={styles.detailBody}>{selectedMemory ? <><h3>{selectedMemory.title}</h3><dl>
        <Detail label="状态" value={statusLabels[selectedMemory.status] ?? '未记录'} /><Detail label="作用域" value={scopeLabels[selectedMemory.scope] ?? '未记录'} />
        <Detail label="置信度" value={percentLabel(selectedMemory.confidence)} /><Detail label="重要程度" value={percentLabel(selectedMemory.importance)} />
        <Detail label="创建时间" value={timeLabel(selectedMemory.created_at)} /><Detail label="过期时间" value={timeLabel(selectedMemory.expires_at)} />
        <Detail label="来源" value={selectedMemory.source ?? '未记录'} /><Detail label="记录 ID" value={selectedMemory.id} /></dl>
        <h3>保存正文</h3><pre>{selectedMemory.content}</pre>{selectedMemory.truncated ? <p className={styles.notice}>正文超过 20,000 字符，仅展示前部。</p> : null}</> : selectedCall ? <>
          <h3>{selectedCall.call.model_id}</h3><p>{selectedCall.session_title} · {purposeLabel(selectedCall.call.purpose)}</p><dl>
            <Detail label="输入 Token" value={countLabel(inputTokens(selectedCall))} /><Detail label="未命中输入" value={countLabel(selectedCall.call.actual_uncached_input_tokens)} />
            <Detail label="缓存读取" value={countLabel(selectedCall.call.actual_cache_read_tokens)} /><Detail label="缓存写入" value={selectedCall.call.actual_cache_write_tokens == null ? '未上报独立写入量' : countLabel(selectedCall.call.actual_cache_write_tokens)} />
            <Detail label="输出 Token" value={countLabel(selectedCall.call.actual_output_tokens)} /><Detail label="思考 Token" value={countLabel(selectedCall.call.actual_reasoning_tokens)} />
            <Detail label="估算输入（非实际量）" value={countLabel(selectedCall.call.estimated_prompt_tokens)} /><Detail label="模型耗时" value={selectedCall.call.model_elapsed_ms == null ? '未记录' : `${selectedCall.call.model_elapsed_ms} ms`} />
            <Detail label="开始时间" value={timeLabel(selectedCall.call.started_at)} /><Detail label="结束时间" value={timeLabel(selectedCall.call.finished_at)} />
            <Detail label="调用 ID" value={selectedCall.call.model_call_id} /><Detail label="重试来源" value={selectedCall.call.retry_of ?? '首次调用'} />
          </dl><p className={styles.footnote}>失败尝试与重试分别记录；未上报用量不代表没有消耗。</p></> : null}</div>
    </aside> : null}
  </section>
}

function Metric({ title, value, note }: { title: string; value: string; note?: string }) {
  return <article className={styles.metric}><span>{title}</span><strong>{value}</strong>{note ? <small>{note}</small> : null}</article>
}
function Detail({ label, value }: { label: string; value: string }) {
  return <div><dt>{label}</dt><dd>{value}</dd></div>
}
