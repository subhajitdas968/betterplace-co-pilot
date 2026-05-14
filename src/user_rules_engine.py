"""
User automations engine.

dispatch_event(event_key, user_email, context) is the single entry point.
Called from:
  - HTTP endpoints (availability change, leave set/cleared, login, etc.)
  - user_scheduler.py subprocess (work_day_started, idle_during_work, etc.)
  - rules_engine on assigned tickets (could route to user_rules too — future)

For each active user_automations row matching the trigger_event:
  1. Evaluate conditions against the user profile + event context
  2. Run each action (best-effort, errors don't kill the chain)
  3. Record fire timestamp + activity log entry
"""

from __future__ import annotations

import json
import os
import re
import smtplib
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
from typing import Callable

from . import db, config, activity, user_automations_catalog as catalog


# ---- Variable substitution ------------------------------------------------

_PLACEHOLDER_RE = re.compile(r"\{\{\s*([\w.]+)\s*\}\}")


def _resolve_placeholders(text: str, vars: dict) -> str:
    """Replace {{key.path}} placeholders. Missing keys → empty string."""
    if not text or "{{" not in text:
        return text or ""

    def repl(m: re.Match) -> str:
        path = m.group(1)
        val = vars
        for part in path.split("."):
            if isinstance(val, dict):
                val = val.get(part)
            else:
                val = None
            if val is None:
                return ""
        return str(val)
    return _PLACEHOLDER_RE.sub(repl, text)


def _build_eval_context(user_email: str, event_context: dict | None) -> dict:
    """Gather the user's profile + computed context fields for condition
    eval AND placeholder substitution in actions."""
    with db.conn() as c:
        profile = db.get_user_profile(c, user_email) or {}
        role_names = [r["name"] for r in (profile.get("roles") or [])] if "roles" in profile else []
        # roles isn't always present on the profile row — fetch explicitly
        if not role_names:
            rows = c.execute("""
                SELECT r.name FROM user_roles ur JOIN roles r ON r.id=ur.role_id
                WHERE ur.user_email=?
            """, (user_email,)).fetchall()
            role_names = [r["name"] for r in rows]
        group_ids = db.get_user_group_ids(c, user_email)
        group_names = [r["name"] for r in c.execute(
            f"SELECT name FROM groups WHERE id IN ({','.join('?'*len(group_ids))})",
            group_ids
        ).fetchall()] if group_ids else []

    # Compute time-aware fields in the user's timezone
    tz_str = profile.get("timezone") or "Asia/Kolkata"
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tz_str)
    except Exception:
        tz = timezone.utc
    now_local = datetime.now(tz)
    work_days = []
    try:
        work_days = json.loads(profile.get("work_days_json") or "[]") or []
    except json.JSONDecodeError:
        pass
    day_code = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"][now_local.weekday()]
    is_work_day = day_code in work_days
    # Parse work start/end into local datetime today
    in_work_hours = False
    if is_work_day:
        try:
            sh, sm = (profile.get("work_start_time") or "09:00").split(":")
            eh, em = (profile.get("work_end_time") or "18:00").split(":")
            start_local = now_local.replace(hour=int(sh), minute=int(sm), second=0, microsecond=0)
            end_local = now_local.replace(hour=int(eh), minute=int(em), second=0, microsecond=0)
            in_work_hours = start_local <= now_local <= end_local
        except (ValueError, TypeError):
            pass

    # Idle calculation
    last_online_at = profile.get("last_online_at")
    minutes_since_online = None
    if last_online_at:
        try:
            lo = datetime.fromisoformat(last_online_at.replace("Z", "+00:00"))
            minutes_since_online = int(
                (datetime.now(lo.tzinfo) - lo).total_seconds() / 60
            )
        except (ValueError, TypeError):
            pass

    ctx = {
        "user": {
            "email": profile.get("email"),
            "email_domain": (profile.get("email") or "").split("@")[-1],
            "name": profile.get("name"),
            "availability": profile.get("availability") or "offline",
            "on_leave": int(profile.get("on_leave") or 0),
            "timezone": tz_str,
            "roles": role_names,
            "has_role": role_names,         # alias for 'in' op
            "in_group": group_names,        # alias
            "groups": group_names,
            "minutes_since_online": minutes_since_online if minutes_since_online is not None else 999999,
            "minutes_idle_during_work": minutes_since_online if (in_work_hours and minutes_since_online is not None) else 0,
            "last_login_at": profile.get("last_login_at"),
            "work_day": int(is_work_day),
            "in_work_hours": int(in_work_hours),
            "hour_of_day": now_local.hour,
        },
        "event": event_context or {},
        "public_url": config.APP_PUBLIC_URL or "http://localhost:8000",
    }
    return ctx


