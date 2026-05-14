"""Placeholder system — like Zendesk's `{{ticket.requester.name}}` but native.

The catalog is the single source of truth for both:
  - The autocomplete UI in the textareas (reply box, internal-note editor,
    automation action bodies, auto-reply templates) — every placeholder
    here shows up in the suggestion popup.
  - The runtime substitution that runs at the moment an action fires.

Three flavours of placeholder:

    {{ticket.subject}}            → standard attribute
    {{ticket.cf.product}}         → custom field by title slug
    {{ticket.cf.15315331275025}}  → custom field by id (always available)

Autocomplete works on either `{{` or `<` so users coming from Zendesk
(`<requester.first_name>`) feel at home.

Functions:
    catalog(c)         → list of {key, label, group, sample} for the UI
    resolve(c, ticket_id, current_user_email) → dict of {placeholder_key: value}
    interpolate(template, vars) → substitute {{…}} in a string
"""
from __future__ import annotations
import json
import re
import sqlite3
from datetime import datetime, timezone, timedelta


# =============================================================================
# Static placeholders — always available
# =============================================================================
STATIC_PLACEHOLDERS = [
    # Ticket
    {"key": "ticket.id",            "group": "Ticket", "label": "Numeric ticket id (ZD or native)"},
    {"key": "ticket.local_id",      "group": "Ticket", "label": "BP-NNNNNN local id (empty for ZD-synced)"},
    {"key": "ticket.display_id",    "group": "Ticket", "label": "Best display id (BP-… if native, #… if ZD)"},
    {"key": "ticket.url",           "group": "Ticket", "label": "Link to ticket detail in this tool"},
    {"key": "ticket.subject",       "group": "Ticket", "label": "Subject line"},
    {"key": "ticket.description",   "group": "Ticket", "label": "First comment / description"},
    {"key": "ticket.status",        "group": "Ticket", "label": "Status (new/open/pending/hold/solved/closed)"},
    {"key": "ticket.custom_status", "group": "Ticket", "label": "Custom-status agent label (e.g. 'With Dev')"},
    {"key": "ticket.priority",      "group": "Ticket", "label": "Priority"},
    {"key": "ticket.type",          "group": "Ticket", "label": "Type (question/incident/problem/task)"},
    {"key": "ticket.tags",          "group": "Ticket", "label": "Tags joined by space"},
    {"key": "ticket.source",        "group": "Ticket", "label": "Source (zendesk/native/gmail)"},
    {"key": "ticket.created_at",    "group": "Ticket", "label": "Created at (IST)"},
    {"key": "ticket.updated_at",    "group": "Ticket", "label": "Last updated (IST)"},
    {"key": "ticket.solved_at",     "group": "Ticket", "label": "Solved at (IST)"},
    # Requester
    {"key": "ticket.requester.name",  "group": "Requester", "label": "Customer's name"},
    {"key": "ticket.requester.email", "group": "Requester", "label": "Customer's email"},
    {"key": "ticket.requester.first_name", "group": "Requester", "label": "Customer's first name"},
    # Org / customer
    {"key": "ticket.organization.name", "group": "Organization", "label": "Organization name"},
    {"key": "ticket.customer",          "group": "Organization", "label": "Customer custom field display name"},
    # Group + assignee
    {"key": "ticket.group.name",        "group": "Assignment", "label": "Group name"},
    {"key": "ticket.assignee.name",     "group": "Assignment", "label": "Assignee's name"},
    {"key": "ticket.assignee.email",    "group": "Assignment", "label": "Assignee's email"},
    {"key": "ticket.assignee.first_name","group": "Assignment", "label": "Assignee's first name"},
    # Conversation
    {"key": "ticket.last_comment",       "group": "Conversation", "label": "Most recent comment body (first 500 chars)"},
    {"key": "ticket.last_customer_comment","group": "Conversation", "label": "Most recent customer comment body"},
    # SLA
    {"key": "sla.first_reply.state",     "group": "SLA", "label": "First-reply SLA state"},
    {"key": "sla.resolution.state",      "group": "SLA", "label": "Resolution SLA state"},
    {"key": "sla.policy",                "group": "SLA", "label": "Active SLA policy name"},
    # AI
    {"key": "ai.summary",                "group": "AI",  "label": "Latest AI summary"},
    # Current actor + time
    {"key": "current_user.name",         "group": "Context", "label": "Current agent's name"},
    {"key": "current_user.email",        "group": "Context", "label": "Current agent's email"},
    {"key": "current_user.first_name",   "group": "Context", "label": "Current agent's first name"},
    {"key": "now",                       "group": "Context", "label": "Current timestamp (IST)"},
    {"key": "now.date",                  "group": "Context", "label": "Today's date (YYYY-MM-DD)"},
]


