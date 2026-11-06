import type { ReactElement, ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, fireEvent, render as baseRender, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { FileLinkContext } from '../conversation/fileLinkContext'
import { toolsScenario } from '../../fixtures/scenarios'
import type { ToolCardView, ToolRowView } from '../../fixtures/types'
import { ToolCard } from './ToolCards'
import { ToolRow } from './ToolRow'

/** 委派卡的实时进度请求：只替换这一条，其余客户端调用照旧走真实现。 */
const delegationLive = vi.hoisted(() => ({ get: vi.fn() }))
vi.mock('../../api/client', async original => ({
  ...await original<typeof import('../../api/client')>(),
  getDelegationLive: delegationLive.get,
}))

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

/**
 * 委派卡会轮询实时进度（2026-11-04），所以任何渲染都要有 QueryClient。
 * 这里覆盖 `render` 而不是逐个改调用点：26 个用例都渲染在同一个 Provider 下才一致。
 */
function render(ui: ReactElement) {
  return baseRender(ui, {
    wrapper: ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    ),
  })
}

function cards(): readonly ToolCardView[] {
  return toolsScenario.messages.flatMap((message) => (message.kind === 'tool' ? [message.tool.card] : []))
}

function cardOf<K extends ToolCardView['kind']>(kind: K): Extract<ToolCardView, { kind: K }> {
  for (const card of cards()) {
    if (card.kind === kind) return card as Extract<ToolCardView, { kind: K }>
  }
  throw new Error(`missing ${kind} card in the tools fixture`)
}

function rowOf(index = 0): ToolRowView {
  const rows = toolsScenario.messages.flatMap((message) =>
    message.kind === 'tool' ? [message.tool] : [],
  )
  const row = rows[index]
  if (!row) throw new Error(`missing tool row #${String(index)} in the tools fixture`)
  return row
}

