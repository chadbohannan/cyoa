# Writing a CYOA Book

This is the playbook for writing a whole branching book, start to finished, through the [MCP authoring](../concepts/mcp-authoring.md) tools. It is distilled from four books written on the `cyoa-book-mcp` branch (2026-10-07 to 2026-10-11), the last of which, "The Case of the Kite-Day Cash" (40 passages, 22 endings, 10.1k words), went through every step below. It is written for an Encyclopedia Brown–style deduction book, but only the fair-play steps are specific to that; the rest applies to any book. Each tip names the failure that taught it.

## 1. Write the lore before the first passage

Everything later checks against the lore, so it is where the effort goes first. Four entries carry a book:

- **A case file (kind `rule`)** holding the truth and a minute-by-minute timeline of what really happened, the same on every route. Include why the culprit did it: the motive is never needed to solve the case, but without it the solved endings have nothing to land on. Record the point of view and tense here too ("second person, present; the reader is eleven, unnamed").
- **People and places** with the one trait each that the plot uses ("exact about times", "will not act on one child's word"). Say who is innocent, because endings that blame them depend on it.
- **One `fact` entry per clue** (cashbox-time, wind-onshore, vest-alibis). A fact is one thing a route must show before a passage may rely on it. Make them small: "the wind blew in off the sea all day" and "Gil's alibi is a kite out over the waves" are two facts, not one.
- **An endings table**: every intended ending with its depth, verdict and what it costs or gives. Planning this first is what stops later endings converging.

Write facts as the truth in the world, not as how a passage will say it, so a rewrite never invalidates them. [Route Canon](../concepts/route-canon.md) explains how the tags are then checked.

## 2. Plan the shape, then write it

The tools measure shape, so plan to those measures ([Shape check](../concepts/mcp-authoring.md)):

- **Never more than three options** per decision; `add_passage` refuses a fourth. Fold options together or move the decision later.
- **Make most wrong turns false trails** of two to four decisions with endings of their own, not one-passage exits. A book where every decision is "end now or go on" is a spine with exits; the Silver Paddle book was exactly that.
- **Let a false trail solve the case**, by a different proof from the main route. In Kite-Day Cash the dunes trail runs five decisions and still ends in "Caught at the Tails".
- **Vary the order of the options.** If the way on sits in slot 2 at every decision the reader learns the slot, not the story. After writing, `story_overview.shape.always_pick` should show that always choosing slot 1, 2 or 3 reaches a failed ending.
- **Make the options differ in kind** (act, ask, search, accuse, wait), not yes and no.
- **Cap the opening to three options and let one end the book at once.** The "accuse the obvious suspect" ending teaches the reader the rule cheaply.

