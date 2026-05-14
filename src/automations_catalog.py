"""Automations + Triggers catalog.

Single source of truth for the visual rule builder. The shape here drives BOTH
the admin UI (what conditions / actions the user can pick) and the runtime
engine (how each rule is evaluated and applied).

Two categories of rules:
  - Trigger   : event-driven — fires the moment something happens to a ticket
                (created, status changed, comment added, SLA warn, AI insight, …)
  - Scheduler : time-driven — fires on an interval / cron / "N hours after X"

Each condition is a structured row: {field, op, value}.
Each action is a structured row: {type, params}.

The builder is driven by the catalog so adding a new condition or action only
needs an entry here — no UI changes.
"""
from __future__ import annotations
from typing import Any


# =============================================================================
# Trigger events — used by category='trigger'
# =============================================================================
# Grouped + labelled so the dropdown reads as "Status changed" rather than
# "ticket.status_changed". Each entry can optionally take parameters (e.g. the
# field.changed event takes a field_id param).

TRIGGER_EVENTS: list[dict] = [
    # --- Ticket lifecycle ---
    {"key": "ticket.created",         "group": "Ticket",      "label": "Ticket created",
     "desc": "Any new ticket — native, from Gmail, or synced from Zendesk."},
    {"key": "ticket.created.native",  "group": "Ticket",      "label": "Native ticket created",
     "desc": "Only ticket created in this tool (BP-NNNNNN). Excludes Zendesk sync."},
    {"key": "ticket.created.gmail",   "group": "Ticket",      "label": "Ticket created from Gmail intake",
     "desc": "When the Gmail watcher opens a new ticket from a thread."},
    {"key": "ticket.updated",         "group": "Ticket",      "label": "Ticket updated",
     "desc": "Any change to a ticket. Use specific events below if you can."},
    {"key": "ticket.solved",          "group": "Ticket",      "label": "Ticket solved"},
    {"key": "ticket.closed",          "group": "Ticket",      "label": "Ticket closed"},
    {"key": "ticket.reopened",        "group": "Ticket",      "label": "Ticket reopened"},
    {"key": "ticket.merged",          "group": "Ticket",      "label": "Ticket merged into another"},

    # --- Field-level changes ---
    {"key": "status.changed",         "group": "Field changes", "label": "Status changed"},
    {"key": "priority.changed",       "group": "Field changes", "label": "Priority changed"},
    {"key": "group.changed",          "group": "Field changes", "label": "Group changed"},
    {"key": "assignee.changed",       "group": "Field changes", "label": "Assignee changed"},
    {"key": "assignee.cleared",       "group": "Field changes", "label": "Ticket became unassigned"},
    {"key": "form.changed",           "group": "Field changes", "label": "Form changed"},
    {"key": "field.changed",          "group": "Field changes", "label": "A specific field changed",
     "params": [{"key": "field_id", "label": "Field", "kind": "field_picker"}]},
    {"key": "tag.added",              "group": "Field changes", "label": "Tag added"},
    {"key": "tag.removed",            "group": "Field changes", "label": "Tag removed"},

    # --- Conversation ---
    {"key": "comment.public_added",   "group": "Conversation", "label": "Public reply added"},
    {"key": "comment.from_customer",  "group": "Conversation", "label": "Customer replied"},
    {"key": "comment.from_agent",     "group": "Conversation", "label": "Agent replied"},
    {"key": "note.added",             "group": "Conversation", "label": "Internal note added"},
    {"key": "attachment.added",       "group": "Conversation", "label": "Attachment uploaded"},
    {"key": "mention.received",       "group": "Conversation", "label": "Agent @-mentioned in a note"},

    # --- SLA ---
    {"key": "sla.warn.any",           "group": "SLA",          "label": "Any SLA clock at warn (≥80%)"},
    {"key": "sla.breach.any",         "group": "SLA",          "label": "Any SLA clock breached"},
    {"key": "sla.warn.first_reply",   "group": "SLA",          "label": "First-reply SLA at warn"},
    {"key": "sla.breach.first_reply", "group": "SLA",          "label": "First-reply SLA breached"},
    {"key": "sla.warn.next_reply",    "group": "SLA",          "label": "Next-reply SLA at warn"},
    {"key": "sla.breach.next_reply",  "group": "SLA",          "label": "Next-reply SLA breached"},
    {"key": "sla.warn.resolution",    "group": "SLA",          "label": "Resolution SLA at warn"},
    {"key": "sla.breach.resolution",  "group": "SLA",          "label": "Resolution SLA breached"},

    # --- AI ---
    {"key": "ai.insight_created",     "group": "AI",           "label": "AI insight saved on ticket"},
    {"key": "ai.flagged_pickup",      "group": "AI",           "label": "AI flagged ticket as needing pickup"},
    {"key": "ai.flagged_kb_worthy",   "group": "AI",           "label": "AI flagged ticket as KB-worthy"},
    {"key": "ai.field_suggestion",    "group": "AI",           "label": "AI suggested a field correction"},

    # --- Form ---
    {"key": "form.required_complete", "group": "Form",         "label": "All required fields filled"},
]


