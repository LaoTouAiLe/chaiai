// 助手回答的 Markdown 渲染（T30；计划实现要点「Markdown、代码块、复制使用设计 Token」）。
//
// 三条口径：
// 1. **不渲染原始 HTML**：`react-markdown` 默认就不解析 HTML，正是我们要的——模型输出是外部内容，
//    在无鉴权的本机页面里注入脚本的代价太高。这里**不**开 `rehype-raw`。
// 2. **代码块自带复制**：复制的是**代码原文**（从子节点里抽文本），不是渲染后的 DOM 文本。
// 3. 复制失败要如实说：`navigator.clipboard` 在非安全上下文可能不存在，那时按钮显示"复制失败"，
//    而不是假装成功（用户会去粘贴一个空内容，还以为是模型的问题）。

import { createContext, memo, useContext, useId, useState, type ComponentPropsWithoutRef, type ReactNode } from 'react'
import Markdown, { defaultUrlTransform, type ExtraProps } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import remarkMath from 'remark-math'
import rehypeKatex from 'rehype-katex'
import 'katex/dist/katex.min.css'

import { AnswerCodeBlock } from './AnswerCodeBlock'
import { MarkdownTable } from './MarkdownTable'
import { MarkdownImage } from './MarkdownImage'
import { Copy } from '../../components/icons'
import { copyText } from './clipboard'
import { FileIcon } from '../panel/fileIcon'
import { FileLinkContext } from './fileLinkContext'
import { filePathFromHref } from './fileLinks'
import styles from './MarkdownMessage.module.css'

