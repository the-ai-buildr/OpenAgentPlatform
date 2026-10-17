SHELL := /bin/bash
.DEFAULT_GOAL := help

PROJECT_NAME  := oap
COMPOSE       := docker compose
DEV_COMPOSE   := docker compose -f docker-compose.yml -f docker-compose.dev.yml
PROD_COMPOSE  := docker compose -f docker-compose.yml -f docker-compose.prod.yml
PY            := python3
VENV          := .venv
VENV_PY       := $(VENV)/bin/python

# Loaded for host-side targets (db-shell, status). Compose reads .env itself.
ifneq (,$(wildcard .env))
include .env
export
endif

OAP_URL       := http://localhost:$(or $(OAP_PORT),8000)
WEBUI_URL     := http://localhost:$(or $(OPEN_WEBUI_PORT),3000)
PHOENIX_URL   := http://localhost:$(or $(OTEL_UI_PORT),6006)
CONTROL_PLANE := https://os.agno.com/			
AUTH_HEADER   := Authorization: Bearer $(OS_SECURITY_KEY)

.PHONY: help setup env venv up dev down restart build rebuild ps status health models \
        watcher-status logs logs-api logs-db logs-otel logs-webui test test-watch lint \
        format typecheck db-shell db-dump db-restore kb-list files-tree urls \
        prod-config prod-up prod-down prod-logs config destroy-volumes \
        oap phoenix-mcp

# --------------------------------------------------------------------------- #
help: ## Show this help
	@echo ""
	@echo "  $(PROJECT_NAME) - Open Agent Platform"
	@echo ""
	@echo "  Setup"
	@grep -hE '^(setup|env|venv|build|rebuild|phoenix-mcp):.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "};{printf "    \033[36m%-18s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "  Run (local development)"
	@grep -hE '^(up|dev|oap|down|restart|ps|urls):.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "};{printf "    \033[36m%-18s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "  Inspect"
	@grep -hE '^(status|health|models|watcher-status|logs|logs-api|logs-db|logs-otel|logs-webui|files-tree|kb-list|config):.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "};{printf "    \033[36m%-18s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "  Quality"
	@grep -hE '^(test|test-watch|lint|format|typecheck):.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "};{printf "    \033[36m%-18s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "  Database"
	@grep -hE '^(db-shell|db-dump|db-restore):.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "};{printf "    \033[36m%-18s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "  Production"
	@grep -hE '^(prod-config|prod-up|prod-down|prod-logs):.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "};{printf "    \033[36m%-18s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "  DESTRUCTIVE (asks for confirmation)"
	@grep -hE '^(destroy-volumes):.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "};{printf "    \033[31m%-18s\033[0m %s\n", $$1, $$2}'
	@echo ""

# --------------------------------------------------------------------------- #
# Setup
# --------------------------------------------------------------------------- #
env: ## Create .env from .env.example with generated local secrets
	@$(PY) scripts/init_env.py
	@mkdir -p workspace_files/general workspace_files/sops workspace_files/contracts

setup: env ## Create .env, workspace folders and build all images
	$(DEV_COMPOSE) build

venv: ## Create a local virtualenv for tests and linting
	$(PY) -m venv $(VENV)
	$(VENV_PY) -m pip install --upgrade pip
	$(VENV_PY) -m pip install -r requirements-dev.txt

build: ## Build images (uses the layer cache)
	$(DEV_COMPOSE) build

rebuild: ## Build images from scratch, no cache
	$(DEV_COMPOSE) build --no-cache

config: ## Render and validate the development compose configuration
	$(DEV_COMPOSE) config

# --------------------------------------------------------------------------- #
# Run
# --------------------------------------------------------------------------- #
up: env ## Start the full stack in the background (development)
	$(DEV_COMPOSE) up -d --build
	@$(MAKE) --no-print-directory urls

dev: env ## Start the stack in the foreground with logs attached
	$(DEV_COMPOSE) up --build

down: ## Stop and remove containers (volumes and data are kept)
	$(DEV_COMPOSE) down --remove-orphans

