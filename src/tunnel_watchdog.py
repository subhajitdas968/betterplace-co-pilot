"""F11 · Quick Tunnel watchdog.

Background thread that polls the Quick Tunnel pid every 30s. If the process
is gone AND the watchdog is enabled, it restarts the tunnel. To stay sane in
the face of a binary that genuinely can't start, we use exponential backoff
and cap attempts at 5 within a 10-minute window — after that the watchdog
goes quiet until the operator hits "Reset attempts" (or 10 min elapses).

Settings (meta table):
  tunnel_watchdog_enabled   "1" / "0"   default "0"  (opt-in)

State (meta table):
  tunnel_watchdog_attempts  JSON array of recent attempt timestamps
                            (we keep ~last 10 — used to compute window count)
  tunnel_watchdog_last_at   ISO timestamp of most recent restart attempt
  tunnel_watchdog_last_ok   "1" / "0"

The actual start logic lives in src/web/app.py (_start_quick_tunnel_proc).
We import it lazily inside the loop so we don't create a circular import
at module load.

Recovery: if a real human stops the tunnel via the UI, the watchdog won't
fight them — the stop endpoint clears the heartbeat AND we expect the
operator to also flip the watchdog toggle off when they want it down.
That's a tiny UX gap but it keeps the behaviour predictable. (A nicer
future: have the stop endpoint also disable the watchdog automatically.)
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta

from . import config, db


TICK_SECONDS = 30
MAX_ATTEMPTS_PER_WINDOW = 5
WINDOW_MINUTES = 10
PID_FILE = config.DATA_DIR / "tunnel.pid"


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def _read_pid() -> int | None:
    if not PID_FILE.exists():
        return None
    try:
        return int(PID_FILE.read_text().strip())
    except (OSError, ValueError):
        return None


# ---- Settings ----

def _enabled() -> bool:
    with db.conn() as c:
        v = db.get_meta(c, "tunnel_watchdog_enabled")
    return (v or "0") == "1"


def set_enabled(enabled: bool) -> dict:
    with db.conn() as c:
        db.set_meta(c, "tunnel_watchdog_enabled", "1" if enabled else "0")
    return {"enabled": enabled}


# ---- Attempt log ----

def _load_attempts() -> list[str]:
    with db.conn() as c:
        raw = db.get_meta(c, "tunnel_watchdog_attempts")
    try:
        v = json.loads(raw) if raw else []
        return v if isinstance(v, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def _save_attempts(items: list[str]) -> None:
    keep = items[-10:]  # latest 10 only
    with db.conn() as c:
        db.set_meta(c, "tunnel_watchdog_attempts", json.dumps(keep))


def _recent_attempt_count() -> int:
    """How many attempts within the last WINDOW_MINUTES."""
    cutoff = datetime.now() - timedelta(minutes=WINDOW_MINUTES)
    n = 0
    for ts in _load_attempts():
        try:
            t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if t.tzinfo is not None:
                t = t.replace(tzinfo=None)
            if t >= cutoff:
                n += 1
        except (ValueError, TypeError):
            continue
    return n


def reset_attempts() -> dict:
    with db.conn() as c:
        db.set_meta(c, "tunnel_watchdog_attempts", "[]")
        db.set_meta(c, "tunnel_watchdog_last_ok", "")
    return {"reset": True}


# ---- One tick ----

def _tick() -> dict:
    if not _enabled():
        return {"checked": True, "enabled": False, "ran": False}
    pid = _read_pid()
    alive = _pid_alive(pid)
    if alive:
        return {"checked": True, "enabled": True, "pid": pid, "alive": True}
    # Tunnel is down — should we try to restart?
    recent = _recent_attempt_count()
    if recent >= MAX_ATTEMPTS_PER_WINDOW:
        return {"checked": True, "enabled": True, "alive": False,
                "ran": False, "reason": "backoff",
                "recent_attempts": recent,
                "window_minutes": WINDOW_MINUTES}
    # Lazy import to avoid web/app.py ↔ tunnel_watchdog cycle at module load
    from .web.app import _start_quick_tunnel_proc
    out = _start_quick_tunnel_proc(actor_email="system.watchdog")
    now_iso = db.now_iso()
    attempts = _load_attempts() + [now_iso]
    _save_attempts(attempts)
    with db.conn() as c:
        db.set_meta(c, "tunnel_watchdog_last_at", now_iso)
        db.set_meta(c, "tunnel_watchdog_last_ok", "1" if out.get("ok") else "0")
        db.log_access(c, actor_email="system.watchdog",
                      event_type="tunnel.watchdog.restart",
                      target_kind="system", target_id="",
                      detail={"ok": out.get("ok"), "pid": out.get("pid"),
                              "error": out.get("error")})
    return {"checked": True, "enabled": True, "alive": False,
            "ran": True, "restart_result": out,
            "recent_attempts": recent + 1}


# ---- Thread ----

_THREAD: threading.Thread | None = None
_STOP_EVENT = threading.Event()
_LAST_TICK: dict = {}


def _loop():
    while not _STOP_EVENT.is_set():
        try:
            globals()["_LAST_TICK"] = _tick()
        except Exception as e:
            globals()["_LAST_TICK"] = {"at": db.now_iso(), "error": str(e)}
            print(f"[tunnel_watchdog] tick fatal: {e}")
        for _ in range(TICK_SECONDS):
            if _STOP_EVENT.is_set():
                return
            time.sleep(1)


def start() -> bool:
    global _THREAD
    if _THREAD and _THREAD.is_alive():
        return False
    _STOP_EVENT.clear()
    _THREAD = threading.Thread(target=_loop, daemon=True, name="tunnel-watchdog")
    _THREAD.start()
    print("[tunnel_watchdog] thread started")
    return True


def stop() -> None:
    _STOP_EVENT.set()


def status() -> dict:
    """Read state for the admin UI."""
    with db.conn() as c:
        last_at = db.get_meta(c, "tunnel_watchdog_last_at")
        last_ok = db.get_meta(c, "tunnel_watchdog_last_ok")
    return {
        "thread_running": bool(_THREAD and _THREAD.is_alive()),
        "enabled": _enabled(),
        "recent_attempts": _recent_attempt_count(),
        "max_attempts_per_window": MAX_ATTEMPTS_PER_WINDOW,
        "window_minutes": WINDOW_MINUTES,
        "last_attempt_at": last_at,
        "last_attempt_ok": (last_ok == "1") if last_ok else None,
        "attempts": _load_attempts(),
        "last_tick": _LAST_TICK,
    }
