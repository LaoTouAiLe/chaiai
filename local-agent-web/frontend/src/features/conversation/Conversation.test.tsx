import type { ReactElement, ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render as baseRender, screen, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import {
  emptySessionScenario,
  errorsScenario,
  loadingHistoryScenario,
  memoryHitScenario,
  scenarios,
  shellScenario,
  toolsScenario,
} from '../../fixtures/scenarios'
import { Conversation } from './Conversation'

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

/**
 * 会话里可能渲染委派卡，而它会轮询实时进度（2026-11-04）。
 * 覆盖 `render` 统一提供 QueryClient，而不是逐个用例改。
 */
function render(ui: ReactElement) {
  return baseRender(ui, {
    wrapper: ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    ),
  })
}

/** Mirrors the component's display rule: `k` only above one thousand. */
function formatTokens(value: number): string {
  return value >= 1_000 ? `${(value / 1000).toFixed(1)}k tok` : `${String(value)} tok`
}

describe('Conversation 复制与编辑（2026-10-21）', () => {
  it('exposes grouped running and failed read operations without requiring an extra expansion', () => {
    const source = toolsScenario.messages.find(row => row.kind === 'tool')!
    if (source.kind !== 'tool') throw new Error('Missing tool fixture')
    const messages = [
      { ...source, id: 'first-read', tool: { ...source.tool, name: 'read_file', target: 'first.ts', status: 'running' as const, expanded: false } },
      { ...source, id: 'second-read', tool: { ...source.tool, name: 'read_file', target: 'second.ts', status: 'failed' as const, expanded: false } },
    ]
    const { rerender } = render(<Conversation scenario={{ ...shellScenario, messages, stats: null }} />)
    expect(screen.getByRole('button', { name: /读取文件 2 次/ })).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByText('first.ts')).toBeVisible()
    expect(screen.getByText('second.ts')).toBeVisible()
    rerender(<Conversation scenario={{ ...shellScenario, messages: messages.map((message, index) => ({ ...message, tool: { ...message.tool, status: index === 0 ? 'uncertain' as const : 'succeeded' as const } })), stats: null }} />)
    expect(screen.getByText('first.ts')).toBeVisible()
    expect(screen.getByText('1 个结果未确认')).toBeVisible()
  })
  const messages = [
    { kind: 'user' as const, id: 'u1', text: '第一问', at: '' },
    { kind: 'assistant' as const, id: 'a1', text: '第一答', at: '' },
    { kind: 'user' as const, id: 'u2', text: '第二问', at: '' },
    { kind: 'assistant' as const, id: 'a2', text: '第二答', at: '' },
  ]

  it('edits the bubble in place and resends from the previous turn', () => {
    // 编辑第 2 轮 → `parentTurnId` 是上一轮（u1）：宿主退到"第一问之后"重发，
    // 旧的第二问及其回答进旧分支（不删除）。改动在**气泡原位**发生，不回填底部输入框。
    // 第 4 项是文档引用（2026-10-29）：可选——不带文件的编辑调用仍然只有三个实参。
    const edited: [string | null, string, readonly string[], (readonly { id: string; name: string }[])?][] = []
    render(
      <Conversation
        scenario={{ ...shellScenario, messages, stats: null }}
        onEditMessage={(...args) => {
          edited.push(args)
          return true
        }}
      />,
    )

    const buttons = screen.getAllByRole('button', { name: '编辑这条提问' })
    // 只有**最后一条**用户消息能编辑：改更早的一轮等于把后面的问答全部作废，
    // 而用户多半只是想改刚发出去的这句。旧轮次走对话地图的「从这里继续」。
    expect(buttons).toHaveLength(1)
    fireEvent.click(buttons[0]!)

    const box = screen.getByRole('textbox', { name: '编辑这条提问' })
    expect(box).toHaveValue('第二问')
    fireEvent.change(box, { target: { value: '改过的第二问' } })
    fireEvent.click(screen.getByRole('button', { name: '发送' }))

    expect(edited).toEqual([['u1', '改过的第二问', []]])
    // 被接受 → 退出编辑态（气泡文本由新快照接管，这里不假设它已经变了）。
    expect(screen.queryByRole('textbox', { name: '编辑这条提问' })).not.toBeInTheDocument()
  })

  it('stays in edit mode when the host refuses, so the typed words are not lost', () => {
    // 一轮正在跑时服务端会拒；这里必须**留着**用户刚改的词，不能静默丢弃。
    render(
      <Conversation
        scenario={{ ...shellScenario, messages, stats: null }}
        onEditMessage={() => false}
      />,
    )

    fireEvent.click(screen.getAllByRole('button', { name: '编辑这条提问' })[0]!)
    const box = screen.getByRole('textbox', { name: '编辑这条提问' })
    fireEvent.change(box, { target: { value: '还在跑' } })
    fireEvent.click(screen.getByRole('button', { name: '发送' }))

    expect(screen.getByRole('textbox', { name: '编辑这条提问' })).toHaveValue('还在跑')
  })

  it('carries the pictures of that message along, and cancels with Esc', () => {
    // 编辑只改文字，图片沿用原消息（豆包/Codex 同一手感）。
    const picture = '/api/sessions/s-1/attachments?attachment_id=2026%2F10%2F16%2Fa%2Fb.png'
    const withPicture = [
      { kind: 'user' as const, id: 'u1', text: '这张图是什么？', at: '', attachments: [picture] },
    ]
    const edited: [string | null, string, readonly string[], (readonly { id: string; name: string }[])?][] = []
    render(
      <Conversation
        scenario={{ ...shellScenario, messages: withPicture, stats: null }}
        onEditMessage={(...args) => {
          edited.push(args)
          return true
        }}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: '编辑这条提问' }))
    expect(screen.getByText(/1 张图片会一起带上/)).toBeInTheDocument()

    // Esc 取消：不发请求，回到原气泡。
    fireEvent.keyDown(screen.getByRole('textbox', { name: '编辑这条提问' }), { key: 'Escape' })
    expect(edited).toEqual([])
    expect(screen.queryByRole('textbox', { name: '编辑这条提问' })).not.toBeInTheDocument()

    // 再进一次，这次发送：图片跟着走。
    fireEvent.click(screen.getByRole('button', { name: '编辑这条提问' }))
    fireEvent.click(screen.getByRole('button', { name: '发送' }))
    expect(edited).toEqual([[null, '这张图是什么？', [picture]]])
  })

  it('hides the edit entry when the host provides no handler, but keeps copy', () => {
    // 原型态（没有宿主）不给一个点了没反应的按钮；复制是纯前端行为，照旧。
    render(<Conversation scenario={{ ...shellScenario, messages, stats: null }} />)

    expect(screen.queryByRole('button', { name: '编辑这条提问' })).not.toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: '复制' }).length).toBeGreaterThan(0)
  })
})

