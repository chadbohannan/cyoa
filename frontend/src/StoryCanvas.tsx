// The editable story map: passages as nodes, choices as edges. With auto layout
// on (the default) positions are derived from the graph's structure (layout.ts)
// and cards cannot be dragged; with it off they come from the passage rows
// (pos_x / pos_y) and are persisted on drag stop. Everything else the canvas
// does is a call into the story store.

import { useCallback, useEffect, useMemo, useRef, useState, type MouseEvent as ReactMouseEvent } from 'react'
import {
  Background,
  Controls,
  Handle,
  MarkerType,
  MiniMap,
  Position,
  ReactFlow,
  ReactFlowProvider,
  useEdgesState,
  useNodesState,
  useReactFlow,
  type Edge,
  type FinalConnectionState,
  type Node,
  type NodeProps,
  type OnConnect,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { api, type Choice, type Passage, type Problems, type Proposal } from './api'
import type { StoryStore } from './useStory'
import { confirmDelete, confirmPrune } from './prune'
import { tidyLayout, type Shape } from './layout'

export type Selection =
  | { kind: 'passage'; id: number }
  | { kind: 'choice'; id: number }
  | { kind: 'proposal'; id: number }
  | null

type PassageData = {
  title: string
  preview: string
  isStart: boolean
  isEnding: boolean
  isUnfinished: boolean // no outgoing choices, but not decided as an ending either
  reviewed: boolean
  dim: boolean // search active and this card does not match
  unreachable: boolean
  dangling: number // outgoing choices with no target (problems overlay on)
  mergeWith: number // other passages sharing its scene (problems overlay on)
  proposals: number // pending edits to this passage
  notes: number // pending canon_check notes
  activity: string | null // what a run is doing to this passage right now
  collapsed: Subtree | null // this card stands in for a hidden subtree
}
type PassageNode = Node<PassageData, 'passage'>

// A proposed passage that does not exist yet: drawn dashed, selectable, not connectable.
type GhostData = { title: string; preview: string }
type GhostNode = Node<GhostData, 'ghost'>
type MapNode = PassageNode | GhostNode

function GhostCard({ data, selected }: NodeProps<GhostNode>) {
  return (
    <div className={['passage-card ghost', selected && 'selected'].filter(Boolean).join(' ')}>
      <Handle type="target" position={Position.Top} isConnectable={false} />
      <div className="passage-card-title">
        <span className="badge">proposed</span> {data.title || <em>untitled</em>}
      </div>
      {data.preview && <div className="passage-card-preview">{data.preview}</div>}
    </div>
  )
}

const ACTIVITY_LABEL: Record<string, string> = { check: 'checking canon…', choices: 'writing choices…', passage: 'writing a passage…' }

function PassageCard({ data, selected }: NodeProps<PassageNode>) {
  const cls = ['passage-card', data.isStart && 'start', selected && 'selected', data.dim && 'dim', data.unreachable && 'unreachable',
    data.activity && 'active', data.collapsed && 'collapsed']
    .filter(Boolean)
    .join(' ')
  return (
    <div className={cls}>
      <Handle type="target" position={Position.Top} />
      <div className="passage-card-title">
        {data.isStart && <span title="start passage">▶ </span>}
        {data.title || <em>untitled</em>}
        {data.isEnding && <span className="badge">ending</span>}
      </div>
      {data.preview && <div className="passage-card-preview">{data.preview}</div>}
      {data.unreachable && <div className="passage-card-problem">unreachable</div>}
      {data.isUnfinished && <div className="passage-card-problem" title="no choices yet, and not marked as an ending">unfinished</div>}
      {data.dangling > 0 && <div className="passage-card-problem">{data.dangling} dangling choice{data.dangling > 1 ? 's' : ''}</div>}
      {data.mergeWith > 0 && <div className="passage-card-merge" title="same time and location as another passage">⧉ merge candidate ×{data.mergeWith + 1}</div>}
      {data.proposals > 0 && <div className="passage-card-proposal">✎ {data.proposals} proposed edit{data.proposals > 1 ? 's' : ''}</div>}
      {data.notes > 0 && <div className="passage-card-problem">⚠ {data.notes} canon note{data.notes > 1 ? 's' : ''}</div>}
      {data.activity && <div className="passage-card-activity">✍ {ACTIVITY_LABEL[data.activity] ?? data.activity}</div>}
      {data.collapsed && (
        <div className="passage-card-collapsed" title="right-click → Expand subtree">
          ▸ {data.collapsed.hidden} hidden · depth {data.collapsed.depth} · {data.collapsed.reviewed} reviewed · {data.collapsed.agent} agent
        </div>
      )}
      <Handle type="source" position={Position.Bottom} />
    </div>
  )
}

const nodeTypes = { passage: PassageCard, ghost: GhostCard }

type Menu = { x: number; y: number; passageId: number }

// Where a proposal would put a new passage, or null when it creates none.
function ghostFor(q: Proposal): { pos: { x: number; y: number }; data: GhostData } | null {
  const pl = q.payload
  if (q.kind === 'new_passage') {
    return { pos: { x: pl.pos_x ?? 0, y: pl.pos_y ?? 0 }, data: { title: pl.title ?? '', preview: (pl.body ?? '').split('\n')[0] } }
  }
  if (q.kind === 'new_choice' && pl.new_target) {
    return { pos: { x: pl.new_target.pos_x ?? 0, y: pl.new_target.pos_y ?? 0 }, data: { title: pl.new_target.title, preview: '' } }
  }
  return null
}

// The edge a proposal would add, from its target_passage.
function edgeFor(q: Proposal, choices: Choice[], ghostId: string | null): { target: string; label: string } | null {
  const pl = q.payload
  if (q.kind === 'new_passage' && ghostId) {
    const via = choices.find((c) => c.id === q.target_choice)
    return { target: ghostId, label: via?.text ?? '' }
  }
  if (q.kind === 'new_choice') {
    const target = pl.to_passage != null ? String(pl.to_passage) : ghostId
    return target ? { target, label: pl.text ?? '' } : null
  }
  return null
}

// --- subtree collapse ----------------------------------------------------------
//
// A collapsed passage hides everything below it that is reachable *only*
// through it: nodes the rest of the map can still reach stay drawn, so a
// collapsed card never hides a passage another branch links to. The frontier
// runs can produce thousands of passages, which no map can draw in full.

type Subtree = { hidden: number; depth: number; reviewed: number; agent: number }

function outgoingMap(choices: Choice[]) {
  const out = new Map<number, number[]>()
  for (const c of choices) if (c.to_passage !== null) out.set(c.from_passage, [...(out.get(c.from_passage) ?? []), c.to_passage])
  return out
}

// Passages hidden by the collapsed set, and the summary each collapsed root shows.
function collapseSubtrees(passages: Passage[], choices: Choice[], startId: number | null, collapsed: Set<number>) {
  const out = outgoingMap(choices)
  const inbound = new Set(choices.filter((c) => c.to_passage !== null).map((c) => c.to_passage as number))
  // everything still reachable without stepping past a collapsed root
  const visible = new Set<number>()
  const sources = passages.filter((p) => p.id === startId || !inbound.has(p.id)).map((p) => p.id)
  const queue = [...sources]
  for (const id of queue) {
    if (visible.has(id)) continue
    visible.add(id)
    if (!collapsed.has(id)) for (const next of out.get(id) ?? []) queue.push(next)
  }
  const hidden = new Set<number>()
  const summaries = new Map<number, Subtree>()
  const byId = new Map(passages.map((p) => [p.id, p]))
  for (const root of collapsed) {
    if (!byId.has(root)) continue
    const mine: number[] = []
    const depthOf = new Map<number, number>([[root, 0]])
    const q = [root]
    for (const id of q) {
      for (const next of out.get(id) ?? []) {
        if (visible.has(next) || depthOf.has(next)) continue
        depthOf.set(next, depthOf.get(id)! + 1)
        mine.push(next)
        q.push(next)
      }
    }
    mine.forEach((id) => hidden.add(id))
    summaries.set(root, {
      hidden: mine.length,
      depth: Math.max(0, ...mine.map((id) => depthOf.get(id)!)),
      reviewed: mine.filter((id) => byId.get(id)!.reviewed).length,
      agent: mine.filter((id) => byId.get(id)!.provenance === 'agent').length,
    })
  }
  return { hidden, summaries }
}

const collapsedKey = (storyId: number) => `cyoa.collapsed.${storyId}`

function useCollapsed(storyId: number) {
  const [collapsed, setCollapsed] = useState<Set<number>>(() => {
    try { return new Set(JSON.parse(localStorage.getItem(collapsedKey(storyId)) ?? '[]') as number[]) } catch { return new Set() }
  })
  const toggle = useCallback((id: number) => {
    setCollapsed((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      try { localStorage.setItem(collapsedKey(storyId), JSON.stringify([...next])) } catch { /* private mode */ }
      return next
    })
  }, [storyId])
  return { collapsed, toggle }
}

// Auto layout is remembered per story in this browser, on unless switched off.
const autoKey = (storyId: number) => `cyoa.autolayout.${storyId}`

function useAutoLayout(storyId: number) {
  const [auto, setAutoState] = useState<boolean>(() => {
    try { return localStorage.getItem(autoKey(storyId)) !== 'off' } catch { return true }
  })
  const setAuto = useCallback((on: boolean) => {
    setAutoState(on)
    try { localStorage.setItem(autoKey(storyId), on ? 'on' : 'off') } catch { /* private mode */ }
  }, [storyId])
  return { auto, setAuto }
}

// New choices go after a passage's existing ones.
function nextSortOrder(choices: Choice[], from: number) {
  const mine = choices.filter((c) => c.from_passage === from)
  return mine.length ? Math.max(...mine.map((c) => c.sort_order)) + 1 : 0
}

type Props = {
  store: StoryStore
  selection: Selection
  onSelect: (s: Selection) => void
  onPlayFrom: (passageId: number) => void
  onExpandFrom: (passageId: number) => void
}

// The problems overlay is fetched from the backend (which owns the semantics)
// and refreshed after every graph change while it is switched on.
function useProblems(store: StoryStore, enabled: boolean) {
  const { storyId, graph } = store
  const [problems, setProblems] = useState<Problems | null>(null)
  useEffect(() => {
    if (!enabled || !graph) return
    let live = true
    const t = window.setTimeout(() => api.problems(storyId).then((p) => live && setProblems(p)).catch(() => {}), 150)
    return () => {
      live = false
      window.clearTimeout(t)
    }
  }, [storyId, graph, enabled])
  return enabled ? problems : null
}

function Canvas({ store, selection, onSelect, onPlayFrom, onExpandFrom }: Props) {
  const { graph, activity } = store
  const { collapsed, toggle: toggleCollapsed } = useCollapsed(store.storyId)
  const { auto, setAuto } = useAutoLayout(store.storyId)
  const { screenToFlowPosition } = useReactFlow()
  const wrapper = useRef<HTMLDivElement>(null)
  const [menu, setMenu] = useState<Menu | null>(null)
  const [query, setQuery] = useState('')
  const [showProblems, setShowProblems] = useState(false)
  const problems = useProblems(store, showProblems)
  // Multi-selection (shift+click / shift+drag) is how a run is scoped.
  // React Flow owns which nodes are selected (so shift-click and box select
  // work); `scope` mirrors that for the run buttons. The inspector selection is
  // pushed into React Flow only when it changes, see the effect below.
  const [scope, setScope] = useState<number[]>([])
  const onSelectionChange = useCallback(({ nodes: sel }: { nodes: Node[] }) => {
    const ids = sel.filter((n) => n.type === 'passage').map((n) => Number(n.id)).sort((a, b) => a - b)
    setScope((prev) => (prev.length === ids.length && prev.every((x, i) => x === ids[i]) ? prev : ids))
  }, [])
  const hasWorld = (graph?.lore.length ?? 0) > 0
  const canonScope = scope.length > 1 ? scope : []
  const checking = store.runs.some((r) => r.workflow === 'canon_check' && r.status === 'running')

  const { initialNodes, initialEdges, shape } = useMemo(() => {
    if (!graph) return { initialNodes: [] as MapNode[], initialEdges: [] as Edge[], shape: { nodes: [], edges: [], root: null } as Shape }
    const outgoing = new Set(graph.choices.map((c) => c.from_passage))
    const q = query.trim().toLowerCase()
    const matches = (p: { title: string; body: string }) => !q || p.title.toLowerCase().includes(q) || p.body.toLowerCase().includes(q)
    const unreachable = new Set(problems?.unreachable ?? [])
    const dangling = new Set(problems?.dangling ?? [])
    const undeclared = new Set(problems?.undeclared.map((u) => u.choice_id) ?? [])
    const mergeWith = new Map<number, number>()
    for (const m of problems?.merge_candidates ?? []) for (const id of m.passage_ids) mergeWith.set(id, m.passage_ids.length - 1)
    const { hidden, summaries } = collapseSubtrees(graph.passages, graph.choices, graph.story.start_passage_id, collapsed)
    const initialNodes: MapNode[] = graph.passages.filter((p) => !hidden.has(p.id)).map((p) => ({
      id: String(p.id),
      type: 'passage',
      position: { x: p.pos_x, y: p.pos_y },
      data: {
        title: p.title,
        preview: p.body.split('\n').filter(Boolean).slice(0, 2).join(' '),
        isStart: graph.story.start_passage_id === p.id,
        isEnding: p.is_ending,
        isUnfinished: !outgoing.has(p.id) && !p.is_ending,
        reviewed: p.reviewed,
        dim: !matches(p),
        unreachable: unreachable.has(p.id),
        dangling: graph.choices.filter((c) => c.from_passage === p.id && dangling.has(c.id)).length,
        mergeWith: mergeWith.get(p.id) ?? 0,
        proposals: graph.proposals.filter((q) => q.kind === 'edit_passage' && q.target_passage === p.id).length,
        notes: graph.proposals.filter((q) => q.kind === 'note' && q.target_passage === p.id).length,
        activity: activity[p.id] ?? null,
        collapsed: summaries.get(p.id) ?? null,
      },
    }))
    const initialEdges: Edge[] = graph.choices
      .filter((c) => c.to_passage !== null && !hidden.has(c.from_passage) && !hidden.has(c.to_passage))
      .map((c) => ({
        id: `c${c.id}`,
        source: String(c.from_passage),
        target: String(c.to_passage),
        label: c.text,
        className: undeclared.has(c.id) ? 'problem' : undefined,
        markerEnd: { type: MarkerType.ArrowClosed },
      }))
    // what auto layout reads: visible passages, their choices in order, then ghosts under their targets
    const shape: Shape = {
      nodes: initialNodes.map((n) => n.id).sort((a, b) => Number(a) - Number(b)),
      edges: graph.choices
        .filter((c) => c.to_passage !== null && !hidden.has(c.from_passage) && !hidden.has(c.to_passage))
        .sort((a, b) => a.from_passage - b.from_passage || a.sort_order - b.sort_order || a.id - b.id)
        .map((c) => [String(c.from_passage), String(c.to_passage)]),
      root: graph.story.start_passage_id === null ? null : String(graph.story.start_passage_id),
    }
    // proposals: ghost nodes for passages that would be created, dashed edges for choices
    for (const q of graph.proposals) {
      if (q.target_passage !== null && hidden.has(q.target_passage)) continue
      const ghostId = `g${q.id}`
      const ghost = ghostFor(q)
      if (ghost) {
        initialNodes.push({ id: ghostId, type: 'ghost', position: ghost.pos, data: ghost.data })
        shape.nodes.push(ghostId)
        if (q.target_passage !== null) shape.edges.push([String(q.target_passage), ghostId])
      }
      const edge = edgeFor(q, graph.choices, ghost ? ghostId : null)
      if (edge && q.target_passage !== null) {
        initialEdges.push({ id: ghostId, source: String(q.target_passage), target: edge.target, label: edge.label,
          className: 'ghost', markerEnd: { type: MarkerType.ArrowClosed } })
      }
    }
    return { initialNodes, initialEdges, shape }
  }, [graph, query, problems, activity, collapsed])

  // Auto layout reruns only when the structure changes: the serialized shape is
  // the memo key, so text edits, search, and overlays never move a card.
  const shapeKey = JSON.stringify(shape)
  const layout = useMemo(() => (auto ? tidyLayout(JSON.parse(shapeKey) as Shape) : null), [auto, shapeKey])
  const placedNodes = useMemo(
    () => (layout ? initialNodes.map((n) => ({ ...n, position: layout.get(n.id) ?? n.position })) : initialNodes),
    [initialNodes, layout],
  )

  const [nodes, setNodes, onNodesChange] = useNodesState(placedNodes)
  const [edges, setEdges, onEdgesChange] = useEdgesState(initialEdges)
  // Graph changes rebuild nodes and edges but keep React Flow's selected flags.
  useEffect(() => {
    setNodes((prev) => {
      const sel = new Set(prev.filter((n) => n.selected).map((n) => n.id))
      return placedNodes.map((n) => ({ ...n, selected: sel.has(n.id) }))
    })
  }, [placedNodes, setNodes])
  useEffect(() => {
    setEdges((prev) => {
      const sel = new Set(prev.filter((e) => e.selected).map((e) => e.id))
      return initialEdges.map((e) => ({ ...e, selected: sel.has(e.id) }))
    })
  }, [initialEdges, setEdges])
  // The inspector's single selection (clicks here, or links in the inspector) becomes the map's.
  useEffect(() => {
    const nodeId = selection?.kind === 'passage' ? String(selection.id) : selection?.kind === 'proposal' ? `g${selection.id}` : null
    const edgeId = selection?.kind === 'choice' ? `c${selection.id}` : selection?.kind === 'proposal' ? `g${selection.id}` : null
    setNodes((ns) => ns.map((n) => (n.selected === (n.id === nodeId) ? n : { ...n, selected: n.id === nodeId })))
    setEdges((es) => es.map((e) => (e.selected === (e.id === edgeId) ? e : { ...e, selected: e.id === edgeId })))
  }, [selection, setNodes, setEdges])

  const onNodeDragStop = useCallback(
    (_: unknown, node: Node) => {
      if (node.type !== 'passage') return
      store.updatePassage(Number(node.id), { pos_x: node.position.x, pos_y: node.position.y })
    },
    [store],
  )

  // Handle-to-handle drag: a new choice between two existing passages.
  const onConnect: OnConnect = useCallback(
    async (conn) => {
      const c = await store.createChoice({
        from_passage: Number(conn.source),
        to_passage: Number(conn.target),
        text: 'Continue',
        sort_order: nextSortOrder(graph?.choices ?? [], Number(conn.source)),
      })
      if (c) onSelect({ kind: 'choice', id: c.id })
    },
    [store, onSelect, graph],
  )

  // Handle drag released on empty canvas: a new passage *and* a choice to it.
  const onConnectEnd = useCallback(
    async (event: MouseEvent | TouchEvent, state: FinalConnectionState) => {
      if (state.isValid || !state.fromNode) return
      const { clientX, clientY } = 'changedTouches' in event ? event.changedTouches[0] : event
      const pos = screenToFlowPosition({ x: clientX, y: clientY })
      const from = Number(state.fromNode.id)
      const p = await store.createPassage({ title: 'New passage', pos_x: pos.x - 90, pos_y: pos.y })
      if (!p) return
      await store.createChoice({ from_passage: from, to_passage: p.id, text: 'Continue', sort_order: nextSortOrder(graph?.choices ?? [], from) })
      onSelect({ kind: 'passage', id: p.id })
    },
    [store, onSelect, screenToFlowPosition, graph],
  )

  // Double-click on the empty pane creates a passage there.
  const onDoubleClick = useCallback(
    async (e: ReactMouseEvent) => {
      if (!(e.target as HTMLElement).classList.contains('react-flow__pane')) return
      const pos = screenToFlowPosition({ x: e.clientX, y: e.clientY })
      const p = await store.createPassage({ title: 'New passage', pos_x: pos.x - 90, pos_y: pos.y - 20 })
      if (p) onSelect({ kind: 'passage', id: p.id })
    },
    [store, onSelect, screenToFlowPosition],
  )

  const onNodeContextMenu = useCallback((e: ReactMouseEvent, node: Node) => {
    e.preventDefault()
    if (node.type !== 'passage') return
    const box = wrapper.current?.getBoundingClientRect()
    setMenu({ x: e.clientX - (box?.left ?? 0), y: e.clientY - (box?.top ?? 0), passageId: Number(node.id) })
  }, [])

  async function deletePassage(id: number) {
    if (!(await confirmDelete(store, id))) return
    if (selection?.kind === 'passage' && selection.id === id) onSelect(null)
  }

  async function prunePassage(id: number) {
    const r = await confirmPrune(store, id)
    if (!r) return
    const gone = new Set(r.passage_ids)
    if ((selection?.kind === 'passage' && gone.has(selection.id)) || (selection?.kind === 'choice' && r.choice_ids.includes(selection.id))) onSelect(null)
    if (collapsed.has(id)) toggleCollapsed(id) // nothing left below it to hide
  }

  // Switching auto layout off offers to keep the arrangement as the stored positions.
  async function toggleAuto() {
    if (!auto) return setAuto(true)
    if (layout && graph && confirm('Keep the automatic arrangement? OK saves it as the passages\' positions; Cancel goes back to the positions you had.')) {
      const positions = graph.passages.flatMap((p) => {
        const at = layout.get(String(p.id))
        return at ? [{ id: p.id, pos_x: at.x, pos_y: at.y }] : []
      })
      if (!(await store.saveLayout(positions))) return
    }
    setAuto(false)
  }

  const menuPassage = menu && graph?.passages.find((p) => p.id === menu.passageId)
  // Merge folds the multi-selection into the inspected passage when it is one of
  // them, else into the lowest id; the survivor keeps its title and body.
  const mergeInto = selection?.kind === 'passage' && scope.includes(selection.id) ? selection.id : scope[0]
  const mergeTarget = graph?.passages.find((p) => p.id === mergeInto)
  async function mergeSelected() {
    if (!mergeTarget || scope.length < 2) return
    const others = scope.filter((id) => id !== mergeInto)
    if (!confirm(`Merge ${others.length} passage(s) into "${mergeTarget.title}"? Their choices move to it and they are deleted.`)) return
    await store.mergePassages(mergeInto, others)
    onSelect({ kind: 'passage', id: mergeInto })
  }
  const allReviewed = scope.length > 1 && scope.every((id) => graph?.passages.find((p) => p.id === id)?.reviewed)
  const problemCount = problems
    ? problems.unreachable.length + problems.dangling.length + problems.undeclared.length + (problems.no_start ? 1 : 0)
    : 0

  return (
    <div ref={wrapper} className={`canvas${auto ? ' auto-layout' : ''}`} onDoubleClick={onDoubleClick} onClick={() => setMenu(null)}>
      <div className="canvas-tools" onDoubleClick={(e) => e.stopPropagation()}>
        <input placeholder="Search passages…" value={query} onChange={(e) => setQuery(e.target.value)} />
        <button className={auto ? 'active' : ''} onClick={toggleAuto}
          title={auto ? 'cards are arranged from the story\'s structure; switch off to place them by hand' : 'arrange cards from the story\'s structure'}>
          Auto layout
        </button>
        <button className={showProblems ? 'active' : ''} onClick={() => setShowProblems((v) => !v)}>
          Problems{problems ? ` (${problemCount})` : ''}
        </button>
        <button
          disabled={checking || !hasWorld}
          title={hasWorld ? 'check passages against lore (runs in the background)' : 'add lore first'}
          onClick={() => store.startRun({ workflow: 'canon_check', scope: canonScope })}
        >
          {checking ? 'Checking…' : canonScope.length ? `Canon check (${canonScope.length} selected)` : 'Canon check (all)'}
        </button>
        {scope.length > 1 && (
          <>
            <button title={allReviewed ? 'clear reviewed on the selection' : 'mark the selection reviewed'}
              onClick={() => store.promote({ passage_ids: scope, reviewed: !allReviewed })}>
              {allReviewed ? `Unreview ${scope.length}` : `Review ${scope.length}`}
            </button>
            <button title="fold the selected passages into one (merge candidates, endings)" onClick={mergeSelected}>
              Merge {scope.length} → {mergeTarget?.title ?? '?'}
            </button>
          </>
        )}
        {collapsed.size > 0 && <span className="muted small">{collapsed.size} collapsed</span>}
        {problems && (
          <span className="muted small">
            {problems.no_start && <span className="error">no start · </span>}
            {problems.unreachable.length} unreachable · {problems.dangling.length} dangling · {problems.undeclared.length} undeclared ·{' '}
            {problems.endings.length} ending{problems.endings.length === 1 ? '' : 's'} · {problems.unfinished.length} unfinished
            {problems.merge_candidates.length > 0 && ` · ${problems.merge_candidates.length} merge group${problems.merge_candidates.length === 1 ? '' : 's'}`}
          </span>
        )}
      </div>
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        onNodesChange={onNodesChange}
        nodesDraggable={!auto}
        onEdgesChange={onEdgesChange}
        onNodeDragStop={onNodeDragStop}
        onConnect={onConnect}
        onConnectEnd={onConnectEnd}
        onNodeClick={(e, n) => {
          if (e.shiftKey || e.metaKey || e.ctrlKey) return // multi-select: leave the inspector alone
          onSelect(n.id.startsWith('g') ? { kind: 'proposal', id: Number(n.id.slice(1)) } : { kind: 'passage', id: Number(n.id) })
        }}
        onEdgeClick={(_, e) => onSelect(e.id.startsWith('g') ? { kind: 'proposal', id: Number(e.id.slice(1)) } : { kind: 'choice', id: Number(e.id.slice(1)) })}
        onPaneClick={() => onSelect(null)}
        onNodeContextMenu={onNodeContextMenu}
        onPaneContextMenu={(e) => e.preventDefault()}
        onSelectionChange={onSelectionChange}
        deleteKeyCode={null}
        zoomOnDoubleClick={false}
        fitView
      >
        <Background />
        <Controls />
        <MiniMap pannable zoomable />
      </ReactFlow>
      {menu && menuPassage && (
        <div className="context-menu" style={{ left: menu.x, top: menu.y }}>
          <div className="context-menu-title">{menuPassage.title}</div>
          <button
            disabled={graph?.story.start_passage_id === menu.passageId}
            onClick={() => store.updateStory({ start_passage_id: menu.passageId })}
          >
            Set as start
          </button>
          <button onClick={() => store.duplicatePassage(menu.passageId)}>Duplicate</button>
          <button onClick={() => onPlayFrom(menu.passageId)}>Play from here</button>
          <button onClick={() => onExpandFrom(menu.passageId)}>Expand from here…</button>
          <button onClick={() => toggleCollapsed(menu.passageId)}>{collapsed.has(menu.passageId) ? 'Expand subtree' : 'Collapse subtree'}</button>
          <button title="flip reviewed on this passage and everything reachable only through it"
            onClick={() => store.promote({ root: menu.passageId, reviewed: !menuPassage.reviewed })}>
            {menuPassage.reviewed ? 'Unreview subtree' : 'Promote subtree'}
          </button>
          <button className="danger" title="delete this passage's choices and every passage below them to the leaves (passages the start reaches another way are kept)"
            disabled={!graph?.choices.some((c) => c.from_passage === menu.passageId)}
            onClick={() => prunePassage(menu.passageId)}>
            Delete subtree
          </button>
          <button className="danger" onClick={() => deletePassage(menu.passageId)}>
            Delete
          </button>
        </div>
      )}
    </div>
  )
}

export function StoryCanvas(props: Props) {
  return (
    <ReactFlowProvider>
      <Canvas {...props} />
    </ReactFlowProvider>
  )
}
