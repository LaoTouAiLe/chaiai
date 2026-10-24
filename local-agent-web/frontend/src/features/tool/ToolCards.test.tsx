import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { toolsScenario } from '../../fixtures/scenarios'
import type { ToolCardView, ToolRowView } from '../../fixtures/types'
import { ToolCard } from './ToolCards'
import { ToolRow } from './ToolRow'

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

    expect(screen.getByText('委派只读调查')).toBeInTheDocument()
    expect(screen.getByText('1/2 个子任务已完成')).toBeInTheDocument()
    expect(screen.getByText('docs')).toBeInTheDocument()
    expect(screen.getByText('已完成 · 证据 6 · 工具 4 次')).toBeInTheDocument()
    // 未完成的那一路必须说明原因：只显示"未完成"用户没法判断要不要重派。
    expect(screen.getByText('未完成 · 子任务超时')).toBeInTheDocument()
    // **不摆"预留 token"**（2026-10-13 裁定）：那是切出来的上限、每路几乎一样，摆在这里会被读成
    // "这一路花了多少"。这条断言就是那道边界，别让它悄悄回来。
    expect(screen.queryByText(/预留/)).not.toBeInTheDocument()
    // 核对过的原文摆在同一张卡里（路径 + 行号，用户据此核对）。
    expect(screen.getByText(/docs\/cancel\.md:1/)).toBeInTheDocument()
  })

  it('reads a window with its own line numbers and the file total', () => {
    render(<ToolCard card={cardOf('read')} />)
    expect(screen.getByText('packages/host/webserver/src/index.ts')).toBeInTheDocument()
    expect(screen.getByText('第 41–45 行 / 共 196 行')).toBeInTheDocument()
    expect(screen.getByText('41')).toBeInTheDocument()
    expect(screen.getByText('45')).toBeInTheDocument()
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
})
