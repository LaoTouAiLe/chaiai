// 只读的工作流状态投影（§18.5）。
//
// 2026-10-13：输入区的「执行方式」下拉撤下（三类确定性工作流不再由用户手选），但**只读投影保留**——
// 通过 API 或评测入口发起的工作流仍要在页面上如实呈现阶段、计数与失败原因。

import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { WorkflowStatus } from './WorkflowStatus'
import { mapWorkflow } from './adapters'

describe('workflow status', () => {
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
