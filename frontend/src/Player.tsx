// The reader view. The server is stateless: each step is a PlayView it handed
// back, and the frontend keeps a stack of them. Back pops, Restart clears, and
// the debug drawer lets the author jump to any earlier step.

import { useEffect, useState } from 'react'
import { api, type Choice, type Playtest, type PlayView } from './api'
import type { StoryStore } from './useStory'

type Step = { view: PlayView; via: Choice | null }

type Props = { store: StoryStore; from: number | null; onStop: () => void }

export function Player({ store, from, onStop }: Props) {
  const { graph, storyId } = store
  const [steps, setSteps] = useState<Step[]>([])
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  // Restart bumps the nonce, which re-runs the initial load.
  const [nonce, setNonce] = useState(0)
  useEffect(() => {
    let live = true
    api.play
      .start(storyId, from)
      .then((view) => live && setSteps([{ view, via: null }]))
      .catch((e) => live && setError((e as Error).message))
    return () => {
      live = false
    }
  }, [storyId, from, nonce])
  function restart() {
    setSteps([])
    setError(null)
    setNonce((n) => n + 1)
  }

  async function choose(c: Choice) {
    const cur = steps[steps.length - 1]
    if (!cur) return
    setBusy(true)
    setError(null)
    try {
      const view = await api.play.choose(storyId, c.id, cur.view.state)
      setSteps((s) => [...s, { view, via: c }])
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const cur = steps[steps.length - 1]
  const isFromHere = from !== null && from !== graph?.story.start_passage_id

  return (
    <div className="player">
      <div className="player-bar">
        <button disabled={steps.length < 2 || busy} onClick={() => setSteps((s) => s.slice(0, -1))}>← Back</button>
        <button disabled={busy} onClick={restart}>Restart</button>
        {isFromHere && <button disabled={busy} onClick={onStop}>From the beginning</button>}
        <span className="spacer" />
        <span className="muted small">step {steps.length}</span>
      </div>

      {error && <p className="error">{error}</p>}
      {cur && (
        <article className="reader">
          <h1>{cur.view.passage.title || <em>untitled</em>}</h1>
          {cur.view.passage.body.split('\n').filter(Boolean).map((line, i) => <p key={i}>{line}</p>)}
          <div className="choices">
            {cur.view.choices.length === 0 && <p className="muted">The End.</p>}
            {cur.view.choices.map((c) => (
              <button key={c.id} className="choice" disabled={busy} onClick={() => choose(c)}>
                {c.text || <em>untitled</em>}
              </button>
            ))}
          </div>
        </article>
      )}

      {cur && (
        <details className="debug">
          <summary>Debug</summary>
          <div className="debug-grid">
            <section>
              <h4>State</h4>
              <table className="vars">
                <tbody>
                  {Object.entries(cur.view.state).map(([k, v]) => (
                    <tr key={k}><td><code>{k}</code></td><td><code>{JSON.stringify(v)}</code></td></tr>
                  ))}
                  {Object.keys(cur.view.state).length === 0 && <tr><td className="muted">no variables</td></tr>}
                </tbody>
              </table>
              <h4>Last effects</h4>
              <pre className="mono">{cur.via ? JSON.stringify(cur.via.effects) : '—'}</pre>
            </section>
            <section>
              <h4>Path</h4>
              <ol className="path">
                {steps.map((s, i) => (
                  <li key={i}>
                    <button className="link" disabled={i === steps.length - 1} onClick={() => setSteps(steps.slice(0, i + 1))}>
                      {s.view.passage.title || 'untitled'}
                    </button>
                    {s.via && <span className="muted small"> via “{s.via.text}”</span>}
                  </li>
                ))}
              </ol>
            </section>
          </div>
        </details>
      )}

      <PlaytestPanel store={store} />
    </div>
  )
}

// Random playthroughs through the same evaluator as the reader above: a
// coverage report over the whole story, not a run. Titles are looked up in the
// graph so the report reads as passages, not ids.
function PlaytestPanel({ store }: { store: StoryStore }) {
  const { graph, storyId } = store
  const [runs, setRuns] = useState(200)
  const [report, setReport] = useState<Playtest | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const title = (id: number | string) => graph?.passages.find((p) => p.id === Number(id))?.title || `#${id}`
  const choiceLabel = (id: number | string) => {
    const c = graph?.choices.find((c) => c.id === Number(id))
    return c ? `${title(c.from_passage)} → “${c.text}”` : `#${id}`
  }
  const counts = (m: Record<string, number>, label: (id: string) => string) =>
    Object.entries(m).sort((a, b) => b[1] - a[1]).map(([id, n]) => <li key={id}>{label(id)} <span className="muted small">×{n}</span></li>)

  async function run() {
    setBusy(true)
    setError(null)
    try {
      setReport(await api.playtest(storyId, { runs }))
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <details className="debug playtest">
      <summary>Random playtest{report ? ` · ${report.unvisited.length} unvisited · ${Object.keys(report.stuck).length} stuck` : ''}</summary>
      <div className="player-bar">
        <label>runs <input type="number" min={1} max={10000} value={runs} onChange={(e) => setRuns(Number(e.target.value) || 1)} style={{ width: '5em' }} /></label>
        <button disabled={busy || !graph} onClick={run}>{busy ? 'Playing…' : 'Play randomly'}</button>
        {report && (
          <span className="muted small">
            {report.runs} walks · mean {report.mean_length.toFixed(1)} steps · {Object.keys(report.visited).length}/{graph?.passages.length ?? 0} passages seen
            {report.cut > 0 && ` · ${report.cut} cut by the step cap`}
          </span>
        )}
      </div>
      {error && <p className="error">{error}</p>}
      {report && (
        <div className="debug-grid">
          <section>
            <h4>Endings reached</h4>
            <ul className="path">{counts(report.endings, title)}{Object.keys(report.endings).length === 0 && <li className="muted">none</li>}</ul>
            <h4>Stuck (choices exist, none open)</h4>
            <ul className="path">{counts(report.stuck, title)}{Object.keys(report.stuck).length === 0 && <li className="muted">none</li>}</ul>
            <h4>Dangling choices hit</h4>
            <ul className="path">{counts(report.dangling, choiceLabel)}{Object.keys(report.dangling).length === 0 && <li className="muted">none</li>}</ul>
          </section>
          <section>
            <h4>Never visited ({report.unvisited.length})</h4>
            <ul className="path">{report.unvisited.map((id) => <li key={id}>{title(id)}</li>)}{report.unvisited.length === 0 && <li className="muted">none</li>}</ul>
            <h4>Never taken ({report.untaken.length})</h4>
            <ul className="path">{report.untaken.map((id) => <li key={id}>{choiceLabel(id)}</li>)}{report.untaken.length === 0 && <li className="muted">none</li>}</ul>
          </section>
        </div>
      )}
    </details>
  )
}
