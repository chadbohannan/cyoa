// The Agents view configures how agents write; it does not watch them (live run
// state stays in the run dock). Its main tab is the story's pacing plan
// (concepts/act-pacing.md) drawn in two linked layers:
//
//  - the timeline: Premise | each act, as wide as its levels | Resolution. A stage
//    opens its fields in the sidebar, and becomes the stage the loop is shown for;
//  - the loop: what expand_frontier runs once per level, with `grow` opened into
//    its prompt steps. The shape comes from the backend (agents.FRONTIER_LOOP), so
//    nothing here restates which roles a step composes. A step opens its prompt
//    editors, with the preview's pacing slots filled as the selected stage fills them.
//
// The second tab draws the assist verbs, the interview and canon_check the same way, without a plan.
//
// The loop must look like the map but never be confused for it, hence its own
// node type: steps can be clicked, never dragged or connected. Prompt roles are
// global (one text per role, every story), so they come from usePrompts, held
// above the story shell, and save explicitly; the plan is the story's and autosaves
// (usePacing).

import { useEffect, useMemo, useState } from 'react'
import { Background, Handle, MarkerType, Position, ReactFlow, type Edge, type Node, type NodeProps } from '@xyflow/react'
import { api, type LoopStep, type PacingPlan, type PromptInfo, type WorkflowGraph } from './api'
import { describePlan } from './runs'
import { planDepth, stageStarts, type PacingStore } from './usePacing'
import { errorReason, type PromptStore } from './usePrompts'
import type { StoryStore } from './useStory'

type StepData = { label: string; when?: string; selected: boolean; dimmed: boolean; edited: boolean }
type StepNode = Node<StepData, 'step'>
type GroupNode = Node<{ label: string }, 'group'>

function StepCard({ data }: NodeProps<StepNode>) {
  const cls = ['step-card', data.selected && 'selected', data.dimmed && 'dimmed'].filter(Boolean).join(' ')
  return (
    <div className={cls} title={data.when ? `runs ${data.when}` : undefined}>
      <Handle type="target" position={Position.Left} isConnectable={false} />
      {data.label}
      {data.edited && <span className="edited-dot" title="a prompt here is edited or has an unsaved draft" />}
      {data.when && <div className="step-when">{data.when}</div>}
      <Handle type="source" position={Position.Right} isConnectable={false} />
    </div>
  )
}
function GroupBox({ data }: NodeProps<GroupNode>) {
  return <div className="step-group"><span>{data.label}</span></div>
}
const nodeTypes = { step: StepCard, group: GroupBox }

const COL = 170
const ROW = 64

// Columns left to right: a step's column is the longest cycle-free path to it from the
// first step, so the loop's back edge (review → plan) doesn't push anything along.
function layout(loop: WorkflowGraph['loop']): Map<string, { x: number; y: number }> {
  const out = new Map<string, string[]>()
  for (const [a, b] of loop.edges) out.set(a, [...(out.get(a) ?? []), b])
  const col = new Map<string, number>()
  const visit = (id: string, depth: number, path: Set<string>) => {
    if (path.has(id) || (col.get(id) ?? -1) >= depth) return
    col.set(id, depth)
    const next = new Set(path).add(id)
    for (const t of out.get(id) ?? []) visit(t, depth + 1, next)
  }
  if (loop.steps.length) visit(loop.steps[0].id, 0, new Set())
  for (const s of loop.steps) if (!col.has(s.id)) col.set(s.id, 0)
  const cols = new Map<number, string[]>()
  for (const s of loop.steps) cols.set(col.get(s.id)!, [...(cols.get(col.get(s.id)!) ?? []), s.id])
  const pos = new Map<string, { x: number; y: number }>()
  for (const [c, ids] of cols) ids.forEach((id, i) => pos.set(id, { x: c * COL, y: (i - (ids.length - 1) / 2) * ROW }))
  return pos
}

