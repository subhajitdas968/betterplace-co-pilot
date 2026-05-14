# BetterPlace Co-Pilot — common commands.
# Always activate your venv first:  source myenv/bin/activate

PY ?= python
UVICORN ?= uvicorn

.PHONY: help setup check sync sync-loop ai ai-loop ai-claude ai-claude-loop web tunnel all clean spend init-db backfill backfill-status mcp mcp-config release release-minor release-major rollback releases version

help:
	@echo "BetterPlace Co-Pilot — Phase 1 commands"
	@echo ""
	@echo "  make setup       create myenv/ and install all deps"
	@echo "  make init-db     initialise SQLite schema"
	@echo "  make check       verify Zendesk auth + group lookup"
	@echo ""
	@echo "  make sync        run one Zendesk → DB sync pass"
	@echo "  make sync-loop   keep syncing every SYNC_INTERVAL_SECONDS (default 5 min)"
	@echo ""
	@echo "  make ai              run METERED AI worker once (needs ENABLE_AI_WORKER=true)"
	@echo "  make ai-loop         keep METERED AI worker running every minute"
	@echo "  make ai-claude       run CLAUDE CODE worker once (free, uses your subscription)"
	@echo "  make ai-claude-loop  keep CLAUDE CODE worker running every 5 min"
	@echo ""
	@echo "  make web         start the FastAPI web app on localhost:8000"
	@echo "  make tunnel      start Cloudflare Tunnel (requires cloudflared)"
	@echo ""
	@echo "  make all         run sync once, ai once, then start web (single-machine demo)"
	@echo "  make spend       show month-to-date Claude spend"
	@echo "  make clean       wipe data/ JSON dumps (keeps DB)"
	@echo ""
	@echo "  make backfill-status         show current sync watermark"
	@echo "  make backfill DAYS=365       reset watermark to N days ago, then run 'make sync'"
	@echo ""
	@echo "Always: source myenv/bin/activate before any of the above."

setup:
	$(PY) -m venv myenv
	./myenv/bin/pip install --upgrade pip
	./myenv/bin/pip install -r requirements.txt
	@echo ""
	@echo "Done. Now: source myenv/bin/activate && cp .env.example .env && edit .env"

init-db:
	$(PY) -c "from src import db; db.init(); print('DB initialised at data/copilot.db')"

check:
	$(PY) scripts/zd_pull.py --check

sync:
	$(PY) -m src.sync_worker --once

sync-loop:
	$(PY) -m src.sync_worker --loop

ai:
	$(PY) -m src.ai_worker --once

ai-loop:
	$(PY) -m src.ai_worker --loop

# Claude Code-driven worker — uses your `claude` CLI subscription, no metered API spend.
# Set ENABLE_AI_WORKER=false in .env to keep the metered worker dormant.
ai-claude:
	$(PY) -m src.claude_code_worker --once --status open --limit 20

ai-claude-loop:
	$(PY) -m src.claude_code_worker --loop --status open --limit 20 --interval 300

web:
	$(UVICORN) src.web.app:app --host $${APP_HOST:-127.0.0.1} --port $${APP_PORT:-8000} --reload

tunnel:
	cloudflared tunnel --config cloudflared.yml run

all: init-db sync ai web

spend:
	@$(PY) -c "from src import db; from src.config import MONTHLY_BUDGET_USD; \
	import sqlite3; \
	c = sqlite3.connect('data/copilot.db'); c.row_factory = sqlite3.Row; \
	s = db.month_to_date_spend(c); \
	print(f'MTD: \$${s:.4f} of \$${MONTHLY_BUDGET_USD:.0f}')"

clean:
	@find data -maxdepth 1 -type f -name '*.json' -delete
	@echo "data/ JSON dumps cleaned (DB and kb_drafts kept)"

backfill-status:
	$(PY) scripts/backfill.py --status

# Usage: make backfill DAYS=365
DAYS ?= 365
backfill:
	$(PY) scripts/backfill.py --days $(DAYS)

# MCP server — for local smoke test only. Claude Desktop spawns scripts/run_mcp.py directly.
mcp:
	$(PY) scripts/run_mcp.py

# Print the snippet you paste into ~/Library/Application Support/Claude/claude_desktop_config.json
# Uses the currently-active python interpreter so it's correct regardless of where myenv lives.
mcp-config:
	@PY_PATH=$$(command -v python); \
	echo '{'; \
	echo '  "mcpServers": {'; \
	echo '    "betterplace-copilot": {'; \
	printf '      "command": "%s",\n' "$$PY_PATH"; \
	echo '      "args": ["$(PWD)/scripts/run_mcp.py"]'; \
	echo '    }'; \
	echo '  }'; \
	echo '}'

# ===== F9 · Release / rollback =====
# Usage: make release notes="Fixed sign-out flow"
# Bumps patch, snapshots DB, tags git, records to releases table.
NOTES ?=
release:
	@$(PY) -c "from src import release; r = release.create_release(part='patch', notes='$(NOTES)', actor_email='cli'); print(f\"✓ Released v{r['version']} (was v{r['previous_version']}) · git_tag={r['git_tag']} · backup={r['db_backup_path']}\")"

release-minor:
	@$(PY) -c "from src import release; r = release.create_release(part='minor', notes='$(NOTES)', actor_email='cli'); print(f\"✓ Released v{r['version']} (minor bump) · backup={r['db_backup_path']}\")"

release-major:
	@$(PY) -c "from src import release; r = release.create_release(part='major', notes='$(NOTES)', actor_email='cli'); print(f\"✓ Released v{r['version']} (major bump) · backup={r['db_backup_path']}\")"

# Usage: make rollback v=1.0.5
# Prints the script you should run; doesn't auto-execute because rollback
# overwrites the running DB.
v ?=
rollback:
	@if [ -z "$(v)" ]; then echo "Usage: make rollback v=1.0.5"; exit 1; fi
	@$(PY) -c "from src import release; r = release.prepare_rollback('$(v)'); print(r['script'])"

releases:
	@$(PY) -c "from src import release; rows = release.list_releases(); \
	print(f'{\"VERSION\":<12} {\"GIT_SHA\":<10} {\"WHEN\":<22} CURRENT  NOTES'); \
	[print(f\"v{r['version']:<11} {r['git_sha'] or '':<10} {r['created_at'][:19]:<22} {'★' if r['is_current'] else ' '}        {(r['notes'] or '')[:60]}\") for r in rows]"

version:
	@$(PY) -c "from src import release; i = release.runtime_info(); print(f\"v{i['version']} · {i['git_sha']} · {i['git_branch']} · clean={i['git_clean']}\")"