restart: ## Restart every service
	$(DEV_COMPOSE) restart

ps: ## Show container status
	$(DEV_COMPOSE) ps

oap: env ## Start the API with its default agents, Postgres and Phoenix (no Open WebUI)
	$(DEV_COMPOSE) up -d --build oap-db oap-otel oap-api
	@echo ""
	@echo "  OAP only. Default agents: platform-builder, platform-engineer,"
	@echo "  platform-manager, platform-telemetry, and the oap coordinator."
	@echo "  Control plane: https://os.agno.com  -> Local -> $(OAP_URL)"
	@echo "  Security key is OS_SECURITY_KEY in .env."
	@$(MAKE) --no-print-directory urls

phoenix-mcp: ## Register Phoenix MCP at $(PHOENIX_URL)/mcp (AGENT=cursor|claude|codex|gemini|opencode|vscode)
	@agent="$(or $(AGENT),cursor)"; \
	case "$$agent" in \
		claude|codex|gemini|cursor|opencode|vscode) ;; \
		*) echo "AGENT must be one of: claude codex gemini cursor opencode vscode"; exit 1 ;; \
	esac; \
	npx -y @arizeai/phoenix-cli setup mcp --agent "$$agent" --endpoint "$(PHOENIX_URL)" --no-input

urls: ## Print the local service URLs
	@echo ""
	@echo "  Open WebUI   $(WEBUI_URL)"
	@echo "  OAP API      $(OAP_URL)/docs    (health: $(OAP_URL)/health)"
	@echo "  Phoenix UI   $(PHOENIX_URL)"
	@echo "  MCP endpoint $(OAP_URL)/mcp"
	@echo ""

# --------------------------------------------------------------------------- #
# Inspect
# --------------------------------------------------------------------------- #
health: ## Fetch /health from the API
	@curl -fsS $(OAP_URL)/health | $(PY) -m json.tool || echo "OAP is not reachable at $(OAP_URL)"

models: ## List the models the bridge advertises to Open WebUI
	@curl -fsS -H "$(AUTH_HEADER)" $(OAP_URL)/v1/models | $(PY) -m json.tool || echo "OAP is not reachable at $(OAP_URL)"

watcher-status: ## Show the workspace file-watcher status
	@curl -fsS -H "$(AUTH_HEADER)" $(OAP_URL)/v1/files/watcher-status | $(PY) -m json.tool || echo "OAP is not reachable at $(OAP_URL)"

status: ps health models ## Containers, health and advertised models

logs: ## Tail logs from every service
	$(DEV_COMPOSE) logs -f --tail=100

logs-api: ## Tail OAP API logs
	$(DEV_COMPOSE) logs -f --tail=100 oap-api

logs-db: ## Tail Postgres logs
	$(DEV_COMPOSE) logs -f --tail=100 oap-db

logs-otel: ## Tail Phoenix (OTel collector/UI) logs
	$(DEV_COMPOSE) logs -f --tail=100 oap-otel

logs-webui: ## Tail Open WebUI logs
	$(DEV_COMPOSE) logs -f --tail=100 open-webui

files-tree: ## List the workspace file tree
	@find workspace_files -maxdepth 4 -not -path '*/.*' | sort

kb-list: ## List the PgVector knowledge tables
	@$(DEV_COMPOSE) exec -T oap-db psql -U $(or $(DB_USER),ai) -d $(or $(DB_NAME),oap) \
		-c "SELECT schemaname, tablename FROM pg_tables WHERE tablename LIKE 'kb\\_%' ORDER BY 1,2;"

# --------------------------------------------------------------------------- #
# Quality
# --------------------------------------------------------------------------- #
test: ## Run the test suite
	@if [ -x "$(VENV_PY)" ]; then $(VENV_PY) -m pytest; else $(PY) -m pytest; fi

test-watch: ## Re-run tests on change (needs pytest-watcher or entr)
	@command -v entr >/dev/null || { echo "install entr, or run 'make test' manually"; exit 1; }
	@find app tests -name '*.py' | entr -c $(MAKE) test

