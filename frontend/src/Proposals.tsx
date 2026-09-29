// Proposals in the inspector: the toolbar that asks for them, the list under a
// passage, and the panel that shows one with Accept / Reject. Only Accept
// writes to the story; everything here is otherwise read-only.

import { useState } from 'react'
import type { AssistVerb, AuditMeta, Passage, Proposal } from './api'
import type { Selection } from './StoryCanvas'
import type { StoryStore } from './useStory'

// A refinement pass that gave up with issues still open (agents.DEFAULT_REFINE's
// on_unresolved: 'keep_flagged') — shown wherever the flag can land: a passage's own
// meta.audit, or a new_choice proposal's payload.audit for its not-yet-created target.
export function AuditFlag({ audit }: { audit: AuditMeta }) {
  return (
    <details className="audit-flag">
      <summary>
        <span className="badge unresolved">unresolved</span>
        {' '}refinement gave up after {audit.rounds} round{audit.rounds === 1 ? '' : 's'} with {audit.issues.length}
        {' '}issue{audit.issues.length === 1 ? '' : 's'} still open
      </summary>
      <ul className="issues">
        {audit.issues.map((i, n) => (
          <li key={n}>
            <span className={`badge ${i.severity}`}>{i.severity}</span> {i.problem}
            <div className="muted small">“{i.quote}”{i.evidence && <> — {i.evidence}</>}</div>
          </li>
        ))}
      </ul>
    </details>
  )
}

const TONES = ['vivid', 'grim', 'playful', 'terse', 'lyrical']

const VERB_LABEL: Record<AssistVerb | 'canon_check', string> = {
  draft_body: 'Draft body',
  suggest_choices: 'Suggest choices',
  draft_target: 'Draft target',
  rewrite: 'Rewrite',
  suggest_next: 'Suggest next step',
  canon_check: 'Canon check',
}

// The four passage verbs. Draft target lives on the dangling choice instead.
export function AssistToolbar({ passage, hasChoices, store, onSelect }: {
  passage: Passage; hasChoices: boolean; store: StoryStore; onSelect: (s: Selection) => void
}) {
  const [tone, setTone] = useState(TONES[0])
  const busy = store.busy[passage.id]
  const run = async (verb: AssistVerb) => {
    const rows = await store.assist(passage.id, { verb, tone: verb === 'rewrite' ? tone : null })
    if (rows?.[0]) onSelect({ kind: 'proposal', id: rows[0].id })
  }
  return (
    <div className="assist">
      <div className="panel-head"><span className="kind">Assist</span>{busy && <span className="muted small">· {VERB_LABEL[busy as keyof typeof VERB_LABEL] ?? busy}…</span>}</div>
      <div className="row wrap">
        <button disabled={!!busy} onClick={() => run('draft_body')}>{VERB_LABEL.draft_body}</button>
        <button disabled={!!busy} onClick={() => run('suggest_choices')}>{VERB_LABEL.suggest_choices}</button>
        <button disabled={!!busy || !passage.body.trim()} onClick={() => run('rewrite')}>{VERB_LABEL.rewrite}</button>
        <button
          disabled={!!busy || !passage.body.trim() || hasChoices}
          title={hasChoices ? 'Only for a passage with no choices yet' : 'Suggest branching into choices, continuing on one path, or ending here'}
          onClick={() => run('suggest_next')}
        >
          {VERB_LABEL.suggest_next}
        </button>
        <select value={tone} onChange={(e) => setTone(e.target.value)} className="narrow" title="tone for Rewrite">
          {TONES.map((t) => <option key={t}>{t}</option>)}
        </select>
      </div>
    </div>
  )
}

export function ProposalList({ proposals, onSelect }: { proposals: Proposal[]; onSelect: (s: Selection) => void }) {
  if (proposals.length === 0) return null
  return (
    <>
      <div className="panel-head"><span className="kind">Proposed</span></div>
      <ul className="choice-list">
        {proposals.map((q) => (
          <li key={q.id}>
            <button className="link" onClick={() => onSelect({ kind: 'proposal', id: q.id })}>{summary(q)}</button>
          </li>
        ))}
      </ul>
    </>
  )
}

function summary(q: Proposal): string {
  const pl = q.payload
  switch (q.kind) {
    case 'edit_passage': return pl.is_ending !== undefined ? 'Mark as ending' : `${q.verb === 'rewrite' ? 'Rewrite' : 'Body'}: ${excerpt(pl.body)}`
    case 'new_passage': return `New passage “${pl.title}”`
    case 'new_choice': return `Choice “${pl.text}” → ${pl.new_target ? `new “${pl.new_target.title}”` : 'existing passage'}${pl.audit ? ' ⚠ unresolved' : ''}`
    case 'edit_choice': return `Choice text: ${pl.text}`
    case 'new_variable': return `Variable ${pl.name} (${pl.kind})`
    default: return pl.issues?.length ? `${pl.issues.length} canon issue${pl.issues.length > 1 ? 's' : ''}` : excerpt(pl.text) || 'note'
  }
}
const excerpt = (s = '', n = 60) => (s.length > n ? s.slice(0, n - 1) + '…' : s)

