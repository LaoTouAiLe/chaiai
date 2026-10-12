import { describe, expect, it } from 'vitest'
import { LINE_WINDOW, windowOf } from './lineWindow'

describe('windowOf', () => {
  it('gives back the whole body while it fits on one screen', () => {
    // 阈值以下必须是**恒等**：卡片 DOM 与视觉基线不变，全靠这一条（§19.3）。
    const rows = Array.from({ length: LINE_WINDOW }, (_, index) => index)
    expect(windowOf(rows, LINE_WINDOW)).toEqual(rows)
    expect(windowOf(rows, LINE_WINDOW * 3)).toEqual(rows)
  })

  it('cuts exactly at the window and never past the end', () => {
    const rows = Array.from({ length: 25 }, (_, index) => index)
    expect(windowOf(rows, LINE_WINDOW)).toHaveLength(LINE_WINDOW)
    expect(windowOf(rows, 0)).toEqual([])
    // 负数/非整数不该变成"从尾部取"这种意外语义（`slice` 的负索引是反着数的）。
    expect(windowOf(rows, -5)).toEqual([])
    expect(windowOf(rows, 3.7)).toHaveLength(3)
  })
})
