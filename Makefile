.PHONY: help install backend frontend mcp serve test test-backend test-frontend eval lint-wiki

help: ## list targets
	@grep -E '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-16s %s\n", $$1, $$2}'

install: ## install backend (uv) and frontend (npm) deps
	cd backend && uv sync
	cd frontend && npm install

# CYOA_LLM=ollama (OLLAMA_URL) or openai (CYOA_OPENAI_URL). The openai backend here is the router on
# :4000/v1, which picks a node by model name ("default", llm.py's own default, is refused: "no nodes serve
# model 'default'"). Override for a bare llama-server: CYOA_OPENAI_URL=http://localhost:8080/v1 CYOA_OPENAI_MODEL=default
CYOA_LLM ?= openai
CYOA_OPENAI_URL ?= http://localhost:4000/v1
CYOA_OPENAI_MODEL ?= salad-chat
export CYOA_LLM CYOA_OPENAI_URL CYOA_OPENAI_MODEL

backend: ## run FastAPI with reload on :8765 (CYOA_PORT); model backend per CYOA_LLM
	cd backend && uv run cyoa

frontend: ## run Vite dev server on :5173 (proxies /api to :8765, CYOA_API)
	cd frontend && npm run dev

# The stdio MCP server (concepts/mcp-authoring.md) is launched by a harness, not run by hand. Its config entry is
#   command: uv   args: run --project <abs path>/backend cyoa-mcp
#   env: CYOA_DB=<abs path>/backend/cyoa.db  CYOA_MCP_LABEL=<harness>/<model>  [CYOA_SUMMARY_URL=http://0.0.0.0:8080/v1]
mcp: ## run the MCP server on stdio against CYOA_DB (default backend/cyoa.db) for a quick manual check
	cd backend && CYOA_DB=$${CYOA_DB:-$(CURDIR)/backend/cyoa.db} uv run cyoa-mcp

serve: ## build the frontend and run the backend serving it, single process on 0.0.0.0:8765 (CYOA_HOST)
	cd frontend && npm run build
	cd backend && CYOA_RELOAD=0 CYOA_HOST=$${CYOA_HOST:-0.0.0.0} uv run cyoa

test: test-backend test-frontend ## run all tests

test-backend: ## pytest
	cd backend && uv run pytest -q

test-frontend: ## typecheck + build
	cd frontend && npx tsc -b && npm run build

ITER ?= 3
PLAN ?=
REPEAT ?= 1
POLICY ?= {}
CONCURRENCY ?=
eval: ## storyline eval on a private backend: ITER premises grown under the default pacing plan (PLAN=plan.json for another, JUDGE=1 to model-grade, POLICY='{"refine":{"rounds":2}}' to refine, REPEAT=n to compare arms, CONCURRENCY=10 to fan out against a pooled backend)
	uv run --project backend python scripts/eval_storylines.py run --iterations $(ITER) $(if $(PLAN),--plan $(PLAN)) --repeat $(REPEAT) --policy '$(POLICY)' $(if $(CONCURRENCY),--concurrency $(CONCURRENCY)) $(if $(JUDGE),--judge)

lint-wiki: ## structural lint of the wiki
	cd wiki/cyoa-wiki && python3 scripts/lint-wiki.py .