export function ProposalPanel({ proposal: q, store, onSelect }: { proposal: Proposal; store: StoryStore; onSelect: (s: Selection) => void }) {
  const graph = store.graph!
  const pl = q.payload
  const passage = graph.passages.find((p) => p.id === q.target_passage)
  const choice = graph.choices.find((c) => c.id === q.target_choice)
  const siblings = graph.proposals.filter((x) => x.batch === q.batch && x.id !== q.id)
  const back: Selection = passage ? { kind: 'passage', id: passage.id } : null

  // Every pending proposal in the story, in creation order: Prev/Next step through this
  // queue so a review pass never has to detour back through a passage panel, and Accept
  // / Reject land on whatever now occupies this slot instead of always exiting to it.
  const queue = [...graph.proposals].sort((a, b) => a.id - b.id)
  const pos = queue.findIndex((x) => x.id === q.id)
  const prevQ = pos > 0 ? queue[pos - 1] : null
  const nextQ = pos >= 0 && pos < queue.length - 1 ? queue[pos + 1] : null
  const after: Selection = nextQ ? { kind: 'proposal', id: nextQ.id } : prevQ ? { kind: 'proposal', id: prevQ.id } : back

  const accept = async () => {
    await store.acceptProposal(q.id)
    onSelect(after)
  }
  const reject = async () => {
    await store.rejectProposal(q.id)
    onSelect(after)
  }
  const discardAll = async () => {
    await store.rejectBatch(q.batch)
    onSelect(back)
  }
  const regenerate = async () => {
    await store.rejectBatch(q.batch)
    if (q.verb === 'canon_check') {
      if (passage) await store.startRun({ workflow: 'canon_check', scope: [passage.id] })
      onSelect(back)
    } else if (passage) {
      const rows = await store.assist(passage.id, { verb: q.verb as AssistVerb, choice_id: q.target_choice })
      onSelect(rows?.[0] ? { kind: 'proposal', id: rows[0].id } : back)
    }
  }

  return (
    <div className="panel">
      <div className="panel-head">
        <span className="kind">Proposal <span className="badge">{q.kind.replace('_', ' ')}</span></span>
        <span className="spacer" />
        <button className="primary" onClick={accept}>Accept</button>
        <button onClick={reject}>Reject</button>
      </div>
      <div className="row">
        <button className="icon" disabled={!prevQ} onClick={() => prevQ && onSelect({ kind: 'proposal', id: prevQ.id })} title="Previous pending proposal">‹ Prev</button>
        {pos >= 0 && <span className="muted small">{pos + 1} of {queue.length} pending</span>}
        <button className="icon" disabled={!nextQ} onClick={() => nextQ && onSelect({ kind: 'proposal', id: nextQ.id })} title="Next pending proposal">Next ›</button>
      </div>
      <p className="muted small">
        {VERB_LABEL[q.verb as keyof typeof VERB_LABEL] ?? q.verb}
        {passage && <> on <button className="link" onClick={() => onSelect({ kind: 'passage', id: passage.id })}>{passage.title}</button></>}
        {choice && <> via “{choice.text}”</>}
      </p>

      {q.kind === 'edit_passage' && passage && (
        pl.is_ending !== undefined ? (
          <>
            <p className="small">Mark <strong>{passage.title}</strong> as a decided ending. {pl.reasoning}</p>
            <p className="muted small">The reader will see no choices here once accepted, whatever is listed on the passage.</p>
          </>
        ) : (
          <>
            <label>Current<div className="proposal-text muted">{passage.body || <em>empty</em>}</div></label>
            <label>Proposed<div className="proposal-text">{pl.body}</div></label>
          </>
        )
      )}
      {q.kind === 'new_passage' && (
        <>
          <label>Title<div className="proposal-text">{pl.title}</div></label>
          <label>Body<div className="proposal-text">{pl.body}</div></label>
          {choice && <p className="muted small">Accepting creates the passage and points “{choice.text}” at it.</p>}
        </>
      )}
      {q.kind === 'new_choice' && (
        <>
          <label>Choice text<div className="proposal-text">{pl.text}</div></label>
          <p className="small">
            Leads to{' '}
            {pl.new_target
              ? <>a new passage <strong>“{pl.new_target.title}”</strong> (created empty on accept)</>
              : <strong>{graph.passages.find((p) => p.id === pl.to_passage)?.title ?? '?'}</strong>}
          </p>
          {pl.audit && <AuditFlag audit={pl.audit} />}
        </>
      )}
      {q.kind === 'edit_choice' && <div className="proposal-text">{pl.text}</div>}
      {q.kind === 'note' && (
        <>
          {pl.issues?.length ? (
            <ul className="issues">
              {pl.issues.map((i, n) => (
                <li key={n}>
                  <span className={`badge ${i.severity}`}>{i.severity}</span> {i.claim}
                  <div className="muted small">conflicts with {i.conflicts_with}</div>
                </li>
              ))}
            </ul>
          ) : (
            <div className="proposal-text">{pl.text}</div>
          )}
          <p className="muted small">A note changes nothing; Accept files it, Reject discards it.
            {siblings.some((s) => s.kind === 'edit_passage' && s.target_passage === q.target_passage) && ' A suggested fix is in the same batch below.'}
          </p>
        </>
      )}
      {q.kind === 'new_variable' && <div className="proposal-text">{pl.name} · {pl.kind} · default {JSON.stringify(pl.default)}</div>}

      <div className="row" style={{ marginTop: '0.75rem' }}>
        <button disabled={!passage || !!store.busy[passage.id]} onClick={regenerate}>Regenerate</button>
        {siblings.length > 0 && <button onClick={discardAll}>Discard all {siblings.length + 1}</button>}
      </div>
      {siblings.length > 0 && (
        <>
          <div className="panel-head"><span className="kind">Same batch</span></div>
          <ul className="choice-list">
            {siblings.map((s) => (
              <li key={s.id}><button className="link" onClick={() => onSelect({ kind: 'proposal', id: s.id })}>{summary(s)}</button></li>
            ))}
          </ul>
        </>
      )}
    </div>
  )
}
