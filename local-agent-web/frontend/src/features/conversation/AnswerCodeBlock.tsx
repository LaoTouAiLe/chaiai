import { useContext, useEffect, useRef, useState, type CSSProperties, type MouseEvent } from 'react'
import type { BundledLanguage, ThemedToken } from 'shiki'
import { copyText } from './clipboard'
import { FileLinkContext } from './fileLinkContext'
import { filePathFromHref } from './fileLinks'
import styles from './AnswerCodeBlock.module.css'

const aliases: Record<string, string> = { ts: 'typescript', js: 'javascript', py: 'python', sh: 'shellscript', bash: 'shellscript', yml: 'yaml', ps1: 'powershell' }

/** Fence metadata is presentation only; it never executes or writes the displayed code. */
function fenceDetails(meta: string, count: number) {
  const name = /(?:title|filename|file)=(?:"([^"]+)"|'([^']+)'|([^\s{}]+))/.exec(meta)
  const path = name ? filePathFromHref(name[1] ?? name[2] ?? name[3]) : null
  const marked = new Set<number>()
  const range = /\{([\d,\s-]+)\}/.exec(meta)?.[1] ?? ''
  for (const part of range.split(',')) {
    const pair = /^(\d+)(?:-(\d+))?$/.exec(part.trim())
    if (!pair) continue
    const first = Math.max(1, Number(pair[1])), last = Math.min(count, Number(pair[2] ?? pair[1]))
    for (let line = first; line <= last; line++) marked.add(line)
  }
  return { path, marked }
}

export function AnswerCodeBlock({ code, language, meta = '', streaming = false }: {
  code: string; language: string | null; meta?: string; streaming?: boolean
}) {
  const root = useRef<HTMLDivElement>(null)
  const openFile = useContext(FileLinkContext)
  const [visible, setVisible] = useState(typeof IntersectionObserver === 'undefined')
  const [tokens, setTokens] = useState<{ code: string; language: string; rows: ThemedToken[][] } | null>(null)
  const [wrap, setWrap] = useState(false)
  const [numbered, setNumbered] = useState(false)
  const [selection, setSelection] = useState<[number, number] | null>(null)
  const [copied, setCopied] = useState<{ target: 'all' | 'selection'; ok: boolean } | null>(null)
  const lang = aliases[language?.toLowerCase() ?? ''] ?? language?.toLowerCase() ?? 'text'
  const source = code.endsWith('\n') ? code.slice(0, -1) : code
  const lines = source.split('\n')
  const { path, marked } = fenceDetails(meta, lines.length)
  const highlighted = tokens?.code === source && tokens.language === lang ? tokens.rows : null
  const selected = selection ? [Math.min(...selection), Math.max(...selection)] : null
  useEffect(() => {
    const element = root.current
    if (!element || typeof IntersectionObserver === 'undefined') return
    const observer = new IntersectionObserver(entries => setVisible(entries.some(entry => entry.isIntersecting)), { rootMargin: '400px' })
    observer.observe(element)
    return () => observer.disconnect()
  }, [])
  useEffect(() => {
    if (!visible || !source || lang === 'text' || source.length > 60_000) return
    let active = true
    // Coalesce token arrivals; source text stays visible immediately, including unfinished fences.
    const timer = setTimeout(() => {
      void import('shiki').then(({ codeToTokens, bundledLanguages }) => {
        if (!(lang in bundledLanguages)) return null
        return codeToTokens(source, { lang: lang as BundledLanguage, themes: { light: 'github-light', dark: 'github-dark' } })
      }).then(result => { if (active && result) setTokens({ code: source, language: lang, rows: result.tokens }) })
        .catch(() => { /* Unsupported languages retain their complete plain source. */ })
    }, streaming ? 100 : 0)
    return () => { active = false; clearTimeout(timer) }
  }, [source, lang, visible, streaming])
  const copy = (text: string, target: 'all' | 'selection') => { void copyText(text).then(ok => setCopied({ target, ok })) }
  const chooseLine = (line: number, event: MouseEvent) => {
    setSelection(previous => event.shiftKey && previous ? [previous[0], line] : previous?.[0] === line && previous[1] === line ? null : [line, line])
  }
  return <div ref={root} className={styles.block} data-code-block data-streaming={streaming || undefined}>
    <div className={styles.bar}>
      <span className={styles.language}>{language ?? 'Plain Text'}</span>
      {path ? <button className={styles.filename} disabled={!openFile} title={path} onClick={() => openFile?.(path)}>{path}</button> : null}
      <div className={styles.actions}>
        <button aria-label={numbered ? '隐藏行号' : '显示行号'} aria-pressed={numbered} onClick={() => setNumbered(value => !value)}>行号</button>
        <button aria-label={wrap ? '关闭代码自动换行' : '开启代码自动换行'} aria-pressed={wrap} onClick={() => setWrap(value => !value)}>换行</button>
        <button aria-label="复制代码" onClick={() => copy(code, 'all')}>{copied?.target === 'all' ? copied.ok ? '已复制' : '复制失败' : '复制'}</button>
      </div>
    </div>
    {selected ? <div className={styles.selection} role="status">
      <span>已标记第 {selected[0]}{selected[0] === selected[1] ? '' : `–${selected[1]}`} 行</span>
      <button aria-label="复制标记行" onClick={() => copy(lines.slice(selected[0] - 1, selected[1]).join('\n'), 'selection')}>{copied?.target === 'selection' ? copied.ok ? '已复制标记行' : '复制失败' : '复制标记行'}</button>
      <button onClick={() => setSelection(null)}>清除标记</button>
    </div> : null}
    <pre className={styles.pre} data-wrap={wrap} data-numbered={numbered} data-highlighted={highlighted !== null} tabIndex={0} aria-label={`${language ?? '纯文本'}代码`}>
      <code>{lines.map((line, index) => <span key={index} className={styles.line} data-code-line={index + 1}
        data-marked={marked.has(index + 1) || (selected !== null && index + 1 >= selected[0] && index + 1 <= selected[1]) || undefined}>
        {numbered ? <button className={styles.number} aria-label={`标记第 ${index + 1} 行`} aria-pressed={selected !== null && index + 1 >= selected[0] && index + 1 <= selected[1]}
          title="点击标记；Shift 点击选择连续行" onClick={event => chooseLine(index + 1, event)}>{index + 1}</button> : null}
        <span className={styles.source}>{highlighted?.[index] ? highlighted[index].map((token, tokenIndex) => <span key={tokenIndex} style={(token.htmlStyle ?? { color: token.color }) as CSSProperties}>{token.content}</span>) : line || '\u200b'}</span>
      </span>)}</code>
    </pre>
    {streaming ? <span className={styles.live} role="status">代码正在生成</span> : null}
  </div>
}
