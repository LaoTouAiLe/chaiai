import type { MessageView } from '../../fixtures/types'

/** A display projection only: tool order and persisted model messages stay untouched. */
export function responsePresentation(rows: readonly MessageView[]) {
  const lastTool = rows.map(row => row.kind).lastIndexOf('tool')
  let answer: Extract<MessageView, { kind: 'assistant' }> | undefined
  for (let index = rows.length - 1; index > lastTool; index--) {
    const row = rows[index]
    if (row.kind === 'assistant' && row.presentationPhase !== 'process' && row.text.trim() !== '') { answer = row; break }
  }
  if (!answer || lastTool < 0 || !answer.reasoning) return { rows: [...rows], answer }
  return {
    answer,
    rows: rows.flatMap(row => row === answer ? [
      { ...answer, id: `${answer.id}:reasoning`, text: '', presentationPhase: 'process' as const },
      { ...answer, reasoning: undefined, reasoningTruncated: undefined, reasoningRunning: false },
    ] : [row]),
  }
}
