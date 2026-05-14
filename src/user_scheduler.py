"""
Per-rule user automation scheduler.

Runs as a background thread inside uvicorn — NOT a separate subprocess. Each
rule of category='scheduler' has its own `interval_minutes` and `next_fire_at`.
The loop wakes every TICK_SECONDS, finds rules whose next_fire_at is past,
runs them against all active users, then re-schedules each.

This means each rule is its own unit of control:
  - The `active` flag pauses just that rule
  - The `interval_minutes` slider sets its own cadence
  - last_fired_at / fire_count / last_error are stored per-rule

There's no central "scheduler on/off" — if any active scheduler rule exists,
this thread is doing its job. The thread is started by app startup hook.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone

from . import db, user_rules_engine


# How often to wake up and check for due rules. Doesn't need to match any
# rule's interval — even a 1-minute rule will fire within TICK_SECONDS of
# becoming due.
TICK_SECONDS = 30


# ---- Per-user time-context evaluation (used by scheduler-only events) ----

def _local_now(tz_str: str | None) -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(tz_str or "Asia/Kolkata"))
    except Exception:
        return datetime.now(timezone.utc)


def _is_work_day_started(profile: dict) -> bool:
    """True iff right now is at or after the user's work_start_time today,
    today is a work day, and we haven't already marked work_day_started today.
    The "mark" lives in app_users.last_work_day_marked so it auto-resets the
    next day."""
    tz = profile.get("timezone") or "Asia/Kolkata"
    now_local = _local_now(tz)
    day_code = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"][now_local.weekday()]
    try:
        work_days = json.loads(profile.get("work_days_json") or "[]")
    except json.JSONDecodeError:
        return False
    if day_code not in work_days:
        return False
    try:
        sh, sm = (profile.get("work_start_time") or "09:00").split(":")
        start_local = now_local.replace(hour=int(sh), minute=int(sm),
                                          second=0, microsecond=0)
    except (ValueError, TypeError):
        return False
    if now_local < start_local:
        return False
    today_str = now_local.strftime("%Y-%m-%d")
    if (profile.get("last_work_day_marked") or "")[:10] == today_str:
        return False  # already fired today
    return True


def _is_idle_during_work(profile: dict) -> bool:
    """True when in work hours AND availability != 'online' AND not on leave AND
    not nudged in the last (effectively the rule's interval) minutes. We don't
    duplicate the throttle here — the per-rule interval already controls it."""
    if profile.get("availability") == "online":
        return False
    if db.is_user_on_leave(profile):
        return False
    tz = profile.get("timezone") or "Asia/Kolkata"
    now_local = _local_now(tz)
    day_code = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"][now_local.weekday()]
    try:
        work_days = json.loads(profile.get("work_days_json") or "[]")
    except json.JSONDecodeError:
        return False
    if day_code not in work_days:
        return False
    try:
        sh, sm = (profile.get("work_start_time") or "09:00").split(":")
        eh, em = (profile.get("work_end_time") or "18:00").split(":")
        start_local = now_local.replace(hour=int(sh), minute=int(sm),
                                          second=0, microsecond=0)
        end_local = now_local.replace(hour=int(eh), minute=int(em),
                                        second=0, microsecond=0)
        return start_local <= now_local <= end_local
    except (ValueError, TypeError):
        return False


# Map trigger_event → predicate(profile) that says "this user matches this
# scheduler-type rule right now". Add new entries here as you add new
# scheduler-fireable triggers in user_automations_catalog.
_TRIGGER_PREDICATES = {
    "user.work_day_started": _is_work_day_started,
    "user.idle_during_work": _is_idle_during_work,
    # Non-scheduler triggers (user.logged_in, user.availability_changed, etc.)
    # aren't here — they fire from request handlers via dispatch_event.
}


def _eligible_users_for(trigger: str) -> list[dict]:
    """For a scheduler-type trigger, find every active user whose state
    matches the trigger's predicate right now."""
    pred = _TRIGGER_PREDICATES.get(trigger)
    if pred is None:
        return []
    with db.conn() as c:
        emails = [r["email"] for r in c.execute(
            "SELECT email FROM app_users WHERE status='active'"
        ).fetchall()]
    matched: list[dict] = []
    for email in emails:
        with db.conn() as c:
            profile = db.get_user_profile(c, email)
        if not profile:
            continue
        try:
            if pred(profile):
                matched.append(profile)
        except Exception as e:
            print(f"[user_scheduler] predicate {trigger} for {email}: {e}")
    return matched


# ---- Main per-rule tick --------------------------------------------------

def _tick() -> dict:
    """Find every due scheduler rule. For each, find matching users, dispatch
    the trigger, then reschedule the rule by its interval. Return summary
    suitable for logging."""
    with db.conn() as c:
        due_rules = db.list_due_user_scheduler_rules(c)
    fired_rules = 0
    total_fires = 0
    errors = 0
    for rule in due_rules:
        rid = rule["id"]
        trigger = rule["trigger_event"]
        interval = max(1, int(rule.get("interval_minutes") or 5))
        try:
            users = _eligible_users_for(trigger)
            rule_fires = 0
            for profile in users:
                # dispatch_event re-evaluates conditions against this user,
                # so a rule whose predicate matched but whose conditions
                # don't will still be filtered.
                results = user_rules_engine.dispatch_event(
                    trigger, profile["email"],
                    context={"trigger": trigger,
                             "fired_by": "scheduler",
                             "rule_id": rid},
                )
                rule_fires += sum(1 for r in results if r.get("fired"))
                # For work_day_started: stamp last_work_day_marked so we
                # don't re-fire later today.
                if trigger == "user.work_day_started":
                    tz = profile.get("timezone") or "Asia/Kolkata"
                    today = _local_now(tz).strftime("%Y-%m-%d")
                    try:
                        with db.conn() as c:
                            c.execute("UPDATE app_users SET last_work_day_marked=? WHERE email=?",
                                       (today, profile["email"]))
                    except Exception as e:
                        print(f"[user_scheduler] mark wd_started fail: {e}")
            # Always reschedule, fired or not
            with db.conn() as c:
                db.record_user_automation_fire(c, rid, success=True,
                                                  schedule_next_in_minutes=interval)
            if rule_fires:
                fired_rules += 1
                total_fires += rule_fires
        except Exception as e:
            errors += 1
            print(f"[user_scheduler] rule {rid} error: {e}")
            try:
                with db.conn() as c:
                    db.record_user_automation_fire(
                        c, rid, success=False, error=str(e),
                        schedule_next_in_minutes=interval,
                    )
            except Exception:
                pass
    return {"due_rules": len(due_rules), "fired_rules": fired_rules,
            "total_fires": total_fires, "errors": errors}


# ---- Background thread orchestration -------------------------------------

_THREAD: threading.Thread | None = None
_STOP_EVENT = threading.Event()
_LAST_TICK: dict = {}


def _loop():
    while not _STOP_EVENT.is_set():
        try:
            t0 = time.perf_counter()
            summary = _tick()
            summary["elapsed_ms"] = int((time.perf_counter() - t0) * 1000)
            summary["at"] = db.now_iso()
            globals()["_LAST_TICK"] = summary
        except Exception as e:
            globals()["_LAST_TICK"] = {"at": db.now_iso(), "error": str(e)}
            print(f"[user_scheduler] tick fatal: {e}")
        # Sleep in 1s chunks so app shutdown is responsive
        for _ in range(TICK_SECONDS):
            if _STOP_EVENT.is_set():
                return
            time.sleep(1)


def start() -> bool:
    """Start the scheduler thread. Idempotent — calling repeatedly is safe."""
    global _THREAD
    if _THREAD and _THREAD.is_alive():
        return False
    _STOP_EVENT.clear()
    _THREAD = threading.Thread(target=_loop, daemon=True, name="user-scheduler")
    _THREAD.start()
    print(f"[user_scheduler] thread started")
    return True


def stop() -> None:
    _STOP_EVENT.set()


def status() -> dict:
    """Read state for the admin UI. Reports whether the thread is alive +
    last tick stats."""
    alive = bool(_THREAD and _THREAD.is_alive())
    return {"running": alive, "last_tick": _LAST_TICK}


def run_rule_now(automation_id: int) -> dict:
    """Manually fire one scheduler rule against currently-matching users.
    Used by the 'Run now' button in /admin/user-automations."""
    with db.conn() as c:
        rule_row = c.execute(
            "SELECT * FROM user_automations WHERE id=?", (automation_id,)
        ).fetchone()
    if not rule_row:
        return {"ok": False, "error": "no_such_rule"}
    rule = dict(rule_row)
    trigger = rule["trigger_event"]
    interval = max(1, int(rule.get("interval_minutes") or 5))
    try:
        users = _eligible_users_for(trigger)
        # If this trigger has no predicate (i.e. it's a request-driven event),
        # we still let "Run now" fire it against ALL active users so admins
        # can test rules like "user.availability_changed" easily.
        if trigger not in _TRIGGER_PREDICATES:
            with db.conn() as c:
                emails = [r["email"] for r in c.execute(
                    "SELECT email FROM app_users WHERE status='active'"
                ).fetchall()]
                users = []
                for email in emails:
                    p = db.get_user_profile(c, email)
                    if p:
                        users.append(p)
        fired = 0
        for profile in users:
            res = user_rules_engine.dispatch_event(
                trigger, profile["email"],
                context={"trigger": trigger, "fired_by": "manual_run",
                         "rule_id": automation_id},
            )
            fired += sum(1 for r in res if r.get("fired"))
        with db.conn() as c:
            db.record_user_automation_fire(c, automation_id, success=True,
                                             schedule_next_in_minutes=interval)
        return {"ok": True, "trigger": trigger, "users_checked": len(users),
                "rules_fired": fired}
    except Exception as e:
        with db.conn() as c:
            db.record_user_automation_fire(c, automation_id, success=False,
                                             error=str(e),
                                             schedule_next_in_minutes=interval)
        return {"ok": False, "error": str(e)}


# Back-compat CLI entry — if someone still has the old `python -m
# src.user_scheduler` command in their docs, just print a deprecation note
# and exit. The scheduler is now embedded in uvicorn.
def main() -> None:
    print("[user_scheduler] this module is now embedded in uvicorn as a "
          "background thread — no separate subprocess needed. "
          "Start uvicorn (make web) and the scheduler runs automatically.")


if __name__ == "__main__":
    main()
