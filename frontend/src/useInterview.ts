// The story's Interview Me conversation (concepts/interview-me.md): the latest open
// interview, the turn in flight, and what the last turn wrote so the Lore view can
// flash it. Every turn and revert writes the premise or lore server-side, so each
// one reloads the story graph afterwards.

import { useEffect, useRef, useState } from 'react'
import { api, type Interview, type InterviewMode } from './api'
import type { StoryStore } from './useStory'

const FLASH_MS = 2500

// What the latest agent turn changed: the premise, and lore rows by id.
export interface Touched {
  premise: boolean
  lore: Set<number>
}

const NOTHING: Touched = { premise: false, lore: new Set() }

function touchedBy(iv: Interview): Touched {
  const last = [...iv.transcript].reverse().find((e) => e.role === 'agent')
  const writes = (last?.writes ?? []).filter((w) => !w.reverted)
  return {
    premise: writes.some((w) => w.table === 'stories'),
    lore: new Set(writes.filter((w) => w.table === 'lore' && w.after).map((w) => w.id)),
  }
}

export function useInterview(store: StoryStore) {
  const { storyId, reload } = store
  const [interview, setInterview] = useState<Interview | null>(null)
  const [loaded, setLoaded] = useState(false)
  // 'start' | 'turn' | 'revert' while a request is out; a turn can wait minutes behind a run
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [touched, setTouched] = useState<Touched>(NOTHING)
  // the panel's visibility; hiding it leaves the interview open
  const [shown, setShown] = useState(false)
  const timer = useRef<number | undefined>(undefined)

  useEffect(() => {
    setLoaded(false)
    api.interviews.list(storyId)
      .then((all) => setInterview([...all].reverse().find((iv) => iv.status === 'open') ?? null))
      .catch((e) => setError(String(e)))
      .finally(() => setLoaded(true))
  }, [storyId])
  useEffect(() => () => window.clearTimeout(timer.current), [])

  const flash = (t: Touched) => {
    window.clearTimeout(timer.current)
    setTouched(t)
    timer.current = window.setTimeout(() => setTouched(NOTHING), FLASH_MS)
  }

  // Resolves to whether the request succeeded, so the caller can keep an unsent answer.
  const call = async (kind: string, fn: () => Promise<Interview>, flashIt: boolean): Promise<boolean> => {
    setBusy(kind)
    setError(null)
    try {
      const iv = await fn()
      setInterview(iv.status === 'open' ? iv : null)
      reload()
      if (flashIt) flash(touchedBy(iv))
      return true
    } catch (e) {
      setError(String(e))
      return false
    } finally {
      setBusy(null)
    }
  }

  const start = () => call('start', () => api.interviews.start(storyId), true)
  const send = (mode: InterviewMode, text = '') =>
    interview ? call('turn', () => api.interviews.turn(storyId, interview.id, { mode, text }), true) : Promise.resolve(false)
  const revert = (entry: number, write?: number) =>
    interview ? call('revert', () => api.interviews.revert(storyId, interview.id, { entry, write }), false) : Promise.resolve(false)
  const close = () => (interview ? call('close', () => api.interviews.close(storyId, interview.id), false) : Promise.resolve(false))

  return { interview, loaded, busy, error, touched, shown, setShown, start, send, revert, close }
}

export type InterviewStore = ReturnType<typeof useInterview>
