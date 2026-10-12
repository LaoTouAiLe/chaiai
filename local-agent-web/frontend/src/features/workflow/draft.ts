import type { SubmissionContext } from '../../api/client'

export interface WorkflowDraft {
  name: 'free' | 'readonly_research' | 'code_change' | 'delegated_research'
  pattern: string
  keywords: string
  sourcePath: string
  testPaths: string
  expectedTestCount: string
  tasks: { scope: string; question: string }[]
  concurrency: 1 | 2
}

export const emptyWorkflowDraft: WorkflowDraft = {
  name: 'free', pattern: '', keywords: '', sourcePath: '', testPaths: '',
  expectedTestCount: '', tasks: [{ scope: '.', question: '' }], concurrency: 1,
}

const lines = (text: string) => text.split('\n').map((line) => line.trim()).filter(Boolean)

export function workflowSelection(draft: WorkflowDraft): SubmissionContext['workflow'] {
  switch (draft.name) {
    case 'free': return null
    case 'readonly_research': return { name: draft.name, request: { pattern: draft.pattern.trim(), keywords: lines(draft.keywords) } }
    case 'code_change': return { name: draft.name, request: {
      source_path: draft.sourcePath.trim(), test_paths: lines(draft.testPaths),
      expected_test_count: Number(draft.expectedTestCount), test_command: 'python -B -m unittest -v',
    } }
    case 'delegated_research': return { name: draft.name, request: {
      tasks: draft.tasks.map((task) => ({ scope: task.scope.trim(), question: task.question.trim() })),
      concurrency: draft.concurrency,
    } }
  }
}

export function workflowDraftValid(draft: WorkflowDraft): boolean {
  switch (draft.name) {
    case 'free': return true
    case 'readonly_research': return Boolean(draft.pattern.trim())
    case 'code_change': return Boolean(draft.sourcePath.trim() && lines(draft.testPaths).length
      && Number.isInteger(Number(draft.expectedTestCount)) && Number(draft.expectedTestCount) > 0)
    case 'delegated_research': return draft.tasks.every((task) => Boolean(task.scope.trim() && task.question.trim()))
  }
}

