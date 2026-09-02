# Local convenience targets for {{name}}.
#
# Every target here mirrors an entry in docs/CLI_COMMANDS.yaml. The YAML file is
# cloud-first (`python scripts/x.py`, paths relative to the workspace root); this
# Makefile is the local mirror and adds `uv run` so the project venv is used.
# When you add a command in one place, add it in the other in the same change.

.DEFAULT_GOAL := help
.PHONY: help status validate

help: ## Show available commands
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  make %-14s %s\n", $$1, $$2}'

status: ## Refresh app-data/storage/STATUS.md
	uv run scripts/update_status.py --status ok --summary "Ready"

validate: ## Check this agent against the kit conventions
	uv run ../../.cinna-kit/tools/kit.py validate .
