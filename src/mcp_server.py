"""BetterPlace Co-Pilot — MCP server.

Exposes our SQLite ticket repository to Claude Desktop / Claude Code / Cowork.
All AI insights for the web UI are produced by Claude through this server —
no metered Claude API calls. Insights written by Claude flow into the existing
ticket_insights table, so the web UI displays them with zero changes.

Run via stdio (Claude Desktop spawns this process):
    python scripts/run_mcp.py

Tools exposed:
  Read-only:
    search_tickets, get_ticket, get_conversation, find_similar_tickets,
    get_customer_summary, list_views, get_field_taxonomy, get_ai_insights
  Write (Claude Desktop will prompt for confirmation):
    save_ticket_insight, update_ticket_field, add_dropdown_option,
    record_ai_feedback

Prompts:
  analyze_ticket       — full structured analysis flow
  morning_digest       — what happened overnight + priorities
  summarize_conversation — concise summary of a single thread
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Ensure project root is on sys.path when launched directly
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from mcp.server.fastmcp import FastMCP  # noqa: E402

from src import db  # noqa: E402
from src import zendesk as _zd  # noqa: E402


# Field IDs we use frequently (matches src/web/app.py)
FID_CUSTOMER = "15315331275025"
FID_PRODUCT = "15316390522769"
FID_MODULE = "15316445624849"
FID_BUCKETIZATION = "35194939804689"
FID_RC1 = "15316740884753"
FID_RC2 = "15316876186897"
FID_JIRA_ID = "15316921871633"
FID_KB_ARTICLE = "15317743732625"


mcp = FastMCP("betterplace-copilot")


# ===== Helpers =====

def _row_to_dict(r: sqlite3.Row | None) -> dict | None:
    return dict(r) if r else None


def _customer_label(c: sqlite3.Connection, raw_value: str | None) -> str:
    if not raw_value:
        return ""
    f = c.execute("SELECT options FROM ticket_fields WHERE id=?", (int(FID_CUSTOMER),)).fetchone()
    if not f:
        return raw_value
    for o in json.loads(f["options"] or "[]"):
        if o.get("value") == raw_value:
            return o.get("name") or raw_value
    return raw_value


def _resolve_dropdown_label(c: sqlite3.Connection, field_id: int, raw: str | None) -> str:
    if not raw:
        return ""
    f = c.execute("SELECT options FROM ticket_fields WHERE id=?", (field_id,)).fetchone()
    if not f:
        return raw
    for o in json.loads(f["options"] or "[]"):
        if o.get("value") == raw:
            return o.get("name") or raw
    return raw


def _ticket_summary_dict(c: sqlite3.Connection, t: sqlite3.Row) -> dict:
    # Merge ZD-synced + local_overrides for the customer lookup
    cfs = db.effective_custom_fields(t)
    grp = c.execute("SELECT name FROM groups WHERE id=?", (t["group_id"],)).fetchone() if t["group_id"] else None
    requester = c.execute("SELECT name, email FROM users WHERE id=?", (t["requester_id"],)).fetchone() if t["requester_id"] else None
    source = (t["source"] if "source" in t.keys() and t["source"] else "zendesk")
    local_id = t["local_id"] if "local_id" in t.keys() else None
    return {
        "id": t["id"],
        "local_id": local_id,
        "source": source,
        "display_id": local_id if (source == "native" and local_id) else f"#{t['id']}",
        "subject": t["subject"],
        "status": t["status"],
        "priority": t["priority"],
        "group": (grp["name"] if grp else None),
        "customer": _customer_label(c, cfs.get(FID_CUSTOMER)),
        "requester": dict(requester) if requester else None,
        "assignee_id": t["assignee_id"],
        "created_at": t["created_at"],
        "updated_at": t["updated_at"],
        "solved_at": t["solved_at"],
        "tags": json.loads(t["tags"] or "[]"),
    }


def _resolve_field_id(c: sqlite3.Connection, title: str) -> int | None:
    row = c.execute("SELECT id FROM ticket_fields WHERE LOWER(title)=LOWER(?)", (title,)).fetchone()
    return row["id"] if row else None


# ===== Read-only tools =====

@mcp.tool()
def search_tickets(query: str = "", status: str = "", customer: str = "",
                   group: str = "", limit: int = 20) -> dict:
    """Search BetterPlace support tickets.

    Args:
      query:    free-text — matches ticket id or subject (LIKE).
      status:   one of new/open/pending/hold/solved/closed, or 'open' for new+open+pending+hold,
                or 'untouched' for unassigned with no agent reply.
      customer: customer display name fragment (e.g. "BPCL"). Matches all customers whose name
                contains this string (so "BPCL" finds "BPCL", "IOWMS-BPCL", etc.).
      group:    group name fragment ("product support", "managed services").
      limit:    max results (default 20, max 100).

    Returns: list of ticket summaries (id, subject, status, customer, priority, dates).
    """
    limit = min(max(int(limit or 20), 1), 100)
    db.init()
    sql = "SELECT * FROM tickets WHERE 1=1"
    params: list = []

    if status == "open":
        sql += " AND status IN ('new','open','pending','hold')"
    elif status == "untouched":
        sql += (" AND status IN ('new','open') AND assignee_id IS NULL "
                "AND id NOT IN (SELECT ticket_id FROM ticket_comments tc "
                "JOIN users u ON u.id=tc.author_id WHERE u.role IN ('agent','admin'))")
    elif status:
        sql += " AND status = ?"; params.append(status)
    if query:
        sql += " AND (CAST(id AS TEXT) LIKE ? OR subject LIKE ?)"
        like = f"%{query}%"; params += [like, like]
    if group:
        sql += " AND group_id IN (SELECT id FROM groups WHERE LOWER(name) LIKE ?)"
        params.append(f"%{group.lower()}%")

    matched_customer_names: list[str] = []
    with db.conn() as c:
        # Resolve customer fragment → all matching option values, applied in SQL (not post-filter)
        if customer:
            f = c.execute("SELECT options FROM ticket_fields WHERE id=?", (int(FID_CUSTOMER),)).fetchone()
            matching_values: list[str] = []
            if f:
                for o in json.loads(f["options"] or "[]"):
                    name = (o.get("name") or "")
                    if customer.lower() in name.lower():
                        matching_values.append(o.get("value"))
                        matched_customer_names.append(name)
            if not matching_values:
                return {"count": 0, "tickets": [],
                        "note": f"No customer name matched '{customer}'. Try a broader fragment, "
                                "or call get_field_taxonomy('Customer Name') to see valid options."}
            placeholders = ",".join(["?"] * len(matching_values))
            sql += f" AND json_extract(custom_fields, '$.\"{FID_CUSTOMER}\"') IN ({placeholders})"
            params += matching_values

        sql += f" ORDER BY updated_at DESC LIMIT {limit}"
        rows = c.execute(sql, params).fetchall()
        out = [_ticket_summary_dict(c, r) for r in rows]

    result = {"count": len(out), "tickets": out}
    if matched_customer_names:
        result["matched_customers"] = matched_customer_names
    return result


@mcp.tool()
def get_ticket(ticket_id: int) -> dict:
    """Full details of a single ticket: header + all custom field values + assignee/requester/org.
    Field values reflect agent's local_overrides merged over ZD-synced custom_fields."""
    db.init()
    with db.conn() as c:
        t = c.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()
        if not t:
            return {"error": f"ticket {ticket_id} not found"}
        s = _ticket_summary_dict(c, t)
        # Merge local_overrides (Block #1)
        cfs = db.effective_custom_fields(t)
        field_rows = {r["id"]: r for r in c.execute("SELECT * FROM ticket_fields").fetchall()}
        fields = []
        for fid_str, val in cfs.items():
            fid = int(fid_str)
            f = field_rows.get(fid)
            if not f or not val:
                continue
            display = val
            if f["type"] in ("tagger", "multiselect"):
                display = _resolve_dropdown_label(c, fid, val)
            fields.append({"name": f["title"], "value": display, "raw_value": val, "type": f["type"]})
        org = c.execute("SELECT name FROM organizations WHERE id=?", (t["organization_id"],)).fetchone() if t["organization_id"] else None
        assignee = c.execute("SELECT name, email FROM users WHERE id=?", (t["assignee_id"],)).fetchone() if t["assignee_id"] else None
        s["organization"] = org["name"] if org else None
        s["assignee"] = dict(assignee) if assignee else None
        s["fields"] = fields
        s["url"] = f"http://127.0.0.1:8000/tickets/{ticket_id}"
    return s


