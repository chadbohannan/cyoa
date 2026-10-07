// Interview Me, docked beside the Lore view (concepts/interview-me.md). The agent
// leads: each of its turns ends in one question, and under it sit the premise and
// character changes the turn made, each revertable. Open conflicts are pinned above
// the answer box because they are what the agent asks about next; only an answer
// settles one. Skip and "You decide" hand the question back without writing one.

import { useEffect, useRef, useState } from 'react'
import type { InterviewConflict, InterviewEntry, InterviewWrite } from './api'
import type { InterviewStore } from './useInterview'
import type { StoryStore } from './useStory'

export function InterviewPanel({ iv, store, onHide }: { iv: InterviewStore; store: StoryStore; onHide: () => void }) {
  const { interview, busy, error } = iv
  const [text, setText] = useState('')
  const end = useRef<HTMLDivElement>(null)
  const turns = interview?.transcript.length ?? 0
  useEffect(() => end.current?.scrollIntoView({ block: 'end' }), [turns, busy])

  const open = interview?.conflicts.filter((c) => c.status === 'open') ?? []
  const queued = store.runs.some((r) => r.status === 'running')
  const answer = async () => {
    if (text.trim() && (await iv.send('answer', text))) setText('')
  }
  const decide = async () => {
    if (await iv.send('decide', text)) setText('')
  }

  return (
    <aside className="interview">
      <div className="interview-head">
        <strong>Interview</strong>
        <span className="muted small">premise &amp; characters</span>
        <span className="spacer" />
        {interview && <button className="link small" disabled={!!busy} onClick={() => iv.close()} title="close this interview for good">End</button>}
        <button className="icon" title="hide the panel (the interview stays open)" onClick={onHide}>×</button>
      </div>

      <div className="interview-log">
        {!interview && !busy && (
          <div className="interview-empty">
            <p>The agent will ask about your story one question at a time and write what it learns into the Premise and the characters as it goes.</p>
            <p className="muted small">Answer, skip, or let it decide. Contradictions come back to you as questions; it never picks a side for you.</p>
            <button onClick={() => iv.start()}>Start the interview</button>
          </div>
        )}
        {interview?.transcript.map((e, i) => (
          <Entry key={i} e={e} index={i} conflicts={interview.conflicts} iv={iv} />
        ))}
        {busy && busy !== 'revert' && (
          <div className="bubble agent pending muted">{queued ? 'queued behind a running agent…' : 'waiting for the model…'}</div>
        )}
        <div ref={end} />
      </div>

      {error && <div className="error small interview-error">{error}</div>}

      {interview && (
        <div className="interview-foot">
          {open.length > 0 && (
            <div className="interview-conflicts">
              <div className="kind">Open conflicts</div>
              {open.map((c) => <Conflict key={c.id} c={c} />)}
            </div>
          )}
          <textarea rows={3} value={text} placeholder="Your answer (Enter to send, Shift+Enter for a new line)" disabled={!!busy}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); answer() } }} />
          <div className="row">
            <button onClick={answer} disabled={!!busy || !text.trim()}>Send</button>
            <button onClick={() => iv.send('skip')} disabled={!!busy} title="pass on this question">Skip</button>
            <button onClick={decide} disabled={!!busy} title="let the agent invent the answer; anything typed steers it">You decide</button>
          </div>
        </div>
      )}
    </aside>
  )
}

function Entry({ e, index, conflicts, iv }: { e: InterviewEntry; index: number; conflicts: InterviewConflict[]; iv: InterviewStore }) {
  if (e.role === 'author') {
    const said = e.mode === 'skip' ? '(skipped)' : e.mode === 'decide' ? `(you decide)${e.text ? ` ${e.text}` : ''}` : e.text
    return <div className={`bubble author${e.mode !== 'answer' ? ' muted' : ''}`}>{said}</div>
  }
  const live = e.writes.filter((w) => !w.reverted).length
  const byId = (id: number) => conflicts.find((c) => c.id === id)
  return (
    <div className="bubble agent">
      <div>{e.text}</div>
      {(e.opened.length > 0 || e.resolved.length > 0) && (
        <div className="small interview-marks">
          {e.opened.map((id) => <span key={`o${id}`} className="badge warn">conflict: {byId(id)?.about ?? `#${id}`}</span>)}
          {e.resolved.map((id) => <span key={`r${id}`} className="badge">settled: {byId(id)?.about ?? `#${id}`}</span>)}
        </div>
      )}
      {e.writes.length > 0 && (
        <details>
          <summary>
            {e.writes.length} change{e.writes.length > 1 ? 's' : ''}
            {live < e.writes.length && ` · ${e.writes.length - live} reverted`}
          </summary>
          {e.writes.map((w, j) => (
            <Write key={j} w={w} disabled={!!iv.busy} onRevert={() => iv.revert(index, j)} />
          ))}
          {live > 1 && <button className="link small" disabled={!!iv.busy} onClick={() => iv.revert(index)}>Revert this turn</button>}
        </details>
      )}
    </div>
  )
}

function Write({ w, disabled, onRevert }: { w: InterviewWrite; disabled: boolean; onRevert: () => void }) {
  const str = (v: unknown) => (typeof v === 'string' ? v : '')
  let what: string
  let before: string | null = null
  let after: string | null = null
  if (w.table === 'stories') {
    what = 'Premise'
    before = str(w.before?.description) || '(empty)'
    after = str(w.after?.description)
  } else if (!w.before) {
    what = `New character: ${str(w.after?.name)}`
    after = str(w.after?.body)
  } else if (!w.after) {
    what = `Merged away: ${str(w.before.name)}`
    before = str(w.before.body)
  } else {
    const renamed = w.before.name !== w.after.name
    what = renamed ? `${str(w.before.name)} → ${str(w.after.name)}` : str(w.after.name)
    before = str(w.before.body)
    after = str(w.after.body)
  }
  return (
    <div className={`interview-write${w.reverted ? ' reverted' : ''}`}>
      <div className="row">
        <span className="grow">{what}</span>
        {w.reverted ? <span className="muted small">reverted</span> : <button className="link small" disabled={disabled} onClick={onRevert}>Revert</button>}
      </div>
      {before !== null && before !== after && <div className="diff-before">{before}</div>}
      {after !== null && <div className="diff-after">{after}</div>}
    </div>
  )
}

function Conflict({ c }: { c: InterviewConflict }) {
  return (
    <div className="interview-conflict small">
      <div><strong>{c.about}</strong>{c.source === 'code' && <span className="badge">rule</span>}</div>
      <div className="muted">“{c.a}”</div>
      <div className="muted">versus “{c.b}”</div>
    </div>
  )
}
