// A story's pacing plan (concepts/act-pacing.md): the acts expand_frontier paces a
// run by, then the fixed ending. The Agents view edits it; the run dock summarises
// it. Like the other story fields it autosaves: edits apply locally at once and are
// written after a short pause, so typing guidance doesn't PUT per keystroke. A 422
// leaves the local plan in place with the reason; the next edit retries.

import { useCallback, useEffect, useRef, useState } from 'react'
import { api, type PacingAct, type PacingPlan } from './api'
import { errorReason } from './usePrompts'

const SAVE_DELAY = 500

// Levels the plan grows below the run's root: every act's, plus the ending's one.
export const planDepth = (plan: Pick<PacingPlan, 'acts'>) => plan.acts.reduce((n, a) => n + a.levels, 0) + 1

// The first level of each stage (acts, then the ending at index acts.length).
export function stageStarts(plan: Pick<PacingPlan, 'acts'>): number[] {
  const starts = [0]
  for (const a of plan.acts) starts.push(starts[starts.length - 1] + a.levels)
  return starts
}

export const NEW_ACT: PacingAct = { name: 'New act', levels: 1, branching: 2, ends_story: true, length: '200–300 words', guidance: '' }

export function usePacing(storyId: number) {
  const [plan, setPlan] = useState<PacingPlan | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const timer = useRef<number | undefined>(undefined)

  useEffect(() => {
    api.pacing.get(storyId).then(setPlan).catch((e) => setError(errorReason(e)))
  }, [storyId])

  const write = useCallback(async (next: PacingPlan) => {
    setSaving(true)
    try {
      await api.pacing.save(storyId, next)
      setError(null)
    } catch (e) {
      setError(errorReason(e))
    } finally {
      setSaving(false)
    }
  }, [storyId])

  // Apply an edit locally now; save the result once edits pause.
  const update = (fn: (p: PacingPlan) => PacingPlan) => {
    setPlan((p) => {
      if (!p) return p
      const next = fn(p)
      window.clearTimeout(timer.current)
      timer.current = window.setTimeout(() => write(next), SAVE_DELAY)
      return next
    })
  }
  useEffect(() => () => window.clearTimeout(timer.current), [])

  const updateAct = (i: number, patch: Partial<PacingAct>) =>
    update((p) => ({ ...p, acts: p.acts.map((a, j) => (j === i ? { ...a, ...patch } : a)) }))
  const addAct = (at: number) => update((p) => ({ ...p, acts: [...p.acts.slice(0, at), { ...NEW_ACT }, ...p.acts.slice(at)] }))
  const removeAct = (i: number) => update((p) => (p.acts.length > 1 ? { ...p, acts: p.acts.filter((_, j) => j !== i) } : p))
  const moveAct = (i: number, by: -1 | 1) =>
    update((p) => {
      const j = i + by
      if (j < 0 || j >= p.acts.length) return p
      const acts = [...p.acts]
      ;[acts[i], acts[j]] = [acts[j], acts[i]]
      return { ...p, acts }
    })
  const reset = async () => {
    window.clearTimeout(timer.current)
    try {
      setPlan(await api.pacing.reset(storyId))
      setError(null)
    } catch (e) {
      setError(errorReason(e))
    }
  }

  return { plan, error, saving, update, updateAct, addAct, removeAct, moveAct, reset }
}

export type PacingStore = ReturnType<typeof usePacing>