// A step the selected stage never reaches: an act writes no endings at the ending
// level, and the Resolution drafts no mid-story passage.
function dimmedIn(plan: PacingPlan | null, stage: number, id: string): boolean {
  if (!plan || stage < 0) return false
  return stage < plan.acts.length ? id === 'ending' : id === 'passage'
}

function LoopCanvas({ loop, selected, dimmed, edited, onPick, onPane }: {
  loop: WorkflowGraph['loop']
  selected: string | null
  dimmed: (id: string) => boolean
  edited: (s: LoopStep) => boolean
  onPick: (id: string) => void
  onPane: () => void
}) {
  const { nodes, edges } = useMemo(() => {
    const pos = layout(loop)
    const steps: StepNode[] = loop.steps.map((s) => ({
      id: s.id, type: 'step', position: pos.get(s.id) ?? { x: 0, y: 0 }, draggable: false,
      data: { label: s.label, when: s.when, selected: s.id === selected, dimmed: dimmed(s.id), edited: edited(s) },
    }))
    // One box behind the steps that run inside the same graph node (grow).
    const groups: GroupNode[] = [...new Set(loop.steps.map((s) => s.inside).filter((x): x is string => !!x))].map((g) => {
      const ps = loop.steps.filter((s) => s.inside === g).map((s) => pos.get(s.id)!)
      const [x0, x1] = [Math.min(...ps.map((p) => p.x)), Math.max(...ps.map((p) => p.x))]
      const [y0, y1] = [Math.min(...ps.map((p) => p.y)), Math.max(...ps.map((p) => p.y))]
      return {
        id: `group:${g}`, type: 'group', position: { x: x0 - 20, y: y0 - 38 }, draggable: false, selectable: false, zIndex: -1,
        style: { width: x1 - x0 + 170, height: y1 - y0 + 90 }, data: { label: `${g} · per frontier passage` },
      }
    })
    const edges: Edge[] = loop.edges.map(([a, b], i) => ({
      id: `e${i}`, source: a, target: b, type: 'smoothstep', markerEnd: { type: MarkerType.ArrowClosed },
      className: dimmed(a) || dimmed(b) ? 'dimmed' : undefined,
    }))
    return { nodes: [...groups, ...steps] as Node[], edges }
  }, [loop, selected, dimmed, edited])

  return (
    <ReactFlow
      nodes={nodes} edges={edges} nodeTypes={nodeTypes} fitView
      nodesConnectable={false} nodesDraggable={false} elementsSelectable={false} zoomOnDoubleClick={false}
      onNodeClick={(_, n) => n.type === 'step' && onPick(n.id)}
      onPaneClick={onPane}
    >
      <Background />
    </ReactFlow>
  )
}

// What the sidebar shows: the selected stage's fields, or a step's prompts.
type Side = { kind: 'stage' } | { kind: 'step'; id: string }
type Tab = 'pacing' | 'assist'