# =============================================================================
# Scheduler kinds — used by category='scheduler'
# =============================================================================

SCHEDULER_KINDS: list[dict] = [
    {"key": "interval",          "label": "Every N minutes / hours / days",
     "params": [
        {"key": "amount", "label": "Amount", "kind": "number", "default": 1},
        {"key": "unit",   "label": "Unit",   "kind": "select", "options": [
            {"value": "minutes", "label": "minutes"},
            {"value": "hours",   "label": "hours"},
            {"value": "days",    "label": "days"},
        ], "default": "hours"},
     ]},
    {"key": "cron",              "label": "At specific times (cron expression)",
     "params": [
        {"key": "expr", "label": "Cron expression", "kind": "text",
         "placeholder": "e.g. 0 9 * * 1-5  (every weekday 9 AM)", "default": "0 9 * * *"},
        {"key": "timezone", "label": "Timezone", "kind": "text", "default": "Asia/Kolkata"},
     ]},
    {"key": "after_status",      "label": "N hours after ticket entered a status",
     "params": [
        {"key": "status", "label": "Status",       "kind": "status"},
        {"key": "hours",  "label": "After (hours)", "kind": "number", "default": 4},
     ]},
    {"key": "after_no_activity", "label": "N hours of no activity",
     "params": [
        {"key": "hours", "label": "Idle hours", "kind": "number", "default": 24},
     ]},
    {"key": "business_open",     "label": "When business hours open (per business-hours schedule)"},
    {"key": "business_close",    "label": "When business hours close"},
]


# =============================================================================
# Condition fields — what you can compare against
# =============================================================================

# Each field carries its `type`, which drives which ops + value control to show.
# Types: enum | enum_multi | text | number | datetime | bool | tags | duration |
#        field_value (dynamic — value picker is the field's own option set)

