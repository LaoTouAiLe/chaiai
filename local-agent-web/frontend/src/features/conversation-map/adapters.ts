import type { components } from '../../api/schema.gen'

export interface MapNode {
  id: string
  parentId: string | null
  originBranchId: string
  question: string
  answerPreview: string
  ordinal: number
  status: string
  canContinue: boolean
}
export interface MapBranch {
  id: string
  title: string
  tipId: string | null
  active: boolean
  position: number
}
export interface MapData {
  nodes: MapNode[]
  branches: MapBranch[]
  activeBranchId: string
  matchedIds: ReadonlySet<string>
}
const STATUS: Record<string, string> = {
  completed: '已完成', failed: '失败', blocked: '等待补充', running: '执行中',
  awaiting_approval: '等待审批', unknown: '历史状态未记录',
}
export function mapConversationMap(dto: components['schemas']['ConversationMapView']): MapData {
  return {
    activeBranchId: dto.active_branch_id,
    matchedIds: new Set(dto.matched_turn_ids),
    branches: dto.branches.map((b) => ({ id: b.branch_id, title: b.title,
      tipId: b.tip_turn_id ?? null, active: b.active, position: b.position })),
    nodes: dto.nodes.map((n) => ({ id: n.turn_id, parentId: n.parent_turn_id ?? null,
      originBranchId: n.origin_branch_id, question: n.question, answerPreview: n.answer_preview,
      ordinal: n.ordinal, status: STATUS[n.state] ?? '历史状态未记录', canContinue: n.can_continue })),
  }
}

export function ancestry(nodes: readonly MapNode[], tipId: string | null): Set<string> {
  const byId = new Map(nodes.map((n) => [n.id, n]))
  const path = new Set<string>()
  let cursor = tipId
  while (cursor !== null && !path.has(cursor)) {
    const node = byId.get(cursor)
    if (node === undefined) break
    path.add(cursor)
    cursor = node.parentId
  }
  return path
}

/** 展示排序仅影响兄弟分支；父关系始终来自后端，长链使用迭代避免递归溢出。 */
export function flattenMap(data: MapData, collapsed: ReadonlySet<string>, searching: boolean) {
  const rank = new Map(data.branches.map((b) => [b.id, b.position]))
  const children = new Map<string | null, MapNode[]>()
  for (const node of data.nodes) {
    const siblings = children.get(node.parentId) ?? []
    siblings.push(node)
    children.set(node.parentId, siblings)
  }
  for (const siblings of children.values()) siblings.sort((a, b) =>
    (rank.get(a.originBranchId) ?? 0) - (rank.get(b.originBranchId) ?? 0) || a.ordinal - b.ordinal)
  const visible = searching ? new Set([...data.matchedIds].flatMap((id) => [...ancestry(data.nodes, id)])) : null
  const stack = (children.get(null) ?? []).slice().reverse().map((node) => ({ node, depth: 0 }))
  const result: { node: MapNode; depth: number; hasChildren: boolean }[] = []
  while (stack.length) {
    const item = stack.pop()!
    if (visible !== null && !visible.has(item.node.id)) continue
    const next = children.get(item.node.id) ?? []
    result.push({ ...item, hasChildren: next.length > 0 })
    if (searching || !collapsed.has(item.node.id)) {
      for (const node of next.slice().reverse()) stack.push({ node, depth: item.depth + 1 })
    }
  }
  return result
}
