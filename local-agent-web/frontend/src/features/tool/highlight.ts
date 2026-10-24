/**
 * diff 的语法高亮（2026-10-22）。
 *
 * 用 shiki 而不是自研词法：hermes 的 diff 也是 shiki（`diff-lines.tsx` 动态 import），
 * 主题取 `github-light` / `github-dark`，与我们的 diff 配色同族。
 *
 * 三条约束：
 * 1. **按需加载**：高亮器是动态 import，首屏不为它付体积；
 * 2. **失败降级**：任何异常都返回 null，调用方继续渲染纯文本；
 * 3. **按行返回**：diff 需要逐行结构，所以用 `codeToTokensBase`，不用 `codeToHtml`。
 */
import type { BundledLanguage, Highlighter } from 'shiki'

const THEMES = ['github-light', 'github-dark'] as const

const LANG_BY_EXTENSION: Record<string, string> = {
  ts: 'typescript', tsx: 'tsx', js: 'javascript', jsx: 'jsx', mjs: 'javascript', cjs: 'javascript',
  css: 'css', scss: 'scss', html: 'html', htm: 'html', json: 'json', jsonc: 'jsonc',
  py: 'python', md: 'markdown', yaml: 'yaml', yml: 'yaml', sh: 'shellscript', bash: 'shellscript',
  toml: 'toml', sql: 'sql', rs: 'rust', go: 'go', java: 'java', c: 'c', h: 'c', cpp: 'cpp', hpp: 'cpp',
}

const LOADED_LANGS = [
  'typescript', 'tsx', 'javascript', 'jsx', 'css', 'scss', 'html', 'json', 'jsonc',
  'python', 'markdown', 'yaml', 'shellscript', 'toml', 'sql', 'rust', 'go', 'java', 'c', 'cpp',
] as const

let highlighter: Promise<Highlighter> | null = null

function loadHighlighter(): Promise<Highlighter> {
  highlighter ??= import('shiki').then(({ createHighlighter }) => createHighlighter({
    themes: [...THEMES],
    langs: [...LOADED_LANGS],
  }))
  return highlighter
}

/** 按扩展名找 shiki 语言；认不出就返回 null（调用方退化为纯文本）。 */
export function languageOf(path: string): string | null {
  const extension = path.split('.').pop()?.toLowerCase() ?? ''
  return LANG_BY_EXTENSION[extension] ?? null
}

function escapeHtml(text: string): string {
  return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
}

/**
 * 把整段代码高亮成「每行一组 `<span>`」。
 *
 * 返回数组与输入行一一对应（空行返回空数组），调用方按行下标取即可；
 * 语言不支持、加载失败、或宿主不是浏览器时返回 null。
 */
export async function highlightLines(
  code: string, lang: string, dark: boolean,
): Promise<string[][] | null> {
  try {
    const instance = await loadHighlighter()
    if (!instance.getLoadedLanguages().includes(lang)) return null
    const rows = instance.codeToTokensBase(code, {
      // 认不出的语言在上面已被拦掉；这里的 cast 只是把 `string` 收窄成 shiki 的联合类型。
      lang: lang as BundledLanguage,
      theme: dark ? 'github-dark' : 'github-light',
    })
    return rows.map((tokens) => tokens.map((token) => (
      `<span style="color:${token.color ?? 'inherit'}">${escapeHtml(token.content)}</span>`
    )))
  } catch {
    return null
  }
}

/** 当前主题是否深色（`PreferencesProvider` 把结果写在 `data-theme` 上）。 */
export function isDarkTheme(): boolean {
  return document.documentElement.dataset.theme === 'dark'
}
