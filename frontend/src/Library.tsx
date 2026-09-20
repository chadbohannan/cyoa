// Landing screen: every story as a card. The only surface that is not
// story-scoped.

import { useEffect, useState } from 'react'
import { api, type StorySummary } from './api'

export function Library({ onOpen }: { onOpen: (id: number) => void }) {
  const [stories, setStories] = useState<StorySummary[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  const load = () => api.stories().then(setStories).catch((e) => setError(String(e)))
  useEffect(() => { load() }, [])

  async function create() {
    const title = prompt('Story title?')
    if (!title) return
    const s = await api.createStory(title)
    onOpen(s.id)
  }
  async function rename(s: StorySummary) {
    const title = prompt('New title', s.title)
    if (!title || title === s.title) return
    await api.updateStory(s.id, { title })
    load()
  }
  async function remove(s: StorySummary) {
    if (!confirm(`Delete "${s.title}" and its ${s.passage_count} passages? This cannot be undone.`)) return
    await api.deleteStory(s.id)
    load()
  }

  return (
    <div className="library">
      <div className="row">
        <h1>Stories</h1>
        <span className="spacer" />
        <button onClick={create}>New story</button>
      </div>
      {error && <p className="error">{error}</p>}
      {stories?.length === 0 && <p className="muted">No stories yet. Create one to get a map with its start passage.</p>}
      <div className="cards">
        {stories?.map((s) => (
          <div key={s.id} className="card story-card" onClick={() => onOpen(s.id)}>
            <h3>{s.title}</h3>
            {s.description && <p className="muted">{s.description}</p>}
            <p className="small muted">
              {s.passage_count} passage{s.passage_count === 1 ? '' : 's'} · edited {formatWhen(s.updated_at)}
            </p>
            <div className="row" onClick={(e) => e.stopPropagation()}>
              <button onClick={() => onOpen(s.id)}>Open</button>
              <button onClick={() => rename(s)}>Rename</button>
              <span className="spacer" />
              <button className="danger" onClick={() => remove(s)}>Delete</button>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

// SQLite writes UTC 'YYYY-MM-DD HH:MM:SS'; render it in local time.
function formatWhen(s: string) {
  const d = new Date(s.replace(' ', 'T') + 'Z')
  return isNaN(d.getTime()) ? s : d.toLocaleString()
}
