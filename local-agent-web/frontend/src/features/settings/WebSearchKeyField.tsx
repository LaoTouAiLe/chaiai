// 联网检索的服务凭据（Tavily / DeepSeek）录入块：「设置 → 联网检索」里的密钥那一格。
//
// 三条口径来自后端契约，页面不自己改口径：
//   1. **只写不回读**：接口从不返回密钥（连掩码都没有），所以这里只报"已配置 / 未配置"；
//   2. **两个动作分开**：覆盖写入走"保存密钥"，清空走"删除已保存密钥"——保存空白不是删除；
//   3. **来源要如实说**：凭据可能来自设置库，也可能来自升级前的环境变量；`source` 决定措辞，
//      删掉设置库里的那一条不等于"没有凭据了"。
//
// 2026-10-26：两个 API 后端各占一条密钥。`provider` 由「检索方式」当前选中的值决定——
// 只有显式选 `deepseek` 才显示 DeepSeek 那格，其余一律是 Tavily 那格（保持本功能上线时的行为）。

import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { clearWebSearchKey, getWebSearchKey, setWebSearchKey } from '../../api/client'
import type { WebSearchKeyDto, WebSearchProvider } from '../../api/client'
import styles from './PreferenceSection.module.css'

/** 查询键按后端分开：两个后端的"已配置 / 未配置"是两条独立事实，共用一个键会互相覆盖。 */
export const webSearchKeyQueryKey = (provider: WebSearchProvider) =>
  ['web-search-credential', provider] as const

/** 后端 → 页面上的名字与录入提示。 */
const PROVIDER_TEXT: Record<WebSearchProvider, { name: string; hint: string }> = {
  tavily: {
    name: 'Tavily',
    hint: '使用 Tavily 需要在这里录入密钥；不录入时按上面的「检索方式」选后端（网页版检索不需要密钥）。',
  },
  deepseek: {
    name: 'DeepSeek 搜索',
    hint: 'DeepSeek 搜索按次计费（每 1000 次成功请求 5 元，失败与无结果不计费）。密钥与模型连接是分开的两条，这里录入不会改动模型那边。',
  },
}

/** `store` 的取值 → 页面说明；`local` 与 `os` 都是"重启后仍在"。 */
const STORE_LABEL: Record<WebSearchKeyDto['store'], string> = {
  local: '已存入本地设置库（重启后仍在）',
  os: '已存入系统凭据存储',
  memory: '只在本进程内有效',
  unavailable: '没有可用的凭据存储',
}

function stateText(view: WebSearchKeyDto): string {
  if (!view.configured) return '未配置'
  return view.source === 'settings' ? '已配置（设置在本地设置库）' : '已配置（来自环境变量）'
}

export function WebSearchKeyField({ provider = 'tavily' }: { provider?: WebSearchProvider }) {
  const queryClient = useQueryClient()
  const queryKey = webSearchKeyQueryKey(provider)
  const text = PROVIDER_TEXT[provider]
  const query = useQuery({
    queryKey,
    queryFn: () => getWebSearchKey(provider),
    staleTime: Infinity,
    refetchOnMount: 'always',
  })
  const [draft, setDraft] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)
  // 已配置时先显示一个"已配置"的只读格：接口不给密钥、连掩码都不给，
  // 所以这格只表示"这里有一条"，点它才转成输入态（2026-10-23 照 hermes 的凭据行）。
  const [editing, setEditing] = useState(false)
  const update = (view: WebSearchKeyDto) => {
    queryClient.setQueryData(queryKey, view)
    setDraft('')
    setEditing(false)
    setError(null)
    setSaved(true)
  }
  const save = useMutation({
    mutationFn: (apiKey: string) => setWebSearchKey(apiKey, provider),
    onSuccess: update,
    onError: (caught: unknown) => {
      setSaved(false)
      setError(caught instanceof Error ? caught.message : '密钥保存失败')
    },
  })
  const clear = useMutation({
    mutationFn: () => clearWebSearchKey(provider),
    onSuccess: update,
    onError: (caught: unknown) => {
      setSaved(false)
      setError(caught instanceof Error ? caught.message : '密钥删除失败')
    },
  })
  const view = query.data
  const busy = save.isPending || clear.isPending
  const masked = view !== undefined && view.configured && !editing
  return (
    <section className={styles.section} aria-label="检索服务凭据">
      <h4>检索服务凭据（{text.name}）</h4>
      <p className={styles.note}>
        密钥只写入不回读；页面只能告诉你"有没有"，不会显示密钥本身。
        {text.hint}
      </p>
      {!view ? (
        <p role="status">
          {query.isError ? '凭据状态读取失败。' : '正在读取凭据状态…'}
          <button type="button" onClick={() => void query.refetch()}>重新读取</button>
        </p>
      ) : (
        <>
          <dl className={styles.overview} aria-label="凭据状态">
            <div><dt>当前状态</dt><dd>{stateText(view)}</dd></div>
            <div><dt>存放位置</dt><dd>{STORE_LABEL[view.store]}</dd></div>
          </dl>
          {view.note === null ? null : <p role="alert" className={styles.error}>{view.note}</p>}
          {view.configured && view.source === 'environment' ? (
            <p className={styles.note}>
              当前凭据来自环境变量（升级前的配置方式）：这里删除不会影响它。要改用设置库里的密钥，
              直接在下面录入即可覆盖。
            </p>
          ) : null}
          {masked ? (
            <span className={styles.field}>
              <span className={styles.fieldText}>
                <strong>密钥</strong>
                <small>已配置，只写入不回读。点右边这一格可以覆盖成新密钥。</small>
              </span>
              <span className={styles.control}>
                <input
                  type="text"
                  readOnly
                  aria-label={`${text.name} API Key（已配置，点击修改）`}
                  className={styles.maskedInput}
                  value="••••••••"
                  onClick={() => setEditing(true)}
                  onFocus={() => setEditing(true)}
                />
              </span>
            </span>
          ) : (
            <label className={styles.field}>
              <span className={styles.fieldText}>
                <strong>录入 / 覆盖密钥</strong>
                <small>粘贴 {text.name} 的 API Key；保存后从下一次检索开始使用，已开始的一轮不受影响。</small>
              </span>
              <span className={styles.control}>
                <input
                  type="password"
                  aria-label={`${text.name} API Key`}
                  autoComplete="off"
                  disabled={busy}
                  value={draft}
                  onChange={event => { setDraft(event.target.value); setSaved(false) }}
                />
              </span>
            </label>
          )}
          <div className={styles.actions}>
            {masked ? null : (
              <button
                type="button"
                className={styles.primary}
                disabled={busy || draft.trim().length === 0}
                onClick={() => save.mutate(draft)}
              >
                {save.isPending ? '正在保存…' : '保存密钥'}
              </button>
            )}
            <button
              type="button"
              // 只删设置库里的那一条：来源是环境变量时按下去什么也不会变，那就不要给这个按钮。
              disabled={busy || view.source !== 'settings'}
              onClick={() => clear.mutate()}
            >
              {clear.isPending ? '正在删除…' : '删除已保存密钥'}
            </button>
          </div>
          {error ? <p role="alert" className={styles.error}>{error}</p> : null}
          {saved ? <p role="status" className={styles.saved}>已保存，下一次检索生效</p> : null}
        </>
      )}
    </section>
  )
}
