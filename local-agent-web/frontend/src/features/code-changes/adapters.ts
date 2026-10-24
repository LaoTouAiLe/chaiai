// 变更视图的适配层（2026-10-21）：DTO → 纯 view model，组件不认 `schema.gen` 的类型。
//
// 口径与对话里的工具卡片一致：一个文件被改多次时，按**文件**聚合、行数累计；
// 点开看的是**整文件** diff（首次改动前 → 现在），不是某一次调用的片段。

import type { FileDiffView } from '../../fixtures/types'
import type { components } from '../../api/schema.gen'

type Schemas = components['schemas']

export interface ChangedFileView {
  path: string
  kind: 'create' | 'modify' | 'delete'
  additions: number
  deletions: number
  /** 这个文件在这一组里被改了几次。 */
  edits: number
  lastAt: string
  hasBaseline: boolean
  unavailableReason: string | null
}

export interface TurnChangesView {
  key: string
  label: string
  time: string
  files: ChangedFileView[]
}

export interface ChangesOverview {
  turns: TurnChangesView[]
  totalFiles: number
  additions: number
  deletions: number
}

export const EMPTY_OVERVIEW: ChangesOverview = {
  turns: [],
  totalFiles: 0,
  additions: 0,
  deletions: 0,
}

export const KIND_LABEL: Record<ChangedFileView['kind'], string> = {
  create: '新增',
  modify: '修改',
  delete: '删除',
}

function timeOf(iso: string | null): string {
  if (iso === null) return ''
  const parsed = new Date(iso)
  if (Number.isNaN(parsed.getTime())) return ''
  return parsed.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
}

function toChangedFile(file: Schemas['FileChangeSummaryView']): ChangedFileView {
  return {
    path: file.path,
    kind: file.kind,
    additions: file.additions,
    deletions: file.deletions,
    edits: file.edits,
    lastAt: file.last_at,
    hasBaseline: file.has_baseline,
    unavailableReason: file.unavailable_reason ?? null,
  }
}

/**
 * 把服务端总览转成面板结构。
 *
 * 轮次编号按**时间顺序**给（后端按最新在前返回）：第一轮是"第 1 轮"，而不是"第 N 轮"——
 * 用户记忆里的"第一次改动"必须和编号对得上。
 */
export function toChangesOverview(
  dto: Schemas['SessionChangesView'] | undefined,
): ChangesOverview {
  if (dto === undefined) return EMPTY_OVERVIEW
  const turns = dto.turns ?? []
  const total = turns.length
  return {
    totalFiles: dto.total_files,
    additions: dto.additions,
    deletions: dto.deletions,
    turns: turns.map((turn, index) => ({
      key: turn.run_id ?? `turn-${String(index)}`,
      label: `第 ${String(total - index)} 轮`,
      time: timeOf(turn.started_at ?? null),
      files: (turn.files ?? []).map(toChangedFile),
    })),
  }
}

/** 从轮次 id 里取 `run_id`：快照消息 id 是 `<run_id>:user` / `tool:<run_id>:<call>`。 */
export function runIdOfTurn(turnId: string): string {
  const parts = turnId.split(':')
  return parts[0] === 'tool' && parts.length > 1 ? parts[1] : parts[0]
}

/** 某一轮的改动（对话流里的那条摘要用它）。找不到这一轮时返回 `null`。 */
export function toTurnChanges(
  dto: Schemas['SessionChangesView'] | undefined,
  turnId: string,
): { files: ChangedFileView[]; additions: number; deletions: number } | null {
  if (dto === undefined) return null
  const runId = runIdOfTurn(turnId)
  const turn = (dto.turns ?? []).find((item) => item.run_id === runId)
  if (turn === undefined) return null
  const files = (turn.files ?? []).map(toChangedFile)
  if (files.length === 0) return null
  return {
    files,
    additions: files.reduce((sum, file) => sum + file.additions, 0),
    deletions: files.reduce((sum, file) => sum + file.deletions, 0),
  }
}

/**
 * 会话维度的平铺总览：把多轮里的同一个文件合并成一行（行数与次数累加）。
 *
 * 后端按轮返回，是因为一轮一次"这次改了什么"；但用户先想知道的是"整个会话一共动了哪些文件"。
 */
export function flattenChanges(overview: ChangesOverview): ChangedFileView[] {
  const merged = new Map<string, ChangedFileView>()
  // `overview.turns` 是最新在前：先出现的记录才是这个文件最新的状态（kind / 是否有基线）。
  for (const turn of overview.turns) {
    for (const file of turn.files) {
      const previous = merged.get(file.path)
      if (previous === undefined) {
        merged.set(file.path, { ...file })
        continue
      }
      merged.set(file.path, {
        ...previous,
        additions: previous.additions + file.additions,
        deletions: previous.deletions + file.deletions,
        edits: previous.edits + file.edits,
      })
    }
  }
  return [...merged.values()]
}

/** 路径拆成"目录 + 文件名"：目录淡色、文件名正常，长路径才容易一眼扫到改的是哪个文件。 */
export function splitPath(path: string): { dir: string; name: string } {
  const index = path.lastIndexOf('/')
  if (index < 0) return { dir: '', name: path }
  return { dir: path.slice(0, index + 1), name: path.slice(index + 1) }
}

/** 整文件 diff → 复用 `DiffCard` 的 view model（组件本身不需要改动）。 */
export function toFileDiffView(dto: Schemas['FileChangeDiffView']): FileDiffView {
  return {
    path: dto.path,
    additions: dto.additions,
    deletions: dto.deletions,
    hunks: (dto.hunks ?? []).map((hunk) => ({
      header: hunk.header,
      lines: (hunk.lines ?? []).map((line) => ({
        kind: line.kind,
        oldLine: line.old_line ?? null,
        newLine: line.new_line ?? null,
        text: line.text,
      })),
    })),
  }
}
