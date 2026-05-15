"""
Permission catalog — single source of truth for what permission keys exist
in the app and which built-in roles get which permissions by default.

Source-of-truth model: permission keys live in code (this file), NOT in the DB.
The DB only stores which roles have which keys (role_permissions table). When
you add a new enforcement point in app.py, register the key here so:
  - It shows up in the /admin/roles permission matrix
  - seed_access_control() can backfill it on existing roles if needed
  - require('your.key') validates against ALL_KEYS to catch typos

Each permission has:
  group       — UI grouping (Tickets / AI / Admin / System)
  key         — the actual string used in code (require('tickets.public_reply'))
  label       — human-readable name for the role matrix UI
  description — tooltip text explaining what this gates
  destructive — True for actions that change state (used to highlight in UI)
"""

from typing import TypedDict


class Permission(TypedDict):
    group: str
    key: str
    label: str
    description: str
    destructive: bool


# Ordered list — the order here is the display order in the role matrix.
# Keep groups contiguous and the order within a group sensible (view first,
# then read-ish actions, then writes, then dangerous writes last).
PERMISSIONS: list[Permission] = [
    # ---------- Tickets ----------
    {"group": "Tickets", "key": "tickets.view", "label": "View tickets",
     "description": "See ticket lists, views, and ticket detail pages.",
     "destructive": False},
    {"group": "Tickets", "key": "tickets.search", "label": "Search tickets",
     "description": "Use the global search bar and filter views.",
     "destructive": False},
    {"group": "Tickets", "key": "tickets.edit_fields", "label": "Edit fields",
     "description": "Change priority, status, assignee, custom fields. Writes back to Zendesk.",
     "destructive": True},
    {"group": "Tickets", "key": "tickets.internal_note", "label": "Post internal note",
     "description": "Add private agent-only notes (not visible to customer).",
     "destructive": True},
    {"group": "Tickets", "key": "tickets.public_reply", "label": "Send public reply",
     "description": "Reply to the customer. Writes to Zendesk (visible to requester).",
     "destructive": True},
    {"group": "Tickets", "key": "tickets.change_status", "label": "Change status",
     "description": "Move tickets between Open / Pending / On-hold / Solved.",
     "destructive": True},
    {"group": "Tickets", "key": "tickets.assign", "label": "Assign tickets",
     "description": "Change assignee or group on a ticket.",
     "destructive": True},
    {"group": "Tickets", "key": "tickets.assign_self", "label": "Assign tickets to me",
     "description": "Click 'Assign to me' to take ownership of a ticket. Requires your account is mapped to a Zendesk user.",
     "destructive": True},
    {"group": "Tickets", "key": "tickets.assign_others", "label": "Assign tickets to others",
     "description": "Reassign a ticket to any other agent in the matching group.",
     "destructive": True},
    {"group": "Tickets", "key": "tickets.bulk_actions", "label": "Bulk actions",
     "description": "Multi-select and bulk-edit tickets from a view.",
     "destructive": True},
    {"group": "Tickets", "key": "tickets.merge", "label": "Merge tickets",
     "description": "Merge duplicate tickets together. Cannot be undone.",
     "destructive": True},
    {"group": "Tickets", "key": "tickets.delete", "label": "Delete tickets",
     "description": "Permanently delete tickets. Use with extreme care.",
     "destructive": True},

    # ---------- AI ----------
    {"group": "AI", "key": "ai.view_insights", "label": "View AI insights",
     "description": "See the AI insights panel on tickets (summary, history, recommended action).",
     "destructive": False},
    {"group": "AI", "key": "ai.view_similar", "label": "View similar tickets",
     "description": "See the suggested similar-tickets panel.",
     "destructive": False},
    {"group": "AI", "key": "ai.view_suggested_reply", "label": "View suggested replies",
     "description": "See the AI-drafted suggested reply on tickets.",
     "destructive": False},
    {"group": "AI", "key": "ai.request_reanalyze", "label": "Re-analyze a ticket",
     "description": "Trigger a fresh AI analysis on a single ticket.",
     "destructive": False},
    {"group": "AI", "key": "ai.feedback", "label": "Submit AI feedback",
     "description": "Thumbs-up / thumbs-down AI suggestions to improve the model.",
     "destructive": False},
    {"group": "AI", "key": "ai.translate", "label": "Use translation",
     "description": "Translate ticket comments and drafts via the translate dropdown.",
     "destructive": False},

    # ---------- Admin ----------
    {"group": "Admin", "key": "admin.view", "label": "Open admin panel",
     "description": "Access /admin at all. Sub-sections require their own keys.",
     "destructive": False},
    {"group": "Admin", "key": "admin.users", "label": "Manage users",
     "description": "Invite, disable, and assign roles to users. CRITICAL — handle with care.",
     "destructive": True},
    {"group": "Admin", "key": "admin.roles", "label": "Manage roles & permissions",
     "description": "Create roles, edit permission matrices. CRITICAL — handle with care.",
     "destructive": True},
    {"group": "Admin", "key": "admin.forms", "label": "Manage ticket forms",
     "description": "Edit /admin/forms — field order, conditional rules.",
     "destructive": True},
    {"group": "Admin", "key": "admin.sla", "label": "Manage SLAs & business hours",
     "description": "Edit /admin/sla and /admin/business-hours.",
     "destructive": True},
    {"group": "Admin", "key": "admin.agents", "label": "Manage native agents",
     "description": "Edit the native agent roster + availability.",
     "destructive": True},
    {"group": "Admin", "key": "admin.assignment", "label": "Manage assignment rules",
     "description": "Edit /admin/assignment — round-robin and routing rules.",
     "destructive": True},
    {"group": "Admin", "key": "admin.automations", "label": "Manage automations",
     "description": "Edit /admin/automations — triggers and schedulers.",
     "destructive": True},
    {"group": "Admin", "key": "admin.auto_replies", "label": "Manage auto-replies",
     "description": "Edit /admin/auto-replies.",
     "destructive": True},
    {"group": "Admin", "key": "admin.fields", "label": "Manage fields",
     "description": "Edit /admin/fields — required overrides, custom statuses, native fields.",
     "destructive": True},
    {"group": "Admin", "key": "admin.gmail", "label": "Manage Gmail intake",
     "description": "Edit /admin/gmail config.",
     "destructive": True},
    {"group": "Admin", "key": "admin.ai_worker", "label": "Control AI worker",
     "description": "Start/stop the AI worker, change model, trigger re-analyze bulk.",
     "destructive": True},
    {"group": "Admin", "key": "admin.attachments", "label": "Run attachments backfill",
     "description": "Start/stop the attachments backfill subprocess.",
     "destructive": True},
    {"group": "Admin", "key": "admin.tunnel", "label": "Manage Cloudflare tunnel",
     "description": "Start/stop the public tunnel, view its URL.",
     "destructive": True},
    {"group": "Admin", "key": "admin.feature_flags", "label": "Toggle feature flags",
     "description": "Future: enable/disable beta features per env.",
     "destructive": True},
    {"group": "Admin", "key": "admin.groups", "label": "Manage groups",
     "description": "Create native groups, rename, archive. ZD-synced groups remain read-only.",
     "destructive": True},
    {"group": "Admin", "key": "admin.feedback", "label": "Manage feedback inbox",
     "description": "Read, triage, reply, close user-submitted feedback.",
     "destructive": False},
    {"group": "Admin", "key": "admin.end_users", "label": "Manage end-users & portal access",
     "description": "View and edit end-user profiles, enable/disable portal access, send portal invites, reset portal passwords. Powers /admin/end-users.",
     "destructive": True},

    # ---------- Views ----------
    {"group": "Views", "key": "views.create_personal", "label": "Create personal views",
     "description": "Save a filtered ticket list as a personal view (only visible to you).",
     "destructive": False},
    {"group": "Views", "key": "views.create_shared", "label": "Share views",
     "description": "Share your views with other users or groups.",
     "destructive": False},
    {"group": "Views", "key": "views.manage_all", "label": "Manage all views",
     "description": "Edit or delete views owned by anyone, including the seeded system views. Admin-only by default.",
     "destructive": True},

    # ---------- System ----------
    {"group": "System", "key": "system.audit_view", "label": "View audit logs",
     "description": "See the global audit log + access-control audit trail.",
     "destructive": False},
    {"group": "System", "key": "system.export", "label": "Export data",
     "description": "Download CSV/JSON exports of tickets and insights.",
     "destructive": False},
    {"group": "System", "key": "system.sync_zendesk", "label": "Run Zendesk sync",
     "description": "Trigger the sync worker on demand.",
     "destructive": True},
]


