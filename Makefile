# Odoo AI Ops - task runner for the local stack, debugging and the test suites.
#
# Everything runs in Docker; nothing is executed against a host Python. Run
# `make` on its own for the list of targets.
#
# Windows: install make with `winget install ezwinports.make`, then RUN THESE FROM
# GIT BASH, not PowerShell - the recipes are bash, and the mount paths below rely
# on `uname` to detect MSYS. Docker Desktop wants `C:/...` style mounts while MSYS
# make reports `/c/...` and rewrites `/`-prefixed arguments; both are handled, so
# the same targets also work unchanged on WSL, macOS and CI.

SHELL := /bin/bash
.DEFAULT_GOAL := help

# --- Host/platform handling -------------------------------------------------
# Docker Desktop needs a `C:/...` mount source. Which shape $(CURDIR) already has
# depends on which make you installed, so normalise rather than assume:
#   * native Windows make (ezwinports, choco) reports `C:/odoo/...` - use as is;
#   * MSYS/Cygwin make reports `/c/odoo/...` - convert with cygpath -m.
# Detecting on the drive-letter colon is what tells the two apart; guessing wrong
# yields `/C:/odoo/...` and every mount fails.
UNAME_S := $(shell uname -s 2>/dev/null || echo Unknown)
ifneq (,$(filter MINGW% MSYS% CYGWIN%,$(UNAME_S)))
  export MSYS_NO_PATHCONV := 1
  ifneq (,$(findstring :,$(CURDIR)))
    MOUNT := $(CURDIR)
  else
    MOUNT := $(shell cygpath -m "$(CURDIR)" 2>/dev/null || echo "/$(CURDIR)")
  endif
else
  MOUNT := $(CURDIR)
endif

# --- Configuration (override on the command line: `make up ODOO_DB=other`) ---
ODOO_DB        ?= odoo_19
ODOO_TEST_DB   ?= test_ai_ops
COMPOSE_NETWORK?= odoo-ai-ops_default
AGENT_IMG      ?= odoo-ai-ops-agent
PY_IMG         ?= python:3.12-slim
SHIM           := ./agent/tests/integration/run_edge_shim.sh

# pytest is not in the agent runtime image; layer it on for test runs.
PYTEST_DEPS := pytest pytest-asyncio

# Run something in the agent image, on the compose network, with the repo's
# agent/ mounted so edits are picked up without a rebuild.
AGENT_RUN = docker run --rm --network $(COMPOSE_NETWORK) --env-file .env \
	-e LANGFUSE_HOST=http://langfuse-web:3000 \
	-e LANGFUSE_BASE_URL=http://langfuse-web:3000 \
	-e VALKEY_URL=redis://redis:6379 \
	-e ODOO_BASE_URL=http://web:8069 -e ODOO_DB=$(ODOO_DB) \
	-e ODOO_USERNAME=admin -e ODOO_PASSWORD=admin \
	-e ENABLE_SQS_WORKER=false -e CLICKHOUSE_HTTP=http://clickhouse:8123 \
	-v "$(MOUNT)/agent:/app" -w /app $(AGENT_IMG)

# Offline agent runs (no stack, no network, no secrets).
AGENT_OFFLINE = docker run --rm -v "$(MOUNT)/agent:/app" -w /app $(AGENT_IMG)

# Plain Python against the whole repo, for the Lambda and live Shopify suites.
REPO_RUN = docker run --rm --env-file .env -v "$(MOUNT):/repo" -w /repo $(PY_IMG)

.PHONY: help up down stop restart ps logs build db-init db-shell db-reset \
        odoo-shell shell-agent shell-odoo redis-cli health traces langfuse \
        shim-start shim-logs shim-stop \
        test test-all test-agent test-lambda test-odoo test-integration \
        test-live-shopify test-live-llm lint format clean-tasks

# ---------------------------------------------------------------------------
help: ## Show this help
	@echo ""
	@echo "  Odoo AI Ops - make targets"
	@echo ""
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "  Config: ODOO_DB=$(ODOO_DB)  NETWORK=$(COMPOSE_NETWORK)"
	@echo ""

# --- Stack ------------------------------------------------------------------
up: ## Start the whole stack (Odoo, agent, Valkey, Langfuse, ClickHouse, MinIO)
	docker compose up -d
	@echo "Odoo http://localhost:8069 | agent :8000 | Langfuse :3000 | MinIO :9001"

down: ## Stop and remove the stack (volumes are kept)
	docker compose down

stop: ## Stop the stack without removing containers
	docker compose stop

restart: ## Restart the stack
	docker compose restart

ps: ## Show service status
	@docker compose ps

logs: ## Follow logs. All services, or one: make logs SVC=web
	@docker compose logs -f --tail 100 $(SVC)

build: ## Rebuild the images
	docker compose build

# --- Database ---------------------------------------------------------------
db-init: ## Create the Odoo database and install odoo_ai_ops (~6 min, once)
	docker compose up -d db
	@echo "waiting for postgres..."
	@until docker compose exec -T db pg_isready -U odoo >/dev/null 2>&1; do sleep 2; done
	docker compose run --rm --entrypoint odoo web \
		-c /etc/odoo/odoo.conf -d $(ODOO_DB) --no-http \
		-i odoo_ai_ops --stop-after-init

