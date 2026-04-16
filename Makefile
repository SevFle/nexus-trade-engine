.DEFAULT_GOAL := help

.PHONY: help dev test lint fix typecheck migrate docker-up docker-down

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

dev: ## Start dev server with hot reload
	uv run uvicorn engine.app:create_app --factory --reload --host 0.0.0.0 --port 8000

test: ## Run test suite
	uv run pytest

test-cov: ## Run tests with coverage report
	uv run pytest --cov=engine --cov-report=html

lint: ## Run linter checks
	uv run ruff check .
	uv run ruff format --check .

fix: ## Auto-fix lint issues
	uv run ruff check --fix .
	uv run ruff format .

typecheck: ## Run type checker
	uv run basedpyright

migrate: ## Run database migrations
	uv run alembic upgrade head

migrate-new: ## Create a new migration (usage: make migrate-new msg="description")
	uv run alembic revision --autogenerate -m "$(msg)"

docker-up: ## Start all services via docker compose
	docker compose up -d

docker-down: ## Stop all services
	docker compose down

docker-build: ## Build docker images
	docker compose build