CONDITION_FIELDS: list[dict] = [
    # Ticket basics
    {"key": "status",          "group": "Ticket",       "label": "Status",          "type": "enum",
     "options": [{"v": "new","l":"New"},{"v":"open","l":"Open"},{"v":"pending","l":"Pending"},
                 {"v":"hold","l":"On hold"},{"v":"solved","l":"Solved"},{"v":"closed","l":"Closed"}]},
    {"key": "custom_status_id","group": "Ticket",       "label": "Custom status",   "type": "custom_status"},
    {"key": "priority",        "group": "Ticket",       "label": "Priority",        "type": "enum",
     "options": [{"v":"low","l":"Low"},{"v":"normal","l":"Normal"},{"v":"high","l":"High"},{"v":"urgent","l":"Urgent"}]},
    {"key": "type",            "group": "Ticket",       "label": "Type",            "type": "enum",
     "options": [{"v":"question","l":"Question"},{"v":"incident","l":"Incident"},
                 {"v":"problem","l":"Problem"},{"v":"task","l":"Task"}]},
    {"key": "group_id",        "group": "Ticket",       "label": "Group",           "type": "group"},
    {"key": "assignee_id",     "group": "Ticket",       "label": "Assignee",        "type": "agent"},
    {"key": "form_id",         "group": "Ticket",       "label": "Form",            "type": "native_form"},
    {"key": "tags",            "group": "Ticket",       "label": "Tags",            "type": "tags"},
    {"key": "subject",         "group": "Ticket",       "label": "Subject",         "type": "text"},
    {"key": "description",     "group": "Ticket",       "label": "Description",     "type": "text"},
    {"key": "source",          "group": "Ticket",       "label": "Source",          "type": "enum",
     "options": [{"v":"zendesk","l":"Zendesk sync"},{"v":"native","l":"Native"},{"v":"gmail","l":"Gmail intake"}]},

    # Customer / requester
    {"key": "customer",        "group": "Customer",     "label": "Customer (custom field)", "type": "customer"},
    {"key": "requester_email", "group": "Customer",     "label": "Requester email", "type": "text"},
    {"key": "requester_domain","group": "Customer",     "label": "Requester email domain", "type": "text"},
    {"key": "organization_id", "group": "Customer",     "label": "Organization",    "type": "text"},

    # Time
    {"key": "created_at",      "group": "Time",         "label": "Created at",      "type": "datetime"},
    {"key": "updated_at",      "group": "Time",         "label": "Last updated at", "type": "datetime"},
    {"key": "solved_at",       "group": "Time",         "label": "Solved at",       "type": "datetime"},
    {"key": "hours_since_created", "group": "Time", "label": "Hours since created", "type": "duration"},
    {"key": "hours_since_updated", "group": "Time", "label": "Hours since last update", "type": "duration"},
    {"key": "hours_since_last_customer_reply", "group": "Time", "label": "Hours since last customer reply", "type": "duration"},
    {"key": "hours_since_last_agent_reply",    "group": "Time", "label": "Hours since last agent reply",    "type": "duration"},
    {"key": "now.day_of_week", "group": "Time", "label": "Current day of week", "type": "enum",
     "options": [{"v":"0","l":"Sun"},{"v":"1","l":"Mon"},{"v":"2","l":"Tue"},{"v":"3","l":"Wed"},
                 {"v":"4","l":"Thu"},{"v":"5","l":"Fri"},{"v":"6","l":"Sat"}]},
    {"key": "within_business_hours", "group": "Time", "label": "Within business hours", "type": "bool"},

    # Conversation
    {"key": "public_reply_count","group": "Conversation","label": "Number of public replies", "type": "number"},
    {"key": "internal_note_count","group": "Conversation","label": "Number of internal notes","type": "number"},
    {"key": "attachment_count",  "group": "Conversation","label": "Number of attachments",   "type": "number"},
    {"key": "last_replier",      "group": "Conversation","label": "Last replier",            "type": "enum",
     "options": [{"v":"agent","l":"Agent"},{"v":"customer","l":"Customer"},{"v":"none","l":"No reply yet"}]},

    # SLA
    {"key": "sla.first_reply_state", "group": "SLA", "label": "First-reply SLA state", "type": "enum",
     "options": [{"v":"ok","l":"OK"},{"v":"warn","l":"Warn"},{"v":"breached","l":"Breached"},{"v":"met","l":"Met"}]},
    {"key": "sla.next_reply_state",  "group": "SLA", "label": "Next-reply SLA state",  "type": "enum",
     "options": [{"v":"ok","l":"OK"},{"v":"warn","l":"Warn"},{"v":"breached","l":"Breached"},{"v":"met","l":"Met"}]},
    {"key": "sla.resolution_state",  "group": "SLA", "label": "Resolution SLA state",  "type": "enum",
     "options": [{"v":"ok","l":"OK"},{"v":"warn","l":"Warn"},{"v":"breached","l":"Breached"},{"v":"met","l":"Met"}]},
    {"key": "sla.policy_id",         "group": "SLA", "label": "SLA policy",            "type": "sla_policy"},

    # AI
    {"key": "ai.has_insight",        "group": "AI",  "label": "Has AI insight",        "type": "bool"},
    {"key": "ai.recommendations_count","group": "AI","label": "AI recommendation count","type": "number"},
    {"key": "ai.kb_worthy",          "group": "AI",  "label": "AI flagged KB-worthy",  "type": "bool"},
    {"key": "ai.pickup_flag",        "group": "AI",  "label": "AI flagged pickup",     "type": "bool"},
    {"key": "ai.summary_contains",   "group": "AI",  "label": "AI summary contains",   "type": "text"},

    # Custom fields — every ticket_field + native_field. Resolved at template-render time
    # so the picker shows every field by title with its own option set.
    # Marker: {"key": "custom_field", ..., "type": "custom_field"} expands client-side.
    {"key": "custom_field",          "group": "Custom fields", "label": "Custom field",        "type": "custom_field"},
]


