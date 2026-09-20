# CYOA Engineering Wiki

A persistent, LLM-maintained knowledge base about the CYOA choose-your-own-adventure game in this repository — its engine, story model, design decisions, and the history of the bugs found and fixed along the way. Modeled on the [code-wiki-template](https://github.com/chadbohannan/code-wiki-template) and on the wikis in the `meeseeks` and `landlords` projects.

The LLM writes and maintains everything under this folder; you curate the raw inputs in `sources/`, direct investigation, and ask questions. The full schema and operating rules live in [CLAUDE.md](CLAUDE.md), which the agent reads as its standing contract.

## Layout

- `index.md` — the routing catalog; the first thing to read when answering a question.
- `log.md` — append-only record of every wiki operation.
- `systems/`, `components/`, `concepts/`, `incidents/`, `runbooks/`, `syntheses/` — the pages, by category.
- `sources/` — raw inputs (design notes, story drafts, session transcripts), never edited by the agent.
- `scripts/` — helper tooling; see [scripts/README.md](scripts/README.md).
- `.obsidian/` — vault config, so the folder opens directly in Obsidian with the graph view enabled.

## Daily use

Talk to the agent in terms of the operations in CLAUDE.md: **ingest** a new source, **query** the wiki, **sync** against recent code changes, **lint** for health, or **enrich** a page from code. The wiki documents the code at the repo root, so pages should cite specific files and functions — keep them honest with periodic syncs.
