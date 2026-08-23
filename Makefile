default: help

-include .env
export

.PHONY: help
help: # Show help for each of the Makefile recipes.
	@grep -E '^[a-zA-Z0-9 -]+:.*#'  Makefile | sort | while read -r l; do printf "\033[1;32m$$(echo $$l | cut -f 1 -d':')\033[00m:$$(echo $$l | cut -f 2- -d'#')\n"; done

.PHONY: install
install: # first-time setup wizard — creates .env, builds, migrates, seeds admin
	bash scripts/install.sh

.PHONY: up
up: # docker-compose up -d
	docker compose up -d
	@echo "\nApp started!"
	@echo "Access URLs:"
	@echo "  Frontend:         http://localhost:5173"
	@echo "  Backend:          http://localhost:8000"
	@echo "  Swagger UI:       http://localhost:8000/docs"
	@echo "  Adminer:          http://localhost:8080"
	@echo "  MailCatcher:      http://localhost:1080/"

.PHONY: up-prod
up-prod: # docker-compose up with production-like containers
	docker compose -f docker-compose.yml up -d
	@echo "\nApp started!"
	@echo "Access URLs:"
	@echo "  Frontend:         http://localhost:5173"
	@echo "  Backend:          http://localhost:8000"
	@echo "  Swagger UI:       http://localhost:8000/docs"
	@echo "  Adminer:          http://localhost:8080"
	@echo "  MailCatcher:      http://localhost:1080/"

.PHONY: h
h: # list of URLs
	@echo "Access URLs:"
	@echo "  Frontend:         http://localhost:5173"
	@echo "  Backend:          http://localhost:8000"
	@echo "  Swagger UI:       http://localhost:8000/docs"
	@echo "  Adminer:          http://localhost:8080"
	@echo "  MailCatcher:      http://localhost:1080/"

.PHONY: down
down: # docker compose down
	docker compose down

.PHONY: down-cleanup
down-cleanup: # docker compose down with cleanup (volumes and orphans)
	docker compose down --volumes --remove-orphans

.PHONY: stop-backend
stop-backend: # docker compose stop backend
	docker compose stop cinna-backend

.PHONY: shell
shell: # backend app shell
	docker compose exec -it backend /bin/sh

.PHONY: dev-front
dev-front: # run development mode for the frontend app
	docker compose stop frontend
	npm run --prefix frontend dev

.PHONY: gen-client-http
gen-client-http: # generate frontend app API client via HTTP request
	./scripts/generate-client-via-http.sh

.PHONY: gen-client
gen-client: # generate frontend app API client via backend
	./scripts/generate-client.sh

.PHONY: env
env: # activates virtual environment
	@echo "Execute the command:"
	 @echo "  source ./backend/.venv/bin/activate"

.PHONY: restart
restart: # restart app
	docker compose restart

.PHONY: logs
logs: # all app logs
	docker compose logs -f

.PHONY: logs-back
logs-back: # backend app logs
	docker compose logs -f backend

.PHONY: logs-front
logs-front: # frontend app logs
	docker compose logs -f frontend

.PHONY: ps
ps: # docker compose ps
	docker compose ps

.PHONY: build
build: # docker compose build
	docker compose build

.PHONY: build-prod
build-prod: # docker compose build for production
	docker compose -f docker-compose.yml build

.PHONY: build-prod-front
build-prod-front: # docker compose build for production, but only frontend
	docker compose -f docker-compose.yml build frontend

.PHONY: stop
stop: # stops app
	docker compose stop

.PHONY: start
start: # starts app
	docker compose start

.PHONY: dev-tunnel
dev-tunnel: # starts dev web tunnel to send queries to local DB
	ssh -p 443 -R0:localhost:8000 free.pinggy.io

.PHONY: mcp-tunnel
mcp-tunnel: # starts tunnel for MCP connector testing, updates .env, recreates backend
	@echo "Starting pinggy tunnel for MCP..."
	@echo "1) Copy the HTTPS URL from the tunnel output"
	@echo "2) In another terminal, run:"
	@echo "   make mcp-set-url URL=https://YOUR-TUNNEL.a.free.pinggy.link"
	@echo ""
	ssh -p 443 -R0:localhost:8000 free.pinggy.io

.PHONY: mcp-set-url
mcp-set-url: # sets MCP_SERVER_BASE_URL in .env and recreates backend (usage: make mcp-set-url URL=https://xxx.pinggy.link)
	@if [ -z "$(URL)" ]; then echo "Usage: make mcp-set-url URL=https://xxx.a.free.pinggy.link"; exit 1; fi
	@sed -i '' 's|^MCP_SERVER_BASE_URL=.*|MCP_SERVER_BASE_URL=$(URL)/mcp|' .env
	@echo "Updated .env: MCP_SERVER_BASE_URL=$(URL)/mcp"
	@# Inline for the same reason as webhook-set-url: make's stale export of the
	@# pre-sed value would otherwise win over the freshly written .env.
	MCP_SERVER_BASE_URL=$(URL)/mcp docker compose up -d backend
	@echo "Backend recreated. Verifying..."
	@sleep 3
	@curl -sf -o /dev/null -w "" $(URL)/mcp/oauth/.well-known/oauth-authorization-server && echo "MCP OAuth endpoint is reachable!" || echo "Warning: Could not reach MCP endpoint. Check tunnel is running."

