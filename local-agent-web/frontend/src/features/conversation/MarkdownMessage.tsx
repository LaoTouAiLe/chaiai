// 助手回答的 Markdown 渲染（T30；计划实现要点「Markdown、代码块、复制使用设计 Token」）。
//
// 三条口径：
// 1. **不渲染原始 HTML**：`react-markdown` 默认就不解析 HTML，正是我们要的——模型输出是外部内容，
//    在无鉴权的本机页面里注入脚本的代价太高。这里**不**开 `rehype-raw`。
// 2. **代码块自带复制**：复制的是**代码原文**（从子节点里抽文本），不是渲染后的 DOM 文本。
// 3. 复制失败要如实说：`navigator.clipboard` 在非安全上下文可能不存在，那时按钮显示"复制失败"，
//    而不是假装成功（用户会去粘贴一个空内容，还以为是模型的问题）。

import { useState, type ComponentPropsWithoutRef, type ReactNode } from 'react'
import Markdown from 'react-markdown'

import { Copy } from '../../components/icons'
import { copyText } from './clipboard'
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

function CodeBlock({ children }: ComponentPropsWithoutRef<'pre'>) {
  return (
    <div className={styles.block}>
      <div className={styles.bar}>
        <CopyButton text={textOf(children)} label="复制代码" />
      </div>
      <pre className={styles.pre}>
        {children}
      </pre>
    </div>
  )
}

function InlineCode({ children, className }: ComponentPropsWithoutRef<'code'>) {
  return (
    <code className={className ?? styles.inline}>
      {children}
    </code>
  )
}

function Link({ children, href, title }: ComponentPropsWithoutRef<'a'>) {
  // 外链在新标签打开并摘掉 referrer：本页面是没有鉴权的本机页面，不该把地址泄露给目标站点。
  return (
    <a className={styles.link} href={href} title={title} target="_blank" rel="noreferrer noopener">
      {children}
    </a>
  )
}

/** 模型正文不可信：图片 URL 不得在用户未操作时触发浏览器请求。 */
function InertImage({ alt }: ComponentPropsWithoutRef<'img'>) {
  return <span className={styles.imagePlaceholder}>[图片未自动加载{alt ? `：${alt}` : ''}]</span>
}

export function MarkdownMessage({ text }: { text: string }) {
  return (
    // 自带一层容器：Markdown 的块级元素样式（外边距复位）只作用于自己的后代，
    // 不去猜外层组件的类名。
    <div className={styles.root}>
      <Markdown components={{ pre: CodeBlock, code: InlineCode, a: Link, img: InertImage }}>
        {text}
      </Markdown>
    </div>
  )
}