# ---- Condition evaluation ------------------------------------------------

def _evaluate_conditions(conditions: dict, ctx: dict) -> bool:
    rules = conditions.get("rules", []) if conditions else []
    if not rules:
        return True
    match_mode = (conditions.get("match") or "all").lower()
    results: list[bool] = []
    for r in rules:
        field = r.get("field") or ""
        op = r.get("op") or "eq"
        target = r.get("value")
        # Resolve field from context (supports dotted paths)
        val = ctx
        for part in field.split("."):
            if isinstance(val, dict):
                val = val.get(part)
            else:
                val = None
            if val is None:
                break
        ok = _apply_op(val, op, target)
        results.append(ok)
    if match_mode == "any":
        return any(results)
    return all(results)


def _apply_op(val, op: str, target) -> bool:
    try:
        if op == "eq":
            return _coerce_eq(val, target)
        if op == "ne":
            return not _coerce_eq(val, target)
        if op == "in":
            return val in (target or [])
        if op == "not_in":
            return val not in (target or [])
        if op == "contains":
            return target in (val or "")
        if op == "starts_with":
            return str(val or "").startswith(str(target or ""))
        if op == "gt":
            return (val or 0) > _to_num(target)
        if op == "gte":
            return (val or 0) >= _to_num(target)
        if op == "lt":
            return (val or 0) < _to_num(target)
        if op == "lte":
            return (val or 0) <= _to_num(target)
        if op == "within_minutes":
            if not val: return False
            try:
                dt = datetime.fromisoformat(str(val).replace("Z","+00:00"))
                delta = (datetime.now(dt.tzinfo) - dt).total_seconds() / 60
                return delta <= _to_num(target)
            except Exception:
                return False
        if op == "older_than_minutes":
            if not val: return True
            try:
                dt = datetime.fromisoformat(str(val).replace("Z","+00:00"))
                delta = (datetime.now(dt.tzinfo) - dt).total_seconds() / 60
                return delta > _to_num(target)
            except Exception:
                return False
    except Exception:
        return False
    return False


def _coerce_eq(a, b) -> bool:
    """Loose equality that handles int/bool/str confusion (e.g. '1' == 1)."""
    if a is None and b is None: return True
    if a is None or b is None:  return False
    # Coerce to string and compare as last resort
    try:
        if isinstance(a, (int, float)) or isinstance(b, (int, float)):
            return float(a) == float(b)
    except (TypeError, ValueError):
        pass
    return str(a).strip() == str(b).strip()