/** 把渲染出来的子节点还原成纯文本（代码块复制用）。 */
function textOf(node: ReactNode): string {
  if (node === null || node === undefined || typeof node === 'boolean') return ''
  if (typeof node === 'string' || typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(textOf).join('')
  if (typeof node === 'object' && 'props' in node) {
    return textOf((node.props as { children?: ReactNode }).children)
  }
  return ''
}


export function CopyButton({ text, label = '复制' }: { text: string; label?: string }) {
  const [state, setState] = useState<'idle' | 'copied' | 'failed'>('idle')

  return (
    <button
      type="button"
      className={styles.copy}
      aria-label={label}
      onClick={() => {
        void copyText(text).then((ok) => setState(ok ? 'copied' : 'failed'))
      }}
    >
      {state === 'idle' ? label : state === 'copied' ? '已复制' : '复制失败'}
    </button>
  )
}

/**
 * 只有图标的复制按钮：**沿用调用方的类名**，所以放到别处也不会改变像素。
 *
 * 反馈只走 `title` 与 `aria-label`，不改图标——图标变了就会动布局，而这条按钮在 54 张基线里。
 */
export function IconCopyButton({ text, className }: { text: string; className?: string }) {
  const [state, setState] = useState<'idle' | 'copied' | 'failed'>('idle')
  const label = state === 'idle' ? '复制' : state === 'copied' ? '已复制' : '复制失败'

  return (
    <button
      type="button"
      className={className}
      aria-label={label}
      title={label}
      onClick={() => {
        void copyText(text).then((ok) => setState(ok ? 'copied' : 'failed'))
      }}
    >
      <Copy />
    </button>
  )
}

/**
 * 代码块的语言名（2026-10-21）：`react-markdown` 把语言放在 `<code class="language-python">`
 * 上，而 `pre` 本身不带。取不到就是 `null`（页面显示 `Plain Text`）。
 */
function languageOf(node: ReactNode): string | null {
  if (node === null || node === undefined || typeof node === 'boolean') return null
  if (Array.isArray(node)) {
    for (const child of node) {
      const found = languageOf(child)
      if (found !== null) return found
    }
    return null
  }
  if (typeof node === 'object' && node !== null && 'props' in node) {
    const props = (node as { props?: { className?: string; children?: ReactNode } }).props
    const matched = /language-([\w+#.-]+)/.exec(props?.className ?? '')
    if (matched !== null) return matched[1] ?? null
    return languageOf(props?.children)
  }
  return null
}

const StreamingContext = createContext(false)

/** GFM's fixed label ID must also be scoped when several messages contain notes. */
function scopeFootnotes({ labelId }: { labelId: string }) {
  type Node = { properties?: Record<string, unknown>; children?: Node[] }
  return (tree: Node) => {
    const visit = (node: Node) => {
      if (node.properties?.id === 'footnote-label') node.properties.id = labelId
      const described = node.properties?.ariaDescribedBy
      if (Array.isArray(described) && node.properties) node.properties.ariaDescribedBy = described.map(value => value === 'footnote-label' ? labelId : value)
      node.children?.forEach(visit)
    }
    visit(tree)
  }
}

type MarkdownNode = { type: string; value?: string; alt?: string | null; meta?: string | null; children?: MarkdownNode[]; data?: { hProperties?: Record<string, unknown> }; position?: { start: { offset?: number }; end: { offset?: number } } }
function plainCell(node: MarkdownNode): string {
  return node.value ?? node.alt ?? node.children?.map(plainCell).join('') ?? ''
}
function tsvCell(value: string): string {
  return /[\t\n"]/.test(value) ? `"${value.replace(/"/g, '""')}"` : value
}
/** Preserve standard fenced-code info strings through the Markdown AST. */
function codeMetadata() {
  return (tree: MarkdownNode, file: { value: unknown }) => {
    const visit = (node: MarkdownNode) => {
      if (node.type === 'table') {
        node.data ??= {}
        node.data.hProperties = { ...node.data.hProperties,
          'data-table-columns': node.children?.[0]?.children?.length ?? 0,
          'data-table-tsv': node.children?.map(row => row.children?.map(cell => tsvCell(plainCell(cell))).join('\t')).join('\n') ?? '',
        }
      }
      if (node.type === 'code') {
        node.data ??= {}
        const raw = String(file.value).slice(node.position?.start.offset ?? 0, node.position?.end.offset)
        const lines = raw.trimEnd().split('\n')
        const fence = /`{3,}|~{3,}/.exec(lines[0] ?? '')?.[0]
        const closing = /^\s*(`+|~+)\s*$/.exec(lines.at(-1) ?? '')?.[1]
        const open = !!fence && !(lines.length > 1 && closing && closing[0] === fence[0] && closing.length >= fence.length)
        node.data.hProperties = { ...node.data.hProperties, 'data-code-meta': node.meta ?? '', 'data-code-open': String(open) }
      }
      node.children?.forEach(visit)
    }
    visit(tree)
  }
}
function codeProperty(node: ReactNode, key: string): string {
  if (Array.isArray(node)) return node.map(child => codeProperty(child, key)).find(Boolean) ?? ''
  if (node && typeof node === 'object' && 'props' in node) {
    const props = node.props as { node?: { properties?: Record<string, unknown> }; children?: ReactNode }
    return String(props.node?.properties?.[key] ?? '') || codeProperty(props.children, key)
  }
  return ''
}
function CodeBlock({ children }: ComponentPropsWithoutRef<'pre'>) {
  const streaming = useContext(StreamingContext)
  return <AnswerCodeBlock code={textOf(children)} language={languageOf(children)} meta={codeProperty(children, 'data-code-meta')} streaming={streaming && codeProperty(children, 'data-code-open') === 'true'} />
}

function InlineCode({ children, className }: ComponentPropsWithoutRef<'code'>) {
  const open = useContext(FileLinkContext)
  const text = textOf(children)
  const file = !className && !/\s/.test(text) && /(?:^|[\\/])[\w.@-]+\.[a-zA-Z\d]{1,10}(?::\d+(?::\d+)?)?$/.test(text)
    ? filePathFromHref(text) : null
  if (file && open) return <button type="button" className={`${styles.inline} ${styles.fileMention}`} title={`在右侧打开：${file}`} onClick={() => open(file)}><FileIcon path={file} /><span>{children}</span></button>
  return (
    <code className={className ?? styles.inline}>
      {children}
    </code>
  )
}

function Link({ children, href, title, id, node, 'aria-describedby': describedBy, 'aria-label': label }: ComponentPropsWithoutRef<'a'> & ExtraProps) {
  const openFile = useContext(FileLinkContext)
  const path = href ? filePathFromHref(href) : null
  if (path !== null) {
    if (!openFile) return <span title={title ?? path}>{children}</span>
    return <a className={`${styles.link} ${styles.fileLink}`} href={href} title={title ?? `在右侧打开：${path}`}
      onClick={event => { event.preventDefault(); openFile(path) }}
      onAuxClick={event => { if (event.button === 1) { event.preventDefault(); openFile(path) } }}>
      <FileIcon path={path} /><span>{children}</span>
    </a>
  }
  if (!href) return <span>{children}</span>
  if (href.startsWith('#')) return <a className={styles.link} href={href} title={title} id={id}
    aria-describedby={describedBy} aria-label={label}
    data-footnote-ref={node?.properties.dataFootnoteRef ? '' : undefined}
    data-footnote-backref={node?.properties.dataFootnoteBackref ? '' : undefined}>{children}</a>
  // 外链在新标签打开并摘掉 referrer：本页面是没有鉴权的本机页面，不该把地址泄露给目标站点。
  return (
    <a className={styles.link} href={href} title={title} target="_blank" rel="noreferrer noopener">
      {children}
    </a>
  )
}

export const MarkdownMessage = memo(function MarkdownMessage({ text, compact = false, streaming = false }: { text: string; compact?: boolean; streaming?: boolean }) {
  const identity = useId()
  return (
    // 自带一层容器：Markdown 的块级元素样式（外边距复位）只作用于自己的后代，
    // 不去猜外层组件的类名。
    <StreamingContext.Provider value={streaming}><div data-markdown-message className={compact ? `${styles.root} ${styles.compact}` : styles.root}>
      {/* GFM 扩展：表格、删除线、任务列表。`react-markdown` 默认只解析 CommonMark，缺了它模型
          输出的 Markdown 表格会退化成一行带竖线的普通文本（实测：表格是本项目架构回答的主要
          呈现形式）。`remark-gfm` 只增加语法解析，不改变上面第 1 条"不解析原始 HTML"的口径。 */}
      <Markdown
        remarkPlugins={[remarkGfm, remarkMath, codeMetadata]}
        rehypePlugins={[[scopeFootnotes, { labelId: `answer-${identity}-notes` }], [rehypeKatex, { trust: false, strict: 'ignore', maxExpand: 1000, maxSize: 20 }]]}
        remarkRehypeOptions={{ clobberPrefix: `answer-${identity}-`, footnoteLabel: '注释', footnoteBackLabel: '返回正文' }}
        urlTransform={(url, key) => (key === 'href' || key === 'src') && filePathFromHref(url) !== null ? url : defaultUrlTransform(url)}
        components={{ pre: CodeBlock, code: InlineCode, a: Link, img: MarkdownImage, table: MarkdownTable }}
      >
        {text}
      </Markdown>
    </div></StreamingContext.Provider>
  )
})
