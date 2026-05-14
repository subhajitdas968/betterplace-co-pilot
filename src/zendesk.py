"""Zendesk API client. Wraps the bits we need with retry + rate-limit handling."""
from __future__ import annotations
import time
from typing import Any, Iterator

import requests

from . import config


_session = requests.Session()
_session.auth = (f"{config.ZD_EMAIL}/token", config.ZD_TOKEN)
_session.headers.update({"Accept": "application/json"})


class ZDError(Exception):
    pass


def get(path: str, params: dict | None = None) -> dict:
    url = path if path.startswith("http") else f"{config.ZD_BASE}/{path}"
    for attempt in range(6):
        r = _session.get(url, params=params, timeout=30)
        if r.status_code == 429:
            wait = int(r.headers.get("Retry-After", "5"))
            time.sleep(wait)
            continue
        if r.status_code == 401:
            raise ZDError("401 Unauthorized — check ZD_EMAIL / ZD_TOKEN in .env")
        if r.status_code >= 500:
            time.sleep(2 ** attempt)
            continue
        r.raise_for_status()
        return r.json()
    raise ZDError(f"too many retries on GET {path}")


def whoami() -> dict:
    return get("users/me.json")["user"]


def list_groups() -> list[dict]:
    out, page = [], get("groups.json")
    out.extend(page.get("groups", []))
    while page.get("next_page"):
        page = get(page["next_page"])
        out.extend(page.get("groups", []))
    return out


def list_ticket_fields() -> list[dict]:
    out, page = [], get("ticket_fields.json")
    out.extend(page.get("ticket_fields", []))
    while page.get("next_page"):
        page = get(page["next_page"])
        out.extend(page.get("ticket_fields", []))
    return out


def list_custom_statuses() -> list[dict]:
    """Returns all custom ticket statuses defined in Zendesk (Suite Professional+)."""
    try:
        page = get("custom_statuses.json")
        return page.get("custom_statuses", [])
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code in (402, 403, 404):
            return []
        raise


def list_ticket_forms() -> list[dict]:
    """Returns all forms — each has ticket_field_ids array. Requires Suite Professional+."""
    try:
        page = get("ticket_forms.json")
        return page.get("ticket_forms", [])
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code in (402, 403, 404):
            return []
        raise


def fetch_ticket_metrics(ticket_id: int) -> dict | None:
    """Per-ticket SLA + timing metrics. 404 if metrics not available (rare)."""
    try:
        res = get(f"tickets/{ticket_id}/metrics.json")
        return res.get("ticket_metric") or res.get("ticket_metrics")
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code in (403, 404):
            return None
        raise


def incremental_tickets(start_time: int, *, group_ids: set[int] | None = None) -> Iterator[dict]:
    """Iterate tickets via /incremental/tickets/cursor.json. Filters by group_id client-side."""
    cursor_url = f"incremental/tickets/cursor.json?start_time={start_time}&per_page=200"
    while True:
        page = get(cursor_url)
        for t in page.get("tickets") or []:
            if group_ids is None or t.get("group_id") in group_ids:
                yield t
        if page.get("end_of_stream"):
            return
        next_url = page.get("after_url")
        if not next_url:
            return
        cursor_url = next_url


def fetch_comments(ticket_id: int) -> tuple[list[dict], list[dict]]:
    """Returns (comments, side_loaded_users). 404 means archived/restricted — return empty silently."""
    try:
        res = get(f"tickets/{ticket_id}/comments.json", params={"include": "users"})
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code in (403, 404):
            return [], []
        raise
    return res.get("comments") or [], res.get("users") or []


def fetch_user(user_id: int) -> dict | None:
    try:
        return get(f"users/{user_id}.json").get("user")
    except Exception:
        return None


def fetch_org(org_id: int) -> dict | None:
    try:
        return get(f"organizations/{org_id}.json").get("organization")
    except Exception:
        return None


def add_field_option(field_id: int, option_name: str, option_value: str | None = None) -> dict:
    """Append a new option to a Zendesk dropdown/multiselect field.
    The PUT call requires sending ALL existing options too — Zendesk replaces the list."""
    # Fetch current field to get existing options
    res = get(f"ticket_fields/{field_id}.json")
    field = res.get("ticket_field") or {}
    options = list(field.get("custom_field_options") or [])
    if option_value is None:
        option_value = option_name.lower().replace(" ", "_").replace("/", "_")
    # Avoid duplicates
    if any(o.get("name") == option_name or o.get("value") == option_value for o in options):
        return {"already_existed": True, "field": field}
    options.append({"name": option_name, "value": option_value})
    payload = {"ticket_field": {"custom_field_options": options}}
    url = f"{config.ZD_BASE}/ticket_fields/{field_id}.json"
    r = _session.put(url, json=payload, timeout=30)
    r.raise_for_status()
    return {"already_existed": False, "field": r.json().get("ticket_field"), "added": {"name": option_name, "value": option_value}}


def fetch_ticket_audits(ticket_id: int) -> list[dict]:
    """Pull the ticket's audit trail from Zendesk. Returns chronological list of
    audit rows (each with events[] inside). 404 means the ticket is archived or
    restricted — return empty silently."""
    out = []
    cursor_url = f"tickets/{ticket_id}/audits.json"
    safety = 0
    while cursor_url:
        try:
            res = get(cursor_url, params={"per_page": 100} if "?" not in cursor_url else None)
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code in (403, 404):
                return out
            raise
        for a in res.get("audits") or []:
            out.append(a)
        next_url = res.get("next_page")
        if not next_url:
            break
        # next_page is a full URL; strip the base so our `get()` helper handles it
        cursor_url = next_url.replace(f"{config.ZD_BASE}/", "")
        safety += 1
        if safety > 25:  # plenty for any single ticket
            break
    return out


def list_triggers() -> list[dict]:
    """Pull all ticket triggers from Zendesk. Used by the import-from-ZD button."""
    out = []
    page = 1
    while True:
        res = get("triggers.json", params={"page": page, "per_page": 100})
        out.extend(res.get("triggers") or [])
        if not res.get("next_page"):
            break
        page += 1
        if page > 50:           # safety: 5K triggers is far beyond realistic
            break
    return out


def list_automations() -> list[dict]:
    """Pull all ticket automations (time-based rules) from Zendesk."""
    out = []
    page = 1
    while True:
        res = get("automations.json", params={"page": page, "per_page": 100})
        out.extend(res.get("automations") or [])
        if not res.get("next_page"):
            break
        page += 1
        if page > 50:
            break
    return out


def write_back_field(ticket_id: int, custom_fields: dict[str, Any] | None = None,
                     standard_fields: dict[str, Any] | None = None) -> dict:
    """PUT updates back to a ticket. Phase 3 capability — kept here so AI can call it."""
    payload: dict[str, Any] = {"ticket": {}}
    if custom_fields:
        payload["ticket"]["custom_fields"] = [{"id": int(fid), "value": v} for fid, v in custom_fields.items()]
    if standard_fields:
        payload["ticket"].update(standard_fields)
    url = f"{config.ZD_BASE}/tickets/{ticket_id}.json"
    r = _session.put(url, json=payload, timeout=30)
    r.raise_for_status()
    return r.json()
