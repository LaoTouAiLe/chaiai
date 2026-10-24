import { describe, expect, it } from 'vitest'
import { ADVANCED, FIELDS } from './PreferenceSection'
import { SETTINGS_SEARCH_INDEX, filterSettings, settingElementId } from './settingsSearch'

describe('settingsSearch', () => {
  it('indexes every preference field plus the read-only advanced rows', () => {
    const fieldCount = Object.values(FIELDS).reduce((sum, fields) => sum + fields.length, 0)
    expect(SETTINGS_SEARCH_INDEX).toHaveLength(fieldCount + Object.keys(ADVANCED).length)
  })

  it('keeps same-named fields apart by section', () => {
    const ids = SETTINGS_SEARCH_INDEX
      .filter(hit => hit.key === 'enabled')
      .map(hit => settingElementId(hit.group, hit.key))
    expect(new Set(ids).size).toBe(ids.length)
    expect(ids).toContain('field-memory-enabled')
    expect(ids).toContain('field-embedding-enabled')
  })

  it('ranks an exact label match first', () => {
    const [first] = filterSettings(SETTINGS_SEARCH_INDEX, '拒绝的域名')
    expect(first?.label).toBe('拒绝的域名')
  })

  it('matches against the field note, not just the label', () => {
    const hits = filterSettings(SETTINGS_SEARCH_INDEX, '回收站')
    expect(hits.length).toBeGreaterThan(0)
    expect(hits[0]?.label).toContain('安全删除')
  })

  it('explains why a conditional field is not on screen', () => {
    const hit = SETTINGS_SEARCH_INDEX.find(item => item.group === 'attachments' && item.key === 'oss_bucket')
    expect(hit?.requirement).toContain('图片存储位置')
    expect(hit?.requirement).toContain('对象存储')
  })

  it('marks advanced rows as read-only', () => {
    const hit = SETTINGS_SEARCH_INDEX.find(item => item.group === 'advanced')
    expect(hit?.readOnly).toBe(true)
  })

  it('returns nothing for a query that matches no field', () => {
    expect(filterSettings(SETTINGS_SEARCH_INDEX, 'zzzz-no-such-setting')).toHaveLength(0)
  })
})
