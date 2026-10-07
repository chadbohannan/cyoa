# Delete Subtree Left Orphans

On 2026-09-29, the day it shipped, the author reported that the map's subtree delete "doesn't seem to work at all". Deleting a passage left the branches below it intact but orphaned, with nothing linking to them. The prune route (`POST .../passages/{pid}/prune`, described on the [backend](../systems/backend.md) page) now deletes everything below a passage down to the leaves, except passages the start still reaches another way. The passage inspector gained an explicit Delete subtree button beside Delete, described on the [frontend](../systems/frontend.md) page.

**Root causes.** There were two.

- **The prune borrowed the collapse rule.** Prune deleted `engine.subtree(pid)`, the same set "Collapse subtree" hides and "Promote subtree" flips. That set is the root plus what is reachable *only* through it, where the rest of the map is seeded from the start **and from every passage nothing links to**. For collapsing and promoting, treating orphans as roots is the cautious choice: it never hides or flips something another entry point shows. For deletion it inverts the intent. Each orphan left behind by an earlier plain Delete counted as an entry point, so any branch it linked into was shielded from the prune. Under that rule, the more orphans a story had, the less a prune removed.
- **The explicit action was hard to find.** "Delete choices & subtree" lived only in the map's right-click menu. The inspector offered only plain Delete. Plain Delete removes one passage, cascades its outgoing choices, and leaves the passages below it unreachable. Its confirm warned only about inbound choices left dangling, not about what would be orphaned.

**Resolution.**

- **A separate deletion rule.** `engine.prune_set(root, start, …)` returns every passage reachable from the root, not the root itself, that the start cannot reach without passing through the root. Only the start protects anything; orphans do not. The walk stops at a protected passage, so what lies below one survives with it. The start passage is always kept, including when the root is the start. `subtree` is unchanged, since collapse and promote still want the cautious rule.
- **A preview route.** `GET .../passages/{pid}/prune` returns the same `{passage_ids, choice_ids}` without deleting anything. The confirm now counts from the server's rule. Before, it counted from the client's `collapseSubtrees`, and the two would have disagreed.
- **One shared confirm path.** `frontend/src/prune.ts` holds `confirmPrune` and `confirmDelete`, used by both the context menu and the inspector. The menu item is renamed "Delete subtree". Plain Delete's confirm now says how many passages below would be orphaned and points at Delete subtree.
- **A regression test.** `test_prune_runs_to_the_leaves_past_orphans` pins the fix. An orphan links into the middle of a four-deep branch, and a passage in the branch rejoins another branch from the start. The test checks three things: the preview deletes nothing; the prune takes the whole branch, orphan link and all; and the rejoined passage and the orphan survive.

**Not done.** Orphans that already exist are not cleaned up automatically. The Problems overlay marks them unreachable. To remove one, use Delete subtree on it, which clears everything below it, then plain Delete. An automatic "delete unreachable" sweep could come later if orphans keep piling up.

## Reference

| Date | Change |
|------|--------|
| 2026-09-29 | Prune shipped with the collapse rule (commit 52e8a77) |
| 2026-09-29 | Reported; `engine.prune_set`, the preview route, `prune.ts` and the inspector button added; one new API test (77 backend tests) |