db-reset: ## DESTRUCTIVE: drop the Odoo database and rebuild it from scratch
	@echo "This will DROP the database '$(ODOO_DB)'. Ctrl-C within 5s to abort."
	@sleep 5
	docker compose stop web
	docker compose exec -T db dropdb -U odoo --if-exists $(ODOO_DB)
	$(MAKE) db-init
	docker compose up -d web

db-shell: ## psql into the Odoo database
	docker compose exec db psql -U odoo -d $(ODOO_DB)

odoo-shell: ## Interactive Odoo shell against the Odoo database
	docker compose run --rm --entrypoint odoo web \
		shell -c /etc/odoo/odoo.conf -d $(ODOO_DB) --no-http

# --- Debugging --------------------------------------------------------------
shell-agent: ## Shell inside the running agent container
	docker compose exec agent bash

shell-odoo: ## Shell inside the running Odoo container
	docker compose exec web bash

redis-cli: ## Valkey CLI (LangGraph checkpoints live here)
	docker compose exec redis valkey-cli

health: ## Hit the agent and Odoo health endpoints from inside the network
	@$(AGENT_RUN) python -c "import httpx; \
print('agent  ', httpx.get('http://agent:8000/healthz', timeout=10).text); \
print('odoo   ', httpx.get('http://web:8069/web/login', timeout=20).status_code)"

traces: ## Last 20 LLM traces recorded in ClickHouse
	@docker compose exec -T clickhouse clickhouse-client --password clickhouse \
		--query "SELECT session_id, name, timestamp FROM traces ORDER BY timestamp DESC LIMIT 20 FORMAT PrettyCompact"

langfuse: ## Check Langfuse has a project + API key (empty tables cause 401s)
	@docker compose exec -T db psql -U odoo -d postgres -c \
		"select (select count(*) from organizations) as orgs, (select count(*) from projects) as projects, (select count(*) from api_keys) as api_keys;"

# --- Edge shim (local stand-in for the ingest Lambda) -----------------------
shim-start: ## Start the edge shim on :9100 (needed by 2 integration tests)
	@$(SHIM)

shim-logs: ## Follow the edge shim's log (watch real Shopify deliveries land)
	@$(SHIM) logs

shim-stop: ## Stop the edge shim
	@$(SHIM) stop

# --- Tests ------------------------------------------------------------------
test: test-agent test-lambda ## Fast offline suites (no stack, no creds, no cost)

test-all: test-agent test-lambda test-odoo test-integration ## Everything that costs nothing

test-agent: ## Agent unit tests (offline)
	$(AGENT_OFFLINE) sh -c "pip install --quiet $(PYTEST_DEPS) && \
		python -m pytest tests/ -q --ignore=tests/integration -p no:cacheprovider"

test-lambda: ## Ingest Lambda tests, incl. the offline HMAC checks
	$(REPO_RUN) bash -c "pip install -q pytest boto3 && \
		python -m pytest lambda/tests -q -p no:cacheprovider"

test-odoo: ## Odoo module suite (tag: ai_ops) in a throwaway database
	docker compose up -d db redis
	@until docker compose exec -T db pg_isready -U odoo >/dev/null 2>&1; do sleep 2; done
	-docker compose exec -T db dropdb -U odoo --if-exists $(ODOO_TEST_DB)
	docker compose run --rm --entrypoint odoo web \
		-c /etc/odoo/odoo.conf -d $(ODOO_TEST_DB) --no-http \
		-i odoo_ai_ops --test-enable --test-tags ai_ops --stop-after-init

test-integration: ## Full-stack suite against the running stack (LLM faked, free)
	@./agent/tests/integration/run.sh

test-live-shopify: ## Live Shopify API tests. Registers webhooks on the real store
	$(REPO_RUN) bash -c "pip install -q requests pytest boto3 && \
		RUN_SHOPIFY_LIVE_TESTS=1 python -m pytest custom_addons/odoo_ai_ops/tests/live -v -p no:cacheprovider"

test-live-llm: ## COSTS MONEY: real Claude calls + the full trace chain
	@echo "This makes real Anthropic API calls and will be billed."
	@echo "Set SHOPIFY_LIVE_TEST_SKU=<a real store SKU> to make reconciliation cross-system."
	@echo "Ctrl-C within 5s to abort."
	@sleep 5
	@RUN_LIVE_LLM=1 SHOPIFY_LIVE_TEST_SKU="$(SHOPIFY_LIVE_TEST_SKU)" \
		./agent/tests/integration/run.sh

# --- Code quality -----------------------------------------------------------
lint: ## ruff check over the agent, module and Lambda sources
	$(REPO_RUN) bash -c "pip install -q ruff && \
		python -m ruff check agent custom_addons lambda"

format: ## ruff format (rewrites files)
	$(REPO_RUN) bash -c "pip install -q ruff && \
		python -m ruff format agent custom_addons lambda"

clean-tasks: ## Delete the ai.ops.task rows the test suites leave behind
	@$(AGENT_RUN) python -c "import asyncio; \
from app.config import Settings; from app.runtime import AgentRuntime; \
async def main(): \
    rt = await AgentRuntime.create(Settings()); \
    ids = await rt.odoo_client.execute_kw('ai.ops.task','search',[[['name','like','AIOPS']]]); \
    await rt.odoo_client.execute_kw('ai.ops.task','unlink',[ids]); \
    print('removed', len(ids), 'tasks'); \
    await rt.aclose(); \
asyncio.run(main())"
