"""
Catalog for user-event automations (parallel to automations_catalog.py for tickets).

The /admin/user-automations editor reads from here to populate dropdowns;
src/user_rules_engine.py reads from here to validate.
"""

from __future__ import annotations


# ---- Triggers -------------------------------------------------------------
# Each entry: (key, label, description, fires_from)
# fires_from: 'request' (sync, from a route) | 'scheduler' (cron, from user_scheduler.py)

TRIGGER_EVENTS: list[tuple[str, str, str, str]] = [
    # Session
    ("user.logged_in",            "User signed in",
     "Fires right after a successful Google OAuth login.", "request"),
    ("user.logged_out",           "User signed out",
     "Fires when a user clicks Sign out (or session is cleared).", "request"),
    ("user.session_started",      "First page view of a session",
     "Fires once per browser session, on the first page view after login.", "request"),

    # Availability
    ("user.availability_changed", "Availability changed",
     "Fires when availability moves between online/away/busy/offline. Context: {before, after}.", "request"),

    # Schedule-based (fired by user_scheduler.py)
    ("user.work_day_started",     "Work day started",
     "Fires when the user's local clock crosses their work_start_time on a work day.", "scheduler"),
    ("user.work_day_ended",       "Work day ended",
     "Fires when the user's local clock crosses their work_end_time on a work day.", "scheduler"),
    ("user.idle_during_work",     "Idle during work hours",
     "Fires every 30 minutes when the user is in work hours but availability != 'online' and not on leave.", "scheduler"),

    # Leave
    ("user.on_leave_set",         "Marked on leave",
     "Fires when the user toggles leave mode ON.", "request"),
    ("user.on_leave_cleared",     "Returned from leave",
     "Fires when the user clears leave mode (back to work).", "request"),

    # Access control
    ("user.created",              "New user invited / first login",
     "Fires when a new app_user row is inserted (self-signup or admin invite).", "request"),
    ("user.role_granted",         "Role granted",
     "Fires when a role is granted to a user. Context: {role_name}.", "request"),
    ("user.role_revoked",         "Role revoked",
     "Fires when a role is removed. Context: {role_name}.", "request"),
    ("user.disabled",             "Account disabled",
     "Fires when an admin disables a user.", "request"),
    ("user.enabled",              "Account re-enabled",
     "Fires when a previously-disabled account is re-enabled.", "request"),
    ("user.profile_updated",      "Profile updated",
     "Fires after a user saves their profile. Context: {fields_changed}.", "request"),

    # Groups
    ("user.group_joined",         "Added to a group",
     "Fires when an admin adds a user to a group.", "request"),
    ("user.group_left",           "Removed from a group",
     "Fires when an admin removes a user from a group.", "request"),
]


# ---- Condition fields the rule can check on the triggering user --------

CONDITION_FIELDS: list[tuple[str, str, str]] = [
    # (key, label, type)
    ("user.availability",     "Availability",         "select"),    # online/away/busy/offline
    ("user.on_leave",         "Currently on leave",   "boolean"),
    ("user.timezone",         "Timezone",             "text"),
    ("user.email",            "Email",                "text"),
    ("user.email_domain",     "Email domain",         "text"),
    ("user.has_role",         "Has role",             "select_role"),
    ("user.in_group",         "Member of group",      "select_group"),
    ("user.minutes_since_online", "Minutes since last online", "number"),
    ("user.minutes_idle_during_work", "Idle minutes during work hours", "number"),
    ("user.last_login_at",    "Last login (relative)", "relative_date"),
    ("user.work_day",         "Currently a work day for them", "boolean"),
    ("user.in_work_hours",    "Currently in work hours", "boolean"),
    ("user.hour_of_day",      "Hour of day (their local time)", "number"),
    # Event-context fields (populated by the dispatcher's context dict)
    ("event.before",          "Event · before value", "text"),
    ("event.after",           "Event · after value",  "text"),
]


# ---- Operators allowed per condition type ------------------------------

OPS_BY_TYPE: dict[str, list[str]] = {
    "select":         ["eq", "ne", "in", "not_in"],
    "select_role":    ["eq", "ne", "in", "not_in"],
    "select_group":   ["eq", "ne", "in", "not_in"],
    "boolean":        ["eq", "ne"],
    "text":           ["eq", "ne", "contains", "starts_with"],
    "number":         ["eq", "ne", "gt", "gte", "lt", "lte"],
    "relative_date":  ["within_minutes", "older_than_minutes"],
}


# ---- Actions ----------------------------------------------------------

ACTION_TYPES: list[tuple[str, str, str]] = [
    # (key, label, params_hint)
    ("set_availability",
     "Set user's availability",
     "value: online|away|busy|offline"),
    ("mark_on_leave",
     "Mark user on leave",
     "start (optional), end (optional), reason (optional)"),
    ("clear_leave",
     "Clear leave mode",
     "(no params)"),
    ("send_in_app_notification",
     "Send in-app notification",
     "kind (info/warning/error/prompt), title, body, action_url, action_label — supports {{user.xxx}} placeholders"),
    ("send_email",
     "Send email to user",
     "subject, body — supports {{user.xxx}} placeholders"),
    ("notify_admin",
     "Send in-app notification to all admins",
     "title, body"),
    ("call_webhook",
     "POST to a webhook URL",
     "url, payload_json — supports placeholder substitution"),
    ("log_event",
     "Write a structured event to user_activity_log",
     "event: short tag, message: human text"),
    ("grant_role",
     "Grant a role (admin-only action)",
     "role_name"),
    ("revoke_role",
     "Revoke a role (admin-only action)",
     "role_name"),
    ("set_status_emoji",
     "Set a custom status emoji + label",
     "emoji, label, maps_to_availability"),
]


# ---- Default trigger event for new rules in the UI ----

DEFAULT_TRIGGER = "user.availability_changed"
