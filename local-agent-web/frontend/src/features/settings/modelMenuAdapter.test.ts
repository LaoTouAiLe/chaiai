import { describe, expect, it } from 'vitest'

import type { components } from '../../api/schema.gen'
import { parseSelectionVersion, selectionExpectation, toModelMenu } from './modelMenuAdapter'

type Snapshot = components['schemas']['SessionSnapshot']
type Connection = components['schemas']['ModelConnectionView']

const snapshot = (overrides: Partial<Snapshot> = {}): Snapshot => ({
  mode: 'agent',
  schema_version: 1,
  snapshot_id: 'snap-1',
  session_id: 's-1',
  workspace: { workspace_id: 'w-1', display_name: '工作区' },
  title: '测试',
  branch_id: 'main',
  branch_title: '原讨论',
  session_state: 'idle',
  created_at: '2026-09-27T00:00:00Z',
  answer_streaming: false,
  ...overrides,
})

const connection = (overrides: Partial<Connection> = {}): Connection => ({
  auth_mode: 'api_key',
  base_url: 'https://example.test/v1',
  config_version: '3',
  connection_id: 'mc-default',
  context_window_tokens: 128000,
  display_name: '默认连接',
  is_default: true,
  key_status: 'configured',
  max_tokens: 4000,
  model_id: 'model-a',
  provider: 'openai_compatible',
  retry_count: 1,
  status: 'verified',
  streaming: true,
  // 2026-10-16：逐连接的"支持图片输入"（生成类型里是必填，夹具照契约写全）。
  supports_vision: false,
  temperature: 0.7,
  timeout_seconds: 30,
  transport: 'https',
  ...overrides,
})

describe('live model menu mapping', () => {
  it('shows the effective default but keeps the session in follow-default mode', () => {
    const menu = toModelMenu(snapshot(), [connection()])
    expect(menu).toMatchObject({ selectedId: 'mc-default', followsDefault: true })
    expect(menu.nextTurnNotice).toContain('跟随默认模型')
    expect(selectionExpectation(snapshot())).toEqual({ connection_id: null, config_version: null })
  })

  it('shows an explicit next-turn choice without claiming to know the pinned running model', () => {
    const menu = toModelMenu(
      snapshot({ session_state: 'running', model_selection: { connection_id: 'mc-b', config_version: '7' } }),
      [connection(), connection({ connection_id: 'mc-b', display_name: '模型 B', is_default: false, config_version: '7' })],
    )
    expect(menu).toMatchObject({ selectedId: 'mc-b', followsDefault: false })
    expect(menu.nextTurnNotice).toContain('模型 B')
    expect(menu.pinnedLabel).toContain('连接版本已固定')
    expect(menu.pinnedLabel).not.toContain('模型 B')
    expect(selectionExpectation(snapshot({ model_selection: { connection_id: 'mc-b', config_version: '7' } })))
      .toEqual({ connection_id: 'mc-b', config_version: 7 })
  })

  it('never presents a missing explicit choice as the default', () => {
    const menu = toModelMenu(snapshot({ model_selection: { connection_id: 'retired', config_version: '1' } }), [connection()])
    expect(menu.selectedId).toBe('retired')
    expect(menu.nextTurnNotice).toContain('尚无可用模型')
  })

  it('rejects malformed or unsafe versions before a write', () => {
    expect(parseSelectionVersion('7')).toBe(7)
    for (const value of ['7x', '-1', '1.5', '01', '9007199254740992']) {
      expect(() => parseSelectionVersion(value)).toThrow('版本无效')
    }
  })
})
