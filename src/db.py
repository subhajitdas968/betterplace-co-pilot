"""SQLite layer. One file at data/copilot.db."""
from __future__ import annotations
import html
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import DATA_DIR, DB_PATH


_TAG_RE = re.compile(r"<[^>]+>")


def clean_body(text: str | None) -> str:
    """Decode HTML entities and strip stray tags. Used for ticket comment bodies."""
    if not text:
        return ""
    # Repeated unescape — sometimes &amp;nbsp; needs two passes
    s = html.unescape(html.unescape(text))
    s = _TAG_RE.sub("", s)
    # Replace non-breaking spaces and stray double-encoded entities
    s = s.replace(" ", " ").replace("&nbsp;", " ")
    return s


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    name TEXT,
    email TEXT,
    role TEXT,
    raw TEXT
);

CREATE TABLE IF NOT EXISTS organizations (
    id INTEGER PRIMARY KEY,
    name TEXT,
    raw TEXT
);

CREATE TABLE IF NOT EXISTS groups (
    id INTEGER PRIMARY KEY,
    name TEXT
);

CREATE TABLE IF NOT EXISTS ticket_fields (
    id INTEGER PRIMARY KEY,
    title TEXT,
    type TEXT,
    required INTEGER,
    options TEXT  -- JSON array of {name, value}
);

CREATE TABLE IF NOT EXISTS ticket_forms (
    id INTEGER PRIMARY KEY,
    name TEXT,
    display_name TEXT,
    active INTEGER,
    field_ids TEXT,  -- JSON array of int
    raw TEXT
);

CREATE TABLE IF NOT EXISTS ticket_metrics (
    ticket_id INTEGER PRIMARY KEY,
    reply_time_in_minutes INTEGER,
    first_resolution_time_in_minutes INTEGER,
    full_resolution_time_in_minutes INTEGER,
    agent_wait_time_in_minutes INTEGER,
    requester_wait_time_in_minutes INTEGER,
    on_hold_time_in_minutes INTEGER,
    latest_comment_added_at TEXT,
    initially_assigned_at TEXT,
    assignee_updated_at TEXT,
    requester_updated_at TEXT,
    raw TEXT,
    FOREIGN KEY (ticket_id) REFERENCES tickets(id)
);
CREATE INDEX IF NOT EXISTS idx_metrics_latest ON ticket_metrics(latest_comment_added_at);

CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY,
    subject TEXT,
    status TEXT,
    priority TEXT,
    type TEXT,
    requester_id INTEGER,
    organization_id INTEGER,
    assignee_id INTEGER,
    group_id INTEGER,
    tags TEXT,            -- JSON array
    custom_fields TEXT,   -- JSON {field_id: value} — refreshed from ZD on every sync
    created_at TEXT,
    updated_at TEXT,
    solved_at TEXT,
    raw TEXT,             -- full Zendesk payload
    last_analyzed_updated_at TEXT,
    -- Block #1 additions: native model
    source TEXT DEFAULT 'zendesk',  -- 'zendesk' | 'gmail' | 'native'
    local_id TEXT,                  -- 'BP-000001' for native; NULL for zendesk-synced
    external_id TEXT,               -- ZD ticket id (string) for synced; NULL for native
    local_overrides TEXT DEFAULT '{}', -- JSON: {custom_fields: {fid: val}, status, priority, ...}
    locally_created_at TEXT         -- when a native ticket was created
);
CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status);
CREATE INDEX IF NOT EXISTS idx_tickets_group ON tickets(group_id);
CREATE INDEX IF NOT EXISTS idx_tickets_assignee ON tickets(assignee_id);
CREATE INDEX IF NOT EXISTS idx_tickets_org ON tickets(organization_id);
CREATE INDEX IF NOT EXISTS idx_tickets_updated ON tickets(updated_at);

CREATE TABLE IF NOT EXISTS ticket_comments (
    id INTEGER PRIMARY KEY,
    ticket_id INTEGER,
    author_id INTEGER,
    public INTEGER,
    body TEXT,
    html_body TEXT,
    created_at TEXT,
    FOREIGN KEY (ticket_id) REFERENCES tickets(id)
);
CREATE INDEX IF NOT EXISTS idx_comments_ticket ON ticket_comments(ticket_id);

CREATE TABLE IF NOT EXISTS ticket_insights (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER,
    model TEXT,
    summary TEXT,
    recommendations TEXT,  -- JSON array of {field, current, suggest, confidence, reason, review?}
    completeness TEXT,     -- JSON array of {state, text, hint?}
    similar_ticket_ids TEXT, -- JSON array of int
    suggested_reply TEXT,  -- JSON object or null
    kb_worthy INTEGER,
    kb_topic TEXT,
    pickup_flag TEXT,      -- JSON object or null
    created_at TEXT,
    cost_usd REAL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cached_input_tokens INTEGER,
    FOREIGN KEY (ticket_id) REFERENCES tickets(id)
);
CREATE INDEX IF NOT EXISTS idx_insights_ticket ON ticket_insights(ticket_id);
CREATE INDEX IF NOT EXISTS idx_insights_created ON ticket_insights(created_at);

CREATE TABLE IF NOT EXISTS spend_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER,
    cost_usd REAL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cached_input_tokens INTEGER,
    model TEXT,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_spend_created ON spend_log(created_at);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_email TEXT,
    action TEXT,
    target_type TEXT,
    target_id TEXT,
    detail TEXT,
    created_at TEXT
);

-- Captures every approve/reject decision an agent makes on an AI suggestion.
-- Feeds back into the prompt so the system learns BetterPlace conventions over time.
CREATE TABLE IF NOT EXISTS ai_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER,
    insight_id INTEGER,
    field_name TEXT,
    ai_current_value TEXT,
    ai_suggested_value TEXT,
    ai_confidence REAL,
    decision TEXT,                -- 'approved', 'rejected', 'edited'
    final_value TEXT,             -- the value the agent actually wants
    rejection_reason TEXT,
    actor_email TEXT,
    created_at TEXT,
    FOREIGN KEY (ticket_id) REFERENCES tickets(id),
    FOREIGN KEY (insight_id) REFERENCES ticket_insights(id)
);
CREATE INDEX IF NOT EXISTS idx_feedback_ticket ON ai_feedback(ticket_id);
CREATE INDEX IF NOT EXISTS idx_feedback_field ON ai_feedback(field_name);

-- AI worker config — single-row table (id=1) so we always update-in-place.
CREATE TABLE IF NOT EXISTS ai_worker_config (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    enabled INTEGER DEFAULT 0,
    model TEXT DEFAULT 'sonnet',         -- 'haiku' | 'sonnet' | 'opus'
    batch_size INTEGER DEFAULT 10,
    poll_interval_seconds INTEGER DEFAULT 60,
    daily_ticket_cap INTEGER DEFAULT 200,
    use_mcp INTEGER DEFAULT 1,           -- 1 = Claude Desktop / Code; 0 = metered API
    process_pid INTEGER,                 -- pid of the running worker loop, NULL if stopped
    last_started_at TEXT,
    last_stopped_at TEXT,
    updated_at TEXT
);

-- Mirror of Zendesk custom statuses (Suite Professional+).
CREATE TABLE IF NOT EXISTS custom_statuses (
    id INTEGER PRIMARY KEY,
    status_category TEXT,
    agent_label TEXT,
    end_user_label TEXT,
    description TEXT,
    active INTEGER,
    raw TEXT
);

-- Local repository of new dropdown options we've proposed/added beyond Zendesk's
-- original definitions. Lets us track our independent taxonomy growth.
CREATE TABLE IF NOT EXISTS local_field_options (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    field_id INTEGER,
    option_name TEXT,
    option_value TEXT,
    proposed_by_email TEXT,
    sync_status TEXT,        -- 'pending' | 'synced_to_zd' | 'failed'
    sync_error TEXT,
    created_at TEXT,
    FOREIGN KEY (field_id) REFERENCES ticket_fields(id)
);
CREATE INDEX IF NOT EXISTS idx_lfo_field ON local_field_options(field_id);

-- Leadership dashboard widgets
CREATE TABLE IF NOT EXISTS dashboard_widgets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT,
    widget_type TEXT,        -- 'kpi' | 'group_table' | 'list'
    config TEXT,             -- JSON
    position INTEGER DEFAULT 0,
    created_by TEXT,
    created_at TEXT
);

-- Ticket attachments — captured from Zendesk comments and (optionally) downloaded
-- to local disk. We always store the metadata; binary download is opt-in (toggle
-- on the /admin/attachments page) because some comments have large files we
-- don't want to pull until the agent opens them.
CREATE TABLE IF NOT EXISTS ticket_attachments (
    id INTEGER PRIMARY KEY,           -- Zendesk attachment id (negative for native uploads)
    ticket_id INTEGER NOT NULL,
    comment_id INTEGER,               -- nullable — could be ticket-level attachment in the future
    file_name TEXT,
    content_type TEXT,
    size_bytes INTEGER,
    content_url TEXT,                 -- temporary ZD signed URL — refreshed on next sync
    local_path TEXT,                  -- relative path under data/attachments/, NULL if not downloaded
    downloaded_at TEXT,
    source TEXT DEFAULT 'zendesk',    -- 'zendesk' | 'native'
    raw TEXT,                         -- full JSON for forensics
    created_at TEXT,
    FOREIGN KEY (ticket_id) REFERENCES tickets(id),
    FOREIGN KEY (comment_id) REFERENCES ticket_comments(id)
);
CREATE INDEX IF NOT EXISTS idx_attachments_ticket  ON ticket_attachments(ticket_id);
CREATE INDEX IF NOT EXISTS idx_attachments_comment ON ticket_attachments(comment_id);