@mcp.tool()
def get_conversation(ticket_id: int) -> dict:
    """Conversation thread for a ticket (public + internal). Decoded HTML entities. Sorted by time."""
    db.init()
    with db.conn() as c:
        rows = c.execute("""
            SELECT tc.*, u.name AS author_name, u.role AS author_role, u.email AS author_email
            FROM ticket_comments tc LEFT JOIN users u ON u.id = tc.author_id
            WHERE tc.ticket_id = ? ORDER BY tc.created_at
        """, (ticket_id,)).fetchall()
    cmts = []
    for r in rows:
        cmts.append({
            "author_name": r["author_name"],
            "author_email": r["author_email"],
            "author_role": r["author_role"],
            "public": bool(r["public"]),
            "kind": "internal" if not r["public"] else ("agent" if r["author_role"] in ("agent", "admin") else "customer"),
            "body": r["body"],
            "created_at": r["created_at"],
        })
    return {"ticket_id": ticket_id, "count": len(cmts), "comments": cmts}


@mcp.tool()
def find_similar_tickets(ticket_id: int, limit: int = 5) -> dict:
    """Find similar past tickets — any status, no time cutoff. Multi-tier match:
    customer+RC1 → customer → RC1 → subject keywords. Solved/closed tickets
    are preferred (have resolutions) but open/pending matches are also returned."""
    limit = min(max(int(limit or 5), 1), 20)
    db.init()
    with db.conn() as c:
        t = c.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()
        if not t:
            return {"error": f"ticket {ticket_id} not found"}
        cfs = json.loads(t["custom_fields"] or "{}")
        cust, prod, module = cfs.get(FID_CUSTOMER), cfs.get(FID_PRODUCT), cfs.get(FID_MODULE)
        rc1, rc2 = cfs.get(FID_RC1), cfs.get(FID_RC2)
        subj_lower = (t["subject"] or "").lower()
        subj_keywords = [w.strip(".,:;!?[]()") for w in subj_lower.split()
                         if len(w.strip(".,:;!?[]()")) > 3][:5]
        if not any([cust, prod, module, rc1]) and not subj_keywords:
            return {"ticket_id": ticket_id, "similar": []}

        ORDER_BY = ("CASE WHEN status IN ('solved','closed') THEN 0 ELSE 1 END, "
                    "updated_at DESC")
        def _scan(where_parts, params, pool):
            extra = (" AND " + " AND ".join(where_parts)) if where_parts else ""
            return c.execute(f"""
                SELECT id, subject, status, custom_fields, solved_at, created_at, updated_at
                FROM tickets WHERE id != ? {extra}
                ORDER BY {ORDER_BY} LIMIT {pool}
            """, [ticket_id] + params).fetchall()

        seen = set(); rows = []
        def _add(more):
            for r in more:
                if r["id"] in seen: continue
                seen.add(r["id"]); rows.append(r)

        if cust and rc1:
            _add(_scan([f"json_extract(custom_fields,'$.\"{FID_CUSTOMER}\"') = ?",
                        f"json_extract(custom_fields,'$.\"{FID_RC1}\"') = ?"],
                       [cust, rc1], 60))
        if len(rows) < limit * 3 and cust:
            _add(_scan([f"json_extract(custom_fields,'$.\"{FID_CUSTOMER}\"') = ?"],
                       [cust], 120))
        if len(rows) < limit * 3 and rc1:
            _add(_scan([f"json_extract(custom_fields,'$.\"{FID_RC1}\"') = ?"],
                       [rc1], 120))
        if len(rows) < limit and subj_keywords:
            like = " OR ".join(["LOWER(subject) LIKE ?"] * len(subj_keywords))
            _add(_scan([f"({like})"], [f"%{kw}%" for kw in subj_keywords], 100))

        scored = []
        for r in rows:
            try: rcfs = json.loads(r["custom_fields"] or "{}")
            except Exception: continue
            score = 0
            if cust and rcfs.get(FID_CUSTOMER) == cust: score += 3
            if prod and rcfs.get(FID_PRODUCT) == prod: score += 2
            if module and rcfs.get(FID_MODULE) == module: score += 2
            if rc1 and rcfs.get(FID_RC1) == rc1: score += 2
            if rc2 and rcfs.get(FID_RC2) == rc2: score += 1
            if subj_keywords:
                rs = (r["subject"] or "").lower()
                score += min(sum(1 for kw in subj_keywords if kw in rs), 3)
            if r["status"] in ("solved", "closed"): score += 1
            if score == 0: continue
            scored.append({
                "ticket_id": r["id"], "subject": r["subject"], "status": r["status"],
                "score": score, "max_score": 14,
                "match_pct": min(100, round(score / 14 * 100)),
                "solved_at": r["solved_at"] or r["updated_at"],
            })
        scored.sort(key=lambda x: -x["score"])
        scored = scored[:limit]
        # Batched insight summary lookup — no N+1
        if scored:
            ids = [s["ticket_id"] for s in scored]
            placeholders = ",".join("?" * len(ids))
            ins_rows = c.execute(
                f"""SELECT ticket_id, summary, MAX(id) AS _last FROM ticket_insights
                    WHERE ticket_id IN ({placeholders}) GROUP BY ticket_id""",
                ids,
            ).fetchall()
            sums = {r["ticket_id"]: r["summary"] for r in ins_rows}
            for s in scored:
                s["summary"] = sums.get(s["ticket_id"])
    return {"ticket_id": ticket_id, "similar": scored}