# =============================================================================
# Operators — keyed by field type
# =============================================================================

OPS_BY_TYPE: dict[str, list[dict]] = {
    "enum":           [{"v":"is","l":"is"},{"v":"is_not","l":"is not"},
                       {"v":"in","l":"is any of"},{"v":"not_in","l":"is none of"}],
    "enum_multi":     [{"v":"has","l":"has"},{"v":"has_any","l":"has any of"},
                       {"v":"has_all","l":"has all of"},{"v":"has_none","l":"has none of"}],
    "text":           [{"v":"eq","l":"equals"},{"v":"neq","l":"not equals"},
                       {"v":"contains","l":"contains"},{"v":"not_contains","l":"does not contain"},
                       {"v":"starts_with","l":"starts with"},{"v":"ends_with","l":"ends with"},
                       {"v":"regex","l":"matches regex"},
                       {"v":"is_empty","l":"is empty"},{"v":"is_not_empty","l":"is not empty"}],
    "number":         [{"v":"eq","l":"="},{"v":"neq","l":"≠"},{"v":"gt","l":">"},{"v":"gte","l":"≥"},
                       {"v":"lt","l":"<"},{"v":"lte","l":"≤"},{"v":"between","l":"between"}],
    "datetime":       [{"v":"before","l":"is before"},{"v":"after","l":"is after"},
                       {"v":"within_last","l":"within the last"},{"v":"older_than","l":"is older than"},
                       {"v":"is_set","l":"is set"},{"v":"is_unset","l":"is not set"}],
    "duration":       [{"v":"gt","l":">"},{"v":"gte","l":"≥"},{"v":"lt","l":"<"},{"v":"lte","l":"≤"},
                       {"v":"between","l":"between"}],
    "bool":           [{"v":"is_true","l":"is true"},{"v":"is_false","l":"is false"}],
    "tags":           [{"v":"has","l":"has"},{"v":"has_any","l":"has any of"},
                       {"v":"has_none","l":"has none of"}],
    "group":          [{"v":"is","l":"is"},{"v":"is_not","l":"is not"},
                       {"v":"in","l":"is any of"},{"v":"not_in","l":"is none of"}],
    "agent":          [{"v":"is","l":"is"},{"v":"is_not","l":"is not"},
                       {"v":"is_current_user","l":"is current user"},
                       {"v":"is_in_group","l":"is in same group as ticket"},
                       {"v":"is_set","l":"is assigned"},{"v":"is_unset","l":"is unassigned"}],
    "native_form":    [{"v":"is","l":"is"},{"v":"is_not","l":"is not"}],
    "custom_status":  [{"v":"is","l":"is"},{"v":"is_not","l":"is not"},
                       {"v":"in","l":"is any of"},{"v":"not_in","l":"is none of"}],
    "customer":       [{"v":"is","l":"is"},{"v":"is_not","l":"is not"},
                       {"v":"in","l":"is any of"},{"v":"not_in","l":"is none of"}],
    "sla_policy":     [{"v":"is","l":"is"},{"v":"is_not","l":"is not"}],
    "custom_field":   [{"v":"eq","l":"equals"},{"v":"neq","l":"not equals"},
                       {"v":"contains","l":"contains"},{"v":"is_set","l":"is set"},
                       {"v":"is_unset","l":"is empty"}],
}


