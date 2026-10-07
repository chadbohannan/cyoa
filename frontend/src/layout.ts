// Auto layout for the story map: a tidy tree read off the graph's structure.
//
// A breadth-first walk from the start gives every card one parent — the first
// passage to reach it, so it sits at its shallowest depth, where a reader first
// meets it — and children are ordered left to right by choice order, so the map
// reads like the choice list. Merges and loops back up stay ordinary edges drawn
// across the tree. Passages the start cannot reach (orphans, proposal ghosts with
// no target) become their own trees to the right. d3's Reingold–Tilford `tree`
// packs subtrees so no two cards touch, and adding a leaf only shifts its
// neighbours, which keeps a growing run readable.

import { hierarchy, tree } from 'd3-hierarchy'

// The structure the layout depends on, nothing else: card text never moves a card.
export interface Shape {
  nodes: string[] // visible card ids, in a stable order (passages by id, then ghosts)
  edges: [string, string][] // parent → child, in each parent's choice order
  root: string | null // the start passage, when it is visible
}

export type Positions = Map<string, { x: number; y: number }>

const COL = 220 // card width (180) plus a gutter
const ROW = 190 // room for a card with badges plus the edge label between rows

type TreeNode = { id: string; children: TreeNode[] }

export function tidyLayout(shape: Shape): Positions {
  const out = new Map<string, string[]>()
  const inbound = new Set<string>()
  for (const [a, b] of shape.edges) {
    out.set(a, [...(out.get(a) ?? []), b])
    inbound.add(b)
  }
  const seen = new Set<string>()
  const trees: TreeNode[] = []
  const grow = (rootId: string) => {
    const root: TreeNode = { id: rootId, children: [] }
    seen.add(rootId)
    const queue = [root]
    for (const node of queue) {
      for (const next of out.get(node.id) ?? []) {
        if (seen.has(next)) continue
        seen.add(next)
        const child = { id: next, children: [] }
        node.children.push(child)
        queue.push(child)
      }
    }
    trees.push(root)
  }
  if (shape.root !== null && shape.nodes.includes(shape.root)) grow(shape.root)
  // then the unreachable: true orphans first, then whatever cycles remain
  for (const id of shape.nodes) if (!seen.has(id) && !inbound.has(id)) grow(id)
  for (const id of shape.nodes) if (!seen.has(id)) grow(id)

  // one invisible root over the forest, so d3 packs the trees side by side
  const forest = hierarchy<TreeNode>({ id: '', children: trees }, (n) => n.children)
  const laid = tree<TreeNode>()
    .nodeSize([COL, ROW])
    .separation((a, b) => (a.parent === b.parent ? (a.depth === 1 ? 1.5 : 1) : 1.2))(forest)
  const positions: Positions = new Map()
  for (const n of laid.descendants()) {
    if (n.depth === 0) continue
    // d3 centres each card on x; React Flow positions its top-left corner
    positions.set(n.data.id, { x: Math.round(n.x - 90), y: (n.depth - 1) * ROW })
  }
  return positions
}