@mcp.tool()
def get_customer_summary(customer_name: str, days: int = 30) -> dict:
    """Open ticket count + recent themes for a customer over the last N days."""
    db.init()
    days = max(1, min(int(days or 30), 365))
    with db.conn() as c:
        # Find the option value for this customer
        f = c.execute("SELECT options FROM ticket_fields WHERE id=?", (int(FID_CUSTOMER),)).fetchone()
        target_value = customer_name
        if f:
            for o in json.loads(f["options"] or "[]"):
                if (o.get("name") or "").lower() == customer_name.lower():
                    target_value = o.get("value")
                    break
        rows = c.execute(f"""
            SELECT id, subject, status, created_at, custom_fields,
                   json_extract(custom_fields, '$."{FID_RC1}"') AS rc1
            FROM tickets
            WHERE LOWER(json_extract(custom_fields, '$."{FID_CUSTOMER}"')) = LOWER(?)
              AND created_at > datetime('now', ?)
            ORDER BY created_at DESC
        """, (target_value, f"-{days} days")).fetchall()
        open_n = sum(1 for r in rows if r["status"] in ("new", "open", "pending", "hold"))
        solved_n = sum(1 for r in rows if r["status"] in ("solved", "closed"))
        rc1_counts: dict[str, int] = {}
        for r in rows:
            v = r["rc1"]
            if v:
                label = _resolve_dropdown_label(c, int(FID_RC1), v)
                rc1_counts[label] = rc1_counts.get(label, 0) + 1
        top_rc1 = sorted(rc1_counts.items(), key=lambda x: -x[1])[:5]
        recent_subjects = [{"id": r["id"], "subject": r["subject"], "status": r["status"]} for r in rows[:10]]
    return {
        "customer": customer_name,
        "window_days": days,
        "total_tickets": len(rows),
        "open_count": open_n,
        "solved_count": solved_n,
        "top_root_causes": [{"name": k, "count": v} for k, v in top_rc1],
        "recent_tickets": recent_subjects,
    }


