// 新增 / 编辑模型连接的表单（T29A；§6.8）。
//
// 分工：这里只做**受控输入 + 明显不合法就拦在本地**（少一次往返、少一条错误提示）；
// 规则的权威始终在服务端——`MODEL_SETTINGS_INVALID` 的理由会原样显示在表单下方。
//
// 三条硬约束：
// 1. **密钥永不回显**：编辑时输入框是空的，占位说明"不填表示不改密钥"；我们从不在 state 里放已存的密钥
//    （契约也不给），所以也无法回显。
// 2. **上下文窗口必填**：§6.8 要求由用户填写或由可信检测确认，不给默认值——猜一个窗口会让压缩触发比失真。
// 3. **非回环明文 HTTP 必须显式确认**：勾选框只在那种地址下出现，且不勾就提交不了（与服务端同一条规则）。

import { useState, type FormEvent } from 'react'

import type { ApiError, ModelConnectionDto, ModelConnectionInput } from '../../api/client'
import styles from './SettingsDialog.module.css'

interface ConnectionFormProps {
  /** 编辑时给当前值；新增时为 `null`。 */
  initial?: ModelConnectionDto | null
  busy: boolean
  /** 服务端返回的校验/冲突错误；原样展示其可执行提示。 */
  error: ApiError | null
  onSubmit: (input: ModelConnectionInput, apiKey: string | null) => void
  onCancel: () => void
}

/** 与服务端同一条回环判定：`localhost` / `127.*` / `[::1]` 的明文 HTTP 不算风险。 */
function isLoopbackHost(host: string): boolean {
  const text = host.trim().replace(/^\[|\]$/g, '').toLowerCase()
  return text === 'localhost' || text === '::1' || text === '127.0.0.1' || text.startsWith('127.')
}

/** 判断地址是否属于"非回环明文 HTTP"。解析失败时按"不是"处理，交给服务端报具体原因。 */
function needsPlaintextConsent(baseUrl: string): boolean {
  try {
    const parsed = new URL(baseUrl)
    return parsed.protocol === 'http:' && !isLoopbackHost(parsed.hostname)
  } catch {
    return false
  }
}

