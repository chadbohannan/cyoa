// The agent workflows in three panes: lists on the left (workflows, prompt
// roles, runs), the chosen workflow's LangGraph topology in the middle (drawn
// from compiled.get_graph().to_json(), the node a run last finished
// highlighted), and an inspector on the right for whatever is selected — a
// node, a prompt role, or a run. The topology must look like the map but never
// be confused for it, hence its own node type: nodes can be clicked to inspect
// them, never dragged or connected.
//
// Prompt roles are the one editable thing here. They are global (one text per
// role, every story), so they come from usePrompts, held above the story shell,
// and save explicitly (see usePrompts.ts).

import { useEffect, useMemo, useState } from 'react'
import { Background, Handle, MarkerType, Position, ReactFlow, type Edge, type Node, type NodeProps } from '@xyflow/react'
import { api, type AgentRun, type PromptInfo, type RunState, type WorkflowGraph } from './api'
import { describeProgress } from './runs'
import { errorReason, type PromptStore } from './usePrompts'
import type { StoryStore } from './useStory'

type StepData = { label: string; current: boolean; next: boolean; terminal: boolean; selected: boolean }
type StepNode = Node<StepData, 'step'>

function StepCard({ data }: NodeProps<StepNode>) {
  const cls = ['step-card', data.current && 'current', data.next && 'next', data.terminal && 'terminal', data.selected && 'selected']
    .filter(Boolean)
    .join(' ')
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

// What the inspector shows. A run is selected through `runId` (App owns it, so
// the run dock can open one here); `sel` says whether it, a node or a prompt is in front.
type Sel = { kind: 'node'; workflow: string; id: string } | { kind: 'prompt'; key: string } | { kind: 'run' } | null

export function AgentsView({ store, prompts, runId, onPickRun, mapPassage }: {
  store: StoryStore
  prompts: PromptStore
  runId: number | null
  onPickRun: (id: number | null) => void
  mapPassage: number | null
}) {
  const [workflows, setWorkflows] = useState<Record<string, WorkflowGraph> | null>(null)
  const [which, setWhich] = useState<string>('expand_frontier')
  const [sel, setSel] = useState<Sel>(runId !== null ? { kind: 'run' } : null)
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
    if (!run) return setState(null)
    let live = true
    api.runs.state(store.storyId, run.id).then((s) => live && setState(s)).catch(() => {})
    return () => { live = false }
  }, [run, current, store.storyId])

  const pickRun = (id: number | null) => {
    onPickRun(id)
    setSel(id === null ? null : { kind: 'run' })
  }
  const pickWorkflow = (w: string) => {
    pickRun(null)
    setWhich(w)
  }
  const showNode = (w: string, id: string) => {
    if (w !== workflow) pickWorkflow(w)
    setSel({ kind: 'node', workflow: w, id })
  }

  const { nodes, edges } = useMemo(() => {
    if (!graph) return { nodes: [] as StepNode[], edges: [] as Edge[] }
    const pos = layout(graph)
    const next = new Set(run ? state?.next ?? [] : [])
    const nodes: StepNode[] = graph.nodes.map((n) => ({
      id: n.id,
      type: 'step',
      position: pos.get(n.id) ?? { x: 0, y: 0 },
      draggable: false,
      data: {
        label: label(n.id), current: n.id === current, next: next.has(n.id), terminal: n.id.startsWith('__'),
        selected: sel?.kind === 'node' && sel.workflow === workflow && sel.id === n.id,
      },
    }))
    const edges: Edge[] = graph.edges.map((e, i) => ({
      id: `e${i}`, source: e.source, target: e.target, className: e.conditional ? 'conditional' : undefined,
      markerEnd: { type: MarkerType.ArrowClosed },
    }))
    return { nodes, edges }
  }, [graph, current, state, run, sel, workflow])

  const prompt = sel?.kind === 'prompt' ? prompts.prompts?.find((p) => p.key === sel.key) ?? null : null
  const node = sel?.kind === 'node' ? workflows?.[sel.workflow]?.nodes.find((n) => n.id === sel.id) ?? null : null

  return (
    <div className="agents">
      <aside className="agents-side">
        <div className="panel-head"><span className="kind">Workflows</span></div>
        <ul className="choice-list">
          {Object.keys(workflows ?? {}).map((w) => (
            <li key={w}>
              <button className={`link ${w === workflow ? 'active' : ''}`} onClick={() => pickWorkflow(w)}>{w}</button>
            </li>
          ))}
        </ul>
        <div className="panel-head"><span className="kind">Prompts</span></div>
        {prompts.loadError && <p className="error small">{prompts.loadError}</p>}
        <ul className="choice-list">
          {(prompts.prompts ?? []).map((p) => (
            <li key={p.key}>
              <button className={`link ${prompt?.key === p.key ? 'active' : ''}`} onClick={() => setSel({ kind: 'prompt', key: p.key })}>
                {p.label}
              </button>
            </li>
          ))}
        </ul>
        <div className="panel-head"><span className="kind">Runs</span></div>
        {store.runs.length === 0 && <p className="hint">Start one from the map's run dock.</p>}
        <ul className="choice-list">
          {store.runs.map((r) => (
            <li key={r.id}>
              <button className={`link ${r.id === runId && sel?.kind === 'run' ? 'active' : ''}`} onClick={() => pickRun(r.id)}>
                #{r.id} {r.workflow} <span className={`badge ${r.status}`}>{r.status}</span>
              </button>
            </li>
          ))}
        </ul>
      </aside>
      <div className="agents-canvas">
        {graph ? (
          <ReactFlow
            nodes={nodes} edges={edges} nodeTypes={nodeTypes} fitView
            nodesConnectable={false} nodesDraggable={false} elementsSelectable={false} zoomOnDoubleClick={false}
            onNodeClick={(_, n) => setSel({ kind: 'node', workflow, id: n.id })}
            onPaneClick={() => setSel(run ? { kind: 'run' } : null)}
          >
            <Background />
          </ReactFlow>
        ) : (
          <p className="hint">{workflows ? 'Backend has no workflows.' : 'Loading…'}</p>
        )}
      </div>
      <aside className="agents-inspector">
        {prompt ? (
          <PromptPanel prompt={prompt} prompts={prompts} store={store} mapPassage={mapPassage} onShowNode={showNode} />
        ) : sel?.kind === 'node' ? (
          <NodePanel workflow={sel.workflow} id={sel.id} roles={node?.prompts ?? []} prompts={prompts.prompts ?? []}
                     onShowPrompt={(key) => setSel({ kind: 'prompt', key })} />
        ) : run ? (
          <RunPanel run={run} state={state} current={current} passages={store.graph?.passages.length} prompts={prompts.prompts ?? []}
                    onShowPrompt={(key) => setSel({ kind: 'prompt', key })} />
        ) : (
          <p className="hint">Click a node, a prompt or a run.</p>
        )}
      </aside>
    </div>
  )
}

