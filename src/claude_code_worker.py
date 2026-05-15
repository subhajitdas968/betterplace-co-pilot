"""Claude Code-driven AI worker (free, uses your Claude subscription).

Loops through unanalyzed tickets and invokes the `claude` CLI for each one with
our MCP server attached. Claude Code generates the structured insight and saves
it via the `save_ticket_insight` MCP tool. Insights flow into the same DB the
web UI reads — identical UX to the metered path, $0 metered spend.

Usage:
    make ai-claude           # one-shot: process up to N unanalyzed tickets, then exit
    make ai-claude-loop      # continuous: polls every CLAUDE_CODE_INTERVAL seconds

Requirements:
    - `claude --version` works (npm install -g @anthropic-ai/claude-code)
    - .env has ZD creds (the MCP server uses them for the read tools)
"""
from __future__ import annotations
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from tempfile import NamedTemporaryFile

from . import db


# Tools we allow Claude Code to invoke without confirmation (headless automation).
# Read-only tools are safe; the only write is save_ticket_insight which only touches
# our local DB. Field updates / option additions / feedback recording are NOT here —
# those should remain interactive (Claude Desktop) for human approval.
ALLOWED_MCP_TOOLS = [
    "mcp__betterplace-copilot__get_ticket",
    "mcp__betterplace-copilot__get_conversation",
    "mcp__betterplace-copilot__find_similar_tickets",
    "mcp__betterplace-copilot__get_field_taxonomy",
    "mcp__betterplace-copilot__get_ai_insights",
    "mcp__betterplace-copilot__list_views",
    "mcp__betterplace-copilot__get_customer_summary",
    "mcp__betterplace-copilot__list_unanalyzed_tickets",
    "mcp__betterplace-copilot__search_tickets",
    "mcp__betterplace-copilot__save_ticket_insight",
]

# Claude Code's built-in tools we explicitly disable — this worker only uses MCP.
DISALLOWED_BUILTIN_TOOLS = ["Bash", "Edit", "Write", "Read", "Glob", "Grep", "WebFetch", "WebSearch"]


