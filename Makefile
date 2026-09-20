.PHONY: help install backend frontend test test-backend test-frontend lint-wiki

help: ## list targets
	@grep -E '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-16s %s\n", $$1, $$2}'

install: ## install backend (uv) and frontend (npm) deps
	cd backend && uv sync
	cd frontend && npm install

backend: ## run FastAPI with reload on :8765 (CYOA_PORT)
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
