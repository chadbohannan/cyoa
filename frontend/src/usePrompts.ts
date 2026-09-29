// The prompt roles (prompts.py) and the author's unsaved drafts of them. Not tied
// to a story: a role is one text for every story, so App holds this above the
// story shell and a draft survives switching node, tab or story.
//
// Unlike every other field in the editor, a role is saved explicitly. A field
// edits one row of one story; a role edits every future model call in every
// story, so autosave would put half-typed text in front of a verb running in
// another tab, and would validate every pause while a brace is half typed.
// Save validates on the backend; a 422 leaves the draft in place with the reason.

import { useEffect, useState } from 'react'
import { api, type PromptInfo } from './api'

// `request` throws "<status> <body>"; FastAPI's body is {"detail": "..."}.
function reason(e: unknown): string {
  const m = String(e).match(/^Error: \d+ (.*)$/s)
  try {
    return m ? JSON.parse(m[1]).detail ?? m[1] : String(e)
  } catch {
    return m ? m[1] : String(e)
  }
}

export function usePrompts() {
  const [prompts, setPrompts] = useState<PromptInfo[] | null>(null)
  const [drafts, setDrafts] = useState<Record<string, string>>({})
  const [errors, setErrors] = useState<Record<string, string | null>>({})
  const [loadError, setLoadError] = useState<string | null>(null)

  useEffect(() => {
    api.prompts.list().then(setPrompts).catch((e) => setLoadError(reason(e)))
  }, [])

  const replace = (p: PromptInfo) => setPrompts((ps) => ps?.map((x) => (x.key === p.key ? p : x)) ?? ps)
  const drop = (key: string) => {
    setDrafts(({ [key]: _, ...rest }) => rest)
    setErrors((es) => ({ ...es, [key]: null }))
  }

  const saved = (p: PromptInfo) => p.override ?? p.default
  const text = (p: PromptInfo) => drafts[p.key] ?? saved(p)
  const dirty = (p: PromptInfo) => p.key in drafts && drafts[p.key] !== saved(p)

  const setDraft = (key: string, value: string) => {
    setDrafts((d) => ({ ...d, [key]: value }))
    setErrors((es) => ({ ...es, [key]: null }))
  }
  const save = async (key: string) => {
    if (!(key in drafts)) return
    try {
      replace(await api.prompts.save(key, { text: drafts[key] }))
      drop(key)
    } catch (e) {
      setErrors((es) => ({ ...es, [key]: reason(e) }))
    }
  }
  const reset = async (key: string) => {
    try {
      replace(await api.prompts.reset(key))
      drop(key)
    } catch (e) {
      setErrors((es) => ({ ...es, [key]: reason(e) }))
    }
  }

  return { prompts, loadError, errors, text, dirty, setDraft, save, revert: drop, reset }
}

export type PromptStore = ReturnType<typeof usePrompts>
export { reason as errorReason }