-- Native forms — defined entirely in this tool. Zendesk ticket_forms are still
-- imported (for the existing 13 forms) but those are read-only. New + edited
-- forms live in native_forms and are owned by us.
--
-- group_ids: JSON array of group IDs the form applies to. A ticket whose group
--            is in this list will, by default, use this form (if multiple match,
--            the lowest position wins).
-- field_ids: JSON array of ticket_fields.id rows shown on the form, in order.
-- required_field_ids: JSON array of field ids treated as REQUIRED on this form
--            (overrides the field's global `required` flag).
CREATE TABLE IF NOT EXISTS native_forms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT,
    active INTEGER DEFAULT 1,
    group_ids TEXT DEFAULT '[]',
    field_ids TEXT DEFAULT '[]',
    required_field_ids TEXT DEFAULT '[]',
    position INTEGER DEFAULT 0,
    created_by TEXT,
    created_at TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_native_forms_active ON native_forms(active);

-- Conditional-visibility rules. The reading is:
--   "show target_field_id only when source_field_id <op> source_value"
-- op is 'eq' | 'neq' | 'set' | 'unset'. When 'set' / 'unset', source_value is ignored.
-- Multiple rules can target the same field — they're OR'd.
CREATE TABLE IF NOT EXISTS native_form_conditions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    form_id INTEGER NOT NULL,
    target_field_id INTEGER NOT NULL,
    source_field_id INTEGER NOT NULL,
    op TEXT NOT NULL,            -- 'eq' | 'neq' | 'set' | 'unset'
    source_value TEXT,
    created_at TEXT,
    FOREIGN KEY (form_id) REFERENCES native_forms(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_form_conditions_form   ON native_form_conditions(form_id);
CREATE INDEX IF NOT EXISTS idx_form_conditions_target ON native_form_conditions(target_field_id);

-- Business hours — Zendesk-replica. Each schedule has a name + IANA timezone,
-- weekly intervals, and a holiday calendar. A ticket's clock pauses outside
-- business hours when the SLA policy uses business_hours mode.
--
-- weekly_intervals: JSON array of {day:0..6, start:"HH:MM", end:"HH:MM"} (UTC of
--   the schedule's timezone — we apply tz at compute time). 0=Sunday, 6=Saturday.
-- holidays: JSON array of "YYYY-MM-DD" — full days excluded.
CREATE TABLE IF NOT EXISTS business_hours (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT,
    timezone TEXT DEFAULT 'Asia/Kolkata',
    weekly_intervals TEXT DEFAULT '[]',
    holidays TEXT DEFAULT '[]',
    is_default INTEGER DEFAULT 0,
    created_by TEXT,
    created_at TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_business_hours_default ON business_hours(is_default);

-- SLA policies. Each policy:
--   - applies based on conditions (priority, group, customer)
--   - has target minutes for one or more clocks: first_reply, next_reply, resolution
--   - uses either business or calendar hours
--
-- applies_to: JSON {priority: ['high','urgent'], group_ids: [...], customer_values: [...]}
-- targets: JSON {first_reply: {minutes: 60}, next_reply: {minutes: 240}, resolution: {minutes: 1440}}
CREATE TABLE IF NOT EXISTS sla_policies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT,
    active INTEGER DEFAULT 1,
    applies_to TEXT DEFAULT '{}',
    targets TEXT DEFAULT '{}',
    clock_type TEXT DEFAULT 'business',     -- 'business' | 'calendar'
    business_hours_id INTEGER,
    position INTEGER DEFAULT 0,
    created_by TEXT,
    created_at TEXT,
    updated_at TEXT,
    FOREIGN KEY (business_hours_id) REFERENCES business_hours(id)
);
CREATE INDEX IF NOT EXISTS idx_sla_policies_active ON sla_policies(active);

-- Customer-level business-hours override. When set, this customer's SLA uses
-- these hours instead of the policy's default. Used for customers on a different
-- timezone / shift than the support org.
CREATE TABLE IF NOT EXISTS customer_business_hours (
    customer_value TEXT PRIMARY KEY,
    business_hours_id INTEGER NOT NULL,
    FOREIGN KEY (business_hours_id) REFERENCES business_hours(id)
);

-- Per-ticket SLA snapshot. Recomputed every time we touch a ticket so the list
-- view doesn't have to do the math each render.
CREATE TABLE IF NOT EXISTS ticket_sla (
    ticket_id INTEGER PRIMARY KEY,
    policy_id INTEGER,
    first_reply_target_minutes INTEGER,
    first_reply_elapsed_minutes INTEGER,
    first_reply_state TEXT,             -- 'ok' | 'warn' | 'breached' | 'met'
    next_reply_target_minutes INTEGER,
    next_reply_elapsed_minutes INTEGER,
    next_reply_state TEXT,
    resolution_target_minutes INTEGER,
    resolution_elapsed_minutes INTEGER,
    resolution_state TEXT,
    updated_at TEXT,
    FOREIGN KEY (ticket_id) REFERENCES tickets(id),
    FOREIGN KEY (policy_id) REFERENCES sla_policies(id)
);
CREATE INDEX IF NOT EXISTS idx_ticket_sla_state ON ticket_sla(first_reply_state, next_reply_state, resolution_state);

-- =============================================================================
-- Native agents + availability (Block #4)
-- =============================================================================
-- We don't replace the users table — Zendesk-synced agents stay there. Instead
-- we layer a 'native_agents' record that points to a users row and adds the
-- extra columns: availability state, max parallel tickets, group memberships.
CREATE TABLE IF NOT EXISTS native_agents (
    user_id INTEGER PRIMARY KEY,             -- FK into users(id)
    display_name TEXT,
    availability TEXT DEFAULT 'offline',     -- 'online' | 'away' | 'busy' | 'offline'
    max_open_tickets INTEGER DEFAULT 20,
    group_ids TEXT DEFAULT '[]',             -- JSON array of group ids the agent picks up from
    active INTEGER DEFAULT 1,
    last_assigned_at TEXT,
    created_at TEXT,
    updated_at TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_native_agents_availability ON native_agents(availability);
CREATE INDEX IF NOT EXISTS idx_native_agents_active ON native_agents(active);

-- =============================================================================
-- Round-robin assignment config (Block #5)
-- =============================================================================
-- One row per group (or per customer scope) describing how new tickets get
-- handed to agents. The simplest config is "round-robin among online agents
-- in this group, weighted by current open load".
CREATE TABLE IF NOT EXISTS assignment_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    active INTEGER DEFAULT 1,
    scope_group_id INTEGER,                  -- nullable = applies to any group
    scope_customer_value TEXT,               -- nullable
    strategy TEXT DEFAULT 'round_robin',     -- 'round_robin' | 'least_loaded' | 'manual'
    only_online INTEGER DEFAULT 1,
    fallback_group_id INTEGER,
    position INTEGER DEFAULT 100,
    created_by TEXT, created_at TEXT, updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_assignment_rules_active ON assignment_rules(active);

-- =============================================================================
-- Automations engine (Block #6)
-- =============================================================================
-- Rules engine. A rule has: when (event or time trigger), if (conditions
-- against the ticket), then (one or more actions).
--
-- trigger_type: 'on_create' | 'on_update' | 'time_elapsed' | 'on_status_change'
-- conditions_json: array of {field, op, value} — same op vocab as forms
-- actions_json: array of {type, params}
--   action types: 'set_field', 'set_status', 'set_priority', 'set_group',
--                 'assign_agent', 'add_internal_note', 'send_auto_reply'
CREATE TABLE IF NOT EXISTS automations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT,
    active INTEGER DEFAULT 1,
    trigger_type TEXT NOT NULL,
    trigger_params TEXT DEFAULT '{}',
    conditions_json TEXT DEFAULT '[]',
    actions_json TEXT DEFAULT '[]',
    position INTEGER DEFAULT 100,
    run_count INTEGER DEFAULT 0,
    last_run_at TEXT,
    created_by TEXT, created_at TEXT, updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_automations_active ON automations(active);
CREATE INDEX IF NOT EXISTS idx_automations_trigger ON automations(trigger_type);

-- =============================================================================
-- Auto-replies (Block #7)
-- =============================================================================
-- Templates fired by the automations engine. Scoped by group / customer.
-- after_hours: 'always' | 'business_only' | 'after_hours_only'
CREATE TABLE IF NOT EXISTS auto_replies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    active INTEGER DEFAULT 1,
    scope_group_id INTEGER,
    scope_customer_value TEXT,
    body TEXT NOT NULL,                       -- Markdown — supports {{customer}}, {{ticket_id}} placeholders
    after_hours TEXT DEFAULT 'always',
    business_hours_id INTEGER,
    fire_on TEXT DEFAULT 'create',            -- 'create' | 'first_response_late' | 'business_open'
    sent_count INTEGER DEFAULT 0,
    created_by TEXT, created_at TEXT, updated_at TEXT,
    FOREIGN KEY (business_hours_id) REFERENCES business_hours(id)
);
CREATE INDEX IF NOT EXISTS idx_auto_replies_active ON auto_replies(active);

-- =============================================================================
-- Native custom fields (Block #3)
-- =============================================================================
-- We don't replace ticket_fields — Zendesk-synced fields stay there read-only.
-- New fields created in this tool live in native_fields, and a small adapter in
-- _full_ticket merges them in. ID range starts at 9_000_000_000 to avoid
-- collisions with ZD's 14-digit field ids.
CREATE TABLE IF NOT EXISTS native_fields (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    type TEXT NOT NULL,                       -- 'text' | 'tagger' | 'multiselect' | 'integer' | 'decimal' | 'date' | 'checkbox' | 'textarea'
    required INTEGER DEFAULT 0,
    options TEXT DEFAULT '[]',                -- JSON array of {name, value}
    description TEXT,
    active INTEGER DEFAULT 1,
    position INTEGER DEFAULT 100,
    created_by TEXT, created_at TEXT, updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_native_fields_active ON native_fields(active);

-- =============================================================================
-- Gmail intake (Block #2)
-- =============================================================================
-- Config for the Gmail watcher. Subject of a thread = ticket subject, first
-- email's body = description. Replies on the same thread append as comments.
CREATE TABLE IF NOT EXISTS gmail_config (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    enabled INTEGER DEFAULT 0,
    mailbox_email TEXT,
    label_in TEXT DEFAULT 'INBOX',
    label_processed TEXT DEFAULT 'cowork-processed',
    default_group_id INTEGER,
    default_customer_value TEXT,
    poll_interval_seconds INTEGER DEFAULT 60,
    last_poll_at TEXT,
    last_history_id TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS gmail_threads (
    gmail_thread_id TEXT PRIMARY KEY,
    ticket_id INTEGER,
    last_message_id TEXT,
    last_seen_at TEXT,
    FOREIGN KEY (ticket_id) REFERENCES tickets(id)
);
CREATE INDEX IF NOT EXISTS idx_gmail_threads_ticket ON gmail_threads(ticket_id);

-- Per-ticket audit log. One row per discrete event so the ticket-detail History
-- panel can render a clean timeline. ZD audits pulled via /api/v2/tickets/{id}/audits
-- land here with source='zendesk'; native events land with source='native' (and
-- the actor email of the agent or 'system' / 'automation' for rule-driven changes).
--
-- before_json / after_json carry structured deltas so the UI can show
-- "Priority: normal → high" without parsing free-text strings.
CREATE TABLE IF NOT EXISTS ticket_audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL,
    event_type TEXT NOT NULL,            -- 'comment.public' | 'field.changed' | 'status.changed' | 'tag.added' | 'rule.fired' | 'sla.warn' | ...
    event_summary TEXT,                  -- human-readable short label
    actor_email TEXT,                    -- 'system' | 'automation:<id>' | agent email | 'zd:<user_id>'
    actor_type TEXT,                     -- 'agent' | 'system' | 'automation' | 'customer' | 'zd'
    field_key TEXT,                      -- when applicable (e.g. 'priority' or '15315331275025')
    before_json TEXT,
    after_json TEXT,
    raw TEXT,                            -- full payload (ZD audit row, action params, etc.)
    source TEXT DEFAULT 'native',        -- 'native' | 'zendesk'
    created_at TEXT,
    FOREIGN KEY (ticket_id) REFERENCES tickets(id)
);
CREATE INDEX IF NOT EXISTS idx_audit_ticket_created ON ticket_audit_log(ticket_id, created_at);
CREATE INDEX IF NOT EXISTS idx_audit_event_type ON ticket_audit_log(event_type);

-- ===== Access Control (F0) =====
-- App-level users (sign-in identities). Distinct from `users` table which
-- holds Zendesk-synced people (customers + agents from ZD). On Google OAuth
-- login, we upsert a row here keyed by email and resolve roles → permissions.
CREATE TABLE IF NOT EXISTS app_users (
    email TEXT PRIMARY KEY,                  -- @betterplace.co.in addresses
    name TEXT,
    picture_url TEXT,
    status TEXT NOT NULL DEFAULT 'active',   -- 'active' | 'disabled'
    last_login_at TEXT,
    created_at TEXT NOT NULL,
    created_by TEXT,                         -- email of admin who invited, or NULL for self-signup
    notes TEXT                               -- admin-only free text
);
CREATE INDEX IF NOT EXISTS idx_app_users_status ON app_users(status);

CREATE TABLE IF NOT EXISTS roles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,               -- 'Admin' | 'Agent' | 'View-only' | <custom>
    description TEXT,
    is_system_default INTEGER NOT NULL DEFAULT 0,   -- 1 = cannot delete (Admin)
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Flat list of permission keys held by a role. Keys are defined in
-- src/permissions.py (the source of truth). This table is just the
-- many-to-many between roles and which permissions they grant.
CREATE TABLE IF NOT EXISTS role_permissions (
    role_id INTEGER NOT NULL,
    permission_key TEXT NOT NULL,
    granted_at TEXT NOT NULL,
    PRIMARY KEY (role_id, permission_key),
    FOREIGN KEY (role_id) REFERENCES roles(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_role_permissions_key ON role_permissions(permission_key);

-- Multi-role: a user can hold any number of roles. Effective permissions
-- are the union across all their roles.
CREATE TABLE IF NOT EXISTS user_roles (
    user_email TEXT NOT NULL,
    role_id INTEGER NOT NULL,
    granted_at TEXT NOT NULL,
    granted_by TEXT,                         -- admin email who granted
    PRIMARY KEY (user_email, role_id),
    FOREIGN KEY (user_email) REFERENCES app_users(email) ON DELETE CASCADE,
    FOREIGN KEY (role_id) REFERENCES roles(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_user_roles_email ON user_roles(user_email);
CREATE INDEX IF NOT EXISTS idx_user_roles_role ON user_roles(role_id);

-- Audit trail for access-control actions (user invited, role granted/revoked,
-- permission added to role, etc.). Distinct from ticket_audit_log and the
-- generic audit_log so we can show a focused "access events" view.
CREATE TABLE IF NOT EXISTS access_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_email TEXT NOT NULL,               -- who did the action
    event_type TEXT NOT NULL,                -- 'user.invite' | 'user.disable' | 'user.enable' | 'role.grant' | 'role.revoke' | 'role.create' | 'role.update' | 'role.delete' | 'perm.add' | 'perm.remove'
    target_kind TEXT,                        -- 'user' | 'role'
    target_id TEXT,                          -- email or role_id
    detail_json TEXT,                        -- {permission_key:..., role_id:..., before:..., after:...}
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_access_audit_created ON access_audit(created_at);
CREATE INDEX IF NOT EXISTS idx_access_audit_actor ON access_audit(actor_email);
CREATE INDEX IF NOT EXISTS idx_access_audit_target ON access_audit(target_kind, target_id);

-- ===== Group membership (F0+) =====
-- Maps app_users (sign-in identities) to groups (ZD-synced or native).
-- Used for: routing (which group a user can act for), view sharing
-- (share-with-group), filtering ("show me tickets for my groups").
CREATE TABLE IF NOT EXISTS user_groups (
    user_email TEXT NOT NULL,
    group_id INTEGER NOT NULL,
    granted_at TEXT NOT NULL,
    granted_by TEXT,
    PRIMARY KEY (user_email, group_id),
    FOREIGN KEY (user_email) REFERENCES app_users(email) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_user_groups_email ON user_groups(user_email);
CREATE INDEX IF NOT EXISTS idx_user_groups_group ON user_groups(group_id);

-- ===== Native views (F0+) =====
-- Saved searches with filter + columns. Replaces the hardcoded STATIC_VIEWS
-- so admins can create their own and agents can save personal views.
-- owner_email NULL = system view (seeded, can't be deleted by non-admins).
-- scope: 'personal' = only owner sees; 'shared' = owner + everyone listed
-- in view_shares; 'system' = everyone sees (the seeded defaults).
CREATE TABLE IF NOT EXISTS native_views (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT,
    owner_email TEXT,                          -- NULL for system views
    scope TEXT NOT NULL DEFAULT 'personal',    -- 'personal' | 'shared' | 'system'
    filter_json TEXT NOT NULL DEFAULT '{}',    -- {rules:[{field:..., op:..., value:...}], match:'all'|'any'}
    column_ids_json TEXT NOT NULL DEFAULT '[]',-- ordered list of column keys to render
    sort_json TEXT NOT NULL DEFAULT '{}',      -- {field:'updated_at', dir:'desc'}
    color TEXT DEFAULT 'indigo',               -- sidebar chip color
    icon TEXT DEFAULT '',                      -- optional emoji prefix
    is_system_default INTEGER NOT NULL DEFAULT 0,  -- 1 = seeded, re-seeded on boot if deleted
    default_position INTEGER NOT NULL DEFAULT 0,   -- creator's natural order (per-user override in app_users.view_order_json)
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_native_views_owner ON native_views(owner_email);
CREATE INDEX IF NOT EXISTS idx_native_views_scope ON native_views(scope);
CREATE INDEX IF NOT EXISTS idx_native_views_active ON native_views(active);

-- Shares — who can see a 'shared' view besides its owner.
CREATE TABLE IF NOT EXISTS view_shares (
    view_id INTEGER NOT NULL,
    share_kind TEXT NOT NULL,            -- 'user' | 'group'
    share_id TEXT NOT NULL,              -- email (for user) or group_id-as-text (for group)
    granted_at TEXT NOT NULL,
    granted_by TEXT,
    PRIMARY KEY (view_id, share_kind, share_id),
    FOREIGN KEY (view_id) REFERENCES native_views(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_view_shares_view ON view_shares(view_id);
CREATE INDEX IF NOT EXISTS idx_view_shares_user ON view_shares(share_kind, share_id);

-- ===== F6 · User activity log =====
-- Every meaningful action a user takes lands here so we can build reports
-- (login frequency, time-on-tool, ticket touch rate, etc.). Append-only.
-- event_type is the broad category, event_subtype the specific action.
CREATE TABLE IF NOT EXISTS user_activity_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_email TEXT NOT NULL,
    event_type TEXT NOT NULL,             -- session | profile | navigation | ticket | ai | admin | system | automation
    event_subtype TEXT NOT NULL,          -- login | logout | view_page | edit_field | availability_change | etc.
    target_kind TEXT,                     -- 'ticket' | 'view' | 'role' | 'group' | 'user' | NULL
    target_id TEXT,
    detail_json TEXT,                     -- arbitrary context (before/after, params, route, etc.)
    ip_address TEXT,
    user_agent TEXT,
    session_id TEXT,                      -- correlates events within a single session
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_user_activity_user_time ON user_activity_log(user_email, created_at);
CREATE INDEX IF NOT EXISTS idx_user_activity_type ON user_activity_log(event_type, event_subtype);
CREATE INDEX IF NOT EXISTS idx_user_activity_created ON user_activity_log(created_at);

-- ===== F6 · User-event automations =====
-- Mirrors automations but fires on user events. trigger_event names live
-- in src/user_automations_catalog.py (e.g. 'user.work_day_started',
-- 'user.idle_during_work', 'user.availability_changed').
CREATE TABLE IF NOT EXISTS user_automations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT,
    trigger_event TEXT NOT NULL,
    conditions_json TEXT NOT NULL DEFAULT '{}',  -- {match:'all'|'any', rules:[{field,op,value}]}
    actions_json TEXT NOT NULL DEFAULT '[]',     -- [{type, params:{...}}, ...]
    active INTEGER NOT NULL DEFAULT 1,
    position INTEGER NOT NULL DEFAULT 0,
    is_system_default INTEGER NOT NULL DEFAULT 0,
    category TEXT NOT NULL DEFAULT 'trigger',    -- 'trigger' | 'scheduler'
    schedule_json TEXT NOT NULL DEFAULT '{}',    -- for scheduler-kind only
    last_fired_at TEXT,
    fire_count INTEGER NOT NULL DEFAULT 0,
    created_by TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_user_automations_trigger ON user_automations(trigger_event, active);
CREATE INDEX IF NOT EXISTS idx_user_automations_position ON user_automations(position);

-- ===== F6 · In-app notifications =====
-- One row per nudge/alert the user should see when they're in the tool.
-- Bell icon polls /api/notifications/list with read_at IS NULL filter.
CREATE TABLE IF NOT EXISTS user_notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_email TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'info',     -- info | warning | error | prompt | success
    title TEXT NOT NULL,
    body TEXT,
    action_url TEXT,                       -- optional "View →" link
    action_label TEXT,
    source TEXT,                           -- 'automation:<id>' | 'system' | 'manual' | etc.
    read_at TEXT,
    dismissed_at TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (user_email) REFERENCES app_users(email) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_user_notifications_user_open ON user_notifications(user_email, dismissed_at);
CREATE INDEX IF NOT EXISTS idx_user_notifications_user_created ON user_notifications(user_email, created_at);

-- ===== F8 · User feedback =====
-- "Send feedback" button on every page lands a row here.
-- Status flow: new → triaged → closed (with optional reply).
CREATE TABLE IF NOT EXISTS user_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_email TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'bug',          -- 'bug' | 'idea' | 'question' | 'praise'
    severity TEXT,                              -- 'low' | 'normal' | 'high' | 'urgent'
    title TEXT,
    body TEXT NOT NULL,
    page_url TEXT,
    ticket_id INTEGER,                          -- if filed from a ticket page
    user_agent TEXT,
    status TEXT NOT NULL DEFAULT 'new',         -- 'new' | 'triaged' | 'closed'
    triaged_by TEXT,
    triaged_at TEXT,
    reply TEXT,
    replied_by TEXT,
    replied_at TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_user_feedback_status_created ON user_feedback(status, created_at);
CREATE INDEX IF NOT EXISTS idx_user_feedback_user ON user_feedback(user_email);

-- ===== F9 · Releases — version snapshots for rollback =====
-- Every `make release` writes a row here + a paired DB backup file
-- under data/backups/copilot-v<version>.db. `make rollback v=...` reads
-- this table to find the matching git tag + backup file.
CREATE TABLE IF NOT EXISTS releases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version TEXT UNIQUE NOT NULL,        -- semver e.g. "1.7.0"
    git_sha TEXT,                         -- short commit SHA at release time
    git_tag TEXT,                         -- tag name (usually "v" + version)
    notes TEXT,                            -- release notes (markdown)
    db_backup_path TEXT,                   -- absolute path to paired snapshot
    code_files_changed INTEGER,            -- # files changed since prev release
    is_current INTEGER NOT NULL DEFAULT 0, -- the version currently running
    created_at TEXT NOT NULL,
    created_by TEXT,
    rolled_back_at TEXT,                   -- when (if ever) this version was rolled back to
    rolled_back_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_releases_created ON releases(created_at);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """Idempotent schema migrations for existing databases.
    Adding columns is safe; we catch 'duplicate column' errors and continue."""
    statements = [
        "ALTER TABLE tickets ADD COLUMN source TEXT DEFAULT 'zendesk'",
        "ALTER TABLE tickets ADD COLUMN local_id TEXT",
        "ALTER TABLE tickets ADD COLUMN external_id TEXT",
        "ALTER TABLE tickets ADD COLUMN local_overrides TEXT DEFAULT '{}'",
        "ALTER TABLE tickets ADD COLUMN locally_created_at TEXT",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_tickets_local_id ON tickets(local_id) WHERE local_id IS NOT NULL",
        "CREATE INDEX IF NOT EXISTS idx_tickets_source ON tickets(source)",
        # Performance indexes (Phase-A perf pass). The NOT IN subquery on
        # ticket_comments + users was the dominant cost — ~16-19s per call.
        # These indexes drop sidebar load from ~50s to <1s.
        "CREATE INDEX IF NOT EXISTS idx_users_role ON users(role)",
        "CREATE INDEX IF NOT EXISTS idx_comments_author ON ticket_comments(author_id)",
        "CREATE INDEX IF NOT EXISTS idx_comments_ticket_author ON ticket_comments(ticket_id, author_id)",
        "CREATE INDEX IF NOT EXISTS idx_tickets_created ON tickets(created_at)",
        "CREATE INDEX IF NOT EXISTS idx_tickets_solved ON tickets(solved_at)",
        "CREATE INDEX IF NOT EXISTS idx_tickets_status_created ON tickets(status, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_tickets_last_analyzed ON tickets(last_analyzed_updated_at)",
        # JSON-extract expression indexes for the hot custom fields. Without these,
        # the similar-tickets matcher (and any /admin filter that compares
        # customer/RC1) does a full table scan of 56K rows. With them: <20ms.
        # SQLite supports CREATE INDEX on json_extract since 3.31+.
        "CREATE INDEX IF NOT EXISTS idx_tickets_cf_customer "
        "ON tickets(json_extract(custom_fields,'$.\"15315331275025\"'))",
        "CREATE INDEX IF NOT EXISTS idx_tickets_cf_rc1 "
        "ON tickets(json_extract(custom_fields,'$.\"15316740884753\"'))",
        "CREATE INDEX IF NOT EXISTS idx_tickets_cf_product "
        "ON tickets(json_extract(custom_fields,'$.\"15316390522769\"'))",
        "CREATE INDEX IF NOT EXISTS idx_tickets_cf_module "
        "ON tickets(json_extract(custom_fields,'$.\"15316445624849\"'))",
        "CREATE INDEX IF NOT EXISTS idx_tickets_cf_jira "
        "ON tickets(json_extract(custom_fields,'$.\"15316921871633\"'))",
        # Helps the "missing KB" view + Tier-3 fallback ordering
        "CREATE INDEX IF NOT EXISTS idx_tickets_cf_kb "
        "ON tickets(json_extract(custom_fields,'$.\"15317743732625\"'))",
        # Richer AI insight columns. The original `summary` column stays for
        # backcompat — older insights still render via the legacy renderer.
        # New columns populate via the rewritten worker prompt.
        "ALTER TABLE ticket_insights ADD COLUMN issue_summary TEXT",
        "ALTER TABLE ticket_insights ADD COLUMN historical_context TEXT",
        "ALTER TABLE ticket_insights ADD COLUMN current_state TEXT",
        "ALTER TABLE ticket_insights ADD COLUMN recommended_action TEXT",
        "ALTER TABLE ticket_insights ADD COLUMN similar_with_reasoning TEXT",
        # Reply-box block: store the rendering format ('plain' | 'markdown' | 'html')
        # and a free-form metadata blob for things like the translate-source language.
        "ALTER TABLE ticket_comments ADD COLUMN body_format TEXT DEFAULT 'plain'",
        "ALTER TABLE ticket_comments ADD COLUMN meta TEXT DEFAULT '{}'",
        "CREATE INDEX IF NOT EXISTS idx_comments_format ON ticket_comments(body_format)",
        # Local-only override for the "required" flag. ZD's required + required_in_portal
        # is what we sync; this column lets the admin mark a field required
        # without depending on what the ZD API exposes. NULL = fall through.
        "ALTER TABLE ticket_fields ADD COLUMN required_override INTEGER DEFAULT NULL",
        # Per-rule mandatory toggle on conditional visibility — when this rule
        # fires (target field becomes visible), should the target also become
        # required? NULL = no override; 1 = force required; 0 = force optional.
        "ALTER TABLE native_form_conditions ADD COLUMN target_required INTEGER DEFAULT NULL",
        # Automations rework: classify each rule as a Trigger (event-driven) or
        # Scheduler (time-driven). schedule_json carries scheduler-specific config
        # like {kind:'interval', minutes:60} or {kind:'cron', expr:'0 9 * * 1-5'}.
        "ALTER TABLE automations ADD COLUMN category TEXT DEFAULT 'trigger'",
        "ALTER TABLE automations ADD COLUMN schedule_json TEXT DEFAULT '{}'",
        # ===== F0+ access control extensions =====
        # zd_user_id maps an app_user (sign-in identity) to the Zendesk users row
        # so "Assigned to me" / assignment writes know which ZD user to write.
        # NULL = unmapped (auto-fill on next login via email match).
        "ALTER TABLE app_users ADD COLUMN zd_user_id INTEGER",
        # F0+ perf: assignee_id was unindexed — "Assigned to me" was a full scan
        # on 50K+ tickets (~200-400ms). With this index it drops to <5ms.
        "CREATE INDEX IF NOT EXISTS idx_tickets_assignee_status ON tickets(assignee_id, status)",
        "CREATE INDEX IF NOT EXISTS idx_tickets_group_status ON tickets(group_id, status)",
        # Per-user view ordering. JSON list of view_ids in display order.
        # Empty list = use default_position from native_views.
        "ALTER TABLE app_users ADD COLUMN view_order_json TEXT DEFAULT '[]'",
        "ALTER TABLE app_users ADD COLUMN hidden_views_json TEXT DEFAULT '[]'",
        "CREATE INDEX IF NOT EXISTS idx_app_users_zd ON app_users(zd_user_id)",
        # F5 · Profile fields. Used by the avatar dropdown + /profile page.
        # NOTE: column is `availability` not `status` — the existing `status`
        # column from F0 means active/disabled account state. Availability is
        # the online/away/busy/offline pill the user sees.
        # Custom availability: free text + emoji; the dropdown still forces
        # the user to pick which availability bucket it maps to, so any
        # availability-aware rule has a clean signal.
        "ALTER TABLE app_users ADD COLUMN title TEXT",
        "ALTER TABLE app_users ADD COLUMN timezone TEXT DEFAULT 'Asia/Kolkata'",
        "ALTER TABLE app_users ADD COLUMN work_days_json TEXT DEFAULT '[\"Mon\",\"Tue\",\"Wed\",\"Thu\",\"Fri\"]'",
        "ALTER TABLE app_users ADD COLUMN work_start_time TEXT DEFAULT '09:00'",
        "ALTER TABLE app_users ADD COLUMN work_end_time TEXT DEFAULT '18:00'",
        "ALTER TABLE app_users ADD COLUMN availability TEXT NOT NULL DEFAULT 'offline'",
        "ALTER TABLE app_users ADD COLUMN availability_emoji TEXT",
        "ALTER TABLE app_users ADD COLUMN availability_label TEXT",
        "ALTER TABLE app_users ADD COLUMN availability_until TEXT",
        "ALTER TABLE app_users ADD COLUMN notify_email INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE app_users ADD COLUMN notify_browser INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE app_users ADD COLUMN notify_sound INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE app_users ADD COLUMN phone TEXT",
        "ALTER TABLE app_users ADD COLUMN slack_handle TEXT",
        "ALTER TABLE app_users ADD COLUMN bio TEXT",
        # F6 · Leave mode + auto-online tracking
        # on_leave: 1 = skip auto-online and idle warnings until cleared
        # last_online_at: last time we observed availability='online'
        #   (used by idle-during-work logic)
        # last_idle_warning_at: prevents spamming the user — gated to one
        #   nudge per warn_interval (default 30min).
        "ALTER TABLE app_users ADD COLUMN on_leave INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE app_users ADD COLUMN leave_start TEXT",
        "ALTER TABLE app_users ADD COLUMN leave_end TEXT",
        "ALTER TABLE app_users ADD COLUMN leave_reason TEXT",
        "ALTER TABLE app_users ADD COLUMN last_online_at TEXT",
        "ALTER TABLE app_users ADD COLUMN last_idle_warning_at TEXT",
        "ALTER TABLE app_users ADD COLUMN last_work_day_marked TEXT",
        # F6+ · Per-rule scheduling. Each scheduler-type rule has its own
        # cadence (interval_minutes), next-fire ETA, and last error so the
        # admin can see exactly when it'll fire next + diagnose failures.
        # No more central scheduler subprocess — the rule itself is the
        # unit of control.
        "ALTER TABLE user_automations ADD COLUMN interval_minutes INTEGER NOT NULL DEFAULT 5",
        "ALTER TABLE user_automations ADD COLUMN next_fire_at TEXT",
        "ALTER TABLE user_automations ADD COLUMN last_error TEXT",
        "ALTER TABLE user_automations ADD COLUMN last_error_at TEXT",
        # Groups: distinguish ZD-synced vs native, plus active flag + description
        # so we can show them in /admin/groups with a proper UI.
        "ALTER TABLE groups ADD COLUMN description TEXT",
        "ALTER TABLE groups ADD COLUMN is_native INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE groups ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE groups ADD COLUMN created_at TEXT",
        "ALTER TABLE groups ADD COLUMN updated_at TEXT",
        "ALTER TABLE groups ADD COLUMN created_by TEXT",
    ]
    for sql in statements:
        try:
            conn.execute(sql)
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            if "duplicate column" in msg or "already exists" in msg:
                continue
            raise
    # Backfill: existing rows are zendesk-sourced
    conn.execute("UPDATE tickets SET source='zendesk' WHERE source IS NULL")
    conn.execute("UPDATE tickets SET external_id=CAST(id AS TEXT) WHERE source='zendesk' AND external_id IS NULL")
    conn.execute("UPDATE tickets SET local_overrides='{}' WHERE local_overrides IS NULL")
    # Backfill automations.category from the legacy trigger_type.
    # NOTE: the ALTER TABLE adds the column with a default of 'trigger', so every
    # row is non-NULL the first time this runs — we must re-classify based on
    # trigger_type explicitly. Idempotent: WHERE filters mean repeated runs
    # only touch rows still showing the wrong category.
    try:
        conn.execute("""
            UPDATE automations SET category = 'scheduler'
            WHERE trigger_type = 'time_elapsed' AND COALESCE(category,'trigger') != 'scheduler'
        """)
        conn.execute("""
            UPDATE automations SET category = 'trigger'
            WHERE (trigger_type IS NULL OR trigger_type != 'time_elapsed')
              AND COALESCE(category,'') NOT IN ('trigger','scheduler')
        """)
        conn.execute("UPDATE automations SET schedule_json='{}' WHERE schedule_json IS NULL")
    except sqlite3.OperationalError:
        pass


def backup(target_dir: Path | None = None, keep_last_n: int = 7) -> Path:
    """Online backup using SQLite's Backup API — does NOT lock writers,
    safe to call while the app is running. Writes to a `.tmp` file first
    then atomically renames, so a half-failed copy never appears in the
    list as a valid backup.

    Prunes only the auto-stamped `copilot-YYYYMMDD-HHMMSS.db` files —
    `copilot-v1.x.y.db` files from `make release` are explicit historical
    pins and are never auto-deleted by this function.

    Triggered nightly by the F13 backup_scheduler thread (or call manually
    from /admin/backups when you want a snapshot on demand). On failure,
    raises so the caller (backup_scheduler.run_backup_now) can stamp the
    error into meta and surface it to the admin UI."""
    from datetime import datetime as _dt
    target_dir = target_dir or (DATA_DIR / "backups")
    target_dir.mkdir(exist_ok=True)
    stamp = _dt.now().strftime("%Y%m%d-%H%M%S")
    target = target_dir / f"copilot-{stamp}.db"
    tmp = target_dir / f"copilot-{stamp}.db.tmp"
    # Sweep any leftover .tmp from a previous crashed run
    if tmp.exists():
        try: tmp.unlink()
        except OSError: pass
    src_size = DB_PATH.stat().st_size if DB_PATH.exists() else 0
    src = sqlite3.connect(str(DB_PATH))
    try:
        dst = sqlite3.connect(str(tmp))
        try:
            # pages=2000 → checkpoint every 2000 pages so we yield to writers
            src.backup(dst, pages=2000, sleep=0)
        except Exception as e:
            print(f"[db.backup] copy failed for {tmp}: {type(e).__name__}: {e}")
            try: dst.close()
            except Exception: pass
            try: tmp.unlink()
            except OSError: pass
            raise
        finally:
            try: dst.close()
            except Exception: pass
    finally:
        src.close()
    # Atomic move — only now does the file enter the "valid backups" list
    try:
        tmp.rename(target)
    except OSError as e:
        print(f"[db.backup] rename {tmp} -> {target} failed: {e}")
        raise RuntimeError(f"backup written but rename failed: {e}") from e
    # Sanity check on size — catches the rare empty-file failure mode
    if target.stat().st_size < min(4096, src_size or 4096):
        print(f"[db.backup] suspicious size: target {target.stat().st_size}B, src {src_size}B")
    # Prune — keep last N of the auto-named files only
    import re as _re
    auto_re = _re.compile(r"^copilot-\d{8}-\d{4,6}\.db$")
    autos = sorted(
        [p for p in target_dir.glob("copilot-*.db") if auto_re.match(p.name)],
        reverse=True,
    )
    for old in autos[keep_last_n:]:
        try: old.unlink()
        except OSError as e: print(f"[db.backup] prune {old}: {e}")
    return target


def checkpoint_wal(conn: sqlite3.Connection | None = None) -> None:
    """Flush the WAL into the main DB file. Call on app shutdown so the
    next startup has no pending uncommitted writes — the failure mode that
    caused the corruption incident."""
    own_conn = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        own_conn = True
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception as e:
        print(f"[db.checkpoint_wal] {e}")
    finally:
        if own_conn:
            conn.close()


def _heal_invalid_json_rows(conn: sqlite3.Connection) -> dict:
    """Belt-and-braces data repair. Any row with invalid JSON in a column
    we query via json_extract() will raise 'malformed JSON' from SQLite —
    killing the whole query (and any page that depends on it).

    We sweep the hot columns on every boot and reset broken rows to empty.
    Cheap: indexed json_valid() checks complete in <50ms even on 50K rows.
    """
    result: dict = {}
    # tickets.custom_fields → '{}', tags / local_overrides → '[]'/'{}', raw → '{}'
    targets = [
        ("tickets", "custom_fields", "{}"),
        ("tickets", "tags", "[]"),
        ("tickets", "local_overrides", "{}"),
        ("tickets", "raw", "{}"),
    ]
    for table, col, default in targets:
        try:
            cur = conn.execute(
                f"UPDATE {table} SET {col}=? WHERE json_valid({col})=0",
                (default,)
            )
            if cur.rowcount:
                print(f"[db.init] healed {cur.rowcount} {table}.{col} rows "
                      f"with invalid JSON (reset to {default})")
                result[f"{table}.{col}"] = cur.rowcount
        except sqlite3.OperationalError as e:
            # Column might not exist on older schemas — ignore
            if "no such column" not in str(e):
                print(f"[db.init] _heal_invalid_json_rows {table}.{col}: {e}")
    return result


def _heal_orphaned_indexes(conn: sqlite3.Connection) -> int:
    """Drop any index in sqlite_master that references a table that no longer
    exists. This corruption mode ("malformed database schema (idx_X) — no such
    table: main.Y") happens when a migration is interrupted between dropping a
    table and dropping its indexes, or when DB files get edited externally.

    The fix needs no writable_schema gymnastics — DROP INDEX works fine on an
    orphan because SQLite only touches the index page, not the (missing) table.
    Returns the number of orphans cleaned up."""
    try:
        rows = conn.execute("""
            SELECT name, tbl_name FROM sqlite_master
            WHERE type='index'
              AND name NOT LIKE 'sqlite_autoindex_%'
              AND tbl_name NOT IN (SELECT name FROM sqlite_master WHERE type='table')
        """).fetchall()
    except sqlite3.DatabaseError:
        # The corruption is severe enough that even reading sqlite_master fails.
        # Fall through to the writable_schema escape hatch.
        rows = []
        try:
            conn.execute("PRAGMA writable_schema=1")
            conn.execute("""
                DELETE FROM sqlite_master
                WHERE type='index'
                  AND name NOT LIKE 'sqlite_autoindex_%'
                  AND tbl_name NOT IN (SELECT name FROM sqlite_master WHERE type='table')
            """)
            conn.execute("PRAGMA writable_schema=0")
            conn.commit()
        except Exception:
            pass
        return 0
    cleaned = 0
    for idx_name, tbl_name in rows:
        try:
            conn.execute(f"DROP INDEX IF EXISTS {idx_name}")
            cleaned += 1
            print(f"  [db.init] healed orphan index: {idx_name} (table {tbl_name} missing)")
        except sqlite3.OperationalError as e:
            # Last resort — surgical delete via writable_schema
            try:
                conn.execute("PRAGMA writable_schema=1")
                conn.execute("DELETE FROM sqlite_master WHERE name=? AND type='index'", (idx_name,))
                conn.execute("PRAGMA writable_schema=0")
                cleaned += 1
                print(f"  [db.init] writable_schema-deleted orphan: {idx_name}")
            except Exception as e2:
                print(f"  [db.init] could not drop {idx_name}: {e} / {e2}")
    if cleaned:
        conn.commit()
    return cleaned


def init() -> None:
    """Create the DB file and schema if they don't exist; run migrations.
    Also auto-heals orphaned indexes from any previous interrupted migration."""
    DB_PATH.parent.mkdir(exist_ok=True)
    # Heal pass — runs BEFORE we touch PRAGMA journal_mode (which validates the
    # full schema and would otherwise blow up on the orphan).
    try:
        repair_conn = sqlite3.connect(DB_PATH, timeout=30)
        try:
            _heal_orphaned_indexes(repair_conn)
        finally:
            repair_conn.close()
    except Exception as e:
        print(f"[db.init] heal pass error (continuing): {e}")
    # Now the schema + migration pass.
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA)
        _migrate(conn)
        # Heal any row that has invalid JSON in a hot column — a single bad
        # row used to crash the sidebar (and therefore every page).
        try:
            _heal_invalid_json_rows(conn)
        except Exception as e:
            print(f"[db.init] json heal pass error (continuing): {e}")
        # Bootstrap roles + permissions + owner (idempotent — safe on every boot).
        # Runs inside init() so a brand-new DB is immediately usable; the
        # owner_email default is overridable for tests.
        try:
            seed_access_control(conn)
        except Exception as e:
            # Don't kill the app if seeding hiccups (e.g. permissions module not
            # importable yet during partial install). Surface it loudly though.
            print(f"[db.init] seed_access_control error (continuing): {e}")
        try:
            seed_default_views(conn)
        except Exception as e:
            print(f"[db.init] seed_default_views error (continuing): {e}")
        try:
            seed_default_user_automations(conn)
        except Exception as e:
            print(f"[db.init] seed_default_user_automations error (continuing): {e}")
        conn.commit()


@contextmanager
def conn() -> Iterator[sqlite3.Connection]:
    """Context manager yielding a sqlite3.Connection with row factory.

    PRAGMA setup for concurrent-writer resilience:
      - journal_mode=WAL     — multiple readers can run while one writer commits
      - busy_timeout=60000   — wait up to 60s for the writer lock before raising
                                "database is locked". Important now that we have
                                uvicorn + user_scheduler thread + AI worker +
                                sync_worker all writing to the same DB.
      - synchronous=NORMAL   — fsync at WAL checkpoints, not every write (3-5x
                                faster, safe with WAL)
      - foreign_keys=ON      — enforce FK constraints

    Self-heals on "malformed database schema" by running _heal_orphaned_indexes
    once and retrying.
    """
    c = sqlite3.connect(DB_PATH, timeout=60, isolation_level=None)
    c.row_factory = sqlite3.Row
    # busy_timeout is in milliseconds — applied at SQLite level so even queries
    # outside our timeout window (e.g. nested transactions) wait properly.
    try:
        c.execute("PRAGMA busy_timeout=60000")
    except sqlite3.DatabaseError:
        pass
    try:
        c.execute("PRAGMA journal_mode=WAL")
    except sqlite3.DatabaseError as e:
        if "malformed database schema" in str(e):
            print(f"[db.conn] {e} — auto-healing")
            try:
                _heal_orphaned_indexes(c)
                c.execute("PRAGMA journal_mode=WAL")
            except Exception as e2:
                c.close()
                raise sqlite3.DatabaseError(
                    f"DB schema malformed and auto-heal failed: {e2}. "
                    f"Run `sqlite3 data/copilot.db 'VACUUM'` from the terminal."
                ) from e
        else:
            c.close()
            raise
    c.execute("PRAGMA synchronous=NORMAL")
    c.execute("PRAGMA foreign_keys=ON")
    try:
        yield c
    finally:
        c.close()


def retry_on_lock(fn, *, attempts: int = 3, base_delay: float = 0.5):
    """Run fn(), retrying on 'database is locked' / 'database is busy' up to
    `attempts` times with exponential backoff (0.5s, 1s, 2s by default).
    Use for non-critical writes that can wait — e.g. activity logging, idle
    notifications. Don't use for user-facing reads (the caller wants a fast
    fail to surface a meaningful error)."""
    import time
    last_err = None
    for i in range(int(attempts)):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            if "locked" in msg or "busy" in msg:
                last_err = e
                time.sleep(base_delay * (2 ** i))
                continue
            raise
    if last_err:
        raise last_err


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---- helpers -----------------------------------------------------------------
def upsert_ticket(c: sqlite3.Connection, t: dict) -> None:
    """Upsert a ZD-synced ticket. Sets source='zendesk' on insert; on update,
    preserves source, local_id, external_id, and local_overrides (sync never
    touches agent-applied local edits)."""
    c.execute("""
        INSERT INTO tickets (id, subject, status, priority, type, requester_id,
            organization_id, assignee_id, group_id, tags, custom_fields,
            created_at, updated_at, solved_at, raw,
            source, external_id, local_overrides)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'zendesk', ?, '{}')
        ON CONFLICT(id) DO UPDATE SET
            subject=excluded.subject, status=excluded.status,
            priority=excluded.priority, type=excluded.type,
            requester_id=excluded.requester_id, organization_id=excluded.organization_id,
            assignee_id=excluded.assignee_id, group_id=excluded.group_id,
            tags=excluded.tags, custom_fields=excluded.custom_fields,
            created_at=excluded.created_at, updated_at=excluded.updated_at,
            solved_at=excluded.solved_at, raw=excluded.raw
            -- intentionally NOT updating: source, local_id, external_id, local_overrides
    """, (
        t["id"], t.get("subject"), t.get("status"), t.get("priority"), t.get("type"),
        t.get("requester_id"), t.get("organization_id"), t.get("assignee_id"), t.get("group_id"),
        json.dumps(t.get("tags") or []),
        json.dumps({str(cf["id"]): cf.get("value") for cf in (t.get("custom_fields") or [])}),
        t.get("created_at"), t.get("updated_at"), t.get("solved_at"),
        json.dumps(t),
        str(t["id"]),  # external_id mirrors id for ZD-sourced rows
    ))


# =============================================================================
# Block #1: native ticket helpers
# =============================================================================

def effective_custom_fields(t_row: sqlite3.Row | dict) -> dict:
    """Merge tickets.custom_fields (ZD-synced) with local_overrides.custom_fields (agent edits).
    Local wins. Returns {field_id_str: value}. Use this anywhere fields are rendered."""
    if isinstance(t_row, dict):
        base_str = t_row.get("custom_fields") or "{}"
        ov_str = t_row.get("local_overrides") or "{}"
    else:
        base_str = t_row["custom_fields"] if t_row["custom_fields"] is not None else "{}"
        try:
            ov_str = t_row["local_overrides"]
        except (IndexError, KeyError):
            ov_str = "{}"
        ov_str = ov_str or "{}"
    try:
        base = json.loads(base_str or "{}")
    except Exception:
        base = {}
    try:
        ov = json.loads(ov_str or "{}")
    except Exception:
        ov = {}
    local_cfs = (ov or {}).get("custom_fields") or {}
    if local_cfs:
        merged = dict(base)
        merged.update(local_cfs)
        return merged
    return base


def effective_status(t_row: sqlite3.Row | dict) -> str:
    if isinstance(t_row, dict):
        ov_str = t_row.get("local_overrides") or "{}"
        ground = t_row.get("status")
    else:
        try:
            ov_str = t_row["local_overrides"] or "{}"
        except (IndexError, KeyError):
            ov_str = "{}"
        ground = t_row["status"]
    try:
        ov = json.loads(ov_str)
    except Exception:
        ov = {}
    return ov.get("status") or ground


def set_local_field_override(c: sqlite3.Connection, ticket_id: int, field_id: int | str, value) -> dict:
    """Apply a single field override to local_overrides JSON. Returns merged custom_fields."""
    row = c.execute("SELECT custom_fields, local_overrides FROM tickets WHERE id=?", (ticket_id,)).fetchone()
    if not row:
        raise ValueError(f"ticket {ticket_id} not found")
    try:
        ov = json.loads(row["local_overrides"] or "{}")
    except Exception:
        ov = {}
    cfs = ov.get("custom_fields") or {}
    cfs[str(field_id)] = value
    ov["custom_fields"] = cfs
    c.execute("UPDATE tickets SET local_overrides=? WHERE id=?", (json.dumps(ov), ticket_id))
    return effective_custom_fields(row) | {str(field_id): value}


def next_native_seq(c: sqlite3.Connection) -> int:
    """Get + increment the native ticket sequence. Returns the new value to assign."""
    cur = get_meta(c, "next_native_seq") or "1"
    n = int(cur)
    set_meta(c, "next_native_seq", str(n + 1))
    return n


def make_local_id(seq: int) -> str:
    return f"BP-{seq:06d}"


def make_native_int_id(seq: int) -> int:
    """Native ticket integer ids start at 1_000_000_000 to avoid colliding with
    Zendesk ticket ids (which top out around 10 million)."""
    return 1_000_000_000 + seq


def insert_native_ticket(c: sqlite3.Connection, *, subject: str, requester_id: int | None,
                        organization_id: int | None, group_id: int | None,
                        priority: str = "normal", custom_fields: dict | None = None,
                        creator_email: str = "") -> dict:
    """Create a native ticket. Generates id (1B+ range) and local_id (BP-NNNNNN)."""
    seq = next_native_seq(c)
    int_id = make_native_int_id(seq)
    local_id = make_local_id(seq)
    now = now_iso()
    raw_payload = {
        "id": int_id,
        "local_id": local_id,
        "subject": subject,
        "status": "new",
        "priority": priority,
        "requester_id": requester_id,
        "organization_id": organization_id,
        "group_id": group_id,
        "created_via": "native_create",
        "created_by": creator_email,
        "created_at": now,
    }
    c.execute("""
        INSERT INTO tickets (id, subject, status, priority, type, requester_id,
            organization_id, assignee_id, group_id, tags, custom_fields,
            created_at, updated_at, solved_at, raw,
            source, local_id, external_id, local_overrides, locally_created_at)
        VALUES (?, ?, 'new', ?, NULL, ?, ?, NULL, ?, '[]', ?, ?, ?, NULL, ?,
                'native', ?, NULL, '{}', ?)
    """, (
        int_id, subject, priority,
        requester_id, organization_id, group_id,
        json.dumps(custom_fields or {}),
        now, now,
        json.dumps(raw_payload),
        local_id, now,
    ))
    return {"id": int_id, "local_id": local_id, "seq": seq}


def insert_native_comment(c: sqlite3.Connection, *, ticket_id: int, author_id: int | None,
                         body: str, public: bool = True, body_format: str = "plain",
                         meta: dict | None = None) -> int:
    """Insert a comment on a native ticket. Uses negative ids in our local namespace
    so we never collide with ZD comment ids. body_format is 'plain' | 'markdown' |
    'html'; the agent UI uses 'markdown' for both public replies and internal notes
    (the toolbar produces Markdown). meta captures things like the translate-source
    language so we can show the original alongside the translation later."""
    row = c.execute("SELECT MIN(id) AS lo FROM ticket_comments").fetchone()
    lo = row["lo"] if row and row["lo"] is not None else 0
    new_id = -1 if lo >= 0 else lo - 1
    # Markdown bodies should NOT be stripped of "<" "/>" because they're valid
    # Markdown punctuation; only run clean_body when format is 'plain' or 'html'.
    stored = body if body_format == "markdown" else clean_body(body)
    c.execute("""
        INSERT INTO ticket_comments (id, ticket_id, author_id, public, body, html_body, created_at, body_format, meta)
        VALUES (?, ?, ?, ?, ?, '', ?, ?, ?)
    """, (new_id, ticket_id, author_id, 1 if public else 0,
          stored, now_iso(), body_format, json.dumps(meta or {})))
    return new_id


def insert_native_attachment(c: sqlite3.Connection, *, ticket_id: int, comment_id: int | None,
                              file_name: str, content_type: str, size_bytes: int,
                              local_path: str) -> int:
    """Register an attachment uploaded by an agent via our UI. Uses negative ids
    to avoid colliding with Zendesk's. local_path is relative to data/."""
    row = c.execute("SELECT MIN(id) AS lo FROM ticket_attachments").fetchone()
    lo = row["lo"] if row and row["lo"] is not None else 0
    new_id = -1 if lo is None or lo >= 0 else lo - 1
    if new_id is None or new_id >= 0:
        new_id = -1
    c.execute("""
        INSERT INTO ticket_attachments (id, ticket_id, comment_id, file_name, content_type,
            size_bytes, content_url, local_path, downloaded_at, source, raw, created_at)
        VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, 'native', ?, ?)
    """, (new_id, ticket_id, comment_id, file_name, content_type,
          size_bytes, local_path, now_iso(),
          json.dumps({"source": "native_upload"}), now_iso()))
    return new_id


def upsert_comment(c: sqlite3.Connection, ticket_id: int, cm: dict) -> None:
    body_raw = cm.get("plain_body") or cm.get("body") or ""
    body = clean_body(body_raw)
    c.execute("""
        INSERT INTO ticket_comments (id, ticket_id, author_id, public, body, html_body, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            body=excluded.body, html_body=excluded.html_body, public=excluded.public
    """, (
        cm["id"], ticket_id, cm.get("author_id"),
        1 if cm.get("public") else 0,
        body,
        cm.get("html_body") or "",
        cm.get("created_at"),
    ))


def upsert_form(c: sqlite3.Connection, f: dict) -> None:
    c.execute("""
        INSERT INTO ticket_forms (id, name, display_name, active, field_ids, raw)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            name=excluded.name, display_name=excluded.display_name,
            active=excluded.active, field_ids=excluded.field_ids, raw=excluded.raw
    """, (
        f["id"], f.get("name"), f.get("display_name"),
        1 if f.get("active") else 0,
        json.dumps(f.get("ticket_field_ids") or []),
        json.dumps(f),
    ))


def upsert_metrics(c: sqlite3.Connection, ticket_id: int, m: dict) -> None:
    c.execute("""
        INSERT INTO ticket_metrics (ticket_id, reply_time_in_minutes, first_resolution_time_in_minutes,
            full_resolution_time_in_minutes, agent_wait_time_in_minutes, requester_wait_time_in_minutes,
            on_hold_time_in_minutes, latest_comment_added_at, initially_assigned_at, assignee_updated_at,
            requester_updated_at, raw)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticket_id) DO UPDATE SET
            reply_time_in_minutes=excluded.reply_time_in_minutes,
            first_resolution_time_in_minutes=excluded.first_resolution_time_in_minutes,
            full_resolution_time_in_minutes=excluded.full_resolution_time_in_minutes,
            agent_wait_time_in_minutes=excluded.agent_wait_time_in_minutes,
            requester_wait_time_in_minutes=excluded.requester_wait_time_in_minutes,
            on_hold_time_in_minutes=excluded.on_hold_time_in_minutes,
            latest_comment_added_at=excluded.latest_comment_added_at,
            initially_assigned_at=excluded.initially_assigned_at,
            assignee_updated_at=excluded.assignee_updated_at,
            requester_updated_at=excluded.requester_updated_at,
            raw=excluded.raw
    """, (
        ticket_id,
        _bm(m, "reply_time_in_minutes"),
        _bm(m, "first_resolution_time_in_minutes"),
        _bm(m, "full_resolution_time_in_minutes"),
        m.get("agent_wait_time_in_minutes", {}).get("calendar") if isinstance(m.get("agent_wait_time_in_minutes"), dict) else m.get("agent_wait_time_in_minutes"),
        m.get("requester_wait_time_in_minutes", {}).get("calendar") if isinstance(m.get("requester_wait_time_in_minutes"), dict) else m.get("requester_wait_time_in_minutes"),
        m.get("on_hold_time_in_minutes", {}).get("calendar") if isinstance(m.get("on_hold_time_in_minutes"), dict) else m.get("on_hold_time_in_minutes"),
        m.get("latest_comment_added_at"), m.get("initially_assigned_at"),
        m.get("assignee_updated_at"), m.get("requester_updated_at"),
        json.dumps(m),
    ))


def _bm(m: dict, key: str) -> int | None:
    """Some Zendesk metric fields return {calendar, business} dicts; pick calendar minutes."""
    v = m.get(key)
    if isinstance(v, dict):
        return v.get("calendar")
    return v


def upsert_user(c: sqlite3.Connection, u: dict) -> None:
    c.execute("""
        INSERT INTO users (id, name, email, role, raw) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET name=excluded.name, email=excluded.email, role=excluded.role, raw=excluded.raw
    """, (u["id"], u.get("name"), u.get("email"), u.get("role"), json.dumps(u)))


def upsert_org(c: sqlite3.Connection, o: dict) -> None:
    c.execute("""
        INSERT INTO organizations (id, name, raw) VALUES (?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET name=excluded.name, raw=excluded.raw
    """, (o["id"], o.get("name"), json.dumps(o)))


def upsert_group(c: sqlite3.Connection, g: dict) -> None:
    c.execute("""
        INSERT INTO groups (id, name) VALUES (?, ?)
        ON CONFLICT(id) DO UPDATE SET name=excluded.name
    """, (g["id"], g.get("name")))


def upsert_field_def(c: sqlite3.Connection, f: dict) -> None:
    options = [{"name": o.get("name"), "value": o.get("value")} for o in (f.get("custom_field_options") or [])]
    # Zendesk exposes TWO required flags: `required` (agent-facing) and
    # `required_in_portal` (end-user submission form). Many BetterPlace fields
    # are marked required for AGENTS only (e.g. Customer Name, Jira ID, KB
    # Article, RC1/RC2). The agent UI needs to enforce both, so OR them.
    required_flag = 1 if (f.get("required") or f.get("required_in_portal")) else 0
    c.execute("""
        INSERT INTO ticket_fields (id, title, type, required, options) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET title=excluded.title, type=excluded.type,
            required=excluded.required, options=excluded.options
    """, (f["id"], f.get("title"), f.get("type"), required_flag, json.dumps(options)))


def get_meta(c: sqlite3.Connection, key: str) -> str | None:
    row = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(c: sqlite3.Connection, key: str, value: str) -> None:
    c.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def log_spend(c: sqlite3.Connection, *, ticket_id: int | None, cost: float,
              in_tok: int, out_tok: int, cached_tok: int, model: str) -> None:
    c.execute("""
        INSERT INTO spend_log (ticket_id, cost_usd, input_tokens, output_tokens, cached_input_tokens, model, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (ticket_id, cost, in_tok, out_tok, cached_tok, model, now_iso()))


def month_to_date_spend(c: sqlite3.Connection) -> float:
    """Sum spend_log for this month (UTC)."""
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
    row = c.execute("SELECT COALESCE(SUM(cost_usd), 0) AS s FROM spend_log WHERE created_at >= ?", (month_start,)).fetchone()
    return float(row["s"])


def upsert_custom_status(c: sqlite3.Connection, s: dict) -> None:
    c.execute("""
        INSERT INTO custom_statuses (id, status_category, agent_label, end_user_label, description, active, raw)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            status_category=excluded.status_category, agent_label=excluded.agent_label,
            end_user_label=excluded.end_user_label, description=excluded.description,
            active=excluded.active, raw=excluded.raw
    """, (
        s["id"], s.get("status_category"),
        s.get("agent_label") or s.get("name"),
        s.get("end_user_label"), s.get("description"),
        1 if s.get("active") else 0,
        json.dumps(s),
    ))


def record_feedback(c: sqlite3.Connection, *, ticket_id: int, insight_id: int | None,
                    field_name: str, ai_current: str | None, ai_suggested: str,
                    confidence: float | None, decision: str, final_value: str | None,
                    rejection_reason: str | None, actor: str) -> int:
    cur = c.execute("""
        INSERT INTO ai_feedback (ticket_id, insight_id, field_name, ai_current_value,
            ai_suggested_value, ai_confidence, decision, final_value, rejection_reason,
            actor_email, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        ticket_id, insight_id, field_name, ai_current, ai_suggested,
        confidence, decision, final_value, rejection_reason, actor, now_iso(),
    ))
    return cur.lastrowid


def feedback_summary(c: sqlite3.Connection, *, days: int = 30) -> dict:
    """Aggregate counts for the dashboard / repository telemetry."""
    rows = c.execute(f"""
        SELECT decision, COUNT(*) AS n
        FROM ai_feedback
        WHERE created_at > datetime('now','-{int(days)} days')
        GROUP BY decision
    """).fetchall()
    return {r["decision"]: r["n"] for r in rows}


def audit(c: sqlite3.Connection, *, actor: str, action: str, target_type: str = "", target_id: str = "", detail: str = "") -> None:
    c.execute("""
        INSERT INTO audit_log (actor_email, action, target_type, target_id, detail, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (actor, action, target_type, target_id, detail, now_iso()))


# =============================================================================
# Ticket audit log (per-ticket event history)
# =============================================================================
def audit_ticket(c: sqlite3.Connection, *, ticket_id: int, event_type: str,
                 event_summary: str = "", actor_email: str = "system",
                 actor_type: str = "system", field_key: str = "",
                 before=None, after=None, raw: dict | None = None,
                 source: str = "native") -> int:
    """Insert a row into ticket_audit_log. before/after can be any JSON-able
    value (string, number, list, dict) — we serialize them here."""
    cur = c.execute("""
        INSERT INTO ticket_audit_log (ticket_id, event_type, event_summary,
            actor_email, actor_type, field_key, before_json, after_json,
            raw, source, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        ticket_id, event_type, (event_summary or "")[:500],
        actor_email or "system", actor_type or "system", field_key,
        json.dumps(before) if before is not None else None,
        json.dumps(after) if after is not None else None,
        json.dumps(raw) if raw else None,
        source, now_iso(),
    ))
    return cur.lastrowid


def list_ticket_audit(c: sqlite3.Connection, ticket_id: int, limit: int = 500) -> list[dict]:
    rows = c.execute("""
        SELECT id, event_type, event_summary, actor_email, actor_type,
               field_key, before_json, after_json, source, created_at
        FROM ticket_audit_log WHERE ticket_id=? ORDER BY id DESC LIMIT ?
    """, (ticket_id, limit)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for k in ("before_json", "after_json"):
            if d.get(k):
                try: d[k.replace("_json", "")] = json.loads(d[k])
                except Exception: d[k.replace("_json", "")] = d[k]
            else:
                d[k.replace("_json", "")] = None
        out.append(d)
    return out


# =============================================================================
# Attachments
# =============================================================================
def upsert_attachment(c: sqlite3.Connection, ticket_id: int, comment_id: int | None, a: dict) -> None:
    """Insert or refresh attachment metadata. content_url is updated every sync
    because Zendesk signs it with a short-lived token; never trust the old value."""
    c.execute("""
        INSERT INTO ticket_attachments (id, ticket_id, comment_id, file_name,
            content_type, size_bytes, content_url, local_path, downloaded_at,
            source, raw, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, 'zendesk', ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            ticket_id=excluded.ticket_id, comment_id=excluded.comment_id,
            file_name=excluded.file_name, content_type=excluded.content_type,
            size_bytes=excluded.size_bytes, content_url=excluded.content_url,
            raw=excluded.raw
            -- intentionally NOT updating: local_path, downloaded_at
    """, (
        a["id"], ticket_id, comment_id, a.get("file_name"),
        a.get("content_type"), a.get("size"), a.get("content_url"),
        json.dumps(a), now_iso(),
    ))


def attachments_for_ticket(c: sqlite3.Connection, ticket_id: int) -> list[dict]:
    rows = c.execute("""
        SELECT id, comment_id, file_name, content_type, size_bytes, content_url, local_path, downloaded_at
        FROM ticket_attachments WHERE ticket_id=? ORDER BY id
    """, (ticket_id,)).fetchall()
    return [dict(r) for r in rows]


# =============================================================================
# Native forms (Block #A3)
# =============================================================================
def list_native_forms(c: sqlite3.Connection, *, active_only: bool = False) -> list[dict]:
    sql = "SELECT * FROM native_forms"
    if active_only:
        sql += " WHERE active=1"
    sql += " ORDER BY position, id"
    return [dict(r) for r in c.execute(sql).fetchall()]


def get_native_form(c: sqlite3.Connection, form_id: int) -> dict | None:
    row = c.execute("SELECT * FROM native_forms WHERE id=?", (form_id,)).fetchone()
    if not row:
        return None
    out = dict(row)
    out["group_ids"] = json.loads(out.get("group_ids") or "[]")
    out["field_ids"] = json.loads(out.get("field_ids") or "[]")
    out["required_field_ids"] = json.loads(out.get("required_field_ids") or "[]")
    out["conditions"] = [
        dict(c) for c in c.execute(
            "SELECT * FROM native_form_conditions WHERE form_id=? ORDER BY target_field_id, id",
            (form_id,)).fetchall()
    ]
    return out


def upsert_native_form(c: sqlite3.Connection, *, form_id: int | None = None, name: str,
                       description: str = "", active: bool = True,
                       group_ids: list[int] | None = None,
                       field_ids: list[int] | None = None,
                       required_field_ids: list[int] | None = None,
                       position: int = 0, actor_email: str = "") -> int:
    """Create or update a native form. Returns the form id."""
    now = now_iso()
    payload = (name, description, 1 if active else 0,
               json.dumps(group_ids or []), json.dumps(field_ids or []),
               json.dumps(required_field_ids or []), position)
    if form_id:
        c.execute("""
            UPDATE native_forms SET name=?, description=?, active=?, group_ids=?,
                field_ids=?, required_field_ids=?, position=?, updated_at=?
            WHERE id=?
        """, payload + (now, form_id))
        return form_id
    c.execute("""
        INSERT INTO native_forms (name, description, active, group_ids, field_ids,
            required_field_ids, position, created_by, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, payload + (actor_email, now, now))
    return c.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]


def replace_form_conditions(c: sqlite3.Connection, form_id: int, conditions: list[dict]) -> int:
    """Wipe + re-insert this form's conditions. Conditions are small (<50 per form)
    so a full replace is simpler than diffing. Each cond may carry `target_required`
    which when set forces the target field to required (1) or optional (0) at the
    moment the rule fires — independent of the form's required_field_ids list."""
    c.execute("DELETE FROM native_form_conditions WHERE form_id=?", (form_id,))
    n = 0
    for cond in conditions:
        op = (cond.get("op") or "eq").lower()
        if op not in ("eq", "neq", "set", "unset"):
            continue
        tr_raw = cond.get("target_required", None)
        if tr_raw is None or tr_raw == "":
            target_required = None
        else:
            try:
                target_required = 1 if int(tr_raw) else 0
            except (TypeError, ValueError):
                target_required = None
        c.execute("""
            INSERT INTO native_form_conditions (form_id, target_field_id, source_field_id,
                op, source_value, target_required, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (form_id, int(cond["target_field_id"]), int(cond["source_field_id"]),
              op, str(cond.get("source_value") or ""), target_required, now_iso()))
        n += 1
    return n


def resolve_form_for_ticket(c: sqlite3.Connection, *, group_id: int | None,
                            existing_form_id: int | None = None) -> dict | None:
    """Pick the best native form for this ticket. Preference order:
       1. The form explicitly set on the ticket (existing_form_id, if it's a
          native form)
       2. The lowest-position active native form whose group_ids contains group_id
       3. None (caller falls back to PRODUCT_SUPPORT_FIELDS / MANAGED_SERVICES_FIELDS)
    """
    if existing_form_id:
        f = get_native_form(c, existing_form_id)
        if f and f["active"]:
            return f
    if group_id is None:
        return None
    # Find native forms whose group_ids JSON array contains this group_id.
    rows = c.execute(
        "SELECT * FROM native_forms WHERE active=1 ORDER BY position, id"
    ).fetchall()
    for r in rows:
        try:
            gids = json.loads(r["group_ids"] or "[]")
        except Exception:
            gids = []
        if group_id in gids:
            return get_native_form(c, r["id"])
    return None


def evaluate_visibility(form: dict, current_values: dict) -> tuple[set[int], dict[int, str], dict[int, bool]]:
    """Run conditional-visibility rules. Returns
       (visible_field_ids, why_hidden, required_overrides).

       current_values maps field_id (int) -> string value of that field on the
       ticket.

       A field is visible if ANY rule for it passes, or it has no rules at all
       (default-visible). When a rule passes AND that rule has target_required
       set (0 or 1), the field's required flag is overridden — last passing
       rule for a given field wins."""
    field_ids = list(form.get("field_ids") or [])
    rules_by_target: dict[int, list[dict]] = {}
    for r in (form.get("conditions") or []):
        rules_by_target.setdefault(int(r["target_field_id"]), []).append(r)

    visible: set[int] = set()
    reasons: dict[int, str] = {}
    required_overrides: dict[int, bool] = {}
    for fid in field_ids:
        rules = rules_by_target.get(int(fid))
        if not rules:
            visible.add(int(fid))
            continue
        passed_rule = None
        for r in rules:
            src_val = (current_values.get(int(r["source_field_id"])) or "")
            op = r["op"]
            need = r.get("source_value") or ""
            if op == "set" and src_val:
                passed_rule = r; break
            if op == "unset" and not src_val:
                passed_rule = r; break
            if op == "eq" and str(src_val) == str(need):
                passed_rule = r; break
            if op == "neq" and str(src_val) != str(need):
                passed_rule = r; break
        if passed_rule is not None:
            visible.add(int(fid))
            tr = passed_rule.get("target_required")
            if tr is not None:
                required_overrides[int(fid)] = bool(int(tr))
        else:
            r0 = rules[0]
            reasons[int(fid)] = f"hidden — needs field #{r0['source_field_id']} {r0['op']} {r0.get('source_value','')}"
    return visible, reasons, required_overrides


# =============================================================================
# Agents + assignment + automations + auto-replies + native fields + gmail (Blocks #2-#7)
# Lightweight CRUD — each admin page calls these instead of inlining SQL.
# =============================================================================

def list_native_agents(c: sqlite3.Connection) -> list[dict]:
    rows = c.execute("""
        SELECT na.*, u.name AS user_name, u.email AS user_email
        FROM native_agents na LEFT JOIN users u ON u.id = na.user_id
        ORDER BY na.active DESC, na.display_name
    """).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try: d["group_ids"] = json.loads(d.get("group_ids") or "[]")
        except Exception: d["group_ids"] = []
        # Active load = only tickets that need attention. Excludes pending
        # (waiting on customer) and hold (with engineering / paused) because
        # those don't really consume agent capacity.
        d["current_load"] = c.execute(
            "SELECT COUNT(*) AS n FROM tickets WHERE assignee_id=? AND status IN ('new','open')",
            (r["user_id"],)).fetchone()["n"]
        out.append(d)
    return out


def upsert_native_agent(c: sqlite3.Connection, *, user_id: int, display_name: str,
                       availability: str, max_open_tickets: int, group_ids: list[int],
                       active: bool) -> None:
    now = now_iso()
    c.execute("""
        INSERT INTO native_agents (user_id, display_name, availability, max_open_tickets,
            group_ids, active, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            display_name=excluded.display_name, availability=excluded.availability,
            max_open_tickets=excluded.max_open_tickets, group_ids=excluded.group_ids,
            active=excluded.active, updated_at=excluded.updated_at
    """, (user_id, display_name, availability, max_open_tickets,
          json.dumps(group_ids), 1 if active else 0, now, now))


def pick_next_agent_for_group(c: sqlite3.Connection, group_id: int, *, only_online: bool = True) -> int | None:
    """Round-robin pick. Among agents whose group_ids contain group_id and who
    are active (and online if only_online), pick the one with the lowest current
    open load. Tie-break by last_assigned_at (oldest first)."""
    rows = c.execute("""
        SELECT na.user_id, na.group_ids, na.availability, na.max_open_tickets,
               na.last_assigned_at,
               (SELECT COUNT(*) FROM tickets WHERE assignee_id=na.user_id
                 AND status IN ('new','open')) AS load
        FROM native_agents na WHERE na.active=1
    """).fetchall()
    eligible = []
    for r in rows:
        try: gids = json.loads(r["group_ids"] or "[]")
        except Exception: gids = []
        if group_id not in gids:
            continue
        if only_online and r["availability"] != "online":
            continue
        if r["max_open_tickets"] and r["load"] >= r["max_open_tickets"]:
            continue
        eligible.append(dict(r))
    if not eligible:
        return None
    eligible.sort(key=lambda r: (r["load"], r["last_assigned_at"] or ""))
    pick = eligible[0]["user_id"]
    c.execute("UPDATE native_agents SET last_assigned_at=? WHERE user_id=?", (now_iso(), pick))
    return pick


def list_automations(c: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in c.execute(
        "SELECT * FROM automations ORDER BY position, id"
    ).fetchall()]


def upsert_automation(c: sqlite3.Connection, *, automation_id: int | None,
                     name: str, description: str, active: bool,
                     trigger_type: str, trigger_params: dict,
                     conditions: list[dict], actions: list[dict],
                     position: int, actor_email: str) -> int:
    now = now_iso()
    payload = (name, description, 1 if active else 0, trigger_type,
               json.dumps(trigger_params), json.dumps(conditions),
               json.dumps(actions), position)
    if automation_id:
        c.execute("""
            UPDATE automations SET name=?, description=?, active=?, trigger_type=?,
                trigger_params=?, conditions_json=?, actions_json=?, position=?, updated_at=?
            WHERE id=?
        """, payload + (now, automation_id))
        return automation_id
    c.execute("""
        INSERT INTO automations (name, description, active, trigger_type, trigger_params,
            conditions_json, actions_json, position, created_by, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, payload + (actor_email, now, now))
    return c.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]


def list_auto_replies(c: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in c.execute(
        "SELECT * FROM auto_replies ORDER BY id"
    ).fetchall()]


def upsert_auto_reply(c: sqlite3.Connection, *, reply_id: int | None,
                     name: str, active: bool, scope_group_id: int | None,
                     scope_customer_value: str, body: str,
                     after_hours: str, business_hours_id: int | None,
                     fire_on: str, actor_email: str) -> int:
    now = now_iso()
    payload = (name, 1 if active else 0, scope_group_id, scope_customer_value,
               body, after_hours, business_hours_id, fire_on)
    if reply_id:
        c.execute("""
            UPDATE auto_replies SET name=?, active=?, scope_group_id=?,
                scope_customer_value=?, body=?, after_hours=?, business_hours_id=?,
                fire_on=?, updated_at=?
            WHERE id=?
        """, payload + (now, reply_id))
        return reply_id
    c.execute("""
        INSERT INTO auto_replies (name, active, scope_group_id, scope_customer_value,
            body, after_hours, business_hours_id, fire_on, created_by, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, payload + (actor_email, now, now))
    return c.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]


def list_native_fields(c: sqlite3.Connection, *, active_only: bool = False) -> list[dict]:
    sql = "SELECT * FROM native_fields"
    if active_only: sql += " WHERE active=1"
    sql += " ORDER BY position, title"
    return [dict(r) for r in c.execute(sql).fetchall()]


def next_native_field_id(c: sqlite3.Connection) -> int:
    """Native field ids start at 9_000_000_000 to avoid collision with ZD's 14-digit ids."""
    row = c.execute("SELECT MAX(id) AS m FROM native_fields").fetchone()
    base = 9_000_000_000
    cur = row["m"] if row and row["m"] else base - 1
    return max(cur + 1, base)


def upsert_native_field(c: sqlite3.Connection, *, field_id: int | None,
                       title: str, type_: str, required: bool,
                       options: list[dict], description: str,
                       active: bool, position: int, actor_email: str) -> int:
    now = now_iso()
    if field_id and c.execute("SELECT 1 FROM native_fields WHERE id=?", (field_id,)).fetchone():
        c.execute("""
            UPDATE native_fields SET title=?, type=?, required=?, options=?,
                description=?, active=?, position=?, updated_at=?
            WHERE id=?
        """, (title, type_, 1 if required else 0, json.dumps(options),
              description, 1 if active else 0, position, now, field_id))
        return field_id
    new_id = field_id or next_native_field_id(c)
    c.execute("""
        INSERT INTO native_fields (id, title, type, required, options, description,
            active, position, created_by, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (new_id, title, type_, 1 if required else 0, json.dumps(options),
          description, 1 if active else 0, position, actor_email, now, now))
    return new_id


def get_ai_worker_config(c: sqlite3.Connection) -> dict:
    row = c.execute("SELECT * FROM ai_worker_config WHERE id=1").fetchone()
    if row:
        return dict(row)
    return {"id": 1, "enabled": 0, "model": "sonnet", "batch_size": 10,
            "poll_interval_seconds": 60, "daily_ticket_cap": 200, "use_mcp": 1,
            "process_pid": None, "last_started_at": None, "last_stopped_at": None}


def save_ai_worker_config(c: sqlite3.Connection, **fields) -> None:
    cur = get_ai_worker_config(c)
    cur.update({k: v for k, v in fields.items() if v is not None})
    cur["updated_at"] = now_iso()
    c.execute("""
        INSERT INTO ai_worker_config (id, enabled, model, batch_size, poll_interval_seconds,
            daily_ticket_cap, use_mcp, process_pid, last_started_at, last_stopped_at, updated_at)
        VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            enabled=excluded.enabled, model=excluded.model,
            batch_size=excluded.batch_size, poll_interval_seconds=excluded.poll_interval_seconds,
            daily_ticket_cap=excluded.daily_ticket_cap, use_mcp=excluded.use_mcp,
            process_pid=excluded.process_pid,
            last_started_at=excluded.last_started_at, last_stopped_at=excluded.last_stopped_at,
            updated_at=excluded.updated_at
    """, (cur.get("enabled", 0), cur.get("model", "sonnet"), cur.get("batch_size", 10),
          cur.get("poll_interval_seconds", 60), cur.get("daily_ticket_cap", 200),
          cur.get("use_mcp", 1), cur.get("process_pid"),
          cur.get("last_started_at"), cur.get("last_stopped_at"), cur["updated_at"]))


def get_gmail_config(c: sqlite3.Connection) -> dict:
    row = c.execute("SELECT * FROM gmail_config WHERE id=1").fetchone()
    if not row:
        return {"id": 1, "enabled": 0, "mailbox_email": "", "label_in": "INBOX",
                "label_processed": "cowork-processed", "poll_interval_seconds": 60}
    return dict(row)


def save_gmail_config(c: sqlite3.Connection, **fields) -> None:
    cur = get_gmail_config(c)
    cur.update({k: v for k, v in fields.items() if v is not None})
    cur["updated_at"] = now_iso()
    c.execute("""
        INSERT INTO gmail_config (id, enabled, mailbox_email, label_in, label_processed,
            default_group_id, default_customer_value, poll_interval_seconds, updated_at)
        VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            enabled=excluded.enabled, mailbox_email=excluded.mailbox_email,
            label_in=excluded.label_in, label_processed=excluded.label_processed,
            default_group_id=excluded.default_group_id,
            default_customer_value=excluded.default_customer_value,
            poll_interval_seconds=excluded.poll_interval_seconds,
            updated_at=excluded.updated_at
    """, (cur.get("enabled", 0), cur.get("mailbox_email"), cur.get("label_in"),
          cur.get("label_processed"), cur.get("default_group_id"),
          cur.get("default_customer_value"), cur.get("poll_interval_seconds", 60),
          cur["updated_at"]))


def attachments_summary(c: sqlite3.Connection) -> dict:
    row = c.execute("""
        SELECT COUNT(*) AS n, COALESCE(SUM(size_bytes),0) AS total_bytes,
               COALESCE(SUM(CASE WHEN local_path IS NOT NULL THEN 1 ELSE 0 END), 0) AS downloaded
        FROM ticket_attachments
    """).fetchone()
    by_type = c.execute("""
        SELECT content_type, COUNT(*) AS n, COALESCE(SUM(size_bytes),0) AS bytes
        FROM ticket_attachments GROUP BY content_type ORDER BY n DESC LIMIT 10
    """).fetchall()
    return {
        "count": row["n"], "total_bytes": row["total_bytes"], "downloaded": row["downloaded"],
        "by_type": [dict(r) for r in by_type],
    }


# =============================================================================
# Access Control helpers (F0)
# =============================================================================
# The schema is in SCHEMA above. These helpers wrap the common operations:
# - list/get/upsert app_users
# - list/get/create/update/delete roles
# - grant/revoke role to user
# - grant/revoke permission to role
# - compute effective permissions for a user
# - access_audit helper
# All bootstrap protections (can't delete last admin, can't strip
# admin.users/admin.roles from last role holding them) are enforced HERE so
# both the UI endpoints AND any future programmatic callers go through the
# same gate.

OWNER_EMAIL_DEFAULT = "subhajit.das@betterplace.co.in"

# Permission keys that, if removed from every role, would brick the app —
# kept in code (not DB) because they're tied to enforcement.
CRITICAL_PERMS = {"admin.users", "admin.roles"}


def list_app_users(c: sqlite3.Connection, *, include_disabled: bool = True) -> list[dict]:
    """All app users with their roles attached as a list of role names."""
    sql = "SELECT * FROM app_users"
    if not include_disabled:
        sql += " WHERE status = 'active'"
    sql += " ORDER BY created_at DESC"
    users = [dict(r) for r in c.execute(sql).fetchall()]
    if not users:
        return []
    # batch-fetch roles
    emails = [u["email"] for u in users]
    placeholders = ",".join("?" * len(emails))
    role_rows = c.execute(f"""
        SELECT ur.user_email, r.id AS role_id, r.name AS role_name
        FROM user_roles ur
        JOIN roles r ON r.id = ur.role_id
        WHERE ur.user_email IN ({placeholders})
    """, emails).fetchall()
    by_email: dict[str, list[dict]] = {}
    for r in role_rows:
        by_email.setdefault(r["user_email"], []).append(
            {"id": r["role_id"], "name": r["role_name"]}
        )
    for u in users:
        u["roles"] = by_email.get(u["email"], [])
    return users


def get_app_user(c: sqlite3.Connection, email: str) -> dict | None:
    row = c.execute("SELECT * FROM app_users WHERE email=?", (email,)).fetchone()
    if not row:
        return None
    user = dict(row)
    user["roles"] = [
        {"id": r["id"], "name": r["name"]}
        for r in c.execute("""
            SELECT r.id, r.name FROM user_roles ur
            JOIN roles r ON r.id = ur.role_id
            WHERE ur.user_email = ?
            ORDER BY r.name
        """, (email,)).fetchall()
    ]
    return user


def upsert_app_user(c: sqlite3.Connection, *, email: str, name: str | None = None,
                    picture_url: str | None = None, status: str = "active",
                    created_by: str | None = None, mark_login: bool = False) -> None:
    """Idempotent insert/update of an app user. Used by OAuth login (mark_login=True)
    and by /admin/users invite (mark_login=False)."""
    now = now_iso()
    existing = c.execute("SELECT email FROM app_users WHERE email=?", (email,)).fetchone()
    if existing:
        sets = []
        params: list = []
        if name is not None:
            sets.append("name=?"); params.append(name)
        if picture_url is not None:
            sets.append("picture_url=?"); params.append(picture_url)
        if status is not None:
            sets.append("status=?"); params.append(status)
        if mark_login:
            sets.append("last_login_at=?"); params.append(now)
        if sets:
            params.append(email)
            c.execute(f"UPDATE app_users SET {', '.join(sets)} WHERE email=?", params)
    else:
        c.execute("""
            INSERT INTO app_users (email, name, picture_url, status, last_login_at,
                created_at, created_by)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (email, name, picture_url, status, now if mark_login else None,
              now, created_by))


def set_app_user_status(c: sqlite3.Connection, email: str, status: str,
                        actor_email: str) -> None:
    """Active/disabled toggle. Refuses to disable the last user holding a
    critical-perm role."""
    if status not in ("active", "disabled"):
        raise ValueError(f"invalid status: {status}")
    if status == "disabled":
        # Refuse if this would leave zero active users with admin.users or admin.roles.
        for crit in CRITICAL_PERMS:
            holders = c.execute("""
                SELECT DISTINCT ur.user_email
                FROM user_roles ur
                JOIN role_permissions rp ON rp.role_id = ur.role_id
                JOIN app_users u ON u.email = ur.user_email
                WHERE rp.permission_key = ? AND u.status = 'active'
            """, (crit,)).fetchall()
            active = {r["user_email"] for r in holders}
            if email in active and len(active) <= 1:
                raise ValueError(
                    f"Refusing to disable {email}: they are the last active user "
                    f"holding '{crit}'. Grant it to someone else first."
                )
    c.execute("UPDATE app_users SET status=? WHERE email=?", (status, email))
    log_access(c, actor_email=actor_email, event_type=f"user.{status}",
               target_kind="user", target_id=email, detail={})


def list_roles(c: sqlite3.Connection) -> list[dict]:
    """Roles with counts of users + permissions, ordered by system-default first then name."""
    rows = c.execute("""
        SELECT r.*,
               (SELECT COUNT(*) FROM user_roles WHERE role_id = r.id) AS user_count,
               (SELECT COUNT(*) FROM role_permissions WHERE role_id = r.id) AS perm_count
        FROM roles r
        ORDER BY r.is_system_default DESC, r.name
    """).fetchall()
    return [dict(r) for r in rows]


def get_role(c: sqlite3.Connection, role_id: int) -> dict | None:
    row = c.execute("SELECT * FROM roles WHERE id=?", (role_id,)).fetchone()
    if not row:
        return None
    role = dict(row)
    role["permissions"] = sorted([
        r["permission_key"] for r in c.execute(
            "SELECT permission_key FROM role_permissions WHERE role_id=?", (role_id,)
        ).fetchall()
    ])
    role["users"] = [
        {"email": r["email"], "name": r["name"], "status": r["status"]}
        for r in c.execute("""
            SELECT u.email, u.name, u.status
            FROM user_roles ur
            JOIN app_users u ON u.email = ur.user_email
            WHERE ur.role_id = ?
            ORDER BY u.name
        """, (role_id,)).fetchall()
    ]
    return role


def upsert_role(c: sqlite3.Connection, *, role_id: int | None = None,
                name: str, description: str = "",
                is_system_default: int = 0,
                actor_email: str) -> int:
    """Create or rename a role. Permissions are managed separately via
    set_role_permissions(). Returns the role id."""
    name = name.strip()
    if not name:
        raise ValueError("Role name cannot be empty")
    now = now_iso()
    if role_id is None:
        c.execute("""
            INSERT INTO roles (name, description, is_system_default, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
        """, (name, description, is_system_default, now, now))
        new_id = c.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
        log_access(c, actor_email=actor_email, event_type="role.create",
                   target_kind="role", target_id=str(new_id),
                   detail={"name": name, "description": description})
        return int(new_id)
    else:
        existing = c.execute("SELECT * FROM roles WHERE id=?", (role_id,)).fetchone()
        if not existing:
            raise ValueError(f"No role with id={role_id}")
        c.execute("""
            UPDATE roles SET name=?, description=?, updated_at=? WHERE id=?
        """, (name, description, now, role_id))
        log_access(c, actor_email=actor_email, event_type="role.update",
                   target_kind="role", target_id=str(role_id),
                   detail={"before": {"name": existing["name"],
                                       "description": existing["description"]},
                            "after": {"name": name, "description": description}})
        return role_id


def delete_role(c: sqlite3.Connection, role_id: int, actor_email: str) -> None:
    """Delete a role. Refuses to delete:
      - System roles (is_system_default=1)
      - The last role holding a critical permission key
    Users mapped to the deleted role are removed from it (ON DELETE CASCADE).
    """
    role = c.execute("SELECT * FROM roles WHERE id=?", (role_id,)).fetchone()
    if not role:
        raise ValueError(f"No role with id={role_id}")
    if role["is_system_default"]:
        raise ValueError(f"Refusing to delete system role '{role['name']}'.")
    # Check critical perms
    role_perms = {r["permission_key"] for r in c.execute(
        "SELECT permission_key FROM role_permissions WHERE role_id=?", (role_id,)
    ).fetchall()}
    for crit in CRITICAL_PERMS & role_perms:
        others = c.execute("""
            SELECT COUNT(DISTINCT role_id) AS n FROM role_permissions
            WHERE permission_key = ? AND role_id != ?
        """, (crit, role_id)).fetchone()["n"]
        if others == 0:
            raise ValueError(
                f"Refusing to delete '{role['name']}': it's the only role with '{crit}'. "
                f"Grant that permission to another role first."
            )
    c.execute("DELETE FROM roles WHERE id=?", (role_id,))
    log_access(c, actor_email=actor_email, event_type="role.delete",
               target_kind="role", target_id=str(role_id),
               detail={"name": role["name"]})


def set_role_permissions(c: sqlite3.Connection, role_id: int,
                          permission_keys: list[str],
                          actor_email: str,
                          *, valid_keys: set[str] | None = None) -> None:
    """Replace the set of permissions on a role. If valid_keys is provided,
    rejects any key not in it (catches typos from the UI). Bootstrap-safe:
    refuses to leave the system with zero roles holding admin.users or admin.roles."""
    role = c.execute("SELECT * FROM roles WHERE id=?", (role_id,)).fetchone()
    if not role:
        raise ValueError(f"No role with id={role_id}")
    new_perms = set(permission_keys)
    if valid_keys is not None:
        unknown = new_perms - valid_keys
        if unknown:
            raise ValueError(f"Unknown permission keys: {sorted(unknown)}")
    # Bootstrap-protect critical perms
    existing_perms = {r["permission_key"] for r in c.execute(
        "SELECT permission_key FROM role_permissions WHERE role_id=?", (role_id,)
    ).fetchall()}
    for crit in CRITICAL_PERMS:
        # Was on this role and is being removed?
        if crit in existing_perms and crit not in new_perms:
            others = c.execute("""
                SELECT COUNT(DISTINCT role_id) AS n FROM role_permissions
                WHERE permission_key = ? AND role_id != ?
            """, (crit, role_id)).fetchone()["n"]
            if others == 0:
                raise ValueError(
                    f"Refusing to remove '{crit}' from role '{role['name']}': "
                    f"no other role holds it. Grant it to another role first."
                )
    # Replace
    c.execute("DELETE FROM role_permissions WHERE role_id=?", (role_id,))
    now = now_iso()
    for k in sorted(new_perms):
        c.execute("""
            INSERT INTO role_permissions (role_id, permission_key, granted_at)
            VALUES (?, ?, ?)
        """, (role_id, k, now))
    added = new_perms - existing_perms
    removed = existing_perms - new_perms
    if added or removed:
        log_access(c, actor_email=actor_email, event_type="role.update_perms",
                   target_kind="role", target_id=str(role_id),
                   detail={"role_name": role["name"],
                           "added": sorted(added), "removed": sorted(removed)})


def grant_role_to_user(c: sqlite3.Connection, *, user_email: str, role_id: int,
                       actor_email: str) -> None:
    user = c.execute("SELECT email FROM app_users WHERE email=?", (user_email,)).fetchone()
    if not user:
        raise ValueError(f"No app_user with email={user_email}")
    role = c.execute("SELECT name FROM roles WHERE id=?", (role_id,)).fetchone()
    if not role:
        raise ValueError(f"No role with id={role_id}")
    c.execute("""
        INSERT OR IGNORE INTO user_roles (user_email, role_id, granted_at, granted_by)
        VALUES (?, ?, ?, ?)
    """, (user_email, role_id, now_iso(), actor_email))
    log_access(c, actor_email=actor_email, event_type="role.grant",
               target_kind="user", target_id=user_email,
               detail={"role_id": role_id, "role_name": role["name"]})


def revoke_role_from_user(c: sqlite3.Connection, *, user_email: str, role_id: int,
                          actor_email: str) -> None:
    """Revoke a role from a user. Refuses if it would leave zero users holding
    a critical permission."""
    role = c.execute("SELECT name FROM roles WHERE id=?", (role_id,)).fetchone()
    if not role:
        return
    role_perms = {r["permission_key"] for r in c.execute(
        "SELECT permission_key FROM role_permissions WHERE role_id=?", (role_id,)
    ).fetchall()}
    for crit in CRITICAL_PERMS & role_perms:
        # Would removing this user's role leave nobody with the perm?
        holders = c.execute("""
            SELECT DISTINCT ur.user_email
            FROM user_roles ur
            JOIN role_permissions rp ON rp.role_id = ur.role_id
            JOIN app_users u ON u.email = ur.user_email
            WHERE rp.permission_key = ? AND u.status = 'active'
        """, (crit,)).fetchall()
        # Simulate the revoke: would this user still have the perm via another role?
        other_role_ids = [
            r["role_id"] for r in c.execute("""
                SELECT role_id FROM user_roles
                WHERE user_email=? AND role_id != ?
            """, (user_email, role_id)).fetchall()
        ]
        still_holds = False
        if other_role_ids:
            placeholders = ",".join("?" * len(other_role_ids))
            row = c.execute(
                f"SELECT 1 FROM role_permissions WHERE permission_key=? "
                f"AND role_id IN ({placeholders}) LIMIT 1",
                [crit] + other_role_ids
            ).fetchone()
            still_holds = row is not None
        if not still_holds:
            active_holders = {r["user_email"] for r in holders}
            if user_email in active_holders and len(active_holders) <= 1:
                raise ValueError(
                    f"Refusing to revoke '{role['name']}' from {user_email}: "
                    f"they're the only user with '{crit}'. Grant it to someone else first."
                )
    c.execute("DELETE FROM user_roles WHERE user_email=? AND role_id=?",
              (user_email, role_id))
    log_access(c, actor_email=actor_email, event_type="role.revoke",
               target_kind="user", target_id=user_email,
               detail={"role_id": role_id, "role_name": role["name"]})


def get_user_permissions(c: sqlite3.Connection, email: str) -> set[str]:
    """Compute the union of permission keys across all of a user's active roles."""
    user = c.execute(
        "SELECT status FROM app_users WHERE email=?", (email,)
    ).fetchone()
    if not user or user["status"] != "active":
        return set()
    rows = c.execute("""
        SELECT DISTINCT rp.permission_key
        FROM user_roles ur
        JOIN role_permissions rp ON rp.role_id = ur.role_id
        WHERE ur.user_email = ?
    """, (email,)).fetchall()
    return {r["permission_key"] for r in rows}


def log_access(c: sqlite3.Connection, *, actor_email: str, event_type: str,
               target_kind: str = "", target_id: str = "",
               detail: dict | None = None) -> None:
    c.execute("""
        INSERT INTO access_audit (actor_email, event_type, target_kind, target_id,
            detail_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (actor_email, event_type, target_kind, target_id,
          json.dumps(detail or {}), now_iso()))


def list_access_audit(c: sqlite3.Connection, *, limit: int = 200,
                      target_kind: str | None = None,
                      target_id: str | None = None) -> list[dict]:
    sql = "SELECT * FROM access_audit"
    params: list = []
    where: list[str] = []
    if target_kind:
        where.append("target_kind=?"); params.append(target_kind)
    if target_id:
        where.append("target_id=?"); params.append(target_id)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in c.execute(sql, params).fetchall()]


def seed_access_control(c: sqlite3.Connection,
                        owner_email: str = OWNER_EMAIL_DEFAULT) -> dict:
    """Idempotent seed: creates the three built-in roles (Admin/Agent/View-only)
    with their default permission sets and ensures `owner_email` exists as
    an Admin user. Safe to run on every boot. Returns a small status dict.

    Additive-seed model: tracks per-role 'seeded perm keys' in `meta` so we can
    add NEW perm keys (added to the catalog after install) to system-default
    roles WITHOUT re-adding perms an admin has intentionally removed.

    Imports permissions catalog lazily to avoid circular imports during db.init.
    """
    from . import permissions as P
    valid_keys = set(P.ALL_KEYS)
    now = now_iso()
    result = {"roles_created": [], "perms_seeded": {}, "owner_bootstrapped": False}

    # 1. Ensure built-in roles exist + their perm sets are up to date.
    for role_name, role_meta in P.DEFAULT_ROLES.items():
        row = c.execute("SELECT * FROM roles WHERE name=?", (role_name,)).fetchone()
        if row is None:
            c.execute("""
                INSERT INTO roles (name, description, is_system_default, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
            """, (role_name, role_meta["description"],
                  1 if role_meta.get("is_system_default") else 0,
                  now, now))
            result["roles_created"].append(role_name)
        role_id = c.execute("SELECT id FROM roles WHERE name=?",
                            (role_name,)).fetchone()["id"]
        # Track which catalog keys we've EVER seeded into this role (across all
        # boots). If a key is in DEFAULT_ROLES but NOT in this set, it's brand
        # new — add it now. If a key was previously seeded but is missing,
        # admin removed it; leave it gone.
        seeded_meta_key = f"role_perms_seeded:{role_name}"
        already_seeded_raw = get_meta(c, seeded_meta_key) or "[]"
        try:
            already_seeded = set(json.loads(already_seeded_raw))
        except json.JSONDecodeError:
            already_seeded = set()
        desired = set(role_meta["permissions"]) & valid_keys
        to_add = desired - already_seeded
        added: list[str] = []
        for pk in sorted(to_add):
            c.execute("""
                INSERT OR IGNORE INTO role_permissions (role_id, permission_key, granted_at)
                VALUES (?, ?, ?)
            """, (role_id, pk, now))
            added.append(pk)
        # Record the new "ever-seeded" set for future boots.
        # We union with `desired` so even keys that already existed (e.g. on
        # the very first install when already_seeded is empty) get tracked.
        new_seeded = already_seeded | desired
        if new_seeded != already_seeded:
            set_meta(c, seeded_meta_key, json.dumps(sorted(new_seeded)))
        if added:
            result["perms_seeded"][role_name] = added

    # 2. Ensure owner exists as Admin (bootstrap)
    owner = c.execute("SELECT email FROM app_users WHERE email=?",
                      (owner_email,)).fetchone()
    if not owner:
        c.execute("""
            INSERT INTO app_users (email, name, status, created_at, created_by)
            VALUES (?, ?, 'active', ?, 'bootstrap')
        """, (owner_email, owner_email.split("@")[0].replace(".", " ").title(), now))
        result["owner_bootstrapped"] = True
    admin_role_id = c.execute(
        "SELECT id FROM roles WHERE name='Admin'"
    ).fetchone()["id"]
    c.execute("""
        INSERT OR IGNORE INTO user_roles (user_email, role_id, granted_at, granted_by)
        VALUES (?, ?, ?, 'bootstrap')
    """, (owner_email, admin_role_id, now))

    return result


# =============================================================================
# Groups (F0+) — extends the basic ZD-synced `groups` table
# =============================================================================

# Native group IDs live in the 9-billion range so they never collide with ZD's.
# ZD group IDs are typically 8-10 digit integers; native_groups start at 9e9.
NATIVE_GROUP_ID_BASE = 9_000_000_000


def next_native_group_id(c: sqlite3.Connection) -> int:
    row = c.execute(
        "SELECT MAX(id) AS m FROM groups WHERE is_native=1 AND id >= ?",
        (NATIVE_GROUP_ID_BASE,)
    ).fetchone()
    return (row["m"] or NATIVE_GROUP_ID_BASE - 1) + 1


def list_groups(c: sqlite3.Connection, *, active_only: bool = False) -> list[dict]:
    """All groups (ZD-synced + native), each with a member count."""
    sql = """
        SELECT g.id, g.name, g.description, g.is_native, g.is_active,
               g.created_at, g.updated_at, g.created_by,
               (SELECT COUNT(*) FROM user_groups WHERE group_id = g.id) AS member_count
        FROM groups g
    """
    if active_only:
        sql += " WHERE g.is_active = 1"
    sql += " ORDER BY g.is_native, g.name"
    return [dict(r) for r in c.execute(sql).fetchall()]


def get_group(c: sqlite3.Connection, group_id: int) -> dict | None:
    row = c.execute("SELECT * FROM groups WHERE id=?", (group_id,)).fetchone()
    if not row:
        return None
    g = dict(row)
    g["members"] = [
        {"email": r["user_email"], "granted_at": r["granted_at"]}
        for r in c.execute("""
            SELECT ug.user_email, ug.granted_at
            FROM user_groups ug
            JOIN app_users u ON u.email = ug.user_email
            WHERE ug.group_id = ?
            ORDER BY u.email
        """, (group_id,)).fetchall()
    ]
    return g


def upsert_native_group(c: sqlite3.Connection, *,
                        group_id: int | None,
                        name: str,
                        description: str = "",
                        is_active: int = 1,
                        actor_email: str) -> int:
    """Create or rename a NATIVE group. Won't touch ZD-synced rows."""
    name = (name or "").strip()
    if not name:
        raise ValueError("Group name required")
    now = now_iso()
    if group_id is None:
        new_id = next_native_group_id(c)
        c.execute("""
            INSERT INTO groups (id, name, description, is_native, is_active,
                created_at, updated_at, created_by)
            VALUES (?, ?, ?, 1, ?, ?, ?, ?)
        """, (new_id, name, description, is_active, now, now, actor_email))
        log_access(c, actor_email=actor_email, event_type="group.create",
                   target_kind="group", target_id=str(new_id),
                   detail={"name": name})
        return new_id
    existing = c.execute("SELECT * FROM groups WHERE id=?", (group_id,)).fetchone()
    if not existing:
        raise ValueError(f"No group {group_id}")
    if not existing["is_native"]:
        raise ValueError(
            f"Cannot edit group '{existing['name']}' — it's synced from "
            f"Zendesk. Edit it in Zendesk instead."
        )
    c.execute("""
        UPDATE groups SET name=?, description=?, is_active=?, updated_at=?
        WHERE id=?
    """, (name, description, is_active, now, group_id))
    log_access(c, actor_email=actor_email, event_type="group.update",
               target_kind="group", target_id=str(group_id),
               detail={"name": name})
    return group_id


def set_group_active(c: sqlite3.Connection, group_id: int, active: int,
                     actor_email: str) -> None:
    g = c.execute("SELECT name, is_native FROM groups WHERE id=?",
                  (group_id,)).fetchone()
    if not g:
        raise ValueError(f"No group {group_id}")
    c.execute("UPDATE groups SET is_active=?, updated_at=? WHERE id=?",
              (1 if active else 0, now_iso(), group_id))
    log_access(c, actor_email=actor_email,
               event_type=f"group.{'enable' if active else 'archive'}",
               target_kind="group", target_id=str(group_id),
               detail={"name": g["name"]})


# ---- User ↔ group membership ----

def get_user_group_ids(c: sqlite3.Connection, email: str) -> list[int]:
    rows = c.execute(
        "SELECT group_id FROM user_groups WHERE user_email=?", (email,)
    ).fetchall()
    return [r["group_id"] for r in rows]


def set_user_groups(c: sqlite3.Connection, *, user_email: str,
                    group_ids: list[int], actor_email: str) -> None:
    """Replace the set of groups a user belongs to. Validates group existence."""
    if not c.execute("SELECT 1 FROM app_users WHERE email=?", (user_email,)).fetchone():
        raise ValueError(f"No app_user {user_email}")
    new_ids = set(group_ids)
    if new_ids:
        existing = {r["id"] for r in c.execute(
            f"SELECT id FROM groups WHERE id IN ({','.join('?' * len(new_ids))})",
            list(new_ids)
        ).fetchall()}
        unknown = new_ids - existing
        if unknown:
            raise ValueError(f"Unknown group_ids: {sorted(unknown)}")
    current = set(get_user_group_ids(c, user_email))
    to_add = new_ids - current
    to_remove = current - new_ids
    now = now_iso()
    for gid in to_remove:
        c.execute("DELETE FROM user_groups WHERE user_email=? AND group_id=?",
                  (user_email, gid))
    for gid in to_add:
        c.execute("""
            INSERT OR IGNORE INTO user_groups (user_email, group_id, granted_at, granted_by)
            VALUES (?, ?, ?, ?)
        """, (user_email, gid, now, actor_email))
    if to_add or to_remove:
        log_access(c, actor_email=actor_email, event_type="user.groups",
                   target_kind="user", target_id=user_email,
                   detail={"added": sorted(to_add), "removed": sorted(to_remove)})


# ---- ZD user mapping for app_users ----

def auto_map_zd_user(c: sqlite3.Connection, email: str) -> int | None:
    """Find the ZD users.id whose email matches this app_user's email.
    Used by OAuth callback to auto-populate app_users.zd_user_id on first
    login. Returns the zd_user_id (or None if no match in synced users)."""
    row = c.execute(
        "SELECT id FROM users WHERE LOWER(email) = LOWER(?) LIMIT 1", (email,)
    ).fetchone()
    if not row:
        return None
    zd_user_id = int(row["id"])
    c.execute("UPDATE app_users SET zd_user_id=? WHERE email=? AND zd_user_id IS NULL",
              (zd_user_id, email))
    return zd_user_id


# ---- F5 · Profile fields ----

VALID_AVAILABILITY = ("online", "away", "busy", "offline")
# Back-compat alias (deprecated — use VALID_AVAILABILITY)
VALID_STATUSES = VALID_AVAILABILITY
DEFAULT_WORK_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri"]


def get_user_profile(c: sqlite3.Connection, email: str) -> dict | None:
    """Returns the full profile row including parsed work_days_json.
    Used by /profile page + /profile/me/json + the avatar dropdown."""
    row = c.execute("SELECT * FROM app_users WHERE email=?", (email,)).fetchone()
    if not row:
        return None
    p = dict(row)
    try:
        p["work_days"] = json.loads(p.get("work_days_json") or "[]")
    except json.JSONDecodeError:
        p["work_days"] = list(DEFAULT_WORK_DAYS)
    if not p["work_days"]:
        p["work_days"] = list(DEFAULT_WORK_DAYS)
    return p


def update_user_profile(c: sqlite3.Connection, *, email: str,
                        name: str | None = None,
                        title: str | None = None,
                        timezone: str | None = None,
                        work_days: list[str] | None = None,
                        work_start_time: str | None = None,
                        work_end_time: str | None = None,
                        phone: str | None = None,
                        slack_handle: str | None = None,
                        bio: str | None = None,
                        notify_email: int | None = None,
                        notify_browser: int | None = None,
                        notify_sound: int | None = None) -> None:
    """Apply only the fields that were explicitly passed (not None)."""
    updates: list[str] = []
    params: list = []
    pairs = [
        ("name", name), ("title", title), ("timezone", timezone),
        ("work_start_time", work_start_time), ("work_end_time", work_end_time),
        ("phone", phone), ("slack_handle", slack_handle), ("bio", bio),
        ("notify_email", notify_email), ("notify_browser", notify_browser),
        ("notify_sound", notify_sound),
    ]
    for col, val in pairs:
        if val is not None:
            updates.append(f"{col}=?"); params.append(val)
    if work_days is not None:
        # Normalize: keep only valid day codes, preserve sensible order
        valid = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
        cleaned = [d for d in work_days if d in valid]
        # Preserve user's order but de-dupe
        seen: set[str] = set()
        ordered = [d for d in cleaned if not (d in seen or seen.add(d))]
        updates.append("work_days_json=?"); params.append(json.dumps(ordered or DEFAULT_WORK_DAYS))
    if not updates:
        return
    params.append(email)
    c.execute(f"UPDATE app_users SET {', '.join(updates)} WHERE email=?", params)


def set_user_availability(c: sqlite3.Connection, *, email: str,
                           availability: str,
                           emoji: str | None = None,
                           label: str | None = None,
                           until: str | None = None) -> None:
    """Set availability bucket + optional custom emoji/label/expiry.
    `availability` MUST be one of VALID_AVAILABILITY — custom statuses still
    map to one of these so availability-aware rules have a clean signal."""
    if availability not in VALID_AVAILABILITY:
        raise ValueError(f"availability must be one of {VALID_AVAILABILITY}, got {availability!r}")
    c.execute("""
        UPDATE app_users SET availability=?, availability_emoji=?,
            availability_label=?, availability_until=? WHERE email=?
    """, (availability, emoji, label, until, email))


# Back-compat alias name (some callers may not be updated yet)
def set_user_status(*args, **kwargs):
    # Adapt old kwargs to new names
    if "status" in kwargs:
        kwargs["availability"] = kwargs.pop("status")
    if "status_emoji" in kwargs:
        kwargs["emoji"] = kwargs.pop("status_emoji")
    if "status_label" in kwargs:
        kwargs["label"] = kwargs.pop("status_label")
    if "status_until" in kwargs:
        kwargs["until"] = kwargs.pop("status_until")
    return set_user_availability(*args, **kwargs)


def set_zd_user_mapping(c: sqlite3.Connection, *, user_email: str,
                        zd_user_id: int | None, actor_email: str) -> None:
    """Manual override of the ZD user mapping. Used from /admin/users when
    the email-auto-match found the wrong user (e.g. agent has a different
    sign-in email vs ZD email)."""
    if zd_user_id is not None:
        if not c.execute("SELECT 1 FROM users WHERE id=?", (zd_user_id,)).fetchone():
            raise ValueError(f"No ZD user with id={zd_user_id}")
    c.execute("UPDATE app_users SET zd_user_id=? WHERE email=?",
              (zd_user_id, user_email))
    log_access(c, actor_email=actor_email, event_type="user.zd_mapping",
               target_kind="user", target_id=user_email,
               detail={"zd_user_id": zd_user_id})


# =============================================================================
# Native views (F0+)
# =============================================================================

def list_views_for_user(c: sqlite3.Connection, email: str,
                        *, include_system: bool = True) -> list[dict]:
    """Returns every view this user can see, ordered by their personal
    view_order_json with any unranked views appended after.

    Visibility:
      - scope='system' → always visible (the seeded defaults)
      - scope='shared' → visible if the user OR any of their groups is in view_shares
      - scope='personal' → visible only to owner_email
    """
    user_groups = get_user_group_ids(c, email)
    group_placeholders = ",".join("?" * len(user_groups)) if user_groups else "NULL"
    where_parts = ["v.active = 1"]
    params: list = []
    visibility_clauses = []
    if include_system:
        visibility_clauses.append("v.scope = 'system'")
    visibility_clauses.append("(v.scope = 'personal' AND v.owner_email = ?)")
    params.append(email)
    visibility_clauses.append("""
        (v.scope = 'shared' AND (
            v.owner_email = ?
            OR EXISTS (SELECT 1 FROM view_shares vs
                WHERE vs.view_id = v.id AND vs.share_kind = 'user' AND vs.share_id = ?)
            OR EXISTS (SELECT 1 FROM view_shares vs
                WHERE vs.view_id = v.id AND vs.share_kind = 'group'
                  AND CAST(vs.share_id AS INTEGER) IN (""" + group_placeholders + """))
        ))
    """)
    params.append(email)
    params.append(email)
    if user_groups:
        params.extend(user_groups)
    where_parts.append("(" + " OR ".join(visibility_clauses) + ")")
    sql = f"""
        SELECT v.* FROM native_views v
        WHERE {' AND '.join(where_parts)}
        ORDER BY v.is_system_default DESC, v.default_position, v.name
    """
    rows = [dict(r) for r in c.execute(sql, params).fetchall()]
    # Apply per-user ordering if set
    u = c.execute("SELECT view_order_json, hidden_views_json FROM app_users WHERE email=?",
                  (email,)).fetchone()
    if u:
        try:
            order = json.loads(u["view_order_json"] or "[]")
        except json.JSONDecodeError:
            order = []
        try:
            hidden = set(json.loads(u["hidden_views_json"] or "[]"))
        except json.JSONDecodeError:
            hidden = set()
        if order:
            order_idx = {vid: i for i, vid in enumerate(order)}
            rows.sort(key=lambda r: (order_idx.get(r["id"], 10_000), r["default_position"]))
        rows = [r for r in rows if r["id"] not in hidden]
    return rows


def get_view(c: sqlite3.Connection, view_id: int) -> dict | None:
    row = c.execute("SELECT * FROM native_views WHERE id=?", (view_id,)).fetchone()
    if not row:
        return None
    v = dict(row)
    v["shares"] = [
        {"kind": r["share_kind"], "id": r["share_id"]}
        for r in c.execute(
            "SELECT share_kind, share_id FROM view_shares WHERE view_id=?",
            (view_id,)
        ).fetchall()
    ]
    return v


def upsert_native_view(c: sqlite3.Connection, *, view_id: int | None,
                       name: str, description: str = "",
                       owner_email: str | None,
                       scope: str = "personal",
                       filter_json: str = "{}",
                       column_ids_json: str = "[]",
                       sort_json: str = "{}",
                       color: str = "indigo",
                       icon: str = "",
                       is_system_default: int = 0,
                       default_position: int = 0,
                       active: int = 1,
                       actor_email: str) -> int:
    name = (name or "").strip()
    if not name:
        raise ValueError("View name required")
    if scope not in ("personal", "shared", "system"):
        raise ValueError(f"Invalid scope: {scope}")
    now = now_iso()
    if view_id is None:
        c.execute("""
            INSERT INTO native_views (name, description, owner_email, scope,
                filter_json, column_ids_json, sort_json, color, icon,
                is_system_default, default_position, active, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (name, description, owner_email, scope, filter_json,
              column_ids_json, sort_json, color, icon,
              is_system_default, default_position, active, now, now))
        new_id = c.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
        log_access(c, actor_email=actor_email, event_type="view.create",
                   target_kind="view", target_id=str(new_id),
                   detail={"name": name, "scope": scope})
        return int(new_id)
    c.execute("""
        UPDATE native_views SET name=?, description=?, scope=?, filter_json=?,
            column_ids_json=?, sort_json=?, color=?, icon=?, active=?, updated_at=?
        WHERE id=?
    """, (name, description, scope, filter_json, column_ids_json, sort_json,
          color, icon, active, now, view_id))
    log_access(c, actor_email=actor_email, event_type="view.update",
               target_kind="view", target_id=str(view_id),
               detail={"name": name, "scope": scope})
    return view_id


def set_view_shares(c: sqlite3.Connection, view_id: int,
                    shares: list[dict], actor_email: str) -> None:
    """Replace the share list for a view. shares = [{kind:'user'|'group', id:str}, ...]"""
    c.execute("DELETE FROM view_shares WHERE view_id=?", (view_id,))
    now = now_iso()
    for s in shares:
        kind = s.get("kind")
        sid = str(s.get("id", "")).strip()
        if kind not in ("user", "group") or not sid:
            continue
        c.execute("""
            INSERT OR IGNORE INTO view_shares (view_id, share_kind, share_id,
                granted_at, granted_by)
            VALUES (?, ?, ?, ?, ?)
        """, (view_id, kind, sid, now, actor_email))


def delete_view(c: sqlite3.Connection, view_id: int, actor_email: str) -> None:
    v = c.execute("SELECT name, is_system_default FROM native_views WHERE id=?",
                  (view_id,)).fetchone()
    if not v:
        raise ValueError(f"No view {view_id}")
    if v["is_system_default"]:
        raise ValueError(
            f"Cannot delete system view '{v['name']}'. Archive it instead "
            f"(deactivate), or remove from your personal list via the eye icon."
        )
    c.execute("DELETE FROM native_views WHERE id=?", (view_id,))
    log_access(c, actor_email=actor_email, event_type="view.delete",
               target_kind="view", target_id=str(view_id),
               detail={"name": v["name"]})


def set_user_view_order(c: sqlite3.Connection, email: str,
                        view_ids: list[int]) -> None:
    """Save the user's drag-reorder of views."""
    c.execute("UPDATE app_users SET view_order_json=? WHERE email=?",
              (json.dumps(view_ids), email))


# =============================================================================
# F6 · User activity / leave / notifications / user automations
# =============================================================================

def log_activity(c: sqlite3.Connection, *, user_email: str,
                 event_type: str, event_subtype: str,
                 target_kind: str | None = None, target_id: str | None = None,
                 detail: dict | None = None,
                 ip_address: str | None = None,
                 user_agent: str | None = None,
                 session_id: str | None = None) -> None:
    """Append a user activity row. Designed to be cheap (single INSERT) and
    NEVER raise — failures here should not break the user's request.
    Reports/queries can aggregate on event_type+event_subtype later."""
    try:
        c.execute("""
            INSERT INTO user_activity_log (user_email, event_type, event_subtype,
                target_kind, target_id, detail_json, ip_address, user_agent,
                session_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (user_email, event_type, event_subtype, target_kind, target_id,
              json.dumps(detail or {}), ip_address, user_agent, session_id,
              now_iso()))
    except sqlite3.Error as e:
        # Swallow — activity logging is best-effort
        print(f"[log_activity] {e}")


def list_user_activity(c: sqlite3.Connection, *, user_email: str | None = None,
                        event_type: str | None = None,
                        since: str | None = None,
                        limit: int = 200) -> list[dict]:
    where: list[str] = []
    params: list = []
    if user_email:
        where.append("user_email=?"); params.append(user_email)
    if event_type:
        where.append("event_type=?"); params.append(event_type)
    if since:
        where.append("created_at>=?"); params.append(since)
    sql = "SELECT * FROM user_activity_log"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in c.execute(sql, params).fetchall()]


# ---- Leave mode ----

def set_user_leave(c: sqlite3.Connection, *, email: str,
                    on_leave: int,
                    leave_start: str | None = None,
                    leave_end: str | None = None,
                    reason: str | None = None) -> None:
    """Toggle leave mode. When on_leave=1 the auto-online + idle-warning
    automations skip this user. leave_start/end are optional date strings —
    NULL on either side means open-ended on that side."""
    c.execute("""
        UPDATE app_users SET on_leave=?, leave_start=?, leave_end=?,
            leave_reason=? WHERE email=?
    """, (1 if on_leave else 0, leave_start, leave_end, reason, email))


def is_user_on_leave(profile: dict, today_date: str | None = None) -> bool:
    """True if the user is in leave window. today_date is YYYY-MM-DD; defaults
    to today's date in UTC. Open-ended ranges are honored: NULL start = always
    on; NULL end = until cleared."""
    if not profile.get("on_leave"):
        return False
    from datetime import datetime as _dt, timezone as _tz
    today = today_date or _dt.now(_tz.utc).strftime("%Y-%m-%d")
    start = (profile.get("leave_start") or "")[:10]
    end = (profile.get("leave_end") or "")[:10]
    if start and today < start:
        return False
    if end and today > end:
        return False
    return True


# ---- Notifications ----

def create_notification(c: sqlite3.Connection, *, user_email: str,
                         title: str, body: str = "",
                         kind: str = "info",
                         action_url: str | None = None,
                         action_label: str | None = None,
                         source: str | None = None) -> int:
    """Create one notification row. Returns the new id so callers can
    cross-reference (e.g. the warning automation can chain follow-ups)."""
    if kind not in ("info", "warning", "error", "prompt", "success"):
        kind = "info"
    c.execute("""
        INSERT INTO user_notifications (user_email, kind, title, body,
            action_url, action_label, source, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (user_email, kind, title, body, action_url, action_label, source, now_iso()))
    return int(c.execute("SELECT last_insert_rowid() AS i").fetchone()["i"])


def list_notifications(c: sqlite3.Connection, *, user_email: str,
                        include_dismissed: bool = False,
                        limit: int = 50) -> list[dict]:
    sql = "SELECT * FROM user_notifications WHERE user_email=?"
    params: list = [user_email]
    if not include_dismissed:
        sql += " AND dismissed_at IS NULL"
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in c.execute(sql, params).fetchall()]


def unread_notification_count(c: sqlite3.Connection, user_email: str) -> int:
    row = c.execute("""
        SELECT COUNT(*) AS n FROM user_notifications
        WHERE user_email=? AND read_at IS NULL AND dismissed_at IS NULL
    """, (user_email,)).fetchone()
    return row["n"] if row else 0


def mark_notification_read(c: sqlite3.Connection, *, notification_id: int,
                            user_email: str) -> None:
    c.execute("""
        UPDATE user_notifications SET read_at=COALESCE(read_at, ?)
        WHERE id=? AND user_email=?
    """, (now_iso(), notification_id, user_email))


def dismiss_notification(c: sqlite3.Connection, *, notification_id: int,
                          user_email: str) -> None:
    c.execute("""
        UPDATE user_notifications SET dismissed_at=?, read_at=COALESCE(read_at, ?)
        WHERE id=? AND user_email=?
    """, (now_iso(), now_iso(), notification_id, user_email))


# ---- F8 · Feedback ----

def create_feedback(c: sqlite3.Connection, *, user_email: str,
                     body: str,
                     kind: str = "bug",
                     severity: str = "normal",
                     title: str = "",
                     page_url: str | None = None,
                     ticket_id: int | None = None,
                     user_agent: str | None = None) -> int:
    if kind not in ("bug", "idea", "question", "praise"):
        kind = "bug"
    if severity not in ("low", "normal", "high", "urgent"):
        severity = "normal"
    c.execute("""
        INSERT INTO user_feedback (user_email, kind, severity, title, body,
            page_url, ticket_id, user_agent, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'new', ?)
    """, (user_email, kind, severity, title, body, page_url, ticket_id,
          user_agent, now_iso()))
    return int(c.execute("SELECT last_insert_rowid() AS i").fetchone()["i"])


def list_feedback(c: sqlite3.Connection, *,
                   status: str | None = None,
                   limit: int = 200) -> list[dict]:
    sql = "SELECT * FROM user_feedback"
    params: list = []
    if status:
        sql += " WHERE status=?"; params.append(status)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in c.execute(sql, params).fetchall()]


def update_feedback(c: sqlite3.Connection, *, feedback_id: int,
                     status: str | None = None,
                     reply: str | None = None,
                     actor_email: str) -> None:
    parts: list[str] = []; params: list = []
    if status in ("new", "triaged", "closed"):
        parts.append("status=?"); params.append(status)
        if status != "new":
            parts.append("triaged_by=?"); params.append(actor_email)
            parts.append("triaged_at=?"); params.append(now_iso())
    if reply is not None:
        parts.append("reply=?"); params.append(reply)
        parts.append("replied_by=?"); params.append(actor_email)
        parts.append("replied_at=?"); params.append(now_iso())
    if not parts: return
    params.append(feedback_id)
    c.execute(f"UPDATE user_feedback SET {', '.join(parts)} WHERE id=?", params)


def feedback_open_count(c: sqlite3.Connection) -> int:
    row = c.execute(
        "SELECT COUNT(*) AS n FROM user_feedback WHERE status='new'"
    ).fetchone()
    return row["n"] if row else 0


def dismiss_all_notifications(c: sqlite3.Connection, user_email: str) -> int:
    """Returns count of rows updated."""
    cur = c.execute("""
        UPDATE user_notifications SET dismissed_at=?, read_at=COALESCE(read_at, ?)
        WHERE user_email=? AND dismissed_at IS NULL
    """, (now_iso(), now_iso(), user_email))
    return cur.rowcount


# ---- User automations ----

def list_user_automations(c: sqlite3.Connection,
                           *, active_only: bool = False) -> list[dict]:
    sql = "SELECT * FROM user_automations"
    if active_only:
        sql += " WHERE active = 1"
    sql += " ORDER BY position, id"
    return [dict(r) for r in c.execute(sql).fetchall()]


def get_user_automation(c: sqlite3.Connection, automation_id: int) -> dict | None:
    row = c.execute("SELECT * FROM user_automations WHERE id=?",
                     (automation_id,)).fetchone()
    return dict(row) if row else None


def upsert_user_automation(c: sqlite3.Connection, *,
                            automation_id: int | None,
                            name: str, description: str = "",
                            trigger_event: str,
                            conditions_json: str = '{"match":"all","rules":[]}',
                            actions_json: str = "[]",
                            active: int = 1,
                            position: int | None = None,
                            category: str = "trigger",
                            schedule_json: str = "{}",
                            is_system_default: int = 0,
                            interval_minutes: int | None = None,
                            actor_email: str) -> int:
    now = now_iso()
    if automation_id is None:
        if position is None:
            row = c.execute(
                "SELECT COALESCE(MAX(position),0)+10 AS p FROM user_automations"
            ).fetchone()
            position = row["p"]
        interval = int(interval_minutes) if interval_minutes else (5 if category == "scheduler" else 0)
        c.execute("""
            INSERT INTO user_automations (name, description, trigger_event,
                conditions_json, actions_json, active, position, category,
                schedule_json, is_system_default, interval_minutes,
                created_by, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (name, description, trigger_event, conditions_json, actions_json,
              1 if active else 0, position, category, schedule_json,
              1 if is_system_default else 0, interval,
              actor_email, now, now))
        return int(c.execute("SELECT last_insert_rowid() AS i").fetchone()["i"])
    # Update — only touch interval_minutes if explicitly passed
    if interval_minutes is not None:
        c.execute("""
            UPDATE user_automations SET name=?, description=?, trigger_event=?,
                conditions_json=?, actions_json=?, active=?, category=?,
                schedule_json=?, interval_minutes=?, updated_at=?
            WHERE id=?
        """, (name, description, trigger_event, conditions_json, actions_json,
              1 if active else 0, category, schedule_json,
              int(interval_minutes), now, automation_id))
    else:
        c.execute("""
            UPDATE user_automations SET name=?, description=?, trigger_event=?,
                conditions_json=?, actions_json=?, active=?, category=?,
                schedule_json=?, updated_at=?
            WHERE id=?
        """, (name, description, trigger_event, conditions_json, actions_json,
              1 if active else 0, category, schedule_json, now, automation_id))
    return automation_id


def delete_user_automation(c: sqlite3.Connection, automation_id: int) -> None:
    """System defaults are kept around for clarity even if deactivated — but
    admins can delete them if they really want to."""
    c.execute("DELETE FROM user_automations WHERE id=?", (automation_id,))


def record_user_automation_fire(c: sqlite3.Connection, automation_id: int,
                                  *, success: bool = True,
                                  error: str | None = None,
                                  schedule_next_in_minutes: int | None = None) -> None:
    """Record one fire of a user automation. Used by both event-driven and
    scheduler-driven paths. If schedule_next_in_minutes is provided, sets
    next_fire_at = now + that — for scheduler-type rules."""
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    now = now_iso()
    next_at = None
    if schedule_next_in_minutes:
        next_at = (_dt.now(_tz.utc) + _td(minutes=int(schedule_next_in_minutes))).isoformat()
    c.execute("""
        UPDATE user_automations SET last_fired_at=?, fire_count=fire_count+1,
            last_error=?, last_error_at=?, next_fire_at=COALESCE(?, next_fire_at)
        WHERE id=?
    """, (now, error if not success else None, now if not success else None,
          next_at, automation_id))


def schedule_next_fire(c: sqlite3.Connection, automation_id: int,
                        interval_minutes: int) -> None:
    """Compute and store the next scheduled fire time. Called from the
    scheduler after evaluating a rule (whether or not it actually fired)."""
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    next_at = (_dt.now(_tz.utc) + _td(minutes=int(interval_minutes))).isoformat()
    c.execute("UPDATE user_automations SET next_fire_at=? WHERE id=?",
              (next_at, automation_id))


def list_due_user_scheduler_rules(c: sqlite3.Connection) -> list[dict]:
    """Active scheduler-type rules whose next_fire_at is past (or NULL =
    never fired). Returned in position order so admins can prioritize."""
    rows = c.execute("""
        SELECT * FROM user_automations
        WHERE active = 1 AND category = 'scheduler'
          AND (next_fire_at IS NULL OR next_fire_at <= ?)
        ORDER BY position, id
    """, (now_iso(),)).fetchall()
    return [dict(r) for r in rows]


def seed_default_user_automations(c: sqlite3.Connection) -> dict:
    """Seed the two automations the user requested.

    (1) Auto-online when work day starts — when user's local time crosses
        their work_start_time on a work day, AND they're not on leave, AND
        they're currently offline, set availability='online'.

    (2) Idle-during-work warning — fires every 30min during work hours when
        the user has not been online and is not on leave. Sends an in-app
        notification AND an email (best-effort; SMTP no-op if not configured).
    """
    now = now_iso()
    defaults = [
        {
            "name": "Auto-online on work day start",
            "description": "Flip availability to Online when the user's local clock crosses their work_start_time on a work day (unless they're on leave). Checked every 5 minutes.",
            "trigger_event": "user.work_day_started",
            "conditions_json": json.dumps({"match": "all", "rules": [
                {"field": "user.on_leave", "op": "eq", "value": 0},
                {"field": "user.availability", "op": "eq", "value": "offline"},
            ]}),
            "actions_json": json.dumps([
                {"type": "set_availability", "params": {"value": "online"}},
                {"type": "log_event", "params": {"event": "auto_online",
                                                   "message": "Set to online by work-day automation"}},
            ]),
            "category": "scheduler",
            "interval_minutes": 5,
        },
        {
            "name": "Idle during work hours — nudge",
            "description": "Fires every 30 minutes when the user is in work hours but not online (and not on leave). Sends in-app notification and email.",
            "trigger_event": "user.idle_during_work",
            "conditions_json": json.dumps({"match": "all", "rules": [
                {"field": "user.on_leave", "op": "eq", "value": 0},
                {"field": "user.availability", "op": "ne", "value": "online"},
                {"field": "user.in_work_hours", "op": "eq", "value": 1},
            ]}),
            "actions_json": json.dumps([
                {"type": "send_in_app_notification", "params": {
                    "kind": "warning",
                    "title": "You appear offline on a work day",
                    "body": "You're set to {{user.availability}} during your work hours. If you're available, set yourself to Online — or mark On Leave if you're out today.",
                    "action_url": "/profile",
                    "action_label": "Update status →",
                }},
                {"type": "send_email", "params": {
                    "subject": "[BetterPlace Co-Pilot] You're showing offline during work hours",
                    "body": "Hi {{user.name}},\n\nYou've been marked {{user.availability}} for over 30 minutes during your scheduled work hours. If you're working, set yourself to Online: {{public_url}}/profile\n\nIf you're out, mark yourself On Leave on the same page to stop these nudges.\n\nThanks.",
                }},
                {"type": "log_event", "params": {"event": "idle_warning",
                                                   "message": "Idle nudge sent"}},
            ]),
            "category": "scheduler",
            "interval_minutes": 30,
        },
    ]
    result = {"created": [], "already_existed": []}
    for d in defaults:
        existing = c.execute("""
            SELECT id FROM user_automations WHERE name=? AND is_system_default=1
        """, (d["name"],)).fetchone()
        if existing:
            # Backfill interval if the row pre-dates the column
            try:
                c.execute("UPDATE user_automations SET interval_minutes=? WHERE id=? AND COALESCE(interval_minutes,0)=0",
                           (d["interval_minutes"], existing["id"]))
            except Exception:
                pass
            result["already_existed"].append(d["name"])
            continue
        # Pull interval out (upsert_user_automation doesn't accept it yet — set it post-insert)
        interval = d.pop("interval_minutes", 5)
        new_id = upsert_user_automation(c, automation_id=None, **d, active=1,
                                          is_system_default=1, actor_email="system")
        c.execute("UPDATE user_automations SET interval_minutes=? WHERE id=?",
                   (interval, new_id))
        result["created"].append(d["name"])
    return result


def seed_default_views(c: sqlite3.Connection) -> dict:
    """Seed the 7 group-specific default views as is_system_default=1, scope='system'.
    Idempotent on view name + is_system_default flag — won't duplicate."""
    now = now_iso()
    # Filter shape: rules list of {field, op, value}, match='all'
    DEFAULTS = [
        # (name, description, color, icon, filter_rules, default_position)
        ("Open · Product Support", "Open tickets routed to Product Support",
         "indigo", "🟢",
         [{"field": "status", "op": "in", "value": ["new", "open"]},
          {"field": "group_name", "op": "eq", "value": "Product Support"}], 10),
        ("Open · Managed Services", "Open tickets routed to Managed Services",
         "indigo", "🟢",
         [{"field": "status", "op": "in", "value": ["new", "open"]},
          {"field": "group_name", "op": "eq", "value": "Managed Services"}], 20),
        ("Assigned to me", "Tickets where you are the current assignee",
         "violet", "⭐",
         [{"field": "assignee_id", "op": "is_me"}], 30),
        ("Pending · Product Support", "Pending tickets routed to Product Support",
         "amber", "⏳",
         [{"field": "status", "op": "eq", "value": "pending"},
          {"field": "group_name", "op": "eq", "value": "Product Support"}], 40),
        ("Pending · Managed Services", "Pending tickets routed to Managed Services",
         "amber", "⏳",
         [{"field": "status", "op": "eq", "value": "pending"},
          {"field": "group_name", "op": "eq", "value": "Managed Services"}], 50),
        ("On-Hold · Product Support", "On-hold tickets routed to Product Support",
         "violet", "⏸",
         [{"field": "status", "op": "eq", "value": "hold"},
          {"field": "group_name", "op": "eq", "value": "Product Support"}], 60),
        ("On-Hold · Managed Services", "On-hold tickets routed to Managed Services",
         "violet", "⏸",
         [{"field": "status", "op": "eq", "value": "hold"},
          {"field": "group_name", "op": "eq", "value": "Managed Services"}], 70),
    ]
    result = {"created": [], "already_existed": []}
    for name, desc, color, icon, rules, pos in DEFAULTS:
        existing = c.execute(
            "SELECT id FROM native_views WHERE name=? AND is_system_default=1",
            (name,)
        ).fetchone()
        if existing:
            result["already_existed"].append(name)
            continue
        filter_obj = {"match": "all", "rules": rules}
        c.execute("""
            INSERT INTO native_views (name, description, owner_email, scope,
                filter_json, column_ids_json, sort_json, color, icon,
                is_system_default, default_position, active, created_at, updated_at)
            VALUES (?, ?, NULL, 'system', ?, ?, ?, ?, ?, 1, ?, 1, ?, ?)
        """, (name, desc, json.dumps(filter_obj), json.dumps([]),
              json.dumps({"field": "updated_at", "dir": "desc"}),
              color, icon, pos, now, now))
        result["created"].append(name)
    return result
