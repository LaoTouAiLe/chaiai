import { useState } from 'react'

import type { McpServerDto } from '../../api/client'
import {
  asApiError, useAddMcpServer, useDeleteMcpServer, useEditMcpServer,
  useMcp, useReloadMcp, useSetCapabilities, useSetMcpServer,
} from '../../api/queries'
import { EditorDialog } from '../../components/ui/EditorDialog'
import styles from './McpSection.module.css'

const STATUS_LABEL: Record<string, string> = {
  disabled: '未启用', idle: '未连接', connecting: '连接中', connected: '已连接', failed: '连接失败',
}
const EXPOSURE_LABEL: Record<string, string> = {
  direct: '直接可见', deferred: '按需检索', hidden: '不可见',
}
const REASON_LABEL: Record<string, string> = {
  server_disabled: 'Server 未启用', not_in_enabled_tools: '不在白名单',
  in_disabled_tools: '已停用', schema_too_large: '工具定义超过大小限制',
  over_direct_quota: '超过直接可见配额，可按需检索',
}

function parseArgs(text: string): string[] {
  const value: unknown = JSON.parse(text || '[]')
  if (!Array.isArray(value) || !value.every(item => typeof item === 'string')) {
    throw new Error('参数必须是 JSON 字符串数组，例如 ["-y", "mcp-demo"]')
  }
  return value as string[]
}

function parseEnv(text: string): Record<string, string> {
  const result: Record<string, string> = Object.create(null) as Record<string, string>
  for (const line of text.split(/\r?\n/)) {
    if (!line.trim()) continue
    const index = line.indexOf('=')
    const key = line.slice(0, index).trim()
    if (index < 1 || !/^[A-Za-z_][A-Za-z0-9_]*$/.test(key)) {
      throw new Error('环境变量每行填写 KEY=VALUE，键只允许字母、数字与下划线')
    }
    if (Object.hasOwn(result, key)) throw new Error(`环境变量 ${key} 重复`)
    result[key] = line.slice(index + 1)
  }
  return result
}

/**
 * 请求头文本 → 键值表（2026-11-02，远程 MCP）。
 *
 * 用 `KEY: VALUE` 而不是 `KEY=VALUE`：这是 HTTP 头的通行写法，用户复制 `Authorization: Bearer …`
 * 进来不用改。值不做格式校验——token 什么形状都有；只挡换行（那等于往请求里塞新头）。
 */
function parseHeaders(text: string): Record<string, string> {
  const result: Record<string, string> = Object.create(null) as Record<string, string>
  for (const line of text.split(/\r?\n/)) {
    if (!line.trim()) continue
    const index = line.indexOf(':')
    const key = line.slice(0, index).trim()
    if (index < 1 || !/^[A-Za-z0-9][A-Za-z0-9-]{0,63}$/.test(key)) {
      throw new Error('请求头每行填写 KEY: VALUE，键只允许字母、数字与连字符')
    }
    if (Object.hasOwn(result, key)) throw new Error(`请求头 ${key} 重复`)
    result[key] = line.slice(index + 1).trim()
  }
  return result
}

