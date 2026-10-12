import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { WorkflowControls } from './WorkflowControls'
import { emptyWorkflowDraft, workflowDraftValid, workflowSelection } from './draft'
import { WorkflowStatus } from './WorkflowStatus'
import { mapWorkflow } from './adapters'

describe('workflow controls', () => {
  it('uses explicit scopes and preserves the fixed test command', () => {
    expect(workflowSelection(emptyWorkflowDraft)).toBeNull()
    const read = { ...emptyWorkflowDraft, name: 'readonly_research' as const, pattern: ' docs/*.md ', keywords: '取消\n\n权限' }
    expect(workflowSelection(read)).toEqual({ name: 'readonly_research', request: { pattern: 'docs/*.md', keywords: ['取消', '权限'] } })
    const code = { ...emptyWorkflowDraft, name: 'code_change' as const, sourcePath: 'bounds.py', testPaths: 'test_bounds.py', expectedTestCount: '3' }
    expect(workflowSelection(code)).toEqual({ name: 'code_change', request: { source_path: 'bounds.py', test_paths: ['test_bounds.py'], expected_test_count: 3, test_command: 'python -B -m unittest -v' } })
    expect(workflowDraftValid(code)).toBe(true)
    expect(workflowDraftValid({ ...code, expectedTestCount: 'bad' })).toBe(false)
    expect(workflowDraftValid({ ...emptyWorkflowDraft, name: 'delegated_research' })).toBe(false)
  })

  it('provides a bounded list of child tasks and locks controls during execution', () => {
    const change = vi.fn()
    const draft = { ...emptyWorkflowDraft, name: 'delegated_research' as const }
    const { rerender } = render(<WorkflowControls draft={draft} onChange={change} disabled={false} />)
    fireEvent.click(screen.getByRole('button', { name: '添加子任务' }))
    expect(change.mock.calls[0]?.[0].tasks).toHaveLength(2)
    rerender(<WorkflowControls draft={{ ...draft, tasks: Array.from({ length: 3 }, () => ({ scope: '.', question: '核对' })) }} onChange={change} disabled />)
    expect(screen.queryByRole('button', { name: '添加子任务' })).toBeNull()
    expect(screen.getByLabelText('子任务 1 目录')).toBeDisabled()
  })

  it('shows verified counters and failure reasons without turning a child into parent success', () => {
    render(<WorkflowStatus view={mapWorkflow({ name: 'delegated_research', run_id: 'parent', phase: 'failed',
      steps: 2, tool_calls: 4, model_calls: 1, model_tokens_reserved: 3000, evidence_count: 2,
      error: '父任务证据核对失败', children: [{ child_id: 'child', scope: 'docs', status: 'completed', evidence_count: 2 }] })} />)
    expect(screen.getByLabelText('工作流进度')).toHaveTextContent('委派只读调查 · 未完成')
    expect(screen.getByRole('status')).toHaveTextContent('父任务证据核对失败')
    expect(screen.getByText('docs：已完成，证据 2')).toBeVisible()
    expect(screen.getByText(/Token 预留 3000/)).toBeVisible()
  })
})