PROMPT_TEMPLATE = """Analyze BetterPlace ticket {ticket_id} and save a HISTORY-AWARE insight to the database.

The agent reading this insight needs to know FOUR things in plain English:
  (1) What is this ticket about?
  (2) What have we seen before that's similar, and how was each one resolved?
  (3) Where does this ticket stand RIGHT NOW?
  (4) What should the agent do NEXT?

Steps:
1. Call get_ticket({ticket_id}) and get_conversation({ticket_id}).
2. Call find_similar_tickets({ticket_id}, limit=5) to find historical context.
3. For each similar ticket returned, call get_ai_insights(that_ticket_id) so you can
   read what we already know about how it was resolved. Skip ones that return empty.
4. For any field where you'll suggest a change, call get_field_taxonomy("<field title>")
   to see the valid dropdown options. Field titles include: "Customer Name", "Priority",
   "Product", "Module", "Section", "Bucketization (Mandatory for Reliance)",
   "Root Cause - Level 1", "Root Cause - Level 2", "Jira ID", "KB Article",
   "How was this ticket resolved?", "What was the issue?".
5. Call save_ticket_insight with this exact JSON structure:

   - issue_summary: 2-3 sentences. What the customer is reporting in plain
     English: who they are, the symptom, the impact. Concrete, not generic.
     Example: "Reliance::Jio reports their SSO callback is returning a 502
     for users in the Mumbai region only. Users see a blank page after
     entering credentials. Started ~2h before the ticket was opened."

   - historical_context: 2-4 sentences. Reference past tickets by their
     display id (BP-NNNNNN if native, #N if Zendesk). Quote how each was
     resolved. Example: "We've seen this 3 times. #594221 (Reliance, 3 months
     ago) was the same SSO callback issue — resolved by restarting the auth
     pod in mum-prod-2. #592888 (different customer but same module) was a
     missing redirect URI. #595401 was a misconfigured firewall rule and is
     less likely to apply here." If no similar past tickets are useful, say:
     "No directly applicable past tickets found — this looks new."

   - current_state: 1-2 sentences. Where is this ticket NOW? Use the
     conversation order. Examples: "Agent acknowledged 35 min ago and asked
     for browser console logs. Customer sent screenshots but no logs yet,
     so we're waiting on the customer." OR "Customer reported 4h ago, no
     agent response yet. Sitting unassigned."

   - recommended_action: 1-2 sentences. SPECIFIC next step. If you've cited
     a similar resolved ticket, link the action to it. Example: "Apply the
     fix from #594221 — restart auth-pod-mum-prod-2 and confirm the
     callback returns 200." Otherwise: "Ask the customer for browser console
     logs to narrow down whether this is auth or network."

   - summary: 3-5 sentence catch-all (kept for backcompat — feel free to make
     it a one-paragraph fusion of the four above).

   - recommendations: array of {{"field": str, "current": str|null, "suggest": str,
     "confidence": 0.0-1.0, "reason": "1-2 sentences", "review": false,
     "propose_new_option": false}}. Empty array if nothing to suggest.

   - completeness: array of {{"state": "ok"|"miss"|"thin", "text": str,
     "hint": str|null}}.

   - similar_ticket_ids: list of int (the ids from find_similar_tickets).

   - similar_with_reasoning: array of {{"ticket_id": int, "match_pct": int,
     "why_relevant": "1 sentence — what makes this similar",
     "how_resolved": "1 sentence — how the past ticket was resolved (null if
     it wasn't resolved yet)", "applicability": "high"|"medium"|"low"}}.
     Include each ticket you actually referenced in historical_context.

   - suggested_reply: {{"flag": str, "flaws": [str], "current": str,
     "suggested": str}} OR null (only generate if there's an agent reply
     that's incomplete; null otherwise).

   - kb_worthy: bool (true only if recurring failure mode + clear resolution
     + no existing KB).

   - kb_topic: str OR null.

   - pickup_flag: null.

6. Reply with a single line: "Saved insight for ticket {ticket_id}".

Constraints:
- ACCURACY > VERBOSITY. Don't fabricate historical context. If you don't have
  strong matches, say so plainly in historical_context.
- DO NOT call update_ticket_field, add_dropdown_option, or record_ai_feedback.
- DO NOT propose dropdown values that aren't in get_field_taxonomy unless
  propose_new_option=true.
- ALWAYS reference past tickets by their display id (BP-NNNNNN or #N), never
  by your internal numbering.
- BE CONCISE in the tool calls (no extra commentary).
"""


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _write_mcp_config() -> Path:
    """Write a temp .mcp.json with absolute paths for our MCP server.
    Returns the path; caller should delete it after use."""
    py = sys.executable  # current python interpreter (the venv one)
    server_script = str(_project_root() / "scripts" / "run_mcp.py")
    cfg = {
        "mcpServers": {
            "betterplace-copilot": {
                "command": py,
                "args": [server_script],
            }
        }
    }
    tmp = NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump(cfg, tmp)
    tmp.close()
    return Path(tmp.name)


def _claude_path() -> str | None:
    return shutil.which("claude")


def find_unanalyzed(limit: int = 50, status: str = "open") -> list[dict]:
    """Direct DB query — same logic as the MCP tool but used server-side here."""
    db.init()
    where = ""
    if status == "open":
        where = " AND t.status IN ('new','open','pending','hold')"
    elif status == "untouched":
        where = (" AND t.status IN ('new','open') AND t.assignee_id IS NULL "
                 "AND t.id NOT IN (SELECT ticket_id FROM ticket_comments tc "
                 "JOIN users u ON u.id=tc.author_id WHERE u.role IN ('agent','admin'))")
    sql = f"""
        SELECT t.id, t.subject, t.status, t.updated_at,
               (SELECT MAX(created_at) FROM ticket_insights WHERE ticket_id = t.id) AS last_insight_at
        FROM tickets t
        WHERE 1=1{where}
          AND (
            NOT EXISTS (SELECT 1 FROM ticket_insights WHERE ticket_id = t.id)
            OR (SELECT MAX(created_at) FROM ticket_insights WHERE ticket_id = t.id) < t.updated_at
          )
        ORDER BY t.updated_at DESC
        LIMIT {int(limit)}
    """
    with db.conn() as c:
        return [dict(r) for r in c.execute(sql).fetchall()]


