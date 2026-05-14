"""Sync worker: pulls Zendesk → SQLite. Run once or in a loop."""
from __future__ import annotations
import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone

from . import config, db, zendesk


def _now_epoch() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def _backfill_start_epoch() -> int:
    return int((datetime.now(timezone.utc) - timedelta(days=config.BACKFILL_DAYS)).timestamp())


def discover_target_groups() -> list[dict]:
    groups = zendesk.list_groups()
    target = [g for g in groups if any(name in (g["name"] or "").lower() for name in config.TARGET_GROUP_NAMES)]
    print(f"Target groups: {[g['name'] for g in target]}")
    return target


def sync_field_definitions() -> int:
    fields = zendesk.list_ticket_fields()
    with db.conn() as c:
        for f in fields:
            db.upsert_field_def(c, f)
    return len(fields)


def sync_ticket_forms() -> int:
    forms = zendesk.list_ticket_forms()
    if not forms:
        print("  (no ticket forms accessible — falling back to per-group field lists)")
        return 0
    with db.conn() as c:
        for f in forms:
            db.upsert_form(c, f)
    return len(forms)


def sync_custom_statuses() -> int:
    statuses = zendesk.list_custom_statuses()
    if not statuses:
        print("  (no custom statuses accessible — using ZD's six standard statuses)")
        return 0
    with db.conn() as c:
        for s in statuses:
            db.upsert_custom_status(c, s)
    return len(statuses)


def sync_tickets_since(start_epoch: int, group_ids: set[int]) -> int:
    """Pull tickets changed since start_epoch, filter by group, persist + their comments.
    Errors on a single ticket are logged and skipped; the worker keeps going."""
    seen, errors = 0, 0
    last_updated_epoch = start_epoch
    with db.conn() as c:
        for t in zendesk.incremental_tickets(start_epoch, group_ids=group_ids):
            try:
                db.upsert_ticket(c, t)
                seen += 1

                # Resolve requester / org / assignee names if missing
                for uid_field in ("requester_id", "assignee_id", "submitter_id"):
                    uid = t.get(uid_field)
                    if uid and not c.execute("SELECT 1 FROM users WHERE id=?", (uid,)).fetchone():
                        u = zendesk.fetch_user(uid)
                        if u:
                            db.upsert_user(c, u)
                oid = t.get("organization_id")
                if oid and not c.execute("SELECT 1 FROM organizations WHERE id=?", (oid,)).fetchone():
                    o = zendesk.fetch_org(oid)
                    if o:
                        db.upsert_org(c, o)

                # Comments — fetch on first sight or when ticket has been updated since last sync
                cmts, side_users = zendesk.fetch_comments(t["id"])
                for u in side_users:
                    db.upsert_user(c, u)
                for cm in cmts:
                    db.upsert_comment(c, t["id"], cm)
                    # Capture every attachment on this comment. Metadata only —
                    # binaries are pulled lazily via /api/attachments/{id}/download.
                    for a in (cm.get("attachments") or []):
                        db.upsert_attachment(c, t["id"], cm.get("id"), a)

                # SLA + timing metrics
                metrics = zendesk.fetch_ticket_metrics(t["id"])
                if metrics:
                    db.upsert_metrics(c, t["id"], metrics)

                ts = t.get("updated_at")
                if ts:
                    try:
                        last_updated_epoch = max(
                            last_updated_epoch,
                            int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()),
                        )
                    except Exception:
                        pass

                if seen % 50 == 0:
                    print(f"  synced {seen} tickets (last updated {ts}) · {errors} errors")
                # Checkpoint watermark every 100 tickets so backfill can resume after a crash
                if seen % 100 == 0:
                    db.set_meta(c, "last_sync_epoch", str(last_updated_epoch))
                    db.set_meta(c, "last_sync_run_at", db.now_iso())

            except Exception as e:
                errors += 1
                print(f"  ⚠ ticket #{t.get('id')}: {type(e).__name__}: {e} — skipping", file=sys.stderr)
                continue

        # Final checkpoint
        db.set_meta(c, "last_sync_epoch", str(last_updated_epoch))
        db.set_meta(c, "last_sync_run_at", db.now_iso())
    if errors:
        print(f"Sync finished with {errors} per-ticket errors (logged, skipped).")
    return seen