export function AgentsView({ store, prompts, pacing, mapPassage }: {
  store: StoryStore
  prompts: PromptStore
  pacing: PacingStore
  mapPassage: number | null
}) {
  const [workflows, setWorkflows] = useState<Record<string, WorkflowGraph> | null>(null)
  const [tab, setTab] = useState<Tab>('pacing')
  const [stage, setStage] = useState(0) // -1 Premise, 0..acts-1 an act, acts.length the Resolution
  const [side, setSide] = useState<Side>({ kind: 'stage' })
  const [other, setOther] = useState('verb')
  const { plan } = pacing

  useEffect(() => {
    api.workflows().then(setWorkflows).catch(() => setWorkflows({}))
  }, [])
  // Keep the selection on a stage that still exists after acts are removed.
  useEffect(() => {
    if (plan && stage > plan.acts.length) setStage(plan.acts.length)
  }, [plan, stage])

  const workflow = tab === 'pacing' ? 'expand_frontier' : other
  const loop = workflows?.[workflow]?.loop ?? null
  const step = side.kind === 'step' ? loop?.steps.find((s) => s.id === side.id) ?? null : null
  const byKey = (k: string) => prompts.prompts?.find((p) => p.key === k)
  const edited = (s: LoopStep) => s.roles.some((k) => { const p = byKey(k); return !!p && (p.override !== null || prompts.dirty(p)) })
  const dimmed = (id: string) => tab === 'pacing' && dimmedIn(plan, stage, id)

  const pickStage = (i: number) => {
    setStage(i)
    setSide({ kind: 'stage' })
  }
  const pickTab = (t: Tab) => {
    setTab(t)
    setSide(t === 'pacing' ? { kind: 'stage' } : { kind: 'step', id: '' })
  }
  // "Used by" in a prompt panel: open the step that composes it.
  const showUse = (w: string, node: string, key: string) => {
    const steps = workflows?.[w]?.loop.steps ?? []
    const s = steps.find((x) => x.roles.includes(key)) ?? steps.find((x) => x.id === node)
    if (w === 'expand_frontier') setTab('pacing')
    else { setTab('assist'); setOther(w) }
    if (s) setSide({ kind: 'step', id: s.id })
  }

  return (
    <div className="agents">
      <div className="agents-main">
        <div className="row agents-tabs">
          <nav className="tabs">
            <button className={tab === 'pacing' ? 'active' : ''} onClick={() => pickTab('pacing')}>Pacing</button>
            <button className={tab === 'assist' ? 'active' : ''} onClick={() => pickTab('assist')}>Assist &amp; canon</button>
          </nav>
          {tab === 'pacing' && plan && <span className="muted small">{describePlan(plan)}</span>}
          {tab === 'assist' && (
            <nav className="tabs">
              {Object.keys(workflows ?? {}).filter((w) => w !== 'expand_frontier').map((w) => (
                <button key={w} className={w === other ? 'active' : ''} onClick={() => { setOther(w); setSide({ kind: 'step', id: '' }) }}>{w}</button>
              ))}
            </nav>
          )}
          <span className="spacer" />
          {pacing.saving && <span className="muted small">saving…</span>}
          {pacing.error && <span className="error small">{pacing.error}</span>}
        </div>
        {tab === 'pacing' && (plan ? (
          <Timeline plan={plan} stage={stage} onPick={pickStage} onAdd={() => { pacing.addAct(plan.acts.length); pickStage(plan.acts.length) }} />
        ) : (
          <p className="hint">{pacing.error ?? 'Loading plan…'}</p>
        ))}
        {tab === 'pacing' && plan && (
          <p className="small muted loop-caption">
            Each level runs this loop, shown for <strong>{stage < 0 ? plan.acts[0].name : stage < plan.acts.length ? plan.acts[stage].name : 'the Resolution'}</strong>.
            Click a step for its prompts.
          </p>
        )}
        <div className="agents-canvas">
          {loop ? (
            <LoopCanvas loop={loop} selected={step?.id ?? null} dimmed={dimmed} edited={edited}
                        onPick={(id) => setSide({ kind: 'step', id })}
                        onPane={() => setSide(tab === 'pacing' ? { kind: 'stage' } : { kind: 'step', id: '' })} />
          ) : (
            <p className="hint">{workflows ? 'Backend has no such workflow.' : 'Loading…'}</p>
          )}
        </div>
      </div>
      <aside className="agents-inspector">
        {prompts.loadError && <p className="error small">{prompts.loadError}</p>}
        {step ? (
          <StepPanel step={step} workflow={workflow} plan={tab === 'pacing' ? plan : null} stage={stage}
                     prompts={prompts} store={store} mapPassage={mapPassage} onShowUse={showUse} />
        ) : tab === 'pacing' && plan ? (
          <StagePanel plan={plan} stage={stage} pacing={pacing} onPick={pickStage} />
        ) : (
          <p className="hint">Click a step for its prompts.</p>
        )}
      </aside>
    </div>
  )
}

// --- timeline --------------------------------------------------------------------