@mcp.tool()
def list_views() -> dict:
    """All views the agent dashboard exposes, with live counts."""
    db.init()
    views = [
        ("All open",              "status IN ('new','open','pending','hold')"),
        ("Untouched · need pickup", "status IN ('new','open') AND assignee_id IS NULL "
                                    "AND id NOT IN (SELECT ticket_id FROM ticket_comments tc "
                                    "JOIN users u ON u.id=tc.author_id WHERE u.role IN ('agent','admin'))"),
        ("Awaiting customer",     "status='pending'"),
        ("With engineering (Jira)", f"status='hold' AND json_extract(custom_fields,'$.\"{FID_JIRA_ID}\"') IS NOT NULL "
                                     f"AND json_extract(custom_fields,'$.\"{FID_JIRA_ID}\"') != ''"),
        ("Solved last 24h",       "status='solved' AND solved_at > datetime('now','-1 day')"),
        ("Missing KB Article",    "status IN ('new','open','pending','hold','solved') AND ("
                                  f"json_extract(custom_fields,'$.\"{FID_KB_ARTICLE}\"') IS NULL "
                                  f"OR json_extract(custom_fields,'$.\"{FID_KB_ARTICLE}\"') IN ('','NA','na'))"),
        ("SLA at risk (>8h)",     "status IN ('new','open') AND created_at < datetime('now','-8 hours') "
                                  "AND id NOT IN (SELECT ticket_id FROM ticket_comments tc "
                                  "JOIN users u ON u.id=tc.author_id WHERE u.role IN ('agent','admin'))"),
        ("SLA breached (>24h)",   "status IN ('new','open') AND created_at < datetime('now','-24 hours') "
                                  "AND id NOT IN (SELECT ticket_id FROM ticket_comments tc "
                                  "JOIN users u ON u.id=tc.author_id WHERE u.role IN ('agent','admin'))"),
    ]
    out = []
    with db.conn() as c:
        for name, where in views:
            row = c.execute(f"SELECT COUNT(*) AS n FROM tickets WHERE {where}").fetchone()
            out.append({"name": name, "count": row["n"]})
    return {"views": out}