.PHONY: webhook-tunnel
webhook-tunnel: # starts a public HTTPS tunnel to the local backend for inbound webhook testing (Google Chat, task triggers, agent hooks)
	@echo "Starting pinggy tunnel to the local backend (localhost:8000)..."
	@echo "1) Copy the HTTPS URL from the tunnel output"
	@echo "2) In another terminal, run:"
	@echo "   make webhook-set-url URL=https://YOUR-TUNNEL.a.free.pinggy.link"
	@echo "3) Reopen the channel in Admin > Server Configuration > Channels and copy the webhook URL"
	@echo "   (keep this tunnel running for the whole test session)"
	@echo ""
	ssh -p 443 -R0:localhost:8000 free.pinggy.io

.PHONY: webhook-set-url
webhook-set-url: # sets BACKEND_BASE_URL in .env and recreates backend (usage: make webhook-set-url URL=https://xxx.pinggy.link)
	@if [ -z "$(URL)" ]; then echo "Usage: make webhook-set-url URL=https://xxx.a.free.pinggy.link"; exit 1; fi
	@if grep -q '^BACKEND_BASE_URL=' .env; then \
		sed -i '' 's|^BACKEND_BASE_URL=.*|BACKEND_BASE_URL=$(URL)|' .env; \
	else \
		printf '\nBACKEND_BASE_URL=%s\n' "$(URL)" >> .env; \
	fi
	@# Blank the superseded former name so the two cannot disagree in a way
	@# that is invisible (BACKEND_BASE_URL wins, but a stale value here reads
	@# like the active setting).
	@sed -i '' 's|^WEBHOOK_BASE_URL=.*|WEBHOOK_BASE_URL=|' .env
	@echo "Updated .env: BACKEND_BASE_URL=$(URL)"
	@# Passed inline on purpose: this Makefile does `-include .env` + `export` at
	@# startup, so make already exported the OLD value, and compose prefers the
	@# process environment over the .env file it would otherwise read.
	BACKEND_BASE_URL=$(URL) docker compose up -d backend
	@echo "Backend recreated. Verifying tunnel reaches the backend..."
	@# Retry: the backend needs a few seconds to boot, and a single early probe
	@# reports a false failure that reads like a broken tunnel.
	@for i in 1 2 3 4 5 6 7 8 9 10; do \
		if curl -sf -o /dev/null --max-time 5 $(URL)/api/v1/utils/health-check/; then \
			echo "Backend is reachable through the tunnel!"; exit 0; \
		fi; \
		sleep 2; \
	done; \
	echo "Warning: could not reach the backend through the tunnel after 20s. Check the tunnel is still running."

.PHONY: webhook-clear-url
webhook-clear-url: # clears BACKEND_BASE_URL in .env (back to FRONTEND_HOST) and recreates backend
	@sed -i '' 's|^BACKEND_BASE_URL=.*|BACKEND_BASE_URL=|' .env
	@echo "Cleared .env: BACKEND_BASE_URL="
	BACKEND_BASE_URL= WEBHOOK_BASE_URL= docker compose up -d backend

.PHONY: prestart
prestart: # run initial app/db setup
	@echo "Setting up the application:"
	docker compose exec backend python /app/app/backend_pre_start.py
	docker compose exec backend alembic upgrade head
	docker compose exec backend python /app/app/initial_data.py

.PHONY: migrate
migrate: # run database migrations (alembic upgrade head) in the backend container
	docker compose exec backend alembic upgrade head

.PHONY: migration
migration: # create a new migration (will prompt for migration name)
	@read -p "Enter migration name: " migration_name; \
	docker compose exec backend alembic revision --autogenerate -m "$$migration_name"

.PHONY: test-backend
test-backend: # run backend pytest suite inside the backend container
	docker compose exec backend python -m pytest tests/ -v

.PHONY: backfill-router-trigger-prompts
backfill-router-trigger-prompts: # one-time: generate router trigger prompts + auto App MCP routes for existing installs
	docker compose exec backend python -m app.scripts.backfill_router_trigger_prompts

.PHONY: backfill-router-trigger-prompts-dry-run
backfill-router-trigger-prompts-dry-run: # dry-run: report what backfill-router-trigger-prompts would change
	docker compose exec backend python -m app.scripts.backfill_router_trigger_prompts --dry-run

.PHONY: check-docs
check-docs: # check documentation for broken file references
	python3 .cinna-core-kit/scripts/check_docs_references.py

.PHONY: sync-platform-knowledge
sync-platform-knowledge: # sync docs + auto-generate API reference into platform-knowledge env template
	python3 .cinna-core-kit/scripts/sync_platform_knowledge.py

.PHONY: mcp-inspector
mcp-inspector: # run mcp inspector for local development and testing
	cd tools && npx --prefix . @modelcontextprotocol/inspector
