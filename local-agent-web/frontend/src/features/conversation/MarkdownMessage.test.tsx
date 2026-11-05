import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { IconCopyButton, MarkdownMessage } from './MarkdownMessage'
import { copyText } from './clipboard'
import { FileLinkContext } from './fileLinkContext'

const originalClipboard = Object.getOwnPropertyDescriptor(navigator, 'clipboard')

function setClipboard(writeText: (text: string) => Promise<void>): void {
  Object.defineProperty(navigator, 'clipboard', {
    configurable: true,
    value: { writeText },
  })
}

afterEach(() => {
  if (originalClipboard === undefined) {
    Reflect.deleteProperty(navigator, 'clipboard')
  } else {
    Object.defineProperty(navigator, 'clipboard', originalClipboard)
  }
})

describe('MarkdownMessage', () => {
  it('copies semantic table cells as TSV, preserving column alignment and source text', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    setClipboard(writeText)
    render(<MarkdownMessage text={'| 模块 | 状态 |\n| :--- | ---: |\n| **正文** | `ready` |'} />)
    expect(screen.getByRole('columnheader', { name: '状态' })).toHaveStyle({ textAlign: 'right' })
    expect(screen.getByRole('region', { name: '表格内容，可横向滚动' })).toHaveAttribute('tabindex', '0')
    fireEvent.click(screen.getByRole('button', { name: '复制表格' }))
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('模块\t状态\n正文\tready'))
  })

  it('renders formulas without allowing resource commands and tolerates incomplete math', () => {
    const { container, rerender } = render(<MarkdownMessage text={'行内 $a^2+b^2=c^2$。\n\n$$\n\\frac{1}{2}\\sum_{i=1}^n x_i\n$$\n\n$$\\includegraphics{https://example.org/track.png}$$'} />)
    expect(container.querySelectorAll('.katex').length).toBeGreaterThanOrEqual(2)
    expect(container.querySelector('.katex-display')).not.toBeNull()
    expect(container.querySelector('img')).toBeNull()
    expect(container.querySelector('script')).toBeNull()
    rerender(<MarkdownMessage text={'未完成 $\\frac{1'} streaming />)
    expect(container).toHaveTextContent('未完成')
  })

  it('keeps footnote IDs unique across messages and uses in-page reference links', () => {
    const text = '正文[^1]。\n\n[^1]: 说明内容。'
    const { container } = render(<><MarkdownMessage text={text} /><MarkdownMessage text={text} /></>)
    const refs = container.querySelectorAll<HTMLAnchorElement>('a[data-footnote-ref]')
    expect(refs).toHaveLength(2)
    expect(refs[0]!.href).not.toBe(refs[1]!.href)
    for (const reference of refs) {
      expect(reference).not.toHaveAttribute('target')
      expect(container.querySelector(`[id="${reference.getAttribute('href')!.slice(1)}"]`)).not.toBeNull()
    }
  })

  it('loads remote images only on demand, allows retry and returns focus after zoom', () => {
    render(<MarkdownMessage text={'![架构图](https://example.org/architecture.png)'} />)
    expect(screen.queryByRole('img')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '加载图片' }))
    const image = screen.getByRole('img', { name: '架构图' })
    expect(image).toHaveAttribute('src', 'https://example.org/architecture.png')
    expect(image).toHaveAttribute('referrerpolicy', 'no-referrer')
    const zoom = screen.getByRole('button', { name: '放大图片：架构图' })
    zoom.focus()
    fireEvent.click(zoom)
    fireEvent.keyDown(screen.getByRole('dialog', { name: '放大查看图片' }), { key: 'Escape' })
    expect(zoom).toHaveFocus()
    fireEvent.error(image)
    expect(screen.getByText('图片加载失败：架构图')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '重试加载图片' }))
    expect(screen.getByRole('img', { name: '架构图' })).toBeInTheDocument()
  })

  it('opens local Markdown images through the file panel and leaves unsafe destinations inert', () => {
    const open = vi.fn()
    const { rerender } = render(<FileLinkContext.Provider value={open}><MarkdownMessage text={'![本地结果](E:/project/plot.png)'} /></FileLinkContext.Provider>)
    fireEvent.click(screen.getByRole('button', { name: '查看图片' }))
    expect(open).toHaveBeenCalledWith('E:/project/plot.png')
    expect(screen.queryByRole('img')).not.toBeInTheDocument()
    rerender(<MarkdownMessage text={'![危险图片](javascript:alert%281%29)\n\n![错误地址](http://)'} />)
    expect(screen.queryByRole('img')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /加载图片|查看图片/ })).not.toBeInTheDocument()
  })

  it('does not carry loading failures or consent to a different streamed image destination', () => {
    const { rerender } = render(<MarkdownMessage text={'![结果](https://example.org/first.png)'} />)
    fireEvent.click(screen.getByRole('button', { name: '加载图片' }))
    fireEvent.error(screen.getByRole('img', { name: '结果' }))
    rerender(<MarkdownMessage text={'![结果](https://example.org/second.png)'} />)
    expect(screen.queryByRole('img')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '加载图片' })).toBeInTheDocument()
    expect(screen.queryByText('图片加载失败：结果')).not.toBeInTheDocument()
  })

  it('renders local file links with icons and opens them through the panel action without navigation', () => {
    const open = vi.fn()
    render(<FileLinkContext.Provider value={open}>
      <MarkdownMessage text={'**改动文件**：[浏览器界面](E:/repo/BrowserPane.tsx)、[后端服务](<file:///E:/My%20Project/browsers.py>)、[相对路径](./src/app.ts)、[反斜杠](<E:\\repo\\app.py>)'} />
    </FileLinkContext.Provider>)
    const link = screen.getByRole('link', { name: '浏览器界面' })
    expect(link.querySelector('svg')).not.toBeNull()
    expect(link).not.toHaveAttribute('target')
    expect(fireEvent.click(link)).toBe(false)
    expect(open).toHaveBeenLastCalledWith('E:/repo/BrowserPane.tsx')
    fireEvent.click(screen.getByRole('link', { name: '后端服务' }))
    expect(open).toHaveBeenLastCalledWith('E:/My Project/browsers.py')
    fireEvent.click(screen.getByRole('link', { name: '相对路径' }))
    expect(open).toHaveBeenLastCalledWith('src/app.ts')
    fireEvent.click(screen.getByRole('link', { name: '反斜杠' }))
    expect(open).toHaveBeenLastCalledWith('E:/repo/app.py')
  })

  it('keeps web links external and blocks script schemes even with a file action available', () => {
    const open = vi.fn()
    render(<FileLinkContext.Provider value={open}>
      <MarkdownMessage text={'[站点](https://example.org/a.py) [恶意链接](javascript:alert%281%29) `README.md`'} />
    </FileLinkContext.Provider>)
    expect(screen.getByRole('link', { name: '站点' })).toHaveAttribute('target', '_blank')
    expect(screen.queryByRole('link', { name: '恶意链接' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'README.md' })).toBeInTheDocument()
    expect(open).not.toHaveBeenCalled()
  })

  it('labels a fenced block with its language, falling back to Plain Text', () => {
    // 语言名只做展示（2026-10-21）：一眼看出"这是 python 还是 bash"。
    const first = render(<MarkdownMessage text={'```python\nprint(1)\n```'} />)
    expect(screen.getByText('python')).toBeInTheDocument()
    first.unmount()

    render(<MarkdownMessage text={'```\nhello\n```'} />)
    expect(screen.getByText('Plain Text')).toBeInTheDocument()
  })

  it('renders emphasis, lists and code, and copies the code source', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    setClipboard(writeText)

    render(<MarkdownMessage text={'**重要**\n\n- 第一项\n\n```ts\nconst answer = 42\n```'} />)

    expect(screen.getByText('重要').tagName).toBe('STRONG')
    expect(screen.getByText('第一项').closest('li')).not.toBeNull()
    fireEvent.click(screen.getByRole('button', { name: '复制代码' }))
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('const answer = 42\n'))
    expect(screen.getByRole('button', { name: '复制代码' })).toHaveTextContent('已复制')
  })

  it('renders a GFM table as a real table instead of raw pipes', () => {
    // 2026-09-30：`react-markdown` 默认只解析 CommonMark，缺 `remark-gfm` 时表格会退化成一行带
    // 竖线的普通文本（实测：表格是本项目架构类回答的主要呈现形式）。
    const { container } = render(
      <MarkdownMessage text={'| 模块 | 职责 |\n|---|---|\n| `api/` | 传输层 |'} />,
    )

    expect(container.querySelector('table')).not.toBeNull()
    expect(screen.getByRole('columnheader', { name: '模块' })).toBeInTheDocument()
    expect(screen.getByRole('cell', { name: '传输层' })).toBeInTheDocument()
    // 没解析时整行会原样作为文本出现：反过来钉住"没有竖线残留"
    expect(container.textContent ?? '').not.toContain('|---|')
  })

  it('reports a failed copy instead of claiming success', async () => {
    setClipboard(vi.fn().mockRejectedValue(new Error('clipboard unavailable')))
    render(<MarkdownMessage text={'```\nsecret-free sample\n```'} />)

    fireEvent.click(screen.getByRole('button', { name: '复制代码' }))
    await waitFor(() => {
      expect(screen.getByRole('button', { name: '复制代码' })).toHaveTextContent('复制失败')
    })
  })

  it('reports failure when the clipboard API is missing', async () => {
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: undefined })
    expect(await copyText('sample')).toBe(false)
  })

  it('copies the assistant message without changing the icon button', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    setClipboard(writeText)
    render(<IconCopyButton text={'**原始 Markdown**'} />)

    fireEvent.click(screen.getByRole('button', { name: '复制' }))
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('**原始 Markdown**'))
    expect(screen.getByRole('button', { name: '已复制' })).toHaveAttribute('title', '已复制')
  })

  it('keeps raw HTML inert and never requests a Markdown image automatically', () => {
    const { container } = render(
      <MarkdownMessage
        text={'<script>alert(1)</script>\n\n![追踪像素](https://example.invalid/pixel?secret=value)\n\n[外链](https://example.invalid/)'}
      />,
    )

    expect(container.querySelector('script')).toBeNull()
    expect(container.querySelector('img')).toBeNull()
    expect(screen.getByText('[图片未自动加载：追踪像素]')).toBeInTheDocument()
    const link = screen.getByRole('link', { name: '外链' })
    expect(link).toHaveAttribute('rel', 'noreferrer noopener')
    expect(link).not.toHaveAttribute('node')
  })
})
