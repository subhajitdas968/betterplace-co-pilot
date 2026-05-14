# MCP Setup — Claude Desktop reads/writes our ticket database

This guide connects Claude Desktop to BetterPlace Co-Pilot via MCP. After setup, Claude Desktop becomes the source of all AI insights — every analysis, summary, suggestion, or write-back flows through your existing Claude subscription, not the metered API. The web UI (`http://127.0.0.1:8000`) keeps displaying insights exactly as today; only the producer changes.

## What you get

- **Search and read tickets from any Claude Desktop conversation.**
- **Generate insights** that flow into the same `ticket_insights` table the web UI reads.
- **Update fields and add dropdown options** with explicit confirmation in Claude Desktop.
- **Pre-built prompts** for common flows: full ticket analysis, morning digest, conversation summary.
- **Zero metered API spend.** The metered AI worker is now opt-in and disabled by default.

## One-time setup (~5 minutes)

### Step 1 — install the MCP package

```bash
cd ~/Documents/zd-copilot
source myenv/bin/activate
pip install -r requirements.txt   # picks up the new mcp dependency
```

### Step 2 — disable the metered AI worker

Edit `~/Documents/zd-copilot/.env` and set:

```
ENABLE_AI_WORKER=false
```

The worker now exits cleanly on `make ai` / `make ai-loop` without making API calls. Existing insights remain visible. You can flip back to `true` any time.

### Step 3 — get the Claude Desktop config snippet

```bash
make mcp-config
```

This prints something like:

```json
{
  "mcpServers": {
    "betterplace-copilot": {
      "command": "/Users/subhajitdas/Documents/zd-copilot/myenv/bin/python",
      "args": ["/Users/subhajitdas/Documents/zd-copilot/scripts/run_mcp.py"]
    }
  }
}
```

Copy this output.

### Step 4 — paste into Claude Desktop's config

The file lives at:

```
~/Library/Application Support/Claude/claude_desktop_config.json
```

Open it in any text editor. If the file doesn't exist, create it. If it does and already has `mcpServers`, merge — keep your existing servers and add `betterplace-copilot` as a sibling.

Save the file.

### Step 5 — restart Claude Desktop

Fully quit Claude Desktop (⌘Q), then re-open it. On a successful boot you should see a small 🔌 plug icon in the input area indicating MCP servers are connected. Click it and you should see `betterplace-copilot` listed.

## Smoke tests — walk through these in order

### Test 1 — Read tickets

In a Claude Desktop conversation, type:

> Search for the 5 most recent open tickets for BPCL.

Expected: Claude uses the `search_tickets` tool, returns a list including ticket IDs, subjects, statuses. If it returns nothing, your database has no tickets yet — run `make sync` first.

### Test 2 — Read a specific ticket

> Get ticket 595049 and tell me what the customer wants.

Expected: Claude calls `get_ticket(595049)` and `get_conversation(595049)`, summarizes plainly.

### Test 3 — Full structured analysis (writes insight to DB)

In Claude Desktop, click the prompt slash menu (`/`) and pick `analyze_ticket`, or type:

> Run the analyze_ticket prompt for ticket 595049.

Expected:
1. Claude calls `get_ticket`, `get_conversation`, and a few `get_field_taxonomy` calls.
2. Claude generates a structured insight.
3. Claude calls `save_ticket_insight(...)` — Claude Desktop will pop a confirmation: **review and approve**.
4. Claude responds with a 3-line summary of what it wrote.

Now reload `http://127.0.0.1:8000/tickets/595049` in your browser. The AI panel should show the insight Claude just generated, exactly as if the metered worker had produced it.

### Test 4 — Bulk analysis

> Run analyze_ticket for the 10 most-untouched tickets.

Claude iterates: for each ticket, calls the tools, generates insight, calls `save_ticket_insight`. You confirm each save.

### Test 5 — Write-back (with confirmation)

> Set the Root Cause - Level 1 on ticket 595049 to "Issue".

Expected: Claude calls `update_ticket_field`. Claude Desktop pops a confirmation. You approve. The Zendesk ticket updates AND the local DB updates.

### Test 6 — Add a new dropdown option

> The customer mentioned "Multi-tenant routing" but that's not in the Bucketization field. Add it as a new option and apply to ticket 595049.

Expected: Claude calls `add_dropdown_option("Bucketization (Mandatory for Reliance)", "Multi-tenant routing")` then `update_ticket_field`. Both confirmations appear.

### Test 7 — Daily digest

> Run the morning_digest prompt.

Expected: a tight overnight briefing — pickup-overdue tickets, top customers by open volume, SLA risk, recommendation.

If all 7 pass, the migration is complete. You can leave `ENABLE_AI_WORKER=false` permanently.

## Where things live

| Concern | Path |
|---|---|
| MCP server code | `src/mcp_server.py` |
| Stdio entry point | `scripts/run_mcp.py` |
| Claude Desktop config | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| MCP server logs | `~/Library/Logs/Claude/mcp-server-betterplace-copilot.log` |
| Local SQLite DB | `~/Documents/zd-copilot/data/copilot.db` |
| Insight table the UI reads | `ticket_insights` (no schema change) |

## Troubleshooting

**🔌 icon doesn't appear in Claude Desktop**
Open `~/Library/Logs/Claude/mcp-server-betterplace-copilot.log`. Most likely:
- Wrong Python path in the config (use the venv interpreter shown by `make mcp-config`).
- `mcp` package not installed — re-run step 1.
- `data/copilot.db` doesn't exist — run `make init-db` then `make sync` once.

**Tools return empty / errors**
Run a smoke test from your terminal: `make mcp` and watch for `[stdin]` prompt — type `Ctrl-D` to exit. If imports fail, the error appears immediately.

**"Permission denied" on writes**
Claude Desktop must surface a confirmation dialog before any write tool runs. If a write silently fails, check the log file for `zendesk write failed: 401` or similar — likely your `.env` ZD token expired.

## What about Cowork / Claude Code?

The same `betterplace-copilot` MCP server works there too:

- **Claude Code** (terminal CLI): add the same JSON snippet to `~/.config/claude-code/config.json` (or wherever your `claude-code` config lives).
- **Cowork**: paste the same `mcpServers` block into your Cowork settings under "MCP servers".

## Going to production for the support team

Currently this is single-user (your laptop, stdio transport). When you're ready to give the team access, we'll switch to **HTTP transport over your Cloudflare Tunnel** — same server code, different startup flag. Each agent's Claude Desktop will then connect to `https://BPSSTicketCoPilot.betterplace.co.in/mcp` instead of spawning a local process. We'll do that as Block #8b after this single-user setup is confirmed working.
