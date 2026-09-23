UV ?= uv
RANGE ?= HEAD

.PHONY: help sync lint fmt test check check-commits validate

help: ## List targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F ':.*## ' '{printf "  %-14s %s\n", $$1, $$2}'

sync: ## Install the locked dev environment (ruff, pytest)
	$(UV) sync --locked

lint: ## Lint and check formatting
	$(UV) run --locked ruff check .
	$(UV) run --locked ruff format --check .

fmt: ## Apply formatting and safe lint fixes
	$(UV) run --locked ruff format .
	$(UV) run --locked ruff check --fix .

test: ## Run the test suite
	$(UV) run --locked pytest

check: lint test ## Everything CI runs locally

check-commits: ## Check commit messages in RANGE (default: HEAD)
	python3 scripts/check_commits.py --range $(RANGE)

validate: ## Validate the plugin (skills, agents, hooks) and the marketplace; needs the claude CLI
	claude plugin validate --strict .claude-plugin/plugin.json
	claude plugin validate --strict .