function ServerCard({ server, unavailable, onEdit }: {
  server: McpServerDto; unavailable: boolean; onEdit: (server: McpServerDto) => void
}) {
  const setServer = useSetMcpServer()
  const remove = useDeleteMcpServer()
  const [deleting, setDeleting] = useState(false)
  const [listsOpen, setListsOpen] = useState(false)
  const [limited, setLimited] = useState(false)
  const [allowed, setAllowed] = useState('')
  const [denied, setDenied] = useState('')
  const busy = unavailable || setServer.isPending || remove.isPending
  const tools = server.tools ?? []
  const disabledTools = server.disabled_tools ?? tools.filter(tool => tool.disabled).map(tool => tool.tool)
  const failure = setServer.error ?? remove.error
  const toggleTool = (raw: string) => {
    const disabling = !disabledTools.includes(raw) && (server.enabled_tools == null || server.enabled_tools.includes(raw))
    const changes: Record<string, unknown> = {
      disabled_tools: disabling ? [...disabledTools, raw] : disabledTools.filter(item => item !== raw),
    }
    if (server.enabled_tools != null) {
      changes.enabled_tools = disabling
        ? server.enabled_tools.filter(item => item !== raw)
        : [...new Set([...server.enabled_tools, raw])]
    }
    setServer.mutate({ name: server.name, changes })
  }
  const openLists = () => {
    setLimited(server.enabled_tools != null)
    setAllowed((server.enabled_tools ?? []).join('\n'))
    setDenied(disabledTools.join('\n'))
    setServer.reset()
    setListsOpen(open => !open)
  }
  const closeLists = () => { setListsOpen(false); setServer.reset() }
  const parseNames = (text: string) => [...new Set(text.split(/[,，\r\n]+/).map(x => x.trim()).filter(Boolean))]
  return (
    <li className={styles.card}>
      <div className={styles.cardHead}>
        <div className={styles.cardTitle}>
          <span className={styles.serverName}>{server.name}</span>
          <span className={styles.status} data-status={server.status}>{STATUS_LABEL[server.status] ?? server.status}</span>
          <span className={styles.count}>{server.tool_count} 个工具</span>
        </div>
        <label className={styles.toggle}>
          <input type="checkbox" checked={server.enabled} aria-label={`${server.name} 开关`}
            disabled={busy} onChange={event => setServer.mutate({
              name: server.name, changes: { enabled: event.target.checked },
            })} />
          {server.enabled ? '已启用' : '已关闭'}
        </label>
      </div>
      <code className={styles.command}>{server.transport === 'http'
        ? (server.url ?? '')
        : JSON.stringify([server.command, ...(server.args ?? [])])}</code>
      <p className={styles.note}>{server.transport === 'http'
        ? '启用后会连接上面的远程端点：工具参数与结果都会经过它。'
        : '启用会在本机执行上面的第三方命令；仅启用你信任的 Server。'}</p>
      <div className={styles.headerActions}>
        <button type="button" className={styles.ghost} disabled={busy} onClick={() => onEdit(server)} aria-label={`编辑 ${server.name}`}>编辑</button>
        <button type="button" className={styles.ghost} disabled={busy} onClick={openLists} aria-label={`${server.name} 工具名单`}>工具名单</button>
        <button type="button" className={styles.ghost} disabled={busy} onClick={() => { remove.reset(); setDeleting(true) }} aria-label={`删除 ${server.name}`}>删除</button>
      </div>
      {deleting ? <div className={styles.confirm}>
        <p className={styles.note}>删除 {server.name} 的配置和工具设置？以后可重新添加。</p>
        <button type="button" className={styles.ghost} disabled={busy} onClick={() => setDeleting(false)}>取消删除</button>
        <button type="button" className={styles.ghost} disabled={busy} onClick={() => remove.mutate(server.name)}>确认删除</button>
      </div> : null}
      {failure && !listsOpen ? <p className={styles.error} role="alert">{asApiError(failure).message}</p> : null}
      {server.failure_message ? <p className={styles.error}>{server.failure_message}</p> : null}
      {listsOpen ? <EditorDialog title={`工具名单 · ${server.name}`} onClose={closeLists} dismissible={!busy}><div className={styles.form}>
        <label className={styles.toggle}><input type="checkbox" checked={limited} aria-label={`${server.name} 使用白名单`}
          onChange={event => setLimited(event.target.checked)} />只允许白名单中的工具</label>
        {limited ? <label className={styles.field}>白名单（原始工具名，每行一个；空名单禁用全部工具）
          <textarea aria-label={`${server.name} 白名单`} value={allowed} onChange={event => setAllowed(event.target.value)} rows={3} />
        </label> : null}
        <label className={styles.field}>停用名单（原始工具名，每行一个）
          <textarea aria-label={`${server.name} 停用名单`} value={denied} onChange={event => setDenied(event.target.value)} rows={3} />
        </label>
        <button type="button" className={styles.primary} disabled={busy} onClick={() => setServer.mutate({
          name: server.name, changes: { enabled_tools: limited ? parseNames(allowed) : null, disabled_tools: parseNames(denied) },
        }, { onSuccess: () => setListsOpen(false) })}>保存工具名单</button>
        {failure ? <p className={styles.error} role="alert">{asApiError(failure).message}</p> : null}
      </div></EditorDialog> : null}
      {tools.length > 0 ? <ul className={styles.tools}>{tools.map(tool => {
        const disabled = disabledTools.includes(tool.tool)
        const permitted = !disabled && (server.enabled_tools == null || server.enabled_tools.includes(tool.tool))
        return <li key={tool.name} className={styles.tool}>
          <button type="button" className={styles.toolButton} data-disabled={!permitted} aria-pressed={permitted}
            aria-label={`${tool.name} 启用`} disabled={busy} title={tool.description ?? ''} onClick={() => toggleTool(tool.tool)}>
            <code className={styles.toolName}>{tool.name}</code>
            <span className={styles.exposure} data-exposure={tool.exposure}>
              {disabled ? '已停用' : tool.reason && tool.exposure === 'hidden'
                ? (REASON_LABEL[tool.reason] ?? tool.reason) : (EXPOSURE_LABEL[tool.exposure] ?? tool.exposure)}
            </span>
          </button>
        </li>
      })}</ul> : null}
    </li>
  )
}

