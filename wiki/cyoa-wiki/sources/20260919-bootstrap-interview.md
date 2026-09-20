# Bootstrap interview — 2026-09-19

Answers given by the project owner when the repo was scaffolded, recorded verbatim in substance.

- **Purpose**: tools to compose choose-your-own-adventure (CYOA) interactive books. The branching story structure should be able to evolve as the tool evolves. Not production software.
- **Stack**: uv-managed Python backend; React + Vite frontend.
- **Story storage**: SQLite database (chosen over markdown-per-passage and single JSON/YAML document).
- **Backend role in v0**: FastAPI plus LLM-assisted authoring from day one.
- **LLM**: local Ollama (chosen over the Anthropic API and over a pluggable provider layer).
- **Frontend v0**: a graph editor for the branching structure; React Flow (`@xyflow/react`) as the graph library.
- **Story model**: needs variables/flags and inventory/stats beyond plain branching. Print/ebook export was not selected.
- **Repo layout**: `backend/` + `frontend/` with a root Makefile.
- **Initial scope**: skeleton plus hello-world wiring.