export function ConnectionForm({ initial, busy, error, onSubmit, onCancel }: ConnectionFormProps) {
  const editing = initial !== undefined && initial !== null
  const [displayName, setDisplayName] = useState(initial?.display_name ?? '')
  const [baseUrl, setBaseUrl] = useState(initial?.base_url ?? '')
  const [modelId, setModelId] = useState(initial?.model_id ?? '')
  const [contextWindow, setContextWindow] = useState(String(initial?.context_window_tokens ?? ''))
  const [maxTokens, setMaxTokens] = useState(String(initial?.max_tokens ?? 4096))
  const [temperature, setTemperature] = useState(String(initial?.temperature ?? 0.2))
  const [timeoutSeconds, setTimeoutSeconds] = useState(String(initial?.timeout_seconds ?? 120))
  const [retryCount, setRetryCount] = useState(String(initial?.retry_count ?? 2))
  const [streaming, setStreaming] = useState(initial?.streaming ?? false)
  // 逐连接声明"支持图片输入"（2026-10-16）：默认关（失败闭合）——勾错会让带图的请求每轮失败。
  const [supportsVision, setSupportsVision] = useState(initial?.supports_vision ?? false)
  const [authMode, setAuthMode] = useState<'api_key' | 'none'>(initial?.auth_mode ?? 'api_key')
  const [apiKey, setApiKey] = useState('')
  const [consent, setConsent] = useState(false)
  const [localError, setLocalError] = useState<string | null>(null)

  const plaintext = needsPlaintextConsent(baseUrl)

  function submit(event: FormEvent) {
    event.preventDefault()
    const window = Number(contextWindow)
    const output = Number(maxTokens)
    if (!displayName.trim()) return setLocalError('连接名称不能为空')
    if (!baseUrl.trim()) return setLocalError('端点地址不能为空')
    if (!modelId.trim()) return setLocalError('模型 ID 不能为空')
    if (!Number.isFinite(window) || window <= 0) {
      return setLocalError('上下文窗口必须填写正整数：请按所连模型的实际窗口填，不要猜')
    }
    if (!Number.isFinite(output) || output <= 0) return setLocalError('输出上限必须填写正整数')
    if (window <= output) {
      return setLocalError(`上下文窗口（${window}）必须大于输出上限（${output}）`)
    }
    if (plaintext && !consent) {
      return setLocalError('非本机回环的端点必须用 HTTPS；确认要明文传输请勾选下面的确认框')
    }

    setLocalError(null)
    onSubmit(
      {
        display_name: displayName.trim(),
        base_url: baseUrl.trim(),
        model: modelId.trim(),
        context_window_tokens: window,
        max_tokens: output,
        temperature: Number(temperature) || 0,
        timeout_seconds: Number(timeoutSeconds) || 120,
        retry_count: Number(retryCount) || 0,
        streaming,
        supports_vision: supportsVision,
        auth_mode: authMode,
        allow_plaintext_http: plaintext ? consent : false,
      },
      apiKey.trim() === '' ? null : apiKey,
    )
  }

  const message = localError ?? error?.message ?? null

  return (
    <form className={styles.form} aria-label={editing ? '编辑连接' : '新增连接'} onSubmit={submit}>
      <div className={styles.formGrid}>
        <label className={styles.field}>
          <span className={styles.label}>名称</span>
          <input
            className={styles.input}
            value={displayName}
            onChange={(event) => setDisplayName(event.target.value)}
            placeholder="例如：主力模型"
          />
        </label>

        <label className={styles.field}>
          <span className={styles.label}>端点（base_url）</span>
          <input
            className={styles.input}
            value={baseUrl}
            onChange={(event) => setBaseUrl(event.target.value)}
            placeholder="https://api.example.com/v1"
          />
        </label>

        <label className={styles.field}>
          <span className={styles.label}>模型 ID</span>
          <input
            className={styles.input}
            value={modelId}
            onChange={(event) => setModelId(event.target.value)}
            placeholder="供应商文档里的模型名"
          />
        </label>

        <label className={styles.field}>
          <span className={styles.label}>上下文窗口（token，必填）</span>
          <input
            className={styles.input}
            value={contextWindow}
            inputMode="numeric"
            onChange={(event) => setContextWindow(event.target.value)}
            placeholder="按模型文档填，不要猜"
          />
        </label>

        <label className={styles.field}>
          <span className={styles.label}>输出上限（max_tokens）</span>
          <input
            className={styles.input}
            value={maxTokens}
            inputMode="numeric"
            onChange={(event) => setMaxTokens(event.target.value)}
          />
        </label>

        <label className={styles.field}>
          <span className={styles.label}>温度</span>
          <input
            className={styles.input}
            value={temperature}
            inputMode="decimal"
            onChange={(event) => setTemperature(event.target.value)}
          />
        </label>

        <label className={styles.field}>
          <span className={styles.label}>超时（秒）</span>
          <input
            className={styles.input}
            value={timeoutSeconds}
            inputMode="numeric"
            onChange={(event) => setTimeoutSeconds(event.target.value)}
          />
        </label>

        <label className={styles.field}>
          <span className={styles.label}>重试次数</span>
          <input
            className={styles.input}
            value={retryCount}
            inputMode="numeric"
            onChange={(event) => setRetryCount(event.target.value)}
          />
        </label>

        <label className={styles.field}>
          <span className={styles.label}>鉴权</span>
          <select
            className={styles.input}
            value={authMode}
            onChange={(event) => setAuthMode(event.target.value === 'none' ? 'none' : 'api_key')}
          >
            <option value="api_key">需要 Key</option>
            <option value="none">本机端点，不需要 Key</option>
          </select>
        </label>

        <label className={styles.field}>
          <span className={styles.label}>密钥{editing ? '（留空表示不改）' : ''}</span>
          <input
            className={styles.input}
            type="password"
            autoComplete="off"
            value={apiKey}
            disabled={authMode === 'none'}
            onChange={(event) => setApiKey(event.target.value)}
            placeholder={editing ? '不填 = 保留已保存的密钥' : '只写入不回读'}
          />
        </label>
      </div>

      <label className={styles.checkboxRow}>
        <input
          type="checkbox"
          checked={streaming}
          onChange={(event) => setStreaming(event.target.checked)}
        />
        按增量流式展示最终回答（失败会退化为一次完整回答）
      </label>

      <label className={styles.checkboxRow}>
        <input
          type="checkbox"
          checked={supportsVision}
          onChange={(event) => setSupportsVision(event.target.checked)}
        />
        这条连接支持图片输入（勾了之后「看图片」工具才会交给模型；不确定就别勾——勾错会让带图的请求失败）
      </label>

      {plaintext ? (
        <label className={styles.checkboxRow}>
          <input
            type="checkbox"
            checked={consent}
            onChange={(event) => setConsent(event.target.checked)}
          />
          我确认这个局域网 HTTP 端点可以明文传输（请求内容在网络上不加密）
        </label>
      ) : null}

      {message === null ? null : (
        <p className={styles.formError} role="alert">
          {message}
        </p>
      )}
      {error?.hint ? <p className={styles.hint}>{error.hint}</p> : null}

      <div className={styles.formActions}>
        <button type="button" className={styles.ghost} disabled={busy} onClick={onCancel}>
          取消
        </button>
        <button type="submit" className={styles.primary} disabled={busy}>
          {busy ? '保存中…' : editing ? '保存修改' : '保存并设为默认'}
        </button>
      </div>
    </form>
  )
}
