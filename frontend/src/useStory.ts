// Owns one story's graph in memory. Every mutator calls the API and then
// patches the local graph with the row the server returned, so the UI never
// has to refetch the whole graph after an edit.

import { useCallback, useEffect, useState } from 'react'
import {
  api,
  type Choice,
  type AssistRequest,
  type ChoiceInput,
  type Lore,
  type LoreInput,
  type Passage,
  type PassageInput,
  type PassagePosition,
  type PromoteRequest,
  type Story,
  type StoryGraph,
  type Variable,
  type VariableInput,
} from './api'
import { guardWith } from './store'
import { useRuns } from './useRuns'


type Row = { id: number }
const upsert = <T extends Row>(rows: T[], row: T) =>
  rows.some((r) => r.id === row.id) ? rows.map((r) => (r.id === row.id ? row : r)) : [...rows, row]
const without = <T extends Row>(rows: T[], id: number) => rows.filter((r) => r.id !== id)

export function useStory(storyId: number) {
  const [graph, setGraph] = useState<StoryGraph | null>(null)
  const [error, setError] = useState<string | null>(null)

  const reload = useCallback(() => {
    api.graph(storyId).then(setGraph).catch((e) => setError(String(e)))
  }, [storyId])
  useEffect(reload, [reload])

  const guard = guardWith(setError)

  const patchGraph = (fn: (g: StoryGraph) => StoryGraph) => setGraph((g) => (g ? fn(g) : g))

  const updateStory = guard(async (body: Partial<Pick<Story, 'title' | 'description' | 'start_passage_id'>>) => {
    const story = await api.updateStory(storyId, body)
    patchGraph((g) => ({ ...g, story }))
    return story
  })

  const createPassage = guard(async (body: PassageInput) => {
    const p = await api.passages.create(storyId, body)
    patchGraph((g) => ({ ...g, passages: upsert(g.passages, p) }))
    return p
  })
  const updatePassage = guard(async (id: number, body: Partial<PassageInput>) => {
    const p = await api.passages.update(storyId, id, body)
    patchGraph((g) => ({ ...g, passages: upsert(g.passages, p) }))
    return p
  })
  const deletePassage = guard(async (id: number) => {
    await api.passages.remove(storyId, id)
    patchGraph((g) => ({
      ...g,
      story: g.story.start_passage_id === id ? { ...g.story, start_passage_id: null } : g.story,
      passages: without(g.passages, id),
      // mirror the server: outgoing choices cascade, inbound ones dangle
      choices: g.choices
        .filter((c) => c.from_passage !== id)
        .map((c) => (c.to_passage === id ? { ...c, to_passage: null } : c)),
    }))
  })
  const duplicatePassage = guard(async (id: number) => {
    const p = await api.passages.duplicate(storyId, id)
    reload() // the copy's choices were made server-side; cheapest to refetch
    return p
  })
  // Pruning deletes a passage's choices and its subtree; the server picks the subtree.
  const prunePassage = guard(async (id: number) => {
    const r = await api.passages.prune(storyId, id)
    const gone = new Set(r.passage_ids)
    const cut = new Set(r.choice_ids)
    patchGraph((g) => ({
      ...g,
      passages: g.passages.filter((p) => !gone.has(p.id)),
      choices: g.choices
        .filter((c) => !cut.has(c.id))
        .map((c) => (c.to_passage !== null && gone.has(c.to_passage) ? { ...c, to_passage: null } : c)),
    }))
    return r
  })
  // Promotion flips `reviewed` over a subtree or a selection at once; the server picks the subtree.
  const promote = guard(async (body: PromoteRequest) => {
    const { passage_ids } = await api.promote(storyId, body)
    const flipped = new Set(passage_ids)
    patchGraph((g) => ({ ...g, passages: g.passages.map((p) => (flipped.has(p.id) ? { ...p, reviewed: body.reviewed ?? true } : p)) }))
    return passage_ids
  })
  const saveLayout = guard(async (positions: PassagePosition[]) => {
    const { passage_ids } = await api.layout(storyId, positions)
    const moved = new Set(passage_ids)
    const at = new Map(positions.map((q) => [q.id, q]))
    patchGraph((g) => ({
      ...g,
      passages: g.passages.map((p) => (moved.has(p.id) ? { ...p, pos_x: at.get(p.id)!.pos_x, pos_y: at.get(p.id)!.pos_y } : p)),
    }))
    return passage_ids
  })
  const mergePassages = guard(async (into: number, ids: number[]) => {
    const p = await api.passages.merge(storyId, into, ids)
    reload() // choices were retargeted and moved server-side
    return p
  })

  const createChoice = guard(async (body: ChoiceInput) => {
    const c = await api.choices.create(storyId, body)
    patchGraph((g) => ({ ...g, choices: upsert(g.choices, c) }))
    return c
  })
  const updateChoice = guard(async (id: number, body: Partial<ChoiceInput>) => {
    const c = await api.choices.update(storyId, id, body)
    patchGraph((g) => ({ ...g, choices: upsert(g.choices, c) }))
    return c
  })
  const deleteChoice = guard(async (id: number) => {
    await api.choices.remove(storyId, id)
    patchGraph((g) => ({ ...g, choices: without(g.choices, id) }))
  })
  // Reorder a passage's choices: `ids` is the new order; sort_order becomes the index.
  const reorderChoices = guard(async (ids: number[]) => {
    const rows = await Promise.all(ids.map((id, i) => api.choices.update(storyId, id, { sort_order: i })))
    patchGraph((g) => ({ ...g, choices: rows.reduce(upsert, g.choices) }))
  })

  const createVariable = guard(async (body: VariableInput) => {
    const v = await api.variables.create(storyId, body)
    patchGraph((g) => ({ ...g, variables: upsert(g.variables, v) }))
    return v
  })
  const updateVariable = guard(async (id: number, body: Partial<VariableInput>) => {
    const v = await api.variables.update(storyId, id, body)
    patchGraph((g) => ({ ...g, variables: upsert(g.variables, v) }))
    return v
  })
  const deleteVariable = guard(async (id: number) => {
    await api.variables.remove(storyId, id)
    patchGraph((g) => ({ ...g, variables: without(g.variables, id) }))
  })

  const createLore = guard(async (body: LoreInput) => {
    const l = await api.lore.create(storyId, body)
    patchGraph((g) => ({ ...g, lore: upsert(g.lore, l) }))
    return l
  })
  const updateLore = guard(async (id: number, body: Partial<LoreInput>) => {
    const l = await api.lore.update(storyId, id, body)
    patchGraph((g) => ({ ...g, lore: upsert(g.lore, l) }))
    return l
  })
  const deleteLore = guard(async (id: number) => {
    await api.lore.remove(storyId, id)
    patchGraph((g) => ({ ...g, lore: without(g.lore, id) }))
  })

  // --- assist ---------------------------------------------------------------
  // `busy` maps passage id to the verb in flight against it, so a request against one passage
  // doesn't disable another's toolbar — the backend (its own concurrency gate, set on the Settings
  // screen) decides whether requests queue or run concurrently; the frontend just reflects what
  // it actually sent. The model is the backend's setting too, not sent from here.
  const [busy, setBusy] = useState<Record<number, string>>({})
  const assist = guard(async (passageId: number, req: AssistRequest) => {
    setBusy((b) => ({ ...b, [passageId]: req.verb }))
    try {
      const rows = await api.assist(storyId, passageId, req)
      if (rows.length > 0) {
        // Proposals were created; patch them in locally
        patchGraph((g) => ({ ...g, proposals: rows.reduce(upsert, g.proposals) }))
      } else {
        // Direct write on an unreviewed passage; reload the whole graph to see the change
        await reload()
      }
      return rows
    } finally {
      setBusy((b) => { const { [passageId]: _drop, ...rest } = b; return rest })
    }
  })
  // Accept materialises rows server-side (possibly a passage *and* a choice), so refetch.
  const acceptProposal = guard(async (id: number) => {
    await api.proposals.accept(storyId, id)
    reload()
  })
  const rejectProposal = guard(async (id: number) => {
    await api.proposals.reject(storyId, id)
    patchGraph((g) => ({ ...g, proposals: without(g.proposals, id) }))
  })
  const rejectBatch = guard(async (batch: string) => {
    const ids = graph?.proposals.filter((p) => p.batch === batch).map((p) => p.id) ?? []
    await Promise.all(ids.map((id) => api.proposals.reject(storyId, id)))
    patchGraph((g) => ({ ...g, proposals: g.proposals.filter((p) => p.batch !== batch) }))
  })

  // --- runs (useRuns.ts) ---------------------------------------------------
  const runs = useRuns(storyId, { reload, setError })

  return {
    storyId,
    graph,
    error,
    reload,
    busy,
    assist,
    ...runs,
    acceptProposal,
    rejectProposal,
    rejectBatch,
    updateStory,
    createPassage,
    updatePassage,
    deletePassage,
    duplicatePassage,
    prunePassage,
    promote,
    saveLayout,
    mergePassages,
    createChoice,
    updateChoice,
    deleteChoice,
    reorderChoices,
    createVariable,
    updateVariable,
    deleteVariable,
    createLore,
    updateLore,
    deleteLore,
  }
}

export type StoryStore = ReturnType<typeof useStory>
export type { Choice, Lore, Passage, Variable }