@mcp.tool()
def get_field_taxonomy(field_title: str) -> dict:
    """All valid dropdown options for a custom field. Useful before suggesting a value."""
    db.init()
    with db.conn() as c:
        r = c.execute("SELECT id, title, type, options, required FROM ticket_fields WHERE LOWER(title)=LOWER(?)", (field_title,)).fetchone()
        if not r:
            return {"error": f"unknown field: {field_title}"}
        opts = json.loads(r["options"] or "[]")
    return {
        "field_id": r["id"], "title": r["title"], "type": r["type"],
        "required": bool(r["required"]),
        "options": [{"name": o.get("name"), "value": o.get("value")} for o in opts],
    }


@mcp.tool()
def list_unanalyzed_tickets(status: str = "open", customer: str = "", limit: int = 50) -> dict:
    """Tickets that need fresh AI insight: never analysed, OR analysed before the latest update.

    Use this to power bulk analysis runs. Typical flow:
      1. Call list_unanalyzed_tickets(status="open", limit=50)
      2. For each ticket id returned, perform the analyze_ticket flow and call save_ticket_insight.

    Args:
      status:   'open' (default) for new+open+pending+hold,
                'untouched' for unassigned with no agent reply,
                or a specific status name.
      customer: customer name fragment to scope the batch (optional).
      limit:    max tickets to return (default 50, max 200).

    Returns: list of {ticket_id, subject, updated_at, last_insight_at} for tickets where no
    insight exists, OR the latest insight predates the ticket's most recent update.
    """
    limit = min(max(int(limit or 50), 1), 200)
    db.init()
    where = ""
    params: list = []
    if status == "open":
        where += " AND t.status IN ('new','open','pending','hold')"
    elif status == "untouched":
        where += (" AND t.status IN ('new','open') AND t.assignee_id IS NULL "
                  "AND t.id NOT IN (SELECT ticket_id FROM ticket_comments tc "
                  "JOIN users u ON u.id=tc.author_id WHERE u.role IN ('agent','admin'))")
    elif status:
        where += " AND t.status = ?"; params.append(status)

    with db.conn() as c:
        if customer:
            f = c.execute("SELECT options FROM ticket_fields WHERE id=?", (int(FID_CUSTOMER),)).fetchone()
            matching: list[str] = []
            if f:
                for o in json.loads(f["options"] or "[]"):
                    if customer.lower() in (o.get("name") or "").lower():
                        matching.append(o.get("value"))
            if not matching:
                return {"count": 0, "tickets": [], "note": f"No customer matched '{customer}'."}
            ph = ",".join(["?"] * len(matching))
            where += f" AND json_extract(t.custom_fields, '$.\"{FID_CUSTOMER}\"') IN ({ph})"
            params += matching

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
            LIMIT {limit}
        """
        rows = c.execute(sql, params).fetchall()
    return {
        "count": len(rows),
        "tickets": [{
            "ticket_id": r["id"], "subject": r["subject"], "status": r["status"],
            "updated_at": r["updated_at"], "last_insight_at": r["last_insight_at"],
            "needs_first_analysis": r["last_insight_at"] is None,
        } for r in rows],
    }


@mcp.tool()
def get_ai_insights(ticket_id: int) -> dict:
    """Most recent stored insights for a ticket (the same data the web UI displays)."""
    db.init()
    with db.conn() as c:
        r = c.execute("SELECT * FROM ticket_insights WHERE ticket_id=? ORDER BY id DESC LIMIT 1", (ticket_id,)).fetchone()
    if not r:
        return {"ticket_id": ticket_id, "insight": None}
    # Safe column access — older inserts won't have the new columns
    def _get(col):
        try: return r[col]
        except (IndexError, KeyError): return None
    return {
        "ticket_id": ticket_id,
        "insight": {
            "summary": r["summary"],
            "issue_summary": _get("issue_summary"),
            "historical_context": _get("historical_context"),
            "current_state": _get("current_state"),
            "recommended_action": _get("recommended_action"),
            "recommendations": json.loads(r["recommendations"] or "[]"),
            "completeness": json.loads(r["completeness"] or "[]"),
            "similar_ticket_keys": json.loads(r["similar_ticket_ids"] or "[]"),
            "similar_with_reasoning": json.loads(_get("similar_with_reasoning") or "[]"),
            "suggested_reply": json.loads(r["suggested_reply"]) if r["suggested_reply"] else None,
            "kb_worthy": bool(r["kb_worthy"]),
            "kb_topic": r["kb_topic"],
            "pickup_flag": json.loads(r["pickup_flag"]) if r["pickup_flag"] else None,
            "model": r["model"],
            "created_at": r["created_at"],
        }
    }


# ===== Write tools (Claude Desktop will surface a confirmation prompt) =====

@mcp.tool()
def save_ticket_insight(
    ticket_id: int,
    summary: str,
    recommendations: list[dict] | None = None,
    completeness: list[dict] | None = None,
    similar_ticket_ids: list[int] | None = None,
    suggested_reply: dict | None = None,
    kb_worthy: bool = False,
    kb_topic: str | None = None,
    pickup_flag: dict | None = None,
    # New history-aware narrative fields. All optional so existing callers
    # (older prompts, older Claude Desktop sessions) keep working.
    issue_summary: str | None = None,
    historical_context: str | None = None,
    current_state: str | None = None,
    recommended_action: str | None = None,
    similar_with_reasoning: list[dict] | None = None,
) -> dict:
    """Persist a ticket insight to the local database.

    Required:
      summary — kept for backcompat with the legacy renderer.

    Recommended NEW (history-aware):
      issue_summary       — 2-3 sentences explaining what this ticket is about.
      historical_context  — 2-4 sentences citing past tickets by BP-NNNNNN/#N
                            and how each was resolved.
      current_state       — 1-2 sentences on where the ticket stands right now.
      recommended_action  — 1-2 sentences with a SPECIFIC next step.
      similar_with_reasoning — array of {ticket_id, match_pct, why_relevant,
                            how_resolved, applicability:"high"|"medium"|"low"}.

    Existing arrays:
      recommendations items: {"field": str, "current": str|null, "suggest": str,
        "confidence": float, "reason": str, "review": bool, "propose_new_option": bool}
      completeness items:    {"state": "ok"|"miss"|"thin", "text": str, "hint": str|null}
      suggested_reply:       {"flag": str, "flaws": [str], "current": str, "suggested": str} or null
      pickup_flag:           {"title": str, "meta": str, "reason": str} or null
    """
    db.init()
    with db.conn() as c:
        c.execute("""
            INSERT INTO ticket_insights (ticket_id, model, summary, recommendations, completeness,
                similar_ticket_ids, suggested_reply, kb_worthy, kb_topic, pickup_flag, created_at,
                cost_usd, input_tokens, output_tokens, cached_input_tokens,
                issue_summary, historical_context, current_state, recommended_action,
                similar_with_reasoning)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            ticket_id, "claude-desktop-mcp", summary or "",
            json.dumps(recommendations or []),
            json.dumps(completeness or []),
            json.dumps(similar_ticket_ids or []),
            json.dumps(suggested_reply) if suggested_reply else None,
            1 if kb_worthy else 0, kb_topic,
            json.dumps(pickup_flag) if pickup_flag else None,
            db.now_iso(),
            0.0, 0, 0, 0,
            issue_summary, historical_context, current_state, recommended_action,
            json.dumps(similar_with_reasoning) if similar_with_reasoning else None,
        ))
        ins_id = c.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        # Stamp the ticket as analyzed so the worker won't pick it again until update
        c.execute(
            "UPDATE tickets SET last_analyzed_updated_at = updated_at WHERE id = ?",
            (ticket_id,),
        )
        db.audit(c, actor="claude-desktop", action="save_ticket_insight",
                 target_type="ticket", target_id=str(ticket_id),
                 detail=f"insight_id={ins_id} recs={len(recommendations or [])} "
                        f"history={'yes' if historical_context else 'no'}")
    return {"ok": True, "insight_id": ins_id, "ticket_id": ticket_id}


