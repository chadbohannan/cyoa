# Backend

The backend is a single FastAPI application in `backend/`, managed by uv and run with `make backend` (which calls the `cyoa` console script in `backend/pyproject.toml`, i.e. `cyoa.main:run`, a reloading uvicorn on `127.0.0.1:${CYOA_PORT:-8765}`). It exists to own the story database and to front a local LLM for authoring assistance; the [frontend](frontend.md) is a client of it and never touches SQLite or Ollama directly.

Port 8765 rather than the conventional 8000 was chosen at bootstrap on 2026-09-19 because something else on the development machine already answered on 8000 (a Flask app returning 404s); the port is an environment variable precisely so that collision is not baked in.

## Storage

Stories live in one SQLite file, `CYOA_DB` (default `cyoa.db` in the working directory, gitignored). `cyoa/db.py` holds the schema as a single `executescript` string and creates it on app startup via a lifespan handler; there is no migration tooling, and the deliberate policy while the [story model](../concepts/story-model.md) is still moving is to delete the file to reset. `connect()` turns on `PRAGMA foreign_keys` so that deleting a story cascades to its passages, choices, and variables, and since 2026-09-19 sets `journal_mode = WAL` with a 5-second busy timeout so the background agent runs planned in [agentic authoring](../concepts/agentic-authoring.md) can write while the editor reads (the `-wal`/`-shm` sidecars are gitignored alongside `*.db`). The 2026-09-19 bootstrap interview (`20260919-bootstrap-interview.md`) chose SQLite over markdown-per-passage and a single JSON document, accepting that stories are not hand-editable or git-diffable in exchange for a queryable store the backend fully owns.

`cyoa/models.py` carries the Pydantic request/response shapes, mirroring the tables one-to-one; `StoryGraph` bundles a story with all of its passages, choices, and variables, which is the one read the graph editor needs.

## Routes

All routes sit under `/api`, so the Vite dev proxy can forward one prefix. As of 2026-09-19 they are: `GET /api/health`; `GET/POST /api/stories` and `DELETE /api/stories/{id}`; `GET /api/stories/{id}/graph`; `POST /api/stories/{id}/passages` and `.../choices`; and the LLM passthroughs `GET /api/llm/models` and `POST /api/llm/generate`. There are no update routes yet — the editor is read-only and creation-only until the model settles.

## LLM

`cyoa/llm.py` is a thin `httpx` client for Ollama's native `/api/tags` and `/api/generate` endpoints at `OLLAMA_URL` (default `http://localhost:11434`), with `OLLAMA_MODEL` (default `bonsai27b:latest`) as the fallback model. It deliberately holds no prompts: authoring prompts belong to the routes that use them, so the client stays a transport. The interview chose local Ollama over the Anthropic API; the routes translate `httpx.HTTPError` into a 503 so an absent Ollama degrades to a clear error rather than a crash. Verified end to end on 2026-09-19: the generate route returned a one-sentence cave description from `qwen3.5-9b`.

## Tests

`backend/tests/test_api.py` drives the app through FastAPI's `TestClient` against a temp-directory database (it reassigns `db.DB_PATH` before the lifespan runs) and covers health, story creation, passage and choice creation with JSON effects round-tripping, graph read, and cascade delete. Run with `make test-backend`; see [Running the Dev Loop](../runbooks/dev-loop.md).

| Reference | Role |
|-----------|------|
| `backend/src/cyoa/main.py` | app, routes, `run()` |
| `backend/src/cyoa/db.py` | schema, connection helpers |
| `backend/src/cyoa/models.py` | Pydantic shapes |
| `backend/src/cyoa/llm.py` | Ollama client |
| `backend/tests/test_api.py` | API round-trip test |
| `sources/20260919-bootstrap-interview.md` | storage and LLM decisions |