class UsageLimitReached(RuntimeError):
    """Raised when Claude Code reports the monthly API/subscription quota is exhausted."""


def _model() -> str:
    """Pick the model for the worker. Defaults to 'sonnet' — much cheaper than Opus
    while still excellent for structured-output tasks like field corrections.
    Override with env CLAUDE_CODE_WORKER_MODEL=haiku for max throughput / lowest cost,
    or =opus for highest quality (be aware of subscription quota burn)."""
    return os.environ.get("CLAUDE_CODE_WORKER_MODEL", "sonnet")


def analyze_one(ticket_id: int, mcp_config: Path, *, timeout: int = 240) -> tuple[bool, str]:
    """Spawn `claude -p ...` with our MCP server. Returns (ok, output)."""
    claude = _claude_path()
    if not claude:
        return False, "claude CLI not found on PATH"
    cmd = [
        claude, "-p", PROMPT_TEMPLATE.format(ticket_id=ticket_id),
        "--model", _model(),
        "--mcp-config", str(mcp_config),
        "--permission-mode", "bypassPermissions",   # whitelisted tools below ARE the safety
        "--allowedTools", ",".join(ALLOWED_MCP_TOOLS),
        "--disallowedTools", ",".join(DISALLOWED_BUILTIN_TOOLS),
    ]
    # IMPORTANT: strip ANTHROPIC_API_KEY so Claude Code uses your Claude.ai subscription
    # (OAuth) instead of the metered API. Otherwise the worker silently bills the API key
    # and hits its monthly cap. Same for ANTHROPIC_AUTH_TOKEN used by some envs.
    env = {k: v for k, v in os.environ.items()
           if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
    try:
        r = subprocess.run(
            cmd, cwd=str(_project_root()),
            capture_output=True, text=True, timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return False, f"timeout after {timeout}s"
    output = (r.stdout or "") + (("\n[stderr]\n" + r.stderr) if r.stderr else "")
    text = output.strip()
    # Fail fast on usage-limit errors — no point retrying 19 more times
    low = text.lower()
    if "api usage limits" in low or "you will regain access on" in low or "rate_limit" in low:
        raise UsageLimitReached(text[:400])
    return r.returncode == 0, text


def run_once(*, status: str = "open", limit: int = 20, max_tickets: int | None = None) -> dict:
    if not _claude_path():
        print("✗ `claude` CLI not found on PATH. Install with:\n"
              "    npm install -g @anthropic-ai/claude-code\n"
              "Then re-run.")
        return {"processed": 0, "errors": 0, "skipped": 0}

    targets = find_unanalyzed(limit=limit, status=status)
    if max_tickets:
        targets = targets[:max_tickets]
    if not targets:
        print("Nothing to analyze.")
        return {"processed": 0, "errors": 0, "skipped": 0}

    print(f"Found {len(targets)} unanalyzed tickets (status={status}) · model={_model()} (override with CLAUDE_CODE_WORKER_MODEL=...)")
    mcp_cfg = _write_mcp_config()
    processed, errors = 0, 0
    quota_exhausted = False
    try:
        for i, t in enumerate(targets, 1):
            tid = t["id"]
            print(f"  [{i}/{len(targets)}] #{tid} · {t['subject'][:80]}")
            try:
                ok, out = analyze_one(tid, mcp_cfg)
            except UsageLimitReached as e:
                print(f"      ✗ Usage limit reached. Stopping run.\n         {e}", file=sys.stderr, flush=True)
                quota_exhausted = True
                break
            if ok:
                processed += 1
                last = (out.splitlines() or [""])[-1]
                print(f"      ✓ {last[:120]}")
            else:
                errors += 1
                print(f"      ✗ {out[:300]}", file=sys.stderr, flush=True)
            # Be polite to the rate limiter
            time.sleep(2)
    finally:
        try:
            mcp_cfg.unlink()
        except Exception:
            pass
    print(f"Done. Processed {processed}, errors {errors}"
          + (" · QUOTA EXHAUSTED — loop will stop." if quota_exhausted else ""))
    return {"processed": processed, "errors": errors,
            "skipped": len(targets) - processed - errors,
            "quota_exhausted": quota_exhausted}


def _reanalyze_heartbeat_path() -> Path:
    return _project_root() / "data" / "reanalyze.heartbeat"


def _write_reanalyze_heartbeat(state: str, **extra) -> None:
    """Heartbeat for the bulk re-analyze worker — completely separate from the
    live worker's heartbeat so the admin UI can show both processes' state
    independently."""
    try:
        from datetime import datetime, timezone
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "pid": os.getpid(),
            "state": state,                # 'booting'|'running'|'done'|'stopped'|'error'
            **extra,
        }
        hb = _reanalyze_heartbeat_path()
        hb.parent.mkdir(exist_ok=True)
        with open(hb, "w") as fh:
            fh.write(json.dumps(payload))
    except Exception as e:
        print(f"reanalyze heartbeat write failed: {e}", file=sys.stderr, flush=True)


def _pick_reanalyze_targets(scope: str = "legacy_only", limit: int = 0) -> list[int]:
    """Decide which ticket ids to re-analyze, oldest first.

      scope='legacy_only': tickets whose latest insight has empty issue_summary
                           (i.e. analyzed under the old prompt) PLUS tickets
                           with no insight at all.
      scope='open':        every open/pending/hold ticket
      scope='all':         every ticket
      scope='no_insight':  only tickets with zero insights

    Order: oldest created_at first. Rationale: legacy insights on old tickets
    most need the history-aware upgrade. Newer tickets are more likely to be
    actively touched (and re-processed by the live worker anyway).
    """
    db.init()
    where_parts: list[str] = []
    if scope == "legacy_only":
        where_parts.append("""
            (NOT EXISTS (SELECT 1 FROM ticket_insights WHERE ticket_id = t.id)
             OR (
               SELECT issue_summary FROM ticket_insights
               WHERE ticket_id = t.id ORDER BY id DESC LIMIT 1
             ) IS NULL
             OR (
               SELECT issue_summary FROM ticket_insights
               WHERE ticket_id = t.id ORDER BY id DESC LIMIT 1
             ) = ''
            )
        """)
    elif scope == "open":
        where_parts.append("t.status IN ('new','open','pending','hold')")
    elif scope == "no_insight":
        where_parts.append("NOT EXISTS (SELECT 1 FROM ticket_insights WHERE ticket_id = t.id)")
    # 'all' = no filter
    where_sql = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""
    limit_sql = f" LIMIT {int(limit)}" if limit and limit > 0 else ""
    with db.conn() as c:
        rows = c.execute(
            f"SELECT t.id FROM tickets t{where_sql} ORDER BY t.created_at ASC{limit_sql}"
        ).fetchall()
    return [r["id"] for r in rows]


def run_reanalyze_bulk(*, scope: str = "legacy_only", limit: int = 0,
                       interval_seconds: float = 2.0) -> dict:
    """Re-analyze every matching ticket sequentially, writing progress to the
    reanalyze.heartbeat file. Designed to be spawned as a one-shot subprocess
    from the admin page — exits on its own when the queue is drained or on
    SIGTERM (Stop button)."""
    import signal as _signal

    def _term(signum, frame):
        raise KeyboardInterrupt()
    try:
        _signal.signal(_signal.SIGTERM, _term)
        _signal.signal(_signal.SIGINT,  _term)
    except Exception:
        pass

    _write_reanalyze_heartbeat("booting", scope=scope, limit=limit)
    ids = _pick_reanalyze_targets(scope=scope, limit=limit)
    total = len(ids)
    print(f"Reanalyze bulk — scope={scope} target={total} tickets · model={_model()}", flush=True)
    _write_reanalyze_heartbeat("running", scope=scope, total=total,
                                processed=0, errors=0, current_ticket=None)
    if total == 0:
        _write_reanalyze_heartbeat("done", scope=scope, total=0, processed=0, errors=0)
        print("Nothing to do.", flush=True)
        return {"processed": 0, "errors": 0, "total": 0}

    if not _claude_path():
        msg = "`claude` CLI not found on PATH. Install: npm install -g @anthropic-ai/claude-code"
        _write_reanalyze_heartbeat("error", error=msg)
        print(f"✗ {msg}", file=sys.stderr, flush=True)
        return {"processed": 0, "errors": 0, "total": total, "error": msg}

    mcp_cfg = _write_mcp_config()
    processed, errors = 0, 0
    quota_exhausted = False
    # Track per-ticket durations so we can show a rolling-average ETA to the UI
    run_started = time.time()
    last_n_durations: list[float] = []           # last 10 tickets, for smooth ETA

    def _emit_heartbeat(state: str, current_tid: int | None = None, extra: dict | None = None):
        # Compute ETA from the trailing window
        avg = (sum(last_n_durations) / len(last_n_durations)) if last_n_durations else None
        remaining = (total - processed - errors)
        eta_seconds = int(avg * remaining) if avg else None
        payload = {
            "scope": scope, "total": total,
            "processed": processed, "errors": errors,
            "current_ticket": current_tid,
            "elapsed_seconds": int(time.time() - run_started),
            "avg_seconds_per_ticket": round(avg, 1) if avg else None,
            "eta_seconds": eta_seconds,
        }
        if extra: payload.update(extra)
        _write_reanalyze_heartbeat(state, **payload)

    try:
        for i, tid in enumerate(ids, 1):
            tic = time.time()
            # Update heartbeat BEFORE starting the ticket so the UI shows
            # "current ticket #N" while it's actually being processed.
            _emit_heartbeat("running", current_tid=tid)
            try:
                ok, out = analyze_one(tid, mcp_cfg)
                if ok:
                    processed += 1
                    last = (out.splitlines() or [""])[-1] if out else ""
                    print(f"  [{i}/{total}] ✓ #{tid} · {last[:100]}", flush=True)
                else:
                    errors += 1
                    print(f"  [{i}/{total}] ✗ #{tid} · {(out or '')[:200]}",
                          file=sys.stderr, flush=True)
            except UsageLimitReached as e:
                quota_exhausted = True
                print(f"  ✗ #{tid}: quota exhausted — stopping.\n     {e}",
                      file=sys.stderr, flush=True)
                _emit_heartbeat("error", current_tid=tid, extra={"error": "quota_exhausted"})
                break
            except KeyboardInterrupt:
                _emit_heartbeat("stopped", current_tid=tid)
                print(f"\nStopped at {processed + errors}/{total}.", flush=True)
                return {"processed": processed, "errors": errors, "total": total,
                        "stopped": True}
            except Exception as e:
                errors += 1
                print(f"  ⚠ #{tid}: {type(e).__name__}: {e}",
                      file=sys.stderr, flush=True)
            # Record duration for the rolling-window ETA
            elapsed = time.time() - tic
            last_n_durations.append(elapsed)
            if len(last_n_durations) > 10:
                last_n_durations.pop(0)
            # Heartbeat after every ticket so the UI updates promptly
            _emit_heartbeat("running", current_tid=tid)
            # Polite gap so the rest of the box stays responsive
            if interval_seconds > 0:
                time.sleep(interval_seconds)
    finally:
        try: mcp_cfg.unlink()
        except Exception: pass

    final_state = "error" if quota_exhausted else "done"
    _write_reanalyze_heartbeat(final_state, scope=scope, total=total,
                                processed=processed, errors=errors)
    print(f"Reanalyze finished. processed={processed} errors={errors} target={total}", flush=True)
    return {"processed": processed, "errors": errors, "total": total,
            "quota_exhausted": quota_exhausted}


def run_reanalyze_one(ticket_id: int) -> dict:
    """Single-ticket re-analysis. Used by the per-ticket Re-analyze button —
    spawned as its own subprocess so it doesn't block uvicorn."""
    db.init()
    print(f"Reanalyze single ticket #{ticket_id} · model={_model()}", flush=True)
    _write_reanalyze_heartbeat("booting", scope="one", ticket_id=ticket_id)
    if not _claude_path():
        _write_reanalyze_heartbeat("error", scope="one", error="claude CLI not found")
        print("✗ `claude` CLI not found on PATH.", file=sys.stderr, flush=True)
        return {"processed": 0, "errors": 1, "error": "claude_not_installed"}
    mcp_cfg = _write_mcp_config()
    _write_reanalyze_heartbeat("running", scope="one", total=1, processed=0,
                                current_ticket=ticket_id)
    try:
        ok, out = analyze_one(ticket_id, mcp_cfg)
    finally:
        try: mcp_cfg.unlink()
        except Exception: pass
    if ok:
        _write_reanalyze_heartbeat("done", scope="one", total=1, processed=1, errors=0,
                                    ticket_id=ticket_id)
        print(f"✓ #{ticket_id}: {(out or '').splitlines()[-1] if out else ''}", flush=True)
        return {"processed": 1, "errors": 0, "output": out}
    _write_reanalyze_heartbeat("error", scope="one", total=1, processed=0, errors=1,
                                ticket_id=ticket_id,
                                error=(out or "claude returned non-zero")[:400])
    print(f"✗ #{ticket_id}: {out}", file=sys.stderr, flush=True)
    return {"processed": 0, "errors": 1, "output": out}


def _write_heartbeat(state: str, extra: dict | None = None) -> None:
    """Drop a one-line heartbeat file the admin page polls. Independent of stdout
    buffering so even if logging breaks, the page can still tell the worker is
    alive."""
    try:
        from datetime import datetime, timezone
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "pid": os.getpid(),
            "state": state,         # 'booting' | 'working' | 'sleeping' | 'quota_exhausted' | 'error'
        }
        if extra: payload.update(extra)
        hb = _project_root() / "data" / "ai_worker.heartbeat"
        hb.parent.mkdir(exist_ok=True)
        with open(hb, "w") as fh:
            fh.write(json.dumps(payload))
    except Exception as e:
        print(f"heartbeat write failed: {e}", file=sys.stderr, flush=True)