@mcp.tool()
def update_ticket_field(ticket_id: int, field_title: str, value: str, reason: str = "") -> dict:
    """Update a custom field LOCALLY (does not write to Zendesk).

    POLICY: this tool stores the change in tickets.local_overrides only.
    The web UI merges overrides over ZD-synced values when displaying. Sync
    never overwrites local overrides.

    Args:
      ticket_id:   target ticket
      field_title: exact title of the custom field (case-insensitive)
      value:       display name OR raw option value (we resolve dropdowns)
      reason:      one-line audit-log entry — strongly recommended
    """
    db.init()
    with db.conn() as c:
        fid = _resolve_field_id(c, field_title)
        if not fid:
            return {"error": f"unknown field: {field_title}"}
        f = c.execute("SELECT type, options FROM ticket_fields WHERE id=?", (fid,)).fetchone()
        stored_value: object = value
        if f["type"] in ("tagger", "multiselect"):
            for o in json.loads(f["options"] or "[]"):
                if o.get("name") == value or o.get("value") == value:
                    stored_value = o.get("value")
                    break
        try:
            db.set_local_field_override(c, ticket_id, fid, stored_value)
        except Exception as e:
            return {"error": f"local save failed: {e}"}
        db.audit(c, actor="claude-desktop", action="update_ticket_field_local",
                 target_type="ticket", target_id=str(ticket_id),
                 detail=f"field={field_title} value={stored_value} reason={reason} (local only)")
    return {"ok": True, "ticket_id": ticket_id, "field": field_title,
            "stored_value": stored_value, "scope": "local_only"}