function Timeline({ plan, stage, onPick, onAdd }: {
  plan: PacingPlan; stage: number; onPick: (i: number) => void; onAdd: () => void
}) {
  const starts = stageStarts(plan)
  const n = plan.acts.length
  return (
    <div className="timeline">
      <button className={`stage premise ${stage === -1 ? 'selected' : ''}`} onClick={() => onPick(-1)}>
        <span className="stage-name">Premise</span>
        <span className="stage-meta">steer · time · policy</span>
      </button>
      {plan.acts.map((a, i) => (
        <button key={i} className={`stage ${stage === i ? 'selected' : ''}`} style={{ flexGrow: a.levels }} onClick={() => onPick(i)}>
          <span className="stage-name">{a.name || `Act ${i + 1}`}</span>
          <span className="ticks">
            {Array.from({ length: a.levels }, (_, k) => <span key={k} className="tick">L{starts[i] + k + 1}</span>)}
          </span>
          <span className="stage-meta">≤{a.branching} choices{a.ends_story ? '' : ' · no early endings'}</span>
        </button>
      ))}
      <button className="stage add" onClick={onAdd} title="add an act before the Resolution">+</button>
      <button className={`stage resolution ${stage === n ? 'selected' : ''}`} style={{ flexGrow: 1 }} onClick={() => onPick(n)}>
        <span className="stage-name">Resolution</span>
        <span className="ticks"><span className="tick">L{planDepth(plan)}</span></span>
        <span className="stage-meta">endings</span>
      </button>
    </div>
  )
}

// The most passages a plan can write: each level multiplies by the branching of the act
// its choices come out of (the root's are the first act's). Early endings and fewer
// choices only lower it.
function maxPassages(plan: PacingPlan): number {
  const starts = stageStarts(plan)
  const actOf = (level: number) => plan.acts[Math.max(0, plan.acts.findIndex((_, j) => level < starts[j + 1]))]
  let width = 1
  let total = 0
  for (let level = 0; level < planDepth(plan); level++) {
    width *= actOf(Math.max(level - 1, 0)).branching
    total += width
  }
  return total
}

const clamp = (v: number, lo: number, hi: number) => (Number.isFinite(v) ? Math.min(hi, Math.max(lo, Math.round(v))) : lo)

