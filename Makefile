.PHONY: help install backend frontend serve test test-backend test-frontend eval lint-wiki

help: ## list targets
	@grep -E '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-16s %s\n", $$1, $$2}'

install: ## install backend (uv) and frontend (npm) deps
	cd backend && uv sync
	cd frontend && npm install

# CYOA_LLM=ollama (OLLAMA_URL) or openai (CYOA_OPENAI_URL, default a llama-server on :8080/v1)
CYOA_LLM ?= openai
export CYOA_LLM

backend: ## run FastAPI with reload on :8765 (CYOA_PORT); model backend per CYOA_LLM
	cd backend && uv run cyoa

frontend: ## run Vite dev server on :5173 (proxies /api to :8765, CYOA_API)
	cd frontend && npm run dev

serve: ## build the frontend and run the backend serving it, single process on :8765
	cd frontend && npm run build
	cd backend && CYOA_RELOAD=0 uv run cyoa

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
