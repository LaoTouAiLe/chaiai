import { describe, expect, it } from 'vitest'

import type { components } from '../../api/schema.gen'
import { KIND_LABEL, flattenChanges, splitPath, toChangesOverview, toFileDiffView } from './adapters'

type Schemas = components['schemas']

function overview(files: Schemas['FileChangeSummaryView'][]): Schemas['SessionChangesView'] {
  return {
    session_id: 's1',
    turns: [{ run_id: 'run-1', started_at: '2026-10-21T03:00:00Z', files }],
    total_files: files.length,
    additions: files.reduce((sum, file) => sum + file.additions, 0),
    deletions: files.reduce((sum, file) => sum + file.deletions, 0),
  }
}

function file(overrides: Partial<Schemas['FileChangeSummaryView']>): Schemas['FileChangeSummaryView'] {
  return {
    path: 'a.ts',
    kind: 'modify',
    additions: 1,
    deletions: 1,
    edits: 1,
    last_at: '2026-10-21T03:01:00Z',
    has_baseline: true,
    ...overrides,
  }
}

describe('toChangesOverview', () => {
  it('keeps one row per file and carries the accumulated counts', () => {
    const view = toChangesOverview(
      overview([file({ path: 'scenarios.ts', additions: 4, deletions: 4, edits: 4 })]),
    )
    expect(view.turns[0]?.files).toHaveLength(1)
    expect(view.turns[0]?.files[0]).toMatchObject({
      path: 'scenarios.ts',
      edits: 4,
      additions: 4,
      deletions: 4,
    })
    expect(view.totalFiles).toBe(1)
  })

  it('numbers turns from the oldest so "第 1 轮" matches the user memory', () => {
    const view = toChangesOverview({
      session_id: 's1',
      turns: [
        { run_id: 'new', started_at: null, files: [file({})] },
        { run_id: 'old', started_at: null, files: [file({})] },
      ],
      total_files: 1,
      additions: 1,
      deletions: 1,
    })
    expect(view.turns.map((turn) => turn.label)).toEqual(['第 2 轮', '第 1 轮'])
  })

  it('keeps the reason when a file has no baseline', () => {
    const view = toChangesOverview(
      overview([
        file({
          path: 'new.ts',
          kind: 'create',
          has_baseline: false,
          unavailable_reason: '新建文件，没有改前内容',
        }),
      ]),
    )
    expect(view.turns[0]?.files[0]?.hasBaseline).toBe(false)
    expect(view.turns[0]?.files[0]?.unavailableReason).toBe('新建文件，没有改前内容')
  })

  it('returns an empty overview when the query has no data yet', () => {
    expect(toChangesOverview(undefined).turns).toEqual([])
  })

  it('labels the three kinds in Chinese', () => {
    expect(KIND_LABEL.create).toBe('新增')
    expect(KIND_LABEL.modify).toBe('修改')
    expect(KIND_LABEL.delete).toBe('删除')
  })
})

describe('flattenChanges', () => {
  it('merges the same file across turns into one row with accumulated counts', () => {
    const view = toChangesOverview({
      session_id: 's1',
      turns: [
        // 最新在前
        { run_id: 'r2', started_at: null, files: [file({ path: 'a.ts', additions: 3, deletions: 0, edits: 2 })] },
        { run_id: 'r1', started_at: null, files: [file({ path: 'a.ts', additions: 1, deletions: 1, edits: 1 })] },
      ],
      total_files: 1,
      additions: 4,
      deletions: 1,
    })
    const flat = flattenChanges(view)
    expect(flat).toHaveLength(1)
    expect(flat[0]).toMatchObject({ path: 'a.ts', additions: 4, deletions: 1, edits: 3 })
  })

  it('keeps the newest kind for a file that was created and then modified', () => {
    const view = toChangesOverview({
      session_id: 's1',
      turns: [
        { run_id: 'r2', started_at: null, files: [file({ path: 'new.ts', kind: 'modify' })] },
        { run_id: 'r1', started_at: null, files: [file({ path: 'new.ts', kind: 'create' })] },
      ],
      total_files: 1,
      additions: 2,
      deletions: 2,
    })
    expect(flattenChanges(view)[0]?.kind).toBe('modify')
  })
})

describe('splitPath', () => {
  it('splits the directory prefix from the file name', () => {
    expect(splitPath('frontend/src/app/LiveApp.tsx')).toEqual({
      dir: 'frontend/src/app/',
      name: 'LiveApp.tsx',
    })
  })

  it('leaves a bare file name without a directory', () => {
    expect(splitPath('README.md')).toEqual({ dir: '', name: 'README.md' })
  })
})

describe('toFileDiffView', () => {
  it('maps snake_case line numbers to the view model DiffCard expects', () => {
    const view = toFileDiffView({
      path: 'a.ts',
      additions: 1,
      deletions: 1,
      truncated: false,
      has_baseline: true,
      hunks: [
        {
          header: '@@ -1,2 +1,2 @@',
          lines: [
            { kind: 'context', old_line: 1, new_line: 1, text: 'keep' },
            { kind: 'del', old_line: 2, new_line: null, text: 'old' },
            { kind: 'add', old_line: null, new_line: 2, text: 'new' },
          ],
        },
      ],
    })
    expect(view.hunks[0]?.lines[1]).toEqual({ kind: 'del', oldLine: 2, newLine: null, text: 'old' })
    expect(view.hunks[0]?.lines[2]).toEqual({ kind: 'add', oldLine: null, newLine: 2, text: 'new' })
  })
})
