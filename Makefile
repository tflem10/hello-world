.DEFAULT_GOAL := help
.PHONY: help install test lint fmt scan confirm backtest report schedule clean

UV ?= uv

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

install: ## Create the venv and install everything (incl. dev tools)
	$(UV) sync

test: ## Run the test suite (offline; the network is blocked in tests)
	$(UV) run pytest -q

lint: ## Check style and formatting without changing anything
	$(UV) run ruff check src tests
	$(UV) run ruff format --check src tests

fmt: ## Reformat and auto-fix what can be auto-fixed
	$(UV) run ruff format src tests
	$(UV) run ruff check --fix src tests

scan: ## Nightly scan (no notifications sent)
	$(UV) run swing scan --dry-run

confirm: ## Morning re-check of last night's picks
	$(UV) run swing confirm --dry-run

backtest: ## Walk-forward backtest over the full universe
	$(UV) run swing backtest --universe full --walkforward

report: ## Print the latest backtest summary
	$(UV) run swing report

schedule: ## Install the launchd timers for scan and confirm
	$(UV) run swing schedule install

clean: ## Remove caches and build artefacts (keeps reports/ and ~/.swing)
	rm -rf .pytest_cache .ruff_cache build dist src/*.egg-info
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
