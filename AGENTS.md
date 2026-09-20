# CYOA — Agent Guidelines

CYOA is a set of tools for composing choose-your-own-adventure interactive books: a branching story graph (passages, choices, flags/stats) stored in SQLite, edited in a React Flow graph editor, with a local Ollama model assisting authoring. It is a prototype, not production software. Scaffolded 2026-09-19.

## The Wiki is the Primary Knowledge Base

**Read the wiki before the codebase.** This project has a living knowledge base at `wiki/cyoa-wiki/` that captures architecture decisions, design rationale, and operational patterns. Start with `wiki/cyoa-wiki/index.md` when exploring or making changes.

**Write to the wiki when you learn something.** If you discover why something is designed a certain way, encounter a non-obvious constraint, or solve a tricky problem — add it to the wiki. This project depends on wiki maintenance to stay navigable as complexity grows.

The wiki's full schema and operating rules (ingest / query / lint / enrich / sync) live in `wiki/cyoa-wiki/CLAUDE.md`; treat that file as the standing contract when working inside the wiki.

### Wiki Structure

```
wiki/cyoa-wiki/
├── index.md           # Content catalog — start here
├── log.md             # Chronological record of operations
├── systems/           # Distinct systems and deployable surfaces
├── components/        # Individual packages and modules
├── concepts/          # Recurring patterns and design ideas
├── incidents/         # Bugs, regressions, and their postmortems
├── runbooks/          # Operational procedures
├── syntheses/         # Cross-cutting analyses
├── sources/           # Raw, immutable inputs — never edited by the agent
└── scripts/           # lint-wiki.py and friends
```

### Wiki Conventions

- Pages are prose documents with contextual internal links
- Every link appears inside a sentence that explains the relationship
- Source attribution is inline (e.g., citing design notes under `sources/`)
- Reference tables at the bottom corroborate the log
- Filenames: lowercase, hyphenated (e.g., `story-graph.md`)

When updating wiki pages, maintain cross-references. Add links to the index for new pages and append a line to `log.md`.

## Codebase Structure

```
backend/                 # uv project (Python 3.12), FastAPI
├── src/cyoa/
│   ├── main.py          # app, /api routes, `cyoa` console script (uvicorn on :8765)
│   ├── db.py            # SQLite schema (stories, passages, choices, variables)
│   ├── models.py        # Pydantic shapes mirroring the tables
│   └── llm.py           # thin Ollama client (OLLAMA_URL, OLLAMA_MODEL)
└── tests/test_api.py
frontend/                # Vite + React + TypeScript
└── src/
    ├── api.ts           # typed gateway to /api (proxied to the backend in dev)
    ├── StoryCanvas.tsx  # React Flow rendering of a story graph
    └── App.tsx          # story picker shell
Makefile                 # install / backend / frontend / test / lint-wiki
```

## Working with the Code

1. **Understand before implementing**: read the wiki's system pages and `concepts/story-model.md`
2. **Run**: `make backend` and `make frontend` (see `wiki/cyoa-wiki/runbooks/dev-loop.md`)
3. **Test**: `make test` — pytest for the backend, `tsc -b` + build for the frontend
4. **Schema changes**: there are no migrations; edit `db.py` and delete `cyoa.db`. Prefer extending the JSON `condition`/`effects` vocabulary over adding columns while the model is moving
5. **Keep `api.ts` and `models.py` in step** — they mirror each other field for field

## Wiki Maintenance Triggers

Update the wiki when you:
- Discover design rationale that isn't documented
- Encounter a constraint or pattern worth explaining
- Implement a new feature or component
- Fix a bug worth a postmortem (file it under `incidents/`)
