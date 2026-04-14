.PHONY: help dev up down test lint seed migrate

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-15s\033[0m %s\n", $$1, $$2}'

# ── Docker ──

up: ## Start all services
	docker compose up -d

down: ## Stop all services
	docker compose down

logs: ## Tail engine logs
	docker compose logs -f engine worker

rebuild: ## Rebuild and restart
	docker compose up -d --build

# ── Development ──

dev: ## Run engine in dev mode (requires local Python env)
	cd engine && uvicorn main:app --reload --port 8000

dev-frontend: ## Run frontend in dev mode
	cd frontend && npm run dev

# ── Database ──

migrate: ## Run database migrations
	cd engine && alembic upgrade head

migrate-new: ## Create a new migration
	cd engine && alembic revision --autogenerate -m "$(msg)"

seed: ## Seed sample market data
	cd scripts && python seed_data.py

# ── Testing ──

test: ## Run all tests
	cd engine && python -m pytest ../tests -v --tb=short

test-cov: ## Run tests with coverage
	cd engine && python -m pytest ../tests -v --cov=. --cov-report=html

test-cost: ## Run cost model tests only
	cd engine && python -m pytest ../tests/test_cost_model.py -v

# ── Code Quality ──

lint: ## Run linters
	cd engine && python -m ruff check .
	cd frontend && npm run lint

format: ## Format code
	cd engine && python -m ruff format .

# ── SDK ──

sdk-install: ## Install SDK in development mode
	cd sdk && pip install -e ".[dev]"

sdk-test: ## Test SDK
	cd sdk && python -m pytest tests/ -v
