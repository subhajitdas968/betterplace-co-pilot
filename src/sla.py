"""SLA engine — Zendesk-replica.

A ticket gets matched to one SLA policy based on its priority + group + customer.
For each clock the policy defines (first_reply, next_reply, resolution), we
compute elapsed minutes (either business or calendar) and compare against the
target to produce a state: 'ok' / 'warn' / 'breached' / 'met'.

Warn threshold = 80% of target. Met means the clock has stopped (e.g. first
reply already happened, ticket already solved).

Business-hours math: weekly_intervals is a list of {day:0..6, start, end} where
day 0=Sunday. We project the elapsed wall-clock onto the business calendar of
the schedule's timezone, skipping holidays.
"""
from __future__ import annotations
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import db


WARN_THRESHOLD = 0.8


# ---- Loaders ---------------------------------------------------------------

def list_business_hours(c: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in c.execute(
        "SELECT * FROM business_hours ORDER BY is_default DESC, id"
    ).fetchall()]


def get_business_hours(c: sqlite3.Connection, bh_id: int) -> dict | None:
    row = c.execute("SELECT * FROM business_hours WHERE id=?", (bh_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["weekly_intervals"] = json.loads(d.get("weekly_intervals") or "[]")
    d["holidays"] = json.loads(d.get("holidays") or "[]")
    return d


def default_business_hours(c: sqlite3.Connection) -> dict | None:
    row = c.execute("SELECT * FROM business_hours WHERE is_default=1 LIMIT 1").fetchone()
    if not row:
        return None
    return get_business_hours(c, row["id"])


def upsert_business_hours(c: sqlite3.Connection, *, bh_id: int | None,
                          name: str, description: str, timezone: str,
                          weekly_intervals: list[dict], holidays: list[str],
                          is_default: bool, actor_email: str) -> int:
    now = db.now_iso()
    if is_default:
        # Only one default — clear the others.
        c.execute("UPDATE business_hours SET is_default=0 WHERE is_default=1")
    payload = (name, description, timezone, json.dumps(weekly_intervals),
               json.dumps(holidays), 1 if is_default else 0)
    if bh_id:
        c.execute("""
            UPDATE business_hours SET name=?, description=?, timezone=?,
                weekly_intervals=?, holidays=?, is_default=?, updated_at=?
            WHERE id=?
        """, payload + (now, bh_id))
        return bh_id
    c.execute("""
        INSERT INTO business_hours (name, description, timezone, weekly_intervals,
            holidays, is_default, created_by, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, payload + (actor_email, now, now))
    return c.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]


def list_sla_policies(c: sqlite3.Connection, *, active_only: bool = False) -> list[dict]:
    sql = "SELECT * FROM sla_policies"
    if active_only:
        sql += " WHERE active=1"
    sql += " ORDER BY position, id"
    return [dict(r) for r in c.execute(sql).fetchall()]


def get_sla_policy(c: sqlite3.Connection, policy_id: int) -> dict | None:
    row = c.execute("SELECT * FROM sla_policies WHERE id=?", (policy_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["applies_to"] = json.loads(d.get("applies_to") or "{}")
    d["targets"] = json.loads(d.get("targets") or "{}")
    return d


def upsert_sla_policy(c: sqlite3.Connection, *, policy_id: int | None,
                      name: str, description: str, active: bool,
                      applies_to: dict, targets: dict,
                      clock_type: str, business_hours_id: int | None,
                      position: int, actor_email: str) -> int:
    now = db.now_iso()
    payload = (name, description, 1 if active else 0,
               json.dumps(applies_to), json.dumps(targets),
               clock_type, business_hours_id, position)
    if policy_id:
        c.execute("""
            UPDATE sla_policies SET name=?, description=?, active=?, applies_to=?,
                targets=?, clock_type=?, business_hours_id=?, position=?, updated_at=?
            WHERE id=?
        """, payload + (now, policy_id))
        return policy_id
    c.execute("""
        INSERT INTO sla_policies (name, description, active, applies_to, targets,
            clock_type, business_hours_id, position, created_by, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, payload + (actor_email, now, now))
    return c.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]


# ---- Policy resolution -----------------------------------------------------

def resolve_policy_for_ticket(c: sqlite3.Connection, ticket: dict) -> dict | None:
    """Walk active SLA policies in position order. The first whose applies_to
    matches is the winner. applies_to keys are AND'd; values within a key are OR'd."""
    priority = (ticket.get("priority") or "").lower()
    group_id = ticket.get("group_id")
    customer_value = ticket.get("customer_value")    # caller resolves this
    for p in list_sla_policies(c, active_only=True):
        applies = json.loads(p.get("applies_to") or "{}")
        if applies.get("priority") and priority not in [x.lower() for x in applies["priority"]]:
            continue
        if applies.get("group_ids") and group_id not in [int(x) for x in applies["group_ids"]]:
            continue
        if applies.get("customer_values") and customer_value not in applies["customer_values"]:
            continue
        # Hydrate
        out = dict(p)
        out["applies_to"] = applies
        out["targets"] = json.loads(p.get("targets") or "{}")
        return out
    return None


# ---- Business-hours math ---------------------------------------------------

def _intervals_to_minutes_per_day(weekly_intervals: list[dict]) -> dict[int, list[tuple[int, int]]]:
    """Convert weekly_intervals to {day_of_week: [(start_minute, end_minute), ...]}.
       day_of_week is Python's Monday=0..Sunday=6 (Python convention)."""
    # Input uses 0=Sunday..6=Saturday (Zendesk convention). Convert.
    out: dict[int, list[tuple[int, int]]] = {}
    for w in weekly_intervals:
        try:
            zd_day = int(w.get("day"))
            # zd_day: Sun=0, Mon=1, ..., Sat=6  →  py_day: Mon=0,...,Sun=6
            py_day = (zd_day - 1) % 7
            sh, sm = w["start"].split(":"); eh, em = w["end"].split(":")
            start = int(sh) * 60 + int(sm)
            end = int(eh) * 60 + int(em)
            if end > start:
                out.setdefault(py_day, []).append((start, end))
        except Exception:
            continue
    for k in out:
        out[k].sort()
    return out


def business_minutes_between(start: datetime, end: datetime, bh: dict) -> int:
    """Return business minutes elapsed between two UTC datetimes against the
    schedule's weekly intervals and holidays."""
    if end <= start:
        return 0
    tz = ZoneInfo(bh.get("timezone") or "Asia/Kolkata")
    start_local = start.astimezone(tz)
    end_local = end.astimezone(tz)
    intervals = _intervals_to_minutes_per_day(bh.get("weekly_intervals") or [])
    holidays = set(bh.get("holidays") or [])
    if not intervals:
        return int((end_local - start_local).total_seconds() // 60)
    total = 0
    # Walk day by day. For each day, intersect the day's intervals with the
    # remaining window.
    cur_day = start_local.date()
    last_day = end_local.date()
    while cur_day <= last_day:
        if cur_day.isoformat() not in holidays:
            for s_min, e_min in intervals.get(cur_day.weekday(), []):
                day_start = datetime.combine(cur_day, datetime.min.time(), tzinfo=tz) + timedelta(minutes=s_min)
                day_end   = datetime.combine(cur_day, datetime.min.time(), tzinfo=tz) + timedelta(minutes=e_min)
                lo = max(day_start, start_local)
                hi = min(day_end,   end_local)
                if hi > lo:
                    total += int((hi - lo).total_seconds() // 60)
        cur_day += timedelta(days=1)
    return total


def calendar_minutes_between(start: datetime, end: datetime) -> int:
    if end <= start:
        return 0
    return int((end - start).total_seconds() // 60)


# ---- Per-clock computation ------------------------------------------------

def _clock_state(elapsed: int, target: int, stopped: bool) -> str:
    if stopped:
        return "met"
    if elapsed >= target:
        return "breached"
    if elapsed >= int(target * WARN_THRESHOLD):
        return "warn"
    return "ok"


def compute_ticket_sla(c: sqlite3.Connection, ticket_id: int) -> dict | None:
    """Compute all clocks for a ticket and persist to ticket_sla. Returns the
    computed dict for inline display."""
    t = c.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()
    if not t:
        return None
    # Resolve customer value
    try:
        cfs = json.loads(t["custom_fields"] or "{}")
    except Exception:
        cfs = {}
    cust = cfs.get("15315331275025")
    ticket_view = {
        "priority": t["priority"], "group_id": t["group_id"],
        "customer_value": cust,
    }
    policy = resolve_policy_for_ticket(c, ticket_view)
    if not policy:
        return None
    # Customer override on business hours, then policy's, then default.
    bh = None
    if cust:
        ov = c.execute("SELECT business_hours_id FROM customer_business_hours WHERE customer_value=?",
                       (cust,)).fetchone()
        if ov:
            bh = get_business_hours(c, ov["business_hours_id"])
    if bh is None and policy.get("business_hours_id"):
        bh = get_business_hours(c, policy["business_hours_id"])
    if bh is None:
        bh = default_business_hours(c)
    use_business = (policy.get("clock_type") == "business") and bh
    # Helpers
    def _parse(ts):
        if not ts: return None
        try: return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except Exception: return None
    created = _parse(t["created_at"]) or datetime.now(timezone.utc)
    solved  = _parse(t["solved_at"])
    now     = datetime.now(timezone.utc)
    # First agent reply timestamp
    fr_row = c.execute("""
        SELECT MIN(tc.created_at) AS first_reply
        FROM ticket_comments tc JOIN users u ON u.id=tc.author_id
        WHERE tc.ticket_id=? AND u.role IN ('agent','admin')
    """, (ticket_id,)).fetchone()
    first_reply = _parse(fr_row["first_reply"]) if fr_row else None
    # Last customer reply after the most recent agent reply — for next_reply clock
    nr_row = c.execute("""
        SELECT MAX(tc.created_at) AS last_cust
        FROM ticket_comments tc LEFT JOIN users u ON u.id=tc.author_id
        WHERE tc.ticket_id=? AND COALESCE(u.role,'end-user') NOT IN ('agent','admin')
    """, (ticket_id,)).fetchone()
    last_customer = _parse(nr_row["last_cust"]) if nr_row else None
    last_agent_row = c.execute("""
        SELECT MAX(tc.created_at) AS last_agent
        FROM ticket_comments tc JOIN users u ON u.id=tc.author_id
        WHERE tc.ticket_id=? AND u.role IN ('agent','admin')
    """, (ticket_id,)).fetchone()
    last_agent = _parse(last_agent_row["last_agent"]) if last_agent_row else None

    def minutes(a, b):
        if not a or not b: return 0
        return business_minutes_between(a, b, bh) if use_business else calendar_minutes_between(a, b)

    targets = policy.get("targets") or {}

    # First reply clock: created → first_reply, or → now if no reply
    fr_target = (targets.get("first_reply") or {}).get("minutes")
    fr_elapsed = minutes(created, first_reply or now)
    fr_state = _clock_state(fr_elapsed, fr_target or 0, stopped=bool(first_reply)) if fr_target else None

    # Next reply clock: only ticks when last_customer > last_agent (customer awaiting reply)
    nr_target = (targets.get("next_reply") or {}).get("minutes")
    nr_state = None; nr_elapsed = 0
    if nr_target:
        if last_customer and (not last_agent or last_customer > last_agent):
            nr_elapsed = minutes(last_customer, now)
            nr_state = _clock_state(nr_elapsed, nr_target, stopped=False)
        else:
            nr_elapsed = 0
            nr_state = "met"

    # Resolution clock: created → solved, or → now
    res_target = (targets.get("resolution") or {}).get("minutes")
    res_elapsed = minutes(created, solved or now)
    res_state = _clock_state(res_elapsed, res_target or 0, stopped=bool(solved)) if res_target else None

    snap = {
        "ticket_id": ticket_id, "policy_id": policy["id"],
        "first_reply_target_minutes": fr_target, "first_reply_elapsed_minutes": fr_elapsed, "first_reply_state": fr_state,
        "next_reply_target_minutes":  nr_target, "next_reply_elapsed_minutes":  nr_elapsed, "next_reply_state":  nr_state,
        "resolution_target_minutes":  res_target, "resolution_elapsed_minutes":  res_elapsed, "resolution_state":  res_state,
        "updated_at": db.now_iso(), "clock_type": policy.get("clock_type"),
        "business_hours_name": bh.get("name") if bh else None,
        "policy_name": policy.get("name"),
    }
    c.execute("""
        INSERT INTO ticket_sla (ticket_id, policy_id, first_reply_target_minutes,
            first_reply_elapsed_minutes, first_reply_state, next_reply_target_minutes,
            next_reply_elapsed_minutes, next_reply_state, resolution_target_minutes,
            resolution_elapsed_minutes, resolution_state, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticket_id) DO UPDATE SET
            policy_id=excluded.policy_id,
            first_reply_target_minutes=excluded.first_reply_target_minutes,
            first_reply_elapsed_minutes=excluded.first_reply_elapsed_minutes,
            first_reply_state=excluded.first_reply_state,
            next_reply_target_minutes=excluded.next_reply_target_minutes,
            next_reply_elapsed_minutes=excluded.next_reply_elapsed_minutes,
            next_reply_state=excluded.next_reply_state,
            resolution_target_minutes=excluded.resolution_target_minutes,
            resolution_elapsed_minutes=excluded.resolution_elapsed_minutes,
            resolution_state=excluded.resolution_state,
            updated_at=excluded.updated_at
    """, (snap["ticket_id"], snap["policy_id"],
          snap["first_reply_target_minutes"], snap["first_reply_elapsed_minutes"], snap["first_reply_state"],
          snap["next_reply_target_minutes"],  snap["next_reply_elapsed_minutes"],  snap["next_reply_state"],
          snap["resolution_target_minutes"],  snap["resolution_elapsed_minutes"],  snap["resolution_state"],
          snap["updated_at"]))
    return snap


def seed_default_business_hours(c: sqlite3.Connection) -> int | None:
    """Idempotent: install a sensible default schedule (Mon–Fri 9–18 IST) and a
    starter SLA policy (Normal priority: first reply 4h, resolution 24h, business)."""
    existing = c.execute("SELECT id FROM business_hours WHERE is_default=1 LIMIT 1").fetchone()
    if existing:
        return existing["id"]
    bh_id = upsert_business_hours(
        c, bh_id=None, name="BetterPlace default (Mon–Fri 9–18 IST)",
        description="Seeded default schedule. Edit at /admin/business-hours.",
        timezone="Asia/Kolkata",
        weekly_intervals=[
            {"day": d, "start": "09:00", "end": "18:00"} for d in (1, 2, 3, 4, 5)
        ],
        holidays=[],
        is_default=True, actor_email="system",
    )
    existing_pol = c.execute("SELECT id FROM sla_policies LIMIT 1").fetchone()
    if not existing_pol:
        upsert_sla_policy(
            c, policy_id=None, name="Standard — Normal priority",
            description="Seeded starter policy. Tune at /admin/sla.",
            active=True,
            applies_to={"priority": ["low", "normal"]},
            targets={
                "first_reply": {"minutes": 240},   # 4 business hours
                "next_reply":  {"minutes": 480},   # 8 business hours
                "resolution":  {"minutes": 1440},  # 24 business hours
            },
            clock_type="business", business_hours_id=bh_id,
            position=10, actor_email="system",
        )
        upsert_sla_policy(
            c, policy_id=None, name="Urgent / High priority",
            description="Seeded starter policy for high+urgent tickets.",
            active=True,
            applies_to={"priority": ["high", "urgent"]},
            targets={
                "first_reply": {"minutes": 60},
                "next_reply":  {"minutes": 120},
                "resolution":  {"minutes": 480},
            },
            clock_type="business", business_hours_id=bh_id,
            position=5, actor_email="system",
        )
    return bh_id
