"""Customer 360 — assemble the requester-centric view shown on ticket detail.

Given a ticket's requester_id, returns a structured dict the template renders
as a side panel: identity, organization(s), activity stats, recent tickets,
and lightweight AI pattern hints. Pure read-only — no schema dependencies
beyond the existing `users`, `organizations`, `tickets`, `ticket_insights`,
and (Phase 2) `end_user_profiles` / `end_user_organizations` tables.

We keep this read-side helper isolated from the write-side (auto-link logic
in the sync worker) so changes to one don't ripple into the other.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone


# How many recent tickets to display in the panel. Anything past this lives
# behind "see all" search by requester email.
RECENT_LIMIT = 10

# How far back to look for "AI pattern" insights. 90 days gives enough
# samples to spot a recurring issue without dragging in ancient history.
PATTERN_LOOKBACK_DAYS = 90

# Minimum count of one AI category in the lookback window before we surface
# it as a "pattern" hint. 3 is the sweet spot — 2 is noise, 4 is too rare
# to be useful.
PATTERN_MIN_COUNT = 3

# Zendesk custom field that holds the Simplesat CSAT rating. Empirically a
# 0-10 NPS-style scale (verified against live data — distribution skews to
# 9 and 10, with a long tail down to 0). Discovered via
# `SELECT id, title FROM ticket_fields WHERE title LIKE '%Simplesat%'`.
# If you ever switch CSAT providers, change these constants — they're the
# only touchpoints outside the helper that reads them.
SIMPLESAT_FIELD_ID = "18607065981329"
SIMPLESAT_SCALE_MAX = 10
# Bands (loosely aligned to NPS: promoter ≥9, passive 7-8, detractor ≤6).
SIMPLESAT_GOOD_THRESHOLD = 8.0
SIMPLESAT_OK_THRESHOLD = 6.0


def _row_to_dict(r) -> dict | None:
    return dict(r) if r is not None else None


def _safe_json(s, fallback):
    if not s:
        return fallback
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return fallback


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _human_age(seconds: int | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    if seconds < 86400 * 30:
        return f"{seconds // 86400}d"
    if seconds < 86400 * 365:
        return f"{seconds // (86400 * 30)}mo"
    return f"{seconds // (86400 * 365)}y"


def for_requester(c: sqlite3.Connection, requester_id: int | None,
                   current_ticket_id: int | None = None) -> dict | None:
    """Assemble the full 360 view for one end-user. Returns None if the
    requester_id isn't known (so the template can fall back gracefully)."""
    if not requester_id:
        return None

    user_row = c.execute(
        "SELECT id, name, email, role, raw FROM users WHERE id=?", (requester_id,)
    ).fetchone()
    if not user_row:
        return None
    user = dict(user_row)
    user_raw = _safe_json(user.get("raw"), {})

    # ---- Identity (phone, time_zone, locale come from ZD raw) ----
    identity = {
        "id": user["id"],
        "name": user.get("name") or "(unknown)",
        "email": user.get("email"),
        "role": user.get("role") or "end-user",
        "phone": user_raw.get("phone"),
        "time_zone": user_raw.get("time_zone"),
        "locale": user_raw.get("locale"),
        "verified": user_raw.get("verified"),
        "shared": user_raw.get("shared"),
        "tags": user_raw.get("tags") or [],
        "created_at": user_raw.get("created_at"),
    }

    # ---- Organizations linked to this user ----
    # Two sources: the ZD-side organization_id on their raw record, plus the
    # native many-to-many (Phase 2 — table may not exist yet, tolerate that).
    orgs: list[dict] = []
    seen_org_ids: set[int] = set()

    primary_org_id = user_raw.get("organization_id")
    if primary_org_id:
        row = c.execute("SELECT id, name FROM organizations WHERE id=?",
                          (primary_org_id,)).fetchone()
        if row:
            orgs.append({"id": row["id"], "name": row["name"], "is_primary": True,
                          "source": "zendesk"})
            seen_org_ids.add(row["id"])
    try:
        link_rows = c.execute("""
            SELECT o.id, o.name, l.is_primary, l.source
              FROM end_user_organizations l
              JOIN organizations o ON o.id = l.organization_id
             WHERE l.user_id = ?
             ORDER BY l.is_primary DESC, o.name
        """, (requester_id,)).fetchall()
        for r in link_rows:
            if r["id"] in seen_org_ids:
                continue
            orgs.append({"id": r["id"], "name": r["name"],
                          "is_primary": bool(r["is_primary"]),
                          "source": r["source"] or "linked"})
            seen_org_ids.add(r["id"])
    except sqlite3.OperationalError:
        # end_user_organizations table doesn't exist yet (pre-Phase 2)
        pass

    # ---- Sidecar profile (Phase 2) — gracefully absent today ----
    profile: dict | None = None
    try:
        prow = c.execute(
            "SELECT * FROM end_user_profiles WHERE user_id=?", (requester_id,)
        ).fetchone()
        if prow:
            profile = dict(prow)
            profile["permissions"] = _safe_json(profile.get("permissions_json"), {})
            profile.pop("portal_password_hash", None)  # never leak in JSON
            profile.pop("portal_invite_token", None)   # ditto — admin-only field
    except sqlite3.OperationalError:
        profile = None

    # ---- Ticket stats ----
    # Total, open, last-contact, avg resolution (closed only).
    stats_row = c.execute("""
        SELECT
          COUNT(*) AS total,
          SUM(CASE WHEN status IN ('new','open','pending','hold') THEN 1 ELSE 0 END) AS open_n,
          MAX(updated_at) AS last_updated,
          MAX(created_at) AS last_created
        FROM tickets
        WHERE requester_id = ?
    """, (requester_id,)).fetchone()

    total = (stats_row["total"] or 0) if stats_row else 0
    open_n = (stats_row["open_n"] or 0) if stats_row else 0
    last_updated = stats_row["last_updated"] if stats_row else None
    last_created = stats_row["last_created"] if stats_row else None
    last_contact = last_updated or last_created
    last_contact_age_sec = None
    t = _parse_iso(last_contact)
    if t:
        now = datetime.now(t.tzinfo) if t.tzinfo else datetime.now(timezone.utc)
        last_contact_age_sec = int((now - t).total_seconds())

    # Avg full resolution (minutes) over closed tickets — pulled from
    # ticket_metrics.full_resolution_time_in_minutes if available.
    avg_resolution_min = None
    try:
        avg_row = c.execute("""
            SELECT AVG(m.full_resolution_time_in_minutes) AS avg_min
              FROM tickets t
              JOIN ticket_metrics m ON m.ticket_id = t.id
             WHERE t.requester_id = ?
               AND t.status IN ('solved','closed')
               AND m.full_resolution_time_in_minutes IS NOT NULL
        """, (requester_id,)).fetchone()
        if avg_row and avg_row["avg_min"]:
            avg_resolution_min = int(avg_row["avg_min"])
    except sqlite3.OperationalError:
        pass

    # CSAT — average Simplesat rating across this requester's tickets that
    # have a score. Empirical NPS-style 0-10 scale (one stray value of
    # 431641 in live data showed why filtering by sane range matters).
    csat_avg = None
    csat_n = None
    try:
        csat_row = c.execute(f"""
            SELECT AVG(CAST(json_extract(custom_fields, '$."{SIMPLESAT_FIELD_ID}"') AS REAL)) AS avg_rating,
                   COUNT(json_extract(custom_fields, '$."{SIMPLESAT_FIELD_ID}"')) AS n
              FROM tickets
             WHERE requester_id = ?
               AND json_extract(custom_fields, '$."{SIMPLESAT_FIELD_ID}"') IS NOT NULL
               AND json_extract(custom_fields, '$."{SIMPLESAT_FIELD_ID}"') != ''
               AND CAST(json_extract(custom_fields, '$."{SIMPLESAT_FIELD_ID}"') AS REAL) BETWEEN 0 AND {SIMPLESAT_SCALE_MAX}
        """, (requester_id,)).fetchone()
        if csat_row and csat_row["n"]:
            csat_avg = round(float(csat_row["avg_rating"]), 2)
            csat_n = csat_row["n"]
    except (sqlite3.OperationalError, ValueError, TypeError):
        pass

    stats = {
        "total": total,
        "open": open_n,
        "closed": max(total - open_n, 0),
        "last_contact_at": last_contact,
        "last_contact_age_sec": last_contact_age_sec,
        "last_contact_human": _human_age(last_contact_age_sec),
        "avg_resolution_min": avg_resolution_min,
        "avg_resolution_human": (
            f"{avg_resolution_min // 60}h" if avg_resolution_min and avg_resolution_min >= 60
            else (f"{avg_resolution_min}m" if avg_resolution_min else "—")
        ),
        "csat_avg": csat_avg,           # e.g. 8.43 or None — on 0..10 scale
        "csat_n": csat_n,                # number of ratings averaged
        "csat_scale_max": SIMPLESAT_SCALE_MAX,
        "csat_human": (
            f"{csat_avg:.2f} / {SIMPLESAT_SCALE_MAX}" if csat_avg is not None
            else "no ratings yet"
        ),
        # Visual cue band — useful for color-coding the chip in the UI.
        # NPS-style thresholds: ≥8 promoter, 6-7 passive, ≤5 detractor.
        "csat_band": (
            "good" if csat_avg is not None and csat_avg >= SIMPLESAT_GOOD_THRESHOLD
            else "ok" if csat_avg is not None and csat_avg >= SIMPLESAT_OK_THRESHOLD
            else "poor" if csat_avg is not None
            else None
        ),
    }

    # ---- Recent tickets — excluding the one we're on, oldest excluded if we hit limit ----
    recent_rows = c.execute("""
        SELECT id, local_id, subject, status, priority, created_at, updated_at, group_id
          FROM tickets
         WHERE requester_id = ?
           AND id != COALESCE(?, -1)
         ORDER BY COALESCE(updated_at, created_at) DESC
         LIMIT ?
    """, (requester_id, current_ticket_id, RECENT_LIMIT)).fetchall()
    recent = [_row_to_dict(r) for r in recent_rows]

    # ---- AI pattern detection (lightweight) ----
    pattern = None
    try:
        cat_rows = c.execute(f"""
            SELECT json_extract(i.payload_json, '$.category') AS category
              FROM ticket_insights i
              JOIN tickets t ON t.id = i.ticket_id
             WHERE t.requester_id = ?
               AND i.created_at >= datetime('now', '-{int(PATTERN_LOOKBACK_DAYS)} days')
        """, (requester_id,)).fetchall()
        cats = [r["category"] for r in cat_rows if r["category"]]
        if cats:
            counter = Counter(cats)
            top, top_n = counter.most_common(1)[0]
            if top_n >= PATTERN_MIN_COUNT:
                pattern = {
                    "category": top,
                    "count": top_n,
                    "window_days": PATTERN_LOOKBACK_DAYS,
                    "hint": (
                        f"{top_n} '{top}' tickets in last {PATTERN_LOOKBACK_DAYS} days — "
                        f"consider whether this is a recurring product issue worth escalating."
                    ),
                }
    except sqlite3.OperationalError:
        pattern = None

    # ---- Email domain (used by Phase 3 permission "can_see_domain") ----
    email_domain = None
    if identity["email"] and "@" in identity["email"]:
        email_domain = identity["email"].split("@", 1)[1].lower()

    return {
        "identity": identity,
        "email_domain": email_domain,
        "orgs": orgs,
        "primary_org": (orgs[0] if orgs else None),
        "stats": stats,
        "recent_tickets": recent,
        "pattern": pattern,
        "profile": profile,
        "has_portal_access": bool(profile and profile.get("portal_access_enabled")),
    }