@mcp.tool()
def add_dropdown_option(field_title: str, new_option_name: str) -> dict:
    """Append a new option to a dropdown LOCALLY (does not write to Zendesk).

    POLICY: the option is added to the local copy of ticket_fields.options and
    tracked in local_field_options. ZD's field definition stays unchanged.
    """
    db.init()
    with db.conn() as c:
        fid = _resolve_field_id(c, field_title)
        if not fid:
            return {"error": f"unknown field: {field_title}"}
        f = c.execute("SELECT type, options FROM ticket_fields WHERE id=?", (fid,)).fetchone()
        if f["type"] not in ("tagger", "multiselect"):
            return {"error": f"field {field_title} is not a dropdown ({f['type']})"}
        opts = json.loads(f["options"] or "[]")
        existing = next((o for o in opts if (o.get("name") or "").lower() == new_option_name.lower()), None)
        if existing:
            new_value = existing.get("value")
            already = True
        else:
            new_value = new_option_name.lower().replace(" ", "_").replace("/", "_").strip("_")
            opts.append({"name": new_option_name, "value": new_value})
            c.execute("UPDATE ticket_fields SET options=? WHERE id=?", (json.dumps(opts), fid))
            already = False
        c.execute("""
            INSERT INTO local_field_options (field_id, option_name, option_value, proposed_by_email, sync_status, created_at)
            VALUES (?, ?, ?, 'claude-desktop', 'local_only', ?)
        """, (fid, new_option_name, new_value, db.now_iso()))
        db.audit(c, actor="claude-desktop", action="add_dropdown_option_local",
                 target_type="ticket_field", target_id=str(fid),
                 detail=f"name={new_option_name} value={new_value} (local only)")
    return {"ok": True, "field": field_title, "new_option_name": new_option_name,
            "new_value": new_value, "already_existed": already, "scope": "local_only"}


@mcp.tool()
def create_native_ticket(subject: str, requester_email: str, requester_name: str = "",
                         description: str = "", group_name: str = "", customer: str = "",
                         priority: str = "normal") -> dict:
    """Create a NATIVE ticket (lives only in our DB, never in Zendesk).
    Returns the new ticket's local_id (BP-NNNNNN) and integer id.

    Args:
      subject:         (required) ticket subject
      requester_email: (required) who's reporting; user is auto-created if new
      requester_name:  display name for new requesters
      description:     initial message body — becomes the first comment
      group_name:      'Product Support' or 'Managed Services' (or any group fragment)
      customer:        customer display name fragment to set Customer Name field
      priority:        low | normal (default) | high | urgent
    """
    db.init()
    if priority not in ("low", "normal", "high", "urgent"):
        priority = "normal"
    with db.conn() as c:
        # Resolve group
        group_id = None
        if group_name:
            g = c.execute("SELECT id FROM groups WHERE LOWER(name) LIKE ?",
                          (f"%{group_name.lower()}%",)).fetchone()
            group_id = g["id"] if g else None
        # Resolve or create requester
        u = c.execute("SELECT id FROM users WHERE LOWER(email) = LOWER(?)", (requester_email,)).fetchone()
        if u:
            requester_id = u["id"]
        else:
            seq_str = db.get_meta(c, "next_native_user_seq") or "1"
            useq = int(seq_str)
            db.set_meta(c, "next_native_user_seq", str(useq + 1))
            requester_id = 1_000_000_000 + useq
            c.execute("""
                INSERT INTO users (id, name, email, role, raw)
                VALUES (?, ?, ?, 'end-user', ?)
            """, (requester_id, requester_name or requester_email, requester_email,
                  json.dumps({"source": "native_create_mcp"})))
        # Resolve customer
        cfs = {}
        if customer:
            cust_field = c.execute("SELECT options FROM ticket_fields WHERE id=?",
                                   (int(FID_CUSTOMER),)).fetchone()
            if cust_field:
                for o in json.loads(cust_field["options"] or "[]"):
                    if customer.lower() in (o.get("name") or "").lower():
                        cfs[FID_CUSTOMER] = o.get("value")
                        break
        result = db.insert_native_ticket(
            c, subject=subject, requester_id=requester_id,
            organization_id=None, group_id=group_id, priority=priority,
            custom_fields=cfs, creator_email="claude-desktop",
        )
        if description.strip():
            db.insert_native_comment(c, ticket_id=result["id"],
                                     author_id=requester_id, body=description, public=True)
        db.audit(c, actor="claude-desktop", action="create_native_ticket",
                 target_type="ticket", target_id=str(result["id"]),
                 detail=f"local_id={result['local_id']} subject={subject[:80]}")
    return {"ok": True, **result}


