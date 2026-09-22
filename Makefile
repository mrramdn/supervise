UV ?= uv
RANGE ?= HEAD

.PHONY: help sync lint fmt typecheck test cov check check-commits validate

help: ## List targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F ':.*## ' '{printf "  %-14s %s\n", $$1, $$2}'

sync: ## Install the locked dev environment
	$(UV) sync --locked

lint: ## Lint and check formatting
	$(UV) run --locked ruff check .
	$(UV) run --locked ruff format --check .

fmt: ## Apply formatting and safe lint fixes
	$(UV) run --locked ruff format .
	$(UV) run --locked ruff check --fix .

typecheck: ## Strict type check
	$(UV) run --locked pyright

test: ## Run the test suite
	$(UV) run --locked pytest

cov: ## Run tests with branch coverage (fails under 90%)
	$(UV) run --locked coverage run -m pytest
	$(UV) run --locked coverage report

check: lint typecheck test ## Everything CI runs locally

check-commits: ## Check commit messages in RANGE (default: HEAD)
	python3 scripts/check_commits.py --range $(RANGE)

validate: ## Validate the plugin and marketplace manifests (needs the claude CLI)
	claude plugin validate --strict .
