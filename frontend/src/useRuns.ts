// The run side of a story: the `agent_runs` rows the backend owns, and the SSE
// tail of every running one. The stream drives three things — `activity`
// (passage → phase; the map pulses that card, `done` clears it), `progress`
// (applied to the row as the backend sends it, whole), and `node` (a finished
// grow / check_passage means rows landed, so the graph is reloaded; the last
// node per run feeds the Agents view). `status` closes the stream.

import { useCallback, useEffect, useRef, useState } from 'react'
import { api, type AgentRun, type RunCreate, type RunEvent } from './api'
import { guardWith } from './store'

const LANDS_ROWS = new Set(['grow', 'check_passage'])

export function useRuns(storyId: number, deps: { reload: () => void; model: string | null; setError: (e: string | null) => void }) {
  const { reload, model, setError } = deps
  const guard = guardWith(setError)
  const [runs, setRuns] = useState<AgentRun[]>([])
  const [activity, setActivity] = useState<Record<number, string>>({})
  const [lastNode, setLastNode] = useState<Record<number, string>>({})
  const streams = useRef<Record<number, EventSource>>({})

  const refreshRuns = useCallback(() => {
    api.runs.list(storyId).then(setRuns).catch((e) => setError(String(e)))
  }, [storyId, setError])
  useEffect(refreshRuns, [refreshRuns])

  const patchRun = (id: number, fn: (r: AgentRun) => AgentRun) => setRuns((rs) => rs.map((r) => (r.id === id ? fn(r) : r)))

  useEffect(() => {
    const open = streams.current
    for (const run of runs) {
      if (run.status !== 'running' || open[run.id]) continue
      const es = api.runs.events(storyId, run.id)
      open[run.id] = es
      const close = () => {
        es.close()
        delete open[run.id]
        refreshRuns()
      }
      es.onmessage = (m) => {
        const ev = JSON.parse(m.data) as RunEvent
        switch (ev.type) {
          case 'activity':
            setActivity((a) => {
              const next = { ...a }
              if (ev.phase === 'done') delete next[ev.passage_id]
              else next[ev.passage_id] = ev.phase
              return next
            })
            break
          case 'progress': {
            patchRun(run.id, (r) => ({ ...r, progress: Object.fromEntries(Object.entries(ev).filter(([k]) => !['type', 'seq', 'at'].includes(k))) }))
            break
          }
          case 'node':
            setLastNode((n) => ({ ...n, [run.id]: ev.node }))
            if (LANDS_ROWS.has(ev.node)) reload()
            break
          case 'status':
            close()
            setActivity({})
            reload()
            break
        }
      }
      es.onerror = close
    }
  }, [runs, storyId, reload, refreshRuns])
  useEffect(() => {
    const open = streams.current
    return () => Object.values(open).forEach((es) => es.close())
  }, [])

  const startRun = guard(async (req: Omit<RunCreate, 'model'>) => {
    const run = await api.runs.start(storyId, { ...req, model })
    setRuns((rs) => [run, ...rs])
    return run
  })
  const resumeRun = guard(async (id: number, body: { steer?: string | null; stop?: boolean }) => {
    const run = await api.runs.resume(storyId, id, body)
    patchRun(id, () => run)
  })
  const cancelRun = guard(async (id: number) => {
    const run = await api.runs.cancel(storyId, id)
    patchRun(id, () => run)
  })

  return { runs, activity, lastNode, refreshRuns, startRun, resumeRun, cancelRun }
}