@mcp.tool()
def record_ai_feedback(ticket_id: int, field_title: str, decision: str,
                       ai_suggested: str, final_value: str = "",
                       reason: str = "") -> dict:
    """Log Claude's approve/reject/edit decision on a field. Builds the learning corpus.

    decision: 'approved' | 'rejected' | 'edited'
    """
    if decision not in ("approved", "rejected", "edited"):
        return {"error": "decision must be approved | rejected | edited"}
    db.init()
    with db.conn() as c:
        last_ins = c.execute("SELECT id FROM ticket_insights WHERE ticket_id=? ORDER BY id DESC LIMIT 1", (ticket_id,)).fetchone()
        insight_id = last_ins["id"] if last_ins else None
        fid = db.record_feedback(
            c, ticket_id=ticket_id, insight_id=insight_id, field_name=field_title,
            ai_current=None, ai_suggested=ai_suggested, confidence=None,
            decision=decision,
            final_value=final_value or (ai_suggested if decision == "approved" else None),
            rejection_reason=reason or None,
            actor="claude-desktop",
        )
    return {"ok": True, "feedback_id": fid, "decision": decision}


# ===== Prompts =====

@mcp.prompt()
def analyze_ticket(ticket_id: int) -> str:
    """Full structured analysis of a single ticket. Saves the insight to the DB."""
    return f"""You are the BetterPlace Co-Pilot AI. Produce a complete insight for ticket #{ticket_id}
using the MCP tools available. Steps, in order:

1. Call `get_ticket({ticket_id})` to load the ticket header and current field values.
2. Call `get_conversation({ticket_id})` to read the full thread (customer + agent + internal).
3. Identify which fields look wrong or empty. For each candidate field where you'll suggest a
   change, call `get_field_taxonomy("<field title>")` to see the valid dropdown options.
4. Call `find_similar_tickets({ticket_id}, limit=5)` for historical context.

Now produce a structured insight. The recommendations array entries must follow this shape:
  {{"field": "<exact title>", "current": "<current value or null>",
    "suggest": "<proposed value>", "confidence": 0.0-1.0,
    "reason": "<1-2 sentence justification>",
    "review": false, "propose_new_option": false}}

If no existing dropdown option fits, set propose_new_option=true; the agent will see a special UI
to add the option to Zendesk.

The completeness array entries: {{"state": "ok"|"miss"|"thin", "text": "<message>", "hint": "<optional>"}}

The suggested_reply object (or null): {{"flag": "<short reason>", "flaws": ["<gap>"],
  "current": "<latest agent reply>", "suggested": "<improved reply>"}}

5. Finally, call `save_ticket_insight(ticket_id={ticket_id}, summary, recommendations, completeness,
   similar_ticket_ids, suggested_reply, kb_worthy, kb_topic, pickup_flag)` to persist the insight.
   The web UI will display it on next page load.

After saving, give the agent a 3-line plain-English summary of what you wrote.
Do not invent values that aren't in the field taxonomy unless propose_new_option=true.
"""


@mcp.prompt()
def morning_digest(group: str = "") -> str:
    """A morning briefing of what's untouched, breached, and trending."""
    g = f' for the "{group}" group' if group else ""
    return f"""You are the BetterPlace Co-Pilot. Produce a morning digest{g} for the support team.

1. Call `list_views()` to see live counts.
2. Call `search_tickets(status="untouched", limit=10)` to list pickup-overdue tickets.
3. Call `search_tickets(status="open", limit=20)` and group mentally by customer. Identify the
   top 3 customers by open ticket count.
4. For each top customer, call `get_customer_summary(customer_name=..., days=7)` to identify themes.

Output a tight summary with:
- 🚨 Tickets needing pickup right now (5 max)
- 🏢 Top 3 customers by open volume + their dominant root cause
- 📈 SLA: how many at risk, how many breached
- ✅ One short recommendation: what should the team focus on first
"""


@mcp.prompt()
def summarize_conversation(ticket_id: int) -> str:
    """3-sentence summary of a single ticket's conversation."""
    return f"""Summarize the conversation on ticket #{ticket_id} in exactly 3 sentences.
1. Call `get_ticket({ticket_id})` and `get_conversation({ticket_id})`.
2. Sentence 1: what the customer reported.
3. Sentence 2: what the agent has done (if anything).
4. Sentence 3: what's currently expected next, and from whom.
"""


# ===== Entry point =====

def main() -> None:
    """Run the MCP server over stdio (for Claude Desktop)."""
    mcp.run()


if __name__ == "__main__":
    main()
