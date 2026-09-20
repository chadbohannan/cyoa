// The run dock under the map: start expand_frontier from the selected passage,
// and watch, steer, resume or cancel every run of this story. Proposals a run
// filed are reviewed in the inspector like any other; "Review" jumps there.

import { useState } from 'react'
import type { AgentRun, Passage } from './api'
import type { Selection } from './StoryCanvas'
import { describeProgress, WORKFLOW_LABEL } from './runs'
import type { StoryStore } from './useStory'

type Props = {
  store: StoryStore
  root: Passage | null // the single selected passage, if any
  onSelect: (s: Selection) => void
  onShowRun: (runId: number) => void // open the Agents view on this run
}

export function RunDock({ store, root, onSelect, onShowRun }: Props) {
  const { runs, graph } = store
  const [open, setOpen] = useState(true)
  const [depth, setDepth] = useState(2)
  const [max, setMax] = useState(12)
  const [steer, setSteer] = useState('')
  const live = runs.filter((r) => r.status === 'running' || r.status === 'paused').length

  const expand = () => {
    if (!root) return
    store.startRun({ workflow: 'expand_frontier', scope: [root.id], steer: steer || null, budget: { depth, max_passages: max } })
  }
  const review = (run: AgentRun) => {
    const q = graph?.proposals.find((p) => p.run_id === run.id)
    if (q) onSelect({ kind: 'proposal', id: q.id })
  }

  return (
    <div className={`run-dock ${open ? 'open' : ''}`}>
      <div className="run-dock-bar" onClick={() => setOpen((v) => !v)}>
        <strong>Runs</strong>
        <span className="muted small">{live ? `${live} active` : runs.length ? `${runs.length} finished` : 'none yet'}</span>
        <span className="spacer" />
        <span className="muted small">{open ? '▾' : '▴'}</span>
      </div>
      {open && (
        <div className="run-dock-body">
          <div className="row wrap run-form" title="grow the story outward from the selected passage">
            <span className="kind">Expand</span>
            <span className="muted small">{root ? `from “${root.title}”` : 'select one passage'}</span>
            <label className="small">depth <input type="number" min={1} max={6} value={depth} onChange={(e) => setDepth(Number(e.target.value))} className="tiny" /></label>
            <label className="small">max passages <input type="number" min={1} max={500} value={max} onChange={(e) => setMax(Number(e.target.value))} className="tiny" /></label>
            <input placeholder="direction for the writer (optional)" value={steer} onChange={(e) => setSteer(e.target.value)} className="steer" />
            <button className="primary" disabled={!root} onClick={expand}>Start</button>
          </div>
          {runs.length > 0 && (
            <ul className="run-list">
              {runs.map((run) => (
                <RunRow key={run.id} run={run} store={store} onReview={() => review(run)} onShow={() => onShowRun(run.id)} />
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  )
}

function RunRow({ run, store, onReview, onShow }: { run: AgentRun; store: StoryStore; onReview: () => void; onShow: () => void }) {
  const [steer, setSteer] = useState(run.params.steer ?? '')
  const active = run.status === 'running' || run.status === 'paused'
  return (
    <li className={`run-row ${run.status}`}>
      <span className={`badge ${run.status}`}>{run.status}</span>
      <span>
        <strong>{WORKFLOW_LABEL[run.workflow]}</strong> #{run.id} <span className="muted small">· {describeProgress(run, store.graph?.passages.length)}</span>
        {run.error && <div className="error small">{run.error}</div>}
      </span>
      <span className="spacer" />
      {run.status === 'paused' && (
        <>
          <input placeholder="steer the next level…" value={steer} onChange={(e) => setSteer(e.target.value)} className="steer" />
          <button className="primary" onClick={() => store.resumeRun(run.id, { steer: steer || null })}>Resume</button>
          <button onClick={() => store.resumeRun(run.id, { stop: true })} title="finish the run here">Stop</button>
        </>
      )}
      {active && <button className="danger" onClick={() => store.cancelRun(run.id)}>Cancel</button>}
      {run.pending_proposals > 0 && <button onClick={onReview}>Review {run.pending_proposals} proposal{run.pending_proposals > 1 ? 's' : ''}</button>}
      <button className="link small" onClick={onShow}>workflow</button>
    </li>
  )
}
