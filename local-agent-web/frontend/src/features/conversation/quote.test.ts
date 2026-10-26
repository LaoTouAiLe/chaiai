import { describe, expect, it } from 'vitest'

import { QUOTE_LIMIT, composeQuotedMessage, quoteMarkdown, quotePreview, splitQuotedMessage } from './quote'

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

describe('splitQuotedMessage（气泡渲染，2026-10-26）', () => {
  it('把开头的引用行拆出来，问题留成正文', () => {
    expect(splitQuotedMessage('> 第一段\n>\n> 第二段\n\n这是啥？')).toEqual({
      quote: '第一段\n\n第二段',
      question: '这是啥？',
    })
  })

  it('没有引用时原样返回，不做任何猜测', () => {
    expect(splitQuotedMessage('普通问题')).toEqual({ quote: null, question: '普通问题' })
  })

  it('正文中间出现的 > 不算引用：只认消息开头', () => {
    // 粘贴的代码、自带的引用块都在正文里，把它们当"这条消息引用了什么"会读错。
    expect(splitQuotedMessage('问题\n> 代码里的重定向')).toEqual({
      quote: null,
      question: '问题\n> 代码里的重定向',
    })
  })
})
