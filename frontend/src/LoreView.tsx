// The story bible and timeline: lore entries grouped by kind, events ordered by
// story time. Plain list-and-form; this is the baseline agents later coordinate
// storylines around, so it stays human-editable first.

import { useState } from 'react'
import { LORE_KINDS, type Lore, type LoreKind, type StoryEvent } from './api'
import { NumberField, TextArea, TextField } from './fields'
import type { StoryStore } from './useStory'

export function LoreView({ store }: { store: StoryStore }) {
  const { graph } = store
  if (!graph) return null
  const events = [...graph.events].sort((a, b) => a.t - b.t || a.id - b.id)
  return (
    <div className="lore">
      <section>
        <h2>Lore</h2>
        <p className="muted">Characters, places, factions and rules that hold on every branch.</p>
        {LORE_KINDS.map((kind) => (
          <LoreGroup key={kind} kind={kind} entries={graph.lore.filter((l) => l.kind === kind)} store={store} />
        ))}
      </section>
      <section>
        <h2>Timeline</h2>
        <p className="muted">What happens at each story time <code>t</code>, regardless of the reader's path.</p>
        <NewEvent store={store} />
        {events.length === 0 && <p className="muted">No events yet.</p>}
        {events.map((e) => <EventCard key={e.id} e={e} store={store} />)}
      </section>
    </div>
  )
}

function LoreGroup({ kind, entries, store }: { kind: LoreKind; entries: Lore[]; store: StoryStore }) {
  const [name, setName] = useState('')
  async function add() {
    if (!name.trim()) return
    if (await store.createLore({ kind, name: name.trim() })) setName('')
  }
  return (
    <div className="lore-group">
      <h3>{kind}s</h3>
      {entries.map((l) => <LoreCard key={l.id} l={l} store={store} />)}
      <div className="row">
        <input placeholder={`new ${kind}`} value={name} onChange={(e) => setName(e.target.value)} onKeyDown={(e) => e.key === 'Enter' && add()} />
        <button onClick={add} disabled={!name.trim()}>Add</button>
      </div>
    </div>
  )
}

function LoreCard({ l, store }: { l: Lore; store: StoryStore }) {
  const save = (body: Partial<Lore>) => store.updateLore(l.id, body)
  return (
    <div className="card">
      <div className="row">
        <TextField className="grow" value={l.name} onSave={(name) => save({ name })} />
        <select value={l.kind} onChange={(e) => save({ kind: e.target.value as LoreKind })}>
          {LORE_KINDS.map((k) => <option key={k}>{k}</option>)}
        </select>
        <label className="row small"><input type="checkbox" checked={l.reviewed} onChange={(e) => save({ reviewed: e.target.checked })} /> reviewed</label>
        <button className="icon danger" title="delete" onClick={() => confirm(`Delete "${l.name}"?`) && store.deleteLore(l.id)}>×</button>
      </div>
      <TextArea rows={3} placeholder="body" value={l.body} onSave={(body) => save({ body })} />
    </div>
  )
}

function NewEvent({ store }: { store: StoryStore }) {
  const [name, setName] = useState('')
  const [t, setT] = useState('0')
  async function add() {
    if (!name.trim() || t.trim() === '') return
    if (await store.createEvent({ t: Number(t), name: name.trim() })) setName('')
  }
  return (
    <div className="row">
      <input type="number" step="any" className="narrow" value={t} onChange={(e) => setT(e.target.value)} title="t" />
      <input className="grow" placeholder="new event" value={name} onChange={(e) => setName(e.target.value)} onKeyDown={(e) => e.key === 'Enter' && add()} />
      <button onClick={add} disabled={!name.trim()}>Add</button>
    </div>
  )
}

function EventCard({ e, store }: { e: StoryEvent; store: StoryStore }) {
  const save = (body: Partial<StoryEvent>) => store.updateEvent(e.id, body)
  return (
    <div className="card">
      <div className="row">
        <NumberField className="narrow" step="any" value={e.t} onSave={(t) => t !== null && save({ t })} />
        <TextField className="grow" value={e.name} onSave={(name) => save({ name })} />
        <label className="row small"><input type="checkbox" checked={e.reviewed} onChange={(ev) => save({ reviewed: ev.target.checked })} /> reviewed</label>
        <button className="icon danger" title="delete" onClick={() => confirm(`Delete "${e.name}"?`) && store.deleteEvent(e.id)}>×</button>
      </div>
      <TextArea rows={2} placeholder="body" value={e.body} onSave={(body) => save({ body })} />
    </div>
  )
}