function NodePanel({ workflow, id, roles, prompts, onShowPrompt }: {
  workflow: string; id: string; roles: string[]; prompts: PromptInfo[]; onShowPrompt: (key: string) => void
}) {
  return (
    <>
      <div className="panel-head"><span className="kind">Node</span> <code>{label(id)}</code></div>
      <p className="small muted">in workflow <code>{workflow}</code></p>
      <div className="panel-head"><span className="kind">Prompts</span></div>
      {roles.length === 0 ? (
        <p className="small muted">This node does not call the model.</p>
      ) : (
        <ul className="choice-list">
          {roles.map((k) => (
            <li key={k}>
              <button className="link" onClick={() => onShowPrompt(k)}>{prompts.find((p) => p.key === k)?.label ?? k}</button>{' '}
              <code>{k}</code>
            </li>
          ))}
        </ul>
      )}
    </>
  )
}

function PromptPanel({ prompt: p, prompts, store, mapPassage, onShowNode }: {
  prompt: PromptInfo
  prompts: PromptStore
  store: StoryStore
  mapPassage: number | null
  onShowNode: (workflow: string, node: string) => void
}) {
  const text = prompts.text(p)
  const dirty = prompts.dirty(p)
  const error = prompts.errors[p.key]
  const passages = store.graph?.passages ?? []
  const [passageId, setPassageId] = useState<number | null>(mapPassage ?? store.graph?.story.start_passage_id ?? passages[0]?.id ?? null)
  const [preview, setPreview] = useState<{ prompt?: string; error?: string } | null>(null)

  // Rendered from the draft, so the author sees what a Save would send; debounced while typing.
  useEffect(() => {
    if (passageId === null) return
    let live = true
    const t = window.setTimeout(() => {
      api.prompts
        .preview(store.storyId, p.key, { passage_id: passageId, text: dirty ? text : null })
        .then((r) => live && setPreview({ prompt: r.prompt }))
        .catch((e) => live && setPreview({ error: errorReason(e) }))
    }, 300)
    return () => { live = false; window.clearTimeout(t) }
  }, [store.storyId, p.key, passageId, text, dirty])

  return (
    <>
      <div className="panel-head">
        <span className="kind">Prompt</span> <strong>{p.label}</strong> <code>{p.key}</code>
        {p.override !== null && <span className="badge edited">edited</span>}
      </div>
      <p className="small">{p.doc}</p>
      <p className="small muted">
        Used by{' '}
        {p.used_by.map(([w, n], i) => (
          <span key={`${w}/${n}`}>
            {i > 0 && ', '}
            <button className="link" onClick={() => onShowNode(w, n)}>{w} · {n}</button>
          </span>
        ))}
        . Edits apply to every story.
      </p>
      <p className="small muted">
        {p.placeholders.length ? <>Placeholders (all required): {p.placeholders.map((x) => <code key={x}>{`{${x}}`} </code>)}</> : 'No placeholders'}
        {' '}— write a literal brace as <code>{'{{'}</code> or <code>{'}}'}</code>.
      </p>
      <textarea className="prompt-text mono" value={text} onChange={(e) => prompts.setDraft(p.key, e.target.value)} rows={10} />
      <div className="row">
        <button className="primary" disabled={!dirty} onClick={() => prompts.save(p.key)}>Save</button>
        <button disabled={!dirty} onClick={() => prompts.revert(p.key)}>Revert</button>
        <span className="spacer" />
        {p.override !== null && <button className="danger" onClick={() => prompts.reset(p.key)}>Reset to default</button>}
      </div>
      {error && <p className="error small">{error}</p>}
      <details className="small">
        <summary>Default</summary>
        <pre className="state">{p.default}</pre>
      </details>

      <div className="panel-head"><span className="kind">Preview</span></div>
      <label className="row small">
        passage
        <select value={passageId ?? ''} onChange={(e) => setPassageId(Number(e.target.value))}>
          {passages.map((x) => <option key={x.id} value={x.id}>{x.title}</option>)}
        </select>
      </label>
      {preview?.error ? <p className="error small">{preview.error}</p> : <pre className="preview">{preview?.prompt ?? '…'}</pre>}
    </>
  )
}

