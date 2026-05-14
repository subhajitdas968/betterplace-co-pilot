"""Translate Zendesk forms / triggers / automations into our native schema.

We pull the ZD definitions once, materialize them as native_forms /
automations rows, then *forget about ZD's copy*. From that point on every edit
the admin makes is local — the same as if they'd built the rule by hand. ZD
remains only for ticket sync (Block #1 policy).

Re-running an import is safe — we match on a stable "imported_from_zd_<id>"
naming convention and update the existing row instead of creating duplicates.
"""
from __future__ import annotations
import json
import sqlite3
from typing import Any

from . import db, zendesk


# ---- Forms -----------------------------------------------------------------

def import_forms_from_zd(c: sqlite3.Connection, *, actor_email: str = "system") -> dict:
    """For each ZD ticket_form, create or update a native_forms row carrying
    the same name and field_ids. The native version is what the agent edits
    going forward; ZD's copy is no longer authoritative."""
    forms = zendesk.list_ticket_forms() or []
    created, updated, skipped = 0, 0, 0
    # We tag native rows with a marker to avoid double-importing
    existing = {
        (r["name"] or ""): r for r in c.execute(
            "SELECT id, name FROM native_forms WHERE name LIKE 'ZD: %'"
        ).fetchall()
    }
    for zf in forms:
        zd_name = f"ZD: {zf.get('name') or zf.get('display_name') or '(unnamed)'}"
        field_ids = list(zf.get("ticket_field_ids") or [])
        # Pull each field's required flag to seed required_field_ids — we won't
        # know per-form requirements from ZD's API, so use the field-level flag.
        required_ids: list[int] = []
        if field_ids:
            placeholders = ",".join("?" * len(field_ids))
            for r in c.execute(
                f"SELECT id, required FROM ticket_fields WHERE id IN ({placeholders})",
                field_ids,
            ).fetchall():
                if r["required"]:
                    required_ids.append(r["id"])
        active = bool(zf.get("active"))
        prev = existing.get(zd_name)
        form_id = prev["id"] if prev else None
        new_id = db.upsert_native_form(
            c, form_id=form_id, name=zd_name,
            description=f"Imported from Zendesk form #{zf['id']}. Editable here — changes stay local.",
            active=active,
            group_ids=[],                    # ZD doesn't expose form↔group binding via this endpoint
            field_ids=field_ids,
            required_field_ids=required_ids,
            position=int(zf.get("position") or 100), actor_email=actor_email,
        )
        if prev:
            updated += 1
        else:
            created += 1
    return {"created": created, "updated": updated, "skipped": skipped, "total": len(forms)}


# ---- Triggers / Automations ------------------------------------------------

# =============================================================================
# Condition translation
# =============================================================================
# ZD operators → our op vocabulary
_ZD_OPERATOR_MAP = {
    "is":           "eq",
    "is_not":       "neq",
    "less_than":    "lt",
    "greater_than": "gt",
    "less_than_business_hours":   "lt",
    "greater_than_business_hours":"gt",
    "changed":      "is_set",     # ZD "changed" with no value → field is now set
    "not_changed":  "is_unset",
    "value":        "eq",         # used by ZD with hours fields — same semantics as is
    "value_previous":"neq",
    "present":      "is_set",
    "not_present":  "is_unset",
    "includes":     "in",
    "not_includes": "not_in",
    "includes_word":"contains",
    "not_includes_word":"not_contains",
    "includes_string":"contains",
    "not_includes_string":"not_contains",
}

# ZD field names → our condition field keys
_ZD_FIELD_MAP = {
    "status":          "status",
    "priority":        "priority",
    "type":            "type",
    "group_id":        "group_id",
    "assignee_id":     "assignee_id",
    "requester_id":    "requester_email",      # closest neighbour
    "organization_id": "organization_id",
    "ticket_form_id":  "form_id",
    "subject":         "subject",
    "description":     "description",
    "comment_text":    "ticket.last_comment",
    "current_tags":    "tags",
    "ticket_type_id":  "type",
    "hours_since_assigned":      "hours_since_updated",   # best mapping
    "hours_since_created":       "hours_since_created",
    "hours_since_updated":       "hours_since_updated",
    "hours_since_solved":        "hours_since_updated",
}


