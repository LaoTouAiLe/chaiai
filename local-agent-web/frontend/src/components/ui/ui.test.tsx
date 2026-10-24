import { fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { Badge } from './Badge'
import { Button } from './Button'
import { EmptyState } from './EmptyState'
import { SearchField } from './SearchField'
import { SegmentedControl } from './SegmentedControl'

/**
 * 原语层的契约测试（2026-10-22 V2）。
 *
 * 只断言行为与语义，不断言 class 名：CSS Modules 的类名由构建生成，
 * 断言它等于把测试绑死在哈希上。
 */
describe('Button', () => {
  it('默认 type=button，避免表单里误触发提交', () => {
    render(<Button>保存</Button>)
    expect(screen.getByRole('button', { name: '保存' })).toHaveAttribute('type', 'button')
  })

  it('可以显式改成 submit', () => {
    render(<Button type="submit">提交</Button>)
    expect(screen.getByRole('button', { name: '提交' })).toHaveAttribute('type', 'submit')
  })

  it('禁用时不触发点击', async () => {
    const onClick = vi.fn()
    render(<Button disabled onClick={onClick}>保存</Button>)
    await userEvent.click(screen.getByRole('button', { name: '保存' }))
    expect(onClick).not.toHaveBeenCalled()
  })
})

describe('Badge', () => {
  it('渲染文本并带上 title', () => {
    render(<Badge tone="success" title="连接可用">可用</Badge>)
    const badge = screen.getByText('可用')
    expect(badge).toHaveAttribute('title', '连接可用')
  })
})

describe('SearchField', () => {
  it('输入时把纯字符串交给 onChange', () => {
    const onChange = vi.fn()
    render(<SearchField value="" onChange={onChange} placeholder="搜索会话…" />)
    // 受控组件固定 value，逐字符输入会被 React 立刻回写为空；这里直接发一次 change。
    fireEvent.change(screen.getByPlaceholderText('搜索会话…'), { target: { value: 'ab' } })
    expect(onChange).toHaveBeenLastCalledWith('ab')
  })

  it('有内容时出现清除按钮，点击回调空串', async () => {
    const onChange = vi.fn()
    render(<SearchField value="abc" onChange={onChange} />)
    await userEvent.click(screen.getByRole('button', { name: '清除搜索' }))
    expect(onChange).toHaveBeenCalledWith('')
  })

  it('空值时不渲染清除按钮', () => {
    render(<SearchField value="" onChange={vi.fn()} />)
    expect(screen.queryByRole('button', { name: '清除搜索' })).toBeNull()
  })
})

describe('EmptyState', () => {
  it('渲染标题、说明与操作', () => {
    render(
      <EmptyState
        title="还没有产物"
        description="Agent 生成的图片与文件会出现在这里"
        action={<button type="button">刷新</button>}
      />,
    )
    expect(screen.getByText('还没有产物')).toBeInTheDocument()
    expect(screen.getByText('Agent 生成的图片与文件会出现在这里')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '刷新' })).toBeInTheDocument()
  })

  it('不给说明与操作时不渲染空节点', () => {
    const { container } = render(<EmptyState title="空" compact />)
    expect(screen.getByText('空')).toBeInTheDocument()
    expect(container.querySelectorAll('p')).toHaveLength(1)
  })
})

describe('SegmentedControl', () => {
  const options = [
    { value: 'a', label: '甲' },
    { value: 'b', label: '乙' },
  ] as const

  it('渲染为 tablist，选中项 aria-selected=true', () => {
    render(<SegmentedControl options={options} value="a" ariaLabel="测试视图" />)
    expect(screen.getByRole('tablist', { name: '测试视图' })).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: '甲' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByRole('tab', { name: '乙' })).toHaveAttribute('aria-selected', 'false')
  })

  it('点击把选中值交回调用方', async () => {
    const onChange = vi.fn()
    render(<SegmentedControl options={options} value="a" ariaLabel="测试视图" onChange={onChange} />)
    await userEvent.click(screen.getByRole('tab', { name: '乙' }))
    expect(onChange).toHaveBeenCalledWith('b')
  })

  it('省略 onChange 时是只读态，点击不抛错也不改变选中', async () => {
    render(<SegmentedControl options={options} value="a" ariaLabel="测试视图" />)
    await userEvent.click(screen.getByRole('tab', { name: '乙' }))
    expect(screen.getByRole('tab', { name: '甲' })).toHaveAttribute('aria-selected', 'true')
  })
})
