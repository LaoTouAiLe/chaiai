import { describe, expect, it } from 'vitest'

import { QUOTE_LIMIT, composeQuotedMessage, quoteMarkdown, quotePreview } from './quote'

describe('追问引用（2026-10-26）', () => {
  it('逐行加 > ，空行写成 > ，引用块不会被空行截断', () => {
    expect(quoteMarkdown('第一段\n\n第二段')).toBe('> 第一段\n>\n> 第二段')
  })

  it('整条回答超上限时截断并注明；短文本（拖选那种）原样保留', () => {
    expect(quoteMarkdown('a'.repeat(QUOTE_LIMIT + 1))).toContain('…（引用已截断，完整内容见原文）')
    expect(quoteMarkdown('短的一段')).toBe('> 短的一段')
  })

  it('没有引用时原样返回草稿；有引用时引用在前、问题在后', () => {
    expect(composeQuotedMessage(null, '问题')).toBe('问题')
    expect(composeQuotedMessage('   ', '问题')).toBe('问题')
    expect(composeQuotedMessage('原文', '问题')).toBe('> 原文\n\n问题')
  })

  it('输入框上方的预览压成一行，超长加省略号', () => {
    expect(quotePreview('一行\n\n两行')).toBe('一行 两行')
    expect(quotePreview('x'.repeat(200))).toHaveLength(121)
  })
})