def _translate_conditions(zd_conditions: dict) -> list[dict]:
    """Flatten ZD conditions (AND'd "all" + OR'd "any") to our shape.
    Both buckets become AND for now — the user can re-arrange after import."""
    out = []
    for bucket in ("all", "any"):
        for cond in (zd_conditions or {}).get(bucket) or []:
            zd_field = cond.get("field") or ""
            zd_op    = cond.get("operator") or "is"
            value    = cond.get("value")
            # custom_fields_NNNN
            if isinstance(zd_field, str) and zd_field.startswith("custom_fields_"):
                try:
                    fid = int(zd_field.replace("custom_fields_", ""))
                    out.append({"field": "custom_field",
                                "custom_field_id": fid,
                                "op": _ZD_OPERATOR_MAP.get(zd_op, "eq"),
                                "value": value, "bucket": bucket})
                    continue
                except ValueError:
                    pass
            our_field = _ZD_FIELD_MAP.get(zd_field, zd_field)
            out.append({"field": our_field,
                        "op": _ZD_OPERATOR_MAP.get(zd_op, "eq"),
                        "value": value, "bucket": bucket})
    return out


# =============================================================================
# Action translation
# =============================================================================
# Each entry maps ZD action.field → (our_action_type, our_param_key)
# When our_param_key is None, the value is passed through `params['value']`.
_ZD_ACTION_DIRECT = {
    "status":          ("set_status",         "status"),
    "priority":        ("set_priority",       "priority"),
    "type":            ("set_type",           "type"),
    "ticket_type_id":  ("set_type",           "type"),
    "group_id":        ("set_group",          "group_id"),
    "assignee_id":     ("set_assignee",       "agent"),
    "ticket_form_id":  ("set_form",           "form_id"),
    "custom_status_id":("set_custom_status",  "custom_status_id"),
    "remove_tags":     ("remove_tag",         "tag"),
    "set_tags":        ("add_tag",            "tag"),
    "current_tags":    ("add_tag",            "tag"),     # ZD "set all tags" — closest single-tag analog
}