describe('ToolCard', () => {
  it('copies the returned result beyond its display window and toggles wrapping', async () => {
    const copy = vi.fn().mockResolvedValue(undefined)
    const original = Object.getOwnPropertyDescriptor(navigator, 'clipboard')
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: copy } })
    const lines = Array.from({ length: 30 }, (_, index) => ({ number: index + 10, text: `result-${index}` }))
    try {
      render(<ToolCard card={{ ...cardOf('read'), lines, totalLines: 100 }} />)
      expect(screen.queryByText('result-29')).not.toBeInTheDocument()
      fireEvent.click(screen.getByRole('button', { name: '复制结果' }))
      await waitFor(() => expect(copy).toHaveBeenCalledWith(lines.map(line => line.text).join('\n')))
      const wrap = screen.getByRole('button', { name: '结果自动换行' })
      fireEvent.click(wrap)
      expect(wrap).toHaveAttribute('aria-pressed', 'true')
    } finally {
      if (original) Object.defineProperty(navigator, 'clipboard', original)
      else Reflect.deleteProperty(navigator, 'clipboard')
    }
  })

  it('opens a search hit as a file without guessing from its snippet', () => {
    const open = vi.fn()
    render(<FileLinkContext.Provider value={open}><ToolCard card={{ kind: 'search', shape: 'matches', total: 1,
      paths: [], truncated: false, groups: [{ path: 'src/search.ts', matches: [{ line: 12, text: 'some code' }] }] }} /></FileLinkContext.Provider>)
    fireEvent.click(screen.getByRole('button', { name: '打开文件 src/search.ts' }))
    expect(open).toHaveBeenCalledWith('src/search.ts')
  })

  it('presents complete JSON as expandable values and leaves malformed text verbatim', () => {
    const { container, rerender } = render(<ToolCard card={{ kind: 'generic', headline: 'MCP 结果', detail: JSON.stringify({ count: 2, files: ['app.ts', '<script>alert(1)</script>'] }) }} />)
    expect(screen.getByText('count')).toBeInTheDocument()
    fireEvent.click(screen.getByText('files'))
    expect(screen.getByText('app.ts')).toBeVisible()
    expect(container.querySelector('script')).toBeNull()
    rerender(<ToolCard card={{ kind: 'generic', headline: 'MCP 结果', detail: '{"未完成":' }} />)
    expect(screen.getByText('{"未完成":')).toBeInTheDocument()
    expect(screen.queryByLabelText('结构化结果')).not.toBeInTheDocument()
  })
  it('opens a tool file separately from expanding its result, and rejects URL targets', () => {
    const open = vi.fn(), toggle = vi.fn()
    const row = { ...rowOf(), name: 'read_file', target: 'src/app.ts:12', expanded: false }
    const { container, rerender } = render(<FileLinkContext.Provider value={open}><ToolRow tool={row} onToggle={toggle} /></FileLinkContext.Provider>)
    fireEvent.click(screen.getByRole('button', { name: '打开文件 src/app.ts' }))
    expect(open).toHaveBeenCalledWith('src/app.ts')
    expect(toggle).not.toHaveBeenCalled()
    expect(container.querySelector('button button')).toBeNull()
    rerender(<FileLinkContext.Provider value={open}><ToolRow tool={{ ...row, target: 'https://example.org/app.ts' }} /></FileLinkContext.Provider>)
    expect(screen.queryByRole('button', { name: /打开文件/ })).not.toBeInTheDocument()
  })
  it('shows document extraction limitations and page continuation hints', () => {
    render(<ToolCard card={{ ...cardOf('read'), note: '正文无法通过文本读取；传 start_page=6 继续读' }} />)
    expect(screen.getByLabelText('读取说明')).toHaveTextContent('正文无法通过文本读取')
    expect(screen.getByLabelText('读取说明')).toHaveTextContent('start_page=6')
  })
  it('covers every card shape the conversation must render', () => {
    expect(cards().map((card) => card.kind)).toEqual([
      'terminal',
      'search',
      'search',
      'read',
      'todo',
      'generic',
      'terminal',
      'delegation',
    ])
  })

  it('covers both a failed and a successful command', () => {
    const exits = cards().flatMap((card) => (card.kind === 'terminal' ? [card.exitCode] : []))
    expect(exits).toEqual([1, 0])
  })

  /**
   * 自主委派（§18.7）：**每个子 Agent 一行**，带状态、证据条数、工具调用次数与失败原因。
   *
   * 这三项是照参照实现的任务面板来的：用户要判断"哪一路查到了、花了多少、哪一路没成"，
   * 而这些必须是能直接渲染的字段，不是让人去读工具返回的那段 JSON。
   */
  it('lists each delegated child with its status, counts and failure reason', () => {
    render(<ToolCard card={cardOf('delegation')} />)

    // 每路一张独立卡片（2026-11-04 用户要求）：不再是"一个框里分几行"，
    // 所以这里按 region 数量断言，而不是找一个大标题。
    expect(screen.getAllByRole('region', { name: '委派子任务' })).toHaveLength(2)
    expect(screen.getByText('1/2 个子任务已完成')).toBeInTheDocument()
    expect(screen.getByText('docs')).toBeInTheDocument()
    // 耗时排在最后（2026-11-04）：几路并发时它是"哪一路慢"的唯一依据。
    expect(screen.getByText('已完成 · 证据 6 · 工具 4 次 · 改动 1 次 · 12.4s')).toBeInTheDocument()
    // 未完成的那一路必须说明原因：只显示"未完成"用户没法判断要不要重派。
    expect(screen.getByText('未完成 · 子任务超时')).toBeInTheDocument()
    // **不摆"预留 token"**（2026-10-13 裁定）：那是切出来的上限、每路几乎一样，摆在这里会被读成
    // "这一路花了多少"。这条断言就是那道边界，别让它悄悄回来。
    expect(screen.queryByText(/预留/)).not.toBeInTheDocument()
    // 核对过的原文摆在同一张卡里（路径 + 行号，用户据此核对）。
    expect(screen.getByText(/docs\/cancel\.md:1/)).toBeInTheDocument()
  })

  /**
   * 委派**还没跑完**时（2026-11-04）：一发起就摊出 N 条子任务行。
   *
   * 改之前只能等全部跑完，用户看到的是一张泛泛的"委派只读调查"空壳，十几秒里完全不知道
   * 派了几路、在查什么。这条断言守的就是那个体验。
   */
  it('shows one pending row per delegated child before any result comes back', () => {
    render(
      <ToolCard
        card={{
          kind: 'delegation',
          children: [],
          pending: ['src/local_agent/tools', 'src/local_agent/graph'],
          report: '',
          truncated: false,
        }}
      />,
    )

    // 总数要把"还在跑"的算进去，否则会显示 0/0，看起来像什么都没派出去。
    expect(screen.getByText('0/2 个子任务已完成')).toBeInTheDocument()
    expect(screen.getByText('src/local_agent/tools')).toBeInTheDocument()
    expect(screen.getByText('src/local_agent/graph')).toBeInTheDocument()
    expect(screen.getAllByText('进行中…')).toHaveLength(2)
  })

  /**
   * 折叠行为（2026-11-04，对齐 CodeBuddy 的 subagent 行）：
   * 跑完的默认收起（结果在标题行里），点一下才展开它自己的工具链。
   * 运行中的默认展开——用户此刻要看的就是"它在查什么"。
   */
  it('collapses a finished child and expands it on click', () => {
    render(
      <ToolCard
        card={{
          kind: 'delegation',
          children: [{
            scope: 'docs',
            status: 'completed',
            toolCalls: 1,
            modelCalls: 1,
            evidenceCount: 1,
            changedFiles: 0,
            elapsedMs: 900,
            tools: [{ name: 'read_file', summary: '', status: 'succeeded', target: 'docs/a.md' }],
            error: null,
          }],
          pending: [],
          report: '',
          truncated: false,
        }}
      />,
    )

    // 收起时看不到工具名，但状态仍在标题行上。
    expect(screen.queryByText('read_file')).not.toBeInTheDocument()
    expect(screen.getByText(/已完成 · 证据 1/)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { expanded: false }))
    expect(screen.getByText('read_file')).toBeInTheDocument()
    expect(screen.getByText('docs/a.md')).toBeInTheDocument()
  })

  /**
   * 等待时长（2026-11-04）：委派可能跑几十秒，没有计时的话"还在跑"和"卡住了"看起来一样。
   *
   * 只在**有子任务还在跑**时显示。全跑完还挂着"已等待 45s"反而会让人以为它没结束。
   */
  it('reports how long the delegation has been waiting, only while it is running', () => {
    const { rerender } = render(
      <ToolCard
        card={{
          kind: 'delegation',
          children: [],
          pending: ['src/local_agent/tools'],
          report: '',
          truncated: false,
        }}
        elapsed="12s"
      />,
    )
    expect(screen.getByText('0/1 个子任务已完成 · 已等待 12s')).toBeInTheDocument()

    rerender(
      <ToolCard
        card={{
          kind: 'delegation',
          children: [
            { scope: 'src/local_agent/tools', status: 'completed', toolCalls: 3, modelCalls: 1, evidenceCount: 2, changedFiles: 0, elapsedMs: 8200, error: null },
          ],
          pending: [],
          report: '',
          truncated: false,
        }}
        elapsed="12s"
      />,
    )
    expect(screen.getByText('1/1 个子任务已完成')).toBeInTheDocument()
    expect(screen.queryByText(/已等待/)).not.toBeInTheDocument()
  })

  it('reads a window with its own line numbers and the file total', () => {
    render(<ToolCard card={cardOf('read')} />)
    expect(screen.getByText('packages/host/webserver/src/index.ts')).toBeInTheDocument()
    expect(screen.getByText('第 41–45 行 / 共 196 行')).toBeInTheDocument()
    expect(screen.getByText('41')).toBeInTheDocument()
    expect(screen.getByText('45')).toBeInTheDocument()
  })

  /**
   * 网页检索（2026-10-26）：卡片要说清「搜了什么、回了几条、后端是谁」。
   *
   * 卡片单独构造而不进 `tools` fixture：那个场景被会话视图与轨迹视图共用，加一条工具消息
   * 会连带改掉它们对行数的断言，代价大于收益。
   */
  it('renders a web search as a result card with its query, backend and hits', () => {
    const card: ToolCardView = {
      kind: 'web',
      target: 'DeepSeek 搜索 API 计费',
      summary: '5 条来源 · 后端 deepseek',
      body: 'DeepSeek 搜索 API 文档 · api-docs.deepseek.com\n计费说明 · platform.deepseek.com',
      truncated: false,
    }
    render(<ToolCard card={card} />)

    // 卡片类型只体现在 `aria-label` 上（可见标题是搜索词），所以按 region 取名断言。
    expect(screen.getByRole('region', { name: '网页结果' })).toBeInTheDocument()
    expect(screen.getByText('DeepSeek 搜索 API 计费')).toBeInTheDocument()
    expect(screen.getByText('5 条来源 · 后端 deepseek')).toBeInTheDocument()
    // 前几条命中按「标题 · 站点」逐行列出：用户据此判断要不要再读全文。
    expect(screen.getByText(/DeepSeek 搜索 API 文档 · api-docs\.deepseek\.com/)).toBeInTheDocument()
  })

  it('groups matches by file and states that the list was truncated', () => {
    const card = cardOf('search')
    render(<ToolCard card={card} />)
    expect(screen.getByText('按内容匹配')).toBeInTheDocument()
    expect(screen.getByText('显示 2 个文件 / 共 42')).toBeInTheDocument()
    expect(screen.getByText('packages/client/ui-primitives/src/SearchBlock.tsx')).toBeInTheDocument()
    // A capped result must say so rather than look complete — and must say **how** to get
    // the whole thing (v0.8 T73/§19.3): the page never pretends it can show it itself.
    expect(screen.getByText(/结果已截断，页面里是服务端给的有界预览/)).toBeInTheDocument()
    expect(screen.getByText(/完整内容可让助手按行区间读取/)).toBeInTheDocument()
  })

  it('renders the path shape as a flat list', () => {
    const shapes = cards().filter((card) => card.kind === 'search')
    const paths = shapes[1]
    if (paths === undefined || paths.kind !== 'search') throw new Error('missing paths search card')
    render(<ToolCard card={paths} />)
    expect(screen.getByText('按路径匹配')).toBeInTheDocument()
    expect(screen.getByText('显示 5 个路径 / 共 23')).toBeInTheDocument()
    expect(screen.getAllByRole('listitem')).toHaveLength(5)
  })

  it('keeps content match totals separate from the number of files', () => {
    render(<ToolCard card={{ kind: 'search', shape: 'matches', paths: [], total: 100,
      totalUnit: 'matches', truncated: true,
      groups: [{ path: 'notes.txt', matches: [{ line: 2, text: 'line 2' }, { line: 20, text: 'line 20' }] }],
    }} />)
    expect(screen.getByText('显示 2 处匹配 / 共 100 处匹配 · 1 个文件')).toBeInTheDocument()
    expect(screen.getByText(/结果已截断，页面里是服务端给的有界预览/)).toBeInTheDocument()
  })

  it('separates the terminal exit code from the output body', () => {
    render(<ToolCard card={cardOf('terminal')} />)
    expect(screen.getByText('pnpm run check')).toBeInTheDocument()
    expect(screen.getByText('退出码 1')).toBeInTheDocument()
    // The exit marker belongs to the card, not to the captured text.
    expect(screen.getByText(/1 of 4 checks failed/)).toBeInTheDocument()
    expect(screen.getByText(/当前省略了 8 行/)).toBeInTheDocument()
  })

  it('counts todo progress and labels every state', () => {
    render(<ToolCard card={cardOf('todo')} />)
    expect(screen.getByText('1/4 已完成')).toBeInTheDocument()
    // Parallel steps stay parallel: two items may be in progress at once.
    expect(screen.getAllByText('进行中')).toHaveLength(2)
    expect(screen.getAllByText('待处理')).toHaveLength(1)
  })

  it('never paints a successful exit as a failure', () => {
    const base = cardOf('terminal')

    const success = render(<ToolCard card={{ ...base, exitCode: 0, signal: null }} />)
    expect(screen.getByText('退出码 0').className).not.toMatch(/exitFailed/)
    success.unmount()

    const failure = render(<ToolCard card={{ ...base, exitCode: 2, signal: null }} />)
    expect(screen.getByText('退出码 2').className).toMatch(/exitFailed/)
    failure.unmount()

    render(<ToolCard card={{ ...base, exitCode: null, signal: 'SIGTERM' }} />)
    expect(screen.getByText('信号 SIGTERM').className).toMatch(/exitFailed/)
  })

  it('keeps unknown tools on the generic renderer', () => {
    render(<ToolCard card={cardOf('generic')} />)
    expect(screen.getByText('工具返回了结构化结果')).toBeInTheDocument()
    expect(screen.getByText('结果详情')).toBeInTheDocument()
  })
})

