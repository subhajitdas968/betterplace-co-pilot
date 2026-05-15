"""F13 · Nightly DB backup scheduler.

Background thread that fires `db.backup()` once a day at a configured hour.
Persists settings + last-run metadata in the `meta` key/value table so admins
can change behaviour without a code reload.

Design notes
------------
- Embedded as a uvicorn thread, NOT a separate subprocess. Same pattern as
  user_scheduler — one less thing to babysit.
- Idempotent: if the thread restarts mid-day, the "have we backed up today?"
  check stops us from doubling up.
- Cheap: the thread sleeps in 60s chunks, so it's essentially free at idle.
- The actual backup uses SQLite's online Backup API (db.backup()) which
  streams pages while the app keeps serving — no lock, no downtime.

Settings live in meta under these keys:
  backup_enabled      "1" / "0"           default "1"
  backup_hour         "0" through "23"    default "2"   (2am local)
  backup_keep_last_n  "7" / "14" / etc.   default "14"

Last-run state:
  backup_last_run_at  ISO timestamp
  backup_last_path    "/path/to/copilot-...db"
  backup_last_size    bytes (str)
  backup_last_error   error string (only if last attempt failed)
"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime
from pathlib import Path

from . import config, db


TICK_SECONDS = 60  # check once a minute; cheap


def _read_settings() -> dict:
    with db.conn() as c:
        enabled = db.get_meta(c, "backup_enabled")
        hour = db.get_meta(c, "backup_hour")
        keep = db.get_meta(c, "backup_keep_last_n")
    try:
        enabled_b = (enabled or "1") == "1"
    except (TypeError, AttributeError):
        enabled_b = True
    try:
        hour_i = int(hour) if hour is not None else 2
        if not 0 <= hour_i <= 23:
            hour_i = 2
    except (TypeError, ValueError):
        hour_i = 2
    try:
        keep_i = int(keep) if keep is not None else 14
        if keep_i < 1:
            keep_i = 14
    except (TypeError, ValueError):
        keep_i = 14
    return {"enabled": enabled_b, "hour": hour_i, "keep_last_n": keep_i}


def set_settings(enabled: bool | None = None, hour: int | None = None,
                 keep_last_n: int | None = None) -> dict:
    """Update one or more settings. None means leave as-is."""
    with db.conn() as c:
        if enabled is not None:
            db.set_meta(c, "backup_enabled", "1" if enabled else "0")
        if hour is not None:
            db.set_meta(c, "backup_hour", str(max(0, min(23, int(hour)))))
        if keep_last_n is not None:
            db.set_meta(c, "backup_keep_last_n", str(max(1, int(keep_last_n))))
    return _read_settings()


def last_run() -> dict:
    """Read the latest run summary the UI shows."""
    with db.conn() as c:
        at = db.get_meta(c, "backup_last_run_at")
        path = db.get_meta(c, "backup_last_path")
        size = db.get_meta(c, "backup_last_size")
        err = db.get_meta(c, "backup_last_error")
    try:
        size_i = int(size) if size else None
    except (TypeError, ValueError):
        size_i = None
    age_seconds = None
    if at:
        try:
            t0 = datetime.fromisoformat(at.replace("Z", "+00:00"))
            age_seconds = int((datetime.now(t0.tzinfo) - t0).total_seconds())
        except (ValueError, TypeError):
            pass
    return {"at": at, "path": path, "size_bytes": size_i,
            "age_seconds": age_seconds, "error": err}


def list_backups() -> list[dict]:
    """All .db files in data/backups/, newest first."""
    backups_dir = config.DATA_DIR / "backups"
    if not backups_dir.exists():
        return []
    out: list[dict] = []
    for p in sorted(backups_dir.glob("copilot-*.db"), reverse=True):
        try:
            st = p.stat()
            out.append({
                "name": p.name,
                "path": str(p),
                "size_bytes": st.st_size,
                "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
            })
        except OSError:
            continue
    return out


def run_backup_now() -> dict:
    """One-off backup. Used by the 'Backup now' button and by the scheduled
    daily run. Stamps meta on success or error."""
    settings = _read_settings()
    try:
        path = db.backup(keep_last_n=settings["keep_last_n"])
        size = path.stat().st_size if path.exists() else 0
        with db.conn() as c:
            db.set_meta(c, "backup_last_run_at", db.now_iso())
            db.set_meta(c, "backup_last_path", str(path))
            db.set_meta(c, "backup_last_size", str(size))
            # Clear stale error
            db.set_meta(c, "backup_last_error", "")
        return {"ok": True, "path": str(path), "size_bytes": size}
    except Exception as e:
        with db.conn() as c:
            db.set_meta(c, "backup_last_error", str(e)[:500])
            db.set_meta(c, "backup_last_run_at", db.now_iso())
        return {"ok": False, "error": str(e)}


# ---- Thread / tick -------------------------------------------------------

_THREAD: threading.Thread | None = None
_STOP_EVENT = threading.Event()
_LAST_TICK: dict = {}


def _should_run_now(settings: dict) -> bool:
    """Run when: enabled, current local hour == configured hour, AND we
    haven't already run today."""
    if not settings["enabled"]:
        return False
    now = datetime.now()
    if now.hour != settings["hour"]:
        return False
    info = last_run()
    if not info["at"]:
        return True
    try:
        prev = datetime.fromisoformat(info["at"].replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return True
    # If the last run was on a different calendar day OR more than 23 hours
    # ago, run again. Comparing local dates avoids the edge case where the
    # configured hour straddles midnight.
    return prev.date() != now.date()


def _tick() -> dict:
    settings = _read_settings()
    out = {"at": db.now_iso(), "enabled": settings["enabled"],
           "hour": settings["hour"], "ran": False}
    if _should_run_now(settings):
        result = run_backup_now()
        out["ran"] = True
        out["result"] = result
    return out


def _loop():
    while not _STOP_EVENT.is_set():
        try:
            globals()["_LAST_TICK"] = _tick()
        except Exception as e:
            globals()["_LAST_TICK"] = {"at": db.now_iso(), "error": str(e)}
            print(f"[backup_scheduler] tick fatal: {e}")
        for _ in range(TICK_SECONDS):
            if _STOP_EVENT.is_set():
                return
            time.sleep(1)


def start() -> bool:
    """Idempotent thread starter — same pattern as user_scheduler."""
    global _THREAD
    if _THREAD and _THREAD.is_alive():
        return False
    _STOP_EVENT.clear()
    _THREAD = threading.Thread(target=_loop, daemon=True, name="backup-scheduler")
    _THREAD.start()
    print("[backup_scheduler] thread started")
    return True


def stop() -> None:
    _STOP_EVENT.set()


def status() -> dict:
    return {
        "running": bool(_THREAD and _THREAD.is_alive()),
        "settings": _read_settings(),
        "last_run": last_run(),
        "last_tick": _LAST_TICK,
        "backups_dir": str(config.DATA_DIR / "backups"),
    }
