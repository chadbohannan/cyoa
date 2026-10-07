import { useEffect, useState } from 'react'
import { AgentsView } from './AgentsView'
import { Inspector } from './Inspector'
import { Library } from './Library'
import { LoreView } from './LoreView'
import { useInterview } from './useInterview'
import { Player } from './Player'
import { Settings } from './Settings'
import { RunDock } from './RunDock'
import { StoryCanvas, type Selection } from './StoryCanvas'
import { usePacing } from './usePacing'
import { usePrompts, type PromptStore } from './usePrompts'
import { useStory } from './useStory'

type Tab = 'map' | 'lore' | 'play' | 'agents'
const TABS: [Tab, string][] = [['map', 'Map'], ['lore', 'Lore'], ['play', 'Play'], ['agents', 'Agents']]

// The story id lives in the URL hash so a reload lands on the same story.
function hashSettings(): boolean {
  return location.hash === '#/settings'
}

function hashStory(): number | null {
  const m = location.hash.match(/^#\/story\/(\d+)/)
  return m ? Number(m[1]) : null
}

export default function App() {
  const [storyId, setStoryId] = useState<number | null>(hashStory)
  const [settings, setSettings] = useState(hashSettings)
  // Prompt roles are global, so their drafts live above any one story.
  const prompts = usePrompts()
  useEffect(() => {
    const onHash = () => {
      setStoryId(hashStory())
      setSettings(hashSettings())
    }
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])
  const open = (id: number | null) => {
    location.hash = id === null ? '' : `#/story/${id}`
  }

  // Back from Settings returns to wherever it was opened from, or the Library when it was the landing page.
  const closeSettings = () => (history.length > 1 ? history.back() : open(null))
  if (settings) return <Settings onBack={closeSettings} />
  if (storyId === null) return <Library onOpen={open} />
  return <StoryShell key={storyId} storyId={storyId} prompts={prompts} onBack={() => open(null)} />
}

function StoryShell({ storyId, prompts, onBack }: { storyId: number; prompts: PromptStore; onBack: () => void }) {
  const store = useStory(storyId)
  const pacing = usePacing(storyId)
  // held here, not in the Lore tab, so a turn in flight survives switching tabs
  const interview = useInterview(store)
  const [tab, setTab] = useState<Tab>('map')
  const [selection, setSelection] = useState<Selection>(null)
  // "Play from here" on the map; null plays from the story's start passage
  const [playFrom, setPlayFrom] = useState<number | null>(null)
  const { graph, error } = store
  const playFromPassage = (id: number) => {
    setPlayFrom(id)
    setTab('play')
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
        <a className="link" href="#/settings" title="model and concurrency">Settings</a>
      </header>
      <main>
        {!graph ? (
          <p className="hint">Loading…</p>
        ) : tab === 'lore' ? (
          <LoreView store={store} interview={interview} />
        ) : tab === 'play' ? (
          <Player key={playFrom ?? 'start'} store={store} from={playFrom} onStop={() => setPlayFrom(null)} />
        ) : tab === 'agents' ? (
          <AgentsView store={store} prompts={prompts} pacing={pacing} mapPassage={selectedPassage?.id ?? null} />
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
              <RunDock store={store} pacing={pacing} root={selectedPassage} onSelect={setSelection} onEditPacing={() => setTab('agents')} />
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