def _to_ist(ts: str | None) -> str:
    if not ts: return ""
    try:
        s = ts.replace("Z", "+00:00") if isinstance(ts, str) else ts
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        ist = dt.astimezone(timezone(timedelta(hours=5, minutes=30)))
        return ist.strftime("%d %b %Y %H:%M IST")
    except Exception:
        return ts


def _slugify(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    return s.strip("_")


# =============================================================================
# Custom field placeholders — built dynamically from ticket_fields + native_fields
# =============================================================================
def custom_field_placeholders(c: sqlite3.Connection) -> list[dict]:
    """Return one placeholder per known field, both by-slug and by-id. The UI
    shows the by-slug version; resolve() understands both."""
    out: list[dict] = []
    rows = list(c.execute("""
        SELECT id, title, type FROM ticket_fields
        WHERE type NOT IN ('subject','description','status','priority','group','assignee','custom_status','tickettype')
        ORDER BY title
    """).fetchall())
    try:
        rows += list(c.execute(
            "SELECT id, title, type FROM native_fields WHERE active=1 ORDER BY title"
        ).fetchall())
    except sqlite3.OperationalError:
        pass
    seen_slugs: set[str] = set()
    for r in rows:
        slug = _slugify(r["title"] or "")
        if not slug or slug in seen_slugs:
            slug = f"{slug}_{r['id']}" if slug else f"field_{r['id']}"
        seen_slugs.add(slug)
        out.append({
            "key":   f"ticket.cf.{slug}",
            "group": "Custom fields",
            "label": r["title"],
            "field_id": r["id"], "by_id": f"ticket.cf.{r['id']}",
        })
    return out


def catalog(c: sqlite3.Connection) -> list[dict]:
    return STATIC_PLACEHOLDERS + custom_field_placeholders(c)


# =============================================================================
# Resolve — compute the full {placeholder: value} dict for a ticket
# =============================================================================
def resolve(c: sqlite3.Connection, ticket_id: int,
            current_user_email: str = "system") -> dict:
    t = c.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()
    if not t:
        return {}
    raw = {}
    try: raw = json.loads(t["raw"] or "{}")
    except Exception: pass
    # Effective custom fields (merges local_overrides)
    from . import db as _db
    cfs = _db.effective_custom_fields(t)
    # Requester
    requester = c.execute(
        "SELECT name, email FROM users WHERE id=?", (t["requester_id"],)
    ).fetchone() if t["requester_id"] else None
    r_name = (requester["name"] if requester else "") or ""
    r_email = (requester["email"] if requester else "") or ""
    r_first = r_name.split()[0] if r_name else ""
    # Org
    org = c.execute(
        "SELECT name FROM organizations WHERE id=?", (t["organization_id"],)
    ).fetchone() if t["organization_id"] else None
    org_name = (org["name"] if org else "") or ""
    # Group + assignee
    grp = c.execute(
        "SELECT name FROM groups WHERE id=?", (t["group_id"],)
    ).fetchone() if t["group_id"] else None
    assignee = c.execute(
        "SELECT name, email FROM users WHERE id=?", (t["assignee_id"],)
    ).fetchone() if t["assignee_id"] else None
    a_name = (assignee["name"] if assignee else "") or ""
    a_email = (assignee["email"] if assignee else "") or ""
    a_first = a_name.split()[0] if a_name else ""
    # Customer field display
    customer_val = cfs.get("15315331275025") or ""
    customer_disp = customer_val
    if customer_val:
        cust_field = c.execute(
            "SELECT options FROM ticket_fields WHERE id=15315331275025"
        ).fetchone()
        if cust_field:
            try:
                for o in json.loads(cust_field["options"] or "[]"):
                    if o.get("value") == customer_val:
                        customer_disp = o.get("name") or customer_val
                        break
            except Exception:
                pass
    # Custom status label
    cs_label = ""
    cs_id = raw.get("custom_status_id")
    if cs_id:
        r = c.execute(
            "SELECT agent_label FROM custom_statuses WHERE id=?", (cs_id,)
        ).fetchone()
        if r: cs_label = r["agent_label"] or ""
    # Last comment + last customer comment
    last_cmt = c.execute("""
        SELECT body FROM ticket_comments WHERE ticket_id=? ORDER BY id DESC LIMIT 1
    """, (ticket_id,)).fetchone()
    last_cust_cmt = c.execute("""
        SELECT tc.body FROM ticket_comments tc LEFT JOIN users u ON u.id=tc.author_id
        WHERE tc.ticket_id=? AND COALESCE(u.role,'end-user') NOT IN ('agent','admin')
        ORDER BY tc.id DESC LIMIT 1
    """, (ticket_id,)).fetchone()
    # SLA snapshot
    sla = c.execute("SELECT * FROM ticket_sla WHERE ticket_id=?", (ticket_id,)).fetchone()
    sla_pol_name = ""
    if sla and sla["policy_id"]:
        pol = c.execute("SELECT name FROM sla_policies WHERE id=?", (sla["policy_id"],)).fetchone()
        if pol: sla_pol_name = pol["name"] or ""
    # AI insight
    ai = c.execute(
        "SELECT summary FROM ticket_insights WHERE ticket_id=? ORDER BY id DESC LIMIT 1",
        (ticket_id,)).fetchone()
    # Current user info
    cu = c.execute(
        "SELECT name, email FROM users WHERE LOWER(email)=LOWER(?)",
        (current_user_email,)).fetchone() if current_user_email else None
    cu_name = (cu["name"] if cu else current_user_email) or "system"
    cu_first = cu_name.split()[0] if cu_name else ""

    now_ist = _to_ist(datetime.now(timezone.utc).isoformat())
    today = (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).strftime("%Y-%m-%d")

    out: dict[str, str] = {
        # Ticket
        "ticket.id":            str(t["id"]),
        "ticket.local_id":      t["local_id"] or "",
        "ticket.display_id":    (t["local_id"] or f"#{t['id']}"),
        "ticket.url":           f"/tickets/{t['local_id'] or t['id']}",
        "ticket.subject":       t["subject"] or "",
        "ticket.description":   "",  # filled below
        "ticket.status":        t["status"] or "",
        "ticket.custom_status": cs_label,
        "ticket.priority":      t["priority"] or "",
        "ticket.type":          t["type"] or "",
        "ticket.tags":          " ".join(json.loads(t["tags"] or "[]")),
        "ticket.source":        t["source"] or "zendesk",
        "ticket.created_at":    _to_ist(t["created_at"]),
        "ticket.updated_at":    _to_ist(t["updated_at"]),
        "ticket.solved_at":     _to_ist(t["solved_at"]),
        # Requester
        "ticket.requester.name":       r_name,
        "ticket.requester.email":      r_email,
        "ticket.requester.first_name": r_first,
        # Org
        "ticket.organization.name":    org_name,
        "ticket.customer":             customer_disp,
        # Assignment
        "ticket.group.name":           (grp["name"] if grp else "") or "",
        "ticket.assignee.name":        a_name,
        "ticket.assignee.email":       a_email,
        "ticket.assignee.first_name":  a_first,
        # Conversation
        "ticket.last_comment":          ((last_cmt["body"] if last_cmt else "") or "")[:500],
        "ticket.last_customer_comment": ((last_cust_cmt["body"] if last_cust_cmt else "") or "")[:500],
        # SLA
        "sla.first_reply.state":  (sla["first_reply_state"] if sla else "") or "",
        "sla.resolution.state":   (sla["resolution_state"] if sla else "") or "",
        "sla.policy":             sla_pol_name,
        # AI
        "ai.summary":             ((ai["summary"] if ai else "") or "")[:500],
        # Context
        "current_user.name":       cu_name,
        "current_user.email":      (cu["email"] if cu else current_user_email) or "",
        "current_user.first_name": cu_first,
        "now":      now_ist,
        "now.date": today,
    }
    # First comment as description fallback
    first_cmt = c.execute(
        "SELECT body FROM ticket_comments WHERE ticket_id=? ORDER BY id ASC LIMIT 1",
        (ticket_id,)).fetchone()
    if first_cmt:
        out["ticket.description"] = (first_cmt["body"] or "")[:500]

    # Custom fields — by slug AND by id, so {{ticket.cf.product}} and
    # {{ticket.cf.15315331275025}} both resolve.
    field_rows = list(c.execute("""
        SELECT id, title, type, options FROM ticket_fields
        WHERE type NOT IN ('subject','description','status','priority','group','assignee','custom_status','tickettype')
    """).fetchall())
    try:
        field_rows += list(c.execute(
            "SELECT id, title, type, options FROM native_fields WHERE active=1"
        ).fetchall())
    except sqlite3.OperationalError:
        pass
    for r in field_rows:
        raw_v = cfs.get(str(r["id"]))
        disp = raw_v
        if r["type"] in ("tagger", "multiselect") and raw_v:
            try:
                for o in json.loads(r["options"] or "[]"):
                    if o.get("value") == raw_v:
                        disp = o.get("name") or raw_v
                        break
            except Exception:
                pass
        slug = _slugify(r["title"] or "") or f"field_{r['id']}"
        out[f"ticket.cf.{slug}"] = (disp or "")
        out[f"ticket.cf.{r['id']}"] = (disp or "")
    return out


# =============================================================================
# Interpolation — replace {{key}} with values
# =============================================================================
_PH_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}")


def interpolate(template: str, vars: dict) -> str:
    """Replace every {{ticket.foo.bar}} in `template` with the matching value in
    `vars`. Unknown keys are left intact so the user can spot typos."""
    if not template:
        return template or ""
    def repl(m):
        key = m.group(1)
        if key in vars:
            v = vars[key]
            if v is None: return ""
            return str(v)
        return m.group(0)
    return _PH_RE.sub(repl, template)
