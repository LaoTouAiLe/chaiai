import { act, fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, expect, it, vi } from 'vitest'
import { PreviewPane } from './PreviewPane'

const mocks = vi.hoisted(() => ({ detail: vi.fn(), preview: vi.fn() }))

vi.mock('../../api/client', async original => ({
  ...await original<typeof import('../../api/client')>(),
  previewArtifact: mocks.preview,
}))
vi.mock('../../api/queries', () => ({
  useArtifactsDetail: (...args: unknown[]) => mocks.detail(...args),
  // 打开方式与本文件断言无关：探测不可用，入口不渲染。
  useOpenInApps: () => ({ data: { available: false, apps: [] } }),
}))
// 高亮要异步加载 shiki 语法包，与本文件断言无关：一律退回纯文本。
vi.mock('./useHighlight', () => ({ languageOf: () => 'text', useHighlight: () => null }))

function page(text: string, nextOffset: number | null) {
  return { kind: 'text' as const, text, next_offset: nextOffset, next_page: nextOffset === null ? null : 1,
    has_more: nextOffset !== null, total_pages: null }
}

function defer<T>() {
  let resolve: (value: T) => void = () => {}
  const promise = new Promise<T>((done) => { resolve = done })
  return { promise, resolve }
}

function detailOf(path: string) {
  return { data: page(path === 'a.txt' ? 'A-1' : 'B-1', 10), isPending: false, isError: false, error: null, refetch: vi.fn() }
}

const props = { workspaceId: 'w1', scope: 'workspace' as const, onClose: () => {} }

beforeEach(() => {
  vi.clearAllMocks()
  mocks.detail.mockImplementation((path: string) => detailOf(path))
})

/**
 * 换文件时上一份文件的在途分页响应必须作废（2026-11-06）。
 *
 * 反过来（旧响应照旧 `setExtra`）用户看到的是"A 的第二页拼在 B 的正文后面"。
 */
it('drops the in-flight page of the previous file after switching files', async () => {
  const first = defer<unknown>()
  const second = defer<unknown>()
  mocks.preview.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise)

  const view = render(<PreviewPane {...props} path="a.txt" />)
  expect(screen.getByText(/A-1/)).toBeVisible()
  fireEvent.click(screen.getByRole('button', { name: '加载更多' }))

  // 切到另一个文件时上一页还没回来：它回来时不能落进新文件的正文。
  view.rerender(<PreviewPane {...props} path="b.txt" />)
  await act(async () => { first.resolve(page('A-2', null)) })
  expect(screen.queryByText(/A-2/)).not.toBeInTheDocument()

  fireEvent.click(screen.getByRole('button', { name: '加载更多' }))
  await act(async () => { second.resolve(page('B-2', null)) })
  expect(screen.getByText(/B-1B-2/)).toBeVisible()
})
