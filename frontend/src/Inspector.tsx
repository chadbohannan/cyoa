// Right-hand panel showing whatever is selected on the map. Autosaves via the
// field components; the story panel (title, description, variables) shows when
// nothing is selected.

import { useState } from 'react'
import { api, type Choice, type Passage, type Variable, type VariableKind } from './api'
import { ConditionEditor, EffectsEditor } from './clauses'
import { NumberField, TextArea, TextField } from './fields'
import { AssistToolbar, AuditFlag, ProposalList, ProposalPanel } from './Proposals'
import type { Selection } from './StoryCanvas'
import type { StoryStore } from './useStory'

type Props = { store: StoryStore; selection: Selection; onSelect: (s: Selection) => void }

export function Inspector({ store, selection, onSelect }: Props) {
  const { graph } = store
  if (!graph) return null

  if (selection?.kind === 'passage') {
    const p = graph.passages.find((x) => x.id === selection.id)
    if (p) return <PassagePanel key={p.id} passage={p} store={store} onSelect={onSelect} />
  }
  if (selection?.kind === 'choice') {
    const c = graph.choices.find((x) => x.id === selection.id)
    if (c) return <ChoicePanel key={c.id} choice={c} store={store} onSelect={onSelect} />
  }
  if (selection?.kind === 'proposal') {
    const q = graph.proposals.find((x) => x.id === selection.id)
    if (q) return <ProposalPanel key={q.id} proposal={q} store={store} onSelect={onSelect} />
  }
  return <StoryPanel store={store} />
}

// --- passage -----------------------------------------------------------------