def _translate_zd_action(field: str, value) -> dict | None:
    """Map one ZD {field, value} action to our {type, params} shape."""
    f = str(field or "").strip()
    # Custom field
    if f.startswith("custom_fields_"):
        try:
            fid = int(f.replace("custom_fields_", ""))
            return {"type": "set_custom_field",
                    "params": {"field_id": fid, "value": value}}
        except ValueError:
            return {"type": "raw", "params": {"field": f, "value": value}}
    # Direct map
    if f in _ZD_ACTION_DIRECT:
        action_type, param_key = _ZD_ACTION_DIRECT[f]
        # ZD's set_assignee value may be the literal string "current_user" — keep it as-is
        return {"type": action_type, "params": {param_key: value}}
    # Comments — ZD emits [body, public_or_format]
    if f in ("comment_value", "comment_value_html"):
        body = value
        is_public = True
        if isinstance(value, list) and len(value) >= 2:
            # Form: [channel/mode, body]; sometimes [body, public_flag]
            if isinstance(value[1], str) and len(value[1]) > len(str(value[0] or "")):
                body = value[1]
            else:
                body = value[0]
            # ZD uses 'channel' first sometimes — peek for public/private string
            mode = str(value[0]).lower() if not isinstance(value[0], (int, list)) else ""
            if "private" in mode or "internal" in mode:
                is_public = False
        return {"type": "add_public_reply" if is_public else "add_internal_note",
                "params": {"body": body if isinstance(body, str) else (str(body) if body else "")}}
    if f == "comment_mode_is_public":
        # Paired metadata — we already inferred from comment_value
        return None
    # Notifications
    if f == "notification_user":
        # value is typically [recipient, subject, body]
        if isinstance(value, list) and len(value) >= 3:
            subj, body = str(value[1] or ""), str(value[2] or "")
            recipient = str(value[0] or "")
            # 'requester_id' / 'assignee_id' / 'current_user' / specific user id
            if "requester" in recipient:
                return {"type": "notify_requester_email", "params": {"subject": subj, "body": body}}
            return {"type": "notify_assignee_email", "params": {"subject": subj, "body": body}}
    if f == "notification_target":
        # value: [target_id, body_template] — translate into a webhook stub the
        # admin can finalize after import.
        if isinstance(value, list) and len(value) >= 2:
            return {"type": "call_webhook",
                    "params": {"url": f"(set URL — was ZD target #{value[0]})",
                               "method": "POST",
                               "body_template": str(value[1] or "")}}
    if f == "notification_group":
        if isinstance(value, list) and len(value) >= 3:
            return {"type": "notify_group_slack" if False else "notify_assignee_email",
                    "params": {"subject": str(value[1] or ""), "body": str(value[2] or "")}}
    if f == "satisfaction_score":
        return None
    # Recipient on a side conversation, etc — keep visible but harmless
    return {"type": "raw", "params": {"field": f, "value": value}}


def _translate_actions(zd_actions: list) -> list[dict]:
    out = []
    for a in zd_actions or []:
        translated = _translate_zd_action(a.get("field"), a.get("value"))
        if translated is not None:
            out.append(translated)
    return out


def import_triggers_from_zd(c: sqlite3.Connection, *, actor_email: str = "system") -> dict:
    """Each ZD trigger becomes a native automation with trigger_type='on_update'
    (closest semantic match — ZD triggers fire on ticket events)."""
    triggers = zendesk.list_triggers()
    created, updated = 0, 0
    existing_by_name = {
        r["name"]: r for r in c.execute(
            "SELECT id, name FROM automations WHERE name LIKE 'ZD trigger: %'"
        ).fetchall()
    }
    for t in triggers:
        name = f"ZD trigger: {t.get('title') or t.get('name') or '(unnamed)'}"
        conds = _translate_conditions(t.get("conditions") or {})
        actions = _translate_actions(t.get("actions") or [])
        prev = existing_by_name.get(name)
        aid = db.upsert_automation(
            c, automation_id=(prev["id"] if prev else None),
            name=name,
            description=f"Imported from Zendesk trigger #{t.get('id')}. Editable here — changes stay local.",
            active=bool(t.get("active")),
            trigger_type="on_update",
            trigger_params={"zd_id": t.get("id"), "zd_position": t.get("position")},
            conditions=conds, actions=actions,
            position=int(t.get("position") or 100), actor_email=actor_email,
        )
        if prev: updated += 1
        else: created += 1
    return {"created": created, "updated": updated, "total": len(triggers)}