describe('Conversation 文档附件（2026-10-29）', () => {
  it('点文件块可以打开附件预览（2026-10-30）', () => {
    // 原来只能看名字，用户没法确认传上去的是哪一份。
    const onOpenAttachment = vi.fn()
    const messages = [
      {
        kind: 'user' as const,
        id: 'u1',
        text: '看看',
        at: '',
        files: [{ id: '2026/10/30/a/b.docx', name: '报告.docx', sizeBytes: 10 }],
      },
    ]
    render(
      <Conversation
        scenario={{ ...shellScenario, messages, stats: null }}
        onOpenAttachment={onOpenAttachment}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: '预览附件 报告.docx' }))

    expect(onOpenAttachment).toHaveBeenCalledWith('2026/10/30/a/b.docx')
  })

  it('没有宿主回调时文件块不可点，不假装能打开', () => {
    const messages = [
      {
        kind: 'user' as const,
        id: 'u1',
        text: '看看',
        at: '',
        files: [{ id: 'doc-1', name: '报告.docx', sizeBytes: 10 }],
      },
    ]
    render(<Conversation scenario={{ ...shellScenario, messages, stats: null }} />)

    expect(screen.getByRole('button', { name: '预览附件 报告.docx' })).toBeDisabled()
  })

  it('shows the uploaded name and size, not the storage hash', () => {
    // 落盘名是内容哈希，用户认不出来——气泡必须显示上传时的文件名。
    const messages = [
      {
        kind: 'user' as const,
        id: 'u1',
        text: '这份文件里写了什么',
        at: '',
        files: [
          { id: '2026/10/29/a/b.docx', name: '季度报告.docx', sizeBytes: 1_258_291 },
        ],
      },
    ]
    render(<Conversation scenario={{ ...shellScenario, messages, stats: null }} />)

    expect(screen.getByRole('list', { name: '随这条消息发送的文件' })).toBeInTheDocument()
    expect(screen.getByText('季度报告.docx')).toBeInTheDocument()
    expect(screen.getByText('1.2 MB')).toBeInTheDocument()
    // 不提供下载/打开：文件内容在磁盘上，页面没有理由拿到它。
    expect(screen.queryByRole('link')).toBeNull()
  })
})

