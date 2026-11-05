import { describe, expect, it } from 'vitest'
import { responsePresentation } from './responsePresentation'
import { shellScenario } from '../../fixtures/scenarios'
import type { MessageView } from '../../fixtures/types'

const tool = shellScenario.messages.find((row): row is Extract<MessageView, { kind: 'tool' }> => row.kind === 'tool')!
describe('response presentation', () => {
  it('splits final reasoning into the process without duplicating or moving tool calls', () => {
    const rows: MessageView[] = [tool, { kind: 'assistant', id: 'answer', text: '最终回答', reasoning: '真实思考', at: '', presentationPhase: 'response' }]
    const result = responsePresentation(rows)
    expect(result.rows.map(row => row.id)).toEqual([tool.id, 'answer:reasoning', 'answer'])
    expect(result.rows[1]).toMatchObject({ text: '', reasoning: '真实思考' })
    expect(result.rows[2]).toMatchObject({ text: '最终回答', reasoning: undefined })
    expect(rows[1]).toMatchObject({ text: '最终回答', reasoning: '真实思考' })
  })
  it('does not turn a trailing tool-decision message into the final answer', () => {
    const result = responsePresentation([tool, { kind: 'assistant', id: 'decision', text: '还要检查', at: '', presentationPhase: 'process' }])
    expect(result.answer).toBeUndefined()
  })
  it('keeps old messages compatible and never treats narration before a tool as final', () => {
    const narration: MessageView = { kind: 'assistant', id: 'old', text: '读取文件', at: '' }
    expect(responsePresentation([narration, tool]).answer).toBeUndefined()
    expect(responsePresentation([tool, narration]).answer).toEqual(narration)
    expect(responsePresentation([narration]).rows).toEqual([narration])
  })
})
