import { beforeEach, describe, expect, it } from 'vitest'

import { OUTBOX_LIMIT, useOutbox } from './outbox'

beforeEach(() => {
  useOutbox.getState().reset()
})

describe('待发队列（2026-10-28）', () => {
  it('编辑保留标识和排队位置，拒绝空文本与已发送条目', () => {
    const store = useOutbox.getState()
    store.enqueue('s1', '原文')
    store.enqueue('s1', '后一条')
    store.enqueue('s2', '另一会话')
    const first = useOutbox.getState().bySession.s1[0]
    expect(store.edit('s1', first.id, ' 修改后 ')).toBe(true)
    expect(useOutbox.getState().bySession.s1[0]).toEqual({ id: first.id, text: '修改后' })
    expect(useOutbox.getState().bySession.s1[1].text).toBe('后一条')
    expect(useOutbox.getState().bySession.s2[0].text).toBe('另一会话')
    expect(store.edit('s1', first.id, '  ')).toBe(false)
    store.shift('s1')
    expect(store.edit('s1', first.id, '迟到修改')).toBe(false)
  })
  it('入队按顺序排，空文本不接受', () => {
    const outbox = useOutbox.getState()

    expect(outbox.enqueue('s1', '第一条')).toBe(true)
    expect(outbox.enqueue('s1', '   ')).toBe(false)
    expect(outbox.enqueue('s1', '第二条')).toBe(true)

    expect(useOutbox.getState().bySession.s1?.map((item) => item.text)).toEqual(['第一条', '第二条'])
  })

  it('超过上限就拒绝，不静默丢掉输入', () => {
    for (let index = 0; index < OUTBOX_LIMIT; index += 1) {
      expect(useOutbox.getState().enqueue('s1', `第 ${index} 条`)).toBe(true)
    }
    expect(useOutbox.getState().enqueue('s1', '溢出')).toBe(false)
    expect(useOutbox.getState().bySession.s1).toHaveLength(OUTBOX_LIMIT)
  })

  it('可以上移、下移；到边界时不动', () => {
    const outbox = useOutbox.getState()
    outbox.enqueue('s1', 'a')
    outbox.enqueue('s1', 'b')
    outbox.enqueue('s1', 'c')
    const [a, b, c] = useOutbox.getState().bySession.s1 ?? []
    expect(a && b && c).toBeTruthy()
    if (a === undefined || b === undefined || c === undefined) return

    useOutbox.getState().move('s1', c.id, 'up')
    expect(useOutbox.getState().bySession.s1?.map((item) => item.text)).toEqual(['a', 'c', 'b'])

    useOutbox.getState().move('s1', a.id, 'up')
    expect(useOutbox.getState().bySession.s1?.map((item) => item.text)).toEqual(['a', 'c', 'b'])

    useOutbox.getState().move('s1', b.id, 'down')
    expect(useOutbox.getState().bySession.s1?.map((item) => item.text)).toEqual(['a', 'c', 'b'])
  })

  it('删除只删指定那条，其他会话不受影响', () => {
    const outbox = useOutbox.getState()
    outbox.enqueue('s1', 'a')
    const queued = useOutbox.getState().bySession.s1 ?? []
    const first = queued[0]
    expect(first).toBeTruthy()
    if (first === undefined) return
    outbox.enqueue('s2', '别的会话')
    outbox.remove('s1', first.id)

    expect(useOutbox.getState().bySession.s1).toEqual([])
    expect(useOutbox.getState().bySession.s2?.map((item) => item.text)).toEqual(['别的会话'])
  })

  it('shift 取队首并摘掉；restore 放回队首（发失败时不能丢消息）', () => {
    const outbox = useOutbox.getState()
    outbox.enqueue('s1', 'a')
    outbox.enqueue('s1', 'b')

    const first = useOutbox.getState().shift('s1')
    expect(first?.text).toBe('a')
    expect(useOutbox.getState().bySession.s1?.map((item) => item.text)).toEqual(['b'])

    if (first === null) return
    useOutbox.getState().restore('s1', first)
    expect(useOutbox.getState().bySession.s1?.map((item) => item.text)).toEqual(['a', 'b'])

    // 同一 id 重复放回不会变成两条
    useOutbox.getState().restore('s1', first)
    expect(useOutbox.getState().bySession.s1).toHaveLength(2)
  })

  it('暂停/恢复只影响该会话；reset 清空队列与暂停标记', () => {
    useOutbox.getState().enqueue('s1', 'a')
    useOutbox.getState().pause('s1')
    expect(useOutbox.getState().paused.s1).toBe(true)
    expect(useOutbox.getState().paused.s2).toBeUndefined()

    useOutbox.getState().resume('s1')
    expect(useOutbox.getState().paused.s1).toBe(false)

    useOutbox.getState().pause('s1')
    useOutbox.getState().reset('s1')
    expect(useOutbox.getState().bySession.s1).toEqual([])
    expect(useOutbox.getState().paused.s1).toBe(false)
  })
})
