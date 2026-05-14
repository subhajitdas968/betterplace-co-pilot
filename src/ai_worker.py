"""AI worker: for each ticket where insights are stale, calls Claude and stores the result.
Respects the monthly budget cap. Skips closed tickets unless --include-closed."""
from __future__ import annotations
import argparse
import json
import sqlite3
import sys
import time
import traceback

from . import ai, config, db


def needs_analysis(c: sqlite3.Connection, ticket_id: int) -> bool:
    """True if no insight exists or ticket has been updated since the last insight."""
    row = c.execute("""
        SELECT t.updated_at, i.created_at AS analyzed_at
        FROM tickets t
        LEFT JOIN ticket_insights i ON i.id = (
            SELECT id FROM ticket_insights WHERE ticket_id = t.id ORDER BY id DESC LIMIT 1
        )
        WHERE t.id = ?
    """, (ticket_id,)).fetchone()
    if not row:
        return False
    if row["analyzed_at"] is None:
        return True
    # Re-analyze if ticket has been updated AFTER the last analysis
    return (row["updated_at"] or "") > (row["analyzed_at"] or "")


def pick_targets(c: sqlite3.Connection, *, include_closed: bool, limit: int | None) -> list[int]:
    """Return ticket IDs needing analysis. Open/new/pending first; closed last (often skipped)."""
    sql = """
        SELECT t.id, t.status, t.updated_at
        FROM tickets t
        WHERE 1=1
    """
    if not include_closed:
        sql += " AND t.status NOT IN ('closed')"
    sql += " ORDER BY CASE WHEN t.status IN ('new','open','pending','hold') THEN 0 ELSE 1 END, t.updated_at DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = c.execute(sql).fetchall()
    return [r["id"] for r in rows if needs_analysis(c, r["id"])]


def store_insight(c: sqlite3.Connection, ticket_id: int, result: dict) -> None:
    insight = result["insight"]
    c.execute("""
        INSERT INTO ticket_insights (ticket_id, model, summary, recommendations, completeness,
            similar_ticket_ids, suggested_reply, kb_worthy, kb_topic, pickup_flag, created_at,
            cost_usd, input_tokens, output_tokens, cached_input_tokens)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        ticket_id, result["model"],
        insight.get("summary", ""),
        json.dumps(insight.get("recommendations") or []),
        json.dumps(insight.get("completeness") or []),
        json.dumps(insight.get("similar_ticket_keys") or []),  # topic keys until Phase 2 vector search
        json.dumps(insight.get("suggested_reply")) if insight.get("suggested_reply") else None,
        1 if insight.get("kb_worthy") else 0,
        insight.get("kb_topic"),
        json.dumps(insight.get("pickup_flag")) if insight.get("pickup_flag") else None,
        db.now_iso(),
        result["cost_usd"], result["input_tokens"], result["output_tokens"], result["cached_input_tokens"],
    ))
    db.log_spend(c, ticket_id=ticket_id, cost=result["cost_usd"],
                 in_tok=result["input_tokens"], out_tok=result["output_tokens"],
                 cached_tok=result["cached_input_tokens"], model=result["model"])


def run_once(*, include_closed: bool = False, limit: int | None = None) -> dict:
    if not config.ENABLE_AI_WORKER:
        print("AI worker disabled (ENABLE_AI_WORKER=false). Insights now come from Claude Desktop via MCP.")
        print("To re-enable the metered worker temporarily, set ENABLE_AI_WORKER=true in .env.")
        return {"processed": 0, "skipped": 0, "errors": 0, "spent": 0, "disabled": True}
    db.init()
    processed, skipped, errors = 0, 0, 0
    with db.conn() as c:
        spent = db.month_to_date_spend(c)
        if spent >= config.MONTHLY_BUDGET_USD:
            print(f"⛔ Monthly budget cap hit (${spent:.4f} >= ${config.MONTHLY_BUDGET_USD}); pausing.")
            return {"processed": 0, "skipped": 0, "errors": 0, "spent": spent}

        targets = pick_targets(c, include_closed=include_closed, limit=limit)
        print(f"AI worker: {len(targets)} ticket(s) need analysis · MTD spend = ${spent:.4f}")

        for tid in targets:
            spent = db.month_to_date_spend(c)
            if spent >= config.MONTHLY_BUDGET_USD:
                print(f"⛔ Reached cap mid-run at ${spent:.4f} — stopping.")
                break
            try:
                result = ai.analyze_ticket(c, tid)
                store_insight(c, tid, result)
                processed += 1
                print(f"  #{tid} → ok · ${result['cost_usd']:.4f} · MTD ${spent + result['cost_usd']:.4f}")
            except Exception as e:
                errors += 1
                print(f"  #{tid} → ERROR: {e}", file=sys.stderr)
                traceback.print_exc(limit=2)
                time.sleep(1)

        spent = db.month_to_date_spend(c)
    return {"processed": processed, "skipped": skipped, "errors": errors, "spent": spent}


def run_loop(*, interval_seconds: int = 60, include_closed: bool = False) -> None:
    while True:
        try:
            run_once(include_closed=include_closed)
        except Exception as e:
            print(f"ai worker error: {e}", file=sys.stderr)
        time.sleep(interval_seconds)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--once", action="store_true")
    p.add_argument("--loop", action="store_true")
    p.add_argument("--include-closed", action="store_true")
    p.add_argument("--limit", type=int, default=None)
    args = p.parse_args()
    if args.loop:
        run_loop(include_closed=args.include_closed)
    else:
        run_once(include_closed=args.include_closed, limit=args.limit)


if __name__ == "__main__":
    main()