function StagePanel({ plan, stage, pacing, onPick }: {
  plan: PacingPlan; stage: number; pacing: PacingStore; onPick: (i: number) => void
}) {
  const n = plan.acts.length
  if (stage < 0) {
    const refine = plan.policy.refine ?? {}
    const setPolicy = (patch: Partial<PacingPlan['policy']>) => pacing.update((p) => ({ ...p, policy: { ...p.policy, ...patch } }))
    return (
      <>
        <div className="panel-head"><span className="kind">Premise</span> <span className="muted small">run-wide settings</span></div>
        <p className="small muted">
          Depth {planDepth(plan)}: the acts' levels plus the Resolution, a hard cap. At most {maxPassages(plan)} passages from
          one root; early endings and fewer choices make it less.
        </p>
        <label className="field">
          <span>Steer <span className="muted small">— direction for everything the writer drafts</span></span>
          <textarea rows={3} value={plan.steer} onChange={(e) => pacing.update((p) => ({ ...p, steer: e.target.value }))} />
        </label>
        <label className="field row">
          <span>Time guard</span>
          <input type="number" min={10} step={60} className="small-num" value={plan.max_seconds}
                 onChange={(e) => pacing.update((p) => ({ ...p, max_seconds: clamp(Number(e.target.value), 10, 86400) }))} />
          <span className="muted small">seconds; a guard, not a target</span>
        </label>
        <label className="field row">
          <input type="checkbox" checked={!!plan.policy.auto_accept_unreviewed}
                 onChange={(e) => setPolicy({ auto_accept_unreviewed: e.target.checked })} />
          <span>Auto-accept proposals on unreviewed passages</span>
        </label>
        <label className="field row">
          <span>Revisions per passage</span>
          <input type="number" min={0} max={6} className="small-num" value={refine.rounds ?? 3}
                 onChange={(e) => setPolicy({ refine: { ...refine, rounds: clamp(Number(e.target.value), 0, 6) } })} />
          <span className="muted small">audit → revise, at most this many times; 0 skips the audits</span>
        </label>
        <div className="row">
          <span className="spacer" />
          <button className="danger" onClick={pacing.reset} title="forget this story's plan and use the default">Reset plan to default</button>
        </div>
      </>
    )
  }
  if (stage >= n) {
    const setEnding = (patch: Partial<PacingPlan['ending']>) => pacing.update((p) => ({ ...p, ending: { ...p.ending, ...patch } }))
    return (
      <>
        <div className="panel-head"><span className="kind">Resolution</span> <span className="muted small">level {planDepth(plan)}, the plan's ending</span></div>
        <p className="small muted">
          Every passage at this level is an ending, written with the Frontier ending prompt. It follows the last act's
          choices, so each of them gets its own resolution.
        </p>
        <p className="small muted">An ending has no length target: it runs as long as the conclusion needs.</p>
        <label className="field">
          <span>Guidance</span>
          <textarea rows={6} value={plan.ending.guidance} onChange={(e) => setEnding({ guidance: e.target.value })} />
        </label>
      </>
    )
  }
  const a = plan.acts[stage]
  const starts = stageStarts(plan)
  const set = (patch: Partial<typeof a>) => pacing.updateAct(stage, patch)
  const move = (by: -1 | 1) => { pacing.moveAct(stage, by); onPick(stage + by) }
  return (
    <>
      <div className="panel-head">
        <span className="kind">Act {stage + 1}</span>
        <span className="muted small">levels {starts[stage] + 1}–{starts[stage + 1]}</span>
      </div>
      <label className="field">
        <span>Name</span>
        <input value={a.name} onChange={(e) => set({ name: e.target.value })} />
      </label>
      <div className="row wrap">
        <label className="field row">
          <span>Levels</span>
          <input type="number" min={1} max={10} className="small-num" value={a.levels} onChange={(e) => set({ levels: clamp(Number(e.target.value), 1, 10) })} />
        </label>
        <label className="field row" title="most choices out of a passage in this act">
          <span>Branching</span>
          <input type="number" min={1} max={6} className="small-num" value={a.branching} onChange={(e) => set({ branching: clamp(Number(e.target.value), 1, 6) })} />
        </label>
        <label className="field row" title="the writer may end a storyline early inside this act">
          <input type="checkbox" checked={a.ends_story} onChange={(e) => set({ ends_story: e.target.checked })} />
          <span>Early endings</span>
        </label>
      </div>
      <label className="field">
        <span>Length <span className="muted small">— fills {'{length}'}</span></span>
        <input value={a.length} placeholder="e.g. 200–300 words" onChange={(e) => set({ length: e.target.value })} />
      </label>
      <label className="field">
        <span>Guidance <span className="muted small">— the writers' direction for this act</span></span>
        <textarea rows={6} value={a.guidance} onChange={(e) => set({ guidance: e.target.value })} />
      </label>
      <div className="row">
        <button disabled={stage === 0} onClick={() => move(-1)} title="move earlier">←</button>
        <button disabled={stage === n - 1} onClick={() => move(1)} title="move later">→</button>
        <button onClick={() => { pacing.addAct(stage + 1); onPick(stage + 1) }}>Add act after</button>
        <span className="spacer" />
        <button className="danger" disabled={n === 1} onClick={() => { pacing.removeAct(stage); onPick(Math.max(0, stage - 1)) }}>Remove</button>
      </div>
    </>
  )
}

// --- steps and prompts ----------------------------------------------------------

// A plan field a step reads, as the selected stage has it.
function readValue(plan: PacingPlan, stage: number, field: string): string {
  const act = stage >= 0 && stage < plan.acts.length ? plan.acts[stage] : null
  const show = (v: unknown) => {
    const s = typeof v === 'string' ? v : JSON.stringify(v)
    return s === '' || s === undefined ? '—' : s.length > 60 ? s.slice(0, 59) + '…' : s
  }
  if (field.startsWith('ending.')) return show(plan.ending[field.slice(7) as keyof PacingPlan['ending']])
  if (field === 'depth') return String(planDepth(plan))
  if (field === 'policy.refine') return show(plan.policy.refine ?? 'default')
  if (field in plan && !['acts', 'ending'].includes(field)) return show(plan[field as keyof PacingPlan])
  if (!act) return stage < 0 ? '(per act)' : '(the last act\'s, for choices)'
  return show(act[field as keyof typeof act])
}

