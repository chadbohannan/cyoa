# Running the Dev Loop

Two processes, driven from the root `Makefile` (`make help` lists every target). Run `make install` once to `uv sync` the [backend](../systems/backend.md) and `npm install` the [frontend](../systems/frontend.md).

```bash
make backend    # FastAPI with reload on 127.0.0.1:8765
make frontend   # Vite on 5173, proxying /api to the backend
```

Open http://localhost:5173. The backend creates `backend/cyoa.db` on first start; delete it to reset — there are no migrations while the [story model](../concepts/story-model.md) is unstable. If 8765 collides, set `CYOA_PORT` for the backend and `CYOA_API` (a full URL) for the Vite proxy.

LLM routes need a local Ollama at `OLLAMA_URL` (default `http://localhost:11434`); `GET /api/llm/models` is the quickest check, and returns 503 with the URL when Ollama is down. `OLLAMA_MODEL` picks the default model for generation.

`make test` runs `pytest` in the backend and `tsc -b` + `vite build` in the frontend; `make lint-wiki` runs this wiki's structural lint.