export function McpSection() {
  const query = useMcp()
  const reload = useReloadMcp()
  const add = useAddMcpServer()
  const edit = useEditMcpServer()
  const capabilities = useSetCapabilities()
  const [formOpen, setFormOpen] = useState(false)
  const [editing, setEditing] = useState<McpServerDto | null>(null)
  const [draftName, setDraftName] = useState('')
  const [draftTransport, setDraftTransport] = useState<'stdio' | 'http'>('stdio')
  const [draftCommand, setDraftCommand] = useState('npx')
  const [draftArgs, setDraftArgs] = useState('[]')
  const [draftEnv, setDraftEnv] = useState('')
  const [removeEnv, setRemoveEnv] = useState<string[]>([])
  const [draftUrl, setDraftUrl] = useState('')
  const [draftHeaders, setDraftHeaders] = useState('')
  const [removeHeaders, setRemoveHeaders] = useState<string[]>([])
  const [formError, setFormError] = useState('')
  const view = query.data
  const failure = reload.error ?? capabilities.error ?? query.error
  const formFailure = formError || (edit.error ? asApiError(edit.error).message : add.error ? asApiError(add.error).message : '')
  const busy = add.isPending || edit.isPending
  const resetForm = () => {
    add.reset(); edit.reset(); setFormError(''); setDraftEnv(''); setRemoveEnv([])
    setDraftHeaders(''); setRemoveHeaders([])
  }
  const openAdd = () => {
    resetForm(); setEditing(null); setDraftName(''); setDraftTransport('stdio')
    setDraftCommand('npx'); setDraftArgs('[]'); setDraftUrl(''); setFormOpen(true)
  }
  const openEdit = (server: McpServerDto) => {
    resetForm(); setEditing(server); setDraftName(server.name)
    setDraftTransport(server.transport === 'http' ? 'http' : 'stdio')
    setDraftCommand(server.command ?? ''); setDraftUrl(server.url ?? '')
    setDraftArgs(JSON.stringify(server.args ?? [])); setFormOpen(true)
  }
  const closeForm = () => { setFormOpen(false); resetForm(); setEditing(null) }
  const submit = () => {
    try {
      // 只提交当前传输那组字段：把 url 跟着 stdio 一起发过去会被模型层直接拒（那是设计如此）。
      const changes: Record<string, unknown> = draftTransport === 'http'
        ? {
            url: draftUrl.trim(), headers: parseHeaders(draftHeaders),
            ...(editing ? { remove_headers: removeHeaders } : {}),
          }
        : {
            command: draftCommand.trim(), args: parseArgs(draftArgs), env: parseEnv(draftEnv),
            ...(editing ? { remove_env: removeEnv } : {}),
          }
      // `type` 只在"要切传输"或"本身就是 http"时带上：stdio 是默认值，多带一个同义字段
      // 只会让请求体与既有测试/日志都变脏。
      if (draftTransport === 'http' || editing?.transport === 'http') changes.type = draftTransport
      setFormError('')
      if (editing) edit.mutate({ name: editing.name, changes }, { onSuccess: closeForm })
      else add.mutate({ name: draftName.trim(), ...changes }, { onSuccess: closeForm })
    } catch (error) { setFormError(error instanceof Error ? error.message : '表单格式不合法') }
  }
  if (!view) return <section className={styles.section}><h3 className={styles.title}>MCP 外部工具</h3>
    <p className={styles.note} role={failure ? 'alert' : undefined}>{failure ? asApiError(failure).message : '正在读取…'}</p></section>
  const summary = view.summary
  const unavailable = view.store_unavailable ?? false
  return (
    <section className={styles.section}>
      <div className={styles.header}>
        <div><h3 className={styles.title}>MCP 外部工具</h3>
          <p className={styles.note}>工具在本机的第三方进程中运行，返回内容属于外部内容。工具开关与名单保存后<strong>下一轮生效</strong>。</p>
          <p className={styles.note}>MCP 仅供主 Agent 使用，子 Agent 暂不支持。</p>
        </div>
        <div className={styles.headerActions}>
          <button type="button" className={styles.ghost} disabled={busy || unavailable} onClick={openAdd}>添加 Server</button>
          <button type="button" className={styles.ghost} disabled={reload.isPending || busy} onClick={() => reload.mutate()}>重读配置</button>
        </div>
      </div>
      <label className={styles.toggle}><input type="checkbox" aria-label="MCP 总开关" checked={view.enabled}
        disabled={unavailable || capabilities.isPending} onChange={event => capabilities.mutate({ mcp: event.target.checked }, {
          onSuccess: () => { void query.refetch() },
        })} />启用 MCP 外部工具</label>
      {!view.enabled ? <p className={styles.note}>总开关已关闭，可管理配置；启用后才能连接和使用工具。</p> : null}
      {unavailable ? <p className={styles.error} role="alert">设置库不可用，无法保存 Server 或工具设置。</p> : null}
      <p className={styles.scale}>已配置 {summary?.configured ?? 0} · 已启用 {summary?.enabled ?? 0} · 已连接 {summary?.connected ?? 0} · 失败 {summary?.failed ?? 0} · 工具 {summary?.tools ?? 0} · 直接可见 {summary?.direct ?? 0} · 其余 {summary?.deferred ?? 0} 按需检索</p>
      {view.config_error ? <p className={styles.error} role="alert">{view.config_error}</p> : null}
      {failure ? <p className={styles.error} role="alert">{asApiError(failure).message}</p> : null}
      {formOpen ? <EditorDialog title={editing ? '编辑 MCP Server' : '添加 MCP Server'} onClose={closeForm} dismissible={!busy}>
      <form className={styles.form} onSubmit={event => { event.preventDefault(); submit() }}>
        <label className={styles.field}>名称<input aria-label="名称" value={draftName} disabled={Boolean(editing) || busy} onChange={event => setDraftName(event.target.value)} placeholder="只允许字母、数字、下划线、连字符" /></label>
        <label className={styles.field}>连接方式<select aria-label="连接方式" value={draftTransport} disabled={busy} onChange={event => setDraftTransport(event.target.value === 'http' ? 'http' : 'stdio')}>
          <option value="stdio">本机进程（stdio）</option>
          <option value="http">远程端点（http）</option>
        </select></label>
        {draftTransport === 'http' ? <>
          <label className={styles.field}>地址<input aria-label="地址" value={draftUrl} disabled={busy} onChange={event => setDraftUrl(event.target.value)} placeholder="https://example.com/mcp" /></label>
          <label className={styles.field}>请求头{editing ? '（留空保留原值，填写同名键更新）' : ''}<textarea aria-label="请求头" value={draftHeaders} disabled={busy} onChange={event => setDraftHeaders(event.target.value)} rows={3} placeholder="Authorization: Bearer …" autoComplete="off" /></label>
          {editing && (editing.header_keys ?? []).length > 0 ? <fieldset className={styles.envKeys}><legend>已保存的请求头（勾选要删除的键）</legend>
            {(editing.header_keys ?? []).map(key => <label key={key} className={styles.toggle}><input type="checkbox" checked={removeHeaders.includes(key)} aria-label={`删除请求头 ${key}`} onChange={event => setRemoveHeaders(keys => event.target.checked ? [...keys, key] : keys.filter(item => item !== key))} />{key}</label>)}
          </fieldset> : null}
        </> : <>
          <label className={styles.field}>命令<input aria-label="命令" value={draftCommand} disabled={busy} onChange={event => setDraftCommand(event.target.value)} placeholder="npx" /></label>
          <label className={styles.field}>参数（JSON 数组，含空格的参数保留在同一项）<textarea aria-label="参数" value={draftArgs} disabled={busy} onChange={event => setDraftArgs(event.target.value)} rows={2} placeholder='["-y", "mcp-demo"]' /></label>
          <label className={styles.field}>环境变量{editing ? '（留空保留原值，填写同名键更新）' : ''}<textarea aria-label="环境变量" value={draftEnv} disabled={busy} onChange={event => setDraftEnv(event.target.value)} rows={3} placeholder="每行一个 KEY=VALUE" autoComplete="off" /></label>
          {editing && (editing.env_keys ?? []).length > 0 ? <fieldset className={styles.envKeys}><legend>已保存的环境变量（勾选要删除的键）</legend>
            {(editing.env_keys ?? []).map(key => <label key={key} className={styles.toggle}><input type="checkbox" checked={removeEnv.includes(key)} aria-label={`删除环境变量 ${key}`} onChange={event => setRemoveEnv(keys => event.target.checked ? [...keys, key] : keys.filter(item => item !== key))} />{key}</label>)}
          </fieldset> : null}
        </>}
        <p className={styles.note}>凭据（环境变量 / 请求头）以明文写进 <code>{view.config_path ?? 'mcp.json'}</code>，页面不回显值。{editing ? '修改连接方式或端点后 Server 会关闭，请核对后重新启用。' : '新增 Server 默认关闭。'}</p>
        {formFailure ? <p className={styles.error} role="alert">{formFailure}</p> : null}
        <div className={styles.formActions}><button type="button" className={styles.ghost} disabled={busy} onClick={closeForm}>取消</button><button type="submit" className={styles.primary} disabled={busy || unavailable || !draftName.trim() || (draftTransport === 'http' ? !draftUrl.trim() : !draftCommand.trim())}>保存</button></div>
      </form></EditorDialog> : null}
      {(view.servers ?? []).length === 0 ? <p className={styles.empty}>还没有配置任何 MCP Server。用上面的「添加 Server」，或把 <code>mcpServers</code> 写进 <code>{view.config_path ?? 'mcp.json'}</code> 后点「重读配置」。</p>
        : <ul className={styles.list}>{(view.servers ?? []).map(server => <ServerCard key={server.name} server={server} unavailable={unavailable} onEdit={openEdit} />)}</ul>}
    </section>
  )
}
