# CYOA Engineering Wiki — Schema & Operating Rules

A persistent, LLM-maintained knowledge base for **CYOA**, a choose-your-own-adventure game project. The wiki captures system behavior, the story/branching model, design decisions, debugging knowledge, and the history of why things are the way they are. The LLM writes and maintains everything under this folder; the human curates sources, directs investigation, and asks questions.

This wiki documents the code that lives in the **same repository** — two directories up from this folder (the repo root). "The codebase" below means that repo. At the time the wiki was created (2026-09-19) the repo held no code yet, so early pages will describe intent and design rather than implementation; as code lands, sync the wiki so every claim is anchored to a real file.

## Directory Layout

```
wiki/cyoa-wiki/
├── CLAUDE.md          # This file — schema and operating rules
├── README.md          # Orientation for humans
├── index.md           # Content catalog — the structured routing layer
├── log.md             # Chronological record of all operations
├── systems/           # Distinct systems and deployable surfaces (the engine, the client, a server)
├── components/        # Individual packages and modules
├── concepts/          # Recurring patterns and design ideas (story graph, choice resolution, state)
├── incidents/         # Specific bugs, regressions, and their postmortems
├── runbooks/          # Operational procedures (dev loop, tests, authoring a story)
├── syntheses/         # Cross-cutting analyses and thematic summaries
├── sources/           # Raw, immutable inputs (design notes, transcripts, story drafts) — never edited
└── scripts/           # Helper scripts (see scripts/README.md)
```

`sources/` holds inputs verbatim. The LLM reads them but never edits them. Everything else under this folder is LLM-authored and LLM-maintained. The folder is also an Obsidian vault (`.obsidian/`), so it can be browsed as a graph.

## Page Conventions

Wiki pages are prose documents. They have no YAML frontmatter. The directory a page lives in determines its category. The first heading serves as the title.

Internal links use relative paths and are embedded in context — every link should appear inside a sentence that explains why the relationship matters. A link to another page is a claim about a connection, and the surrounding prose is the evidence.

```markdown
# Page Title

Opening paragraph establishing what this page covers and why it matters.

Body paragraphs with contextual links woven in. Each internal link is a
relative path to another page — for example, a page in `concepts/` linking to
`../components/story-engine.md` when explaining that the engine enforces a
rule — and the sentence around it explains why the relationship matters.
```

Source attribution is inline. When a factual claim traces to a specific source, cite it naturally: "The September 2026 design notes (`20260919-initial-design.md`) settled on a directed graph of scenes rather than a tree..." The reader should understand not just *what* the source is, but *what claim it supports and why it was consulted.*

### Naming Conventions

