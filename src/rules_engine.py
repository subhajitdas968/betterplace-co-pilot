"""Rules engine — evaluates conditions and runs actions for Triggers + Schedulers.

Entry points:
  - dispatch_event(c, event_key, ticket_id, *, actor_email, extra_context=None)
        Called by write paths in app.py. Finds all active trigger rules for
        this event, evaluates each rule's conditions against the live ticket,
        runs the actions of any rule that passes, audits everything.

  - evaluate(c, rule, ticket_id)
        Returns (passes: bool, condition_breakdown: list).
        Used by both dispatch (passes-only check) and the visual-test UI
        (full breakdown).

  - execute_actions(c, rule, ticket_id, *, actor_email, dry_run=False)
        Returns list of {action, params, ok, summary, error?}.
        dry_run=True doesn't write to the DB — used by the visual-test "preview".

Action registry:
  Each action type is one function: action_<name>(c, ticket_id, params, ctx) -> dict.
  Returns {ok, summary, after_value?, error?}. Side-effects go through normal
  db.* helpers so the audit log captures them too.
"""
from __future__ import annotations
import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable

from . import db


# =============================================================================
# Loading rules (with in-process TTL cache)
# =============================================================================
_RULES_CACHE: dict[str, tuple[float, list[dict]]] = {}
_RULES_TTL = 30.0   # seconds — admin edits become visible within 30s