def run_loop_or_quit_on_quota(*, status: str = "open", limit: int = 20, interval_seconds: int = 300) -> None:
    """Continuous loop that exits if quota is exhausted (no point retrying every 5 min).
    Writes a heartbeat file on every transition so the admin page can tell the
    worker is alive even when nothing's in the queue."""
    print(f"Claude Code AI worker · interval={interval_seconds}s · status={status}", flush=True)
    _write_heartbeat("booting", {"interval": interval_seconds})
    while True:
        try:
            _write_heartbeat("working")
            result = run_once(status=status, limit=limit)
            _write_heartbeat("sleeping", {"last_processed": result.get("processed", 0),
                                          "last_errors": result.get("errors", 0)})
            if result.get("quota_exhausted"):
                _write_heartbeat("quota_exhausted")
                print("Quota exhausted — exiting loop. Re-run after the reset, or "
                      "see docs/MCP_SETUP.md for fallback options.", flush=True)
                return
        except Exception as e:
            _write_heartbeat("error", {"error": str(e)[:300]})
            # "database is locked" is the most common transient — note it but
            # don't escalate. The retry happens on the NEXT loop tick.
            kind = "transient" if ("locked" in str(e).lower() or "busy" in str(e).lower()) else "error"
            print(f"loop {kind}: {e}", file=sys.stderr, flush=True)
        print(f"sleeping {interval_seconds}s …", flush=True)
        time.sleep(interval_seconds)