describe('Conversation 图片（2026-10-16）', () => {
  const PICTURE = '/api/sessions/s-1/attachments?attachment_id=2026%2F10%2F16%2Fa%2Fb.png'

  it('opens the picture at full size from the bubble thumbnail, and closes it with Esc', () => {
    // 缩略图有尺寸上限（220×160）——截图里的小字、UI 细节得放大才看得清 ✔。
    const messages = [
      { kind: 'user' as const, id: 'u1', text: '这张图是什么颜色？', at: '', attachments: [PICTURE] },
    ]
    render(<Conversation scenario={{ ...shellScenario, messages, stats: null }} />)

    const thumb = screen.getByRole('button', { name: '放大查看这张图片' })
    expect(screen.queryByRole('dialog', { name: '放大查看图片' })).not.toBeInTheDocument()

    fireEvent.click(thumb)

    const dialog = screen.getByRole('dialog', { name: '放大查看图片' })
    // 放大用的是**同一个地址**：服务端回的是原始字节，不是重新编码过的副本 ✔。
    expect(within(dialog).getByRole('img', { name: '放大查看的图片' })).toHaveAttribute('src', PICTURE)
    // 关闭按钮是给鼠标/触屏那条路的；Esc 是键盘那条路。
    expect(within(dialog).getByRole('button', { name: '关闭放大视图' })).toBeInTheDocument()

    fireEvent.keyDown(dialog, { key: 'Escape' })
    expect(screen.queryByRole('dialog', { name: '放大查看图片' })).not.toBeInTheDocument()
  })

  it('renders a picture-only message without inventing text', () => {
    // 只发图不发字是合法提交：气泡里只有图，不编一句占位文字 ✗。
    const messages = [
      { kind: 'user' as const, id: 'u1', text: '', at: '', attachments: [PICTURE] },
    ]
    render(<Conversation scenario={{ ...shellScenario, messages, stats: null }} />)

    expect(screen.getByRole('button', { name: '放大查看这张图片' })).toBeInTheDocument()
  })
})