# =============================================================================
# Actions — what a rule does when it fires
# =============================================================================

# Each action has params, each param has a `kind` that drives the input control.
# Param kinds: text | textarea | number | select | status | priority | group |
#              agent | field_picker | option_picker | tag_picker | lang_select |
#              custom_status | duration | bool | auto_reply | macro | webhook_method

ACTION_TYPES: list[dict] = [
    # ----- Field changes -----
    {"key": "set_status",      "group": "Field changes", "label": "Set status",
     "params": [{"key":"status","label":"Status","kind":"status","required":True}]},
    {"key": "set_custom_status","group": "Field changes","label": "Set custom status",
     "params": [{"key":"custom_status_id","label":"Custom status","kind":"custom_status","required":True}]},
    {"key": "set_priority",    "group": "Field changes", "label": "Set priority",
     "params": [{"key":"priority","label":"Priority","kind":"priority","required":True}]},
    {"key": "set_type",        "group": "Field changes", "label": "Set type",
     "params": [{"key":"type","label":"Type","kind":"select","required":True,
        "options":[{"v":"question","l":"Question"},{"v":"incident","l":"Incident"},
                   {"v":"problem","l":"Problem"},{"v":"task","l":"Task"}]}]},
    {"key": "set_group",       "group": "Field changes", "label": "Set group",
     "params": [{"key":"group_id","label":"Group","kind":"group","required":True}]},
    {"key": "set_assignee",    "group": "Field changes", "label": "Set assignee",
     "params": [{"key":"agent","label":"Agent","kind":"agent_with_specials","required":True,
                 "desc":"Choose a specific agent, 'round-robin within group', or 'unassign'."}]},
    {"key": "set_form",        "group": "Field changes", "label": "Set form",
     "params": [{"key":"form_id","label":"Form","kind":"native_form","required":True}]},
    {"key": "set_custom_field","group": "Field changes", "label": "Set a custom field value",
     "params": [{"key":"field_id","label":"Field","kind":"field_picker","required":True},
                {"key":"value","label":"Value","kind":"option_picker"}]},
    {"key": "clear_field",     "group": "Field changes", "label": "Clear a custom field",
     "params": [{"key":"field_id","label":"Field","kind":"field_picker","required":True}]},
    {"key": "add_tag",         "group": "Field changes", "label": "Add tag",
     "params": [{"key":"tag","label":"Tag","kind":"tag_picker","required":True}]},
    {"key": "remove_tag",      "group": "Field changes", "label": "Remove tag",
     "params": [{"key":"tag","label":"Tag","kind":"tag_picker","required":True}]},

    # ----- Conversation -----
    {"key": "add_public_reply","group": "Conversation",  "label": "Add a public reply",
     "params": [{"key":"body","label":"Body (Markdown, supports placeholders)","kind":"textarea","required":True,
                 "placeholder":"Hi {{requester_name}}, …"}]},
    {"key": "add_internal_note","group": "Conversation", "label": "Add an internal note",
     "params": [{"key":"body","label":"Body","kind":"textarea","required":True}]},
    {"key": "send_auto_reply", "group": "Conversation",  "label": "Send an auto-reply",
     "params": [{"key":"auto_reply_id","label":"Template","kind":"auto_reply","required":True}]},
    {"key": "translate_body",  "group": "Conversation",  "label": "Translate last comment to language",
     "params": [{"key":"lang","label":"Target language","kind":"lang_select","required":True}]},

    # ----- Notifications -----
    {"key": "notify_assignee_email","group": "Notifications","label": "Email the assignee",
     "params": [{"key":"subject","label":"Subject","kind":"text","required":True},
                {"key":"body","label":"Body","kind":"textarea","required":True}]},
    {"key": "notify_requester_email","group": "Notifications","label": "Email the requester",
     "params": [{"key":"subject","label":"Subject","kind":"text","required":True},
                {"key":"body","label":"Body","kind":"textarea","required":True}]},
    {"key": "notify_group_slack","group": "Notifications","label": "Send Slack message to a channel",
     "params": [{"key":"channel","label":"Channel","kind":"text","required":True,"placeholder":"#support"},
                {"key":"message","label":"Message","kind":"textarea","required":True}]},
    {"key": "mention_agent",   "group": "Notifications", "label": "@-mention an agent on the ticket",
     "params": [{"key":"agent_id","label":"Agent","kind":"agent","required":True},
                {"key":"message","label":"Optional message","kind":"text"}]},

    # ----- AI -----
    {"key": "request_ai_insight","group": "AI",          "label": "Queue ticket for AI analysis",
     "params": []},
    {"key": "generate_ai_summary","group": "AI",         "label": "Regenerate AI summary",
     "params": []},
    {"key": "generate_kb_draft","group": "AI",           "label": "Generate KB draft from this ticket",
     "params": []},

    # ----- External -----
    {"key": "call_webhook",    "group": "External",      "label": "Call a webhook",
     "params": [{"key":"url","label":"URL","kind":"text","required":True,"placeholder":"https://…"},
                {"key":"method","label":"Method","kind":"webhook_method","required":True,"default":"POST"},
                {"key":"headers_json","label":"Headers (JSON)","kind":"text","default":"{}"},
                {"key":"body_template","label":"Body (placeholders allowed)","kind":"textarea"}]},
    {"key": "create_jira_issue","group": "External",     "label": "Create a Jira issue",
     "params": [{"key":"project","label":"Project key","kind":"text","required":True},
                {"key":"summary","label":"Summary","kind":"text","required":True,"default":"{{subject}}"},
                {"key":"description","label":"Description","kind":"textarea"}]},

    # ----- Workflow -----
    {"key": "close_ticket",    "group": "Workflow",      "label": "Close the ticket",
     "params": [{"key":"note","label":"Optional resolution note","kind":"text"}]},
    {"key": "reopen_ticket",   "group": "Workflow",      "label": "Reopen the ticket"},
    {"key": "snooze_until",    "group": "Workflow",      "label": "Snooze (hold) for…",
     "params": [{"key":"hours","label":"Hours","kind":"number","required":True,"default":24}]},
    {"key": "link_to_ticket",  "group": "Workflow",      "label": "Link to another ticket",
     "params": [{"key":"ticket_id","label":"Other ticket id","kind":"text","required":True},
                {"key":"link_type","label":"Link type","kind":"select","default":"related",
                 "options":[{"v":"related","l":"related"},{"v":"duplicate_of","l":"duplicate of"},
                            {"v":"blocks","l":"blocks"},{"v":"caused_by","l":"caused by"}]}]},
    {"key": "apply_macro",     "group": "Workflow",      "label": "Apply a macro",
     "params": [{"key":"macro_id","label":"Macro","kind":"macro","required":True}]},
    {"key": "escalate_to_manager","group": "Workflow",   "label": "Escalate to assignee's manager",
     "params": [{"key":"note","label":"Note","kind":"text"}]},

    # ----- Imported but not yet mapped -----
    # ZD actions that don't (yet) have a clean native equivalent land here so
    # the admin sees what was imported rather than the dropdown silently
    # falling back to "Set status → new".
    {"key": "raw", "group": "Imported (unmapped)", "label": "Unmapped action (from Zendesk import)",
     "desc": "This action was imported from a Zendesk trigger/automation but doesn't yet have a native equivalent. Pick a real action above to replace it.",
     "params": [
        {"key": "field", "label": "Original ZD field", "kind": "text"},
        {"key": "value", "label": "Original ZD value", "kind": "textarea"},
     ]},
]


def grouped(items: list[dict]) -> dict[str, list[dict]]:
    """Helper for the template: return {group: [items]} preserving insertion order."""
    out: dict[str, list[dict]] = {}
    for it in items:
        out.setdefault(it.get("group", "Other"), []).append(it)
    return out


def trigger_event_groups() -> dict[str, list[dict]]:
    return grouped(TRIGGER_EVENTS)


def condition_field_groups() -> dict[str, list[dict]]:
    return grouped(CONDITION_FIELDS)


def action_type_groups() -> dict[str, list[dict]]:
    return grouped(ACTION_TYPES)