def _to_num(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


# ---- Action runners -------------------------------------------------------

_ACTIONS: dict[str, Callable] = {}


def action(name: str):
    def wrap(fn):
        _ACTIONS[name] = fn
        return fn
    return wrap


@action("set_availability")
def _act_set_availability(user_email: str, params: dict, ctx: dict) -> dict:
    value = params.get("value") or "online"
    with db.conn() as c:
        db.set_user_availability(c, email=user_email, availability=value)
        if value == "online":
            c.execute("UPDATE app_users SET last_online_at=? WHERE email=?",
                      (db.now_iso(), user_email))
    return {"set_availability": value}


@action("mark_on_leave")
def _act_mark_on_leave(user_email: str, params: dict, ctx: dict) -> dict:
    with db.conn() as c:
        db.set_user_leave(c, email=user_email, on_leave=1,
                           leave_start=params.get("start") or None,
                           leave_end=params.get("end") or None,
                           reason=params.get("reason") or None)
    return {"on_leave": True}


@action("clear_leave")
def _act_clear_leave(user_email: str, params: dict, ctx: dict) -> dict:
    with db.conn() as c:
        db.set_user_leave(c, email=user_email, on_leave=0)
    return {"on_leave": False}


@action("send_in_app_notification")
def _act_send_notification(user_email: str, params: dict, ctx: dict) -> dict:
    title = _resolve_placeholders(params.get("title") or "Notification", ctx)
    body = _resolve_placeholders(params.get("body") or "", ctx)
    action_url = _resolve_placeholders(params.get("action_url") or "", ctx) or None
    action_label = params.get("action_label") or None
    kind = params.get("kind") or "info"
    with db.conn() as c:
        nid = db.create_notification(c, user_email=user_email, title=title,
                                       body=body, kind=kind,
                                       action_url=action_url,
                                       action_label=action_label,
                                       source=f"automation:{ctx.get('_automation_id','?')}")
    return {"notification_id": nid}


@action("send_email")
def _act_send_email(user_email: str, params: dict, ctx: dict) -> dict:
    """Best-effort SMTP send. If SMTP_HOST isn't configured in env, we log
    the email content to user_activity_log instead of failing."""
    subject = _resolve_placeholders(params.get("subject") or "Notification", ctx)
    body = _resolve_placeholders(params.get("body") or "", ctx)
    smtp_host = os.environ.get("SMTP_HOST")
    smtp_from = os.environ.get("SMTP_FROM") or smtp_host
    if not smtp_host:
        # SMTP not configured — log so we have a trail, but don't fail
        with db.conn() as c:
            db.log_activity(c, user_email=user_email,
                            event_type="automation", event_subtype="email_skipped",
                            detail={"reason": "SMTP_HOST not set",
                                    "subject": subject, "body": body})
        return {"sent": False, "reason": "smtp_not_configured"}
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = smtp_from
        msg["To"] = user_email
        msg.set_content(body)
        smtp_port = int(os.environ.get("SMTP_PORT", "587"))
        use_tls = os.environ.get("SMTP_TLS", "true").lower() in ("true", "1", "yes")
        with smtplib.SMTP(smtp_host, smtp_port) as s:
            if use_tls:
                s.starttls()
            user_env = os.environ.get("SMTP_USER")
            pwd_env = os.environ.get("SMTP_PASSWORD")
            if user_env and pwd_env:
                s.login(user_env, pwd_env)
            s.send_message(msg)
        return {"sent": True}
    except Exception as e:
        with db.conn() as c:
            db.log_activity(c, user_email=user_email,
                            event_type="automation", event_subtype="email_failed",
                            detail={"error": str(e), "subject": subject})
        return {"sent": False, "error": str(e)}


@action("notify_admin")
def _act_notify_admin(user_email: str, params: dict, ctx: dict) -> dict:
    """Send an in-app notification to everyone with admin.users perm."""
    title = _resolve_placeholders(params.get("title") or "", ctx)
    body = _resolve_placeholders(params.get("body") or "", ctx)
    with db.conn() as c:
        admins = c.execute("""
            SELECT DISTINCT ur.user_email FROM user_roles ur
            JOIN role_permissions rp ON rp.role_id = ur.role_id
            WHERE rp.permission_key = 'admin.users'
        """).fetchall()
        for a in admins:
            db.create_notification(c, user_email=a["user_email"],
                                     title=title, body=body, kind="info",
                                     source=f"automation:{ctx.get('_automation_id','?')}")
    return {"notified_admins": len(admins)}


@action("call_webhook")
def _act_webhook(user_email: str, params: dict, ctx: dict) -> dict:
    import urllib.request
    url = _resolve_placeholders(params.get("url") or "", ctx)
    if not url:
        return {"sent": False, "reason": "no_url"}
    try:
        payload = params.get("payload_json") or "{}"
        if isinstance(payload, str):
            payload = _resolve_placeholders(payload, ctx)
            body_bytes = payload.encode("utf-8")
        else:
            body_bytes = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=body_bytes,
                                       headers={"Content-Type": "application/json"},
                                       method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return {"sent": True, "status": resp.status}
    except Exception as e:
        return {"sent": False, "error": str(e)}


@action("log_event")
def _act_log(user_email: str, params: dict, ctx: dict) -> dict:
    event = params.get("event") or "automation_event"
    message = params.get("message") or ""
    with db.conn() as c:
        db.log_activity(c, user_email=user_email,
                        event_type="automation", event_subtype=event,
                        detail={"message": _resolve_placeholders(message, ctx),
                                "automation_id": ctx.get("_automation_id")})
    return {"logged": True}


@action("grant_role")
def _act_grant_role(user_email: str, params: dict, ctx: dict) -> dict:
    role_name = params.get("role_name")
    if not role_name:
        return {"granted": False}
    with db.conn() as c:
        row = c.execute("SELECT id FROM roles WHERE name=?", (role_name,)).fetchone()
        if not row:
            return {"granted": False, "reason": "role_not_found"}
        try:
            db.grant_role_to_user(c, user_email=user_email, role_id=row["id"],
                                   actor_email="system:automation")
            return {"granted": True}
        except ValueError as e:
            return {"granted": False, "error": str(e)}


@action("revoke_role")
def _act_revoke_role(user_email: str, params: dict, ctx: dict) -> dict:
    role_name = params.get("role_name")
    if not role_name:
        return {"revoked": False}
    with db.conn() as c:
        row = c.execute("SELECT id FROM roles WHERE name=?", (role_name,)).fetchone()
        if not row:
            return {"revoked": False}
        try:
            db.revoke_role_from_user(c, user_email=user_email, role_id=row["id"],
                                      actor_email="system:automation")
            return {"revoked": True}
        except ValueError as e:
            return {"revoked": False, "error": str(e)}


@action("set_status_emoji")
def _act_set_emoji(user_email: str, params: dict, ctx: dict) -> dict:
    with db.conn() as c:
        db.set_user_availability(
            c, email=user_email,
            availability=params.get("maps_to_availability") or "online",
            emoji=params.get("emoji"),
            label=params.get("label"),
        )
    return {"set": True}


# ---- Dispatcher ----------------------------------------------------------

# Cache active rules per trigger for ~30s. Invalidated when an admin saves
# a rule via /admin/user-automations.
import time as _time

_RULES_CACHE: dict[str, list[dict]] = {}
_RULES_CACHE_AT: float = 0
_RULES_TTL = 30


def invalidate_rules_cache() -> None:
    global _RULES_CACHE, _RULES_CACHE_AT
    _RULES_CACHE = {}
    _RULES_CACHE_AT = 0


def _load_rules_for(trigger_event: str) -> list[dict]:
    global _RULES_CACHE, _RULES_CACHE_AT
    now = _time.monotonic()
    if now - _RULES_CACHE_AT > _RULES_TTL:
        _RULES_CACHE = {}
        _RULES_CACHE_AT = now
    if trigger_event in _RULES_CACHE:
        return _RULES_CACHE[trigger_event]
    with db.conn() as c:
        rows = c.execute("""
            SELECT * FROM user_automations
            WHERE active=1 AND trigger_event=?
            ORDER BY position, id
        """, (trigger_event,)).fetchall()
    rules: list[dict] = []
    for r in rows:
        d = dict(r)
        try:
            d["_conditions"] = json.loads(d.get("conditions_json") or "{}")
        except json.JSONDecodeError:
            d["_conditions"] = {}
        try:
            d["_actions"] = json.loads(d.get("actions_json") or "[]")
        except json.JSONDecodeError:
            d["_actions"] = []
        rules.append(d)
    _RULES_CACHE[trigger_event] = rules
    return rules


def dispatch_event(trigger_event: str, user_email: str,
                    context: dict | None = None) -> list[dict]:
    """Run every active rule matching trigger_event against user_email.
    Returns a list of {automation_id, fired, action_results}. Best-effort —
    never raises so callers (endpoints, schedulers) stay healthy."""
    if not user_email:
        return []
    try:
        rules = _load_rules_for(trigger_event)
    except Exception as e:
        print(f"[user_rules_engine] load failed: {e}")
        return []
    if not rules:
        return []
    eval_ctx = _build_eval_context(user_email, context)
    summaries: list[dict] = []
    for rule in rules:
        eval_ctx["_automation_id"] = rule["id"]
        try:
            passed = _evaluate_conditions(rule["_conditions"], eval_ctx)
        except Exception as e:
            print(f"[user_rules_engine] eval failed for #{rule['id']}: {e}")
            passed = False
        if not passed:
            summaries.append({"automation_id": rule["id"], "fired": False,
                              "reason": "conditions_failed"})
            continue
        action_results: list[dict] = []
        for act in rule["_actions"]:
            atype = act.get("type")
            handler = _ACTIONS.get(atype)
            if not handler:
                action_results.append({"type": atype, "error": "unknown_action_type"})
                continue
            try:
                res = handler(user_email, act.get("params") or {}, eval_ctx)
                action_results.append({"type": atype, **(res or {})})
            except Exception as e:
                action_results.append({"type": atype, "error": str(e)})
        # Record fire
        try:
            with db.conn() as c:
                db.record_user_automation_fire(c, rule["id"])
                db.log_activity(c, user_email=user_email,
                                event_type="automation", event_subtype="fired",
                                target_kind="user_automation",
                                target_id=str(rule["id"]),
                                detail={"trigger": trigger_event,
                                        "rule_name": rule["name"],
                                        "actions": action_results})
        except Exception as e:
            print(f"[user_rules_engine] post-fire bookkeeping failed: {e}")
        summaries.append({"automation_id": rule["id"], "fired": True,
                          "rule_name": rule["name"],
                          "actions": action_results})
    return summaries