describe('Conversation', () => {
  it('folds completed successful work, keeps the final answer visible, and shows one reply time', () => {
    const tool = shellScenario.messages.find((row): row is Extract<typeof row, { kind: 'tool' }> => row.kind === 'tool')!
    render(<Conversation scenario={{ ...shellScenario, stats: null, messages: [
      { kind: 'user', id: 'u', text: '检查项目', at: '' },
      { kind: 'assistant', id: 'a', text: '先检查文件', at: '09:10' },
      { ...tool, id: 't', tool: { ...tool.tool, status: 'succeeded' } },
      { kind: 'assistant', id: 'b', text: '这是最终结论', at: '09:11' },
    ] }} />)
    const process = screen.getByRole('button', { name: /处理过程/ })
    expect(process).toHaveAttribute('aria-expanded', 'false')
    expect(screen.getByText('先检查文件')).not.toBeVisible()
    expect(screen.getByText('这是最终结论')).toBeVisible()
    expect(screen.getAllByLabelText('回复时间')).toHaveLength(1)
    expect(screen.getByLabelText('回复时间')).toHaveTextContent('09:11')
    fireEvent.click(process)
    expect(screen.getByText('先检查文件')).toBeVisible()
    expect(screen.getByText('读取文件')).toBeVisible()
  })

  // 2026-11-02 验收复现：真实图执行几乎每轮都有「检索与记忆 · 未命中」，而那一行曾一律
  // 挡住折叠，于是成功任务也不出现「处理过程」。正常检索结果应与工具一起收起。
  it('folds a normal retrieval row together with the tool work', () => {
    const tool = shellScenario.messages.find((row): row is Extract<typeof row, { kind: 'tool' }> => row.kind === 'tool')!
    render(<Conversation scenario={{ ...shellScenario, stats: null, messages: [
      { kind: 'user', id: 'u', text: '检查项目', at: '' },
      { kind: 'memory', id: 'm', memory: {
        retrieval: { outcome: 'miss', note: '本轮没有可引用的工作区文件或长期记忆。' },
        write: null,
      } },
      { ...tool, id: 't', tool: { ...tool.tool, status: 'succeeded' } },
      { kind: 'assistant', id: 'b', text: '这是最终结论', at: '' },
    ] }} />)
    const process = screen.getByRole('button', { name: /处理过程/ })
    expect(process).toHaveAttribute('aria-expanded', 'false')
    // 收起时那一行在 `hidden` 容器里：role 查询会跳过不可见子树，这里按标签取节点本身。
    expect(screen.getByLabelText('检索与记忆')).not.toBeVisible()
    expect(screen.getByText('这是最终结论')).toBeVisible()
    fireEvent.click(process)
    expect(screen.getByLabelText('检索与记忆')).toBeVisible()
  })

  // 反过来：需要用户注意的记忆行仍留在正文——检索失败、写入失败、有候选却没落库。
  it.each([
    ['retrieval failed', {
      retrieval: { outcome: 'failed' as const, reason: '记忆库不可读', note: '本轮没有成功检索。' },
      write: null,
    }],
    ['write failed', {
      retrieval: { outcome: 'miss' as const, note: '本轮没有可引用的工作区文件或长期记忆。' },
      write: { outcome: 'failed' as const, detail: '写入失败 · 不影响本轮回答' },
    }],
    ['write skipped', {
      retrieval: { outcome: 'miss' as const, note: '本轮没有可引用的工作区文件或长期记忆。' },
      write: { outcome: 'skipped' as const, detail: '中期与长期记忆都已关闭，候选没有落库' },
    }],
  ] as const)('keeps a memory row that needs attention exposed: %s', (_label, memory) => {
    const tool = shellScenario.messages.find((row): row is Extract<typeof row, { kind: 'tool' }> => row.kind === 'tool')!
    render(<Conversation scenario={{ ...shellScenario, stats: null, messages: [
      { kind: 'user', id: 'u', text: '检查项目', at: '' },
      { kind: 'memory', id: 'm', memory },
      { ...tool, id: 't', tool: { ...tool.tool, status: 'succeeded' } },
      { kind: 'assistant', id: 'b', text: '这是最终结论', at: '' },
    ] }} />)
    expect(screen.queryByRole('button', { name: /处理过程/ })).not.toBeInTheDocument()
    expect(screen.getByRole('region', { name: '检索与记忆' })).toBeVisible()
  })

  it.each(['failed', 'uncertain', 'pending', 'running'] as const)('keeps %s tool work visible', (status) => {
    const tool = shellScenario.messages.find((row): row is Extract<typeof row, { kind: 'tool' }> => row.kind === 'tool')!
    render(<Conversation scenario={{ ...shellScenario, stats: null, messages: [
      { kind: 'user', id: 'u', text: '检查项目', at: '' },
      { kind: 'assistant', id: 'a', text: '先检查文件', at: '' },
      { ...tool, id: 't', tool: { ...tool.tool, status } },
      { kind: 'assistant', id: 'b', text: '已有回复', at: '' },
    ] }} />)
    expect(screen.queryByRole('button', { name: /处理过程/ })).not.toBeInTheDocument()
    expect(screen.getByText('先检查文件')).toBeVisible()
  })

  it('keeps the current running turn open and folds it only after completion', () => {
    const tool = shellScenario.messages.find((row): row is Extract<typeof row, { kind: 'tool' }> => row.kind === 'tool')!
    const scene = { ...shellScenario, stats: null, messages: [
      { kind: 'user' as const, id: 'u', text: '检查项目', at: '' },
      { kind: 'assistant' as const, id: 'a', text: '正在检查', at: '' },
      { ...tool, id: 't', tool: { ...tool.tool, status: 'succeeded' as const } },
      { kind: 'assistant' as const, id: 'b', text: '最终结论', at: '' },
    ] }
    const view = render(<Conversation scenario={{ ...scene, session: { ...scene.session, status: 'running' } }} />)
    expect(screen.queryByRole('button', { name: /处理过程/ })).not.toBeInTheDocument()
    expect(screen.getByText('正在检查')).toBeVisible()
    view.rerender(<Conversation scenario={{ ...scene, session: { ...scene.session, status: 'completed' } }} />)
    expect(screen.getByRole('button', { name: /处理过程/ })).toHaveAttribute('aria-expanded', 'false')
    expect(screen.getByText('最终结论')).toBeVisible()
  })

  it('keeps an explicitly expanded tool visible after its turn completes', () => {
    const tool = shellScenario.messages.find((row): row is Extract<typeof row, { kind: 'tool' }> => row.kind === 'tool')!
    render(<Conversation scenario={{ ...shellScenario, stats: null, messages: [
      { kind: 'user', id: 'u', text: '检查项目', at: '' },
      { ...tool, id: 't', tool: { ...tool.tool, status: 'succeeded', expanded: true } },
      { kind: 'assistant', id: 'b', text: '最终结论', at: '' },
    ] }} />)
    expect(screen.queryByRole('button', { name: /处理过程/ })).not.toBeInTheDocument()
    expect(screen.getByText('读取文件')).toBeVisible()
  })

  it('keeps thought, narration and tools in chronological order, without inventing thought', () => {
    const tool = shellScenario.messages.find(message => message.kind === 'tool')!
    const messages = [
      { kind: 'user' as const, id: 'u', text: '测试', at: '' },
      { kind: 'assistant' as const, id: 'a', text: '开始查阅', at: '', reasoning: '模型思考' },
      { ...tool, id: 't1' },
      { kind: 'assistant' as const, id: 'b', text: '继续查阅', at: '' },
      { ...tool, id: 't2' },
    ]
    render(<Conversation scenario={{ ...shellScenario, messages, stats: null }} />)
    expect(screen.getAllByRole('button', { name: '思考' })).toHaveLength(1)
    const text = screen.getByRole('region', { name: '执行过程记录' }).textContent ?? ''
    const label = '读取文件'
    expect(text.indexOf(label)).toBeGreaterThan(text.indexOf('开始查阅'))
    expect(text.lastIndexOf(label)).toBeGreaterThan(text.indexOf('继续查阅'))
  })
  it('reports unknown usage as unknown, never as zero percent', () => {
    render(<Conversation scenario={errorsScenario} />)
    expect(screen.getAllByText(/未知/).length).toBeGreaterThan(0)
    expect(screen.queryByText(/0%/)).not.toBeInTheDocument()
  })

  it('renders each message kind through its own component', () => {
    render(<Conversation scenario={shellScenario} />)
    expect(screen.getByText('在 README 里补一段最小启动步骤。')).toBeInTheDocument()
    expect(screen.getByText('读取文件')).toBeInTheDocument()
    expect(screen.getByText('修改文件')).toBeInTheDocument()
    expect(screen.queryByText(/执行过程/)).not.toBeInTheDocument()
    expect(screen.getByText('缓存命中 62%')).toBeInTheDocument()
  })

  it('sums the turn from its per-call details instead of trusting a parallel total', () => {
    const stats = toolsScenario.stats
    if (stats === null) throw new Error('tools fixture has no stats')
    render(<Conversation scenario={toolsScenario} />)

    const input = stats.calls.reduce((total, call) => total + (call.inputTokens ?? 0), 0)
    const output = stats.calls.reduce((total, call) => total + (call.outputTokens ?? 0), 0)
    expect(screen.getByText(new RegExp(`输入 ${formatTokens(input)}`))).toBeInTheDocument()
    expect(screen.getByText(new RegExp(`输出 ${formatTokens(output)}`))).toBeInTheDocument()
    // 2026-09-30：摘要把整轮耗时也写进去了，这里只钉住条数部分。
    expect(screen.getByText(new RegExp(`明细（${String(stats.calls.length)} 次模型调用`))).toBeInTheDocument()
  })

  it('labels estimated token numbers as estimates', () => {
    const { unmount } = render(<Conversation scenario={toolsScenario} />)
    expect(screen.getByText('估算')).toBeInTheDocument()
    unmount()

    render(<Conversation scenario={shellScenario} />)
    expect(screen.queryByText('估算')).not.toBeInTheDocument()
  })

  it('renders the retrieval row as its own message kind', () => {
    render(<Conversation scenario={memoryHitScenario} />)
    expect(screen.getByRole('region', { name: '检索与记忆' })).toBeInTheDocument()
    expect(screen.getByText('已命中')).toBeInTheDocument()
  })

  it('replaces the flow with the empty state instead of an empty column', () => {
    const { container } = render(<Conversation scenario={emptySessionScenario} />)
    expect(screen.getByText('在工作区里动手做事')).toBeInTheDocument()
    expect(screen.getByText(/会先给出计划/)).toBeInTheDocument()
    expect(container.textContent ?? '').not.toContain('缓存命中')
  })

  it('renders the welcome card from the empty-state highlights', () => {
    render(<Conversation scenario={emptySessionScenario} />)
    const card = screen.getByRole('region', { name: '它替你做的三件事' })
    expect(within(card).getAllByRole('listitem')).toHaveLength(3)
    expect(within(card).getByText('跨文件读代码，回答带路径与行号')).toBeInTheDocument()
    // 插图只是装饰：没有可读文本，也不抢标题层级。
    expect(screen.getByRole('heading', { name: '在工作区里动手做事' })).toBeInTheDocument()
  })

  it('replaces the flow with the loading state and a content-free skeleton', () => {
    const { container } = render(<Conversation scenario={loadingHistoryScenario} />)
    expect(screen.getByText('正在读取会话事件…')).toBeInTheDocument()
    expect(screen.getByText(/不会重放模型调用/)).toBeInTheDocument()
    // The skeleton is shape only: six bars, no text of its own.
    expect(container.querySelectorAll('[aria-hidden="true"] > span')).toHaveLength(6)
    expect(container.textContent ?? '').not.toContain('开始一个新会话')
  })

  it('keeps every fixture internally consistent', () => {
    for (const scenario of scenarios) {
      const stats = scenario.stats
      if (stats === null || stats.calls.length === 0) continue
      const input = stats.calls.reduce((total, call) => total + (call.inputTokens ?? 0), 0)
      const output = stats.calls.reduce((total, call) => total + (call.outputTokens ?? 0), 0)
      expect(input, `${scenario.id} input total`).toBe(stats.inputTokens)
      expect(output, `${scenario.id} output total`).toBe(stats.outputTokens)
    }
  })

  it('offers one jump tick per user question once the flow scrolls', async () => {
    const user = (id: string, text: string) => ({ kind: 'user' as const, id, text, at: '' })
    const messages = [user('u1', '第一个问题'), user('u2', '第二个问题'), user('u3', '第三个问题')]
    render(<Conversation scenario={{ ...shellScenario, messages, stats: null }} />)

    const log = screen.getByRole('log', { name: '对话' })
    // jsdom 没有布局：把滚动区的尺寸写成"内容高于一屏"，刻度条才会出现。
    Object.defineProperty(log, 'scrollHeight', { configurable: true, value: 1200 })
    Object.defineProperty(log, 'clientHeight', { configurable: true, value: 400 })
    fireEvent.scroll(log)

    const rail = await screen.findByRole('group', { name: '问题跳转' })
    // 一条提问一根刻度，顺序与提问顺序一致；助手回答不进刻度条。
    expect(within(rail).getAllByRole('button').map((tick) => tick.getAttribute('aria-label')))
      .toEqual(['跳到：第一个问题', '跳到：第二个问题', '跳到：第三个问题'])
  })

  it('adds no jump rail when the conversation fits or has a single question', () => {
    // shellScenario 只有一条提问，且默认尺寸下一屏放得下。
    render(<Conversation scenario={shellScenario} />)
    expect(screen.queryByRole('group', { name: '问题跳转' })).not.toBeInTheDocument()
  })

  it('shows the assistant identity once per turn, not once per message part', () => {
    // 一轮里助手可能分好几段（流式 + 工具后继续），每段都挂一行名字就是刷屏。
    const messages = [
      { kind: 'user' as const, id: 'u1', text: '问一句', at: '' },
      { kind: 'assistant' as const, id: 'a1', text: '第一段', at: '' },
      { kind: 'assistant' as const, id: 'a2', text: '第二段', at: '' },
      { kind: 'user' as const, id: 'u2', text: '再问一句', at: '' },
      { kind: 'assistant' as const, id: 'a3', text: '第三段', at: '' },
    ]
    render(<Conversation scenario={{ ...shellScenario, messages, stats: null }} />)

    // 两轮 → 两个身份行，而不是三段助手消息 → 三个。
    expect(screen.getAllByText('chaiai')).toHaveLength(2)
  })

  it('offers a jump-to-bottom arrow only after scrolling away from the latest turn', async () => {
    // 往上翻历史之后才出现；在底部时不出现（那时按钮没有意义，只会挡内容）。
    render(<Conversation scenario={shellScenario} />)
    const log = screen.getByRole('log', { name: '对话' })
    Object.defineProperty(log, 'scrollHeight', { configurable: true, value: 1200 })
    Object.defineProperty(log, 'clientHeight', { configurable: true, value: 400 })
    const scrolled: unknown[] = []
    Object.defineProperty(log, 'scrollTo', {
      configurable: true,
      value: (options: unknown) => scrolled.push(options),
    })

    // 1200 - 400 = 800 就是底部：这里停在底部，不该出现。
    log.scrollTop = 800
    fireEvent.scroll(log)
    expect(screen.queryByRole('button', { name: '滑到最底' })).not.toBeInTheDocument()

    log.scrollTop = 200
    fireEvent.scroll(log)
    const arrow = await screen.findByRole('button', { name: '滑到最底' })

    fireEvent.click(arrow)

    // 滚到底（jsdom 不实现平滑滚动，只断言调用的目标）。
    expect(scrolled).toEqual([{ top: 1200, behavior: 'smooth' }])
  })

  it('follows live updates only while the reader remains near the bottom', () => {
    const { rerender } = render(<Conversation scenario={shellScenario} autoFollow />)
    const log = screen.getByRole('log', { name: '对话' })
    Object.defineProperty(log, 'scrollHeight', { configurable: true, value: 1000 })
    Object.defineProperty(log, 'clientHeight', { configurable: true, value: 500 })
    rerender(<Conversation scenario={{ ...shellScenario, messages: [...shellScenario.messages] }} autoFollow />)
    expect(log.scrollTop).toBe(1000)

    log.scrollTop = 100
    fireEvent.scroll(log)
    rerender(<Conversation scenario={{ ...shellScenario, messages: [...shellScenario.messages] }} autoFollow />)
    expect(log.scrollTop).toBe(100)
  })
})