- **Systems**: `{system-name}.md`
- **Incidents**: `{YYYY-MM-DD}-{short-description}.md`
- **Components**: `{component-name}.md`
- **Concepts**: `{concept-name}.md`
- **Runbooks**: `{procedure-name}.md`
- **Syntheses**: `{analysis-topic}.md`
- **Sources**: `{YYYYMMDD}-{short-description}.md` (or the source's own name, verbatim)

Filenames are lowercase, hyphenated, and descriptive.

### Writing Style

Every sentence should earn its place. A short, precise page is better than a long, vague one. Write in plain, humanist prose — not corporate-memo bullet lists.

For incidents, open with a plain-English summary that captures what broke, how it was found, and how it was fixed in a sentence or two.

Cross-references are a first-class feature. When creating or updating a page, consider what other pages should link here, and what this page should link to. But every link must be contextual — no "see also" lists at the bottom. If a relationship can't be explained in a sentence, it probably isn't meaningful enough to link. A reference table at the bottom of a page listing the code paths and sources it cites is fine; it corroborates the log rather than substituting for prose links.

Every concrete claim — a type, a file path, a function name, a rule value — is implicitly anchored either to current code (in which case sync must keep it true) or to a named source document (in which case the prose must say so). Before extending or relying on a code-anchored citation in an existing page, verify it still resolves.

When new information contradicts existing wiki content, don't silently overwrite. Note the contradiction explicitly, cite both sources, and flag it for the user. If a claim comes from a single source or is speculative, say so.

### Density and Single Ownership

Maximize information per token — *information*, not text. Cut any token the reader can already reconstruct: a page in `incidents/` needs no "Category: incident" line. Keep anything the reader can't reconstruct from what's in front of them.

Git records *edit*-time, invisible to anyone reading rendered markdown. So event dates live in the prose, and when a sync confirms a page's code-anchored claims, record verify-time: "verified against `src/engine/` as of 2026-09." The history is the part the code can't tell you.

Duplicate *immutable* facts freely (a past bug's date will never change). Give every *mutable* fact — a rule value, a default, a limit — one owning page and link to it; each copy is one more place a sync must correct.

## The Index

`index.md` is the structured routing layer for the wiki. It is the first thing an LLM reads when answering a query, and the primary mechanism for navigating the wiki at scale.

Each entry is a link with a one-line description, grouped by category. The index is updated on every ingest. The index links only to wiki pages; do not clutter it with links to raw `sources/` material.

## The Log

`log.md` is a chronological, append-only record of wiki operations. Each entry is a single line with a consistent prefix for parseability:

```
[YYYY-MM-DD] operation | Subject — brief description of what happened
```

For ingest operations, the subject should be a relative link to the source document under `sources/`, with the link text being the source's name. Other mutation operations use plain-text subjects.

Operations: `ingest`, `query`, `lint`, `enrich`, `sync`, `update`.

## Discovery Layers

This wiki is fundamentally *about* a codebase, so wiki pages should describe code behavior, types, surfaces, and configuration directly. The wiki is the narrative and historical layer; the code is ground truth.

- **The wiki** explains how the pieces fit together, what their fields mean, how they fail, and what changed and why. Be dense with concrete detail: dates, file paths, function names, types, rule values, test names.
- **The code** is ground truth, at the repo root. Reach into it with `grep`/`glob`/`read`, and with the project's test command to confirm behavior. A `code-rag` instance, if running and indexing this repo, accelerates exploratory queries; it is optional and the wiki works fine with plain file search.
- **`index.md`** is the routing layer. Read the index to find relevant wiki pages, then drill into source code when a page points toward a specific component or behavior.

**Bias toward ingesting.** If a change, source, or commit relates to the project, default to capturing it somewhere — even a one-line update to a component page is better than skipping it because it "isn't incident-grade." The cost of a small, accurate update is low; the cost of a missing fact later is high.

## Operations

### Ingest

When the user provides a new source (a design note, story draft, debug log, session transcript, commit history, etc.):

1. **Read** the source fully.
2. **Discuss** key takeaways with the user — what's important, what's surprising, what connects to existing knowledge. Keep this brief (3–5 sentences).
3. **Create or update** wiki pages:
   - Create or update entity pages for every system, component, or concept mentioned.
   - Create or update incident pages if the source describes a bug or regression.
   - Extract any operational procedures into runbook pages.
   - Weave cross-references into the prose of every page touched — both the new/updated pages and existing pages that should now link to them.
4. **Update index.md** — add or revise entries for every wiki page touched.
5. **Append to log.md** — one entry line recording the ingest.
6. **Report** to the user: what pages were created/updated, what connections were found, what gaps remain.

### Query

When the user asks a question:

1. **Read index.md** to identify relevant pages.
2. **Read** the relevant wiki pages.
3. **Use code search** (`grep`/`read` over the repo, or code-rag) if the question needs grounding in source.
4. **Synthesize** an answer with citations to wiki pages and original sources.
5. If the answer is substantial and reusable, **offer to file it** as a new page.
6. **Log** the query.

### Lint

When the user asks for a health check (or periodically as the wiki grows):

1. Check for **contradictions** between pages.
2. Find **orphan pages** — pages not linked from any other page (`index.md` links don't count).
3. Identify **mentioned-but-missing** entities that deserve their own page.
4. Flag **stale pages** — prefer a page's recorded verify-time stamp; fall back to `git log` for pages without one.
5. Verify **link integrity** — all internal links resolve to existing files.
6. Suggest **new questions** or sources that would fill gaps.
7. Report findings and fix what can be fixed automatically.
8. **Log** the lint pass.

The structural checks are automated by `scripts/lint-wiki.py` — run it as `scripts/lint-wiki.py .` from this folder, then handle the prose-level checks by reading.

### Enrich

When deeper context could improve clarity:

1. Search the repo (or code-rag) for relevant source, patterns, and implementation details.
2. Integrate findings into wiki pages as prose with citations to specific code paths.
3. **Log** the enrichment.

### Sync

When the user asks to sync the wiki against recent code changes:

1. Inspect what moved in the repo — `git log --oneline <range>` and the diffs — for anything that adds or alters a real surface: a type, a rule value, a function, an endpoint, a test contract, a documented behavior.
2. **Hunt for broken anchors.** For each diff, grep the wiki for the names, paths, or functions it touches. An existing claim about a changed surface is the primary signal that the wiki may now be wrong.
3. **Discuss** with the user which changes are wiki-relevant before writing anything. Pure refactors and cosmetic tweaks can be skipped.
4. **Update** affected pages, weaving cross-references and citing specific commits or files. When a page's code-anchored claims are confirmed still true, record verify-time in the prose.
5. **Append to log.md** — one entry per sync, noting what was inspected and which pages were touched.

## Incident Page Structure

Incident pages don't use a rigid template, but they should cover these aspects in whatever order makes the narrative clearest:

- **Opening summary**: what broke and the outcome, in one or two sentences.
- **Timeline / discovery**: how it was noticed — a failing test, a code review, observed behavior.
- **Root cause**: what actually broke and why.
- **Resolution**: what fixed it, with the commit or file.
- **Contributing factors**: systemic issues that made it possible, with links to relevant concept and component pages.
- **Patterns**: connections to other bugs and recurring failure modes, woven into the narrative rather than listed at the end.
