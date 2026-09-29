import { useEffect, useState } from 'react'
import { AgentsView } from './AgentsView'
import { api } from './api'
import { Inspector } from './Inspector'
import { Library } from './Library'
import { LoreView } from './LoreView'
import { Player } from './Player'
import { RunDock } from './RunDock'
import { StoryCanvas, type Selection } from './StoryCanvas'
import { usePrompts, type PromptStore } from './usePrompts'
import { useStory, type StoryStore } from './useStory'

type Tab = 'map' | 'lore' | 'play' | 'agents'
const TABS: [Tab, string][] = [['map', 'Map'], ['lore', 'Lore'], ['play', 'Play'], ['agents', 'Agents']]

// The story id lives in the URL hash so a reload lands on the same story.
function hashStory(): number | null {
  const m = location.hash.match(/^#\/story\/(\d+)/)
  return m ? Number(m[1]) : null
}

export default function App() {
  const [storyId, setStoryId] = useState<number | null>(hashStory)
  // Prompt roles are global, so their drafts live above any one story.
  const prompts = usePrompts()
  useEffect(() => {
    const onHash = () => setStoryId(hashStory())
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])
  const open = (id: number | null) => {
    location.hash = id === null ? '' : `#/story/${id}`
  }

  if (storyId === null) return <Library onOpen={open} />
  return <StoryShell key={storyId} storyId={storyId} prompts={prompts} onBack={() => open(null)} />
}

function StoryShell({ storyId, prompts, onBack }: { storyId: number; prompts: PromptStore; onBack: () => void }) {
  const store = useStory(storyId)
  const [tab, setTab] = useState<Tab>('map')
  const [selection, setSelection] = useState<Selection>(null)
  // "Play from here" on the map; null plays from the story's start passage
  const [playFrom, setPlayFrom] = useState<number | null>(null)
  // The run the Agents view is showing; set from the run dock's "workflow" link
  const [showRun, setShowRun] = useState<number | null>(null)
  const { graph, error } = store
  const playFromPassage = (id: number) => {
    setPlayFrom(id)
    setTab('play')
  }
  const showRunInAgents = (id: number) => {
    setShowRun(id)
    setTab('agents')
  }
  const selectedPassage = selection?.kind === 'passage' ? graph?.passages.find((p) => p.id === selection.id) ?? null : null

  return (
    <div className="app">
      <header>
        <button className="link" onClick={onBack}>← Library</button>
        <strong>{graph?.story.title ?? '…'}</strong>
        <nav className="tabs">
          {TABS.map(([t, label]) => (
            <button key={t} className={tab === t ? 'active' : ''} onClick={() => setTab(t)}>
              {label}
            </button>
          ))}
        </nav>
        <span className="spacer" />
        {error && <span className="error">{error}</span>}
        <ModelPicker store={store} />
      </header>
      <main>
        {!graph ? (
          <p className="hint">Loading…</p>
        ) : tab === 'lore' ? (
          <LoreView store={store} />
        ) : tab === 'play' ? (
          <Player key={playFrom ?? 'start'} store={store} from={playFrom} onStop={() => setPlayFrom(null)} />
        ) : tab === 'agents' ? (
          <AgentsView store={store} prompts={prompts} runId={showRun} onPickRun={setShowRun} mapPassage={selectedPassage?.id ?? null} />
        ) : (
          <div className="editor">
            <div className="map-column">
              <StoryCanvas
                store={store}
                selection={selection}
                onSelect={setSelection}
                onPlayFrom={playFromPassage}
                onExpandFrom={(id) => setSelection({ kind: 'passage', id })}
              />
              <RunDock store={store} root={selectedPassage} onSelect={setSelection} onShowRun={showRunInAgents} />
            </div>
            <aside className="inspector">
              <Inspector store={store} selection={selection} onSelect={setSelection} />
            </aside>
          </div>
        )}
      </main>
    </div>
  )
}

// Which Ollama model the assist verbs use; blank means the backend's default.
function ModelPicker({ store }: { store: StoryStore }) {
  const [models, setModels] = useState<string[] | null>(null)
  const [err, setErr] = useState(false)
  useEffect(() => {
    api.llmModels().then(setModels).catch(() => setErr(true))
  }, [])
  if (err) return <span className="muted small" title="Ollama unreachable">no model</span>
  return (
    <select className="model" value={store.model ?? ''} onChange={(e) => store.setModel(e.target.value || null)} title="model for Assist">
      <option value="">default model</option>
      {(models ?? []).map((m) => <option key={m} value={m}>{m}</option>)}
      {store.model && !models?.includes(store.model) && <option value={store.model}>{store.model}</option>}
    </select>
  )
}