def import_ticket_audits_from_zd(c: sqlite3.Connection, ticket_id: int) -> dict:
    """Pull a single ticket's audit history from Zendesk and materialize each
    audit row as one or more ticket_audit_log entries. Idempotent — keyed on
    the original audit id stored in raw['zd_audit_id']."""
    audits = zendesk.fetch_ticket_audits(ticket_id)
    inserted = 0
    # We dedupe by checking existing audit log rows that already carry this audit id
    have = {
        r["zd_id"] for r in c.execute(
            "SELECT json_extract(raw,'$.zd_audit_id') AS zd_id FROM ticket_audit_log "
            "WHERE ticket_id=? AND source='zendesk'", (ticket_id,)).fetchall()
        if r["zd_id"] is not None
    }
    for a in audits:
        aid = a.get("id")
        if aid in have:
            continue
        actor_id = a.get("author_id")
        actor_email = f"zd:{actor_id}" if actor_id else "zd:system"
        created = a.get("created_at")
        for ev in (a.get("events") or []):
            etype = ev.get("type") or ""
            # Translate ZD's event types into our timeline vocabulary
            event_type = _translate_zd_event_type(etype, ev)
            summary = _summarize_zd_event(ev)
            before = ev.get("previous_value")
            after  = ev.get("value")
            field_key = ev.get("field_name") or ""
            c.execute("""
                INSERT INTO ticket_audit_log (ticket_id, event_type, event_summary,
                    actor_email, actor_type, field_key, before_json, after_json,
                    raw, source, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'zendesk', ?)
            """, (
                ticket_id, event_type, summary[:500],
                actor_email, "zd",
                field_key,
                json.dumps(before) if before is not None else None,
                json.dumps(after)  if after  is not None else None,
                json.dumps({"zd_audit_id": aid, "zd_event_type": etype, "event": ev}),
                created,
            ))
            inserted += 1
    return {"audits_pulled": len(audits), "events_inserted": inserted}


def _translate_zd_event_type(etype: str, ev: dict) -> str:
    if etype == "Comment":
        return "comment.public" if ev.get("public") else "note.added"
    if etype == "Change":
        f = (ev.get("field_name") or "").lower()
        if f == "status":     return "status.changed"
        if f == "priority":   return "priority.changed"
        if f == "group":      return "group.changed"
        if f == "assignee":   return "assignee.changed"
        if f == "subject":    return "field.changed"
        if f.startswith("custom_fields"): return "field.changed"
        return "field.changed"
    if etype == "Create":  return "ticket.created"
    if etype == "Notification": return "notification.sent"
    if etype == "Cc": return "cc.changed"
    if etype == "SatisfactionRating": return "csat.received"
    return f"zd.{etype.lower()}"


def _summarize_zd_event(ev: dict) -> str:
    etype = ev.get("type")
    if etype == "Comment":
        b = (ev.get("body") or ev.get("html_body") or "").strip().replace("\n", " ")
        kind = "Public reply" if ev.get("public") else "Internal note"
        return f"{kind}: {b[:120]}"
    if etype == "Change":
        f = ev.get("field_name") or "field"
        return f"{f}: {ev.get('previous_value')} → {ev.get('value')}"
    if etype == "Create":
        return f"Ticket created"
    if etype == "Notification":
        return f"Notification sent: {ev.get('subject') or ''}"
    return f"{etype}"


def import_automations_from_zd(c: sqlite3.Connection, *, actor_email: str = "system") -> dict:
    """ZD's "automations" tab — time-elapsed rules. Mapped to trigger_type='time_elapsed'."""
    autos = zendesk.list_automations()
    created, updated = 0, 0
    existing_by_name = {
        r["name"]: r for r in c.execute(
            "SELECT id, name FROM automations WHERE name LIKE 'ZD automation: %'"
        ).fetchall()
    }
    for a in autos:
        name = f"ZD automation: {a.get('title') or a.get('name') or '(unnamed)'}"
        conds = _translate_conditions(a.get("conditions") or {})
        actions = _translate_actions(a.get("actions") or [])
        prev = existing_by_name.get(name)
        aid = db.upsert_automation(
            c, automation_id=(prev["id"] if prev else None),
            name=name,
            description=f"Imported from Zendesk automation #{a.get('id')}. Editable here — changes stay local.",
            active=bool(a.get("active")),
            trigger_type="time_elapsed",
            trigger_params={"zd_id": a.get("id"), "zd_position": a.get("position")},
            conditions=conds, actions=actions,
            position=int(a.get("position") or 100), actor_email=actor_email,
        )
        if prev: updated += 1
        else: created += 1
    return {"created": created, "updated": updated, "total": len(autos)}
