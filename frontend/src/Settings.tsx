// Machine settings: which model the agents ask by default, and how many model calls may be in
// flight at once. Both belong to this machine's model server, not to a story, so they live on
// their own screen, saved explicitly (like a prompt role) and applied by the backend at once.

import { useEffect, useState } from 'react'
import { api, type SettingsInfo } from './api'

function reason(e: unknown): string {
  const m = String(e).match(/^Error: \d+ (.*)$/s)
  try {
    return m ? JSON.parse(m[1]).detail?.[0]?.msg ?? JSON.parse(m[1]).detail ?? m[1] : String(e)
  } catch {
    return m ? m[1] : String(e)
  }
}

export function Settings({ onBack }: { onBack: () => void }) {
  const [info, setInfo] = useState<SettingsInfo | null>(null)
  const [models, setModels] = useState<string[] | null>(null)
  const [modelsError, setModelsError] = useState<string | null>(null)
  const [model, setModel] = useState('') // '' = the launch configuration's model
  const [conc, setConc] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)

  const load = (i: SettingsInfo) => {
    setInfo(i)
    setModel(i.saved.model ?? '')
    setConc(String(i.saved.concurrency ?? i.concurrency))
  }
  const loadModels = () => {
    setModelsError(null)
    api.llmModels().then(setModels).catch((e) => { setModels(null); setModelsError(reason(e)) })
  }
  useEffect(() => {
    api.settings.get().then(load).catch((e) => setError(reason(e)))
    loadModels()
  }, [])

  if (!info) return <div className="library"><button className="link" onClick={onBack}>← Back</button><p className="hint">{error ?? 'Loading…'}</p></div>

  const concNum = Number(conc)
  const concValid = Number.isInteger(concNum) && concNum >= 1 && concNum <= 64
  const dirty = model !== (info.saved.model ?? '') || (concValid && concNum !== (info.saved.concurrency ?? info.concurrency))
  const save = async () => {
    setError(null)
    try {
      load(await api.settings.save({
        model: model || null,
        // saving the launch value stores nothing: the setting stays "unset" and follows the environment
        concurrency: concNum === info.env_concurrency ? null : concNum,
      }))
      setSaved(true)
      setTimeout(() => setSaved(false), 2000)
    } catch (e) {
      setError(reason(e))
    }
  }

  return (
    <div className="library settings">
      <div className="row">
        <button className="link" onClick={onBack}>← Back</button>
        <h1>Settings</h1>
      </div>
      <p className="small muted">
        The model server is <code>{info.url}</code> ({info.backend}). Changes apply to the next model call; calls in
        flight finish as they were.
      </p>

      <div className="panel-head"><span className="kind">Model</span></div>
      <label className="field">
        <span>Default model <span className="muted small">— asked whenever a request names none</span></span>
        <select value={model} onChange={(e) => setModel(e.target.value)}>
          <option value="">launch default ({info.env_model})</option>
          {(models ?? []).map((m) => <option key={m} value={m}>{m}</option>)}
          {model && !models?.includes(model) && <option value={model}>{model} (not listed by the server)</option>}
        </select>
      </label>
      <div className="row small muted">
        {modelsError ? <span className="error">The server's models could not be listed: {modelsError}</span>
          : models ? <span>{models.length} model{models.length === 1 ? '' : 's'} on the server</span> : <span>asking the server…</span>}
        <button className="link" onClick={loadModels}>refresh</button>
      </div>

      <div className="panel-head"><span className="kind">Concurrency</span></div>
      <label className="field">
        <span>Model calls in flight <span className="muted small">— 1 to 64</span></span>
        <input type="number" min={1} max={64} value={conc} onChange={(e) => setConc(e.target.value)} />
      </label>
      <p className="small muted">
        Keep this at 1. A run writes one passage at a time, depth-first, whatever the setting; it only limits how
        many model calls overlap (auditors within a passage, or several runs at once). Two contexts at once also
        ran a 12 GB GPU out of memory on a single local model. Launch default: {info.env_concurrency}
        (<code>CYOA_AGENT_CONCURRENCY</code>).
      </p>

      <div className="row">
        <button onClick={save} disabled={!dirty || !concValid}>Save</button>
        {saved && <span className="muted small">saved</span>}
        {!concValid && <span className="error small">concurrency must be a whole number from 1 to 64</span>}
        {error && <span className="error small">{error}</span>}
      </div>
    </div>
  )
}