/**
 * 长正文的窗口（v0.8 T73；契约 §19.3）。
 *
 * 这一组用例守的是三件事：① 长正文默认只露一屏、并**如实**说还有多少行没显示；
 * ② "显示更多"只在页面**已经拿到**的内容上翻（是纯界面状态，不发请求）；
 * ③ 正文短于一屏时 DOM 与从前一字不差（视觉基线不重拍靠的就是它）。
 */
describe('长正文的窗口（T73）', () => {
  const lines = (count: number, start = 1) =>
    Array.from({ length: count }, (_, index) => ({ number: start + index, text: `内容 ${String(start + index)}` }))
  const readCard = (count: number, start = 1): Extract<ToolCardView, { kind: 'read' }> => ({
    kind: 'read',
    path: 'big.txt',
    lines: lines(count, start),
    totalLines: count,
    language: '',
  })
  const rows = () => screen.getAllByText(/^内容 \d+$/)
  /** 正文是单个 `<pre>` 的卡片（终端 / 结果）按**整段文本**断言。 */
  const preWith = (text: string) =>
    screen.queryByText((_, element) => element?.tagName === 'PRE' && element.textContent === text)

  it('shows one screen and says how many lines are still hidden', () => {
    render(<ToolCard card={readCard(25)} />)
    expect(rows()).toHaveLength(20)
    expect(screen.getByText('共 25 行 · 还有 5 行未显示')).toBeInTheDocument()
  })

  it('reveals the rest on demand and then drops the control', () => {
    render(<ToolCard card={readCard(25)} />)
    fireEvent.click(screen.getByRole('button', { name: '显示更多' }))
    expect(rows()).toHaveLength(25)
    // 到底了就没有"更多"可显示：控件必须消失，不能留着点不动。
    expect(screen.queryByRole('button', { name: '显示更多' })).not.toBeInTheDocument()
    expect(screen.queryByText(/还有 \d+ 行未显示/)).not.toBeInTheDocument()
  })

  it('numbers the visible rows with the file line numbers the contract gave', () => {
    // 窗口从文件第 200 行开始：开始那一行必须是 200，否则与落盘文件的行号核对不上。
    render(<ToolCard card={{ ...readCard(3, 200), firstLine: 200, totalLines: 1000 }} />)
    expect(screen.getByText('第 200–202 行 / 共 1000 行')).toBeInTheDocument()
    expect(screen.getByText('200')).toBeInTheDocument()
  })

  it('windows both terminal streams as one sequence', () => {
    const stdout = lines(25).map((line) => line.text).join('\n')
    render(
      <ToolCard
        card={{
          kind: 'terminal',
          command: 'pnpm run check',
          cwd: 'local-agent-web',
          exitCode: 1,
          signal: null,
          output: stdout,
          stdout,
          stderr: '构建失败',
          collapsedLines: 0,
        }}
      />,
    )

    // 终端正文是一个 `<pre>`，所以按**整段文本**断言（窗口就是把它切成前 20 行）。
    expect(preWith(lines(20).map((line) => line.text).join('\n'))).not.toBeNull()
    expect(preWith(stdout)).toBeNull()
    expect(screen.getByText('共 26 行 · 还有 6 行未显示')).toBeInTheDocument()
    // 窗口还没走到 stderr：不能显示成"标准错误输出：(空)"——那是与事实相反的读数。
    expect(screen.queryByText('标准错误输出：')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '显示更多' }))
    expect(preWith(stdout)).not.toBeNull()
    expect(preWith('构建失败')).not.toBeNull()
    expect(screen.getByText('标准错误输出：')).toBeInTheDocument()
  })

  it('never lets a server-clipped output look complete', () => {
    // 服务端截断过（`truncated`）就要说明"这是有界预览 + 怎么拿到完整的"（§19.3）：
    // 此前 shell 卡压根没有这个字段，页面对截断**只字不提**。
    render(
      <ToolCard
        card={{
          kind: 'terminal',
          command: 'pnpm run check',
          cwd: 'local-agent-web',
          exitCode: 0,
          signal: null,
          output: '全部输出',
          stdout: '全部输出',
          collapsedLines: 0,
          truncated: true,
        }}
      />,
    )

    expect(screen.getByText(/页面里是服务端给的有界预览/)).toBeInTheDocument()
    expect(screen.getByText(/完整内容可让助手按行区间读取/)).toBeInTheDocument()
  })

  it('leaves a body that fits on one screen exactly as it was', () => {
    // 原型夹具走的就是这一支：没有"显示更多"、正文原样（视觉基线不重拍的依据）。
    const short = 'a\nb\nc'
    render(
      <ToolCard
        card={{
          kind: 'terminal',
          command: 'ls',
          cwd: '.',
          exitCode: 0,
          signal: null,
          output: short,
          stdout: short,
          stderr: '',
          collapsedLines: 0,
        }}
      />,
    )

    expect(screen.queryByRole('button', { name: '显示更多' })).not.toBeInTheDocument()
    expect(preWith(short)).not.toBeNull()
    expect(screen.getByText('标准错误输出：')).toBeInTheDocument()
  })
})