def run_once() -> dict:
    db.init()

    # Auth probe
    me = zendesk.whoami()
    print(f"Authed as: {me['name']} <{me['email']}>  · role={me['role']}")

    # Sync groups + fields
    groups = discover_target_groups()
    if not groups:
        print("✗ No target groups found — check TARGET_GROUP_NAMES in .env")
        sys.exit(2)

    with db.conn() as c:
        for g in groups:
            db.upsert_group(c, g)

    n_fields = sync_field_definitions()
    print(f"Synced {n_fields} ticket field definitions.")
    n_forms = sync_ticket_forms()
    print(f"Synced {n_forms} ticket forms.")
    n_statuses = sync_custom_statuses()
    print(f"Synced {n_statuses} custom statuses.")

    # Decide watermark
    with db.conn() as c:
        last_str = db.get_meta(c, "last_sync_epoch")
    if last_str:
        start_epoch = max(int(last_str) - 60, 0)  # back off 1 min for safety
        print(f"Incremental sync since epoch {start_epoch}")
    else:
        start_epoch = _backfill_start_epoch()
        print(f"Initial backfill — pulling last {config.BACKFILL_DAYS} days (since epoch {start_epoch})")

    group_ids = {g["id"] for g in groups}
    n = sync_tickets_since(start_epoch, group_ids)
    print(f"Synced {n} tickets.")

    return {"ticket_count": n}


def run_loop() -> None:
    while True:
        try:
            run_once()
        except Exception as e:
            print(f"sync error: {e}", file=sys.stderr)
        print(f"sleeping {config.SYNC_INTERVAL_SECONDS}s …")
        time.sleep(config.SYNC_INTERVAL_SECONDS)


def _heartbeat_path():
    from pathlib import Path
    return Path(__file__).resolve().parent.parent / "data" / "attachment_backfill.heartbeat"


def _write_attachment_heartbeat(state: str, **extra) -> None:
    """Write progress so /admin/attachments can poll without ssh-ing in. The
    file is overwritten in place so reads always get the latest snapshot."""
    import os
    from datetime import datetime, timezone
    try:
        hb = _heartbeat_path()
        hb.parent.mkdir(exist_ok=True)
        payload = {
            "ts":    datetime.now(timezone.utc).isoformat(),
            "pid":   os.getpid(),
            "state": state,        # 'booting' | 'running' | 'done' | 'stopped' | 'error'
            **extra,
        }
        with open(hb, "w") as fh:
            fh.write(json.dumps(payload))
    except Exception as e:
        print(f"heartbeat write failed: {e}", file=sys.stderr, flush=True)