def run_loop(*, status: str = "open", limit: int = 20, interval_seconds: int = 300) -> None:
    print(f"Claude Code AI worker starting · interval={interval_seconds}s · status={status}")
    while True:
        try:
            run_once(status=status, limit=limit)
        except Exception as e:
            kind = "transient" if ("locked" in str(e).lower() or "busy" in str(e).lower()) else "error"
            print(f"loop {kind}: {e}", file=sys.stderr, flush=True)
        print(f"sleeping {interval_seconds}s …")
        time.sleep(interval_seconds)


def _print_boot_banner(args) -> None:
    """Dump the worker's effective config to stdout the second it starts so the
    log file isn't blank while we wait for the first batch."""
    import shutil
    claude_bin = shutil.which("claude")
    # Detect which mode we're booting into so the banner is actually accurate.
    if args.reanalyze_one is not None:
        mode = f"single re-analyze (#{args.reanalyze_one})"
    elif args.reanalyze_bulk:
        mode = f"bulk re-analyze"
    elif args.loop:
        mode = "live loop"
    else:
        mode = "one-shot batch"
    print("=" * 60, flush=True)
    print(f"Claude Code AI worker booted at {time.strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
    print(f"  mode:          {mode}", flush=True)
    print(f"  python:        {sys.executable}", flush=True)
    print(f"  cwd:           {os.getcwd()}", flush=True)
    print(f"  claude bin:    {claude_bin or '(NOT FOUND ON PATH — install with `npm install -g @anthropic-ai/claude-code`)'}", flush=True)
    print(f"  model:         {_model()}", flush=True)
    # Mode-specific config — don't print misleading live-worker defaults when
    # we're actually in a re-analyze subprocess.
    if args.reanalyze_bulk:
        print(f"  scope:         {args.reanalyze_scope}", flush=True)
        print(f"  hard cap:      {args.max if args.max else 'no cap (process every match)'}", flush=True)
        print(f"  throttle:      {args.reanalyze_throttle}s between tickets", flush=True)
        print(f"  order:         created_at ASC (oldest first)", flush=True)
    elif args.reanalyze_one is not None:
        print(f"  target ticket: #{args.reanalyze_one}", flush=True)
    else:
        print(f"  status filter: {args.status}", flush=True)
        print(f"  batch limit:   {args.limit}", flush=True)
        print(f"  loop interval: {args.interval}s", flush=True)
    print(f"  has ANTHROPIC_API_KEY:    {'YES' if os.getenv('ANTHROPIC_API_KEY') else 'no  (MCP/OAuth path)'}", flush=True)
    print(f"  has ANTHROPIC_AUTH_TOKEN: {'YES' if os.getenv('ANTHROPIC_AUTH_TOKEN') else 'no'}", flush=True)
    print("=" * 60, flush=True)


def main() -> None:
    # Force line-buffered stdout/stderr so every print() flushes on newline.
    # Critical when stdout is redirected to a file (as it is when spawned by
    # the FastAPI worker controller); without this Python buffers in 4KB blocks
    # and the log file looks empty for minutes.
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except AttributeError:
        pass  # very old Python; PYTHONUNBUFFERED=1 covers it

    p = argparse.ArgumentParser(description="Claude Code-driven AI worker.")
    p.add_argument("--once", action="store_true", help="Process one batch and exit (default).")
    p.add_argument("--loop", action="store_true", help="Run continuously every --interval seconds.")
    p.add_argument("--status", default="open",
                   help="open | untouched | <specific zd status> (default: open)")
    p.add_argument("--limit", type=int, default=int(os.getenv("AI_WORKER_BATCH_SIZE", "20")),
                   help="Max tickets per batch (default 20 or AI_WORKER_BATCH_SIZE env).")
    p.add_argument("--interval", type=int,
                   default=int(os.getenv("AI_WORKER_POLL_SECONDS", "300")),
                   help="Loop interval in seconds (default 300 or AI_WORKER_POLL_SECONDS env).")
    p.add_argument("--max", type=int, default=None, help="Hard cap on total tickets to process this run.")
    # Re-analyze modes — process tickets that already have insights but need
    # the new history-aware version. Spawned as one-shot subprocesses by the
    # admin UI so they don't block uvicorn.
    p.add_argument("--reanalyze-bulk", action="store_true",
                   help="Re-analyze every ticket matching --reanalyze-scope and exit.")
    p.add_argument("--reanalyze-scope", default="legacy_only",
                   choices=["legacy_only", "open", "all", "no_insight"],
                   help="Which tickets to re-analyze.")
    p.add_argument("--reanalyze-one", type=int,
                   help="Re-analyze a single ticket by id and exit.")
    p.add_argument("--reanalyze-throttle", type=float, default=2.0,
                   help="Seconds to sleep between tickets in bulk mode (default 2.0).")
    args = p.parse_args()
    _print_boot_banner(args)

    # New re-analyze modes take priority over the legacy --once / --loop flow
    if args.reanalyze_one is not None:
        run_reanalyze_one(args.reanalyze_one)
        return
    if args.reanalyze_bulk:
        run_reanalyze_bulk(scope=args.reanalyze_scope, limit=args.max or 0,
                            interval_seconds=args.reanalyze_throttle)
        return

    if args.loop:
        run_loop_or_quit_on_quota(status=args.status, limit=args.limit, interval_seconds=args.interval)
    else:
        run_once(status=args.status, limit=args.limit, max_tickets=args.max)


if __name__ == "__main__":
    main()