def _load_active_rules(c: sqlite3.Connection, category: str) -> list[dict]:
    import time as _t
    hit = _RULES_CACHE.get(category)
    now = _t.monotonic()
    if hit and (now - hit[0] < _RULES_TTL):
        return hit[1]
    rows = c.execute("""
        SELECT * FROM automations
        WHERE active=1 AND COALESCE(category,'trigger') = ?
        ORDER BY position, id
    """, (category,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try: d["conditions"] = json.loads(d.get("conditions_json") or "[]")
        except Exception: d["conditions"] = []
        try: d["actions"] = json.loads(d.get("actions_json") or "[]")
        except Exception: d["actions"] = []
        try: d["trigger_params_obj"] = json.loads(d.get("trigger_params") or "{}")
        except Exception: d["trigger_params_obj"] = {}
        try: d["schedule_obj"] = json.loads(d.get("schedule_json") or "{}")
        except Exception: d["schedule_obj"] = {}
        out.append(d)
    _RULES_CACHE[category] = (now, out)
    return out


def invalidate_rules_cache() -> None:
    _RULES_CACHE.clear()


# =============================================================================
# Ticket context — read once, used by all conditions of a rule
# =============================================================================
def _ticket_context(c: sqlite3.Connection, ticket_id: int) -> dict:
    """Builds a flat dict the condition evaluator queries by `field` key."""
    t = c.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()
    if not t:
        return {}
    raw = {}
    try: raw = json.loads(t["raw"] or "{}")
    except Exception: pass
    cfs = db.effective_custom_fields(t)
    # Requester
    requester = c.execute("SELECT email, name FROM users WHERE id=?", (t["requester_id"],)).fetchone() if t["requester_id"] else None
    req_email = (requester["email"] if requester else "") or ""
    req_domain = req_email.split("@", 1)[-1] if "@" in req_email else ""
    # Comments / replies
    cmts = c.execute("""
        SELECT tc.id, tc.public, tc.created_at, u.role
        FROM ticket_comments tc LEFT JOIN users u ON u.id=tc.author_id
        WHERE tc.ticket_id=? ORDER BY tc.created_at
    """, (ticket_id,)).fetchall()
    public_reply_count = sum(1 for r in cmts if r["public"])
    internal_note_count = sum(1 for r in cmts if not r["public"])
    last_replier = None
    if cmts:
        last = cmts[-1]
        if last["role"] in ("agent", "admin"):
            last_replier = "agent"
        elif last["role"]:
            last_replier = "customer"
        else:
            last_replier = "customer"   # unknown role defaults to customer (typical for email submitters)
    else:
        last_replier = "none"
    # Hours since
    def _hrs_since(ts):
        if not ts: return None
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0
        except Exception:
            return None
    last_cust_reply = None; last_agent_reply = None
    for r in cmts:
        if r["role"] in ("agent", "admin"):
            last_agent_reply = r["created_at"]
        else:
            last_cust_reply = r["created_at"]
    # SLA snapshot
    sla = c.execute("SELECT * FROM ticket_sla WHERE ticket_id=?", (ticket_id,)).fetchone()
    sla_d = dict(sla) if sla else {}
    # AI insight
    ai = c.execute("SELECT * FROM ticket_insights WHERE ticket_id=? ORDER BY id DESC LIMIT 1", (ticket_id,)).fetchone()
    ai_d = dict(ai) if ai else {}
    try: ai_recs = json.loads(ai_d.get("recommendations") or "[]")
    except Exception: ai_recs = []
    try: ai_pickup = json.loads(ai_d.get("pickup_flag") or "null") or None
    except Exception: ai_pickup = None
    # Attachments
    att_count = c.execute("SELECT COUNT(*) AS n FROM ticket_attachments WHERE ticket_id=?", (ticket_id,)).fetchone()["n"]

    # Day of week (Mon=0 in Python, but our catalog uses Sun=0)
    now = datetime.now(timezone.utc)
    py_dow = now.weekday()  # Mon=0..Sun=6
    cat_dow = str((py_dow + 1) % 7)  # Sun=0..Sat=6

    return {
        # Ticket
        "status":        (t["status"] or ""),
        "priority":      (t["priority"] or ""),
        "type":          (t["type"] or ""),
        "group_id":      t["group_id"],
        "assignee_id":   t["assignee_id"],
        "form_id":       raw.get("ticket_form_id"),
        "tags":          json.loads(t["tags"] or "[]"),
        "subject":       t["subject"] or "",
        "description":   "",   # description lives as the first comment — populated below if needed
        "source":        t["source"] or "zendesk",
        "custom_status_id": raw.get("custom_status_id"),
        # Custom field values (keyed by field id as string)
        "_custom_fields": cfs,
        # Customer
        "customer":          cfs.get("15315331275025"),
        "requester_email":   req_email,
        "requester_domain":  req_domain,
        "organization_id":   t["organization_id"],
        # Time
        "created_at":   t["created_at"] or "",
        "updated_at":   t["updated_at"] or "",
        "solved_at":    t["solved_at"] or "",
        "hours_since_created": _hrs_since(t["created_at"]),
        "hours_since_updated": _hrs_since(t["updated_at"]),
        "hours_since_last_customer_reply": _hrs_since(last_cust_reply),
        "hours_since_last_agent_reply":    _hrs_since(last_agent_reply),
        "now.day_of_week": cat_dow,
        "within_business_hours": True,   # TODO: thread through sla.business_hours
        # Conversation
        "public_reply_count":  public_reply_count,
        "internal_note_count": internal_note_count,
        "attachment_count":    att_count,
        "last_replier":        last_replier,
        # SLA
        "sla.first_reply_state": sla_d.get("first_reply_state"),
        "sla.next_reply_state":  sla_d.get("next_reply_state"),
        "sla.resolution_state":  sla_d.get("resolution_state"),
        "sla.policy_id":         sla_d.get("policy_id"),
        # AI
        "ai.has_insight":             bool(ai_d),
        "ai.recommendations_count":   len(ai_recs),
        "ai.kb_worthy":               bool(ai_d.get("kb_worthy")),
        "ai.pickup_flag":             bool(ai_pickup),
        "ai.summary_contains":        (ai_d.get("summary") or ""),
        # Raw refs
        "_ticket":  dict(t),
        "_raw":     raw,
    }


# =============================================================================
# Operator engine
# =============================================================================
def _coerce_str(v) -> str:
    if v is None: return ""
    return str(v)


def _eval_op(actual, op: str, expected) -> bool:
    """Single condition evaluator. `actual` is whatever the ticket has, `expected`
    is whatever the rule says."""
    if op == "is" or op == "eq":
        return _coerce_str(actual) == _coerce_str(expected)
    if op == "is_not" or op == "neq":
        return _coerce_str(actual) != _coerce_str(expected)
    if op == "in":
        opts = _split_list(expected)
        return _coerce_str(actual) in opts
    if op == "not_in":
        opts = _split_list(expected)
        return _coerce_str(actual) not in opts
    if op == "contains":
        return _coerce_str(expected).lower() in _coerce_str(actual).lower()
    if op == "not_contains":
        return _coerce_str(expected).lower() not in _coerce_str(actual).lower()
    if op == "starts_with":
        return _coerce_str(actual).lower().startswith(_coerce_str(expected).lower())
    if op == "ends_with":
        return _coerce_str(actual).lower().endswith(_coerce_str(expected).lower())
    if op == "regex":
        try: return bool(re.search(expected or "", _coerce_str(actual)))
        except Exception: return False
    if op == "is_empty":
        return not _coerce_str(actual).strip()
    if op == "is_not_empty":
        return bool(_coerce_str(actual).strip())
    if op == "gt":  return _to_num(actual) >  _to_num(expected)
    if op == "gte": return _to_num(actual) >= _to_num(expected)
    if op == "lt":  return _to_num(actual) <  _to_num(expected)
    if op == "lte": return _to_num(actual) <= _to_num(expected)
    if op == "between":
        lo, hi = _split_range(expected)
        return _to_num(lo) <= _to_num(actual) <= _to_num(hi)
    if op == "is_true":  return bool(actual)
    if op == "is_false": return not bool(actual)
    if op == "is_set":   return actual not in (None, "", 0, [])
    if op == "is_unset": return actual in (None, "", [])
    if op == "has":      return _coerce_str(expected) in (actual or [])
    if op == "has_any":  return any(t in (actual or []) for t in _split_list(expected))
    if op == "has_all":  return all(t in (actual or []) for t in _split_list(expected))
    if op == "has_none": return not any(t in (actual or []) for t in _split_list(expected))
    if op == "before":
        return _to_dt(actual) and _to_dt(expected) and _to_dt(actual) < _to_dt(expected)
    if op == "after":
        return _to_dt(actual) and _to_dt(expected) and _to_dt(actual) > _to_dt(expected)
    if op == "within_last":
        amt, unit = _split_duration(expected)
        hrs = _duration_to_hours(amt, unit)
        if actual is None or hrs is None: return False
        return float(actual) <= hrs
    if op == "older_than":
        amt, unit = _split_duration(expected)
        hrs = _duration_to_hours(amt, unit)
        if actual is None or hrs is None: return False
        return float(actual) > hrs
    # Special agent ops
    if op == "is_current_user":
        return False   # filled in by caller with current_user context
    if op == "is_in_group":
        return False   # similar — needs special context
    return False


def _to_num(v) -> float:
    try: return float(v)
    except (TypeError, ValueError): return 0.0


def _to_dt(v):
    if not v: return None
    try: return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except Exception: return None


def _split_list(v):
    if isinstance(v, list): return [str(x) for x in v]
    return [s.strip() for s in str(v or "").split(",") if s.strip()]


def _split_range(v):
    if isinstance(v, (list, tuple)) and len(v) >= 2: return v[0], v[1]
    s = str(v or "")
    if ".." in s: a, b = s.split("..", 1); return a, b
    return s, s


def _split_duration(v):
    if isinstance(v, dict):
        return v.get("amount") or 0, v.get("unit") or "hours"
    s = str(v or "").strip()
    m = re.match(r"(\d+(?:\.\d+)?)\s*(minutes|hours|days)?", s)
    if not m: return 0, "hours"
    return float(m.group(1)), (m.group(2) or "hours")


def _duration_to_hours(amount, unit):
    a = _to_num(amount)
    return {"minutes": a/60, "hours": a, "days": a*24}.get(unit, a)


# =============================================================================
# Condition resolution
# =============================================================================
def _resolve_condition_value(cond: dict, ctx: dict) -> tuple[str, Any]:
    """Return (label, actual_value_from_ticket) for a condition row.
    Labels are used in the test breakdown ('Status', 'Customer Name', etc.)."""
    field = cond.get("field") or ""
    if field == "custom_field":
        fid = cond.get("custom_field_id")
        if fid is None: return "(no custom field selected)", None
        val = ctx.get("_custom_fields", {}).get(str(fid))
        return f"custom field #{fid}", val
    val = ctx.get(field)
    # Pretty labels for common fields
    pretty = {
        "status": "Status", "priority": "Priority", "type": "Type",
        "group_id": "Group", "assignee_id": "Assignee", "form_id": "Form",
        "tags": "Tags", "subject": "Subject", "description": "Description",
        "source": "Source", "custom_status_id": "Custom status",
        "customer": "Customer", "requester_email": "Requester email",
        "requester_domain": "Requester email domain",
        "organization_id": "Organization",
        "created_at": "Created at", "updated_at": "Updated at", "solved_at": "Solved at",
        "hours_since_created": "Hours since created",
        "hours_since_updated": "Hours since last update",
        "hours_since_last_customer_reply": "Hours since last customer reply",
        "hours_since_last_agent_reply":    "Hours since last agent reply",
        "now.day_of_week": "Day of week",
        "within_business_hours": "Within business hours",
        "public_reply_count": "Public reply count",
        "internal_note_count": "Internal note count",
        "attachment_count": "Attachment count",
        "last_replier": "Last replier",
        "sla.first_reply_state": "SLA first-reply state",
        "sla.next_reply_state":  "SLA next-reply state",
        "sla.resolution_state":  "SLA resolution state",
        "sla.policy_id": "SLA policy",
        "ai.has_insight": "Has AI insight",
        "ai.recommendations_count": "AI recommendation count",
        "ai.kb_worthy": "AI KB-worthy",
        "ai.pickup_flag": "AI pickup flag",
        "ai.summary_contains": "AI summary",
    }
    return pretty.get(field, field), val


def evaluate(c: sqlite3.Connection, rule: dict, ticket_id: int,
             ctx: dict | None = None) -> tuple[bool, list[dict]]:
    """Run every condition. Returns (all_passed, breakdown).
    breakdown is a list of {field, op, expected, actual, passed, label}.
    Empty conditions list = always passes."""
    if ctx is None:
        ctx = _ticket_context(c, ticket_id)
    breakdown = []
    all_passed = True
    for cond in (rule.get("conditions") or []):
        label, actual = _resolve_condition_value(cond, ctx)
        op = cond.get("op", "is")
        expected = cond.get("value", "")
        try:
            passed = _eval_op(actual, op, expected)
        except Exception as e:
            passed = False
            breakdown.append({"field": cond.get("field"), "label": label,
                              "op": op, "expected": expected,
                              "actual": _coerce_str(actual),
                              "passed": False, "error": str(e)})
            all_passed = False
            continue
        breakdown.append({"field": cond.get("field"), "label": label,
                          "op": op, "expected": expected,
                          "actual": _coerce_str(actual), "passed": passed})
        if not passed:
            all_passed = False
    return all_passed, breakdown


# =============================================================================
# Action registry
# =============================================================================
ActionFn = Callable[[sqlite3.Connection, int, dict, dict], dict]
_ACTION_REGISTRY: dict[str, ActionFn] = {}


def action(key: str):
    def deco(fn: ActionFn):
        _ACTION_REGISTRY[key] = fn
        return fn
    return deco


def _audit_field_change(c, ticket_id: int, *, actor_email: str, field_key: str,
                       before, after, summary: str, raw=None):
    db.audit_ticket(c, ticket_id=ticket_id, event_type="field.changed",
                    event_summary=summary, actor_email=actor_email,
                    actor_type=("automation" if actor_email.startswith("automation:") else "agent"),
                    field_key=field_key, before=before, after=after, raw=raw)


# ---- Field changes ----------------------------------------------------------

@action("set_status")
def _set_status(c, ticket_id, p, ctx):
    new = (p or {}).get("status") or ""
    if not new: return {"ok": False, "error": "no status given"}
    row = c.execute("SELECT status FROM tickets WHERE id=?", (ticket_id,)).fetchone()
    before = row["status"] if row else None
    if not ctx.get("dry_run"):
        c.execute("UPDATE tickets SET status=?, updated_at=? WHERE id=?", (new, db.now_iso(), ticket_id))
        _audit_field_change(c, ticket_id, actor_email=ctx["actor_email"],
                            field_key="status", before=before, after=new,
                            summary=f"Status: {before} → {new}")
    return {"ok": True, "summary": f"Status {before} → {new}", "before": before, "after": new}


@action("set_priority")
def _set_priority(c, ticket_id, p, ctx):
    new = (p or {}).get("priority") or ""
    if not new: return {"ok": False, "error": "no priority given"}
    row = c.execute("SELECT priority FROM tickets WHERE id=?", (ticket_id,)).fetchone()
    before = row["priority"] if row else None
    if not ctx.get("dry_run"):
        c.execute("UPDATE tickets SET priority=?, updated_at=? WHERE id=?", (new, db.now_iso(), ticket_id))
        _audit_field_change(c, ticket_id, actor_email=ctx["actor_email"],
                            field_key="priority", before=before, after=new,
                            summary=f"Priority: {before} → {new}")
    return {"ok": True, "summary": f"Priority {before} → {new}", "before": before, "after": new}


@action("set_group")
def _set_group(c, ticket_id, p, ctx):
    gid = (p or {}).get("group_id")
    if not gid: return {"ok": False, "error": "no group_id"}
    try: gid = int(gid)
    except (TypeError, ValueError): return {"ok": False, "error": f"bad group_id: {gid}"}
    row = c.execute("SELECT group_id FROM tickets WHERE id=?", (ticket_id,)).fetchone()
    before = row["group_id"] if row else None
    g = c.execute("SELECT name FROM groups WHERE id=?", (gid,)).fetchone()
    new_name = g["name"] if g else gid
    if not ctx.get("dry_run"):
        c.execute("UPDATE tickets SET group_id=?, updated_at=? WHERE id=?", (gid, db.now_iso(), ticket_id))
        _audit_field_change(c, ticket_id, actor_email=ctx["actor_email"],
                            field_key="group_id", before=before, after=gid,
                            summary=f"Group → {new_name}")
    return {"ok": True, "summary": f"Group → {new_name}", "before": before, "after": gid}


@action("set_assignee")
def _set_assignee(c, ticket_id, p, ctx):
    agent = (p or {}).get("agent") or ""
    row = c.execute("SELECT assignee_id, group_id FROM tickets WHERE id=?", (ticket_id,)).fetchone()
    before = row["assignee_id"] if row else None
    aid = None
    if agent == "__unassign__":
        aid = None
    elif agent == "__current_user__":
        # actor must be a user with a row
        u = c.execute("SELECT id FROM users WHERE LOWER(email)=LOWER(?)", (ctx["actor_email"],)).fetchone()
        aid = u["id"] if u else None
    elif agent == "__round_robin__":
        if row and row["group_id"]:
            aid = db.pick_next_agent_for_group(c, row["group_id"])
    else:
        try: aid = int(agent)
        except (TypeError, ValueError): aid = None
    if not ctx.get("dry_run"):
        c.execute("UPDATE tickets SET assignee_id=?, updated_at=? WHERE id=?", (aid, db.now_iso(), ticket_id))
        _audit_field_change(c, ticket_id, actor_email=ctx["actor_email"],
                            field_key="assignee_id", before=before, after=aid,
                            summary=f"Assignee → {aid if aid else 'unassigned'}")
    return {"ok": True, "summary": f"Assignee → {aid if aid else 'unassigned'}", "before": before, "after": aid}


@action("set_custom_field")
def _set_custom_field(c, ticket_id, p, ctx):
    fid = (p or {}).get("field_id")
    val = (p or {}).get("value", "")
    if not fid: return {"ok": False, "error": "no field_id"}
    f = c.execute("SELECT title, type, options FROM ticket_fields WHERE id=?", (int(fid),)).fetchone()
    if not f: return {"ok": False, "error": f"unknown field {fid}"}
    cfs = db.effective_custom_fields(c.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone())
    before = cfs.get(str(fid))
    if not ctx.get("dry_run"):
        db.set_local_field_override(c, ticket_id, fid, val)
        _audit_field_change(c, ticket_id, actor_email=ctx["actor_email"],
                            field_key=str(fid), before=before, after=val,
                            summary=f"{f['title']}: {before or '∅'} → {val or '∅'}")
    return {"ok": True, "summary": f"{f['title']}: {before or '∅'} → {val or '∅'}",
            "before": before, "after": val}


@action("clear_field")
def _clear_field(c, ticket_id, p, ctx):
    return _set_custom_field(c, ticket_id, {**(p or {}), "value": ""}, ctx)


@action("add_tag")
def _add_tag(c, ticket_id, p, ctx):
    tag = (p or {}).get("tag") or ""
    if not tag: return {"ok": False, "error": "no tag"}
    row = c.execute("SELECT tags FROM tickets WHERE id=?", (ticket_id,)).fetchone()
    tags = json.loads(row["tags"] or "[]") if row else []
    if tag in tags:
        return {"ok": True, "summary": f"tag '{tag}' already present"}
    new_tags = tags + [tag]
    if not ctx.get("dry_run"):
        c.execute("UPDATE tickets SET tags=?, updated_at=? WHERE id=?",
                  (json.dumps(new_tags), db.now_iso(), ticket_id))
        db.audit_ticket(c, ticket_id=ticket_id, event_type="tag.added",
                        event_summary=f"+ tag '{tag}'", actor_email=ctx["actor_email"],
                        actor_type="automation" if ctx["actor_email"].startswith("automation:") else "agent",
                        field_key="tags", before=tags, after=new_tags)
    return {"ok": True, "summary": f"+ tag '{tag}'", "before": tags, "after": new_tags}


@action("remove_tag")
def _remove_tag(c, ticket_id, p, ctx):
    tag = (p or {}).get("tag") or ""
    row = c.execute("SELECT tags FROM tickets WHERE id=?", (ticket_id,)).fetchone()
    tags = json.loads(row["tags"] or "[]") if row else []
    if tag not in tags:
        return {"ok": True, "summary": f"tag '{tag}' not present"}
    new_tags = [t for t in tags if t != tag]
    if not ctx.get("dry_run"):
        c.execute("UPDATE tickets SET tags=?, updated_at=? WHERE id=?",
                  (json.dumps(new_tags), db.now_iso(), ticket_id))
        db.audit_ticket(c, ticket_id=ticket_id, event_type="tag.removed",
                        event_summary=f"− tag '{tag}'", actor_email=ctx["actor_email"],
                        actor_type="automation" if ctx["actor_email"].startswith("automation:") else "agent",
                        field_key="tags", before=tags, after=new_tags)
    return {"ok": True, "summary": f"− tag '{tag}'", "before": tags, "after": new_tags}


# ---- Conversation -----------------------------------------------------------

@action("add_internal_note")
def _add_internal_note(c, ticket_id, p, ctx):
    body = (p or {}).get("body") or ""
    body = _interpolate(body, ctx.get("placeholders") or _ticket_placeholders(c, ticket_id, ctx.get("actor_email") or "system"))
    if not body.strip(): return {"ok": False, "error": "empty body"}
    if ctx.get("dry_run"):
        return {"ok": True, "summary": f"would post internal note: {body[:80]}…"}
    # Author: a system pseudo-user (id 1_000_000_999) so audits don't show as the agent
    u_id = _system_user_id(c, ctx.get("actor_email") or "system")
    cid = db.insert_native_comment(c, ticket_id=ticket_id, author_id=u_id,
                                    body=body, public=False, body_format="markdown")
    db.audit_ticket(c, ticket_id=ticket_id, event_type="note.added",
                    event_summary=f"Internal note: {body[:60]}…",
                    actor_email=ctx["actor_email"], actor_type="automation",
                    raw={"comment_id": cid, "body": body})
    return {"ok": True, "summary": f"Internal note posted (cid={cid})"}


@action("add_public_reply")
def _add_public_reply(c, ticket_id, p, ctx):
    body = (p or {}).get("body") or ""
    body = _interpolate(body, ctx.get("placeholders") or _ticket_placeholders(c, ticket_id, ctx.get("actor_email") or "system"))
    if not body.strip(): return {"ok": False, "error": "empty body"}
    if ctx.get("dry_run"):
        return {"ok": True, "summary": f"would post public reply: {body[:80]}…"}
    u_id = _system_user_id(c, ctx.get("actor_email") or "system")
    cid = db.insert_native_comment(c, ticket_id=ticket_id, author_id=u_id,
                                    body=body, public=True, body_format="markdown")
    db.audit_ticket(c, ticket_id=ticket_id, event_type="comment.public",
                    event_summary=f"Public reply: {body[:60]}…",
                    actor_email=ctx["actor_email"], actor_type="automation",
                    raw={"comment_id": cid, "body": body})
    return {"ok": True, "summary": f"Public reply posted (cid={cid})"}


@action("send_auto_reply")
def _send_auto_reply(c, ticket_id, p, ctx):
    rid = (p or {}).get("auto_reply_id")
    if not rid: return {"ok": False, "error": "no auto_reply_id"}
    row = c.execute("SELECT body, name FROM auto_replies WHERE id=?", (int(rid),)).fetchone()
    if not row: return {"ok": False, "error": f"auto-reply #{rid} not found"}
    body = _interpolate(row["body"], _ticket_placeholders(c, ticket_id, ctx.get("actor_email") or "system"))
    if ctx.get("dry_run"):
        return {"ok": True, "summary": f"would send auto-reply '{row['name']}'"}
    u_id = _system_user_id(c, ctx.get("actor_email") or "system")
    cid = db.insert_native_comment(c, ticket_id=ticket_id, author_id=u_id,
                                    body=body, public=True, body_format="markdown")
    c.execute("UPDATE auto_replies SET sent_count = COALESCE(sent_count,0) + 1 WHERE id=?", (int(rid),))
    db.audit_ticket(c, ticket_id=ticket_id, event_type="auto_reply.sent",
                    event_summary=f"Auto-reply '{row['name']}' sent",
                    actor_email=ctx["actor_email"], actor_type="automation",
                    raw={"comment_id": cid, "auto_reply_id": rid})
    return {"ok": True, "summary": f"Auto-reply '{row['name']}' sent"}


# ---- AI ---------------------------------------------------------------------

@action("request_ai_insight")
def _request_ai_insight(c, ticket_id, p, ctx):
    if ctx.get("dry_run"):
        return {"ok": True, "summary": "would queue ticket for AI analysis"}
    # Reset last_analyzed_updated_at so the worker picks it up on the next cycle
    c.execute("UPDATE tickets SET last_analyzed_updated_at=NULL WHERE id=?", (ticket_id,))
    db.audit_ticket(c, ticket_id=ticket_id, event_type="ai.queued",
                    event_summary="Queued for AI analysis",
                    actor_email=ctx["actor_email"], actor_type="automation")
    return {"ok": True, "summary": "Queued for AI analysis on next worker cycle"}


# ---- External ---------------------------------------------------------------

# ---- Workflow ---------------------------------------------------------------

@action("close_ticket")
def _close_ticket(c, ticket_id, p, ctx):
    note = (p or {}).get("note") or ""
    row = c.execute("SELECT status FROM tickets WHERE id=?", (ticket_id,)).fetchone()
    before = row["status"] if row else None
    if not ctx.get("dry_run"):
        c.execute("UPDATE tickets SET status='closed', solved_at=COALESCE(solved_at, ?), updated_at=? WHERE id=?",
                  (db.now_iso(), db.now_iso(), ticket_id))
        _audit_field_change(c, ticket_id, actor_email=ctx["actor_email"],
                            field_key="status", before=before, after="closed",
                            summary=f"Closed{(': '+note) if note else ''}")
        if note:
            uid = _system_user_id(c, ctx.get("actor_email") or "system")
            db.insert_native_comment(c, ticket_id=ticket_id, author_id=uid,
                                      body=note, public=False, body_format="markdown")
    return {"ok": True, "summary": f"Closed (was {before})"}


@action("reopen_ticket")
def _reopen_ticket(c, ticket_id, p, ctx):
    row = c.execute("SELECT status FROM tickets WHERE id=?", (ticket_id,)).fetchone()
    before = row["status"] if row else None
    if not ctx.get("dry_run"):
        c.execute("UPDATE tickets SET status='open', solved_at=NULL, updated_at=? WHERE id=?",
                  (db.now_iso(), ticket_id))
        _audit_field_change(c, ticket_id, actor_email=ctx["actor_email"],
                            field_key="status", before=before, after="open",
                            summary=f"Reopened (was {before})")
    return {"ok": True, "summary": f"Reopened (was {before})"}


@action("snooze_until")
def _snooze_until(c, ticket_id, p, ctx):
    try: hours = int((p or {}).get("hours") or 24)
    except (TypeError, ValueError): hours = 24
    row = c.execute("SELECT status FROM tickets WHERE id=?", (ticket_id,)).fetchone()
    before = row["status"] if row else None
    if not ctx.get("dry_run"):
        c.execute("UPDATE tickets SET status='hold', updated_at=? WHERE id=?",
                  (db.now_iso(), ticket_id))
        db.audit_ticket(c, ticket_id=ticket_id, event_type="ticket.snoozed",
                        event_summary=f"Snoozed for {hours}h", actor_email=ctx["actor_email"],
                        actor_type=("automation" if ctx["actor_email"].startswith("automation:") else "agent"),
                        before=before, after="hold", raw={"hours": hours})
    return {"ok": True, "summary": f"Snoozed for {hours}h (status now hold)"}


@action("set_custom_status")
def _set_custom_status(c, ticket_id, p, ctx):
    csid = (p or {}).get("custom_status_id")
    if not csid: return {"ok": False, "error": "no custom_status_id"}
    cs = c.execute("SELECT agent_label, status_category FROM custom_statuses WHERE id=?", (int(csid),)).fetchone()
    if not cs: return {"ok": False, "error": f"unknown custom status {csid}"}
    # Pull current raw to write the override
    t = c.execute("SELECT raw, local_overrides FROM tickets WHERE id=?", (ticket_id,)).fetchone()
    raw = json.loads(t["raw"] or "{}")
    before = raw.get("custom_status_id")
    if not ctx.get("dry_run"):
        # Store custom_status under local_overrides — sync never clobbers
        ov = json.loads(t["local_overrides"] or "{}")
        ov["custom_status_id"] = int(csid)
        c.execute("UPDATE tickets SET local_overrides=?, status=?, updated_at=? WHERE id=?",
                  (json.dumps(ov), cs["status_category"] or "open", db.now_iso(), ticket_id))
        db.audit_ticket(c, ticket_id=ticket_id, event_type="status.changed",
                        event_summary=f"Custom status → {cs['agent_label']}",
                        actor_email=ctx["actor_email"],
                        actor_type=("automation" if ctx["actor_email"].startswith("automation:") else "agent"),
                        field_key="custom_status_id", before=before, after=int(csid))
    return {"ok": True, "summary": f"Custom status → {cs['agent_label']}"}


@action("set_type")
def _set_type(c, ticket_id, p, ctx):
    t = (p or {}).get("type") or ""
    if not t: return {"ok": False, "error": "no type"}
    row = c.execute("SELECT type FROM tickets WHERE id=?", (ticket_id,)).fetchone()
    before = row["type"] if row else None
    if not ctx.get("dry_run"):
        c.execute("UPDATE tickets SET type=?, updated_at=? WHERE id=?", (t, db.now_iso(), ticket_id))
        _audit_field_change(c, ticket_id, actor_email=ctx["actor_email"],
                            field_key="type", before=before, after=t, summary=f"Type → {t}")
    return {"ok": True, "summary": f"Type {before} → {t}"}


@action("set_form")
def _set_form(c, ticket_id, p, ctx):
    fid = (p or {}).get("form_id")
    if not fid: return {"ok": False, "error": "no form_id"}
    t = c.execute("SELECT raw, local_overrides FROM tickets WHERE id=?", (ticket_id,)).fetchone()
    raw = json.loads(t["raw"] or "{}")
    before = raw.get("ticket_form_id")
    if not ctx.get("dry_run"):
        ov = json.loads(t["local_overrides"] or "{}")
        ov["ticket_form_id"] = int(fid)
        c.execute("UPDATE tickets SET local_overrides=?, updated_at=? WHERE id=?",
                  (json.dumps(ov), db.now_iso(), ticket_id))
        _audit_field_change(c, ticket_id, actor_email=ctx["actor_email"],
                            field_key="ticket_form_id", before=before, after=int(fid),
                            summary=f"Form → #{fid}")
    return {"ok": True, "summary": f"Form → #{fid}"}


@action("link_to_ticket")
def _link_to_ticket(c, ticket_id, p, ctx):
    other = (p or {}).get("ticket_id")
    link_type = (p or {}).get("link_type") or "related"
    if not other: return {"ok": False, "error": "no ticket_id"}
    if ctx.get("dry_run"):
        return {"ok": True, "summary": f"would link {ticket_id} → {other} ({link_type})"}
    db.audit_ticket(c, ticket_id=ticket_id, event_type="ticket.linked",
                    event_summary=f"Linked {link_type} → {other}",
                    actor_email=ctx["actor_email"], actor_type="automation",
                    raw={"other_ticket_id": other, "link_type": link_type})
    db.audit_ticket(c, ticket_id=int(other), event_type="ticket.linked",
                    event_summary=f"Linked {link_type} ← {ticket_id}",
                    actor_email=ctx["actor_email"], actor_type="automation",
                    raw={"other_ticket_id": ticket_id, "link_type": link_type, "reverse": True})
    return {"ok": True, "summary": f"Linked {link_type} → {other}"}


@action("escalate_to_manager")
def _escalate(c, ticket_id, p, ctx):
    note = (p or {}).get("note") or "Escalated by automation"
    row = c.execute("SELECT priority FROM tickets WHERE id=?", (ticket_id,)).fetchone()
    before = row["priority"] if row else None
    new_priority = "urgent" if before in ("low", "normal", None, "") else "urgent"
    if not ctx.get("dry_run"):
        c.execute("UPDATE tickets SET priority=?, updated_at=? WHERE id=?",
                  (new_priority, db.now_iso(), ticket_id))
        uid = _system_user_id(c, ctx.get("actor_email") or "system")
        db.insert_native_comment(c, ticket_id=ticket_id, author_id=uid,
                                  body=f"**ESCALATED**: {note}", public=False, body_format="markdown")
        db.audit_ticket(c, ticket_id=ticket_id, event_type="ticket.escalated",
                        event_summary=f"Escalated: {note}", actor_email=ctx["actor_email"],
                        actor_type="automation", before=before, after=new_priority)
    return {"ok": True, "summary": f"Escalated (priority {before} → {new_priority})"}


# ---- Notifications (best-effort — Slack/email need a transport) ------------

@action("notify_assignee_email")
def _notify_assignee_email(c, ticket_id, p, ctx):
    subject = _interpolate((p or {}).get("subject") or "", ctx.get("placeholders") or _ticket_placeholders(c, ticket_id, ctx.get("actor_email")))
    body = _interpolate((p or {}).get("body") or "", ctx.get("placeholders") or _ticket_placeholders(c, ticket_id, ctx.get("actor_email")))
    t = c.execute("SELECT assignee_id FROM tickets WHERE id=?", (ticket_id,)).fetchone()
    if not t or not t["assignee_id"]:
        return {"ok": False, "error": "ticket has no assignee"}
    u = c.execute("SELECT email FROM users WHERE id=?", (t["assignee_id"],)).fetchone()
    to = u["email"] if u else None
    if not to:
        return {"ok": False, "error": "assignee has no email"}
    if ctx.get("dry_run"):
        return {"ok": True, "summary": f"would email {to}: {subject[:60]}"}
    sent = _send_email_best_effort(to, subject, body)
    db.audit_ticket(c, ticket_id=ticket_id, event_type="notification.email",
                    event_summary=f"Emailed assignee {to}: {subject[:60]}",
                    actor_email=ctx["actor_email"], actor_type="automation",
                    raw={"to": to, "subject": subject, "delivered": sent})
    return {"ok": sent, "summary": f"Emailed {to}" + (" ✓" if sent else " (transport not configured — captured to audit only)")}


@action("notify_requester_email")
def _notify_requester_email(c, ticket_id, p, ctx):
    subject = _interpolate((p or {}).get("subject") or "", ctx.get("placeholders") or _ticket_placeholders(c, ticket_id, ctx.get("actor_email")))
    body = _interpolate((p or {}).get("body") or "", ctx.get("placeholders") or _ticket_placeholders(c, ticket_id, ctx.get("actor_email")))
    t = c.execute("SELECT requester_id FROM tickets WHERE id=?", (ticket_id,)).fetchone()
    if not t or not t["requester_id"]:
        return {"ok": False, "error": "ticket has no requester"}
    u = c.execute("SELECT email FROM users WHERE id=?", (t["requester_id"],)).fetchone()
    to = u["email"] if u else None
    if not to:
        return {"ok": False, "error": "requester has no email"}
    if ctx.get("dry_run"):
        return {"ok": True, "summary": f"would email {to}: {subject[:60]}"}
    sent = _send_email_best_effort(to, subject, body)
    db.audit_ticket(c, ticket_id=ticket_id, event_type="notification.email",
                    event_summary=f"Emailed requester {to}: {subject[:60]}",
                    actor_email=ctx["actor_email"], actor_type="automation",
                    raw={"to": to, "subject": subject, "delivered": sent})
    return {"ok": sent, "summary": f"Emailed {to}" + (" ✓" if sent else " (transport not configured)")}


@action("mention_agent")
def _mention_agent(c, ticket_id, p, ctx):
    aid = (p or {}).get("agent_id")
    message = (p or {}).get("message") or ""
    if not aid: return {"ok": False, "error": "no agent_id"}
    u = c.execute("SELECT name, email FROM users WHERE id=?", (int(aid),)).fetchone()
    if not u: return {"ok": False, "error": f"agent {aid} not found"}
    body = f"@{u['name']} — {message}" if message else f"@{u['name']}"
    if ctx.get("dry_run"):
        return {"ok": True, "summary": f"would mention {u['name']}"}
    uid = _system_user_id(c, ctx.get("actor_email") or "system")
    db.insert_native_comment(c, ticket_id=ticket_id, author_id=uid,
                              body=body, public=False, body_format="markdown")
    db.audit_ticket(c, ticket_id=ticket_id, event_type="mention.added",
                    event_summary=f"@{u['name']} mentioned",
                    actor_email=ctx["actor_email"], actor_type="automation",
                    raw={"agent_id": aid, "agent_email": u["email"], "message": message})
    return {"ok": True, "summary": f"@{u['name']} mentioned"}


# ---- AI extras --------------------------------------------------------------

@action("generate_ai_summary")
def _generate_ai_summary(c, ticket_id, p, ctx):
    # Same wire as request_ai_insight — the worker will re-run analysis
    return _request_ai_insight(c, ticket_id, p, ctx)


@action("generate_kb_draft")
def _generate_kb_draft(c, ticket_id, p, ctx):
    if ctx.get("dry_run"):
        return {"ok": True, "summary": "would queue KB draft generation"}
    c.execute("UPDATE tickets SET last_analyzed_updated_at=NULL WHERE id=?", (ticket_id,))
    db.audit_ticket(c, ticket_id=ticket_id, event_type="ai.kb_queued",
                    event_summary="Queued for KB draft generation",
                    actor_email=ctx["actor_email"], actor_type="automation")
    return {"ok": True, "summary": "KB draft queued for next AI cycle"}


def _send_email_best_effort(to: str, subject: str, body: str) -> bool:
    """Try to deliver via SMTP if SMTP_HOST is configured; otherwise return False
    and the caller will log the intent to audit. We intentionally do NOT crash
    on missing config — automations should still record what they wanted to do."""
    import os, smtplib
    from email.mime.text import MIMEText
    host = os.getenv("SMTP_HOST")
    if not host:
        return False
    try:
        port = int(os.getenv("SMTP_PORT", "587"))
        user = os.getenv("SMTP_USER")
        pwd  = os.getenv("SMTP_PASS")
        sender = os.getenv("SMTP_FROM") or user or "noreply@local"
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"]    = sender
        msg["To"]      = to
        s = smtplib.SMTP(host, port, timeout=20)
        s.starttls()
        if user and pwd: s.login(user, pwd)
        s.sendmail(sender, [to], msg.as_string())
        s.quit()
        return True
    except Exception as e:
        print(f"[automation] SMTP send failed: {e}")
        return False


@action("call_webhook")
def _call_webhook(c, ticket_id, p, ctx):
    url = (p or {}).get("url") or ""
    method = ((p or {}).get("method") or "POST").upper()
    if not url: return {"ok": False, "error": "no url"}
    body = _interpolate((p or {}).get("body_template") or "", _ticket_placeholders(c, ticket_id, ctx.get("actor_email") or "system"))
    try: headers = json.loads((p or {}).get("headers_json") or "{}")
    except Exception: headers = {}
    if ctx.get("dry_run"):
        return {"ok": True, "summary": f"would {method} {url}", "preview_body": body[:200]}
    try:
        import requests as _rq
        r = _rq.request(method, url, headers=headers, data=body, timeout=10)
        ok = 200 <= r.status_code < 300
    except Exception as e:
        db.audit_ticket(c, ticket_id=ticket_id, event_type="webhook.failed",
                        event_summary=f"Webhook {method} {url} failed: {e}",
                        actor_email=ctx["actor_email"], actor_type="automation")
        return {"ok": False, "error": str(e), "summary": f"webhook {method} {url} failed"}
    db.audit_ticket(c, ticket_id=ticket_id, event_type="webhook.fired",
                    event_summary=f"{method} {url} → {r.status_code}",
                    actor_email=ctx["actor_email"], actor_type="automation",
                    raw={"status": r.status_code, "body_preview": r.text[:500]})
    return {"ok": ok, "summary": f"{method} {url} → {r.status_code}"}


# ---- Helpers ----------------------------------------------------------------

def _system_user_id(c, actor_email: str) -> int:
    """A pseudo system user id for automation-authored comments."""
    u = c.execute("SELECT id FROM users WHERE LOWER(email)=LOWER(?)", (actor_email,)).fetchone()
    if u: return u["id"]
    sid = 1_000_000_999
    c.execute("INSERT OR IGNORE INTO users (id, name, email, role, raw) VALUES (?, 'Automation', 'automation@local', 'agent', '{}')",
              (sid,))
    return sid


def _ticket_placeholders(c, ticket_id: int, current_user_email: str = "system") -> dict:
    """Resolve the full placeholder dict (delegates to src.placeholders so the
    autocomplete UI and the runtime substitution share the same keys)."""
    from . import placeholders as _ph
    return _ph.resolve(c, ticket_id, current_user_email=current_user_email)


def _interpolate(template: str, vars: dict) -> str:
    from . import placeholders as _ph
    return _ph.interpolate(template, vars)


# =============================================================================
# Execution
# =============================================================================
def execute_actions(c: sqlite3.Connection, rule: dict, ticket_id: int, *,
                    actor_email: str, dry_run: bool = False) -> list[dict]:
    """Run each action in the rule. Returns a list of result dicts so the
    visual-test UI can render the outcome of each step."""
    ctx = {
        "actor_email": actor_email,
        "dry_run": dry_run,
        "rule_id": rule.get("id"),
        "rule_name": rule.get("name"),
    }
    results = []
    for a in (rule.get("actions") or []):
        atype = a.get("type")
        params = a.get("params") or {}
        fn = _ACTION_REGISTRY.get(atype)
        if not fn:
            results.append({"action": atype, "params": params, "ok": False,
                            "error": f"action '{atype}' not implemented yet"})
            continue
        try:
            out = fn(c, ticket_id, params, ctx) or {}
        except Exception as e:
            out = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        results.append({"action": atype, "params": params, **out})
    # One audit entry for the whole rule firing
    if not dry_run and not rule.get("__suppress_rule_audit__"):
        db.audit_ticket(c, ticket_id=ticket_id, event_type="rule.fired",
                        event_summary=f"{rule.get('name')}",
                        actor_email=f"automation:{rule.get('id')}",
                        actor_type="automation",
                        raw={"results": results, "rule_id": rule.get("id")})
        c.execute("UPDATE automations SET run_count=COALESCE(run_count,0)+1, last_run_at=? WHERE id=?",
                  (db.now_iso(), rule["id"]))
    return results


# =============================================================================
# Dispatcher — call this from write paths
# =============================================================================
def dispatch_event(c: sqlite3.Connection, event_key: str, ticket_id: int, *,
                   actor_email: str = "system", extra_context: dict | None = None) -> dict:
    """Find every active trigger rule whose trigger_type matches event_key,
    evaluate, and run actions on the matched ones."""
    fired = []
    skipped = []
    rules = _load_active_rules(c, "trigger")
    ctx = _ticket_context(c, ticket_id)
    if extra_context: ctx.update(extra_context)
    for rule in rules:
        if (rule.get("trigger_type") or "") != event_key:
            continue
        passes, breakdown = evaluate(c, rule, ticket_id, ctx=ctx)
        if not passes:
            skipped.append({"id": rule["id"], "name": rule["name"], "reason": "conditions_failed"})
            continue
        results = execute_actions(c, rule, ticket_id, actor_email=actor_email)
        fired.append({"id": rule["id"], "name": rule["name"], "results": results})
    return {"event": event_key, "fired": fired, "skipped": skipped}
