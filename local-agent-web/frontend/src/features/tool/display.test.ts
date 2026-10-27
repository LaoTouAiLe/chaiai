import { describe, expect, it } from 'vitest'

import { toolLabel } from './display'

/**
 * 工具行的名字（2026-10-27）。
 *
 * 名字表漏一条，整行就退化成兜底文案「执行操作」——常驻记忆的 `memory` 就是这么被漏掉的：
 * 后端有投影、有参数，页面上只显示"执行操作 / 暂无可展示的详情"（用户实拍反馈）。
 * 这里钉住"已登记的工具必须有自己的名字，且不返回兜底文案"。
 */
describe('toolLabel', () => {
  it('names the memory tool instead of falling back to the generic label', () => {
    expect(toolLabel('memory')).toBe('更新记忆')
  })

  it('keeps the generic label only for tools it does not know', () => {
    expect(toolLabel('some_future_tool')).toBe('执行操作')
  })

  it('never returns the fallback for a registered tool', () => {
    for (const name of [
      'read_file', 'write_file', 'run_shell', 'web_search', 'delegate_research', 'memory',
    ]) {
      expect(toolLabel(name)).not.toBe('执行操作')
    }
  })
})
