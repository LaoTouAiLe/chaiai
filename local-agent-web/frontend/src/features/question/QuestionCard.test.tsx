import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { QuestionView } from '../../fixtures/types'
import { QuestionCard } from './QuestionCard'

function question(overrides: Partial<QuestionView> = {}): QuestionView {
  return {
    questionId: 'q-1',
    question: '部署到哪个环境？',
    items: [
      { index: 0, question: '部署到哪个环境？', options: ['测试环境', '生产环境'], allowFreeText: true, answer: null, skipped: false },
      { index: 1, question: '用哪个分支？', options: [], allowFreeText: true, answer: null, skipped: false },
      { index: 2, question: '要不要先备份？', options: ['要', '不要'], allowFreeText: false, answer: null, skipped: false },
    ],
    state: 'pending',
    answer: null,
    note: null,
    ...overrides,
  }
}

const actions = (overrides: Partial<Parameters<typeof QuestionCard>[0]['actions'] & object> = {}) => ({
  onSubmit: vi.fn(),
  onSkip: vi.fn(),
  busy: false,
  ...overrides,
})

beforeEach(() => {
  globalThis.sessionStorage?.clear()
})

describe('QuestionCard（题组）', () => {
  it('未确认的提交保留刷新草稿，后端确认结局后才清除', async () => {
    const user = userEvent.setup()
    const pending = question({ items: [{ index: 0, question: '部署到哪里？', options: [], allowFreeText: true, answer: null, skipped: false }] })
    const onSubmit = vi.fn()
    const first = render(<QuestionCard question={pending} actions={actions({ onSubmit })} />)
    await user.type(screen.getByRole('textbox'), '预发布环境')
    await user.click(screen.getByRole('button', { name: '提交' }))
    expect(onSubmit).toHaveBeenCalledOnce()
    first.unmount()
    const second = render(<QuestionCard question={pending} actions={actions()} />)
    expect(screen.getByRole('textbox')).toHaveValue('预发布环境')
    second.rerender(<QuestionCard question={{ ...pending, state: 'answered' }} actions={actions()} />)
    second.unmount()
    render(<QuestionCard question={pending} actions={actions()} />)
    expect(screen.getByRole('textbox')).toHaveValue('')
  })
  it('shows the group progress and walks through the questions locally', async () => {
    const user = userEvent.setup()
    render(<QuestionCard question={question()} actions={actions()} />)

    expect(screen.getByText('1/3')).toBeInTheDocument()
    expect(screen.getByText('部署到哪个环境？')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '下一题' }))
    expect(screen.getByText('2/3')).toBeInTheDocument()
    expect(screen.getByText('用哪个分支？')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '上一题' }))
    expect(screen.getByText('1/3')).toBeInTheDocument()
  })

  it('offers option buttons that fill the answer, and a free-text box next to them', async () => {
    const user = userEvent.setup()
    render(<QuestionCard question={question()} actions={actions()} />)

    const option = screen.getByRole('button', { name: '生产环境' })
    await user.click(option)
    expect(option).toHaveAttribute('aria-pressed', 'true')

    const box = screen.getByRole('textbox', { name: '第 1 题的回答' })
    expect(box).toHaveValue('生产环境')

    await user.clear(box)
    await user.type(box, '预发布环境')
    expect(box).toHaveValue('预发布环境')
  })

  it('hides the text box when a question only allows listed options', async () => {
    const user = userEvent.setup()
    render(<QuestionCard question={question()} actions={actions()} />)

    await user.click(screen.getByRole('button', { name: '下一题' }))
    await user.click(screen.getByRole('button', { name: '下一题' }))

    expect(screen.getByText('3/3')).toBeInTheDocument()
    expect(screen.queryByRole('textbox', { name: '第 3 题的回答' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '不要' })).toBeInTheDocument()
  })

  it('submits the whole group once, with per-question answers', async () => {
    const user = userEvent.setup()
    const onSubmit = vi.fn()
    render(<QuestionCard question={question()} actions={actions({ onSubmit })} />)

    await user.type(screen.getByRole('textbox', { name: '第 1 题的回答' }), '测试环境')
    await user.click(screen.getByRole('button', { name: '下一题' }))
    // 第 2 题故意留空：空的题按"跳过"提交
    await user.click(screen.getByRole('button', { name: '下一题' }))
    await user.click(screen.getByRole('button', { name: '要' }))
    await user.click(screen.getByRole('button', { name: '提交' }))

    expect(onSubmit).toHaveBeenCalledTimes(1)
    expect(onSubmit.mock.calls[0]?.[0]).toEqual([
      { index: 0, answer: '测试环境' },
      { index: 1, answer: '' },
      { index: 2, answer: '要' },
    ])
  })

  it('offers 全部跳过 as a real exit for the whole group', async () => {
    const user = userEvent.setup()
    const onSkip = vi.fn()
    render(<QuestionCard question={question()} actions={actions({ onSkip })} />)

    await user.click(screen.getByRole('button', { name: '全部跳过' }))
    expect(onSkip).toHaveBeenCalledTimes(1)
  })

  it('locks every control while a submission is in flight', () => {
    render(<QuestionCard question={question()} actions={actions({ busy: true })} />)

    expect(screen.getByRole('button', { name: '下一题' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '全部跳过' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '测试环境' })).toBeDisabled()
    expect(screen.getByText('正在提交，结果以后端为准…')).toBeInTheDocument()
  })

  it('disappears once the group is answered (the answer already lives in the reply)', () => {
    const answered = question({
      state: 'answered',
      items: [
        { index: 0, question: '部署到哪个环境？', options: ['测试环境'], allowFreeText: true, answer: '测试环境', skipped: false },
        { index: 1, question: '用哪个分支？', options: [], allowFreeText: true, answer: null, skipped: true },
        { index: 2, question: '要不要先备份？', options: ['要'], allowFreeText: false, answer: '要', skipped: false },
      ],
    })
    const { container } = render(<QuestionCard question={answered} actions={actions()} />)

    // 2026-10-28 用户实测反馈：答完之后那条卡片一直挤在输入区上方，不该再占位。
    expect(container).toBeEmptyDOMElement()
    expect(screen.queryByRole('region', { name: '提问' })).not.toBeInTheDocument()
  })

  it('disappears after a whole-group skip for the same reason', () => {
    const skipped = question({
      state: 'skipped',
      note: '你跳过了这组提问，本轮按没有补充信息继续。',
    })
    const { container } = render(<QuestionCard question={skipped} actions={actions()} />)

    expect(container).toBeEmptyDOMElement()
  })

  it('still explains an expired question (nothing else on screen says why)', () => {
    const expired = question({ state: 'expired', note: '该提问已失效：本轮已结束。' })
    render(<QuestionCard question={expired} />)

    expect(screen.getByRole('region', { name: '提问' })).toBeInTheDocument()
    expect(screen.getByText('提问已失效')).toBeInTheDocument()
    expect(screen.getByText('该提问已失效：本轮已结束。')).toBeInTheDocument()
  })

  it('keeps a single-question ask plain: no paging, no counter', () => {
    const single = question({
      items: [
        { index: 0, question: '部署到哪个环境？', options: [], allowFreeText: true, answer: null, skipped: false },
      ],
      question: '部署到哪个环境？',
    })
    render(<QuestionCard question={single} actions={actions()} />)

    expect(screen.queryByText('1/1')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '上一题' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '提交' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '跳过' })).toBeInTheDocument()
  })

  it('restores drafts after a remount (same tab, accidental refresh)', async () => {
    const user = userEvent.setup()
    const { unmount } = render(<QuestionCard question={question()} actions={actions()} />)
    await user.type(screen.getByRole('textbox', { name: '第 1 题的回答' }), '预发布')
    unmount()

    render(<QuestionCard question={question()} actions={actions()} />)
    expect(screen.getByRole('textbox', { name: '第 1 题的回答' })).toHaveValue('预发布')
  })
})
