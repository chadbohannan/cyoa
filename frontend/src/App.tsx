import { useEffect, useState } from 'react'
import { api, type Story, type StoryGraph } from './api'
import { StoryCanvas } from './StoryCanvas'

export default function App() {
  const [stories, setStories] = useState<Story[]>([])
  const [selected, setSelected] = useState<number | null>(null)
  const [graph, setGraph] = useState<StoryGraph | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    api.stories().then(setStories).catch((e) => setError(String(e)))
  }, [])

  useEffect(() => {
    if (selected === null) return setGraph(null)
    api.graph(selected).then(setGraph).catch((e) => setError(String(e)))
  }, [selected])

  async function newStory() {
    const title = prompt('Story title?')
    if (!title) return
    const s = await api.createStory(title)
    setStories((prev) => [...prev, s])
    setSelected(s.id)
  }

  return (
    <div className="app">
      <header>
        <strong>cyoa</strong>
        <select value={selected ?? ''} onChange={(e) => setSelected(e.target.value ? Number(e.target.value) : null)}>
          <option value="">— pick a story —</option>
          {stories.map((s) => (
            <option key={s.id} value={s.id}>{s.title}</option>
          ))}
        </select>
        <button onClick={newStory}>New story</button>
        {error && <span className="error">{error}</span>}
      </header>
      <main>
        {graph ? <StoryCanvas graph={graph} /> : <p className="hint">Pick or create a story to see its graph.</p>}
      </main>
    </div>
  )
}