Aim for several solved endings, each resting on a different proof from the same world facts. Seven solved endings by seven routes (waiting to catch Gil at the tail box, the wind against his alibi, the vests, the tail box with the vests, Mr. Pike's testimony, Gil refusing to sell the tails, the footprints) made the book fair without making it repetitive.

## 3. The opening needs exposition

The first draft of Kite-Day Cash began with the gust and the missing box, and the author's reaction was that it started abruptly. Give the first passage two or three paragraphs before the hook: who the reader is and what they are known for, what the place is and why people are there, who is visible and what each is doing. Introduce every suspect who matters, so none arrives only when accused. Show the setting details that will become clues (which way the kites lean) as scenery before they are clues. Do not place the culprit at the time of the crime. A reader who saw him at 1:00 can solve the book before opening a door.

## 4. Make endings cost different things

Failed endings that all end the same way, however differently they start, are the commonest defect. In the Silver Paddle book ten failures closed on one scene.

- Give every failure a different cost: someone sent home, a friend who stops speaking to you, a festival that loses its prize, the thief escaping with the money, a case that goes into an envelope marked lost and found.
- Write the cost in the endings table before writing the passage.
- Do not rely on `nearest_endings` to catch convergence when the embeddings come from the chat model. Gemma-4's pooled vectors scored a pair with the same meaning 0.893 and a pair with opposite meaning 0.845, so read the endings table and the endings themselves ([Semantic convergence check](../concepts/mcp-authoring.md)).
- Let some failures be good ones. "Hearsay" and "Your Own Kite" both prove something true and still fail.

## 5. Tag as you write

Tag each passage with the facts it `reveals` and `needs` while writing it, and give every ending a `verdict`. Run `fact_check` after every batch. It is deterministic and cheap, and it proves every solved ending rests on clues that route showed. Tagging afterwards is where the seven missing tags in Kite-Day Cash came from.

When a rewrite introduces new canon (a motive, a flyer on a counter, a detail about someone's father), put it in the lore first and then check route by route which passages the reader has seen before relying on it. In Kite-Day Cash the championship flyer is seen in three passages, and every ending that mentions the championship either follows one of them or has a character explain it.

## 6. Use `canon_check` sparingly and read it skeptically

`canon_check` sends each passage to the local 12B model. On Kite-Day Cash it flagged 16 passages and about nine of those held something real: an ending where Mrs. Okafor quoted a story change she never heard, an allowance refunded that was never paid, seven missing reveal tags. The rest was noise, and on the second pass all six flags were. The recurring noise is the model reading a *mention* of something as *stating a fact* (a pile of dollar bills read as revealing where the money is hidden, a yellow vest read as revealing that there are three). It cannot judge whether the reader has seen something on that route; `fact_check` and your own reading can.

- Pass `passage_ids`, about ten per call. A `limit` of 15 ran past Claude Code's 120-second MCP timeout and finished only in the background.
- Results are cached by content, so rechecking unchanged passages costs nothing.
- Treat a flag as a pointer to reread and a clean result as no proof.
- Run it on every passage you rewrote, especially when a rewrite adds canon.

## 7. Write the literary pass last

The structural tools cannot tell you whether the book is good. A spot check of Kite-Day Cash as young-adult fiction found a clean, fair puzzle with middle-grade stakes, and these habits to fix:

- **No personal stake.** Make the culprit someone the reader cares about, with a motive the reader can see. Gil had no reason to steal, and a catch cost the reader nothing. Giving him a championship he could not afford and a garage where he helped build the reader's kite changed every solved ending.
- **Failure endings that end on a one-line moral.** "You could have checked the times." Replace it with an image or a consequence and let the reader draw the lesson: a red kite in the trash, a page headed "no evidence", a trail of string behind a boy dragging his cooler.
- **The clue underlined every time.** Plant the main clue once before the solution. Mentioning the wind in three passages of one route gives the book away.
- **A solved ending that is one speech.** Break the detective's explanation into an argument with questions and interruptions, and let one detail (the mallet with blue tape) land the proof.
- **The same climax staged five times.** Not everyone has to unroll the tails. Vary how the money surfaces and who finds out why.
- **Time stamps as a closer.** "It is a quarter to two." works as a clock until every passage ends that way.
- **Lines in an adult voice.** "Which is how grown-ups say goodbye" belongs to a narrator older than the reader.
- **Repeated stock phrases.** Check mechanically. `scripts/book-routes.py` lists which passages share phrases ("both arms", "thirty years", "garage"). Keep a repeat only when it is an echo that the reader will notice.

Soften, but do not remove, harsh outcomes. Three of the Kite-Day failures end on an innocent child's humiliation; wording changed, the outcome stayed.

## 8. The process that worked

1. **Reset the book** for each test iteration and write from the lore. No story content is sacred; the experiment compares harnesses, so the lore and pacing plan stay identical across arms.
2. **Write in tree order, a level at a time,** with `add_passage` (which wires the choice in one step), checking `frontier()` and `problems()`.
3. **Run the checks in this order:** `fact_check`, `problems`, `playtest`, `endings_report`, `story_overview.shape`, then `canon_check` on the passages you changed.
4. **Edit with `update_passage`.** Only the fields you pass change, but a change to the body needs the whole body, and a `meta` argument replaces the whole `meta` (use the `reveals`, `needs` and `verdict` arguments to change tags).
5. **Save the whole book as Twee and read it route by route** at the end. `export_twee` returns about 50 KB and is saved to a file by the harness. `scripts/book-routes.py` lists every route with each ending's word count and the stock-phrase hits, and prints any passage with `--text`.
6. **Do the literary pass after the structural passes.** Doing it before means rewriting passages you later cut.
7. **Build one allowlisted script** for anything that would otherwise be a storm of one-off commands. The route lister is the example.

## What was left alone

Endings stayed short of the pacing plan's 400–600 words (solved endings 334–432, failures 188–273). Padding failures weakens them, and the plan is held constant so every harness and model gets the same one. Whether a real reader finds the emotional beats land is untested; the local model cannot judge that.

## References

| Item | Where |
|---|---|
| Finished example | story 1, "The Case of the Kite-Day Cash", in the dev database `CYOA_DB` points at |
| Route lister | `scripts/book-routes.py` |
| Shape metrics | `engine.shape`, returned by `story_overview` |
| Fact and canon checks | `canon.py`, tools `fact_check`, `canon_check` |
