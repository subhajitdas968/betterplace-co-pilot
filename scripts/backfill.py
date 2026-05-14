#!/usr/bin/env python3
"""Reset the sync watermark to N days ago so the next `make sync` re-pulls
the older tickets you don't yet have. Existing tickets are upserted (idempotent),
so nothing is lost — only new history is added.

Usage:
    source myenv/bin/activate
    python scripts/backfill.py --days 365     # pull last 1 year
    python scripts/backfill.py --days 730     # pull last 2 years
    python scripts/backfill.py --status       # just show the current watermark

Then run `make sync` to actually fetch.
"""
from __future__ import annotations
import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Make `from src import db` work when this script is run directly
# (e.g. `python scripts/backfill.py`) rather than only as a module.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import db  # noqa: E402


def show_status() -> None:
    db.init()
    with db.conn() as c:
        ts_str = db.get_meta(c, "last_sync_epoch")
        last_run = db.get_meta(c, "last_sync_run_at") or "(never)"
        n_tickets = c.execute("SELECT COUNT(*) AS n FROM tickets").fetchone()["n"]
    if ts_str:
        ts = int(ts_str)
        dt = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        age_days = (datetime.now(timezone.utc).timestamp() - ts) / 86400
    else:
        dt, age_days = "(unset)", 0
    print(f"Local DB tickets: {n_tickets}")
    print(f"Last sync run:    {last_run}")
    print(f"Watermark:        {dt}  ({age_days:.1f} days ago)")


def reset(days: int) -> None:
    db.init()
    new_ts = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())
    new_iso = datetime.fromtimestamp(new_ts, tz=timezone.utc).isoformat()
    with db.conn() as c:
        old = db.get_meta(c, "last_sync_epoch")
        db.set_meta(c, "last_sync_epoch", str(new_ts))
    print(f"Watermark reset.")
    print(f"  was: {old or '(unset)'}")
    print(f"  now: {new_ts}  ({new_iso}, {days} days ago)")
    print()
    print("Next:")
    print("  1. STOP your `make sync-loop` terminal (Ctrl+C)")
    print("  2. Run:  make sync")
    print("     This will pull every ticket changed since the watermark.")
    print(f"     At your volume, ~{days // 30} months of data will take 30-60 min.")
    print("  3. When sync finishes, restart `make sync-loop` to keep up with new tickets.")
    print()
    print("Notes:")
    print("  - Tickets already in the DB are upserted (idempotent) — no data lost.")
    print("  - The AI worker has a $20/mo cap and processes recent OPEN tickets first.")
    print("    Backfilled CLOSED tickets are skipped by default (saves Claude spend).")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--days", type=int, default=None, help="Reset watermark to N days ago.")
    p.add_argument("--status", action="store_true", help="Print current watermark and exit.")
    args = p.parse_args()
    if args.status or args.days is None:
        show_status()
        if args.days is None:
            print()
            print("Pass --days N to reset, e.g. --days 365 for 1 year.")
        return
    reset(args.days)


if __name__ == "__main__":
    main()
