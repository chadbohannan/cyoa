.PHONY: help install backend frontend test test-backend test-frontend lint-wiki

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

test: test-backend test-frontend ## run all tests

test-backend: ## pytest
	cd backend && uv run pytest -q

test-frontend: ## typecheck + build
	cd frontend && npx tsc -b && npm run build

lint-wiki: ## structural lint of the wiki
	cd wiki/cyoa-wiki && python3 scripts/lint-wiki.py .