# ---- Derived constants used by the rest of the app -------------------------

ALL_KEYS: set[str] = {p["key"] for p in PERMISSIONS}

GROUPS: list[str] = []
for _p in PERMISSIONS:
    if _p["group"] not in GROUPS:
        GROUPS.append(_p["group"])

PERMISSIONS_BY_GROUP: dict[str, list[Permission]] = {g: [] for g in GROUPS}
for _p in PERMISSIONS:
    PERMISSIONS_BY_GROUP[_p["group"]].append(_p)

PERMISSION_LABELS: dict[str, str] = {p["key"]: p["label"] for p in PERMISSIONS}
PERMISSION_DESCRIPTIONS: dict[str, str] = {p["key"]: p["description"] for p in PERMISSIONS}


# ---- Built-in role definitions ---------------------------------------------
# These are SEEDED on first install by db.seed_access_control() and ONLY for
# system-default roles (is_system_default=1). Admins can edit them afterwards
# — the seed only fills perms when a role currently has zero perms (i.e. fresh
# install), it doesn't overwrite existing customizations.

# Admin: every permission, system-default (can't be deleted).
_ADMIN_PERMS: set[str] = set(ALL_KEYS)

# Agent: full ticket workflow + AI usage, NO /admin/* access.
# Includes assign-self / assign-others and personal/shared view creation
# (engineers + agents both want to slice the queue their own way).
_AGENT_PERMS: set[str] = {
    "tickets.view", "tickets.search", "tickets.edit_fields",
    "tickets.internal_note", "tickets.public_reply",
    "tickets.change_status", "tickets.assign", "tickets.bulk_actions",
    "tickets.assign_self", "tickets.assign_others",
    "ai.view_insights", "ai.view_similar", "ai.view_suggested_reply",
    "ai.request_reanalyze", "ai.feedback", "ai.translate",
    "views.create_personal", "views.create_shared",
}

# View-only: pure read access. The role we hand to engineers first.
_VIEWER_PERMS: set[str] = {
    "tickets.view", "tickets.search",
    "ai.view_insights", "ai.view_similar", "ai.view_suggested_reply",
}


DEFAULT_ROLES: dict[str, dict] = {
    "Admin": {
        "description": "Full access to everything. System role — cannot be deleted.",
        "is_system_default": True,
        "permissions": sorted(_ADMIN_PERMS),
    },
    "Agent": {
        "description": "Full ticket workflow — edit, reply, change status. No admin panel.",
        "is_system_default": True,
        "permissions": sorted(_AGENT_PERMS),
    },
    "View-only": {
        "description": "Read tickets and AI insights. No edits, no replies, no admin.",
        "is_system_default": True,
        "permissions": sorted(_VIEWER_PERMS),
    },
}


def describe(key: str) -> str:
    """Friendly label for a permission key. Returns the key itself if unknown."""
    return PERMISSION_LABELS.get(key, key)
