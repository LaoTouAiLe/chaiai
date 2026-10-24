import { useEffect, useState } from 'react'
import { codeToHtml } from 'shiki'

/**
 * 代码高亮（2026-10-23）。
 *
 * `shiki` 早就在依赖里，但一直没接——对话里的代码块和文件预览都只是纯文本 `<pre>`。
 * 这里做成 hook：异步出 HTML，**认不出语言或加载失败就返回 null**，
 * 调用方退回纯文本渲染，不允许白屏。
 *
 * 为什么不用 CodeMirror（hermes 用的那套）：那是**编辑器**，我们只读，
 * 为了一段只读文本挂一个编辑器实例不划算。
 */
export function useHighlight(code: string, lang: string): string | null {
  const [html, setHtml] = useState<string | null>(null)

  useEffect(() => {
    if (lang === 'text' || code === '') {
      setHtml(null)
      return
    }
    let cancelled = false
    // 只高亮前 20000 字符：再长渲染会很慢，而预览本身也截断在这个量级。
    void codeToHtml(code.slice(0, 20_000), { lang, theme: 'github-light' })
      .then((result) => { if (!cancelled) setHtml(result) })
      .catch(() => { if (!cancelled) setHtml(null) })
    return () => { cancelled = true }
  }, [code, lang])

  return html
}

/** 按扩展名给 shiki 一个语言 id；认不出就 `text`（调用方据此跳过高亮）。 */
export function languageOf(path: string): string {
  const name = path.split('/').pop() ?? path
  const dot = name.lastIndexOf('.')
  if (dot === -1) return 'text'
  const ext = name.slice(dot + 1).toLowerCase()
  const map: Record<string, string> = {
    ts: 'typescript', tsx: 'tsx', js: 'javascript', jsx: 'jsx', py: 'python',
    rs: 'rust', go: 'go', java: 'java', c: 'c', h: 'c', cpp: 'cpp', cs: 'csharp',
    rb: 'ruby', php: 'php', css: 'css', scss: 'scss', html: 'html', vue: 'vue',
    json: 'json', yaml: 'yaml', yml: 'yaml', toml: 'toml', xml: 'xml',
    md: 'markdown', sh: 'shellscript', ps1: 'powershell', sql: 'sql',
  }
  return map[ext] ?? 'text'
}
