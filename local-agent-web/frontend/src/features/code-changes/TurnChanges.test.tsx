import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, expect, it, vi } from 'vitest'
import { TurnChanges } from './TurnChanges'

const mocks = vi.hoisted(() => ({ restore: vi.fn() }))
vi.mock('../../api/queries', () => ({
  useSessionChanges: () => ({ data: { turns: [{ run_id: 'run-1', files: [{
    path: 'target.py', kind: 'modify', additions: 1, deletions: 1, edits: 1,
    last_at: '2026-10-29T12:00:00Z', has_baseline: true,
  }] }] } }),
  useSessionChangeDiff: () => ({ data: undefined, isLoading: false, isError: false }),
  useRestoreChanges: () => ({ mutate: mocks.restore, data: undefined, error: null,
    isPending: false, reset: vi.fn() }),
}))

beforeEach(() => mocks.restore.mockReset())

it.each(['run-1', 'run-1:user', 'tool:run-1:write'])('restores the run represented by message id %s', (turnId) => {
  render(<TurnChanges sessionId="session-1" turnId={turnId} selectedPath={null} onSelect={vi.fn()} />)
  fireEvent.click(screen.getByRole('button', { name: '还原' }))
  fireEvent.click(screen.getByRole('button', { name: '确认还原' }))
  expect(mocks.restore).toHaveBeenCalledWith(expect.objectContaining({
    scope: 'turn', run_id: 'run-1', force: false,
  }), expect.any(Object))
})