lint: ## Lint with ruff
	@if [ -x "$(VENV)/bin/ruff" ]; then $(VENV)/bin/ruff check app tests scripts && $(VENV)/bin/ruff format --check app tests scripts; \
	else ruff check app tests scripts && ruff format --check app tests scripts; fi

format: ## Auto-format with ruff
	@if [ -x "$(VENV)/bin/ruff" ]; then $(VENV)/bin/ruff check --fix app tests scripts && $(VENV)/bin/ruff format app tests scripts; \
	else ruff check --fix app tests scripts && ruff format app tests scripts; fi

typecheck: ## Byte-compile every module as a cheap import/syntax check
	@$(PY) -m compileall -q app tests && echo "compileall: ok"

# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #
db-shell: ## Open a psql shell on the database
	$(DEV_COMPOSE) exec oap-db psql -U $(or $(DB_USER),ai) -d $(or $(DB_NAME),oap)

db-dump: ## Dump the database to db/backups/<timestamp>.sql
	@mkdir -p db/backups
	@out="db/backups/$$(date -u +%Y%m%dT%H%M%SZ).sql"; \
	tmp="$$out.partial"; \
	if $(DEV_COMPOSE) exec -T oap-db pg_dump -U $(or $(DB_USER),ai) -d $(or $(DB_NAME),oap) > "$$tmp"; then \
		mv "$$tmp" "$$out"; echo "Wrote $$out ($$(wc -c < "$$out") bytes)"; \
	else \
		rm -f "$$tmp"; echo "Dump failed - no file written. Is the database running? (make ps)" >&2; exit 1; \
	fi

db-restore: ## Restore a dump: make db-restore FILE=db/backups/<file>.sql
	@test -n "$(FILE)" || { echo "Usage: make db-restore FILE=db/backups/<file>.sql"; exit 1; }
	@test -f "$(FILE)" || { echo "No such file: $(FILE)"; exit 1; }
	$(DEV_COMPOSE) exec -T oap-db psql -U $(or $(DB_USER),ai) -d $(or $(DB_NAME),oap) < "$(FILE)"

# --------------------------------------------------------------------------- #
# Production
# --------------------------------------------------------------------------- #
prod-config: ## Render and validate the production compose configuration
	$(PROD_COMPOSE) config

prod-up: ## Start the stack with production settings (no reload, no bind mount)
	$(PROD_COMPOSE) up -d --build
	@echo "Started. Put a TLS-terminating reverse proxy in front of 127.0.0.1:$(or $(OPEN_WEBUI_PORT),3000)."

prod-down: ## Stop the production stack (volumes and data are kept)
	$(PROD_COMPOSE) down --remove-orphans

prod-logs: ## Tail production logs
	$(PROD_COMPOSE) logs -f --tail=100

# --------------------------------------------------------------------------- #
# Destructive
# --------------------------------------------------------------------------- #
destroy-volumes: ## !! DELETES the database, chat history and traces forever !!
	@echo ""
	@echo "  ****************************  DANGER  ****************************"
	@echo "  This deletes the Postgres volume (all agent sessions, knowledge"
	@echo "  embeddings and session tables), the Open WebUI volume (all accounts,"
	@echo "  chats and uploads) and the Phoenix volume (all traces)."
	@echo ""
	@echo "  Files in ./workspace_files are NOT touched."
	@echo ""
	@echo "  This is NOT the fix for a database password error. If Postgres"
	@echo "  rejects your password, the volume was initialised with a different"
	@echo "  DB_PASS - see 'Password authentication failed' in the README"
	@echo "  troubleshooting section and change the role's password instead."
	@echo "  ******************************************************************"
	@echo ""
	@read -r -p "  Type 'destroy' to continue: " answer; \
	if [ "$$answer" = "destroy" ]; then \
		$(DEV_COMPOSE) down -v --remove-orphans; \
		echo "  Volumes removed."; \
	else \
		echo "  Aborted - nothing was deleted."; \
	fi
