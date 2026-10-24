import { describe, expect, it } from 'vitest'
import type { MapData, MapNode } from './adapters'
import { CARD_HEIGHT, CARD_WIDTH, fitTree, layoutTree, zoomAt } from './layout'

function node(id: string, parentId: string | null, ordinal: number): MapNode {
  return { id, parentId, ordinal, originBranchId: 'main', question: id, answerPreview: '', status: '已完成', canContinue: true }
}
function data(nodes: MapNode[]): MapData {
  return { nodes, branches: [{ id: 'main', title: '主讨论', tipId: nodes.at(-1)?.id ?? null, active: true, position: 0 }], activeBranchId: 'main', matchedIds: new Set() }
}
describe('横向树与视野几何', () => {
  it('puts every child to the right and keeps sibling subtrees apart', () => {
    const input = data([node('root', null, 1), node('a', 'root', 2), node('a1', 'a', 3),
      node('a2', 'a', 4), node('b', 'root', 5), node('b1', 'b', 6)])
    const layout = layoutTree(input, new Set(), false)
    const cards = new Map(layout.cards.map((card) => [card.node.id, card]))
    for (const edge of layout.edges) {
      expect(cards.get(edge.childId)!.x).toBeGreaterThan(cards.get(edge.parentId)!.x + CARD_WIDTH)
    }
    for (const card of layout.cards) for (const other of layout.cards) {
      if (card.node.id !== other.node.id && card.x === other.x) expect(Math.abs(card.y - other.y)).toBeGreaterThanOrEqual(CARD_HEIGHT)
    }
    expect(layout.edges).toHaveLength(5)
    expect(input.nodes[4]!.parentId).toBe('root')
  })
  it('lays out a long chain without stack overflow and collapses only descendants', () => {
    const input = data(Array.from({ length: 2500 }, (_, i) => node(String(i), i ? String(i - 1) : null, i + 1)))
    const full = layoutTree(input, new Set(), false)
    expect(full.cards).toHaveLength(2500)
    expect(new Set(full.cards.map((card) => card.y)).size).toBe(1)
    const folded = layoutTree(input, new Set(['3']), false)
    expect(folded.cards).toHaveLength(4)
    expect(folded.edges).toHaveLength(3)
    expect(folded.cards[3]!.childCount).toBe(1)
  })
  it('keeps matching nodes and their ancestors while ignoring a collapsed path during search', () => {
    const input = data([node('root', null, 1), node('a', 'root', 2), node('b', 'root', 3)])
    input.matchedIds = new Set(['b'])
    expect(layoutTree(input, new Set(['root']), true).cards.map((card) => card.node.id)).toEqual(['root', 'b'])
  })
  it('keeps the point under the cursor stationary when zooming', () => {
    const view = { x: 80, y: -30, scale: 0.8 }
    const next = zoomAt(view, 1.4, 250, 180)
    expect((250 - next.x) / next.scale).toBeCloseTo((250 - view.x) / view.scale)
    expect((180 - next.y) / next.scale).toBeCloseTo((180 - view.y) / view.scale)
    expect(zoomAt(view, 9, 0, 0).scale).toBe(2)
    expect(zoomAt(view, 0, 0, 0).scale).toBe(0.05)
  })
  it('fits a branching scene inside the viewport and centers it', () => {
    const layout = layoutTree(data([node('root', null, 1), node('a', 'root', 2), node('b', 'root', 3)]), new Set(), false)
    const view = fitTree(layout, 900, 550)
    expect(view.x).toBeGreaterThanOrEqual(20)
    expect(view.y).toBeGreaterThanOrEqual(20)
    expect(view.x + layout.width * view.scale).toBeLessThanOrEqual(880)
    expect(view.y + layout.height * view.scale).toBeLessThanOrEqual(530)
  })
})
