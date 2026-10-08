import { flattenMap, type MapData, type MapNode } from './adapters'

export const CARD_WIDTH = 264
export const CARD_HEIGHT = 182
const COLUMN_GAP = 96
const ROW_GAP = 58
const PADDING = 48
export interface TreeCard { node: MapNode; x: number; y: number; childCount: number }
export interface TreeEdge { parentId: string; childId: string; path: string }
export interface TreeLayout { cards: TreeCard[]; edges: TreeEdge[]; width: number; height: number }
export interface MapViewport { x: number; y: number; scale: number }

/** 叶节点分配独立行，父卡片置于首尾子节点之间。整条长链使用迭代，不递归。 */
export function layoutTree(data: MapData, collapsed: ReadonlySet<string>, searching: boolean): TreeLayout {
  const visible = flattenMap(data, collapsed, searching)
  const visibleIds = new Set(visible.map((item) => item.node.id))
  const children = new Map<string, string[]>()
  const childCounts = new Map<string, number>()
  for (const node of data.nodes) if (node.parentId !== null) {
    childCounts.set(node.parentId, (childCounts.get(node.parentId) ?? 0) + 1)
  }
  for (const { node } of visible) if (node.parentId !== null && visibleIds.has(node.parentId)) {
    const siblings = children.get(node.parentId) ?? []
    siblings.push(node.id)
    children.set(node.parentId, siblings)
  }
  const centers = new Map<string, number>()
  let leaf = 0
  for (const { node } of visible) if (!children.has(node.id)) {
    centers.set(node.id, PADDING + CARD_HEIGHT / 2 + leaf++ * (CARD_HEIGHT + ROW_GAP))
  }
  for (let index = visible.length - 1; index >= 0; index--) {
    const item = visible[index]!
    const ids = children.get(item.node.id)
    if (ids?.length) centers.set(item.node.id, (centers.get(ids[0]!)! + centers.get(ids[ids.length - 1]!)!) / 2)
  }
  const cards = visible.map(({ node, depth }) => ({ node,
    x: PADDING + depth * (CARD_WIDTH + COLUMN_GAP),
    y: centers.get(node.id)! - CARD_HEIGHT / 2, childCount: childCounts.get(node.id) ?? 0,
  }))
  const byId = new Map(cards.map((card) => [card.node.id, card]))
  const edges = cards.flatMap((card): TreeEdge[] => {
    const parent = card.node.parentId === null ? undefined : byId.get(card.node.parentId)
    if (parent === undefined) return []
    const x1 = parent.x + CARD_WIDTH, y1 = parent.y + CARD_HEIGHT / 2
    const x2 = card.x, y2 = card.y + CARD_HEIGHT / 2
    const middle = (x1 + x2) / 2
    return [{ parentId: parent.node.id, childId: card.node.id,
      path: `M ${x1} ${y1} C ${middle} ${y1}, ${middle} ${y2}, ${x2} ${y2}` }]
  })
  return { cards, edges,
    width: Math.max(0, ...cards.map((card) => card.x + CARD_WIDTH)) + PADDING,
    height: (leaf ? leaf * (CARD_HEIGHT + ROW_GAP) - ROW_GAP : 0) + PADDING * 2,
  }
}

export function zoomAt(view: MapViewport, scale: number, x: number, y: number): MapViewport {
  const nextScale = Math.min(2, Math.max(0.05, scale))
  const ratio = nextScale / view.scale
  return { x: x - (x - view.x) * ratio, y: y - (y - view.y) * ratio, scale: nextScale }
}

export function fitTree(layout: TreeLayout, width: number, height: number): MapViewport {
  const scale = Math.min(1, Math.max(0.05, Math.min((width - 40) / layout.width, (height - 40) / layout.height)))
  return { x: (width - layout.width * scale) / 2, y: (height - layout.height * scale) / 2, scale }
}
