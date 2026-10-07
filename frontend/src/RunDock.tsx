// The run dock under the map: start expand_frontier from the selected passage
// under the story's pacing plan, and watch, steer, resume or cancel every run of
// this story. The plan itself is edited in the Agents view; the dock shows it in
// one line. Proposals a run filed are reviewed in the inspector like any other;
// "Review" jumps there.

import { useState } from 'react'
import { api, type AgentRun, type Passage, type RunLogEvent } from './api'
import type { Selection } from './StoryCanvas'
import { describePlan, describeProgress, WORKFLOW_LABEL } from './runs'
import type { PacingStore } from './usePacing'
import type { StoryStore } from './useStory'

type Props = {
  store: StoryStore
  pacing: PacingStore
  root: Passage | null // the single selected passage, if any
  onSelect: (s: Selection) => void
  onEditPacing: () => void // open the Agents view on the plan
}

export function RunDock({ store, pacing, root, onSelect, onEditPacing }: Props) {
  const { runs, graph } = store
  const { plan } = pacing
  const [open, setOpen] = useState(true)
  const [skipInterrupt, setSkipInterrupt] = useState(false)
  const live = runs.filter((r) => r.status === 'running' || r.status === 'paused').length

  // The plan goes with the request, so the run copies what the author sees even
  // while an edit is still waiting to autosave.
  const expand = () => {
    if (!root || !plan) return
    store.startRun({
      workflow: 'expand_frontier',
      scope: [root.id],
      budget: plan,
      policy: { skip_review_interrupt: skipInterrupt },
    })
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
            <span className="muted small">{root ? `from "${root.title}"` : 'select one passage'}</span>
            <button className="link small plan-summary" onClick={onEditPacing} title="edit the pacing plan in the Agents view">
              {plan ? describePlan(plan) : pacing.error ?? 'loading plan…'}
            </button>
            <span className="spacer" />
            <label className="small row" title="don't pause for review when a storyline ends">
              <input type="checkbox" checked={skipInterrupt} onChange={(e) => setSkipInterrupt(e.target.checked)} />
              Unattended
            </label>
            <button className="primary" disabled={!root || !plan} onClick={expand}>Run</button>
          </div>
          {runs.length > 0 && (
            <>
              {runs.some((r) => r.status !== 'running' && r.status !== 'paused') && (
                <div className="row">
                  <span className="spacer" />
                  <button className="small" onClick={() => store.clearRuns()}
                          title="remove finished runs from this list; running and paused runs, runs with proposals to review, and the passages stay">
                    Clear history
                  </button>
                </div>
              )}
            <ul className="run-list">
              {runs.map((run) => (
                <RunRow key={run.id} run={run} store={store} onReview={() => review(run)} />
              ))}
            </ul>
            </>
          )}
        </div>
      )}
    </div>
  )
}

function RunRow({ run, store, onReview }: { run: AgentRun; store: StoryStore; onReview: () => void }) {
  const [steer, setSteer] = useState(run.params.steer ?? '')
  const active = run.status === 'running' || run.status === 'paused'
  const [retries, setRetries] = useState<RunLogEvent[] | null>(null)
  const toggleRetries = () =>
    retries ? setRetries(null) : api.runs.log(run.story_id, run.id, 'retry').then(setRetries).catch(() => setRetries([]))
  return (
    <li className={`run-row ${run.status}`}>
      <span className={`badge ${run.status}`}>{run.status}</span>
      <span>
        <strong>{WORKFLOW_LABEL[run.workflow]}</strong> #{run.id} <span className="muted small">· {describeProgress(run, store.graph?.passages.length)}</span>
        {run.error && <div className="error small" style={{ whiteSpace: 'pre-wrap' }}>{run.error}</div>}
        {!!run.progress.retries && (
          <button className="link small" onClick={toggleRetries}>{retries ? 'hide retries' : 'show retries'}</button>
        )}
        {retries && (
          <ul className="small" style={{ whiteSpace: 'pre-wrap' }}>
            {retries.length === 0 && <li className="muted">no retries were saved for this run</li>}
            {retries.map((e) => (
              <li key={e.seq}>
                passage {e.passage_id} · {e.phase} · attempt {e.attempt} · {e.seconds}s · {e.kind}: {e.reason}
                {e.answer && <div className="muted">said: {e.answer}</div>}
              </li>
            ))}
          </ul>
        )}
      </span>
      <span className="spacer" />
      {run.status === 'paused' && (
        <>
          <input placeholder="steer the next storyline..." value={steer} onChange={(e) => setSteer(e.target.value)} className="steer" />
          <button className="primary" onClick={() => store.resumeRun(run.id, { steer: steer || null })}>Resume</button>
          <button onClick={() => store.resumeRun(run.id, { stop: true })} title="finish the run here">Stop</button>
        </>
      )}
      {active && <button className="danger" onClick={() => store.cancelRun(run.id)}>Cancel</button>}
      {run.pending_proposals > 0 && <button onClick={onReview}>Review {run.pending_proposals} proposal{run.pending_proposals > 1 ? 's' : ''}</button>}
    </li>
  )
}
