# Zendesk Co-Pilot — BetterPlace

Local-first AI co-pilot for BetterPlace Group Product Support and Managed Services. Pulls tickets from Zendesk, runs Claude over each one, and serves a live agent dashboard at `https://BPSSTicketCoPilot.betterplace.co.in`.

> **Phase 1 status:** code complete, ready to bring up.
> **Hosting:** Subhajit's laptop (short-term) → company server (when available).
> **Cost:** Claude API only, capped at $20/month.

---

## Folder layout

```
zd-copilot/
├── README.md
├── .env.example                ← copy to .env
├── .gitignore
├── requirements.txt
├── Makefile                    ← make setup / sync / ai / web / tunnel
├── cloudflared.yml.example     ← Cloudflare Tunnel config
├── src/
│   ├── config.py               ← env loading + constants
│   ├── db.py                   ← SQLite schema + helpers
│   ├── zendesk.py              ← Zendesk API client
│   ├── ai.py                   ← Claude prompt + analyzer (Haiku 4.5 + caching)
│   ├── sync_worker.py          ← Zendesk → SQLite (every 5 min)
│   ├── ai_worker.py            ← per-ticket Claude analysis
│   └── web/
│       ├── app.py              ← FastAPI + Google SSO + agent view
│       ├── templates/
│       │   ├── index.html      ← ticket list + first ticket
│       │   └── ticket_panel.html ← single ticket panel (HTMX swaps)
│       └── static/styles.css   ← UI stylesheet
├── scripts/
│   ├── zd_pull.py              ← Phase 0 sample pull
│   └── build_phasing_doc.js
├── docs/                       ← arch doc, mockup, agent view, KB drafts
└── data/                       ← copilot.db (SQLite), kb_drafts/, gitignored
```

---

## First-time setup (one-time, ~10 min)

```bash
cd ~/Documents/zd-copilot

# 1. Create venv + install all deps
make setup

# 2. Activate the venv (every new terminal)
source myenv/bin/activate

# 3. Copy and fill in .env
cp .env.example .env
# Edit .env — fill ZD_TOKEN, ANTHROPIC_API_KEY, GOOGLE_CLIENT_ID/SECRET, SESSION_SECRET

# 4. Initialise the SQLite database
make init-db

# 5. Sanity check Zendesk auth
make check
```

---

## Bring it up — daily workflow

After setup, three things need to be running. Open three terminals (or use `tmux`/`screen`):

**Terminal A — sync worker** (keeps DB in sync with Zendesk):
```bash
source myenv/bin/activate && make sync-loop
```

**Terminal B — AI worker** (analyses new/changed tickets):
```bash
source myenv/bin/activate && make ai-loop
```

**Terminal C — web app** (the agent dashboard):
```bash
source myenv/bin/activate && make web
```

Open `http://localhost:8000` to use it. The Cloudflare Tunnel (next section) makes it available to the team at `https://BPSSTicketCoPilot.betterplace.co.in`.

To stop everything: `Ctrl+C` in each terminal.

---

## Cloudflare Tunnel — team access from anywhere (~5 min one-time)

This exposes your laptop's local web app at `https://BPSSTicketCoPilot.betterplace.co.in` without opening any ports.

```bash
# 1. Install cloudflared (macOS)
brew install cloudflared

# 2. Log in to your Cloudflare account
cloudflared tunnel login

# 3. Create the tunnel
cloudflared tunnel create copilot

# 4. Route your domain to the tunnel
cloudflared tunnel route dns copilot BPSSTicketCoPilot.betterplace.co.in

# 5. Copy the example config and fill in the tunnel UUID
cp cloudflared.yml.example cloudflared.yml
# Edit cloudflared.yml — paste the UUID from step 3

# 6. Run the tunnel (keep this terminal open, or daemonise)
make tunnel
```

The DNS record needs to point at Cloudflare. If `betterplace.co.in` is on Cloudflare, this is automatic. If not, add a CNAME record pointing `BPSSTicketCoPilot` to `<UUID>.cfargotunnel.com`.

---

## Google Workspace SSO — auth (~5 min one-time)

1. Go to https://console.cloud.google.com → APIs & Services → Credentials
2. Create OAuth 2.0 Client ID (type: Web application)
3. Add authorised redirect URIs:
   - `https://BPSSTicketCoPilot.betterplace.co.in/auth/callback`
   - `http://localhost:8000/auth/callback` (for local testing)
4. Copy the Client ID + Secret into `.env` as `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`
5. Generate a session secret: `python -c "import secrets; print(secrets.token_hex(32))"` → paste into `SESSION_SECRET`
6. The 14 engineer emails in `ALLOWED_EMAILS` (already populated in `.env.example`) are who can sign in

When `GOOGLE_CLIENT_ID` is unset, the app runs in dev-open mode — anyone hitting the URL is logged in as `dev@local`. Useful for testing before SSO is wired up.

---

## Cost & budget

The system is hard-capped at `MONTHLY_BUDGET_USD=20` (configurable in `.env`).

- Default model: `claude-haiku-4-5-20251001` (5× cheaper than Sonnet)
- Prompt caching on the field taxonomy → 90% discount on cached input
- Tickets are only re-analysed when their conversation changes
- Closed tickets are skipped unless an agent opens them

Check spend: `make spend` or visit `/spend` in the app.

When the cap is hit, the AI worker pauses. Sync continues; agents still see existing insights; new tickets just don't get analysed until the cap resets next month or you raise it.

---

## Day 1 sanity test

```bash
source myenv/bin/activate
make init-db
make check          # auth works, groups located
make sync           # pulls last 60 days of tickets — takes ~5-10 min
make ai             # analyses the 10 most recent untouched tickets — costs ~$0.05
make web            # http://localhost:8000 — see the agent dashboard
```

---

## Phasing

- **Phase 0** ✅ Design + 10-ticket proof + agent-view mockup (done)
- **Phase 1** 🚧 Local MVP — sync, AI, web, SSO, tunnel (this document)
- **Phase 2** Confluence indexing + Jira ID detection + KB autodraft
- **Phase 3** Suggested replies + write-back to Zendesk + auto-rules
- **Phase 4** Leadership dashboard (separate URL)
- **Phase 5** Continuous polish

See `docs/phasing_plan.docx` for the full plan.
