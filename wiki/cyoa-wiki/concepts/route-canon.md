# Route Canon

Route canon is the set of checks that ask whether each route through a book is *correct*, not just well-shaped. It was built 2026-10-09 for the [MCP authoring](mcp-authoring.md) experiment, after a review of the third and fourth pilots found errors that every existing tool had passed. `problems` and `playtest` check shape (dangling choices, reachability), and `nearest_endings` checks divergence, but none of them reads what a route has told the reader.

## Why it was needed

The Encyclopedia Brown–style pilot (story 3, "The Case of the Stolen Ruckus") made the gap obvious. A deduction book is fair only if every solved ending rests on clues *that route* showed. In the pilot, "The Keyring" cleared two suspects with evidence its route never visited, "Wet Sneakers" relied on a witness its route never met, and a passage invented a fact that contradicted the case file. In the ragged Salt Lamp pilot (story 2), two endings broke the book's central rule (Wren comes at dawn only if the lamp was lit) and one called the deliberately genderless reader "a man". A branching book breaks here because each route is its own history ([Stateless Story](stateless-story.md)), so a passage that reads fine alone can be wrong on the route that reaches it.

## Facts and the deterministic check

A **fact** is a lore entry of kind `fact`: one thing a route must show the reader before a passage may rely on it, such as a clue, an alibi or a secret. Passages carry two tags in `meta`: `reveals` (facts the reader learns here) and `needs` (facts the passage relies on the reader already knowing). Tag names must match a fact entry when written, and are stored as that entry spells them.

`fact_check` (`canon.py`) proves every `needs` is revealed by an earlier passage on **every** route from the start. It is a must-analysis over the graph, iterated to a fixpoint, so it never enumerates routes and copes with rejoins and cycles. For each unmet need it returns a witness route that arrives without the fact. It also reports tags that name no fact (after a rename, for example), facts that no reachable passage reveals, and ending verdicts. `get_passage` shows the same analysis as `facts_on_arrival`: `known` lists facts the passage may rely on, and `on_some_routes_only` lists facts it may not.

This is checked when writing, never evaluated at play. No reader state exists, the Twee export is unchanged, and the stateless model holds. The tags do the work a flag would do in a stateful engine, but only the author's tools ever read them.

An ending's **verdict** (`meta.verdict`, free text such as `solved` / `failed`) labels its outcome. `fact_check`, `endings_report` and `playtest` (as walk counts per verdict) all report it. In a deduction book a solved ending should `need` the facts its solution rests on; the check then enforces fair play.

## The model check

The tags cover only what the author tagged. `canon_check` covers the prose. For each passage and each of up to two routes into it, the local model (the summariser's llama-server) is given:

- the canon: the premise and every non-fact lore entry, rules included;
- the facts that route has revealed, word for word;
- the previous passage, word for word;
- the cached rolling summary of the rest of the route, labelled as notes that may be wrong.

It makes two calls, cached together in `canon_checks` and keyed by content, so rechecking an unchanged book costs nothing.

1. **Contradictions.** Each issue must quote the passage *and* the text it conflicts with. Code checks both quotes, says where the evidence came from (`evidence_from`: canon, previous passage or notes) and counts unbacked issues in `dropped_unbacked`.
2. **Fact use.** For each fact, the model must name a sentence of the passage that states it. Code checks that the sentence is really there. A fact the prose states but the route never revealed, and the passage does not tag, becomes an issue (`evidence_from: "fact flow"`). A tagged reveal the prose never states becomes a hint.

An unreadable reply is retried once, then listed in `unchecked`. A model that is down makes the call refuse rather than pass.

## What the first live run showed (2026-10-09)

The tools were tried on story 1, "The Case of the Lemonade Heist": 26 passages, about 8,100 words, 6 solved and 8 failed endings, every clue a fact. `fact_check` was clean from the start because the tags were planned alongside the facts, so on its own it proved fair play on every route.

`canon_check` ran on the 2-bit Ternary-Bonsai-2-27B then loaded on :8080, and it was weak:

- **First version** (summary only, quote checked): it found one real error, a passage claiming the reader had "only heard about" a stand they had been shown. It missed a planted one. Most flags were "the earlier text does not mention X", which is not a contradiction.
- **The summaries were the root cause.** They invented times (1:55, 2:45, 2:18 for events the text puts at 1:45 and 2:08), and the check then reported conflicts against those inventions. The same summaries reach the author through `get_passage`.
- **Second version** (verbatim facts and previous passage, two-sided quotes): the unbacked noise went away, but the model then quoted *agreeing* canon as evidence. Two-sided quoting cannot tell agreement from conflict.
- **The fact-use pass** first named almost every fact for every passage. Requiring a quoted sentence cut that to a few, but it then missed the planted error: "Under the Awning" says Ollie sat on the tuba case on a route that never visited the bandstand.

Five real errors were fixed, found by the check, by rereading, or both. The conclusion is that the deterministic check carries fair play, and the model check needs a stronger model than this one. With this model it is a list of places to reread, nothing more. One option not yet built: have the authoring agent read a per-route review packet (canon, route facts, previous passage, passage) instead of the local model.

## Limits

- Tags can disagree with prose: a passage tagged `reveals: keyring` may never mention it. `canon_check` and rereading are the only guards against that.
- A fact that is revealed and *then contradicted* on the same route is not modelled. Facts only accumulate.
- Facts are flat names. A per-subtree hinge value ([Hinge Canon](hinge-canon.md)) can be a fact too, revealed only in its branch, and the check then keeps the other branch from relying on it.
