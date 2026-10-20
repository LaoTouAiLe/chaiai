import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { getPreferences, savePreferences, testMemoryPreferences } from '../../api/client'
import type { MemoryProbeDto, PreferencesDto, PreferenceValues } from '../../api/client'
import { generalDefaults, preferencesKey } from './preferencesContext'
import { OssCredentialField } from './OssCredentialField'
import { WebSearchKeyField } from './WebSearchKeyField'
import styles from './PreferenceSection.module.css'

export type PreferenceGroup = 'general' | 'execution' | 'retrieval' | 'web_access' | 'attachments' | 'memory' | 'medium_term' | 'embedding' | 'advanced'
type Field = { key: string; label: string; note: string; min?: number; max?: number; step?: number | 'any'; type?: 'text'; providers?: readonly string[]; required?: boolean; unlimited?: boolean; options?: readonly (readonly [string, string])[]; when?: readonly (readonly [string, string])[] }
const FIELDS: Record<Exclude<PreferenceGroup, 'advanced'>, Field[]> = {
  general: [
    { key: 'theme', label: '主题', note: '跟随系统会随系统外观切换。', options: [['light', '浅色'], ['dark', '深色'], ['system', '跟随系统']] },
    { key: 'font_size', label: '文字大小', note: '调整会话和界面的文字比例。', options: [['small', '小'], ['standard', '标准'], ['large', '大']] },
    { key: 'animations', label: '界面动画', note: '关闭后停止流动与过渡动画；同时尊重系统的减少动态效果设置。' },
    { key: 'send_shortcut', label: '发送快捷键', note: 'Shift+Enter 始终换行；Ctrl+Enter 在 macOS 上也支持 Command+Enter。', options: [['enter', 'Enter 发送'], ['ctrl_enter', 'Ctrl / ⌘ + Enter']] },
    { key: 'reasoning_expanded', label: '默认展开 Think', note: '仅展示模型实际返回的思考内容，每条消息仍可单独折叠。' },
  ],
  execution: [
    { key: 'max_tool_loops', label: '每轮工具循环上限', note: '留空表示不限，由模型决定继续或结束。', min: 1, unlimited: true },
    { key: 'max_retries_per_step', label: '重复失败反馈阈值', note: '超过后反馈给模型，不强制停止或转交用户。', min: 0 },
    { key: 'shell_timeout_seconds', label: '命令超时（秒）', note: '留空表示默认不限时；命令仍可取消或单独指定超时。', min: 1, unlimited: true },
    { key: 'shell_max_output_chars', label: '命令输出上限（字符）', note: '限制保留的命令输出长度。', min: 1 },
    { key: 'safe_delete', label: '安全删除（移入回收站）', note: '删除文件时移进数据目录的 trash 子目录（冻结版默认是 %LOCALAPPDATA%\\chaiai\\trash），而不是立即抹掉，可以手动找回；移动失败会报错而不会退化成直接删除。只对文件工具生效，终端里的删除命令不受它管辖。' },
  ],
  retrieval: [
    { key: 'enabled', label: '启用任务前自动检索', note: '控制工具任务开始前的自动代码检索；不影响模型主动调用 grep / glob。' },
    { key: 'max_files', label: '扫描文件上限', note: '限制任务前自动检索扫描的文件数。', min: 1 },
    { key: 'max_results', label: '返回结果上限', note: '限制任务前自动检索返回给模型的结果条数。', min: 1 },
    { key: 'timeout_seconds', label: '检索超时（秒）', note: '限制任务前自动检索时长。', min: 1 },
    { key: 'context_lines', label: '上下文行数', note: '匹配结果附近保留的代码行数，允许为 0。', min: 0 },
  ],
  web_access: [
    { key: 'provider', label: '检索方式', note: '自动：配了 Tavily 密钥就用 API，否则用网页版检索。显式指定后，失败不会静默换后端。', options: [['auto', '自动'], ['tavily', 'Tavily API'], ['bing_html', 'Bing 网页版'], ['duckduckgo_html', 'DuckDuckGo 网页版']] },
    { key: 'max_results', label: '检索结果条数上限', note: '模型未指定条数时用它（1–20）。', min: 1, max: 20 },
    { key: 'max_chars', label: '单次取回正文上限（字符）', note: '超过即截断，并在结果里说明；模型可按 start_char 续读（500–100000）。', min: 500, max: 100000 },
    { key: 'max_search_calls', label: '每轮检索次数上限', note: '留空表示不限。', min: 1, unlimited: true },
    { key: 'max_read_pages', label: '每轮读取地址数上限', note: '留空表示不限。', min: 1, unlimited: true },
    { key: 'max_reads_per_page', label: '单页面读取次数上限', note: '留空表示不限。', min: 1, unlimited: true },
    { key: 'timeout_seconds', label: '单次请求超时（秒）', note: '超时按失败如实报告，不重试（1–120）。', min: 1, max: 120 },
    { key: 'domain_policy', label: '域名策略', note: '默认放行：除黑名单外都允许（主流做法）。仅白名单：只允许白名单里的域名及其子域。两档都不放松本机与内网拦截。', options: [['allow', '默认放行'], ['allowlist', '仅白名单']] },
    { key: 'denied_domains', label: '拒绝的域名', note: '只写主机名（如 example.com），多个用逗号或空格分隔；含其子域。', type: 'text' },
    { key: 'allowed_domains', label: '仅白名单时允许的域名', note: '只写主机名，多个用逗号或空格分隔；仅在"仅白名单"档生效。', type: 'text' },
  ],
  attachments: [
    { key: 'image_context_turns', label: '图片上下文轮数', note: '发给模型的图片覆盖最近几轮（含当前轮，1–10）。1 = 只发当前轮的图，下一轮就换成文字占位；值越大，历史图重发得越多（每张图固定按 1024 token 计）。', min: 1, max: 10 },
    { key: 'backend', label: '图片存储位置', note: '本机：图片留在本机，发送时把内容编码进请求；对象存储：图片上传到 S3 兼容存储，请求里只发公开链接（需要桶允许公开读）。', options: [['local', '本机文件'], ['oss', '对象存储（S3 兼容）']] },
    { key: 'oss_endpoint', label: '对象存储端点', note: 'S3 兼容端点，例如 https://oss-cn-hangzhou.aliyuncs.com；本机 MinIO 可用 http://127.0.0.1:9000。', type: 'text', when: [['backend', 'oss']] },
    { key: 'oss_region', label: '区域', note: '多数 S3 兼容存储填 auto 即可；云厂商要求具体区域时按文档填。', type: 'text', when: [['backend', 'oss']] },
    { key: 'oss_bucket', label: '桶名', note: '存图片的桶；需要允许公开读（模型与页面都直接取链接）。', type: 'text', when: [['backend', 'oss']] },
    { key: 'oss_public_base_url', label: '公开访问前缀', note: '必须是 https 地址，例如 https://cdn.example.com；最终图片链接 = 前缀 + 对象键。', type: 'text', when: [['backend', 'oss']] },
    { key: 'oss_key_prefix', label: '对象键前缀', note: '默认 chaiai/，便于与同桶其它数据分开。', type: 'text', when: [['backend', 'oss']] },
    { key: 'oss_path_style', label: '使用 path-style 地址', note: 'MinIO/自建通常要开；云厂商默认关。', when: [['backend', 'oss']] },
  ],
  memory: [
    { key: 'enabled', label: '启用长期记忆', note: '控制长期记忆的读取与保存；关闭不会删除已有记录。' },
    { key: 'confidence_threshold', label: '置信度阈值', note: '召回记忆需要达到的最低置信度（0–1），不限制写入。', min: 0, max: 1, step: 'any' },
    { key: 'importance_threshold', label: '重要性阈值', note: '召回记忆需要达到的最低有效重要性（0–1），不限制写入。', min: 0, max: 1, step: 'any' },
    { key: 'aging_enabled', label: '启用记忆老化', note: '按使用时间降低有效重要性；显式过期仍然生效。' },
    { key: 'forget_after_days', label: '记忆老化天数', note: '超过此天数的未使用事实不再自动召回；已有记录不物理删除。', min: 1 },
  ],
  medium_term: [
    { key: 'enabled', label: '启用中期记忆', note: '使用向量相似度召回历史记忆；需要先启用并配置 Embedding。关闭不会删除已有数据。' },
    { key: 'top_k', label: '召回条数上限', note: '每次向模型提供的中期记忆条数上限。', min: 1 },
    { key: 'min_score', label: '最低语义相似度', note: '0–1；阈值取决于向量模型，换模型后需要重新标定。', min: 0, max: 1, step: 'any' },
    { key: 'decay_half_life_days', label: '时间衰减半衰期（天）', note: '每经过此天数，记忆的时间评分降为一半；不会删除记录。', min: 1 },
  ],
  embedding: [
    { key: 'enabled', label: '启用 Embedding', note: '启用向量生成能力；保存本身不会下载模型或调用接口。' },
    { key: 'provider', label: '向量生成方式', note: '本地 ONNX 使用模型缓存；兼容 API 使用已配置的密钥环境变量。', options: [['onnx_local', '本地 ONNX'], ['openai_compatible', 'OpenAI 兼容 API']] },
    { key: 'model', label: '向量模型名称', note: '填写所选提供方支持的模型名称；已有集合会检查模型兼容性。', type: 'text', required: true },
    { key: 'cache_dir', label: '模型缓存目录', note: '本机可写的绝对路径；已加载缓存路径后，更换目录需保存并重启后端，再测试和运行任务。首次加载可能下载模型。', type: 'text', required: true, providers: ['onnx_local'] },
    { key: 'local_files_only', label: '仅使用本地缓存', note: '禁止联网下载；缺少权重时测试或召回会明确失败。', providers: ['onnx_local'] },
    { key: 'base_url', label: 'Embedding API 端点', note: '仅接受 HTTP(S) 地址，不允许内嵌密钥、查询参数或片段。', type: 'text', required: true, providers: ['openai_compatible'] },
    { key: 'api_key_env', label: 'API Key 环境变量名', note: '填写变量名称，不能填写密钥。密钥放在本机 .env 或环境变量中，修改后重启后端加载。', type: 'text', required: true, providers: ['openai_compatible'] },
    { key: 'allow_plaintext_http', label: '允许非本机端点明文 HTTP', note: '只在你确认可信网络中的明文传输风险时勾选；HTTPS 与本机回环 HTTP 无需勾选。', providers: ['openai_compatible'] },
  ],
}
const TITLES: Record<PreferenceGroup, string> = { general: '通用与外观', execution: '任务执行', retrieval: '任务前自动代码检索', web_access: '联网检索', attachments: '图片与附件', memory: '长期记忆', medium_term: '中期记忆', embedding: '向量库与 Embedding', advanced: '高级配置概览' }
const ADVANCED: Record<string, string> = {
  compression_enabled: '启用压缩', compression_trigger_ratio: '压缩触发比例',
  preserve_recent_turns: '保留最近轮数', preserve_recent_tool_steps: '保留最近工具步数',
  tool_step_budget_chars: '工具步字符预算', tool_result_keep_chars: '单条工具结果字符阈值',
  summary_max_chars: '摘要字符上限', short_term_enabled: '短期记忆',
  agents_enabled: 'AGENTS 规则', agents_max_file_chars: '单份规则字符上限',
  agents_max_total_chars: '规则总字符上限',
}
type Draft = { base: PreferencesDto; fields: Record<string, string | boolean> }
function fieldsFor(view: PreferencesDto, group: Exclude<PreferenceGroup, 'advanced'>, defaults = false) {
  return Object.fromEntries(Object.entries((defaults ? view.defaults : view.values)[group] ?? generalDefaults).map(([key, value]) => [key, typeof value === 'boolean' ? value : String(value)]))
}

