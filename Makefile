default: help

include .env
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
down-cleanup: # docker compose down with cleanup (volumes)
	docker compose down -v

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
	docker compose up -d backend
	@echo "Backend recreated. Verifying..."
	@sleep 3
	@curl -sf -o /dev/null -w "" $(URL)/mcp/oauth/.well-known/oauth-authorization-server && echo "MCP OAuth endpoint is reachable!" || echo "Warning: Could not reach MCP endpoint. Check tunnel is running."

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

.PHONY: check-docs
check-docs: # check documentation for broken file references
	python3 .cinna-core-kit/scripts/check_docs_references.py

.PHONY: sync-ga-knowledge
sync-ga-knowledge: # sync docs + auto-generate API reference into GA env template
	python3 .cinna-core-kit/scripts/sync_ga_knowledge.py

.PHONY: mcp-inspector
mcp-inspector: # run mcp inspector for local development and testing
	cd tools && npx --prefix . @modelcontextprotocol/inspector