function RunPanel({ run, state, current, passages, prompts, onShowPrompt }: {
  run: AgentRun
  state: RunState | null
  current: string | null
  passages: number | undefined
  prompts: PromptInfo[]
  onShowPrompt: (key: string) => void
}) {
  const { model, steer, prompts: snapshot } = run.params
  return (
    <>
      <div className="panel-head">
        <span className="kind">Run #{run.id}</span> <code>{run.workflow}</code> <span className={`badge ${run.status}`}>{run.status}</span>
      </div>
      <p className="small">{describeProgress(run, passages)}{current && <> · last node <code>{current}</code></>}</p>
      {run.error && <p className="error small">{run.error}</p>}

      <div className="panel-head"><span className="kind">Params</span></div>
      <dl className="params small">
        <dt>model</dt><dd>{model || 'default'}</dd>
        <dt>steer</dt><dd>{steer || '—'}</dd>
        <dt>budget</dt><dd className="mono">{Object.keys(run.budget).length ? JSON.stringify(run.budget) : '—'}</dd>
        <dt>policy</dt><dd className="mono">{Object.keys(run.policy).length ? JSON.stringify(run.policy) : '—'}</dd>
      </dl>

      <div className="panel-head"><span className="kind">Prompts it ran with</span></div>
      {snapshot ? (
        Object.entries(snapshot).map(([k, text]) => (
          <details key={k} className="small">
            <summary>
              {prompts.find((p) => p.key === k)?.label ?? k} <code>{k}</code>{' '}
              <button className="link" onClick={(e) => { e.preventDefault(); onShowPrompt(k) }}>open</button>
            </summary>
            <pre className="state">{text}</pre>
          </details>
        ))
      ) : (
        <p className="small muted">Started before runs recorded their prompts.</p>
      )}

      {state && (
        <>
          <div className="panel-head"><span className="kind">State</span></div>
          <p className="small muted">next: {state.next.length ? state.next.join(', ') : '—'} · {state.history.length} checkpoints</p>
          {Object.entries(state.values).map(([k, v]) => (
            <details key={k} className="small">
              <summary><code>{k}</code> <span className="muted">{summarize(v)}</span></summary>
              <pre className="state">{JSON.stringify(v, null, 1)}</pre>
            </details>
          ))}
          <div className="panel-head"><span className="kind">Steps</span></div>
          <ol className="steps small">
            {[...state.history].reverse().map((h, i) => (
              <li key={i}>{h.step ?? '·'} → {h.next.length ? h.next.join(', ') : 'end'}</li>
            ))}
          </ol>
        </>
      )}
    </>
  )
}

// A one-line hint next to a collapsed state value.
function summarize(v: unknown): string {
  if (Array.isArray(v)) return `[${v.length}]`
  if (v && typeof v === 'object') return `{${Object.keys(v).length}}`
  const s = JSON.stringify(v) ?? ''
  return s.length > 40 ? s.slice(0, 39) + '…' : s
}
