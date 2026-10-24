import { describe, expect, it } from 'vitest'
import { describeCron } from './cronDescribe'

function text(expression: string): string {
  const result = describeCron(expression)
  if (!result.ok) throw new Error(`expected ${expression} to parse, got: ${result.reason}`)
  return result.text
}

function reason(expression: string): string {
  const result = describeCron(expression)
  if (result.ok) throw new Error(`expected ${expression} to fail, got: ${result.text}`)
  return result.reason
}

describe('describeCron', () => {
  it('reads the common time shapes', () => {
    expect(text('* * * * *')).toBe('每分钟')
    expect(text('*/30 * * * *')).toBe('每 30 分钟')
    expect(text('0 * * * *')).toBe('每小时的第 0 分')
    expect(text('0 9 * * *')).toBe('每天 09:00')
    expect(text('0 9,18 * * *')).toBe('每天 09:00、18:00')
    expect(text('15,45 * * * *')).toBe('每小时的第 15、45 分')
  })

  it('reads the common date shapes', () => {
    expect(text('0 9 * * 1')).toBe('每周一 09:00')
    expect(text('0 9 * * 1-5')).toBe('工作日 09:00')
    expect(text('0 9 * * 0,6')).toBe('周末 09:00')
    expect(text('0 9 * * 7')).toBe('每周日 09:00')
    expect(text('0 0 1 * *')).toBe('每月 1 日 00:00')
    expect(text('0 0 1 1 *')).toBe('每年 1 月 1 日 00:00')
  })

  it('states the OR semantics when both day-of-month and day-of-week are set', () => {
    // cron 里这两个条件是"或"：漏掉这句话会让人以为必须同时满足。
    expect(text('0 0 1 * 1')).toContain('或')
    expect(text('0 0 1 * 1')).toContain('每周一')
  })

  it('rejects shapes a human would also call wrong', () => {
    expect(reason('0 9 * *')).toContain('5 段')
    expect(reason('0 25 * * *')).toContain('0–23')
    expect(reason('0 9 * * 8')).toContain('0–7')
    expect(reason('*/0 * * * *')).toContain('步长')
    expect(reason('')).toContain('还没填')
  })

  it('says plainly when it cannot read an extended syntax', () => {
    // 后端 croniter 认识 L / W / #，但页面不假装自己懂。
    expect(reason('0 9 * * L')).toContain('扩展写法')
    expect(reason('0 9 L * *')).toContain('扩展写法')
  })

  it('never turns a wildcard into a concrete time', () => {
    // `* * * * *` 说成"每小时的第 0 分"是最容易写错的一种：前者每分钟，后者每小时一次。
    expect(text('* * * * *')).not.toContain('0 分')
  })
})