function PassagePanel({ passage: p, store, onSelect }: { passage: Passage; store: StoryStore; onSelect: (s: Selection) => void }) {
  const graph = store.graph!
  const isStart = graph.story.start_passage_id === p.id
  const outgoing = graph.choices.filter((c) => c.from_passage === p.id).sort((a, b) => a.sort_order - b.sort_order)
  const inbound = graph.choices.filter((c) => c.to_passage === p.id)
  const proposals = graph.proposals.filter((q) => q.target_passage === p.id)
  const save = (body: Partial<Passage>) => store.updatePassage(p.id, body)

  async function addChoice() {
    const c = await store.createChoice({ from_passage: p.id, text: 'Continue', sort_order: outgoing.length })
    if (c) onSelect({ kind: 'choice', id: c.id })
  }
  function move(i: number, dir: -1 | 1) {
    const ids = outgoing.map((c) => c.id)
    const j = i + dir
    if (j < 0 || j >= ids.length) return
    ;[ids[i], ids[j]] = [ids[j], ids[i]]
    store.reorderChoices(ids)
  }
  async function remove() {
    if (inbound.length && !confirm(`${inbound.length} choice(s) lead here and will dangle. Delete anyway?`)) return
    await store.deletePassage(p.id)
    onSelect(null)
  }

  return (
    <div className="panel">
      <div className="panel-head">
        <span className="kind">Passage {isStart && <span className="badge">start</span>}</span>
        <span className="spacer" />
        {!isStart && <button onClick={() => store.updateStory({ start_passage_id: p.id })}>Set as start</button>}
        <button className="danger" onClick={remove}>Delete</button>
      </div>

      <label>Title<TextField value={p.title} onSave={(title) => save({ title })} /></label>
      <label>Body<TextArea rows={10} value={p.body} onSave={(body) => save({ body })} /></label>
      <AssistToolbar passage={p} hasChoices={outgoing.length > 0} store={store} onSelect={onSelect} />
      {p.meta.audit && <AuditFlag audit={p.meta.audit} />}
      <ProposalList proposals={proposals} onSelect={onSelect} />

      <details>
        <summary>World</summary>
        <label>Story time (t)<NumberField step="any" value={p.t} onSave={(t) => save({ t })} /></label>
        <label>Location<TextField value={p.meta.location ?? ''} onSave={(location) => save({ meta: { ...p.meta, location } })} /></label>
        <label>Present (comma separated)
          <TextField
            value={(p.meta.present ?? []).join(', ')}
            onSave={(s) => save({ meta: { ...p.meta, present: s.split(',').map((x) => x.trim()).filter(Boolean) } })}
          />
        </label>
        <label className="row">
          <input type="checkbox" checked={p.reviewed} onChange={(e) => save({ reviewed: e.target.checked })} /> Reviewed
          <span className="muted">· {p.provenance}</span>
        </label>
      </details>

      <label className="row">
        <input type="checkbox" checked={p.is_ending} onChange={(e) => save({ is_ending: e.target.checked })} /> Ending
        <span className="muted small">— a decided, terminal passage; overrides any choices below at play</span>
      </label>

      <div className="panel-head">
        <span className="kind">Choices</span>
        <span className="spacer" />
        <button onClick={addChoice}>+ Choice</button>
      </div>
      {p.is_ending
        ? <p className="muted">Marked as an ending — the reader sees no choices here, whatever is listed below.</p>
        : outgoing.length === 0 && <p className="muted">No choices yet — mark it Ending above once that's deliberate.</p>}
      <ul className="choice-list">
        {outgoing.map((c, i) => {
          const target = graph.passages.find((x) => x.id === c.to_passage)
          return (
            <li key={c.id}>
              <button className="link" onClick={() => onSelect({ kind: 'choice', id: c.id })}>
                {c.text || <em>untitled</em>}
              </button>
              <span className={target ? 'muted' : 'error'}>→ {target ? target.title : 'dangling'}</span>
              <span className="spacer" />
              <button className="icon" disabled={i === 0} onClick={() => move(i, -1)} title="move up">↑</button>
              <button className="icon" disabled={i === outgoing.length - 1} onClick={() => move(i, 1)} title="move down">↓</button>
            </li>
          )
        })}
      </ul>
      {inbound.length > 0 && (
        <p className="muted small">
          Reached from:{' '}
          {inbound.map((c, i) => {
            const from = graph.passages.find((x) => x.id === c.from_passage)
            return (
              <span key={c.id}>
                {i > 0 && ', '}
                <button className="link" onClick={() => onSelect({ kind: 'passage', id: c.from_passage })}>{from?.title}</button>
              </span>
            )
          })}
        </p>
      )}
    </div>
  )
}

// --- choice ------------------------------------------------------------------

function ChoicePanel({ choice: c, store, onSelect }: { choice: Choice; store: StoryStore; onSelect: (s: Selection) => void }) {
  const graph = store.graph!
  const save = (body: Partial<Choice>) => store.updateChoice(c.id, body)
  const from = graph.passages.find((x) => x.id === c.from_passage)

  return (
    <div className="panel">
      <div className="panel-head">
        <span className="kind">Choice</span>
        <span className="spacer" />
        <button className="danger" onClick={async () => { await store.deleteChoice(c.id); onSelect({ kind: 'passage', id: c.from_passage }) }}>
          Delete
        </button>
      </div>
      <p className="muted small">
        From <button className="link" onClick={() => onSelect({ kind: 'passage', id: c.from_passage })}>{from?.title}</button>
      </p>
      <label>Text<TextField value={c.text} onSave={(text) => save({ text })} /></label>
      <label>
        Target
        <select value={c.to_passage ?? ''} onChange={(e) => save({ to_passage: e.target.value ? Number(e.target.value) : null })}>
          <option value="">— dangling —</option>
          {graph.passages.map((p) => (
            <option key={p.id} value={p.id}>{p.title}</option>
          ))}
        </select>
      </label>
      {c.to_passage !== null ? (
        <p className="small">
          <button className="link" onClick={() => onSelect({ kind: 'passage', id: c.to_passage! })}>Go to target →</button>
        </p>
      ) : (
        <p className="small row">
          <button disabled={!!store.busy[c.from_passage]} onClick={async () => {
            const rows = await store.assist(c.from_passage, { verb: 'draft_target', choice_id: c.id })
            if (rows?.[0]) onSelect({ kind: 'proposal', id: rows[0].id })
          }}>
            Draft target
          </button>
          <span className="muted">{store.busy[c.from_passage] === 'draft_target' ? 'drafting…' : 'ask the model to write the destination'}</span>
        </p>
      )}
      <div className="panel-head"><span className="kind">Condition</span></div>
      <ConditionEditor choice={c} variables={graph.variables} onSave={(condition) => save({ condition })} />
      <div className="panel-head"><span className="kind">Effects</span></div>
      <EffectsEditor choice={c} variables={graph.variables} onSave={(effects) => save({ effects })} />
      {graph.variables.length === 0 && <p className="muted small">Declare variables on the story panel to use conditions and effects.</p>}
    </div>
  )
}

// --- story -------------------------------------------------------------------

const KINDS: VariableKind[] = ['flag', 'stat', 'item']
const defaultFor = (kind: VariableKind) => (kind === 'flag' ? false : 0)

function StoryPanel({ store }: { store: StoryStore }) {
  const { story, variables, passages } = store.graph!
  const [name, setName] = useState('')
  const [kind, setKind] = useState<VariableKind>('flag')

  async function add() {
    const n = name.trim()
    if (!n) return
    const v = await store.createVariable({ name: n, kind, default: defaultFor(kind) })
    if (v) setName('')
  }

  return (
    <div className="panel">
      <div className="panel-head"><span className="kind">Story</span></div>
      <label>Title<TextField value={story.title} onSave={(title) => store.updateStory({ title })} /></label>
      <label>Description<TextArea rows={4} value={story.description} onSave={(description) => store.updateStory({ description })} /></label>
      <p className="muted small">
        {passages.length} passage{passages.length === 1 ? '' : 's'} · IFID <code>{story.ifid}</code>
        {' · '}
        <a href={api.tweeUrl(story.id)} download title="Twee 3 with SugarCube macros; Twine and Tweego import it">Export Twee</a>
      </p>

      <div className="panel-head"><span className="kind">Variables</span></div>
      <table className="vars">
        <thead><tr><th>name</th><th>kind</th><th>default</th><th /></tr></thead>
        <tbody>
          {variables.map((v) => <VariableRow key={v.id} v={v} store={store} />)}
          <tr>
            <td><input placeholder="new variable" value={name} onChange={(e) => setName(e.target.value)} onKeyDown={(e) => e.key === 'Enter' && add()} /></td>
            <td>
              <select value={kind} onChange={(e) => setKind(e.target.value as VariableKind)}>
                {KINDS.map((k) => <option key={k}>{k}</option>)}
              </select>
            </td>
            <td />
            <td><button onClick={add} disabled={!name.trim()}>Add</button></td>
          </tr>
        </tbody>
      </table>
      <p className="muted small">Flags are booleans, stats are numbers, items are counts.</p>
    </div>
  )
}

function VariableRow({ v, store }: { v: Variable; store: StoryStore }) {
  const save = (body: Partial<Variable>) => store.updateVariable(v.id, body)
  return (
    <tr>
      <td><TextField value={v.name} onSave={(name) => save({ name })} /></td>
      <td>
        <select value={v.kind} onChange={(e) => { const kind = e.target.value as VariableKind; save({ kind, default: defaultFor(kind) }) }}>
          {KINDS.map((k) => <option key={k}>{k}</option>)}
        </select>
      </td>
      <td>
        {v.kind === 'flag' ? (
          <input type="checkbox" checked={v.default === true} onChange={(e) => save({ default: e.target.checked })} />
        ) : (
          <NumberField value={typeof v.default === 'number' ? v.default : null} onSave={(n) => save({ default: n ?? 0 })} />
        )}
      </td>
      <td><button className="icon danger" title="delete" onClick={() => store.deleteVariable(v.id)}>×</button></td>
    </tr>
  )
}
