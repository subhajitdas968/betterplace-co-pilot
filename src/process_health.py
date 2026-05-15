"""F12+ · Unified process-health snapshot for /admin/status.

The app has half a dozen long-running things — uvicorn itself, two background
threads (user_scheduler, backup_scheduler, tunnel_watchdog), one tunnel
subprocess (cloudflared), and three managed worker subprocesses (ZD sync,
AI worker, attachments backfill, bulk re-analyze). Each follows its own
historical convention for pid/heartbeat storage. This module consolidates
all that into one structure the status template can iterate over.

Every entry returned by `all_workers()` has the same shape:

  {
    "key":       short id (e.g. "ai_worker"),
    "title":     human label,
    "state":     "ok" | "idle" | "warn" | "err",
    "summary":   one-line current state ("running 3m" / "stopped" / etc.),
    "pid":       int | None,
    "heartbeat_age_seconds": int | None,
    "last_action_at": ISO timestamp | None,
    "detail":    dict of worker-specific extras,
    "manage_url": admin page URL (so the UI links straight to it),
  }
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from . import config, db


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (ProcessLookupError, PermissionError, OSError, ValueError, TypeError):
        return False


def _age_seconds(iso_ts: str | None) -> int | None:
    if not iso_ts:
        return None
    try:
        t = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        now = datetime.now(t.tzinfo) if t.tzinfo else datetime.now()
        return int((now - t).total_seconds())
    except (ValueError, TypeError):
        return None


def _read_json_heartbeat(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


# ---------------------------------------------------------------------------
# Per-worker readers. Each returns the unified dict described in the module
# docstring, so the template doesn't need a special case per worker.
# ---------------------------------------------------------------------------

def web_app_status(process_started_at: float | None = None) -> dict:
    """We're literally rendering this, so the web app is alive by definition.
    The interesting bit is uptime."""
    import time
    pid = os.getpid()
    age = int(time.time() - process_started_at) if process_started_at else None
    return {
        "key": "web",
        "title": "Web app (uvicorn)",
        "state": "ok",
        "summary": "serving · this process",
        "pid": pid,
        "heartbeat_age_seconds": 0,
        "last_action_at": None,
        "detail": {"uptime_sec": age},
        "manage_url": None,
    }


def sync_worker_status() -> dict:
    """ZD sync worker — now manageable from /admin/sync. Lives as a
    detached subprocess with:
      - pid in meta.sync_worker_pid
      - heartbeat in data/sync_worker.heartbeat (state + ts each tick)
      - last_sync_run_at bumped on every successful pass

    For backward compat we still tolerate the worker being run from a
    terminal (no pid stored). In that case we fall back to the
    last_sync_run_at staleness check."""
    interval = getattr(config, "SYNC_INTERVAL_SECONDS", 300)
    stale_after = interval * 3

    with db.conn() as c:
        pid_str = db.get_meta(c, "sync_worker_pid")
        last_at = db.get_meta(c, "last_sync_run_at")
    try:
        pid = int(pid_str) if pid_str else None
    except (TypeError, ValueError):
        pid = None
    alive = _pid_alive(pid)

    hb = _read_json_heartbeat(config.DATA_DIR / "sync_worker.heartbeat") or {}
    hb_age = _age_seconds(hb.get("ts"))
    sync_age = _age_seconds(last_at)

    # Managed (pid known + alive) → trust the pid.
    if alive:
        hb_state = hb.get("state") or "running"
        bits = [hb_state]
        if sync_age is not None: bits.append(f"last sync {_human(sync_age)} ago")
        bits.append(f"every {interval}s")
        return {
            "key": "sync_worker",
            "title": "Zendesk sync",
            "state": "ok",
            "summary": " · ".join(bits),
            "pid": pid,
            "heartbeat_age_seconds": hb_age,
            "last_action_at": last_at,
            "detail": {"interval_sec": interval, "hb_state": hb_state,
                       "last_processed": hb.get("last_processed")},
            "manage_url": "/admin/sync",
        }

    # No managed pid (or pid dead) — fall back to staleness inference.
    if sync_age is None:
        return {
            "key": "sync_worker",
            "title": "Zendesk sync",
            "state": "warn",
            "summary": "never run · start it from /admin/sync",
            "pid": None,
            "heartbeat_age_seconds": None,
            "last_action_at": None,
            "detail": {"interval_sec": interval,
                       "hint": "Click 'manage' to start the sync loop."},
            "manage_url": "/admin/sync",
        }
    if sync_age > stale_after:
        return {
            "key": "sync_worker",
            "title": "Zendesk sync",
            "state": "err",
            "summary": f"stopped · last ran {_human(sync_age)} ago",
            "pid": None,
            "heartbeat_age_seconds": hb_age,
            "last_action_at": last_at,
            "detail": {"interval_sec": interval,
                       "expected_within_sec": stale_after,
                       "hint": "Process is stopped. Start it from /admin/sync."},
            "manage_url": "/admin/sync",
        }
    # Recent activity but no managed pid → someone is running it in a
    # terminal (legacy). Mark as ok but flag the source.
    return {
        "key": "sync_worker",
        "title": "Zendesk sync",
        "state": "ok",
        "summary": f"last ran {_human(sync_age)} ago · (running outside UI)",
        "pid": None,
        "heartbeat_age_seconds": hb_age,
        "last_action_at": last_at,
        "detail": {"interval_sec": interval,
                   "hint": "Running from a terminal, not the UI. Stop the terminal loop and start it from /admin/sync to manage from here."},
        "manage_url": "/admin/sync",
    }


def ai_worker_status() -> dict:
    """The AI worker writes data/ai_worker.heartbeat on every loop transition
    and stores its pid in ai_worker_config.process_pid. We surface both."""
    hb_path = config.DATA_DIR / "ai_worker.heartbeat"
    hb = _read_json_heartbeat(hb_path)
    with db.conn() as c:
        cfg = db.get_ai_worker_config(c) if hasattr(db, "get_ai_worker_config") else {}
    pid = (cfg or {}).get("process_pid")
    alive = _pid_alive(pid)
    age = _age_seconds(hb.get("ts")) if hb else None
    state = "ok" if alive else ("warn" if (cfg or {}).get("enabled") else "idle")
    if alive:
        hb_state = (hb or {}).get("state") or "running"
        summary = f"{hb_state} · last beat {_human(age) if age is not None else '—'} ago"
    elif (cfg or {}).get("enabled"):
        summary = "enabled but process not alive"
    else:
        summary = "stopped"
    return {
        "key": "ai_worker",
        "title": "AI worker (Claude analysis)",
        "state": state,
        "summary": summary,
        "pid": pid if alive else None,
        "heartbeat_age_seconds": age,
        "last_action_at": (hb or {}).get("ts"),
        "detail": {
            "model": (cfg or {}).get("model"),
            "daily_cap": (cfg or {}).get("daily_ticket_cap"),
            "batch_size": (cfg or {}).get("batch_size"),
            "hb_state": (hb or {}).get("state"),
            "last_processed": (hb or {}).get("last_processed"),
        },
        "manage_url": "/admin/ai-worker",
    }


def reanalyze_worker_status() -> dict:
    """Bulk re-analyze: data/reanalyze.heartbeat + meta.reanalyze_worker_pid.
    This one is by design transient — it only runs when an admin triggers
    a bulk pass. So "stopped" is the resting state, not an error."""
    hb = _read_json_heartbeat(config.DATA_DIR / "reanalyze.heartbeat")
    with db.conn() as c:
        pid_str = db.get_meta(c, "reanalyze_worker_pid")
    try:
        pid = int(pid_str) if pid_str else None
    except (TypeError, ValueError):
        pid = None
    alive = _pid_alive(pid)
    age = _age_seconds((hb or {}).get("ts"))
    if alive:
        processed = (hb or {}).get("processed", 0)
        total = (hb or {}).get("total", 0)
        cur = (hb or {}).get("current_tid")
        state = "ok"
        bits = [f"{processed}/{total}"]
        if cur: bits.append(f"on #{cur}")
        summary = "running · " + " · ".join(bits)
    else:
        state = "idle"
        last_state = (hb or {}).get("state")
        if last_state == "done":
            summary = "idle · last run completed " + (_human(age) + " ago" if age is not None else "—")
        elif last_state == "error":
            summary = "idle · last run errored"
            state = "warn"
        elif hb:
            summary = f"idle · last status {last_state}"
        else:
            summary = "idle · never run"
    return {
        "key": "reanalyze",
        "title": "Bulk AI re-analyze",
        "state": state,
        "summary": summary,
        "pid": pid if alive else None,
        "heartbeat_age_seconds": age,
        "last_action_at": (hb or {}).get("ts"),
        "detail": {
            "processed": (hb or {}).get("processed"),
            "total": (hb or {}).get("total"),
            "errors": (hb or {}).get("errors"),
            "current_tid": (hb or {}).get("current_tid"),
        },
        "manage_url": "/admin/ai-worker",
    }


def attachments_backfill_status() -> dict:
    """Attachments backfill: data/attachment_backfill.heartbeat +
    meta.attachment_backfill_pid. Transient like re-analyze."""
    hb = _read_json_heartbeat(config.DATA_DIR / "attachment_backfill.heartbeat")
    with db.conn() as c:
        pid_str = db.get_meta(c, "attachment_backfill_pid")
    try:
        pid = int(pid_str) if pid_str else None
    except (TypeError, ValueError):
        pid = None
    alive = _pid_alive(pid)
    age = _age_seconds((hb or {}).get("ts"))
    if alive:
        processed = (hb or {}).get("processed", 0)
        total = (hb or {}).get("total", 0)
        state = "ok"
        summary = f"running · {processed}/{total}"
    else:
        state = "idle"
        last_state = (hb or {}).get("state")
        if last_state == "done":
            summary = "idle · last run completed"
        elif last_state == "error":
            summary = "idle · last run errored"
            state = "warn"
        elif hb:
            summary = f"idle · last status {last_state}"
        else:
            summary = "idle · never run"
    return {
        "key": "attachments_backfill",
        "title": "Attachments backfill",
        "state": state,
        "summary": summary,
        "pid": pid if alive else None,
        "heartbeat_age_seconds": age,
        "last_action_at": (hb or {}).get("ts"),
        "detail": {
            "processed": (hb or {}).get("processed"),
            "total": (hb or {}).get("total"),
            "errors": (hb or {}).get("errors"),
        },
        "manage_url": "/admin/attachments",
    }


def cloudflared_quick_status() -> dict:
    """Cloudflared Quick Tunnel — heartbeat path + state from web.app helpers.
    We re-read the pid file directly to avoid a circular import."""
    pid_file = config.DATA_DIR / "tunnel.pid"
    hb = _read_json_heartbeat(config.DATA_DIR / "tunnel.heartbeat") or {}
    pid = None
    if pid_file.exists():
        try: pid = int(pid_file.read_text().strip())
        except (OSError, ValueError): pid = None
    alive = _pid_alive(pid)
    age = _age_seconds(hb.get("started_at"))
    if alive:
        state = "ok"
        url = hb.get("public_url") or "(URL pending…)"
        summary = f"running · {url}"
    else:
        state = "idle"
        summary = "stopped"
    return {
        "key": "cloudflared_quick",
        "title": "Cloudflared (Quick Tunnel)",
        "state": state,
        "summary": summary,
        "pid": pid if alive else None,
        "heartbeat_age_seconds": age,
        "last_action_at": hb.get("started_at"),
        "detail": {"public_url": hb.get("public_url")},
        "manage_url": "/admin/tunnel",
    }


# ---------------------------------------------------------------------------
# Roll-up
# ---------------------------------------------------------------------------

def all_workers(process_started_at: float | None = None) -> list[dict]:
    """Return every worker in display order. Cheap — each call is just file
    stat()s + a couple of meta-table lookups."""
    out: list[dict] = []
    funcs = [
        lambda: web_app_status(process_started_at),
        sync_worker_status,
        ai_worker_status,
        cloudflared_quick_status,
        reanalyze_worker_status,
        attachments_backfill_status,
    ]
    for fn in funcs:
        try:
            out.append(fn())
        except Exception as e:
            out.append({
                "key": getattr(fn, "__name__", "unknown"),
                "title": "unknown",
                "state": "err",
                "summary": f"status read failed: {e}",
                "pid": None,
                "heartbeat_age_seconds": None,
                "last_action_at": None,
                "detail": {},
                "manage_url": None,
            })
    return out


def _human(seconds: int | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"