describe('Conversation 追问（2026-10-26）', () => {
  const messages = [
    { kind: 'user' as const, id: 'u1', text: '这段怎么理解', at: '' },
    { kind: 'assistant' as const, id: 'a1', text: '第一段回答\n\n第二段回答', at: '' },
  ]
  const scenario = { ...shellScenario, messages, stats: null }

  it('quotes the whole turn from the footer button', () => {
    const quoted: string[] = []
    render(<Conversation scenario={scenario} onQuote={(text) => quoted.push(text)} />)

    fireEvent.click(screen.getByRole('button', { name: '追问' }))
    expect(quoted).toEqual(['第一段回答\n\n第二段回答'])
  })

  it('offers 引用这段追问 when text inside an answer is selected', () => {
    const quoted: string[] = []
    render(<Conversation scenario={scenario} onQuote={(text) => quoted.push(text)} />)

    const paragraph = screen.getByText('第一段回答')
    const range = document.createRange()
    range.selectNodeContents(paragraph)
    // jsdom 不做布局：矩形全 0 会被当成"看不见的选区"而收起浮层，所以给一个非零矩形。
    const original = Range.prototype.getBoundingClientRect
    Range.prototype.getBoundingClientRect = () => ({
      left: 100, top: 100, right: 180, bottom: 116, width: 80, height: 16,
      x: 100, y: 100, toJSON: () => ({}),
    }) as unknown as DOMRect
    const selection = window.getSelection()
    try {
      selection?.removeAllRanges()
      selection?.addRange(range)
      fireEvent.mouseUp(document.body)

      fireEvent.click(screen.getByRole('button', { name: '引用这段追问' }))
      expect(quoted).toEqual(['第一段回答'])
    } finally {
      Range.prototype.getBoundingClientRect = original
      selection?.removeAllRanges()
    }
  })

  it('renders the quoted part of a sent message as its own block', () => {
    // 存储里只有纯文本：气泡必须重新认出开头的引用块，否则那段 `>` 看起来就是用户打的字。
    render(<Conversation scenario={{
      ...scenario,
      messages: [
        { kind: 'user' as const, id: 'u1', text: '> 被引用的旧内容\n\n这是啥？', at: '' },
        { kind: 'assistant' as const, id: 'a1', text: '回答', at: '' },
      ],
    }} />)

    const block = screen.getByText('被引用的旧内容')
    expect(block.tagName).toBe('BLOCKQUOTE')
    // 问题不在引用块里：引用是旧内容、问题是这次问的，两者必须能分开读。
    expect(block).not.toHaveTextContent('这是啥？')
    expect(screen.getByText('这是啥？')).toBeInTheDocument()
  })

  it('does not offer 编辑 on a message that carries a quote', () => {
    render(<Conversation
      scenario={{
        ...scenario,
        messages: [
          { kind: 'user' as const, id: 'u1', text: '> 被引用的旧内容\n\n这是啥？', at: '' },
          { kind: 'assistant' as const, id: 'a1', text: '回答', at: '' },
        ],
      }}
      onEditMessage={() => true}
    />)

    // 复制照旧可用（用户气泡一个、轮尾一个）；编辑不出现——编辑框只能显示 `> ……` 原文，
    // 那段引用会重新变成用户打的字。
    expect(screen.getAllByRole('button', { name: '复制' }).length).toBeGreaterThan(0)
    expect(screen.queryByRole('button', { name: '编辑这条提问' })).not.toBeInTheDocument()
  })

  it('keeps both quote entrances hidden in prototype mode', () => {
    // 省略 `onQuote` = 原型态（静态基线、地图抽屉）：既没有追问按钮，也没有选区浮层。
    render(<Conversation scenario={scenario} />)
    expect(screen.queryByRole('button', { name: '追问' })).not.toBeInTheDocument()
  })
})
