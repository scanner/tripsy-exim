ROOT_DIR := $(shell git rev-parse --show-toplevel 2>/dev/null || pwd)

.PHONY: setup sync lint ruff-format ruff-check mypy test coverage clean help

setup: ## Initial project setup: install dependencies and pre-commit hooks
	uv sync
	uv run pre-commit install
	@echo "Setup complete."

sync: ## Sync dependencies
	uv sync

lint: sync ## Run all pre-commit hooks (ruff, ruff-format, mypy, etc)
	uv run pre-commit run --all-files

ruff-check: sync ## Run just the ruff linter
	uv run ruff check .

ruff-format: sync ## Run just the ruff formatter
	uv run ruff format .

mypy: sync ## Run just the mypy type checker
	uv run mypy .

test: sync ## Run the test suite, skipping tests that need the live API
	uv run pytest -v -m "not live"

test-live: sync ## Run the tests that talk to the real Tripsy API
	uv run pytest -v -m live

coverage: sync ## Run the test suite with a coverage report
	uv run pytest -m "not live" --cov --cov-report=term-missing

clean: ## Remove generated files and caches
	rm -rf .mypy_cache .pytest_cache .ruff_cache .coverage htmlcov build dist
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	@echo "Clean complete."

help: ## Show this help
	@grep -hE '^[A-Za-z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'