export function PreferenceSection({ group }: { group: PreferenceGroup }) {
  const queryClient = useQueryClient()
  // Closing the dialog unmounts its sections; reopening must refresh the shared
  // cache even though the appearance provider stays mounted. Drafts keep their base version.
  const query = useQuery({ queryKey: preferencesKey, queryFn: getPreferences, staleTime: Infinity, refetchOnMount: 'always' })
  const [draft, setDraft] = useState<Draft | null>(null)
  const [operation, setOperation] = useState<'save' | 'test' | null>(null)
  const busy = operation !== null
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)
  const [probe, setProbe] = useState<{ version: number; result: MemoryProbeDto } | null>(null)
  const view = query.data
  if (!view) return <div role="status">{query.isError ? '设置读取失败。' : '正在读取设置…'} <button onClick={() => void query.refetch()}>重新读取</button></div>
  if (group === 'advanced') return <section className={styles.section}>
    <header className={styles.head}>
      <h3 className={styles.title}>{TITLES[group]}</h3>
      <p className={styles.note}>只读 · 当前进程启动时加载的配置。压缩与历史规则需要单独验证，暂不开放页面修改。</p>
    </header>
    <dl className={styles.overview}>{Object.entries(view.advanced).map(([key, value]) => <div key={key}><dt>{ADVANCED[key] ?? key}</dt><dd>{typeof value === 'boolean' ? (value ? '开启' : '关闭') : value}</dd></div>)}</dl>
  </section>
  const fields = draft?.fields ?? fieldsFor(view, group)
  const change = (key: string, value: string | boolean) => {
    setDraft(current => {
      const next = { ...(current?.fields ?? fieldsFor(view, group)), [key]: value }
      if (group === 'embedding' && key === 'provider') {
        Object.assign(next, { model: '', allow_plaintext_http: false }, value === 'onnx_local'
          ? { base_url: '', api_key_env: '' }
          : { cache_dir: '', local_files_only: false, api_key_env: 'EMBEDDING_API_KEY' })
      }
      return { base: current?.base ?? view, fields: next }
    })
    setSaved(false)
    setError(null)
    setProbe(null)
  }
  const draftValues = (): PreferenceValues => {
    const base = draft?.base ?? view
    const original = base.values[group] ?? generalDefaults
    const numericKeys = new Set(Object.entries(original).filter(([, value]) => typeof value === 'number').map(([key]) => key))
    // 域名清单是**数组**：输入框里是"a.com, b.com"，提交前拆回数组——否则会以字符串发出去，
    // 被服务端按形状拒掉（422），用户看到的是"保存失败"却不知道自己哪里填错了。
    const listKeys = new Set(Object.entries(original).filter(([, value]) => Array.isArray(value)).map(([key]) => key))
    const replacement = Object.fromEntries(Object.entries(fields).map(([key, value]) => [
      key,
      FIELDS[group].some(field => field.key === key && field.unlimited) ? (value === '' || value === null ? null : Number(value))
        : numericKeys.has(key) ? Number(value)
        : listKeys.has(key) ? String(value).split(/[\s,，;；]+/).filter(Boolean)
          : value,
    ]))
    return { ...base.values, [group]: replacement }
  }
  const save = async () => {
    if (!draft || busy) return
    setOperation('save')
    setError(null)
    setProbe(null)
    try {
      const result = await savePreferences(draftValues(), draft.base.version)
      // An earlier settings read must not replace the just-saved restart status.
      await queryClient.cancelQueries({ queryKey: preferencesKey })
      queryClient.setQueryData(preferencesKey, result)
      setDraft(null)
      setSaved(true)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '设置保存失败')
    } finally { setOperation(null) }
  }
  const test = async () => {
    if (busy) return
    const version = draft?.base.version ?? view.version
    setOperation('test')
    setError(null)
    setProbe(null)
    try {
      setProbe({ version, result: await testMemoryPreferences(draftValues(), version) })
    } catch (caught) { setError(caught instanceof Error ? caught.message : '测试失败') }
    finally { setOperation(null) }
  }
  const resources = view.resources
  return <form className={styles.section} onSubmit={event => {
    event.preventDefault()
    if ((event.nativeEvent as SubmitEvent).submitter?.getAttribute('data-action') === 'test') void test()
    else void save()
  }}>
    <header className={styles.head}>
      <h3 className={styles.title}>{TITLES[group]}</h3>
      <p className={styles.note}>{group === 'general' ? '保存后立即生效；恢复初始值使用应用默认外观。' : group === 'embedding' ? '普通模型参数保存后从下一次新任务生效；更换本地缓存目录可能需要重启后端，准备状态会说明。正在执行或等待审批的任务保留原值。初值来自加载的 config.yaml。' : '保存后从下一次新任务生效；正在执行或等待审批的任务保留原值。初值来自加载的 config.yaml。'} 设置写入本地设置库。</p>
    </header>
    {(group === 'embedding' || group === 'medium_term') && resources?.restart_required ? <p role="alert" className={styles.error}>新缓存目录需重启后端生效。配置已保留，请重启后测试，再开始使用中期记忆的新任务。</p> : null}
    {group === 'retrieval' || group === 'web_access' || group === 'memory' || group === 'medium_term' || group === 'embedding' ? <p className={styles.note}>修改这些参数会改变模型收到的内容，可能影响回答质量与缓存命中率。</p> : null}
    {group === 'embedding' && resources ? <>
      <dl className={styles.overview} aria-label="向量库信息">
        <div><dt>向量库</dt><dd>{resources.backend}</dd></div>
        <div><dt>数据目录（只读）</dt><dd>{resources.path}</dd></div>
        <div><dt>集合名称（只读）</dt><dd>{resources.collection}</dd></div>
        <div><dt>Chroma 依赖</dt><dd>{resources.vector_available ? '已安装' : '未安装，需安装 memory 可选依赖'}</dd></div>
        <div><dt>数据目录状态</dt><dd>{resources.vector_initialized ? '已存在向量库文件' : '尚未初始化，首次使用时创建'}</dd></div>
        <div><dt>密钥环境变量</dt><dd>{resources.key_configured ? '已配置，不回读密钥' : '未配置或本地模型不需要'}</dd></div>
      </dl>
      <p className={styles.note}>已保存配置的准备状态：{resources.restart_required ? '需重启后端' : resources.embedding_state === 'ready' ? '具备加载条件，实际向量生成仍需测试' : ({ disabled: '未启用', unavailable: '依赖不可用', not_ready: '未准备好' } as const)[resources.embedding_state]}。{resources.embedding_detail}</p>
    </> : null}
    {!view.writable ? <p role="alert">设置库不可用，当前只读。</p> : null}
    <fieldset disabled={busy || !view.writable} className={styles.fields}>
      {FIELDS[group].filter(field => (!field.providers || field.providers.includes(String(fields.provider))) && (!field.when || field.when.some(([key, value]) => String(fields[key]) === value))).map(field => <label key={field.key} className={styles.field}>
        <span className={styles.fieldText}>
          <strong>{field.label}</strong>
          <small>{field.note}</small>
        </span>
        <span className={`${styles.control} ${field.type === 'text' ? styles.controlWide : ''}`}>
          {field.options ? <select aria-label={field.label} value={String(fields[field.key])} onChange={event => change(field.key, event.target.value)}>
            {field.options.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select> : typeof fields[field.key] === 'boolean' ? <span className={styles.switch}>
            <input type="checkbox" aria-label={field.label} checked={Boolean(fields[field.key])} onChange={event => change(field.key, event.target.checked)} />
            <span className={styles.switchTrack} aria-hidden="true" />
          </span> : field.type === 'text' ?
            <input type="text" required={field.required && Boolean(fields.enabled)} aria-label={field.label} value={String(fields[field.key] ?? '')} onChange={event => change(field.key, event.target.value)} /> :
            <input type="number" required={!field.unlimited} aria-label={field.label} placeholder={field.unlimited ? '不限' : undefined} min={field.min} max={field.max} step={field.step ?? 1} value={String(fields[field.key] ?? '')} onChange={event => change(field.key, event.target.value)} />}
        </span>
      </label>)}
    </fieldset>
    {/* 密钥不在偏好里（偏好会随 Checkpoint 走）：它有自己的接口，单独一块。 */}
    {group === 'web_access' ? <WebSearchKeyField /> : null}
    {group === 'attachments' ? <OssCredentialField /> : null}
    {group === 'embedding' ? <p className={styles.note}>测试使用当前分区草稿及其他分区的已保存值，不保存草稿、不写入记忆。本地模型可能下载权重；远端接口会发送一条固定测试文本并可能产生少量费用。</p> : null}
    <div className={styles.actionBar}>
      <div className={styles.feedback}>
        {error ? <p role="alert" className={styles.error}>{error} <button type="button" disabled={busy} onClick={() => { void query.refetch().then(result => { if (!result.isError) { setDraft(null); setError(null); setProbe(null) } }) }}>重新读取并放弃草稿</button></p> : null}
        {saved ? <p role="status" className={styles.saved}>{group === 'general' ? '已保存并生效' : (group === 'embedding' || group === 'medium_term') && resources?.restart_required ? '已保存，新缓存目录需重启后端生效' : '已保存，下一次新任务生效'}</p> : null}
        {probe && probe.version === (draft?.base.version ?? view.version) ? <p role="status" className={probe.result.state === 'passed' ? styles.saved : styles.error}>
          {probe.result.state === 'passed' ? '测试通过。' : '测试未通过。'}{probe.result.detail}{probe.result.dimension ? ` 向量维度：${probe.result.dimension}。` : ''}
        </p> : null}
      </div>
      <div className={styles.actions}>
        <button type="submit" className={styles.primary} disabled={!draft || busy || !view.writable}>{operation === 'save' ? '正在保存…' : '保存'}</button>
        <button type="button" disabled={!draft || busy} onClick={() => { setDraft(null); setError(null); setSaved(false); setProbe(null) }}>取消修改</button>
        <button type="button" disabled={busy || !view.writable} onClick={() => { setDraft({ base: view, fields: fieldsFor(view, group, true) }); setError(null); setSaved(false); setProbe(null) }}>恢复初始值</button>
        {group === 'embedding' ? <button type="submit" data-action="test" disabled={busy || !view.writable}>{operation === 'test' ? '正在测试…' : '测试当前配置'}</button> : null}
      </div>
    </div>
  </form>
}
