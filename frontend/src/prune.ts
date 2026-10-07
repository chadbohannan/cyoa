// Deleting on the map, shared by the context menu and the passage inspector. The
// server picks what a prune removes (GET .../prune previews it), so the confirm
// counts exactly what will go: everything below the passage down to the leaves,
// except passages the start still reaches another way.

import { api, type PruneResult } from './api'
import type { StoryStore } from './useStory'

// Preview, confirm, prune. Resolves to what was deleted, or null if nothing was.
export async function confirmPrune(store: StoryStore, id: number): Promise<PruneResult | null> {
  const graph = store.graph
  if (!graph) return null
  const plan = await api.passages.prunePreview(store.storyId, id).catch(() => null)
  if (!plan) return null
  const doomed = new Set(plan.passage_ids)
  const reviewed = graph.passages.filter((p) => doomed.has(p.id) && p.reviewed).length
  const note = reviewed ? ` (${reviewed} reviewed)` : ''
  const msg = `Delete ${plan.choice_ids.length} choice(s) and the ${plan.passage_ids.length} passage(s) below them${note}? `
    + 'Passages the start still reaches another way are kept.'
  if (!confirm(msg)) return null
  return (await store.prunePassage(id)) ?? null
}

// Delete one passage, warning about what it would leave behind: choices that
// dangle into it and passages below it nothing else reaches (Delete subtree removes those).
export async function confirmDelete(store: StoryStore, id: number): Promise<boolean> {
  const graph = store.graph
  if (!graph) return false
  const inbound = graph.choices.filter((c) => c.to_passage === id).length
  const below = (await api.passages.prunePreview(store.storyId, id).catch(() => null))?.passage_ids.length ?? 0
  const warnings = [
    inbound && `${inbound} choice(s) lead here and will dangle.`,
    below && `${below} passage(s) below it will be orphaned; use Delete subtree first to remove them.`,
  ].filter(Boolean)
  if (warnings.length && !confirm(`${warnings.join(' ')} Delete anyway?`)) return false
  await store.deletePassage(id)
  return true
}