def backfill_attachments_from_zd(limit: int | None = None) -> dict:
    """Walk every ticket and re-fetch its comments solely to capture attachment
    metadata. Writes data/attachment_backfill.heartbeat every 25 tickets so the
    admin page can poll progress + stop button works.

    Captures SIGTERM (sent by the admin Stop button) and treats it the same as
    KeyboardInterrupt so the worker exits gracefully with a 'stopped' heartbeat
    rather than dying mid-write."""
    import signal as _signal

    def _term_handler(signum, frame):
        # Raise KeyboardInterrupt from the signal so the existing handler runs.
        raise KeyboardInterrupt()
    try:
        _signal.signal(_signal.SIGTERM, _term_handler)
        _signal.signal(_signal.SIGINT,  _term_handler)
    except Exception:
        pass

    db.init()
    seen_tickets, total_attachments, errors = 0, 0, 0
    _write_attachment_heartbeat("booting")
    with db.conn() as c:
        sql = "SELECT id FROM tickets WHERE source='zendesk' ORDER BY updated_at DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        ids = [r["id"] for r in c.execute(sql).fetchall()]
    total = len(ids)
    print(f"Backfilling attachments across {total} tickets …", flush=True)
    _write_attachment_heartbeat("running",
                                processed=0, total=total,
                                attachments=0, errors=0, current_ticket=None)
    # Throttling — without a sleep, the backfill hammers the ZD API + holds the
    # SQLite write lock continuously, which makes the web UI feel sluggish when
    # opening other pages. A 200 ms inter-ticket pause is invisible at the
    # backfill timescale (hours total) but lets uvicorn breathe.
    import time as _time
    THROTTLE_SECONDS = float(os.getenv("ATTACHMENT_BACKFILL_THROTTLE", "0.2"))

    try:
        with db.conn() as c:
            # Give other writers a fair chance — if SQLite returns SQLITE_BUSY
            # because uvicorn is mid-write (field edit, audit log, etc.) we
            # wait up to 5s instead of crashing.
            c.execute("PRAGMA busy_timeout=5000")
            # Don't grow the WAL forever — checkpoint every 1000 frames.
            c.execute("PRAGMA wal_autocheckpoint=1000")
            for i, tid in enumerate(ids, 1):
                try:
                    cmts, side_users = zendesk.fetch_comments(tid)
                    # Make sure the side-loaded comment authors exist; otherwise
                    # users(id) FK on upsert_comment would fail too.
                    for u in side_users:
                        try: db.upsert_user(c, u)
                        except Exception: pass
                    for cm in cmts:
                        cid = cm.get("id")
                        # Backfill: ensure the comment exists FIRST so the
                        # attachment FK (comment_id → ticket_comments.id) is
                        # satisfied. upsert_comment is idempotent.
                        try:
                            db.upsert_comment(c, tid, cm)
                        except Exception as e:
                            print(f"  ⚠ #{tid} cm={cid}: comment upsert: {type(e).__name__}: {e}",
                                  file=sys.stderr, flush=True)
                        for a in (cm.get("attachments") or []):
                            try:
                                db.upsert_attachment(c, tid, cid, a)
                                total_attachments += 1
                            except Exception as e:
                                # One bad attachment shouldn't kill the whole batch.
                                errors += 1
                                print(f"  ⚠ #{tid} att={a.get('id')}: {type(e).__name__}: {e}",
                                      file=sys.stderr, flush=True)
                    seen_tickets += 1
                except KeyboardInterrupt:
                    raise
                except Exception as e:
                    errors += 1
                    print(f"  ⚠ #{tid}: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
                if i % 25 == 0:
                    _write_attachment_heartbeat("running",
                                                processed=i, total=total,
                                                attachments=total_attachments,
                                                errors=errors, current_ticket=tid)
                    print(f"  {i}/{total} tickets · {total_attachments} attachments · {errors} errors", flush=True)
                # Polite pause between tickets — covers the gap where the web
                # UI might want to write.
                if THROTTLE_SECONDS > 0:
                    _time.sleep(THROTTLE_SECONDS)
    except KeyboardInterrupt:
        _write_attachment_heartbeat("stopped",
                                    processed=seen_tickets, total=total,
                                    attachments=total_attachments, errors=errors)
        print(f"Interrupted at {seen_tickets}/{total}. Saved what we had.", flush=True)
        return {"tickets": seen_tickets, "attachments": total_attachments,
                "errors": errors, "stopped": True}
    _write_attachment_heartbeat("done",
                                processed=seen_tickets, total=total,
                                attachments=total_attachments, errors=errors)
    print(f"Done. {seen_tickets} tickets · {total_attachments} attachments · {errors} errors.", flush=True)
    return {"tickets": seen_tickets, "attachments": total_attachments, "errors": errors}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--once", action="store_true", help="Run a single sync pass and exit.")
    p.add_argument("--loop", action="store_true", help="Run continuously (every SYNC_INTERVAL_SECONDS).")
    p.add_argument("--backfill-attachments", type=int, nargs="?", const=0, default=None,
                   metavar="LIMIT",
                   help="Walk tickets and capture comment.attachments[] only. Optional ticket-count limit.")
    args = p.parse_args()
    if args.backfill_attachments is not None:
        backfill_attachments_from_zd(limit=args.backfill_attachments or None)
        return
    if args.loop:
        run_loop()
    else:
        run_once()


if __name__ == "__main__":
    main()
