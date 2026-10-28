// The story bible: the premise (the story's description and its opening passage,
// which every agent prompt starts from) and lore entries grouped by kind. Plain
// list-and-form; this is the baseline agents coordinate storylines around, so it
// stays human-editable first. Interview Me docks beside it and flashes the premise
// and the characters each of its turns changed.

import { useState } from 'react'
import { LORE_KINDS, type Lore, type LoreKind } from './api'
import { TextArea, TextField } from './fields'
import { InterviewPanel } from './InterviewPanel'
import type { InterviewStore, Touched } from './useInterview'
import type { StoryStore } from './useStory'

export function LoreView({ store, interview }: { store: StoryStore; interview: InterviewStore }) {
  const { graph } = store
  if (!graph) return null
  const { touched } = interview
  return (
    <div className="lore-shell">
      <div className="lore">
        <Premise store={store} interview={interview} />
        <section>
          <h2>Lore</h2>
          <p className="muted">Characters, places, factions, rules and facts that hold on every branch. A fact is named in a passage’s reveals and needs, which fact_check walks per route.</p>
          {LORE_KINDS.map((kind) => (
            <LoreGroup key={kind} kind={kind} entries={graph.lore.filter((l) => l.kind === kind)} store={store} touched={touched} />
          ))}
        </section>
      </div>
      {interview.shown && <InterviewPanel iv={interview} store={store} onHide={() => interview.setShown(false)} />}
    </div>
  )
}

// The description heads every prompt as STORY:, and the opening passage is the
// root runs grow from and the first line of every STORY SO FAR.
function Premise({ store, interview }: { store: StoryStore; interview: InterviewStore }) {
  const { story, passages } = store.graph!
  const start = passages.find((p) => p.id === story.start_passage_id)
  const label = interview.interview ? 'Resume interview' : 'Interview me'
  return (
    <section className="premise">
      <div className="row">
        <h2>Premise</h2>
        <span className="spacer" />
        {!interview.shown && (
          <button onClick={() => interview.setShown(true)} disabled={!interview.loaded}
            title="an agent asks about your story and writes the premise and characters as you answer">{label}</button>
        )}
      </div>
      <p className="muted">What every agent reads before it writes: the story's description, then the opening passage.</p>
      <div className="premise-cols">
        <div>
          <label>Title<TextField value={story.title} onSave={(title) => store.updateStory({ title })} /></label>
          <label>Description<TextArea rows={8} className={interview.touched.premise ? 'touched' : undefined} value={story.description} placeholder="genre, who the reader is, what they want, what stands in the way"
            onSave={(description) => store.updateStory({ description })} /></label>
        </div>
        <div>
          {start ? (
            <>
              <label>Opening passage<TextField value={start.title} onSave={(title) => store.updatePassage(start.id, { title })} /></label>
              <label>Opening text<TextArea rows={8} value={start.body} onSave={(body) => store.updatePassage(start.id, { body })} /></label>
            </>
          ) : (
            <p className="muted">No start passage: right-click a passage on the map and choose "Set as start".</p>
          )}
        </div>
      </div>
    </section>
  )
}

function LoreGroup({ kind, entries, store, touched }: { kind: LoreKind; entries: Lore[]; store: StoryStore; touched: Touched }) {
  const [name, setName] = useState('')
  async function add() {
    if (!name.trim()) return
    if (await store.createLore({ kind, name: name.trim() })) setName('')
  }
  return (
    <div className="lore-group">
      <h3>{kind}s</h3>
      {entries.map((l) => <LoreCard key={l.id} l={l} store={store} touched={touched.lore.has(l.id)} />)}
      <div className="row">
        <input placeholder={`new ${kind}`} value={name} onChange={(e) => setName(e.target.value)} onKeyDown={(e) => e.key === 'Enter' && add()} />
        <button onClick={add} disabled={!name.trim()}>Add</button>
      </div>
    </div>
  )
}

function LoreCard({ l, store, touched }: { l: Lore; store: StoryStore; touched: boolean }) {
  const save = (body: Partial<Lore>) => store.updateLore(l.id, body)
  return (
    <div className={`card${touched ? ' touched' : ''}`}>
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