describe('ToolRow', () => {
  it('renders the specialised card for a known tool and never a generic one', () => {
    render(<ToolCard card={cardOf('read')} />)
    expect(screen.queryByText('结果详情')).not.toBeInTheDocument()
  })

  it('routes a file mutation to the diff card', () => {
    const message = toolsScenario.messages.find(
      (candidate) => candidate.kind === 'tool' && candidate.tool.card.kind === 'file-mutation',
    )
    // The tools fixture has no mutation; the diff scenario owns that card.
    expect(message).toBeUndefined()

    const mutation = {
      name: 'edit_file',
      summary: '精确串替换',
      status: 'succeeded' as const,
      durationLabel: '0.2s',
      target: 'notes/demo.txt',
      expanded: true,
      card: {
        kind: 'file-mutation' as const,
        diff: { path: 'notes/demo.txt', additions: 1, deletions: 0, hunks: [] },
      },
    }
    render(<ToolRow tool={mutation} />)
    // The path shows in the row and again inside the diff card it expands into.
    expect(screen.getAllByText('notes/demo.txt')).toHaveLength(2)
    expect(screen.getByRole('figure')).toBeInTheDocument()
    expect(screen.queryByText('结果详情')).not.toBeInTheDocument()
  })

  it('names the verification result in the user’s terms, never by our internal grade', () => {
    // 2026-10-15：这一行曾写「· 已机械校验 / · 未通过机械校验」——`verified` 确实是反思层用代码
    // 核对出来的事实，但"机械校验"是内部评级名，用户要判断的是"这个结论能不能当依据"。
    const evidence = { evidenceType: 'file_hash', summary: '文件哈希与预期一致', verified: true }
    const { rerender } = render(<ToolRow tool={{ ...rowOf(), expanded: true, evidence }} />)
    expect(screen.getByText(/已核对/)).toBeInTheDocument()
    expect(screen.queryByText(/机械/)).not.toBeInTheDocument()

    rerender(
      <ToolRow tool={{ ...rowOf(), expanded: true, evidence: { ...evidence, verified: false } }} />,
    )
    expect(screen.getByText(/未核对通过/)).toBeInTheDocument()
    expect(screen.queryByText(/机械/)).not.toBeInTheDocument()
    // 事实不能为了去术语被删掉：核对依据本身仍要在（用户据此核对结果）。
    expect(screen.getAllByText(/文件哈希与预期一致/).length).toBeGreaterThan(0)
  })

  /**
   * 实时进度只在"还在跑"时轮询（2026-11-06）。
   *
   * `pending` 来自工具入参，任务被取消/失败时它不会被清空；只看它，页面会每 1.5 秒请求一次
   * `/api/delegation/live` 直到用户关页。行的状态才是"结束了没有"。
   */
  it('stops asking for the live delegation feed once the row is no longer running', async () => {
    const live = vi.fn().mockResolvedValue({ lanes: [] })
    delegationLive.get.mockImplementation(live)
    const card: ToolCardView = {
      kind: 'delegation', children: [], pending: ['src/local_agent/tools'], report: '', truncated: false,
    }
    const row: ToolRowView = { ...rowOf(), name: 'delegate_research', expanded: true, card }
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    )

    vi.useFakeTimers()
    try {
      const view = baseRender(<ToolRow tool={{ ...row, status: 'running' }} />, { wrapper })
      await act(async () => { await vi.advanceTimersByTimeAsync(0) })
      expect(live).toHaveBeenCalledTimes(1)

      // 取消/失败后 pending 仍在，但已经不该再问服务端了。
      view.rerender(<ToolRow tool={{ ...row, status: 'failed' }} />)
      await act(async () => { await vi.advanceTimersByTimeAsync(6000) })
      expect(live).toHaveBeenCalledTimes(1)
    } finally {
      vi.useRealTimers()
    }
  })
})