function StepPanel({ step, workflow, plan, stage, prompts, store, mapPassage, onShowUse }: {
  step: LoopStep
  workflow: string
  plan: PacingPlan | null
  stage: number
  prompts: PromptStore
  store: StoryStore
  mapPassage: number | null
  onShowUse: (workflow: string, node: string, key: string) => void
}) {
  return (
    <>
      <div className="panel-head">
        <span className="kind">Step</span> <strong>{step.label}</strong>
        {step.inside && <span className="muted small">inside <code>{step.inside}</code></span>}
      </div>
      {step.doc && <p className="small">{step.doc}</p>}
      {step.when && <p className="small muted">Runs {step.when}.</p>}
      {plan && step.reads.length > 0 && (
        <dl className="params small">
          {step.reads.map((f) => (
            <span key={f} className="contents">
              <dt>{f}</dt><dd>{readValue(plan, stage, f)}</dd>
            </span>
          ))}
        </dl>
      )}
      {step.roles.length === 0 && <p className="small muted">This step does not call the model.</p>}
      {step.roles.map((k) => {
        const p = prompts.prompts?.find((x) => x.key === k)
        return p ? (
          <PromptPanel key={k} prompt={p} prompts={prompts} store={store} mapPassage={mapPassage}
                       plan={workflow === 'expand_frontier' ? plan : null} stage={stage} onShowUse={onShowUse} />
        ) : null
      })}
    </>
  )
}

function PromptPanel({ prompt: p, prompts, store, mapPassage, plan, stage, onShowUse }: {
  prompt: PromptInfo
  prompts: PromptStore
  store: StoryStore
  mapPassage: number | null
  plan: PacingPlan | null
  stage: number
  onShowUse: (workflow: string, node: string, key: string) => void
}) {
  const text = prompts.text(p)
  const dirty = prompts.dirty(p)
  const error = prompts.errors[p.key]
  const passages = store.graph?.passages ?? []
  const [passageId, setPassageId] = useState<number | null>(mapPassage ?? store.graph?.story.start_passage_id ?? passages[0]?.id ?? null)
  const [preview, setPreview] = useState<{ prompt?: string; error?: string } | null>(null)
  const planKey = plan ? JSON.stringify(plan) : ''

  // Rendered from the draft and the plan as they stand, so the author sees what a Save (and
  // the plan's autosave) would send, at the selected stage; debounced while typing.
  useEffect(() => {
    if (passageId === null) return
    let live = true
    const t = window.setTimeout(() => {
      api.prompts
        .preview(store.storyId, p.key, {
          passage_id: passageId, text: dirty ? text : null,
          plan: planKey ? (JSON.parse(planKey) as PacingPlan) : null, stage: Math.max(stage, 0),
        })
        .then((r) => live && setPreview({ prompt: r.prompt }))
        .catch((e) => live && setPreview({ error: errorReason(e) }))
    }, 300)
    return () => { live = false; window.clearTimeout(t) }
  }, [store.storyId, p.key, passageId, text, dirty, planKey, stage])

  return (
    <section className="prompt-panel">
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
            <button className="link" onClick={() => onShowUse(w, n, p.key)}>{w} · {n}</button>
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
      <details className="small" open>
        <summary>Preview</summary>
        <label className="row small">
          passage
          <select value={passageId ?? ''} onChange={(e) => setPassageId(Number(e.target.value))}>
            {passages.map((x) => <option key={x.id} value={x.id}>{x.title}</option>)}
          </select>
        </label>
        {preview?.error ? <p className="error small">{preview.error}</p> : <pre className="preview">{preview?.prompt ?? '…'}</pre>}
      </details>
    </section>
  )
}
