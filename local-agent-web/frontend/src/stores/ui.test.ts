import { beforeEach, describe, expect, it } from 'vitest'

import { useUiStore } from './ui'

beforeEach(() => {
  useUiStore.setState({ workspaceId: null, activeSessionId: null, drafts: {} })
})

describe('useUiStore', () => {
  it('keeps a draft per session instead of one shared draft', () => {
    const { setDraft } = useUiStore.getState()

    setDraft('s-1', '第一句')
    setDraft('s-2', '另一句')

    // 切走再回来不能丢草稿：执行中允许继续写（T30 裁定），两个会话的草稿互不覆盖。
    expect(useUiStore.getState().drafts).toEqual({ 's-1': '第一句', 's-2': '另一句' })
  })

  it('keeps an emptied draft as an empty string rather than deleting the key', () => {
    const { setDraft } = useUiStore.getState()

    setDraft('s-1', '写了一半')

    // 显式清空与"从没写过"是两回事：前者是用户的动作，后者是默认。用空串表达前者。
    setDraft('s-1', '')

    expect(useUiStore.getState().drafts['s-1']).toBe('')
  })

  it('tracks the selected workspace and session without touching server state', () => {
    const { setWorkspace, openSession } = useUiStore.getState()

    setWorkspace('ws-1')
    openSession('s-7')

    expect(useUiStore.getState().workspaceId).toBe('ws-1')
    expect(useUiStore.getState().activeSessionId).toBe('s-7')
    // 会话内容不在这里：它是服务端权威状态，只在 React Query 缓存里。
    expect(Object.keys(useUiStore.getState())).not.toContain('messages')
  })
})
