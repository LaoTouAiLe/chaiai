import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { renderHook, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { expect, it, vi } from 'vitest'
import { useSearchWorkspaceFiles } from '../../api/queries'

const search = vi.hoisted(() => vi.fn())
vi.mock('../../api/client', async importOriginal => ({
  ...(await importOriginal<typeof import('../../api/client')>()), searchWorkspaceFiles: search,
}))

it('uses the selected workspace and isolates cached results for identical searches', async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  search.mockImplementation(async (_query: string, workspaceId: string) => ({ entries: [{ name: `${workspaceId}.ts` }] }))
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>
  const { result, rerender } = renderHook(({ workspaceId }) => useSearchWorkspaceFiles('main', workspaceId), {
    wrapper, initialProps: { workspaceId: 'first' },
  })
  await waitFor(() => expect(result.current.data?.entries?.[0]?.name).toBe('first.ts'))
  rerender({ workspaceId: 'second' })
  expect(result.current.data).toBeUndefined()
  await waitFor(() => expect(result.current.data?.entries?.[0]?.name).toBe('second.ts'))
  expect(search.mock.calls).toEqual([['main', 'first'], ['main', 'second']])
  client.clear()
})
