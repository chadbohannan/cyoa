// Read-only view of the agent workflows: each LangGraph topology drawn with
// React Flow (from compiled.get_graph().to_json()), the node a chosen run last
// finished highlighted, and that run's checkpointed state and step history.
// This is the second React Flow surface the agentic-authoring page asks for;
// it must look like the map but never be confused for it, hence its own node
// type and no editing at all.

import { useEffect, useMemo, useState } from 'react'
import { Background, Handle, MarkerType, Position, ReactFlow, type Edge, type Node, type NodeProps } from '@xyflow/react'
import { api, type RunState, type WorkflowGraph } from './api'
import { describeProgress } from './runs'
import type { StoryStore } from './useStory'

type StepData = { label: string; current: boolean; next: boolean; terminal: boolean }
type StepNode = Node<StepData, 'step'>

function StepCard({ data }: NodeProps<StepNode>) {
  const cls = ['step-card', data.current && 'current', data.next && 'next', data.terminal && 'terminal'].filter(Boolean).join(' ')
  return (
    <div className={cls}>
      <Handle type="target" position={Position.Top} isConnectable={false} />
      {data.label}
      <Handle type="source" position={Position.Bottom} isConnectable={false} />
    </div>
  )
}
const nodeTypes = { step: StepCard }

// Layered layout: a node's row is the longest cycle-free path from __start__.
function layout(g: WorkflowGraph): Map<string, { x: number; y: number }> {
  const out = new Map<string, string[]>()
  for (const e of g.edges) out.set(e.source, [...(out.get(e.source) ?? []), e.target])
  const row = new Map<string, number>()
  const visit = (id: string, depth: number, path: Set<string>) => {
    if (path.has(id)) return
    if ((row.get(id) ?? -1) >= depth) return
    row.set(id, depth)
    const next = new Set(path).add(id)
    for (const t of out.get(id) ?? []) visit(t, depth + 1, next)
  }
  visit('__start__', 0, new Set())
  for (const n of g.nodes) if (!row.has(n.id)) row.set(n.id, 0)
  const rows = new Map<number, string[]>()
  for (const [id, r] of row) rows.set(r, [...(rows.get(r) ?? []), id])
  const pos = new Map<string, { x: number; y: number }>()
  for (const [r, ids] of rows) ids.forEach((id, i) => pos.set(id, { x: (i - (ids.length - 1) / 2) * 170, y: r * 90 }))
  return pos
}

const label = (id: string) => (id === '__start__' ? 'start' : id === '__end__' ? 'end' : id)

export function AgentsView({ store, runId, onPickRun }: { store: StoryStore; runId: number | null; onPickRun: (id: number | null) => void }) {
  const [workflows, setWorkflows] = useState<Record<string, WorkflowGraph> | null>(null)
  const [which, setWhich] = useState<string>('expand_frontier')
  const [state, setState] = useState<RunState | null>(null)
  const run = store.runs.find((r) => r.id === runId) ?? null
  const workflow = run?.workflow ?? which
  const graph = workflows?.[workflow] ?? null
  const current = run ? store.lastNode[run.id] ?? null : null

  useEffect(() => {
    api.workflows().then(setWorkflows).catch(() => setWorkflows({}))
  }, [])
  // The checkpoint is read when a run is picked and again whenever it changes status or finishes a node.
  useEffect(() => {
    if (!run) return
    let live = true
    api.runs.state(store.storyId, run.id).then((s) => live && setState(s)).catch(() => {})
    return () => { live = false }
  }, [run, current, store.storyId])

  const { nodes, edges } = useMemo(() => {
    if (!graph) return { nodes: [] as StepNode[], edges: [] as Edge[] }
    const pos = layout(graph)
    const next = new Set(run ? state?.next ?? [] : [])
    const nodes: StepNode[] = graph.nodes.map((n) => ({
      id: n.id,
      type: 'step',
      position: pos.get(n.id) ?? { x: 0, y: 0 },
      draggable: false,
      data: { label: label(n.id), current: n.id === current, next: next.has(n.id), terminal: n.id.startsWith('__') },
    }))
    const edges: Edge[] = graph.edges.map((e, i) => ({
      id: `e${i}`, source: e.source, target: e.target, className: e.conditional ? 'conditional' : undefined,
      markerEnd: { type: MarkerType.ArrowClosed },
    }))
    return { nodes, edges }
  }, [graph, current, state, run])

  return (
    <div className="agents">
      <aside className="agents-side">
        <div className="panel-head"><span className="kind">Workflows</span></div>
        <ul className="choice-list">
          {Object.keys(workflows ?? {}).map((w) => (
            <li key={w}>
              <button className={`link ${w === workflow ? 'active' : ''}`} onClick={() => { onPickRun(null); setWhich(w) }}>{w}</button>
            </li>
          ))}
        </ul>
        <div className="panel-head"><span className="kind">Runs</span></div>
        {store.runs.length === 0 && <p className="hint">Start one from the map's run dock.</p>}
        <ul className="choice-list">
          {store.runs.map((r) => (
            <li key={r.id}>
              <button className={`link ${r.id === runId ? 'active' : ''}`} onClick={() => onPickRun(r.id)}>
                #{r.id} {r.workflow} <span className="badge">{r.status}</span>
              </button>
            </li>
          ))}
        </ul>
        {run && (
          <>
            <div className="panel-head"><span className="kind">Run #{run.id}</span></div>
            <p className="small">{describeProgress(run, store.graph?.passages.length)}{current && <> · last node <code>{current}</code></>}</p>
            {run.error && <p className="error small">{run.error}</p>}
            {state && (
              <>
                <p className="small muted">next: {state.next.length ? state.next.join(', ') : '—'} · {state.history.length} checkpoints</p>
                <pre className="state">{JSON.stringify(state.values, null, 1)}</pre>
                <div className="panel-head"><span className="kind">Steps</span></div>
                <ol className="steps small">
                  {[...state.history].reverse().map((h, i) => (
                    <li key={i}>{h.step ?? '·'} → {h.next.length ? h.next.join(', ') : 'end'}</li>
                  ))}
                </ol>
              </>
            )}
          </>
        )}
      </aside>
      <div className="agents-canvas">
        {graph ? (
          <ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes} fitView nodesConnectable={false} elementsSelectable={false} zoomOnDoubleClick={false}>
            <Background />
          </ReactFlow>
        ) : (
          <p className="hint">{workflows ? 'Backend has no workflows.' : 'Loading…'}</p>
        )}
      </div>
    </div>
  )
}
