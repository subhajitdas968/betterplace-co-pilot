"""FastAPI app — three-pane Zendesk-style UI.

Routes:
  GET  /                  → redirect to /views/all_open
  GET  /views/{view}      → ticket list table for that view (with sort/filter)
  GET  /tickets/{id}      → ticket detail with conversation + AI insights
  POST /auth/login        → start Google OAuth
  GET  /auth/callback     → OAuth callback
  POST /auth/logout       → logout
  GET  /spend             → MTD Claude spend (JSON)
  GET  /health            → health check
"""
from __future__ import annotations
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from authlib.integrations.starlette_client import OAuth
from fastapi import FastAPI, Form, HTTPException, Request, Depends
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .. import config, db


HERE = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(HERE / "templates"))


def _clean_html(text: str | None) -> str:
    """Render-time fallback: decodes HTML entities and strips stray tags so existing
    dirty data (rows synced before db.clean_body landed) still displays correctly."""
    if not text:
        return ""
    import html as _html
    import re as _re
    s = _html.unescape(_html.unescape(text))
    s = _re.sub(r"<[^>]+>", "", s)
    return s.replace("\xa0", " ")


def _to_ist(ts: str | None, fmt: str = "%d %b %Y, %H:%M IST") -> str:
    """Convert an ISO/UTC timestamp to Asia/Kolkata (IST = UTC+5:30) display."""
    if not ts:
        return ""
    try:
        from datetime import datetime, timedelta, timezone
        s = ts.replace("Z", "+00:00") if isinstance(ts, str) else ts
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        ist = dt.astimezone(timezone(timedelta(hours=5, minutes=30)))
        return ist.strftime(fmt)
    except Exception:
        return ts


TEMPLATES.env.filters["clean"] = _clean_html
TEMPLATES.env.filters["ist"] = _to_ist

# Cache-busting token included in every CSS link, so browsers don't serve stale styles.
import time as _time
TEMPLATES.env.globals["asset_v"] = str(int(_time.time()))

# F0 · Access Control: expose has_perm() to every template so we can wrap
# edit buttons / admin links with {% if has_perm(user, 'tickets.public_reply') %}.
# Importing here (not at the top) keeps the import graph clean: auth.py depends
# on db.py and permissions.py, both already loaded by this point.
from .. import auth as _auth_for_jinja  # noqa: E402
TEMPLATES.env.globals["has_perm"] = _auth_for_jinja.has_perm
TEMPLATES.env.globals["has_any_perm"] = _auth_for_jinja.has_any_perm

# F9 · Expose the running version + git short-sha to every template so the
# footer chip always reflects the live build.
from .. import release as _release_for_jinja  # noqa: E402
def _runtime_info_safe():
    try:
        return _release_for_jinja.runtime_info()
    except Exception:
        return {"version": "?", "git_sha": "", "git_branch": "", "git_clean": True}
TEMPLATES.env.globals["runtime_info"] = _runtime_info_safe


app = FastAPI(title="BetterPlace Co-Pilot")
app.add_middleware(SessionMiddleware, secret_key=config.SESSION_SECRET, https_only=False, same_site="lax")
# F6 · User activity logger — must be added AFTER SessionMiddleware so it can
# read request.session. Order of add_middleware is reversed in execution, so
# Activity runs INSIDE Session (which is what we want).
from .. import activity as _activity
# PERF: log_pages=False — the middleware used to INSERT a row + UPDATE
# app_users on every page view, costing 50-200ms/request. Now it only assigns
# a session_id on the first request of a session. Meaningful events are
# logged explicitly from inside endpoints via activity.log().
app.add_middleware(_activity.ActivityMiddleware, log_pages=False)
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")


# ---------- Field IDs (BetterPlace specific) ----------
FID_CUSTOMER = "15315331275025"
FID_PRODUCT = "15316390522769"
FID_MODULE = "15316445624849"
FID_SECTION = "15316602616721"
FID_BUCKETIZATION = "35194939804689"
FID_RC1 = "15316740884753"
FID_RC2 = "15316876186897"
FID_JIRA_ID = "15316921871633"
FID_JIRA_STATUS = "15407912716049"
FID_KB_ARTICLE = "15317743732625"
FID_HOW_RESOLVED = "15410842136337"
FID_WHAT_ISSUE = "44188920511761"
FID_TOTAL_PROFILES = "15648816519313"

# Per-group field title lists (fallback when no form is set)
PRODUCT_SUPPORT_FIELDS = [
    "Customer Name", "Priority", "Product", "Module", "Section",
    "Bucketization (Mandatory for Reliance)",
    "Root Cause - Level 1", "Root Cause - Level 2",
    "Jira ID", "Jira Status - DO NOT EDIT", "KB Article",
    "How was this ticket resolved?", "What was the issue?",
    "Total Number of Profiles Mentioned On This Ticket",
]
MANAGED_SERVICES_FIELDS = [
    "Request type", "Assigned Name", "Priority", "Parent Client", "Customer Name",
    "Product", "Service type", "Service Sub-Type", "Root Cause - Level 1",
    "Number of Sites", "Number of Profiles Updated/Terminated/Created",
    "How was this ticket resolved?", "Simplesat rating",
]


# ---------- Auth (F0: DB-backed permissions, multi-role) ----------
# Identity comes from Google OAuth → session cookie. Permissions come from
# src/db.py (roles + role_permissions + user_roles) and are resolved fresh
# on every request so role changes take effect immediately. See src/auth.py
# for the require()/has_perm helpers, and src/permissions.py for the catalog.
from .. import auth as auth_mod  # noqa: E402  (placed here intentionally so config + db are loaded first)

oauth = OAuth()
if config.AUTH_ENABLED:
    oauth.register(
        name="google",
        client_id=config.GOOGLE_CLIENT_ID,
        client_secret=config.GOOGLE_CLIENT_SECRET,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )

# Domain gate. Used by /auth/callback to reject non-corporate Google accounts
# without us having to maintain an explicit ALLOWED_EMAILS env var.
ALLOWED_DOMAINS = {"betterplace.co.in"}


def current_user(request: Request) -> dict | None:
    """Resolves identity + DB permissions; see src/auth.py."""
    return auth_mod.current_user(request)


def require_user(request: Request) -> dict:
    """Base dependency on every authenticated route. Pulls identity from the
    session, looks up the user + their effective permissions in the DB, and
    returns a dict with `permissions: set[str]` for downstream checks."""
    return auth_mod.require_user(request)


# Re-export under the names existing routes already use, so adding `Depends(require('...'))`
# elsewhere doesn't need a separate import everywhere.
require = auth_mod.require
require_any = auth_mod.require_any


def _redirect_uri_for(request: Request) -> str:
    """Build the OAuth callback URL from the incoming request, not from
    APP_PUBLIC_URL. This makes login work whether the user accessed the app
    via localhost (dev) or the public tunnel URL (engineers) — the session
    cookie set during /auth/login on host X will only be sent back to host X,
    so the callback URI MUST be on the same host.

    Respects x-forwarded-proto + x-forwarded-host so Cloudflare tunnel /
    reverse-proxy deployments report https correctly instead of the
    upstream http://127.0.0.1.
    """
    fwd_host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    fwd_proto = request.headers.get("x-forwarded-proto")
    if fwd_host and fwd_proto:
        return f"{fwd_proto}://{fwd_host}/auth/callback"
    # Fall back to whatever scheme/host Starlette parsed
    base = str(request.base_url).rstrip("/")
    return f"{base}/auth/callback"


@app.get("/auth/login")
async def login(request: Request):
    if not config.AUTH_ENABLED:
        return RedirectResponse("/")
    redirect_uri = _redirect_uri_for(request)
    # prompt=select_account → Google shows the account picker even if
    # the user is already signed into a Google account, so they can switch
    # accounts or at least see who they're logging in as. Without this,
    # SSO is silent and "sign out" appears to do nothing.
    return await oauth.google.authorize_redirect(
        request, redirect_uri, prompt="select_account"
    )


@app.get("/auth/callback")
async def auth_callback(request: Request):
    token = await oauth.google.authorize_access_token(request)
    info = token.get("userinfo") or {}
    email = (info.get("email") or "").lower()
    # Domain gate first — reject anything that's not a corporate account.
    domain = email.rsplit("@", 1)[-1] if "@" in email else ""
    if not email or domain not in ALLOWED_DOMAINS:
        return HTMLResponse(
            f"<h1>Access denied</h1>"
            f"<p>This app is restricted to @betterplace.co.in accounts. "
            f"You signed in as <code>{email or 'unknown'}</code>.</p>",
            status_code=403,
        )
    # Upsert app_user + auto-grant View-only on first login.
    resolved = auth_mod.ensure_user_on_login(
        email=email, name=info.get("name"), picture_url=info.get("picture")
    )
    if resolved.get("status") == "disabled":
        return HTMLResponse(
            f"<h1>Account disabled</h1>"
            f"<p>Your account ({email}) is currently disabled. Ask an admin to re-enable it.</p>",
            status_code=403,
        )
    # Session carries identity ONLY. Permissions are re-resolved per request
    # so admin changes take effect immediately.
    request.session["user"] = {
        "email": email,
        "name": resolved.get("name") or info.get("name") or email,
        "picture": info.get("picture"),
    }
    # Mint a session_id so all activity in this browser session correlates
    import secrets as _secrets
    request.session["session_id"] = _secrets.token_urlsafe(12)
    _activity.log(user_email=email, event_type="session", event_subtype="login",
                  detail={"new_user": resolved.get("status") == "active"
                                       and resolved.get("name") is None},
                  request=request)
    return RedirectResponse("/")


@app.post("/auth/logout")
@app.get("/auth/logout")
async def logout(request: Request):
    """Clear the session and land on /auth/signed-out — NOT on /, because /
    bounces to /auth/login which silently re-auths via Google SSO (the user
    is still logged into their Google account in the browser, so OAuth
    completes with no prompt → 'logout did nothing'). The signed-out page
    has an explicit Sign-in CTA so the user controls when re-auth happens."""
    user = request.session.get("user") or {}
    if user.get("email"):
        _activity.log(user_email=user["email"], event_type="session",
                      event_subtype="logout", request=request)
    request.session.clear()
    return RedirectResponse("/auth/signed-out", status_code=303)


@app.get("/auth/signed-out", response_class=HTMLResponse)
async def signed_out_page(request: Request):
    """Static landing page after logout. Does NOT trigger OAuth on view —
    user must explicitly click 'Sign in again' to start a fresh login."""
    signed_out_html = """
<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8"><title>Signed out · BetterPlace Co-Pilot</title>
<link rel="stylesheet" href="/static/styles.css">
</head>
<body style="background:linear-gradient(135deg,#1e1b4b,#312e81);min-height:100vh;display:flex;align-items:center;justify-content:center;font-family:Inter,system-ui,sans-serif;">
  <div style="max-width:420px;background:#fff;border-radius:16px;padding:36px 32px;box-shadow:0 20px 60px rgba(15,23,42,.5);text-align:center;">
    <div style="font-size:42px;margin-bottom:8px;">👋</div>
    <h1 style="margin:0 0 8px 0;font-size:22px;color:#1e293b;">You're signed out</h1>
    <p style="font-size:13px;color:#64748b;line-height:1.6;margin:0 0 24px;">
      Your BetterPlace Co-Pilot session has been cleared. Your Google
      account is still signed in elsewhere — click below to sign back in,
      or close this tab.
    </p>
    <a href="/auth/login" style="display:inline-block;background:linear-gradient(135deg,#4f46e5,#7c3aed);color:#fff;text-decoration:none;padding:11px 28px;border-radius:10px;font-weight:600;font-size:13px;">
      Sign in again
    </a>
    <div style="margin-top:18px;font-size:11px;color:#94a3b8;">
      To sign out of <strong>Google entirely</strong>, visit
      <a href="https://accounts.google.com/Logout" target="_blank" rel="noopener" style="color:#4f46e5;">accounts.google.com/Logout</a>
      — that clears your Google session across all tabs.
    </div>
  </div>
</body></html>"""
    return HTMLResponse(signed_out_html)


# ===========================================================================
# F5 · Profile page + status endpoints
# ===========================================================================
# /profile         — full profile editor (name, timezone, work days/hours, etc.)
# /api/profile     — POST: update editable fields
# /api/profile/me  — GET:  JSON of current user's profile (for avatar dropdown)
# /api/profile/availability — POST: quick status change

# Available timezones — keep it tight to the ones we actually use. Add more
# as engineers join from other regions.
TIMEZONE_CHOICES = [
    "Asia/Kolkata",
    "Asia/Dubai",
    "Asia/Singapore",
    "Asia/Tokyo",
    "Europe/London",
    "Europe/Berlin",
    "America/New_York",
    "America/Chicago",
    "America/Los_Angeles",
    "Australia/Sydney",
    "UTC",
]

WEEKDAY_ORDER = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


@app.get("/profile", response_class=HTMLResponse)
async def profile_page(request: Request, user: dict = Depends(require_user)):
    with db.conn() as c:
        sb = _sidebar_ctx(c, user)
        profile = db.get_user_profile(c, user["email"])
        group_ids = db.get_user_group_ids(c, user["email"])
        groups = db.list_groups(c, active_only=True)
        my_groups = [g for g in groups if g["id"] in group_ids]
        # Show ZD-mapped agent for context (resolves "Assigned to me")
        zd_user = None
        if profile and profile.get("zd_user_id"):
            zd_user = c.execute(
                "SELECT name, email FROM users WHERE id=?", (profile["zd_user_id"],)
            ).fetchone()
            if zd_user:
                zd_user = dict(zd_user)
    return TEMPLATES.TemplateResponse("profile.html", {
        "request": request, "user": user, "profile": profile or {},
        "my_groups": my_groups,
        "zd_user": zd_user,
        "timezone_choices": TIMEZONE_CHOICES,
        "weekday_order": WEEKDAY_ORDER,
        "availability_choices": list(db.VALID_AVAILABILITY),
        "current_view": "_profile", "in_detail": False, "search": "",
        **sb,
    })


# ===========================================================================
# F8 · Feedback widget + /admin/feedback inbox
# ===========================================================================

@app.post("/api/feedback")
async def submit_feedback_widget(
    request: Request,
    body: str = Form(...),
    kind: str = Form("bug"),
    severity: str = Form("normal"),
    title: str = Form(""),
    page_url: str = Form(""),
    ticket_id: str = Form(""),
    user: dict = Depends(require_user),
):
    """Any logged-in user can submit feedback. Lands in user_feedback table
    + pings every admin via the notification bell so it's seen fast."""
    body = (body or "").strip()
    if not body:
        raise HTTPException(400, "Feedback body required")
    tid: int | None = None
    if ticket_id.strip() and ticket_id.strip().lstrip("-").isdigit():
        tid = int(ticket_id.strip())
    with db.conn() as c:
        new_id = db.create_feedback(
            c, user_email=user["email"],
            body=body, kind=kind, severity=severity,
            title=title.strip() or None,
            page_url=page_url.strip() or None,
            ticket_id=tid,
            user_agent=request.headers.get("user-agent"),
        )
        # Ping every admin who can manage the inbox so they see it in the bell
        admins = c.execute("""
            SELECT DISTINCT ur.user_email FROM user_roles ur
            JOIN role_permissions rp ON rp.role_id = ur.role_id
            WHERE rp.permission_key IN ('admin.feedback', 'admin.users')
        """).fetchall()
        kind_emoji = {"bug": "🐛", "idea": "💡", "question": "❓", "praise": "✨"}.get(kind, "💬")
        sev_chip = f" [{severity}]" if severity in ("high", "urgent") else ""
        for a in admins:
            db.create_notification(
                c, user_email=a["user_email"],
                kind="warning" if severity in ("high", "urgent") else "info",
                title=f"{kind_emoji} New feedback from {user['email']}{sev_chip}",
                body=body[:200] + ("…" if len(body) > 200 else ""),
                action_url=f"/admin/feedback#item-{new_id}",
                action_label="Open inbox →",
                source=f"feedback:{new_id}",
            )
    _activity.log(user_email=user["email"], event_type="profile",
                  event_subtype="feedback_submitted",
                  detail={"kind": kind, "severity": severity,
                          "page_url": page_url, "ticket_id": tid},
                  request=request)
    return JSONResponse({"ok": True, "id": new_id})


@app.get("/admin/feedback", response_class=HTMLResponse)
async def admin_feedback(
    request: Request, status: str = "all",
    user: dict = Depends(auth_mod.require_any("admin.feedback", "admin.users")),
):
    with db.conn() as c:
        sb = _sidebar_ctx(c, user)
        items = db.list_feedback(c, status=status if status in ("new","triaged","closed") else None)
        counts = {
            "new": c.execute("SELECT COUNT(*) AS n FROM user_feedback WHERE status='new'").fetchone()["n"],
            "triaged": c.execute("SELECT COUNT(*) AS n FROM user_feedback WHERE status='triaged'").fetchone()["n"],
            "closed": c.execute("SELECT COUNT(*) AS n FROM user_feedback WHERE status='closed'").fetchone()["n"],
        }
    return TEMPLATES.TemplateResponse("admin/feedback.html", {
        "request": request, "user": user,
        "items": items, "counts": counts, "current_status": status,
        "current_view": "_admin", "in_detail": False, "search": "",
        **sb,
    })


@app.post("/api/admin/feedback/{fid}")
async def admin_feedback_update(
    fid: int,
    status: str = Form(""),
    reply: str = Form(""),
    user: dict = Depends(auth_mod.require_any("admin.feedback", "admin.users")),
):
    with db.conn() as c:
        db.update_feedback(c, feedback_id=fid,
                            status=status or None,
                            reply=reply or None,
                            actor_email=user["email"])
        # If a reply was added, notify the submitter
        if reply:
            row = c.execute(
                "SELECT user_email, kind FROM user_feedback WHERE id=?", (fid,)
            ).fetchone()
            if row:
                db.create_notification(
                    c, user_email=row["user_email"],
                    kind="info",
                    title=f"✓ Admin replied to your feedback",
                    body=reply[:200] + ("…" if len(reply) > 200 else ""),
                    action_url=f"/admin/feedback#item-{fid}",
                    action_label="See full reply →",
                    source=f"feedback_reply:{fid}",
                )
    return JSONResponse({"ok": True})


# ---- F6 · Notification bell endpoints ----

@app.get("/api/notifications/list")
async def notifications_list(
    include_dismissed: int = 0,
    user: dict = Depends(require_user),
):
    with db.conn() as c:
        rows = db.list_notifications(c, user_email=user["email"],
                                       include_dismissed=bool(include_dismissed),
                                       limit=50)
        unread = db.unread_notification_count(c, user["email"])
    return JSONResponse({"notifications": rows, "unread": unread})


@app.post("/api/notifications/{nid}/dismiss")
async def notifications_dismiss(
    nid: int,
    user: dict = Depends(require_user),
):
    with db.conn() as c:
        db.dismiss_notification(c, notification_id=nid, user_email=user["email"])
    return JSONResponse({"ok": True})


@app.post("/api/notifications/{nid}/read")
async def notifications_read(
    nid: int,
    user: dict = Depends(require_user),
):
    with db.conn() as c:
        db.mark_notification_read(c, notification_id=nid, user_email=user["email"])
    return JSONResponse({"ok": True})


@app.post("/api/notifications/dismiss-all")
async def notifications_dismiss_all(
    user: dict = Depends(require_user),
):
    with db.conn() as c:
        n = db.dismiss_all_notifications(c, user["email"])
    return JSONResponse({"ok": True, "dismissed": n})


@app.get("/api/profile/me")
async def profile_me_json(user: dict = Depends(require_user)):
    """Used by the avatar dropdown to show current status + quick links."""
    with db.conn() as c:
        p = db.get_user_profile(c, user["email"])
    if not p:
        return JSONResponse({"present": False})
    return JSONResponse({
        "present": True,
        "email": p["email"], "name": p.get("name"),
        "picture_url": p.get("picture_url"),
        "title": p.get("title"),
        "availability": p.get("availability") or "offline",
        "availability_emoji": p.get("availability_emoji"),
        "availability_label": p.get("availability_label"),
        "timezone": p.get("timezone"),
        "roles": [r["name"] for r in p.get("roles", [])],
    })


@app.post("/api/profile")
async def update_profile(
    name: str = Form(""),
    title: str = Form(""),
    timezone: str = Form(""),
    work_start_time: str = Form(""),
    work_end_time: str = Form(""),
    work_days: str = Form(""),         # comma-separated 'Mon,Tue,...'
    phone: str = Form(""),
    slack_handle: str = Form(""),
    bio: str = Form(""),
    notify_email: int = Form(1),
    notify_browser: int = Form(1),
    notify_sound: int = Form(0),
    user: dict = Depends(require_user),
):
    days = [d.strip() for d in work_days.split(",") if d.strip()]
    if timezone and timezone not in TIMEZONE_CHOICES:
        raise HTTPException(400, "Unsupported timezone")
    # Basic time format check ('HH:MM') — anything not matching falls through
    # to None so we don't break stored value.
    import re as _re
    def _time_or_none(s: str) -> str | None:
        return s if (s and _re.match(r"^\d{2}:\d{2}$", s)) else None
    with db.conn() as c:
        db.update_user_profile(
            c, email=user["email"],
            name=name or None,
            title=title or None,
            timezone=timezone or None,
            work_days=days or None,
            work_start_time=_time_or_none(work_start_time),
            work_end_time=_time_or_none(work_end_time),
            phone=phone or None,
            slack_handle=slack_handle or None,
            bio=bio or None,
            notify_email=int(notify_email),
            notify_browser=int(notify_browser),
            notify_sound=int(notify_sound),
        )
    _activity.log(user_email=user["email"], event_type="profile",
                  event_subtype="profile_update",
                  detail={"fields": [k for k, v in [
                      ("name", name), ("title", title), ("timezone", timezone),
                      ("work_days", work_days), ("phone", phone),
                  ] if v]})
    return JSONResponse({"ok": True})


@app.post("/api/profile/leave")
async def update_leave(
    request: Request,
    on_leave: int = Form(...),
    leave_start: str = Form(""),
    leave_end: str = Form(""),
    reason: str = Form(""),
    user: dict = Depends(require_user),
):
    """Set or clear leave mode. When on_leave=1, the auto-online + idle
    warning automations skip this user, so they're not nagged on holidays."""
    with db.conn() as c:
        db.set_user_leave(c, email=user["email"],
                           on_leave=int(on_leave),
                           leave_start=leave_start.strip() or None,
                           leave_end=leave_end.strip() or None,
                           reason=reason.strip() or None)
        # If going on leave while online, drop availability to offline so the
        # UI doesn't show "online" + leave-mode simultaneously (confusing).
        if int(on_leave):
            c.execute("UPDATE app_users SET availability='offline' WHERE email=?",
                      (user["email"],))
    _activity.log(user_email=user["email"], event_type="profile",
                  event_subtype="leave_set" if int(on_leave) else "leave_cleared",
                  detail={"start": leave_start or None,
                          "end": leave_end or None,
                          "reason": reason or None},
                  request=request)
    # Fire user automations
    try:
        from .. import user_rules_engine
        evt = "user.on_leave_set" if int(on_leave) else "user.on_leave_cleared"
        user_rules_engine.dispatch_event(evt, user["email"],
            context={"start": leave_start, "end": leave_end, "reason": reason})
    except Exception as e:
        print(f"[user_rules dispatch] {e}")
    return JSONResponse({"ok": True, "on_leave": bool(int(on_leave))})


@app.post("/api/profile/availability")
async def update_availability(
    request: Request,
    availability: str = Form(...),
    emoji: str = Form(""),
    label: str = Form(""),
    until: str = Form(""),
    user: dict = Depends(require_user),
):
    """Set status. Custom statuses pass emoji+label but ALWAYS map to one of
    online/away/busy/offline so any availability-aware logic still works."""
    try:
        with db.conn() as c:
            # Snapshot before for the activity log + user automations
            prev_row = c.execute(
                "SELECT availability FROM app_users WHERE email=?", (user["email"],)
            ).fetchone()
            prev = prev_row["availability"] if prev_row else None
            db.set_user_availability(c, email=user["email"],
                                      availability=availability,
                                      emoji=emoji.strip() or None,
                                      label=label.strip() or None,
                                      until=until.strip() or None)
            # Track last_online_at when going online — used by idle calc
            if availability == "online":
                c.execute("UPDATE app_users SET last_online_at=? WHERE email=?",
                          (db.now_iso(), user["email"]))
    except ValueError as e:
        raise HTTPException(400, str(e))
    _activity.log(user_email=user["email"], event_type="profile",
                  event_subtype="availability_change",
                  detail={"before": prev, "after": availability,
                          "emoji": emoji or None, "label": label or None},
                  request=request)
    # F6 · Fire user.availability_changed for user automations
    try:
        from .. import user_rules_engine
        user_rules_engine.dispatch_event(
            "user.availability_changed", user["email"],
            context={"before": prev, "after": availability,
                     "emoji": emoji or None, "label": label or None})
    except Exception as e:
        print(f"[user_rules dispatch] {e}")
    return JSONResponse({"ok": True, "availability": availability,
                          "emoji": emoji or None, "label": label or None})


# ---------- Static views ----------
STATIC_VIEWS = [
    {"key": "open",              "label": "Open",                    "color": "indigo"},
    {"key": "on_hold",           "label": "On hold · with dev",      "color": "violet"},
    {"key": "awaiting_customer", "label": "Awaiting customer",       "color": "amber"},
    {"key": "untouched",         "label": "Untouched · need pickup", "color": "rose"},
    {"key": "assigned_to_me",    "label": "Assigned to me",          "color": "violet"},
    {"key": "solved_24h",        "label": "Solved last 24h",         "color": "emerald"},
    {"key": "with_ai",           "label": "Has AI corrections",      "color": "violet"},
    {"key": "missing_kb",        "label": "Missing KB Article",      "color": "amber"},
    {"key": "sla_at_risk",       "label": "SLA at risk (8h+)",       "color": "amber"},
    {"key": "sla_breached",      "label": "SLA breached (24h+)",     "color": "rose"},
]


_NO_AGENT_REPLY_CLAUSE = (
    "NOT EXISTS (SELECT 1 FROM ticket_comments tc JOIN users u ON u.id=tc.author_id "
    "WHERE tc.ticket_id=tickets.id AND u.role IN ('agent','admin'))"
)


# Cache of view_id → (filter_obj, scope) so each request doesn't re-fetch.
# Invalidated on view CRUD via _invalidate_view_cache().
_NATIVE_VIEW_CACHE: dict[int, dict] = {}

# Module-level cache of group_name → group_id. Groups change rarely so this
# stays warm for the life of the process. Invalidated when groups change
# (create/rename/sync) via _invalidate_group_name_cache().
_GROUP_NAME_TO_IDS_CACHE: dict[str, list[int]] | None = None


def _invalidate_view_cache() -> None:
    _NATIVE_VIEW_CACHE.clear()


def _invalidate_group_name_cache() -> None:
    global _GROUP_NAME_TO_IDS_CACHE
    _GROUP_NAME_TO_IDS_CACHE = None


def _group_name_to_ids(c, names: list[str]) -> list[int]:
    """Lookup multiple group names → ids using the warm cache. The cache is
    built once from a single SELECT then reused until invalidated."""
    global _GROUP_NAME_TO_IDS_CACHE
    if _GROUP_NAME_TO_IDS_CACHE is None:
        rows = c.execute("SELECT id, name FROM groups").fetchall()
        cache: dict[str, list[int]] = {}
        for r in rows:
            cache.setdefault(r["name"], []).append(r["id"])
        _GROUP_NAME_TO_IDS_CACHE = cache
    out: list[int] = []
    for n in names:
        out.extend(_GROUP_NAME_TO_IDS_CACHE.get(n, []))
    return out


def _load_native_view(view_id: int) -> dict | None:
    if view_id in _NATIVE_VIEW_CACHE:
        return _NATIVE_VIEW_CACHE[view_id]
    with db.conn() as c:
        row = c.execute("SELECT * FROM native_views WHERE id=?", (view_id,)).fetchone()
        if not row:
            return None
        v = dict(row)
        try:
            v["_filter"] = json.loads(v.get("filter_json") or "{}")
        except json.JSONDecodeError:
            v["_filter"] = {}
    _NATIVE_VIEW_CACHE[view_id] = v
    return v


def _resolve_zd_user_id(user_email: str) -> int | None:
    """Get the ZD user_id mapped to this app_user. Used by 'is_me' operator
    on assignee_id. Returns None if unmapped — caller should fall back to
    a never-match clause so the view shows zero rows (rather than silently
    matching the wrong user)."""
    if not user_email:
        return None
    with db.conn() as c:
        row = c.execute("SELECT zd_user_id FROM app_users WHERE email=?",
                         (user_email,)).fetchone()
    return int(row["zd_user_id"]) if row and row["zd_user_id"] else None


def _view_sql_from_filter(filter_obj: dict, user_email: str = "",
                           *, conn=None, zd_user_id: int | None = None) -> tuple[str, list]:
    """Render a native_views.filter_json into a SQL WHERE clause + params.

    Filter shape: {match: 'all'|'any', rules: [{field, op, value}]}

    PERF: pass `conn` (already-open DB connection) and `zd_user_id` (already
    resolved for this user) to avoid opening fresh connections inside the
    loop. With 7 default views all using group_name, the old behavior was
    7+ extra db.conn() opens per sidebar render (~50-200ms wasted). With the
    new path, group lookups use a process-wide cache and zd_user_id comes
    pre-resolved from the user dict.
    """
    rules = (filter_obj or {}).get("rules", []) or []
    match_mode = (filter_obj or {}).get("match", "all").lower()
    if not rules:
        return "1=1", []
    where_parts: list[str] = []
    params: list = []

    # Resolve zd_user_id once if 'is_me' is anywhere in the rules. Falls back
    # to a fresh lookup only when neither conn nor a pre-resolved id is passed.
    resolved_zd_id = zd_user_id
    need_zd_id = any(r.get("field") == "assignee_id" and r.get("op") == "is_me"
                      for r in rules)
    if need_zd_id and resolved_zd_id is None and user_email:
        resolved_zd_id = _resolve_zd_user_id(user_email)

    def _ident(name: str) -> str | None:
        """Map filter-field-name to actual SQL column expression."""
        if name in ("status", "priority", "type",
                    "assignee_id", "requester_id", "group_id",
                    "created_at", "updated_at", "solved_at",
                    "subject", "tags"):
            return name
        return None

    for r in rules:
        field = (r.get("field") or "").strip()
        op = (r.get("op") or "eq").strip()
        value = r.get("value")

        # --- group_name → group_id translation (cached at module level)
        if field == "group_name":
            if op in ("eq", "in"):
                names = value if isinstance(value, list) else [value]
                if not names:
                    where_parts.append("1=0")
                    continue
                # Use the existing open connection if provided, else open one
                # ONCE per call (still better than per-rule).
                if conn is not None:
                    ids = _group_name_to_ids(conn, names)
                else:
                    with db.conn() as _c:
                        ids = _group_name_to_ids(_c, names)
                if not ids:
                    where_parts.append("1=0")
                else:
                    where_parts.append(f"group_id IN ({','.join('?' * len(ids))})")
                    params.extend(ids)
            continue

        # --- assignee_id is_me → uses logged-in user's zd_user_id
        if field == "assignee_id" and op == "is_me":
            if resolved_zd_id is None:
                where_parts.append("1=0")  # unmapped → no results, not all results
            else:
                where_parts.append("assignee_id = ?")
                params.append(resolved_zd_id)
            continue

        # --- custom field: cf.<id>
        if field.startswith("cf."):
            cf_id = field[3:]
            col = f"json_extract(custom_fields,'$.\"{cf_id}\"')"
            if op == "eq":
                where_parts.append(f"{col} = ?"); params.append(value)
            elif op == "in":
                vals = value if isinstance(value, list) else [value]
                if vals:
                    where_parts.append(f"{col} IN ({','.join('?' * len(vals))})")
                    params.extend(vals)
            elif op == "is_null":
                where_parts.append(f"({col} IS NULL OR {col} = '')")
            elif op == "not_null":
                where_parts.append(f"{col} IS NOT NULL AND {col} != ''")
            continue

        # --- regular column
        ident = _ident(field)
        if not ident:
            continue
        if op == "eq":
            where_parts.append(f"{ident} = ?"); params.append(value)
        elif op == "ne":
            where_parts.append(f"({ident} != ? OR {ident} IS NULL)"); params.append(value)
        elif op == "in":
            vals = value if isinstance(value, list) else [value]
            if not vals:
                where_parts.append("1=0")
                continue
            where_parts.append(f"{ident} IN ({','.join('?' * len(vals))})")
            params.extend(vals)
        elif op == "not_in":
            vals = value if isinstance(value, list) else [value]
            if not vals: continue
            where_parts.append(f"({ident} NOT IN ({','.join('?' * len(vals))}) OR {ident} IS NULL)")
            params.extend(vals)
        elif op == "is_null":
            where_parts.append(f"{ident} IS NULL")
        elif op == "not_null":
            where_parts.append(f"{ident} IS NOT NULL")
        elif op == "within_days":
            try:
                days = int(value)
                where_parts.append(f"{ident} > datetime('now', '-{days} days')")
            except (TypeError, ValueError):
                pass
        elif op == "older_than_days":
            try:
                days = int(value)
                where_parts.append(f"{ident} < datetime('now', '-{days} days')")
            except (TypeError, ValueError):
                pass

    if not where_parts:
        return "1=1", []
    joiner = " AND " if match_mode == "all" else " OR "
    return "(" + joiner.join(where_parts) + ")", params


def _view_sql(view: str, user_email: str = "") -> tuple[str, list]:
    # Native views: keys like 'nv_1', 'nv_42'
    if view.startswith("nv_"):
        try:
            view_id = int(view[3:])
        except ValueError:
            return "1=0", []
        nv = _load_native_view(view_id)
        if not nv or not nv.get("active"):
            return "1=0", []
        return _view_sql_from_filter(nv["_filter"], user_email)
    # New split views
    if view == "open":
        return "status IN ('new','open')", []
    if view == "on_hold":
        return "status='hold'", []
    # Backward compat — old URLs still work
    if view == "all_open":
        return "status IN ('new','open','pending','hold')", []
    if view == "with_engineering":
        return ("status='hold' AND ("
                f"json_extract(custom_fields,'$.\"{FID_JIRA_ID}\"') IS NOT NULL "
                f"AND json_extract(custom_fields,'$.\"{FID_JIRA_ID}\"') != ''"
                ")"), []
    if view == "untouched":
        return (f"status IN ('new','open') AND assignee_id IS NULL AND {_NO_AGENT_REPLY_CLAUSE}"), []
    if view == "assigned_to_me":
        return "assignee_id IN (SELECT id FROM users WHERE LOWER(email)=?)", [user_email.lower()]
    if view == "awaiting_customer":
        return "status='pending'", []
    if view == "solved_24h":
        return "status='solved' AND solved_at > datetime('now','-1 day')", []
    if view == "with_ai":
        return ("id IN (SELECT ticket_id FROM ticket_insights WHERE recommendations != '[]') "
                "AND status IN ('new','open','pending','hold')"), []
    if view == "missing_kb":
        return ("status IN ('new','open','pending','hold','solved') AND ("
                f"json_extract(custom_fields,'$.\"{FID_KB_ARTICLE}\"') IS NULL "
                f"OR json_extract(custom_fields,'$.\"{FID_KB_ARTICLE}\"') IN ('', 'NA', 'na'))"), []
    if view == "sla_at_risk":
        return (f"status IN ('new','open') AND created_at < datetime('now','-8 hours') AND {_NO_AGENT_REPLY_CLAUSE}"), []
    if view == "sla_breached":
        return (f"status IN ('new','open') AND created_at < datetime('now','-24 hours') AND {_NO_AGENT_REPLY_CLAUSE}"), []
    # Customer-prefixed views: cust_<value>:open / cust_<value>:eng / cust_<value>:pending / cust_<value>:unassigned
    if view.startswith("cust_"):
        body = view[5:]
        if ":" not in body:
            return "1=0", []
        cust_value, sub = body.rsplit(":", 1)
        cust_clause = f"json_extract(custom_fields,'$.\"{FID_CUSTOMER}\"') = ?"
        if sub == "open":
            return f"({cust_clause}) AND status IN ('new','open')", [cust_value]
        if sub == "eng":
            return (f"({cust_clause}) AND status='hold' AND "
                    f"json_extract(custom_fields,'$.\"{FID_JIRA_ID}\"') IS NOT NULL AND "
                    f"json_extract(custom_fields,'$.\"{FID_JIRA_ID}\"') != ''"), [cust_value]
        if sub == "pending":
            return f"({cust_clause}) AND status='pending'", [cust_value]
        if sub == "unassigned":
            return f"({cust_clause}) AND status IN ('new','open') AND assignee_id IS NULL", [cust_value]
    return "1=1", []


def _top_customers(c: sqlite3.Connection, limit: int = 10) -> list[dict]:
    """Top customers by open ticket count. Returns [{value, label, count}]."""
    rows = c.execute(f"""
        SELECT json_extract(custom_fields, '$."{FID_CUSTOMER}"') AS cust_value, COUNT(*) AS n
        FROM tickets
        WHERE status IN ('new','open','pending','hold')
          AND json_extract(custom_fields, '$."{FID_CUSTOMER}"') IS NOT NULL
          AND json_extract(custom_fields, '$."{FID_CUSTOMER}"') != ''
        GROUP BY cust_value
        ORDER BY n DESC
        LIMIT ?
    """, (limit,)).fetchall()
    # Resolve display names
    field = c.execute("SELECT options FROM ticket_fields WHERE id=?", (int(FID_CUSTOMER),)).fetchone()
    name_by_value = {}
    if field:
        for o in json.loads(field["options"] or "[]"):
            name_by_value[o.get("value")] = o.get("name") or o.get("value")
    out = []
    for r in rows:
        v = r["cust_value"]
        out.append({"value": v, "label": name_by_value.get(v, v), "count": r["n"]})
    return out


def _customer_view_counts(c: sqlite3.Connection, cust_value: str) -> dict:
    """Return counts for the 4 sub-views of a customer."""
    out = {}
    for sub in ("open", "eng", "pending", "unassigned"):
        where, params = _view_sql(f"cust_{cust_value}:{sub}")
        row = c.execute(f"SELECT COUNT(*) AS n FROM tickets WHERE {where}", params).fetchone()
        out[sub] = row["n"] if row else 0
    return out


def _view_counts(c: sqlite3.Connection, user_email: str) -> dict[str, int]:
    out = {}
    for v in STATIC_VIEWS:
        where, params = _view_sql(v["key"], user_email)
        row = c.execute(f"SELECT COUNT(*) AS n FROM tickets WHERE {where}", params).fetchone()
        out[v["key"]] = row["n"] if row else 0
    return out


# ---------- Helpers ----------
def _customer_name(custom_fields: dict, field_rows: dict) -> str:
    cn = custom_fields.get(FID_CUSTOMER)
    if not cn:
        return ""
    f = field_rows.get(int(FID_CUSTOMER))
    if f:
        for o in json.loads(f["options"] or "[]"):
            if o.get("value") == cn:
                return o.get("name") or cn
    return cn or ""


def _hours_since(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return round((datetime.now(timezone.utc) - dt).total_seconds() / 3600, 1)
    except Exception:
        return None


def _sla_status(c: sqlite3.Connection, t: sqlite3.Row, *, recompute: bool = False) -> dict:
    """Per-ticket SLA chip. If an SLA policy matches, use the policy clocks
    (first_reply / next_reply / resolution) and surface the worst breaching
    one. Otherwise fall back to the legacy "hours since created" heuristic so
    tickets without a policy still show something.

    recompute=False reads the persisted snapshot (cheap, used in list views).
    recompute=True recomputes fresh (used on the detail page)."""
    snap = None
    if recompute:
        try:
            from .. import sla as _sla
            snap = _sla.compute_ticket_sla(c, t["id"])
        except Exception:
            snap = None
    else:
        try:
            row = c.execute(
                "SELECT * FROM ticket_sla WHERE ticket_id=?", (t["id"],)
            ).fetchone()
            if row:
                snap = dict(row)
        except Exception:
            snap = None
    if snap:
        worst_level = "ok"
        worst_label = ""
        for clock_name, lbl in (
            ("first_reply_state", "First reply"),
            ("next_reply_state",  "Next reply"),
            ("resolution_state",  "Resolution"),
        ):
            state = snap.get(clock_name)
            elapsed_key = clock_name.replace("_state", "_elapsed_minutes")
            target_key  = clock_name.replace("_state", "_target_minutes")
            elapsed = snap.get(elapsed_key)
            target  = snap.get(target_key)
            if state is None:
                continue
            if state == "breached" and worst_level != "breached":
                worst_level = "breached"; worst_label = f"{lbl} breached · {elapsed}/{target}m"
            elif state == "warn" and worst_level not in ("breached",):
                worst_level = "at_risk"; worst_label = f"{lbl} at risk · {elapsed}/{target}m"
            elif worst_level == "ok" and state == "met":
                worst_label = f"{lbl} met"
            elif worst_level == "ok" and state == "ok":
                worst_label = f"{lbl} ok · {elapsed}/{target}m"
        if worst_label:
            return {"label": worst_label, "level": worst_level}

    # Legacy fallback.
    age = _hours_since(t["created_at"])
    if age is None:
        return {"label": "—", "level": "ok"}
    if t["status"] not in ("new", "open", "pending", "hold"):
        return {"label": "Closed", "level": "ok"}
    has_agent_reply = c.execute("""
        SELECT 1 FROM ticket_comments tc JOIN users u ON u.id=tc.author_id
        WHERE tc.ticket_id=? AND u.role IN ('agent','admin') LIMIT 1
    """, (t["id"],)).fetchone() is not None
    if not has_agent_reply:
        if age >= 24:
            return {"label": f"Breached · {int(age)}h", "level": "breached"}
        if age >= 8:
            return {"label": f"At risk · {int(age)}h", "level": "at_risk"}
        return {"label": f"Open · {int(age)}h", "level": "ok"}
    return {"label": f"Active · {int(age)}h", "level": "ok"}


def _ticket_summary_row(c: sqlite3.Connection, t: sqlite3.Row, field_rows: dict) -> dict:
    # Use effective (override-merged) fields so locally-edited values show in lists
    cfs = db.effective_custom_fields(t)
    cust = _customer_name(cfs, field_rows)
    grp = c.execute("SELECT name FROM groups WHERE id=?", (t["group_id"],)).fetchone() if t["group_id"] else None
    is_ms = grp and "managed" in (grp["name"] or "").lower()
    pickup = False
    if t["status"] in ("new", "open") and not t["assignee_id"]:
        cmt_count = c.execute("""
            SELECT COUNT(*) AS n FROM ticket_comments tc
            LEFT JOIN users u ON u.id = tc.author_id
            WHERE tc.ticket_id = ? AND COALESCE(u.role,'') IN ('agent','admin')
        """, (t["id"],)).fetchone()
        if cmt_count and cmt_count["n"] == 0:
            pickup = True
    has_ai = c.execute("SELECT 1 FROM ticket_insights WHERE ticket_id=? LIMIT 1", (t["id"],)).fetchone() is not None
    ai_count = 0
    ai_row = c.execute("SELECT recommendations FROM ticket_insights WHERE ticket_id=? ORDER BY id DESC LIMIT 1", (t["id"],)).fetchone()
    if ai_row:
        try:
            ai_count = len(json.loads(ai_row["recommendations"] or "[]"))
        except Exception:
            ai_count = 0
    sla = _sla_status(c, t)
    jira_id = cfs.get(FID_JIRA_ID) or ""

    # Custom status label (use the agent-facing name from custom_statuses if present)
    custom_status_label = None
    try:
        raw = json.loads(t["raw"] or "{}")
        cs_id = raw.get("custom_status_id")
        if cs_id:
            cs = c.execute("SELECT agent_label FROM custom_statuses WHERE id=?", (cs_id,)).fetchone()
            if cs:
                custom_status_label = cs["agent_label"]
    except Exception:
        pass

    source = t["source"] if "source" in t.keys() and t["source"] else "zendesk"
    local_id = t["local_id"] if "local_id" in t.keys() else None
    display_id = local_id if (source == "native" and local_id) else f"#{t['id']}"

    # F0+ T4: surface enough extra fields to render any column the user picks
    # in the view editor. Resolved lazily — small extra cost only if asked.
    assignee_name = None
    if t["assignee_id"]:
        a = c.execute("SELECT name FROM users WHERE id=?", (t["assignee_id"],)).fetchone()
        assignee_name = a["name"] if a else None
    requester_name = None
    if t["requester_id"]:
        r = c.execute("SELECT name FROM users WHERE id=?", (t["requester_id"],)).fetchone()
        requester_name = r["name"] if r else None
    group_name = grp["name"] if grp else None
    solved_at = t["solved_at"] if "solved_at" in t.keys() else None
    ttype = t["type"] if "type" in t.keys() else None
    tags = ""
    try:
        raw = json.loads(t["raw"] or "{}")
        tg = raw.get("tags") or []
        if isinstance(tg, list):
            tags = ", ".join(tg)
    except Exception:
        pass

    return {
        "id": t["id"],
        "local_id": local_id,
        "source": source,
        "display_id": display_id,
        "subject": t["subject"] or "(no subject)",
        "status": t["status"],
        "type": ttype,
        "custom_status_label": custom_status_label,
        "priority": t["priority"] or "—",
        "group": "MS" if is_ms else "PS",        # short chip code (legacy)
        "group_name": group_name,                # full name for custom views
        "customer": cust or "—",
        "created_at": (t["created_at"] or "").replace("T", " ").replace("Z", "")[:16],
        "updated_at": (t["updated_at"] or "").replace("T", " ").replace("Z", "")[:16],
        "solved_at":  (solved_at or "").replace("T", " ").replace("Z", "")[:16] if solved_at else None,
        "pickup": pickup,
        "has_ai": has_ai,
        "ai_count": ai_count,
        "assignee_id": t["assignee_id"],
        "assignee_name": assignee_name,
        "requester_id": t["requester_id"],
        "requester_name": requester_name,
        "sla_label": sla["label"],
        "sla_level": sla["level"],
        "sla_status": sla["label"],
        "jira_id": jira_id,
        "tags": tags,
        # All effective custom field values (for cf.<id> column rendering).
        # Keyed as 'cf_<id>' so we can do {{ t['cf_15315331275025'] }} in Jinja.
        # We resolve option labels for taggers/multiselects via field_rows.
        **_cf_for_display(cfs, field_rows),
    }


def _cf_for_display(cfs: dict, field_rows) -> dict:
    """Convert effective custom_fields {id_str: raw_value} into display
    strings keyed as cf_<id>. For tagger/multiselect, resolves the
    option value → name. For other types, returns the value as-is.

    field_rows can be either:
      - dict {id → Row} (the shape from _list_view_tickets)
      - list of Row (the shape some other callers use)
    """
    out: dict = {}
    options_by_field_id: dict = {}
    rows_iter = field_rows.values() if isinstance(field_rows, dict) else field_rows
    for f in rows_iter:
        # f is a sqlite3.Row, not subscriptable as f["x"] in older Python without row_factory.
        try:
            ftype = f["type"]
        except (TypeError, KeyError):
            continue
        if ftype in ("tagger", "multiselect", "partialcredit"):
            try:
                opts = json.loads(f["options"] or "[]")
            except (json.JSONDecodeError, TypeError):
                opts = []
            options_by_field_id[str(f["id"])] = {
                o.get("value"): o.get("name") or o.get("value") for o in opts
            }
    for fid, value in (cfs or {}).items():
        key = f"cf_{fid}"
        if value in (None, "", []):
            out[key] = "—"
            continue
        labels = options_by_field_id.get(str(fid))
        if labels:
            if isinstance(value, list):
                out[key] = ", ".join(labels.get(v, v) for v in value)
            else:
                out[key] = labels.get(value, value)
        else:
            out[key] = ", ".join(value) if isinstance(value, list) else value
    return out


SORT_KEYS = {
    "updated_desc": "updated_at DESC",
    "updated_asc": "updated_at ASC",
    "created_desc": "created_at DESC",
    "created_asc": "created_at ASC",
    "customer": f"json_extract(custom_fields, '$.\"{FID_CUSTOMER}\"') ASC, updated_at DESC",
}


# ===========================================================================
# F0+ · Column registry for dynamic view rendering
# ===========================================================================
# Maps a column key → metadata used by view_list.html to render the cell.
# `kind` tells the template which Jinja branch to use:
#   id      — ticket id badge + PICKUP tag
#   subject — clickable subject text
#   status  — status badge (b-<status> class)
#   priority/type/customer/group_name/assignee_name/requester_name — plain text
#   group   — short PS/MS chip (legacy)
#   date    — IST-formatted date
#   sla     — SLA chip
#   jira    — Jira ID chip
#   ai      — AI count badge
#   tags    — comma-separated tags
#   raw     — generic text fallback (used for custom fields)

STANDARD_COLUMN_DEFS: dict[str, dict] = {
    "id":             {"label": "ID",          "kind": "id",        "th": "th-id",       "td": "td-id"},
    "subject":        {"label": "Subject",     "kind": "subject",   "th": "th-subject",  "td": "td-subject"},
    "status":         {"label": "Status",      "kind": "status",    "th": "th-status",   "td": ""},
    "priority":       {"label": "Priority",    "kind": "text",      "th": "th-priority", "td": "td-priority"},
    "type":           {"label": "Type",        "kind": "text",      "th": "",            "td": ""},
    "group_name":     {"label": "Group",       "kind": "group",     "th": "th-grp",      "td": ""},
    "assignee_name":  {"label": "Assignee",    "kind": "text",      "th": "",            "td": ""},
    "requester_name": {"label": "Requester",   "kind": "text",      "th": "",            "td": ""},
    "customer":       {"label": "Customer",    "kind": "text",      "th": "th-customer", "td": "td-customer"},
    "created_at":     {"label": "Created",     "kind": "date",      "th": "th-date",     "td": "td-date"},
    "updated_at":     {"label": "Updated",     "kind": "date",      "th": "th-date",     "td": "td-date"},
    "solved_at":      {"label": "Solved at",   "kind": "date",      "th": "th-date",     "td": "td-date"},
    "sla_status":     {"label": "SLA",         "kind": "sla",       "th": "th-sla",      "td": "td-sla"},
    "tags":           {"label": "Tags",        "kind": "text",      "th": "",            "td": ""},
    "jira_id":        {"label": "Jira",        "kind": "jira",      "th": "th-jira",     "td": "td-jira"},
    "ai":             {"label": "AI",          "kind": "ai",        "th": "th-ai",       "td": "td-ai"},
}

# Default column set when a view doesn't specify one (and for legacy non-native views).
DEFAULT_VIEW_COLUMNS = [
    "id", "status", "customer", "subject", "jira_id", "priority",
    "group_name", "created_at", "updated_at", "sla_status", "ai",
]


def _resolve_view_columns(c, view_name: str) -> list[dict]:
    """Return the ordered list of columns to render for this view.
    Each entry: {key, label, kind, th, td, is_custom_field}.

    For native views (nv_<id>), reads column_ids_json. For legacy views
    (open / on_hold / cust_X / etc.) returns the default set."""
    column_keys: list[str] = []
    if view_name.startswith("nv_"):
        try:
            vid = int(view_name[3:])
            row = c.execute(
                "SELECT column_ids_json FROM native_views WHERE id=?", (vid,)
            ).fetchone()
            if row:
                try:
                    column_keys = json.loads(row["column_ids_json"] or "[]")
                except json.JSONDecodeError:
                    column_keys = []
        except ValueError:
            pass
    if not column_keys:
        column_keys = list(DEFAULT_VIEW_COLUMNS)
    # Build the resolved list. Custom-field columns (cf.<id>) get a label
    # lookup from ticket_fields.
    resolved: list[dict] = []
    cf_id_to_title: dict = {}
    cf_ids_needed = [k[3:] for k in column_keys if k.startswith("cf.")]
    if cf_ids_needed:
        placeholders = ",".join("?" * len(cf_ids_needed))
        for r in c.execute(
            f"SELECT id, title FROM ticket_fields WHERE id IN ({placeholders})",
            cf_ids_needed
        ).fetchall():
            cf_id_to_title[str(r["id"])] = r["title"]
    for k in column_keys:
        if k.startswith("cf."):
            cf_id = k[3:]
            resolved.append({
                "key": f"cf_{cf_id}",
                "label": cf_id_to_title.get(cf_id, f"Field #{cf_id}"),
                "kind": "raw",
                "th": "", "td": "",
                "is_custom_field": True,
            })
        elif k in STANDARD_COLUMN_DEFS:
            d = STANDARD_COLUMN_DEFS[k]
            resolved.append({"key": k, **d, "is_custom_field": False})
        # Unknown keys are skipped silently rather than crashing the page
    if not resolved:
        # Belt-and-braces: if everything got dropped, fall back to default
        for k in DEFAULT_VIEW_COLUMNS:
            d = STANDARD_COLUMN_DEFS[k]
            resolved.append({"key": k, **d, "is_custom_field": False})
    return resolved


def _search_tickets(c: sqlite3.Connection, *,
                     q: str = "",
                     status: list[str] | None = None,
                     group_ids: list[int] | None = None,
                     priority: list[str] | None = None,
                     customer: str = "",
                     has_ai: str = "",
                     date_field: str = "updated_at",
                     date_within_days: int = 0,
                     include_comments: int = 0,
                     sort: str = "updated_desc",
                     limit: int = 100,
                     offset: int = 0) -> tuple[list[dict], int, bool]:
    """Universal ticket search. Returns (rows, total_count_or_limit, has_more).

    PERF strategy (60s → <100ms for typical queries):
      1. Pure-digit query → exact PK lookup, no OR clauses, no COUNT.
      2. BP-prefix query → exact local_id lookup (indexed).
      3. Multi-field text path uses a tight OR set; the COUNT(*) query is
         SKIPPED — we fetch limit+1 rows and report "N+ matches" if we hit
         the cap. The exact total wasn't useful enough to justify a second
         full-table scan.
      4. The 4 expression indexes on hot custom fields (added in F0+) make
         the json_extract LIKEs only cost as much as the OR set wide.
    """
    qs = (q or "").strip()
    has_filters = bool(status or group_ids or priority or customer or has_ai or date_within_days)

    field_rows = {r["id"]: r for r in c.execute("SELECT * FROM ticket_fields").fetchall()}

    # ---- FAST PATH 1: pure digit query, no filters → exact PK hit (<1ms)
    if qs.isdigit() and not has_filters:
        row = c.execute("SELECT * FROM tickets WHERE id = ?", (int(qs),)).fetchone()
        if row:
            return [_ticket_summary_row(c, row, field_rows)], 1, False
        return [], 0, False

    # ---- FAST PATH 2: BP-XXXXXX exact match (indexed via idx_tickets_local_id)
    if qs.upper().startswith("BP-") and not has_filters:
        row = c.execute("SELECT * FROM tickets WHERE local_id = ?",
                         (qs.upper(),)).fetchone()
        if row:
            return [_ticket_summary_row(c, row, field_rows)], 1, False
        return [], 0, False

    # ---- General path
    where_parts: list[str] = ["1=1"]
    params: list = []

    if qs:
        like = f"%{qs}%"
        text_clauses: list[str] = []
        if qs.isdigit():
            text_clauses.append("tickets.id = ?")
            params.append(int(qs))
        # Subject is the highest-signal text field — keep first for OR short-circuit
        text_clauses.append("tickets.subject LIKE ?"); params.append(like)
        text_clauses.append("tickets.local_id LIKE ?"); params.append(like)
        text_clauses.append("tickets.tags LIKE ?"); params.append(like)
        # Requester (single subquery — sqlite handles the IN efficiently)
        text_clauses.append("""tickets.requester_id IN
            (SELECT id FROM users WHERE name LIKE ? OR email LIKE ?)""")
        params.append(like); params.append(like)
        # Hot custom fields (we have json_extract expression indexes on these).
        # Trimmed to the most-searched 2 — adding more OR clauses costs a
        # full-table scan each; users can use the customer filter dropdown
        # for the rest.
        for cf_id in (FID_CUSTOMER, FID_JIRA_ID):
            text_clauses.append(
                f"json_extract(tickets.custom_fields, '$.\"{cf_id}\"') LIKE ?"
            )
            params.append(like)
        if include_comments:
            text_clauses.append("""EXISTS (SELECT 1 FROM ticket_comments tc
                WHERE tc.ticket_id = tickets.id AND tc.body LIKE ?)""")
            params.append(like)
        where_parts.append("(" + " OR ".join(text_clauses) + ")")

    if status:
        placeholders = ",".join("?" * len(status))
        where_parts.append(f"tickets.status IN ({placeholders})")
        params.extend(status)
    if priority:
        placeholders = ",".join("?" * len(priority))
        where_parts.append(f"tickets.priority IN ({placeholders})")
        params.extend(priority)
    if group_ids:
        placeholders = ",".join("?" * len(group_ids))
        where_parts.append(f"tickets.group_id IN ({placeholders})")
        params.extend(group_ids)
    if customer:
        where_parts.append(
            f"json_extract(tickets.custom_fields, '$.\"{FID_CUSTOMER}\"') = ?"
        )
        params.append(customer)
    if has_ai == "yes":
        where_parts.append("tickets.id IN (SELECT ticket_id FROM ticket_insights)")
    elif has_ai == "no":
        where_parts.append("tickets.id NOT IN (SELECT ticket_id FROM ticket_insights)")
    if date_within_days and date_field in ("created_at", "updated_at", "solved_at"):
        where_parts.append(
            f"tickets.{date_field} > datetime('now', '-{int(date_within_days)} days')"
        )

    where_sql = " AND ".join(where_parts)
    order = SORT_KEYS.get(sort, SORT_KEYS["updated_desc"])

    # PERF: fetch (limit+1) so we know whether there's a next page without
    # running a separate COUNT(*). The "+1" extra row is dropped before return.
    fetch_n = int(limit) + 1
    sql = (f"SELECT * FROM tickets WHERE {where_sql} "
           f"ORDER BY {order} LIMIT ? OFFSET ?")
    rows = c.execute(sql, params + [fetch_n, int(offset)]).fetchall()
    has_more = len(rows) > int(limit)
    rows = rows[:int(limit)]
    summaries = [_ticket_summary_row(c, r, field_rows) for r in rows]
    # "Total" we report is exactly what's been fetched on this page; the
    # has_more flag signals there are more results without a separate count.
    return summaries, len(summaries) + int(offset), has_more


def _list_view_tickets(c: sqlite3.Connection, view: str, user_email: str,
                       *, search: str = "", sort: str = "updated_desc",
                       filter_customer: str = "", filter_jira: str = "",
                       filter_rc1: str = "", filter_group: str = "",
                       limit: int = 200) -> list[dict]:
    where, params = _view_sql(view, user_email)
    sql = f"SELECT * FROM tickets WHERE {where}"
    params = list(params)
    if search:
        sql += " AND (CAST(id AS TEXT) LIKE ? OR subject LIKE ?)"
        like = f"%{search}%"
        params += [like, like]
    if filter_customer:
        sql += f" AND json_extract(custom_fields, '$.\"{FID_CUSTOMER}\"') = ?"
        params.append(filter_customer)
    if filter_jira == "present":
        sql += f" AND json_extract(custom_fields, '$.\"{FID_JIRA_ID}\"') IS NOT NULL AND json_extract(custom_fields, '$.\"{FID_JIRA_ID}\"') != ''"
    elif filter_jira == "absent":
        sql += f" AND (json_extract(custom_fields, '$.\"{FID_JIRA_ID}\"') IS NULL OR json_extract(custom_fields, '$.\"{FID_JIRA_ID}\"') = '')"
    if filter_rc1:
        sql += f" AND json_extract(custom_fields, '$.\"{FID_RC1}\"') = ?"
        params.append(filter_rc1)
    if filter_group:
        sql += " AND group_id IN (SELECT id FROM groups WHERE LOWER(name) LIKE ?)"
        params.append(f"%{filter_group.lower()}%")
    order = SORT_KEYS.get(sort, SORT_KEYS["updated_desc"])
    sql += f" ORDER BY {order} LIMIT {int(limit)}"
    rows = c.execute(sql, params).fetchall()
    field_rows = {r["id"]: r for r in c.execute("SELECT * FROM ticket_fields").fetchall()}
    return [_ticket_summary_row(c, r, field_rows) for r in rows]


# Zendesk system field types we never want to show in the custom-fields panel —
# subject / description / priority / status etc. are already rendered in the header.
# These are the field "type" values used by ZD's `/api/v2/ticket_fields` endpoint.
SYSTEM_FIELD_TYPES = {
    "subject", "description", "status", "tickettype",
    "priority", "group", "assignee", "custom_status",
}

# "Important" custom fields — shown right after the mandatory ones, before the
# long tail of optional taxonomy. Order matters: higher index = more important.
IMPORTANT_FIELD_IDS = {
    int(FID_CUSTOMER), int(FID_PRODUCT), int(FID_MODULE),
    int(FID_RC1), int(FID_RC2), int(FID_JIRA_ID), int(FID_KB_ARTICLE),
}


def _form_field_titles(c: sqlite3.Connection, ticket_form_id: int | None, group_name: str) -> list[str]:
    """Return ordered list of field titles to show for this ticket. System fields
    (subject/description/priority/group/assignee/status/custom_status) are
    filtered out — they live in the ticket header, not the custom-fields panel."""
    if ticket_form_id:
        form = c.execute("SELECT field_ids FROM ticket_forms WHERE id=?", (ticket_form_id,)).fetchone()
        if form:
            field_ids = json.loads(form["field_ids"] or "[]")
            titles = []
            for fid in field_ids:
                row = c.execute("SELECT title, type FROM ticket_fields WHERE id=?", (fid,)).fetchone()
                if not row or not row["title"]:
                    continue
                if (row["type"] or "").lower() in SYSTEM_FIELD_TYPES:
                    continue
                titles.append(row["title"])
            if titles:
                return titles
    is_ms = "managed" in (group_name or "").lower()
    base = MANAGED_SERVICES_FIELDS if is_ms else PRODUCT_SUPPORT_FIELDS
    # Even for the fallback lists, drop "Priority" — it's a system field, not custom.
    return [t for t in base if t.lower() != "priority"]


def _full_ticket(c: sqlite3.Connection, ticket_id: int) -> dict | None:
    t = c.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()
    if not t:
        return None
    requester = c.execute("SELECT name, email FROM users WHERE id=?", (t["requester_id"],)).fetchone()
    org = c.execute("SELECT name FROM organizations WHERE id=?", (t["organization_id"],)).fetchone() if t["organization_id"] else None
    grp = c.execute("SELECT name FROM groups WHERE id=?", (t["group_id"],)).fetchone() if t["group_id"] else None
    assignee = c.execute("SELECT name FROM users WHERE id=?", (t["assignee_id"],)).fetchone() if t["assignee_id"] else None

    cmts = c.execute("""
        SELECT tc.*, u.name AS author_name, u.role AS author_role, u.email AS author_email
        FROM ticket_comments tc LEFT JOIN users u ON u.id = tc.author_id
        WHERE tc.ticket_id = ? ORDER BY tc.created_at
    """, (ticket_id,)).fetchall()

    # Attachments grouped by comment_id so the template can list them under
    # each bubble. Cheap — typically <10 attachments per ticket.
    att_rows = c.execute("""
        SELECT id, comment_id, file_name, content_type, size_bytes, local_path
        FROM ticket_attachments WHERE ticket_id=? ORDER BY id
    """, (ticket_id,)).fetchall()
    attachments_by_comment: dict[int, list[dict]] = {}
    for a in att_rows:
        attachments_by_comment.setdefault(a["comment_id"], []).append(dict(a))

    field_rows = {r["id"]: r for r in c.execute("SELECT * FROM ticket_fields").fetchall()}
    # Block #1: merge ZD-synced custom_fields with agent's local_overrides
    cfs = db.effective_custom_fields(t)
    has_local_overrides = bool(json.loads(t["local_overrides"] or "{}").get("custom_fields"))

    # Form-aware field selection. The order of precedence is:
    #   1. A native form that matches this ticket's group (forms engine, Block #A3)
    #   2. The Zendesk-imported form referenced on the ticket
    #   3. The hard-coded PRODUCT_SUPPORT_FIELDS / MANAGED_SERVICES_FIELDS list
    raw_ticket = json.loads(t["raw"] or "{}")
    ticket_form_id = raw_ticket.get("ticket_form_id")
    native_form = db.resolve_form_for_ticket(c, group_id=t["group_id"], existing_form_id=None)
    if native_form:
        # Translate field_ids → titles for the existing _form_field_titles helper.
        titles_to_show: list[str] = []
        for fid in native_form["field_ids"]:
            r = field_rows.get(int(fid))
            if r and r["title"] and (r["type"] or "").lower() not in SYSTEM_FIELD_TYPES:
                titles_to_show.append(r["title"])
    else:
        titles_to_show = _form_field_titles(c, ticket_form_id, (grp["name"] if grp else ""))

    # AI insight
    insight_row = c.execute("""
        SELECT * FROM ticket_insights WHERE ticket_id=? ORDER BY id DESC LIMIT 1
    """, (ticket_id,)).fetchone()
    ai_recs_by_field_lower = {}
    insight = None
    if insight_row:
        recs = json.loads(insight_row["recommendations"] or "[]")
        for r in recs:
            ai_recs_by_field_lower[(r.get("field") or "").lower()] = r
        # Safe lookup for the new history-aware columns — older rows won't have them
        def _ig(col):
            try: return insight_row[col]
            except (IndexError, KeyError): return None
        try:
            similar_reasoning = json.loads(_ig("similar_with_reasoning") or "[]")
        except Exception:
            similar_reasoning = []
        insight = {
            "summary": insight_row["summary"],
            # New narrative sections (None if this is a legacy insight row)
            "issue_summary":      _ig("issue_summary"),
            "historical_context": _ig("historical_context"),
            "current_state":      _ig("current_state"),
            "recommended_action": _ig("recommended_action"),
            "similar_with_reasoning": similar_reasoning,
            "is_history_aware": bool(_ig("issue_summary") or _ig("historical_context")),
            # Existing fields
            "recommendations": recs,
            "completeness": json.loads(insight_row["completeness"] or "[]"),
            "similar_ticket_keys": json.loads(insight_row["similar_ticket_ids"] or "[]"),
            "suggested_reply": json.loads(insight_row["suggested_reply"]) if insight_row["suggested_reply"] else None,
            "kb_worthy": bool(insight_row["kb_worthy"]),
            "kb_topic": insight_row["kb_topic"],
            "pickup_flag": json.loads(insight_row["pickup_flag"]) if insight_row["pickup_flag"] else None,
            "created_at": insight_row["created_at"],
            "cost_usd": insight_row["cost_usd"],
        }

    # Build fields list. Order: required-and-empty → required-and-filled →
    # important-and-empty → important-and-filled → other-empty → other-filled.
    # Skip system field types entirely (those live in the header).
    #
    # If a native form matched, apply its conditional visibility (hide fields
    # whose dependency isn't satisfied) and its required_field_ids override.
    fields_raw = []
    title_to_field = {(r["title"] or "").lower(): r for r in field_rows.values()}
    seen_field_ids = set()
    # Visibility + form-level required override + per-rule required overrides
    visible_ids: set[int] | None = None
    why_hidden: dict[int, str] = {}
    form_required_ids: set[int] = set()
    rule_required_overrides: dict[int, bool] = {}
    if native_form:
        # current_values keyed by int field_id, value as string for comparison
        cur_int = {int(k): (v or "") for k, v in cfs.items() if str(k).isdigit()}
        visible_ids, why_hidden, rule_required_overrides = db.evaluate_visibility(native_form, cur_int)
        form_required_ids = set(int(x) for x in (native_form.get("required_field_ids") or []))
    for title in titles_to_show:
        f = title_to_field.get(title.lower())
        if not f:
            continue
        ftype = (f["type"] or "").lower()
        if ftype in SYSTEM_FIELD_TYPES:
            continue
        if f["id"] in seen_field_ids:
            continue
        seen_field_ids.add(f["id"])
        # Conditional visibility from native form (None = no form, show all)
        if visible_ids is not None and int(f["id"]) not in visible_ids:
            continue
        raw_val = cfs.get(str(f["id"]))
        display = raw_val
        opts = json.loads(f["options"] or "[]")
        if ftype in ("tagger", "multiselect") and raw_val:
            for o in opts:
                if o.get("value") == raw_val:
                    display = o.get("name") or raw_val
                    break
        suggestion = ai_recs_by_field_lower.get((f["title"] or "").lower())
        # Required resolution order (highest priority first):
        #   1. Per-rule target_required from the firing conditional rule
        #   2. Form-level required_field_ids (if a native form matched)
        #   3. Admin's explicit required_override column on ticket_fields
        #   4. ZD's required flag (agent OR portal, OR'd at sync time)
        try:
            req_override = f["required_override"]
        except (IndexError, KeyError):
            req_override = None
        if int(f["id"]) in rule_required_overrides:
            is_required = rule_required_overrides[int(f["id"])]
        elif native_form:
            is_required = (int(f["id"]) in form_required_ids)
        elif req_override is not None:
            is_required = bool(req_override)
        else:
            is_required = bool(f["required"])
        is_important = f["id"] in IMPORTANT_FIELD_IDS
        is_empty = not raw_val
        # Sort key: lower sorts first.
        # Buckets: 0=required-empty, 1=required-filled, 2=important-empty,
        #          3=important-filled, 4=other-empty, 5=other-filled.
        if is_required and is_empty:        bucket = 0
        elif is_required:                   bucket = 1
        elif is_important and is_empty:     bucket = 2
        elif is_important:                  bucket = 3
        elif is_empty:                      bucket = 4
        else:                               bucket = 5
        fields_raw.append({
            "id": f["id"],
            "name": f["title"],
            "type": f["type"],
            "value": display if raw_val else None,
            "raw_value": raw_val or "",
            "options": opts,
            "empty": is_empty,
            "has_suggestion": bool(suggestion),
            "required": is_required,
            "important": is_important,
            "_bucket": bucket,
        })
    fields_raw.sort(key=lambda x: (x["_bucket"], (x["name"] or "").lower()))
    fields = [{k: v for k, v in f.items() if k != "_bucket"} for f in fields_raw]

    similar = _find_similar_tickets(c, t, cfs, field_rows)
    sla = _sla_status(c, t, recompute=True)

    # Metrics for SLA detail
    metrics_row = c.execute("SELECT * FROM ticket_metrics WHERE ticket_id=?", (ticket_id,)).fetchone()
    metrics = dict(metrics_row) if metrics_row else None

    # Custom status display label (Suite Pro+) — fall back to standard status
    custom_status_label = None
    cs_id = raw_ticket.get("custom_status_id")
    if cs_id:
        cs = c.execute("SELECT agent_label, status_category FROM custom_statuses WHERE id=?", (cs_id,)).fetchone()
        if cs:
            custom_status_label = cs["agent_label"]

    # Mandatory-fields check. Source of "required" is:
    #   - if a native form matched: the form's required_field_ids override
    #   - otherwise: ZD's `required_in_portal` flag
    # System field types are always excluded — their values live elsewhere.
    # Hidden-by-condition fields are excluded too.
    mandatory_missing = []
    titles_lower = [tt.lower() for tt in titles_to_show]
    if native_form:
        required_ids = set(int(x) for x in (native_form.get("required_field_ids") or []))
        for fid in required_ids:
            f = field_rows.get(fid)
            if not f:
                continue
            if (f["type"] or "").lower() in SYSTEM_FIELD_TYPES:
                continue
            if visible_ids is not None and fid not in visible_ids:
                continue
            v = cfs.get(str(fid))
            if not v:
                mandatory_missing.append((f["title"] or "").strip() or f"field {fid}")
    else:
        for fid, f in field_rows.items():
            if not f["required"]:
                continue
            if (f["type"] or "").lower() in SYSTEM_FIELD_TYPES:
                continue
            title = (f["title"] or "").strip()
            if not title or title.lower() not in titles_lower:
                continue
            v = cfs.get(str(fid))
            if not v:
                mandatory_missing.append(title)

    # Recent feedback decisions for transparency
    fb_rows = c.execute("""
        SELECT field_name, decision, final_value, ai_suggested_value, created_at
        FROM ai_feedback WHERE ticket_id=? ORDER BY id DESC LIMIT 20
    """, (ticket_id,)).fetchall()
    feedback_log = [dict(r) for r in fb_rows]
    feedback_by_field = {(r["field_name"] or "").lower(): dict(r) for r in fb_rows}

    # Block #1: native vs ZD-sourced display
    source = t["source"] if t["source"] else "zendesk"
    local_id = t["local_id"]
    external_id = t["external_id"]
    display_id = local_id if (source == "native" and local_id) else str(t["id"])

    return {
        "id": t["id"],
        "local_id": local_id,
        "external_id": external_id,
        "source": source,
        "display_id": display_id,
        "has_local_overrides": has_local_overrides,
        "subject": t["subject"],
        "status": t["status"],
        "custom_status_label": custom_status_label,
        "priority": t["priority"],
        "tags": json.loads(t["tags"] or "[]"),
        "created_at": t["created_at"],
        "updated_at": t["updated_at"],
        "requester": dict(requester) if requester else None,
        "organization": dict(org) if org else None,
        "group": dict(grp) if grp else None,
        "assignee": dict(assignee) if assignee else None,
        "comments": [
            {**dict(cm), "attachments": attachments_by_comment.get(cm["id"], [])}
            for cm in cmts
        ],
        "fields": fields,
        "insight": insight,
        "similar": similar,
        "sla": sla,
        "metrics": metrics,
        "form_id": ticket_form_id,
        "mandatory_missing": mandatory_missing,
        "feedback_log": feedback_log,
        "feedback_by_field": feedback_by_field,
    }


def _find_similar_tickets(c: sqlite3.Connection, t: sqlite3.Row, cfs: dict, field_rows: dict, limit: int = 5) -> list[dict]:
    """Multi-tier similarity matcher.

    Previous version restricted to solved/closed tickets in the last 90 days,
    which excluded almost everything for new customers / customers whose
    solved tickets are older than 3 months. This version matches across all
    ticket statuses, prefers solved (they have resolutions = more useful) via
    a score bonus, has no time cutoff, and falls back through tiers until it
    finds candidates:

      Tier 1: same customer + same RC1   (strongest signal)
      Tier 2: same customer              (B2B context)
      Tier 3: same RC1                   (same root cause across customers)
      Tier 4: subject word-overlap       (broad fallback for anything else)

    Performance: each tier is capped to a few hundred rows. We score in
    Python and return the top N.
    """
    cust = cfs.get(FID_CUSTOMER)
    prod = cfs.get(FID_PRODUCT)
    module = cfs.get(FID_MODULE)
    rc1 = cfs.get(FID_RC1)
    rc2 = cfs.get(FID_RC2)
    subj_lower = (t["subject"] or "").lower()
    # Words from subject to use for the fallback text match — keep tokens >3
    # chars so we don't match noise like "the", "and".
    subj_keywords = [w.strip(".,:;!?[]()") for w in subj_lower.split()
                     if len(w.strip(".,:;!?[]()")) > 3][:5]

    if not any([cust, prod, module, rc1]) and not subj_keywords:
        return []

    # Common ORDER BY — solved tickets first (they're the most useful past
    # references because they include a resolution), then most-recent first.
    ORDER_BY = ("CASE WHEN status IN ('solved','closed') THEN 0 ELSE 1 END, "
                "updated_at DESC")

    def _scan(where_parts: list, where_params: list, pool: int):
        extra = (" AND " + " AND ".join(where_parts)) if where_parts else ""
        return c.execute(f"""
            SELECT id, subject, status, custom_fields, created_at, updated_at, solved_at
            FROM tickets
            WHERE id != ? {extra}
            ORDER BY {ORDER_BY} LIMIT {pool}
        """, [t["id"]] + where_params).fetchall()

    seen_ids = set()
    rows: list = []
    def _add(more):
        for r in more:
            if r["id"] in seen_ids: continue
            seen_ids.add(r["id"])
            rows.append(r)

    # Tier 1 — same customer + same RC1
    if cust and rc1:
        _add(_scan(
            [f"json_extract(custom_fields,'$.\"{FID_CUSTOMER}\"') = ?",
             f"json_extract(custom_fields,'$.\"{FID_RC1}\"') = ?"],
            [cust, rc1], 60))
    # Tier 2 — same customer
    if len(rows) < limit * 3 and cust:
        _add(_scan(
            [f"json_extract(custom_fields,'$.\"{FID_CUSTOMER}\"') = ?"],
            [cust], 120))
    # Tier 3 — same RC1 (cross-customer)
    if len(rows) < limit * 3 and rc1:
        _add(_scan(
            [f"json_extract(custom_fields,'$.\"{FID_RC1}\"') = ?"],
            [rc1], 120))
    # Tier 4 — subject keyword overlap (last resort)
    if len(rows) < limit and subj_keywords:
        like_clauses = " OR ".join(["LOWER(subject) LIKE ?"] * len(subj_keywords))
        _add(_scan(
            [f"({like_clauses})"],
            [f"%{kw}%" for kw in subj_keywords], 100))
    # Pull the CURRENT ticket's stored AI insight summary (if any) — we use it
    # as a similarity signal against each candidate's summary. This is the
    # "match by AI analysis, not just fields" the user asked for: zero extra
    # AI calls at match time, just leverages summaries we've already saved.
    cur_summary = ""
    cur_ins = c.execute(
        "SELECT summary, issue_summary FROM ticket_insights WHERE ticket_id=? ORDER BY id DESC LIMIT 1",
        (t["id"],)).fetchone()
    if cur_ins:
        cur_summary = (cur_ins["issue_summary"] or cur_ins["summary"] or "")

    # Tokenize summaries into 4+ char words for Jaccard similarity
    def _tok(s: str) -> set:
        if not s: return set()
        return {w.strip(".,:;!?[]()'\"-").lower()
                for w in s.split()
                if len(w.strip(".,:;!?[]()'\"-")) >= 4}
    cur_tokens = _tok(cur_summary)

    # Batch-fetch candidate summaries in one query (no N+1)
    candidate_summaries: dict[int, str] = {}
    if rows and cur_tokens:
        cand_ids = [r["id"] for r in rows]
        ph = ",".join("?" * len(cand_ids))
        for ir in c.execute(
            f"""SELECT ticket_id, summary, issue_summary, MAX(id) AS _last
                FROM ticket_insights
                WHERE ticket_id IN ({ph})
                GROUP BY ticket_id""",
            cand_ids,
        ).fetchall():
            candidate_summaries[ir["ticket_id"]] = (
                ir["issue_summary"] or ir["summary"] or ""
            )

    # Score every candidate
    scored = []
    for r in rows:
        try:
            rcfs = json.loads(r["custom_fields"] or "{}")
        except Exception:
            continue
        score = 0
        if cust and rcfs.get(FID_CUSTOMER) == cust:  score += 3
        if prod and rcfs.get(FID_PRODUCT)  == prod:  score += 2
        if module and rcfs.get(FID_MODULE) == module: score += 2
        if rc1 and rcfs.get(FID_RC1) == rc1:         score += 2
        if rc2 and rcfs.get(FID_RC2) == rc2:         score += 1
        # Subject keyword overlap (caps at 3)
        if subj_keywords:
            r_subj = (r["subject"] or "").lower()
            overlap = sum(1 for kw in subj_keywords if kw in r_subj)
            score += min(overlap, 3)
        # AI insight summary overlap — Jaccard on 4+ char tokens. Two tickets
        # whose AI-generated summaries share a lot of vocabulary are probably
        # about the same underlying issue, even when their custom fields don't
        # line up. Caps at 5 to dominate match% when it's strong.
        ai_sim_pct = 0
        cand_sum = candidate_summaries.get(r["id"], "")
        if cur_tokens and cand_sum:
            cand_tokens = _tok(cand_sum)
            if cand_tokens:
                inter = len(cur_tokens & cand_tokens)
                uni   = len(cur_tokens | cand_tokens)
                if uni:
                    ai_sim = inter / uni
                    ai_sim_pct = int(ai_sim * 100)
                    score += min(int(ai_sim * 10), 5)   # up to +5
        # Solved tickets are more useful for "have we fixed this before?"
        if r["status"] in ("solved", "closed"):       score += 1
        if score == 0:
            continue
        # max_score = 3+2+2+2+1 + 3 + 5 + 1 = 19
        max_score = 19
        scored.append({
            "id": r["id"],
            "subject": r["subject"],
            "status": r["status"],
            "score": score,
            "max_score": max_score,
            "match_pct": min(100, round(score / max_score * 100)),
            "ai_summary_overlap_pct": ai_sim_pct,    # signal the UI shows
            "summary": candidate_summaries.get(r["id"], "") or "",
            "solved_at": (r["solved_at"] or r["updated_at"] or "")[:10],
        })
    scored.sort(key=lambda x: -x["score"])
    return scored[:limit]


# ---------- Sidebar context (used by every page) ----------
# Computing the sidebar runs ~50 COUNT(*) queries (10 static views + 10 customers
# × 4 sub-views). With indexes that's ~150 ms; without, it was 50 s. We still
# memoize per-user for 60 s so multi-tab browsing and quick clicks are instant.
_SIDEBAR_CACHE: dict[str, tuple[float, dict]] = {}
_SIDEBAR_TTL_SECONDS = 60.0


def _compute_sidebar_ctx(c: sqlite3.Connection, user: dict) -> dict:
    # ---- Native views the user can see (system + personal + shared) ----
    native_views = db.list_views_for_user(c, user["email"]) if user.get("email") else []
    # Convert native views into the same shape STATIC_VIEWS uses + add the
    # filter-derived count.
    # PERF: resolve zd_user_id ONCE and reuse for every view filter that has
    # `is_me`. Pass the already-open connection so the per-view group_name
    # lookup uses the cache instead of opening fresh connections.
    user_zd_id = user.get("zd_user_id") if user else None
    native_view_defs: list[dict] = []
    for nv in native_views:
        key = f"nv_{nv['id']}"
        try:
            filter_obj = json.loads(nv.get("filter_json") or "{}")
        except json.JSONDecodeError:
            filter_obj = {}
        where_sql, params = _view_sql_from_filter(
            filter_obj, user["email"],
            conn=c, zd_user_id=user_zd_id,
        )
        try:
            cnt = c.execute(f"SELECT COUNT(*) AS n FROM tickets WHERE {where_sql}", params).fetchone()["n"]
        except Exception:
            cnt = 0
        # Label: icon + name. Prefer the icon column if set.
        label = (nv.get("icon") or "") + " " + nv["name"] if nv.get("icon") else nv["name"]
        native_view_defs.append({
            "key": key, "label": label.strip(),
            "color": nv.get("color") or "indigo",
            "scope": nv.get("scope") or "personal",
            "owner_email": nv.get("owner_email"),
            "is_system_default": bool(nv.get("is_system_default")),
            "count": cnt,
        })

    # ---- Legacy static views (tucked into a collapsed "More views" section) ----
    legacy_counts = _view_counts(c, user["email"])
    legacy_view_defs = [{**v, "count": legacy_counts.get(v["key"], 0)} for v in STATIC_VIEWS]

    # ---- Counts dict combining both for the sidebar template ----
    counts: dict[str, int] = dict(legacy_counts)
    for v in native_view_defs:
        counts[v["key"]] = v["count"]

    customers = _top_customers(c, limit=10)
    cust_with_counts = []
    for cu in customers:
        cust_with_counts.append({**cu, "subs": _customer_view_counts(c, cu["value"])})
    spent = db.month_to_date_spend(c)
    last_sync = db.get_meta(c, "last_sync_run_at") or "—"
    rc1_options = []
    rc1_field = c.execute("SELECT options FROM ticket_fields WHERE id=?", (int(FID_RC1),)).fetchone()
    if rc1_field:
        rc1_options = [{"value": o.get("value"), "name": o.get("name")} for o in json.loads(rc1_field["options"] or "[]")[:50]]

    # Partition native views by section
    system_views = [v for v in native_view_defs if v["scope"] == "system"]
    personal_views = [v for v in native_view_defs if v["scope"] == "personal" and v["owner_email"] == user.get("email")]
    shared_views = [v for v in native_view_defs if v["scope"] == "shared"]

    return {
        # Sections the new sidebar renders. Empty sections render nothing.
        "system_views": system_views,
        "personal_views": personal_views,
        "shared_views": shared_views,
        "legacy_view_defs": legacy_view_defs,
        # Backward compat — older templates still expect view_defs/view_counts
        "view_defs": [{**v, "key": v["key"], "label": v["label"], "color": v["color"]} for v in native_view_defs] or STATIC_VIEWS,
        "view_counts": counts,
        "customer_buckets": cust_with_counts,
        "spend": spent,
        "budget": config.MONTHLY_BUDGET_USD,
        "last_sync": last_sync,
        "rc1_options": rc1_options,
    }


def _sidebar_ctx(c: sqlite3.Connection, user: dict) -> dict:
    """Cached wrapper around _compute_sidebar_ctx. Keyed on user email.
    The Reload button on each page busts the cache by calling /api/sidebar/refresh."""
    import time as _t
    key = (user or {}).get("email") or "_anon"
    hit = _SIDEBAR_CACHE.get(key)
    now = _t.monotonic()
    if hit and (now - hit[0] < _SIDEBAR_TTL_SECONDS):
        return hit[1]
    data = _compute_sidebar_ctx(c, user)
    _SIDEBAR_CACHE[key] = (now, data)
    return data


def _invalidate_sidebar_cache(user_email: str | None = None) -> None:
    if user_email is None:
        _SIDEBAR_CACHE.clear()
    else:
        _SIDEBAR_CACHE.pop(user_email, None)


# ---------- Routes ----------
@app.get("/", response_class=HTMLResponse)
async def index(request: Request, user: dict = Depends(require_user)):
    return RedirectResponse("/views/open")


# ===========================================================================
# F7 · Universal search — cross-view ticket finder
# ===========================================================================

@app.get("/search", response_class=HTMLResponse)
async def search_page(
    request: Request,
    q: str = "",
    status: str = "",         # comma-separated
    priority: str = "",       # comma-separated
    group: str = "",          # comma-separated group ids
    customer: str = "",
    has_ai: str = "",
    date_field: str = "updated_at",
    days: int = 0,
    include_comments: int = 0,
    sort: str = "updated_desc",
    page: int = 1,
    user: dict = Depends(auth_mod.require_any("tickets.view", "tickets.search")),
):
    """Universal ticket search. Renders the results page; the form on this
    page POSTs back to /search (GET) with all filters in query params, so
    every search is a shareable URL."""
    page = max(1, int(page))
    per_page = 50
    offset = (page - 1) * per_page

    status_list = [s for s in status.split(",") if s.strip()]
    priority_list = [p for p in priority.split(",") if p.strip()]
    group_id_list: list[int] = []
    for g in group.split(","):
        if g.strip().lstrip("-").isdigit():
            group_id_list.append(int(g))

    with db.conn() as c:
        # PERF: search page uses the slim sidebar ctx — skips the per-view
        # COUNT loop + customer-bucket aggregation which together added
        # ~300-500ms on a 50K-ticket DB.
        sb = _slim_sidebar_ctx(c, user)
        has_more = False
        if q.strip() or status_list or priority_list or group_id_list or customer or has_ai or days:
            tickets, total, has_more = _search_tickets(
                c, q=q.strip(),
                status=status_list or None,
                priority=priority_list or None,
                group_ids=group_id_list or None,
                customer=customer.strip() or "",
                has_ai=has_ai,
                date_field=date_field,
                date_within_days=int(days),
                include_comments=int(include_comments),
                sort=sort,
                limit=per_page, offset=offset,
            )
        else:
            tickets, total = [], 0
        # Filter dropdowns
        groups_all = [dict(r) for r in c.execute(
            "SELECT id, name FROM groups WHERE COALESCE(is_active,1)=1 ORDER BY name"
        ).fetchall()]
        customers_top = _top_customers(c, limit=20)

    # Activity log — record search so reports can show "popular queries"
    if q.strip() and user.get("email"):
        _activity.log(user_email=user["email"], event_type="navigation",
                      event_subtype="search",
                      detail={"q": q.strip(), "results": total,
                              "filters": {"status": status_list,
                                           "priority": priority_list,
                                           "groups": group_id_list,
                                           "customer": customer,
                                           "has_ai": has_ai,
                                           "date_field": date_field,
                                           "days": days,
                                           "include_comments": bool(int(include_comments))}},
                      request=request)

    # With the no-COUNT strategy, total_pages is unknown. We use the
    # has_more flag from the search helper to decide whether to show Next.
    from urllib.parse import urlencode as _urlencode
    base_params: list[tuple[str, str]] = []
    for k, v in request.query_params.multi_items():
        if k != "page":
            base_params.append((k, v))
    def _build_page_url(p: int) -> str:
        return "/search?" + _urlencode(base_params + [("page", str(p))], doseq=True)
    prev_url = _build_page_url(page - 1) if page > 1 else ""
    next_url = _build_page_url(page + 1) if has_more else ""

    return TEMPLATES.TemplateResponse("search.html", {
        "request": request, "user": user, "tickets": tickets,
        "q": q, "total": total, "has_more": has_more,
        "page": page, "per_page": per_page,
        "prev_url": prev_url, "next_url": next_url,
        "filters": {
            "status": status_list,
            "priority": priority_list,
            "group": group_id_list,
            "customer": customer,
            "has_ai": has_ai,
            "date_field": date_field,
            "days": days,
            "include_comments": int(include_comments),
            "sort": sort,
        },
        "groups_all": groups_all,
        "customers_top": customers_top,
        "current_view": "_search", "in_detail": False, "search": q,
        **sb,
    })


@app.get("/views/{view_name:path}", response_class=HTMLResponse)
async def view_list(view_name: str, request: Request, q: str = "",
                    sort: str = "updated_desc",
                    customer: str = "", jira: str = "", rc1: str = "", group: str = "",
                    user: dict = Depends(require_user)):
    with db.conn() as c:
        sb = _sidebar_ctx(c, user)
        # Resolve view label
        label = next((v["label"] for v in STATIC_VIEWS if v["key"] == view_name), None)
        # Native view label lookup (nv_<id>)
        if label is None and view_name.startswith("nv_"):
            try:
                vid = int(view_name[3:])
                nv_row = c.execute("SELECT name, icon FROM native_views WHERE id=?",
                                    (vid,)).fetchone()
                if nv_row:
                    label = ((nv_row["icon"] or "") + " " + nv_row["name"]).strip()
            except ValueError:
                pass
        if label is None and view_name.startswith("cust_"):
            body = view_name[5:]
            if ":" in body:
                cust_value, sub = body.rsplit(":", 1)
                cust_label = next((cu["label"] for cu in sb["customer_buckets"] if cu["value"] == cust_value), cust_value)
                sub_labels = {"open": "Open with support", "eng": "With engineering",
                              "pending": "Awaiting customer", "unassigned": "Open & unassigned"}
                label = f"{cust_label} · {sub_labels.get(sub, sub)}"
        if label is None:
            raise HTTPException(404, "Unknown view")
        tickets = _list_view_tickets(c, view_name, user["email"], search=q, sort=sort,
                                     filter_customer=customer, filter_jira=jira,
                                     filter_rc1=rc1, filter_group=group)
        groups = c.execute("SELECT id, name FROM groups ORDER BY name").fetchall()
        # F0+ T4: dynamic columns based on the view's column_ids_json
        view_columns = _resolve_view_columns(c, view_name)
    return TEMPLATES.TemplateResponse("view_list.html", {
        "request": request, "user": user, "tickets": tickets,
        "current_view": view_name, "current_view_label": label,
        "view_columns": view_columns,
        "search": q, "sort": sort,
        "filter_customer": customer, "filter_jira": jira,
        "filter_rc1": rc1, "filter_group": group,
        "groups": [dict(g) for g in groups],
        "in_detail": False,
        **sb,
    })


@app.get("/tickets/new", response_class=HTMLResponse)
async def new_ticket_form(request: Request, user: dict = Depends(require_user)):
    """Form to create a native ticket. Accessed from the sidebar '+ Create ticket' link."""
    with db.conn() as c:
        sb = _sidebar_ctx(c, user)
        groups = c.execute("SELECT id, name FROM groups ORDER BY name").fetchall()
        # Customer options for the dropdown
        cust_field = c.execute("SELECT options FROM ticket_fields WHERE id=?", (int(FID_CUSTOMER),)).fetchone()
        customers = []
        if cust_field:
            customers = [{"value": o.get("value"), "name": o.get("name")}
                         for o in json.loads(cust_field["options"] or "[]")]
            customers.sort(key=lambda x: (x["name"] or "").lower())
    return TEMPLATES.TemplateResponse("create_ticket.html", {
        "request": request, "user": user, "current_view": "_new",
        "in_detail": False, "search": "",
        "groups": [dict(g) for g in groups],
        "customers": customers,
        **sb,
    })


@app.post("/api/tickets/create")
async def create_native_ticket(
    request: Request,
    subject: str = Form(...),
    description: str = Form(""),
    requester_email: str = Form(...),
    requester_name: str = Form(""),
    customer_value: str = Form(""),
    group_id: int = Form(...),
    priority: str = Form("normal"),
    user: dict = Depends(auth_mod.require("tickets.edit_fields")),
):
    """Create a native ticket. Source='native', generates local_id (BP-NNNNNN).
    Stays entirely in our DB — never sent to Zendesk."""
    if not subject.strip():
        raise HTTPException(400, "subject is required")
    if priority not in ("low", "normal", "high", "urgent"):
        priority = "normal"
    with db.conn() as c:
        # Find or create the requester user (uses email as the natural key)
        u = c.execute("SELECT id FROM users WHERE LOWER(email) = LOWER(?)", (requester_email,)).fetchone()
        if u:
            requester_id = u["id"]
        else:
            # New user — id in 1B+ range to avoid colliding with ZD users
            seq_str = db.get_meta(c, "next_native_user_seq") or "1"
            useq = int(seq_str)
            db.set_meta(c, "next_native_user_seq", str(useq + 1))
            requester_id = 1_000_000_000 + useq
            c.execute("""
                INSERT INTO users (id, name, email, role, raw)
                VALUES (?, ?, ?, 'end-user', ?)
            """, (requester_id, requester_name or requester_email, requester_email,
                  json.dumps({"source": "native_create", "created_by": user["email"]})))

        # Build initial custom fields if customer was picked
        cfs = {}
        if customer_value:
            cfs[FID_CUSTOMER] = customer_value

        result = db.insert_native_ticket(
            c, subject=subject.strip(), requester_id=requester_id,
            organization_id=None, group_id=group_id, priority=priority,
            custom_fields=cfs, creator_email=user["email"],
        )

        # Insert the description as the initial comment from the requester
        if description.strip():
            db.insert_native_comment(
                c, ticket_id=result["id"], author_id=requester_id,
                body=description.strip(), public=True,
            )

        db.audit(c, actor=user["email"], action="create_native_ticket",
                 target_type="ticket", target_id=str(result["id"]),
                 detail=f"local_id={result['local_id']} subject={subject[:80]}")
        # Per-ticket timeline + triggers
        db.audit_ticket(c, ticket_id=result["id"], event_type="ticket.created",
                        event_summary=f"Native ticket created: {subject[:80]}",
                        actor_email=user["email"], actor_type="agent",
                        raw={"local_id": result["local_id"], "subject": subject})
        try:
            from .. import rules_engine
            rules_engine.dispatch_event(c, "ticket.created", result["id"], actor_email=user["email"])
            rules_engine.dispatch_event(c, "ticket.created.native", result["id"], actor_email=user["email"])
        except Exception as e:
            print(f"rules dispatch failed: {e}")
    return JSONResponse({"ok": True, **result, "url": f"/tickets/{result['local_id']}"})


@app.get("/tickets/{ident}", response_class=HTMLResponse)
async def ticket_detail(ident: str, request: Request, view: str = "open",
                        user: dict = Depends(require_user)):
    """Accept either a numeric ZD id (e.g. 595049) or a local_id (e.g. BP-000001)."""
    with db.conn() as c:
        ticket_id = _resolve_ticket_id(c, ident)
        if ticket_id is None:
            raise HTTPException(404, "Ticket not found")
        t = _full_ticket(c, ticket_id)
        if not t:
            raise HTTPException(404, "Ticket not found")
        sb = _sidebar_ctx(c, user)
    return TEMPLATES.TemplateResponse("ticket_detail.html", {
        "request": request, "user": user, "ticket": t,
        "current_view": view, "in_detail": True,
        "search": "",
        "translate_languages": TRANSLATE_LANGUAGES,
        **sb,
    })


@app.get("/spend")
async def spend(user: dict = Depends(require_user)):
    with db.conn() as c:
        s = db.month_to_date_spend(c)
        n_insights = c.execute("SELECT COUNT(*) AS n FROM ticket_insights WHERE created_at >= datetime('now','start of month')").fetchone()["n"]
    return JSONResponse({"month_to_date_usd": round(s, 4), "budget_usd": config.MONTHLY_BUDGET_USD,
                         "insights_this_month": n_insights, "model": config.ANTHROPIC_MODEL})


def _resolve_field_id_by_title(c: sqlite3.Connection, title: str) -> int | None:
    row = c.execute("SELECT id FROM ticket_fields WHERE LOWER(title) = LOWER(?)", (title,)).fetchone()
    return row["id"] if row else None


def _resolve_ticket_id(c: sqlite3.Connection, ident: str) -> int | None:
    """Accept either a numeric id (existing ZD format) or a local_id (BP-NNNNNN)."""
    s = (ident or "").strip()
    if s.isdigit():
        row = c.execute("SELECT id FROM tickets WHERE id=?", (int(s),)).fetchone()
        return row["id"] if row else None
    row = c.execute("SELECT id FROM tickets WHERE local_id=?", (s,)).fetchone()
    return row["id"] if row else None


@app.post("/api/tickets/{ticket_id}/feedback")
async def submit_feedback(
    ticket_id: int, request: Request,
    field: str = Form(...),
    decision: str = Form(...),                 # 'approved' | 'rejected' | 'edited'
    ai_current: str = Form(""),
    ai_suggested: str = Form(...),
    confidence: str = Form(""),
    final_value: str = Form(""),
    rejection_reason: str = Form(""),
    user: dict = Depends(auth_mod.require("ai.feedback")),
):
    """Approve / reject / edit an AI suggestion. APPROVE now writes to Zendesk."""
    if decision not in ("approved", "rejected", "edited"):
        raise HTTPException(400, "decision must be 'approved', 'rejected', or 'edited'")
    try:
        conf = float(confidence) if confidence else None
    except ValueError:
        conf = None

    final = (final_value or ai_suggested) if decision == "approved" else (final_value or None)
    write_status = None

    with db.conn() as c:
        last_ins = c.execute("SELECT id FROM ticket_insights WHERE ticket_id=? ORDER BY id DESC LIMIT 1", (ticket_id,)).fetchone()
        insight_id = last_ins["id"] if last_ins else None

        # POLICY (Block #1): no writes to Zendesk. All field changes are LOCAL ONLY.
        # Approve/edit decisions go into tickets.local_overrides; sync never touches them.
        if decision in ("approved", "edited") and final:
            fid = _resolve_field_id_by_title(c, field)
            if fid:
                f = c.execute("SELECT type, options FROM ticket_fields WHERE id=?", (fid,)).fetchone()
                stored_value = final
                if f and f["type"] in ("tagger", "multiselect"):
                    for o in json.loads(f["options"] or "[]"):
                        if o.get("name") == final or o.get("value") == final:
                            stored_value = o.get("value")
                            break
                try:
                    db.set_local_field_override(c, ticket_id, fid, stored_value)
                    write_status = "saved_local_only"
                except Exception as e:
                    write_status = f"local_save_error: {e}"
            else:
                write_status = f"field_not_found: {field}"

        fb_id = db.record_feedback(
            c, ticket_id=ticket_id, insight_id=insight_id, field_name=field,
            ai_current=ai_current or None, ai_suggested=ai_suggested,
            confidence=conf, decision=decision,
            final_value=final, rejection_reason=rejection_reason or None,
            actor=user["email"],
        )
        db.audit(c, actor=user["email"], action=f"ai_feedback_{decision}",
                 target_type="ticket", target_id=str(ticket_id),
                 detail=f"field={field} suggested={ai_suggested} final={final} write={write_status}")
    return JSONResponse({"ok": True, "feedback_id": fb_id, "decision": decision,
                         "write_status": write_status, "final_value": final})


@app.post("/api/tickets/{ticket_id}/field")
async def update_ticket_field(
    ticket_id: int, request: Request,
    field_id: int = Form(...),
    value: str = Form(""),
    field_kind: str = Form("custom"),   # 'custom' | 'assignee' | 'priority' | 'status' | 'group'
    user: dict = Depends(auth_mod.require_any(
        "tickets.edit_fields", "tickets.assign_self", "tickets.assign_others",
        "tickets.change_status", "tickets.assign",
    )),
):
    """Manual edit of a single ticket field. Writes to local_overrides only — Zendesk untouched.

    field_kind:
      - 'custom' (default): operates on ticket_fields[field_id] custom field
      - 'assignee': updates tickets.assignee_id (value = ZD user_id or empty → NULL)
                    Requires tickets.assign_self (if value matches your zd_user_id)
                    OR tickets.assign_others.
      - 'priority' / 'status' / 'group': updates the corresponding column.
                    Requires tickets.edit_fields or tickets.change_status (status).
    """
    with db.conn() as c:
        # ---- F0+ T6 · Special case: assignee write goes to tickets.assignee_id ----
        if field_kind == "assignee":
            new_zd_id: int | None = None
            if value.strip():
                try: new_zd_id = int(value.strip())
                except ValueError: raise HTTPException(400, "value must be an integer ZD user id")
            # Permission check: assign_self if assigning to OWN zd_user_id, else assign_others
            own_zd = user.get("zd_user_id") or db.auto_map_zd_user(c, user["email"])
            assigning_to_self = (new_zd_id is not None and own_zd is not None and new_zd_id == own_zd)
            if assigning_to_self:
                if "tickets.assign_self" not in user["permissions"]:
                    raise HTTPException(403, "Missing permission: tickets.assign_self")
            else:
                if "tickets.assign_others" not in user["permissions"]:
                    raise HTTPException(403, "Missing permission: tickets.assign_others (or assign_self if you mean yourself)")
            prev = c.execute("SELECT assignee_id FROM tickets WHERE id=?", (ticket_id,)).fetchone()
            if not prev:
                raise HTTPException(404, f"Ticket {ticket_id} not found")
            before_id = prev["assignee_id"]
            c.execute("UPDATE tickets SET assignee_id=?, updated_at=? WHERE id=?",
                      (new_zd_id, db.now_iso(), ticket_id))
            # Resolve display names for the audit entry
            def _name(uid):
                if not uid: return None
                r = c.execute("SELECT name FROM users WHERE id=?", (uid,)).fetchone()
                return r["name"] if r else f"#{uid}"
            db.audit_ticket(c, ticket_id=ticket_id, event_type="assignee.changed",
                            event_summary=f"{_name(before_id) or '∅'} → {_name(new_zd_id) or '∅'}",
                            actor_email=user["email"], actor_type="agent",
                            field_key="assignee_id",
                            before=before_id, after=new_zd_id)
            try:
                from .. import rules_engine
                rules_engine.dispatch_event(c, "assignee.changed", ticket_id,
                                              actor_email=user["email"],
                                              extra_context={"before_value": before_id,
                                                              "after_value": new_zd_id})
            except Exception as e:
                print(f"rules dispatch failed: {e}")
            return JSONResponse({"ok": True, "ticket_id": ticket_id,
                                  "assignee_id": new_zd_id,
                                  "display": _name(new_zd_id)})

        # ---- Default path: custom field edit (requires tickets.edit_fields) ----
        if "tickets.edit_fields" not in user["permissions"]:
            raise HTTPException(403, "Missing permission: tickets.edit_fields")
        f = c.execute("SELECT title, type, options FROM ticket_fields WHERE id=?", (field_id,)).fetchone()
        if not f:
            raise HTTPException(404, f"Unknown field_id {field_id}")

        # Capture before-value for audit
        prev_row = c.execute("SELECT custom_fields, local_overrides FROM tickets WHERE id=?", (ticket_id,)).fetchone()
        before = (db.effective_custom_fields(prev_row) or {}).get(str(field_id)) if prev_row else None

        stored_value: object = value
        if f["type"] in ("tagger", "multiselect"):
            opts = json.loads(f["options"] or "[]")
            for o in opts:
                if value and (o.get("name") == value or o.get("value") == value):
                    stored_value = o.get("value")
                    break

        try:
            db.set_local_field_override(c, ticket_id, field_id, stored_value)
        except Exception as e:
            raise HTTPException(500, f"Local save failed: {e}")

        db.audit(c, actor=user["email"], action="manual_field_edit_local",
                 target_type="ticket", target_id=str(ticket_id),
                 detail=f"field={f['title']} value={stored_value} (local only — ZD untouched)")
        # Per-ticket audit timeline
        db.audit_ticket(c, ticket_id=ticket_id, event_type="field.changed",
                        event_summary=f"{f['title']}: {before or '∅'} → {stored_value or '∅'}",
                        actor_email=user["email"], actor_type="agent",
                        field_key=str(field_id), before=before, after=stored_value)
        # Fire trigger rules
        try:
            from .. import rules_engine
            rules_engine.dispatch_event(c, "field.changed", ticket_id,
                                         actor_email=user["email"],
                                         extra_context={"changed_field_id": field_id,
                                                        "before_value": before,
                                                        "after_value": stored_value})
        except Exception as e:
            print(f"rules dispatch failed: {e}")

        display = stored_value
        if f["type"] in ("tagger", "multiselect"):
            for o in json.loads(f["options"] or "[]"):
                if o.get("value") == stored_value:
                    display = o.get("name") or stored_value
                    break
    return JSONResponse({"ok": True, "field": f["title"], "raw_value": stored_value,
                         "display": display, "scope": "local_only"})


@app.post("/api/tickets/{ticket_id}/add-field-option")
async def add_field_option(
    ticket_id: int, request: Request,
    field: str = Form(...),
    option_name: str = Form(...),
    user: dict = Depends(auth_mod.require("tickets.edit_fields")),
):
    """Add a new option to a dropdown locally, then apply it to this ticket.
    POLICY (Block #1): no calls to Zendesk. Option is appended to the local
    ticket_fields.options copy and tracked in local_field_options. Field value
    is set via tickets.local_overrides."""
    with db.conn() as c:
        fid = _resolve_field_id_by_title(c, field)
        if not fid:
            raise HTTPException(404, f"Unknown field: {field}")
        f = c.execute("SELECT type, options FROM ticket_fields WHERE id=?", (fid,)).fetchone()
        if f["type"] not in ("tagger", "multiselect"):
            raise HTTPException(400, f"Field {field} is not a dropdown ({f['type']})")

        opts = json.loads(f["options"] or "[]")
        existing = next((o for o in opts if (o.get("name") or "").lower() == option_name.lower()), None)
        if existing:
            new_value = existing.get("value")
            already = True
        else:
            new_value = option_name.lower().replace(" ", "_").replace("/", "_").strip("_")
            opts.append({"name": option_name, "value": new_value})
            c.execute("UPDATE ticket_fields SET options=? WHERE id=?", (json.dumps(opts), fid))
            already = False

        # Track every local addition for the audit / repository
        c.execute("""
            INSERT INTO local_field_options (field_id, option_name, option_value, proposed_by_email, sync_status, created_at)
            VALUES (?, ?, ?, ?, 'local_only', ?)
        """, (fid, option_name, new_value, user["email"], db.now_iso()))

        # Apply the value to this ticket via local override
        db.set_local_field_override(c, ticket_id, fid, new_value)

        db.audit(c, actor=user["email"], action="add_field_option_local",
                 target_type="ticket_field", target_id=str(fid),
                 detail=f"name={option_name} value={new_value} ticket={ticket_id} (local only)")
    return JSONResponse({"ok": True, "field": field, "option_name": option_name,
                         "option_value": new_value, "display": option_name,
                         "already_existed": already, "scope": "local_only"})


# =============================================================================
# Reply box block: upload an attachment, post a reply, translate a draft
# =============================================================================

# Max upload size — guard the form. Configurable via /admin/reply-box later.
MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20 MB


@app.post("/api/tickets/{ticket_id}/upload-attachment")
async def upload_reply_attachment(
    ticket_id: int, request: Request,
    user: dict = Depends(auth_mod.require_any("tickets.public_reply", "tickets.internal_note")),
):
    """Multipart file upload. Stores the file on disk and registers a row in
    ticket_attachments with source='native'. Returns the new attachment id so the
    UI can chip it onto the next reply submission."""
    form = await request.form()
    f = form.get("file")
    if not f or not getattr(f, "filename", ""):
        raise HTTPException(400, "no file uploaded")
    data = await f.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"file too large ({len(data)} > {MAX_UPLOAD_BYTES} bytes)")
    base = Path(config.DB_PATH).parent / "attachments" / "native" / str(ticket_id)
    base.mkdir(parents=True, exist_ok=True)
    safe = "".join(ch for ch in f.filename if ch.isalnum() or ch in "._- ").strip() or "upload.bin"
    import uuid as _uuid
    dest = base / f"{_uuid.uuid4().hex[:8]}_{safe}"
    with open(dest, "wb") as fh:
        fh.write(data)
    rel = str(dest.relative_to(Path(config.DB_PATH).parent))
    with db.conn() as c:
        att_id = db.insert_native_attachment(
            c, ticket_id=ticket_id, comment_id=None,
            file_name=f.filename, content_type=f.content_type or "application/octet-stream",
            size_bytes=len(data), local_path=rel,
        )
        db.audit(c, actor=user["email"], action="upload_attachment",
                 target_type="ticket", target_id=str(ticket_id),
                 detail=f"name={f.filename} bytes={len(data)} att_id={att_id}")
    return JSONResponse({
        "ok": True, "id": att_id, "file_name": f.filename,
        "content_type": f.content_type, "size_bytes": len(data),
    })


@app.post("/api/tickets/{ticket_id}/reply")
async def post_reply(
    ticket_id: int, request: Request,
    body: str = Form(...),
    mode: str = Form("public"),               # 'public' | 'internal'
    body_format: str = Form("markdown"),      # 'plain' | 'markdown'
    attachment_ids: str = Form(""),           # comma-separated negative ids from /upload-attachment
    source_language: str = Form(""),          # if the draft was translated, capture original lang
    user: dict = Depends(auth_mod.require_any("tickets.public_reply", "tickets.internal_note")),
):
    """Save a native comment on this ticket. Stays entirely local (Block #1
    policy — no Zendesk write). Attachments uploaded via /upload-attachment are
    associated to the new comment id."""
    # Mode-specific permission check: a user holding only internal_note can
    # still hit this endpoint, but only with mode='internal'.
    if mode == "public" and "tickets.public_reply" not in user["permissions"]:
        raise HTTPException(403, "Missing permission: tickets.public_reply (needed to post a public reply)")
    if mode == "internal" and "tickets.internal_note" not in user["permissions"]:
        raise HTTPException(403, "Missing permission: tickets.internal_note (needed to post an internal note)")
    if mode not in ("public", "internal"):
        raise HTTPException(400, "mode must be 'public' or 'internal'")
    if body_format not in ("plain", "markdown"):
        body_format = "markdown"
    body = (body or "").strip()
    if not body and not attachment_ids:
        raise HTTPException(400, "empty reply")
    # Resolve the author. If the agent doesn't have a users row yet (e.g. dev
    # mode with auth off), create a placeholder under their email.
    with db.conn() as c:
        u = c.execute("SELECT id FROM users WHERE LOWER(email)=LOWER(?)", (user["email"],)).fetchone()
        if u:
            author_id = u["id"]
        else:
            seq = int(db.get_meta(c, "next_native_user_seq") or "1")
            db.set_meta(c, "next_native_user_seq", str(seq + 1))
            author_id = 1_000_000_000 + seq
            c.execute("INSERT INTO users (id, name, email, role, raw) VALUES (?, ?, ?, 'agent', ?)",
                      (author_id, user.get("name") or user["email"], user["email"],
                       json.dumps({"source": "native_login"})))
        cid = db.insert_native_comment(
            c, ticket_id=ticket_id, author_id=author_id,
            body=body, public=(mode == "public"),
            body_format=body_format,
            meta={"source_language": source_language} if source_language else {},
        )
        # Attach any pre-uploaded files
        att_id_list = [int(x) for x in attachment_ids.split(",") if x.strip().lstrip("-").isdigit()]
        for aid in att_id_list:
            c.execute("UPDATE ticket_attachments SET comment_id=? WHERE id=? AND ticket_id=?",
                      (cid, aid, ticket_id))
        # Bump the ticket's updated_at so list views surface it
        c.execute("UPDATE tickets SET updated_at=? WHERE id=?", (db.now_iso(), ticket_id))
        db.audit(c, actor=user["email"], action=f"reply_{mode}_local",
                 target_type="ticket", target_id=str(ticket_id),
                 detail=f"comment_id={cid} format={body_format} attachments={len(att_id_list)}")
        # Per-ticket timeline
        snippet = (body[:80] + ("…" if len(body) > 80 else "")) if body else "(empty)"
        ev_type = "comment.public" if mode == "public" else "note.added"
        db.audit_ticket(c, ticket_id=ticket_id, event_type=ev_type,
                        event_summary=("Public reply: " if mode == "public" else "Internal note: ") + snippet,
                        actor_email=user["email"], actor_type="agent",
                        raw={"comment_id": cid, "attachments_linked": len(att_id_list)})
        # Fire triggers — both the generic comment event and the role-specific one
        try:
            from .. import rules_engine
            rules_engine.dispatch_event(c, "comment.from_agent", ticket_id, actor_email=user["email"])
            if mode == "public":
                rules_engine.dispatch_event(c, "comment.public_added", ticket_id, actor_email=user["email"])
            else:
                rules_engine.dispatch_event(c, "note.added", ticket_id, actor_email=user["email"])
        except Exception as e:
            print(f"rules dispatch failed: {e}")
    return JSONResponse({"ok": True, "comment_id": cid, "mode": mode,
                         "attachments_linked": len(att_id_list)})


# Supported translate languages — used in the reply box dropdown.
TRANSLATE_LANGUAGES = [
    {"code": "en", "name": "English"},
    {"code": "hi", "name": "Hindi (हिन्दी)"},
    {"code": "ta", "name": "Tamil (தமிழ்)"},
    {"code": "te", "name": "Telugu (తెలుగు)"},
    {"code": "kn", "name": "Kannada (ಕನ್ನಡ)"},
    {"code": "mr", "name": "Marathi (मराठी)"},
    {"code": "bn", "name": "Bengali (বাংলা)"},
    {"code": "gu", "name": "Gujarati (ગુજરાતી)"},
    {"code": "pa", "name": "Punjabi (ਪੰਜਾਬੀ)"},
]


@app.post("/api/tickets/{ticket_id}/translate-draft")
async def translate_draft(
    ticket_id: int, request: Request,
    draft: str = Form(...),
    target_lang: str = Form("en"),
    user: dict = Depends(auth_mod.require("ai.translate")),
):
    """Translate the agent's draft to target_lang via Claude (MCP if available,
    falls back to the ai module). Returns the translated text and detected source."""
    from .. import ai as _ai
    valid = {l["code"] for l in TRANSLATE_LANGUAGES}
    if target_lang not in valid:
        raise HTTPException(400, f"unsupported language: {target_lang}")
    if not (draft or "").strip():
        raise HTTPException(400, "draft is empty — type something first")
    try:
        out = _ai.translate(draft, target_lang)
    except AttributeError:
        # ai.translate not present in some test envs — minimal inline fallback
        out = {"translated": draft, "source_lang": "unknown", "model": "noop",
               "input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0, "cost_usd": 0}
    except RuntimeError as e:
        # Our own thrown error — message is already clean
        raise HTTPException(502, str(e))
    except Exception as e:
        raise HTTPException(502, f"translate failed: {type(e).__name__}: {e}")
    with db.conn() as c:
        db.log_spend(c, ticket_id=ticket_id, cost=out.get("cost_usd", 0),
                     in_tok=out.get("input_tokens", 0), out_tok=out.get("output_tokens", 0),
                     cached_tok=out.get("cached_input_tokens", 0), model=out.get("model", ""))
    return JSONResponse({
        "ok": True,
        "translated": out["translated"],
        "source_lang": out.get("source_lang"),
        "target_lang": target_lang,
        # Surface the actual path used so the UI can warn if it's the slow CLI fallback
        "model_used": out.get("model"),
        "via_cli_fallback": (out.get("model") == "claude-code-cli"),
    })


@app.post("/api/tickets/{ticket_id}/smart-reply")
async def smart_reply(
    ticket_id: int, request: Request,
    draft: str = Form(""),
    user: dict = Depends(auth_mod.require_any("tickets.public_reply", "tickets.internal_note")),
):
    """Run Claude on the full conversation + the agent's current draft.
    Returns an improved suggested reply."""
    from .. import ai as _ai
    with db.conn() as c:
        t = c.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()
        if not t:
            raise HTTPException(404, "Ticket not found")
        try:
            result = _ai.suggest_reply(c, ticket_id, draft)
        except Exception as e:
            raise HTTPException(502, f"AI call failed: {e}")
        db.log_spend(c, ticket_id=ticket_id, cost=result.get("cost_usd", 0),
                     in_tok=result.get("input_tokens", 0), out_tok=result.get("output_tokens", 0),
                     cached_tok=result.get("cached_input_tokens", 0), model=result.get("model", ""))
    return JSONResponse(result["reply"])


@app.post("/api/tickets/{ticket_id}/generate-doc")
async def generate_doc(
    ticket_id: int, request: Request,
    user: dict = Depends(auth_mod.require("ai.translate")),
):
    """Generate a full Confluence-ready markdown document from the ticket history."""
    from .. import ai as _ai
    with db.conn() as c:
        t = c.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()
        if not t:
            raise HTTPException(404, "Ticket not found")
        try:
            result = _ai.generate_doc(c, ticket_id)
        except Exception as e:
            raise HTTPException(502, f"AI call failed: {e}")
        db.log_spend(c, ticket_id=ticket_id, cost=result.get("cost_usd", 0),
                     in_tok=result.get("input_tokens", 0), out_tok=result.get("output_tokens", 0),
                     cached_tok=result.get("cached_input_tokens", 0), model=result.get("model", ""))
    return JSONResponse({"markdown": result["markdown"]})


# =============================================================================
# Leadership Dashboard
# =============================================================================

# Catalogue of metric definitions a KPI widget can choose from.
KPI_METRICS = {
    "open_tickets":      {"label": "Open tickets",        "sql": "SELECT COUNT(*) AS n FROM tickets WHERE status IN ('new','open','pending','hold')"},
    "untouched_4h":      {"label": "Untouched > 4h",      "sql": "SELECT COUNT(*) AS n FROM tickets WHERE status IN ('new','open') AND assignee_id IS NULL AND created_at < datetime('now','-4 hours')"},
    "sla_at_risk":       {"label": "SLA at risk (>8h)",   "sql": f"SELECT COUNT(*) AS n FROM tickets WHERE status IN ('new','open') AND created_at < datetime('now','-8 hours') AND {_NO_AGENT_REPLY_CLAUSE}"},
    "sla_breached":      {"label": "SLA breached (>24h)", "sql": f"SELECT COUNT(*) AS n FROM tickets WHERE status IN ('new','open') AND created_at < datetime('now','-24 hours') AND {_NO_AGENT_REPLY_CLAUSE}"},
    "solved_24h":        {"label": "Solved last 24h",     "sql": "SELECT COUNT(*) AS n FROM tickets WHERE status='solved' AND solved_at > datetime('now','-1 day')"},
    "solved_7d":         {"label": "Solved last 7d",      "sql": "SELECT COUNT(*) AS n FROM tickets WHERE status='solved' AND solved_at > datetime('now','-7 days')"},
    "with_ai_correction":{"label": "Has AI corrections",  "sql": "SELECT COUNT(*) AS n FROM ticket_insights WHERE recommendations != '[]' AND ticket_id IN (SELECT id FROM tickets WHERE status IN ('new','open','pending','hold'))"},
    "missing_kb":        {"label": "Missing KB Article",  "sql": f"SELECT COUNT(*) AS n FROM tickets WHERE status IN ('new','open','pending','hold','solved') AND (json_extract(custom_fields,'$.\"{FID_KB_ARTICLE}\"') IS NULL OR json_extract(custom_fields,'$.\"{FID_KB_ARTICLE}\"') IN ('','NA','na'))"},
    "feedback_approved": {"label": "AI suggestions approved", "sql": "SELECT COUNT(*) AS n FROM ai_feedback WHERE decision='approved'"},
    "feedback_rejected": {"label": "AI suggestions rejected", "sql": "SELECT COUNT(*) AS n FROM ai_feedback WHERE decision='rejected'"},
    "claude_spend_mtd":  {"label": "Claude spend MTD ($)", "sql": "SELECT COALESCE(ROUND(SUM(cost_usd),4), 0) AS n FROM spend_log WHERE created_at >= datetime('now','start of month')"},
}


GROUP_BY_OPTIONS = {
    "customer":  {"label": "Customer",        "expr": f"json_extract(custom_fields,'$.\"{FID_CUSTOMER}\"')",  "join_lookup": ("ticket_fields", FID_CUSTOMER)},
    "status":    {"label": "Status",          "expr": "status", "join_lookup": None},
    "priority":  {"label": "Priority",        "expr": "priority", "join_lookup": None},
    "group":     {"label": "Group",           "expr": "(SELECT name FROM groups WHERE id = group_id)", "join_lookup": None},
    "assignee":  {"label": "Assignee",        "expr": "(SELECT name FROM users WHERE id = assignee_id)", "join_lookup": None},
    "rc1":       {"label": "Root Cause L1",   "expr": f"json_extract(custom_fields,'$.\"{FID_RC1}\"')", "join_lookup": ("ticket_fields", FID_RC1)},
    "rc2":       {"label": "Root Cause L2",   "expr": f"json_extract(custom_fields,'$.\"{FID_RC2}\"')", "join_lookup": ("ticket_fields", FID_RC2)},
    "module":    {"label": "Module",          "expr": f"json_extract(custom_fields,'$.\"{FID_MODULE}\"')", "join_lookup": ("ticket_fields", FID_MODULE)},
    "product":   {"label": "Product",         "expr": f"json_extract(custom_fields,'$.\"{FID_PRODUCT}\"')", "join_lookup": ("ticket_fields", FID_PRODUCT)},
    "bucketization": {"label": "Bucketization", "expr": f"json_extract(custom_fields,'$.\"{FID_BUCKETIZATION}\"')", "join_lookup": ("ticket_fields", FID_BUCKETIZATION)},
}


STATUS_FILTERS = {
    "open":     ("Open / new / pending / hold", "status IN ('new','open','pending','hold')"),
    "untouched":("Untouched (unassigned, no agent reply)", f"status IN ('new','open') AND assignee_id IS NULL AND {_NO_AGENT_REPLY_CLAUSE}"),
    "solved":   ("Solved", "status='solved'"),
    "all":      ("All tickets", "1=1"),
}


def _resolve_lookup(c: sqlite3.Connection, field_id_str: str, value: str | None) -> str:
    if not value:
        return value or "(empty)"
    f = c.execute("SELECT options FROM ticket_fields WHERE id=?", (int(field_id_str),)).fetchone()
    if not f:
        return value
    for o in json.loads(f["options"] or "[]"):
        if o.get("value") == value:
            return o.get("name") or value
    return value


def _compute_widget_data(c: sqlite3.Connection, w: dict) -> dict:
    cfg = json.loads(w.get("config") or "{}")
    wtype = w.get("widget_type")
    if wtype == "kpi":
        m = cfg.get("metric") or "open_tickets"
        meta = KPI_METRICS.get(m) or KPI_METRICS["open_tickets"]
        try:
            row = c.execute(meta["sql"]).fetchone()
            return {"type": "kpi", "label": cfg.get("label_override") or meta["label"], "value": row["n"]}
        except Exception as e:
            return {"type": "kpi", "label": meta["label"], "value": 0, "error": str(e)}

    if wtype == "group_table":
        gb_key = cfg.get("group_by") or "customer"
        gb = GROUP_BY_OPTIONS.get(gb_key) or GROUP_BY_OPTIONS["customer"]
        status_filter = STATUS_FILTERS.get(cfg.get("status") or "open", STATUS_FILTERS["open"])[1]
        limit = int(cfg.get("limit") or 10)
        sql = f"""
            SELECT {gb['expr']} AS k, COUNT(*) AS n
            FROM tickets
            WHERE {status_filter}
            GROUP BY k
            ORDER BY n DESC
            LIMIT {limit}
        """
        rows = c.execute(sql).fetchall()
        out = []
        for r in rows:
            label = r["k"]
            if gb["join_lookup"]:
                _, fid = gb["join_lookup"]
                label = _resolve_lookup(c, fid, r["k"])
            out.append({"key": label or "(empty)", "count": r["n"]})
        return {"type": "group_table", "rows": out, "group_by_label": gb["label"], "status_label": STATUS_FILTERS[cfg.get("status","open")][0]}

    if wtype == "list":
        status_filter = STATUS_FILTERS.get(cfg.get("status") or "open", STATUS_FILTERS["open"])[1]
        limit = int(cfg.get("limit") or 10)
        sort = cfg.get("sort") or "updated_desc"
        order = SORT_KEYS.get(sort, SORT_KEYS["updated_desc"])
        rows = c.execute(f"SELECT id, subject, status, priority, created_at, updated_at FROM tickets WHERE {status_filter} ORDER BY {order} LIMIT {limit}").fetchall()
        return {"type": "list", "rows": [dict(r) for r in rows], "status_label": STATUS_FILTERS[cfg.get("status","open")][0]}

    return {"type": "unknown", "error": f"unknown widget_type: {wtype}"}


DEFAULT_WIDGETS = [
    {"title": "Open tickets",               "widget_type": "kpi",         "config": {"metric": "open_tickets"}},
    {"title": "Untouched > 4h",             "widget_type": "kpi",         "config": {"metric": "untouched_4h"}},
    {"title": "SLA breached",               "widget_type": "kpi",         "config": {"metric": "sla_breached"}},
    {"title": "Solved last 7d",             "widget_type": "kpi",         "config": {"metric": "solved_7d"}},
    {"title": "Tickets by customer (open)", "widget_type": "group_table", "config": {"group_by": "customer", "status": "open", "limit": 10}},
    {"title": "Tickets by Root Cause L1 (last 7d solved)", "widget_type": "group_table", "config": {"group_by": "rc1", "status": "solved", "limit": 10}},
    {"title": "Top untouched tickets",      "widget_type": "list",        "config": {"status": "untouched", "limit": 10, "sort": "created_asc"}},
]


def _seed_default_widgets(c: sqlite3.Connection) -> None:
    n = c.execute("SELECT COUNT(*) AS n FROM dashboard_widgets").fetchone()["n"]
    if n > 0:
        return
    for i, w in enumerate(DEFAULT_WIDGETS):
        c.execute("""
            INSERT INTO dashboard_widgets (title, widget_type, config, position, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (w["title"], w["widget_type"], json.dumps(w["config"]), i, "system", db.now_iso()))


@app.get("/leadership", response_class=HTMLResponse)
async def leadership_dashboard(request: Request, user: dict = Depends(require_user)):
    with db.conn() as c:
        _seed_default_widgets(c)
        widgets = c.execute("SELECT * FROM dashboard_widgets ORDER BY position, id").fetchall()
        widgets_with_data = []
        for w in widgets:
            wdict = dict(w)
            wdict["config_obj"] = json.loads(w["config"] or "{}")
            wdict["data"] = _compute_widget_data(c, wdict)
            widgets_with_data.append(wdict)
        sb = _sidebar_ctx(c, user)
    return TEMPLATES.TemplateResponse("leadership.html", {
        "request": request, "user": user,
        "widgets": widgets_with_data,
        "kpi_metrics": KPI_METRICS,
        "group_by_options": GROUP_BY_OPTIONS,
        "status_filters": STATUS_FILTERS,
        "sort_keys": SORT_KEYS,
        "current_view": "_leadership", "in_detail": False, "search": "",
        **sb,
    })


@app.post("/api/dashboard/widgets")
async def create_widget(
    title: str = Form(...),
    widget_type: str = Form(...),
    config: str = Form("{}"),
    user: dict = Depends(require_user),
):
    if widget_type not in ("kpi", "group_table", "list"):
        raise HTTPException(400, f"Bad widget_type: {widget_type}")
    try:
        json.loads(config)  # validate
    except Exception:
        raise HTTPException(400, "config must be valid JSON")
    with db.conn() as c:
        pos = c.execute("SELECT COALESCE(MAX(position),0)+1 AS p FROM dashboard_widgets").fetchone()["p"]
        c.execute("""
            INSERT INTO dashboard_widgets (title, widget_type, config, position, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (title, widget_type, config, pos, user["email"], db.now_iso()))
        wid = c.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    return JSONResponse({"ok": True, "id": wid})


@app.put("/api/dashboard/widgets/{widget_id}")
async def update_widget(
    widget_id: int,
    title: str = Form(...),
    config: str = Form("{}"),
    user: dict = Depends(require_user),
):
    try:
        json.loads(config)
    except Exception:
        raise HTTPException(400, "config must be valid JSON")
    with db.conn() as c:
        c.execute("UPDATE dashboard_widgets SET title=?, config=? WHERE id=?", (title, config, widget_id))
    return JSONResponse({"ok": True})


@app.delete("/api/dashboard/widgets/{widget_id}")
async def delete_widget(widget_id: int, user: dict = Depends(require_user)):
    with db.conn() as c:
        c.execute("DELETE FROM dashboard_widgets WHERE id=?", (widget_id,))
    return JSONResponse({"ok": True})


# =============================================================================
# Admin & Setup Panel (Block #9)
# =============================================================================
#
# The admin panel is the single hub where everything the tool does — what's
# already running and what's still pending — is visible and configurable.
#
# Each feature is one entry in FEATURE_CATALOG with:
#   - key           machine name used in URLs
#   - title         human label
#   - status        'done' | 'partial' | 'pending'
#   - block         which strategic block this implements (e.g. "#1" for native)
#   - category      'platform' | 'workflow' | 'ai' | 'admin' | 'integration'
#   - summary       one-liner of what it does
#   - setup_url     where the setup/config sub-page lives (None if no config UI yet)
#   - stat_fn       optional callable(c) -> str returning a live status line
#                   ("1,247 attachments synced", "Last sync 2m ago", etc.)
#
# When a pending feature lands, we change its status to 'done' and wire its
# setup_url. The catalog drives both the admin landing and the nav.


def _stat_ticket_count(c):
    n = c.execute("SELECT COUNT(*) AS n FROM tickets WHERE source='zendesk'").fetchone()["n"]
    nv = c.execute("SELECT COUNT(*) AS n FROM tickets WHERE source='native'").fetchone()["n"]
    return f"{n:,} synced from Zendesk · {nv:,} native"


def _stat_last_sync(c):
    return db.get_meta(c, "last_sync_run_at") or "never"


def _stat_ai_runs(c):
    n = c.execute("SELECT COUNT(*) AS n FROM ticket_insights WHERE created_at > datetime('now','-7 days')").fetchone()["n"]
    return f"{n:,} insights in the last 7 days"


def _stat_mcp_status(c):
    n = c.execute("SELECT COUNT(*) AS n FROM ticket_insights WHERE model='claude-desktop-mcp'").fetchone()["n"]
    return f"{n:,} insights via Claude Desktop / MCP (no API spend)"


def _stat_attachments(c):
    try:
        n = c.execute("SELECT COUNT(*) AS n FROM ticket_attachments").fetchone()["n"]
        return f"{n:,} attachments stored"
    except Exception:
        return "table not yet created"


def _stat_users(c):
    a = c.execute("SELECT COUNT(*) AS n FROM users WHERE role IN ('agent','admin')").fetchone()["n"]
    e = c.execute("SELECT COUNT(*) AS n FROM users WHERE role='end-user'").fetchone()["n"]
    return f"{a} agents/admins · {e:,} end-users"


def _stat_groups(c):
    n = c.execute("SELECT COUNT(*) AS n FROM groups").fetchone()["n"]
    return f"{n} groups"


def _stat_forms(c):
    try:
        n = c.execute("SELECT COUNT(*) AS n FROM ticket_forms WHERE active=1").fetchone()["n"]
        return f"{n} forms (from Zendesk, read-only) · native forms engine pending"
    except Exception:
        return "—"


def _stat_feedback(c):
    a = c.execute("SELECT COUNT(*) AS n FROM ai_feedback WHERE decision='approved'").fetchone()["n"]
    r = c.execute("SELECT COUNT(*) AS n FROM ai_feedback WHERE decision='rejected'").fetchone()["n"]
    return f"{a} approvals · {r} rejections"


def _stat_spend(c):
    s = db.month_to_date_spend(c)
    return f"${s:.4f} MTD"


def _stat_sla_policies(c):
    try:
        a = c.execute("SELECT COUNT(*) AS n FROM sla_policies WHERE active=1").fetchone()["n"]
        total = c.execute("SELECT COUNT(*) AS n FROM sla_policies").fetchone()["n"]
        breached = c.execute("""SELECT COUNT(*) AS n FROM ticket_sla
            WHERE first_reply_state='breached' OR next_reply_state='breached' OR resolution_state='breached'""").fetchone()["n"]
        return f"{a} active policy / {total} total · {breached} tickets currently breaching"
    except Exception:
        return "schema not yet migrated"


def _stat_business_hours(c):
    try:
        n = c.execute("SELECT COUNT(*) AS n FROM business_hours").fetchone()["n"]
        d = c.execute("SELECT name FROM business_hours WHERE is_default=1 LIMIT 1").fetchone()
        return f"{n} schedules · default: {d['name'] if d else 'none'}"
    except Exception:
        return "schema not yet migrated"


def _stat_db_size(c):
    try:
        p = c.execute("PRAGMA page_count").fetchone()[0]
        ps = c.execute("PRAGMA page_size").fetchone()[0]
        mb = (p * ps) / 1024 / 1024
        return f"{mb:,.0f} MB · {p:,} pages"
    except Exception:
        return "—"


def _stat_tunnel_state(c):
    """Status pill text for the /admin landing — quick read of tunnel state."""
    try:
        import shutil as _sh
        if not _sh.which("cloudflared"):
            return "⚠ cloudflared not installed"
        # Look for our pid file (don't depend on _detect_tunnel_state being importable here)
        pid_file = config.DATA_DIR / "tunnel.pid"
        if not pid_file.exists():
            return "✓ ready to start"
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)  # check liveness
            url = db.get_meta(c, "tunnel_public_url") or "starting…"
            return f"▶ running · {url[:50]}"
        except (OSError, ValueError, ProcessLookupError):
            return "✓ ready to start"
    except Exception:
        return "—"


def _stat_auth_state(c):
    """Quick read of whether OAuth is configured + how many app users have logged in."""
    try:
        configured = config.AUTH_ENABLED
        logged_in = c.execute(
            "SELECT COUNT(*) AS n FROM app_users WHERE last_login_at IS NOT NULL"
        ).fetchone()["n"]
        if not configured:
            return "⚠ not configured"
        return f"✓ enabled · {logged_in} signed in"
    except Exception:
        return "—"


def _stat_app_users(c):
    """Count active app users for the /admin landing tile."""
    try:
        n = c.execute("SELECT COUNT(*) AS n FROM app_users WHERE status='active'").fetchone()["n"]
        d = c.execute("SELECT COUNT(*) AS n FROM app_users WHERE status='disabled'").fetchone()["n"]
        if d:
            return f"{n} active · {d} disabled"
        return f"{n} active"
    except Exception:
        return "—"


def _stat_roles(c):
    try:
        n = c.execute("SELECT COUNT(*) AS n FROM roles").fetchone()["n"]
        custom = c.execute("SELECT COUNT(*) AS n FROM roles WHERE is_system_default=0").fetchone()["n"]
        if custom:
            return f"{n} roles ({custom} custom)"
        return f"{n} roles (all built-in)"
    except Exception:
        return "—"


def _stat_groups_v2(c):
    """Count of active groups + breakdown native vs ZD-synced."""
    try:
        n = c.execute("SELECT COUNT(*) AS n FROM groups WHERE is_active=1").fetchone()["n"]
        native = c.execute("SELECT COUNT(*) AS n FROM groups WHERE is_active=1 AND is_native=1").fetchone()["n"]
        return f"{n} active ({native} native, {n - native} from ZD)"
    except Exception:
        return "—"


def _slim_sidebar_ctx(c: sqlite3.Connection, user: dict) -> dict:
    """Lightweight sidebar context for pages that don't need view counts /
    customer buckets / spend numbers (e.g. /search). Skips the expensive
    per-view COUNT loop and the customer-bucket aggregation which together
    were ~300-500ms on a 50K-ticket DB."""
    return {
        "view_defs": [], "view_counts": {},
        "system_views": [], "personal_views": [], "shared_views": [],
        "legacy_view_defs": [],
        "customer_buckets": [],
        "spend": 0.0, "budget": config.MONTHLY_BUDGET_USD,
        "last_sync": "—", "rc1_options": [],
    }


def _stat_releases(c):
    try:
        n = c.execute("SELECT COUNT(*) AS n FROM releases").fetchone()["n"]
        last = c.execute("SELECT version FROM releases WHERE is_current=1 LIMIT 1").fetchone()
        if last:
            return f"v{last['version']} · {n} total"
        return f"{n} releases" if n else "no releases yet"
    except Exception:
        return "—"


def _stat_user_automations(c):
    try:
        total = c.execute("SELECT COUNT(*) AS n FROM user_automations").fetchone()["n"]
        active = c.execute("SELECT COUNT(*) AS n FROM user_automations WHERE active=1").fetchone()["n"]
        return f"{active} active · {total} total"
    except Exception:
        return "—"


def _stat_views(c):
    try:
        total = c.execute("SELECT COUNT(*) AS n FROM native_views WHERE active=1").fetchone()["n"]
        sys = c.execute("SELECT COUNT(*) AS n FROM native_views WHERE active=1 AND scope='system'").fetchone()["n"]
        return f"{total} views ({sys} default · {total - sys} custom)"
    except Exception:
        return "—"


FEATURE_CATALOG = [
    # ---- Platform / data layer ----
    {"key": "zd_sync",        "category": "integration", "status": "done",   "block": "Phase 1",
     "title": "Zendesk read-only sync",
     "summary": "Pull tickets, comments, users, groups, fields, forms, metrics, custom statuses. Watermark-based, idempotent.",
     "setup_url": "/admin/zd-sync", "stat_fn": _stat_last_sync},

    {"key": "native_model",   "category": "platform",    "status": "done",   "block": "#1",
     "title": "Native ticket model + BP-NNNNNN IDs",
     "summary": "Tickets created in this tool stay local. Source='native', local_id='BP-000001'. Sync never touches them.",
     "setup_url": "/admin/native", "stat_fn": _stat_ticket_count},

    {"key": "local_overrides","category": "platform",    "status": "done",   "block": "#1",
     "title": "Local overrides (no ZD writes)",
     "summary": "All field edits go to tickets.local_overrides JSON. Sync writes only to custom_fields; agent edits are preserved.",
     "setup_url": None, "stat_fn": None},

    {"key": "perf",           "category": "platform",    "status": "done",   "block": "Polish",
     "title": "Performance indexes + sidebar cache",
     "summary": "Ticket page load dropped from ~60s to <100ms. Indexed users.role, comment.author, status+created, solved_at. NOT-EXISTS rewrite.",
     "setup_url": "/admin/perf", "stat_fn": _stat_db_size},

    {"key": "auth",           "category": "platform",    "status": "done",   "block": "Phase 1",
     "title": "Google SSO setup",
     "summary": "@betterplace.co.in only sign-in via Google. Auto-creates new users as View-only on first login. Click Configure for setup wizard with env-file helper.",
     "setup_url": "/admin/auth", "stat_fn": _stat_auth_state},

    # ---- AI ----
    {"key": "mcp",            "category": "ai",          "status": "done",   "block": "#8",
     "title": "Claude Desktop / MCP server",
     "summary": "FastMCP server exposes 14 tools (search, get_ticket, save_insight, update_field, etc.) to Claude Desktop. No API spend.",
     "setup_url": "/admin/mcp", "stat_fn": _stat_mcp_status},

    {"key": "ai_worker",      "category": "ai",          "status": "done",   "block": "#8",
     "title": "Claude Code AI worker",
     "summary": "Headless `claude -p` loop analyzes unanalyzed tickets using the MCP server. Uses Team OAuth — no metered API.",
     "setup_url": "/admin/ai-worker", "stat_fn": _stat_ai_runs},

    {"key": "ai_feedback",    "category": "ai",          "status": "done",   "block": "Phase 1",
     "title": "AI feedback loop",
     "summary": "Agents approve / reject / edit AI suggestions. Approve writes to tickets.local_overrides. Decisions feed back into prompt.",
     "setup_url": None, "stat_fn": _stat_feedback},

    {"key": "spend_cap",      "category": "ai",          "status": "done",   "block": "Phase 1",
     "title": "AI spend tracking + budget cap",
     "summary": "Every Claude call logged. MTD spend visible in topbar. Worker bails on UsageLimitReached.",
     "setup_url": "/admin/spend", "stat_fn": _stat_spend},

    # ---- Workflow / ticket UI ----
    {"key": "views",          "category": "workflow",    "status": "done",   "block": "Phase 1",
     "title": "Views (Open / On hold / Pending / Untouched / SLA / etc.)",
     "summary": "10 static views + per-customer sub-views. Sort + filter on customer / Jira / RC1 / group.",
     "setup_url": None, "stat_fn": None},

    {"key": "ticket_detail",  "category": "workflow",    "status": "done",   "block": "Polish",
     "title": "Ticket detail page",
     "summary": "WhatsApp-bubble conversation, collapsible fields panel (required→important→other), inline edit, AI panel with feedback.",
     "setup_url": None, "stat_fn": None},

    {"key": "leadership",     "category": "workflow",    "status": "partial","block": "Phase 3",
     "title": "Leadership dashboard",
     "summary": "Configurable widgets (KPI / group-by / list) over ticket data. Default set seeded on first load.",
     "setup_url": "/leadership", "stat_fn": None},

    {"key": "create_ticket",  "category": "workflow",    "status": "done",   "block": "#1",
     "title": "Create native ticket UI",
     "summary": "/tickets/new form. Generates BP-NNNNNN, source='native'. Pre-fills customer + group. AI worker picks it up next cycle.",
     "setup_url": None, "stat_fn": None},

    # ---- Pending: workflow ----
    {"key": "attachments",    "category": "workflow",    "status": "partial","block": "#A1",
     "title": "Pull attachments from Zendesk",
     "summary": "Sync captures comment.attachments[] every pass. Binaries downloaded lazily on first click and cached to data/attachments/. Backfill runs from /admin/attachments.",
     "setup_url": "/admin/attachments", "stat_fn": _stat_attachments},

    {"key": "reply_box",      "category": "workflow",    "status": "done",   "block": "#A2",
     "title": "Reply box upgrades",
     "summary": "Markdown toolbar (B/I/code/list/link), file attachments, 9-language translate via Claude, distinct yellow theme + lock icon on internal notes. Saved as native comments.",
     "setup_url": "/admin/reply-box", "stat_fn": None},


    {"key": "forms",          "category": "workflow",    "status": "done",   "block": "#A3",
     "title": "Forms engine (Zendesk-replica)",
     "summary": "Define native forms, link to groups, pick + order fields, mark required, add conditional visibility rules (eq / neq / set / unset). Native forms override ZD-imported forms when the group matches.",
     "setup_url": "/admin/forms", "stat_fn": _stat_forms},

    {"key": "internal_notes", "category": "workflow",    "status": "done",   "block": "#A2",
     "title": "Internal-note editor",
     "summary": "Distinct yellow background + 🔒 prefix on the reply box. Markdown toolbar (B/I/code/code-block/list/link), attachment chips. Built into the reply-box block.",
     "setup_url": "/admin/reply-box", "stat_fn": None},

    # ---- Pending: SLA & business hours ----
    {"key": "sla",            "category": "workflow",    "status": "done",   "block": "#A4",
     "title": "SLA engine",
     "summary": "Policies match by priority / group / customer with first-reply, next-reply, resolution clocks. Warn at 80% of target, breach at 100%. Surfaces on each ticket's SLA chip.",
     "setup_url": "/admin/sla", "stat_fn": _stat_sla_policies},

    {"key": "business_hours", "category": "admin",       "status": "done",   "block": "#A4",
     "title": "Business hours",
     "summary": "Per-schedule timezone, weekly intervals, holidays. Customer-level overrides on the SLA page. Seeded default: India Mon–Fri 9–18 IST.",
     "setup_url": "/admin/business-hours", "stat_fn": _stat_business_hours},

    # ---- Pending: bigger blocks from instruc.txt ----
    {"key": "gmail_intake",   "category": "integration", "status": "partial","block": "#2",
     "title": "Direct Gmail → ticket intake",
     "summary": "Schema + admin config landed (poll interval, default group, label routing). Watcher process pending — needs GMAIL_CREDENTIALS_JSON before flipping live.",
     "setup_url": "/admin/gmail", "stat_fn": None},

    {"key": "fields_admin",   "category": "admin",       "status": "done",   "block": "#3",
     "title": "Native custom fields admin",
     "summary": "Create / edit / archive native fields with IDs in the 9-billion range so they never collide with ZD's. ZD-synced fields remain read-only.",
     "setup_url": "/admin/fields", "stat_fn": None},

    {"key": "users_admin",    "category": "admin",       "status": "done",   "block": "#4",
     "title": "Agents + availability",
     "summary": "Promote any ZD-synced agent to a native agent. Set availability (online/away/busy/offline), max parallel load, group memberships. Feeds the round-robin engine.",
     "setup_url": "/admin/agents", "stat_fn": _stat_users},

    {"key": "round_robin",    "category": "workflow",    "status": "done",   "block": "#5",
     "title": "Round-robin assignment",
     "summary": "Least-loaded online agent in the matching group, tie-breaking by oldest last-assigned. Respects max-load caps. Test-pick endpoint at /admin/assignment.",
     "setup_url": "/admin/assignment", "stat_fn": None},

    {"key": "automations",    "category": "workflow",    "status": "done",   "block": "#6",
     "title": "Automations engine (rules)",
     "summary": "Trigger (on_create / on_update / on_status_change / time_elapsed) → conditions (field op value, AND'd) → actions (set_field / set_status / assign_agent / send_auto_reply / …).",
     "setup_url": "/admin/automations", "stat_fn": None},

    {"key": "auto_replies",   "category": "workflow",    "status": "done",   "block": "#7",
     "title": "Auto-replies",
     "summary": "Template per group/customer. Fires on create / first-response-late / business-open. Optional business-hours gate. Body supports {{customer}} {{ticket_id}} {{requester_name}} placeholders.",
     "setup_url": "/admin/auto-replies", "stat_fn": None},

    {"key": "groups_admin",   "category": "admin",       "status": "partial","block": "Phase 1",
     "title": "Groups",
     "summary": "Synced from Zendesk read-only. Native group creation comes with the agents block (#4).",
     "setup_url": "/admin/groups", "stat_fn": _stat_groups},

    {"key": "tunnel",         "category": "integration", "status": "done",   "block": "F3",
     "title": "Cloudflare Tunnel",
     "summary": "One-click Quick Tunnel for instant *.trycloudflare.com public URL. Named-tunnel path documented for permanent domains. Start/stop from admin UI.",
     "setup_url": "/admin/tunnel", "stat_fn": _stat_tunnel_state},

    # ----- F0 · Access Control -----
    {"key": "access_users",   "category": "admin",       "status": "done",   "block": "F0",
     "title": "Users",
     "summary": "Invite teammates by email, assign one or more roles, disable accounts. Anyone with @betterplace.co.in can self-signup as View-only on first Google login.",
     "setup_url": "/admin/users", "stat_fn": _stat_app_users},

    {"key": "access_roles",   "category": "admin",       "status": "done",   "block": "F0",
     "title": "Roles & permissions",
     "summary": "Built-in roles: Admin / Agent / View-only. Create custom roles, edit the permission matrix per role. Critical permissions can't be left orphan.",
     "setup_url": "/admin/roles", "stat_fn": _stat_roles},

    {"key": "groups",         "category": "admin",       "status": "done",   "block": "F0+",
     "title": "Groups",
     "summary": "ZD-synced groups + native ones. Add native groups, archive unused. Members are tracked here too — assign app users to groups under /admin/users.",
     "setup_url": "/admin/groups", "stat_fn": _stat_groups_v2},

    {"key": "views",          "category": "workflow",    "status": "done",   "block": "F0+",
     "title": "Saved views",
     "summary": "Default views (Open / Pending / On-Hold per group + Assigned to me) ship out of the box. Anyone with views.create_personal can build their own; views.create_shared lets them share with users or groups.",
     "setup_url": "/admin/views", "stat_fn": _stat_views},

    {"key": "releases",       "category": "admin",       "status": "done",   "block": "F9",
     "title": "Releases & rollback",
     "summary": "Tag each known-good state with a version + DB snapshot. One-command rollback if a deploy breaks something. Footer chip shows live version on every page.",
     "setup_url": "/admin/releases", "stat_fn": _stat_releases},

    {"key": "user_automations","category": "admin",       "status": "done",   "block": "F6",
     "title": "User automations",
     "summary": "Event-driven rules for user lifecycle: auto-online on work day start, idle-during-work warning, role grants, leave triggers, etc. Two defaults seeded — fully editable. Driven by user_scheduler subprocess.",
     "setup_url": "/admin/user-automations", "stat_fn": _stat_user_automations},
]


def _feature_status_counts():
    out = {"done": 0, "partial": 0, "pending": 0}
    for f in FEATURE_CATALOG:
        out[f["status"]] = out.get(f["status"], 0) + 1
    return out


def _require_admin(user: dict) -> None:
    if not user.get("is_admin"):
        raise HTTPException(403, "Admin access required")


@app.get("/admin", response_class=HTMLResponse)
async def admin_index(request: Request, user: dict = Depends(require_user)):
    _require_admin(user)
    with db.conn() as c:
        sb = _sidebar_ctx(c, user)
        # Resolve each feature's live stat (cheap — most are single COUNT)
        features = []
        for f in FEATURE_CATALOG:
            stat = ""
            if f.get("stat_fn"):
                try:
                    stat = f["stat_fn"](c)
                except Exception as e:
                    stat = f"(error: {e})"
            features.append({**f, "stat": stat})
        counts = _feature_status_counts()
    return TEMPLATES.TemplateResponse("admin/index.html", {
        "request": request, "user": user,
        "features": features, "counts": counts,
        "current_view": "_admin", "in_detail": False, "search": "",
        **sb,
    })


# ---- Admin sub-pages (stubs for not-yet-built features so links don't 404) ----

ADMIN_STUB_DESCRIPTIONS = {
    "attachments":     "Pull attachments from Zendesk and store locally.",
    "reply-box":       "Attachment upload + translate + rich-text in the reply box.",
    "forms":           "Forms engine — link forms to groups, conditional field visibility.",
    "sla":             "Define SLA policies (first-reply / next-reply / resolution).",
    "business-hours":  "Business hours per group and per customer. Holiday calendar.",
    "gmail":           "Watch the support Gmail inbox and create native tickets.",
    "fields":          "Create / edit / archive native custom fields.",
    "agents":          "Native agent records + availability state.",
    "assignment":      "Round-robin auto-assignment engine.",
    "automations":     "Time + event triggered rules.",
    "auto-replies":    "Acknowledgement templates fired on ticket create.",
    "tunnel":          "Cloudflare Tunnel for team rollout.",
    "zd-sync":         "View Zendesk sync state, manually trigger sync, set watermark.",
    "native":          "Native ticket sequence + counts.",
    "perf":            "DB indexes, page-load timings, sidebar cache state.",
    "auth":            "Allow-listed emails, admin role assignments.",
    "mcp":             "MCP server config, available tools, Claude Desktop wiring.",
    "ai-worker":       "Claude Code worker model, schedule, queue state.",
    "spend":           "Claude spend log, monthly budget, breakdown by model.",
    "groups":          "Group list (read-only from Zendesk).",
}


# ----- SLA + Business hours admin (Block #A4) -------------------------------

@app.get("/admin/business-hours", response_class=HTMLResponse)
async def admin_business_hours(request: Request, user: dict = Depends(require_user)):
    _require_admin(user)
    from .. import sla as _sla
    with db.conn() as c:
        sb = _sidebar_ctx(c, user)
        _sla.seed_default_business_hours(c)   # idempotent
        bh_list = _sla.list_business_hours(c)
        for b in bh_list:
            try:
                b["weekly_intervals"] = json.loads(b.get("weekly_intervals") or "[]")
            except Exception:
                b["weekly_intervals"] = []
            try:
                b["holidays"] = json.loads(b.get("holidays") or "[]")
            except Exception:
                b["holidays"] = []
        feature = next((f for f in FEATURE_CATALOG if f["key"] == "business_hours"), {})
    return TEMPLATES.TemplateResponse("admin/business_hours.html", {
        "request": request, "user": user, "feature": feature,
        "business_hours": bh_list,
        "current_view": "_admin", "in_detail": False, "search": "",
        **sb,
    })


@app.post("/api/admin/business-hours")
async def admin_bh_save(
    request: Request,
    bh_id: int = Form(0),
    name: str = Form(...),
    description: str = Form(""),
    timezone: str = Form("Asia/Kolkata"),
    is_default: int = Form(0),
    weekly_intervals_json: str = Form("[]"),
    holidays_json: str = Form("[]"),
    user: dict = Depends(require_user),
):
    _require_admin(user)
    from .. import sla as _sla
    try:
        intervals = json.loads(weekly_intervals_json)
        holidays = json.loads(holidays_json)
        assert isinstance(intervals, list) and isinstance(holidays, list)
    except Exception:
        raise HTTPException(400, "weekly_intervals_json + holidays_json must be JSON arrays")
    with db.conn() as c:
        nid = _sla.upsert_business_hours(
            c, bh_id=(bh_id or None), name=name.strip(), description=description.strip(),
            timezone=timezone.strip() or "Asia/Kolkata",
            weekly_intervals=intervals, holidays=holidays,
            is_default=bool(is_default), actor_email=user["email"],
        )
        db.audit(c, actor=user["email"], action="business_hours_upsert",
                 target_type="business_hours", target_id=str(nid),
                 detail=f"name={name} intervals={len(intervals)} holidays={len(holidays)} default={is_default}")
    return JSONResponse({"ok": True, "id": nid})


@app.post("/api/admin/business-hours/{bh_id}/delete")
async def admin_bh_delete(bh_id: int, user: dict = Depends(require_user)):
    _require_admin(user)
    with db.conn() as c:
        c.execute("DELETE FROM business_hours WHERE id=?", (bh_id,))
        db.audit(c, actor=user["email"], action="business_hours_delete",
                 target_type="business_hours", target_id=str(bh_id), detail="")
    return JSONResponse({"ok": True})


@app.get("/admin/sla", response_class=HTMLResponse)
async def admin_sla(request: Request, user: dict = Depends(require_user)):
    _require_admin(user)
    from .. import sla as _sla
    with db.conn() as c:
        sb = _sidebar_ctx(c, user)
        _sla.seed_default_business_hours(c)
        policies = _sla.list_sla_policies(c)
        for p in policies:
            try: p["applies_to"] = json.loads(p.get("applies_to") or "{}")
            except Exception: p["applies_to"] = {}
            try: p["targets"] = json.loads(p.get("targets") or "{}")
            except Exception: p["targets"] = {}
        bh_list = _sla.list_business_hours(c)
        groups = c.execute("SELECT id, name FROM groups ORDER BY name").fetchall()
        # Customer values from the BetterPlace customer field
        cust_field = c.execute("SELECT options FROM ticket_fields WHERE id=15315331275025").fetchone()
        customers = []
        if cust_field:
            try:
                opts = json.loads(cust_field["options"] or "[]")
                customers = sorted(
                    [{"value": o["value"], "name": o["name"]} for o in opts if o.get("value")],
                    key=lambda x: (x["name"] or "").lower())[:200]
            except Exception:
                customers = []
        feature = next((f for f in FEATURE_CATALOG if f["key"] == "sla"), {})
    return TEMPLATES.TemplateResponse("admin/sla.html", {
        "request": request, "user": user, "feature": feature,
        "policies": policies, "business_hours": bh_list,
        "groups": [dict(g) for g in groups], "customers": customers,
        "current_view": "_admin", "in_detail": False, "search": "",
        **sb,
    })


@app.post("/api/admin/sla")
async def admin_sla_save(
    request: Request,
    policy_id: int = Form(0),
    name: str = Form(...),
    description: str = Form(""),
    active: int = Form(1),
    clock_type: str = Form("business"),
    business_hours_id: int = Form(0),
    applies_to_json: str = Form("{}"),
    targets_json: str = Form("{}"),
    position: int = Form(100),
    user: dict = Depends(require_user),
):
    _require_admin(user)
    from .. import sla as _sla
    try:
        applies_to = json.loads(applies_to_json)
        targets = json.loads(targets_json)
        assert isinstance(applies_to, dict) and isinstance(targets, dict)
    except Exception:
        raise HTTPException(400, "applies_to_json and targets_json must be JSON objects")
    if clock_type not in ("business", "calendar"):
        clock_type = "business"
    with db.conn() as c:
        pid = _sla.upsert_sla_policy(
            c, policy_id=(policy_id or None), name=name.strip(),
            description=description.strip(), active=bool(active),
            applies_to=applies_to, targets=targets,
            clock_type=clock_type, business_hours_id=(business_hours_id or None),
            position=position, actor_email=user["email"],
        )
        db.audit(c, actor=user["email"], action="sla_policy_upsert",
                 target_type="sla_policy", target_id=str(pid),
                 detail=f"name={name} clock={clock_type} targets={list(targets.keys())}")
    return JSONResponse({"ok": True, "id": pid})


@app.post("/api/admin/sla/{policy_id}/delete")
async def admin_sla_delete(policy_id: int, user: dict = Depends(require_user)):
    _require_admin(user)
    with db.conn() as c:
        c.execute("DELETE FROM sla_policies WHERE id=?", (policy_id,))
        db.audit(c, actor=user["email"], action="sla_policy_delete",
                 target_type="sla_policy", target_id=str(policy_id), detail="")
    return JSONResponse({"ok": True})


@app.post("/api/admin/customer-business-hours")
async def admin_cust_bh_save(
    customer_value: str = Form(...),
    business_hours_id: int = Form(...),
    user: dict = Depends(require_user),
):
    _require_admin(user)
    with db.conn() as c:
        c.execute("""
            INSERT INTO customer_business_hours (customer_value, business_hours_id)
            VALUES (?, ?)
            ON CONFLICT(customer_value) DO UPDATE SET business_hours_id=excluded.business_hours_id
        """, (customer_value, business_hours_id))
        db.audit(c, actor=user["email"], action="customer_business_hours_set",
                 target_type="customer", target_id=customer_value,
                 detail=f"business_hours_id={business_hours_id}")
    return JSONResponse({"ok": True})


# ----- Forms admin (Block #A3) -----------------------------------------------

@app.get("/admin/forms", response_class=HTMLResponse)
async def admin_forms(request: Request, user: dict = Depends(require_user)):
    _require_admin(user)
    with db.conn() as c:
        sb = _sidebar_ctx(c, user)
        forms = db.list_native_forms(c)
        # Hydrate group names for display
        all_groups = {r["id"]: r["name"] for r in c.execute("SELECT id, name FROM groups").fetchall()}
        for f in forms:
            try:
                gids = json.loads(f.get("group_ids") or "[]")
            except Exception:
                gids = []
            f["groups_labels"] = [all_groups.get(int(g), f"#{g}") for g in gids]
            try:
                f["field_count"] = len(json.loads(f.get("field_ids") or "[]"))
            except Exception:
                f["field_count"] = 0
            try:
                f["required_count"] = len(json.loads(f.get("required_field_ids") or "[]"))
            except Exception:
                f["required_count"] = 0
            f["condition_count"] = c.execute(
                "SELECT COUNT(*) AS n FROM native_form_conditions WHERE form_id=?", (f["id"],)
            ).fetchone()["n"]
        feature = next((fe for fe in FEATURE_CATALOG if fe["key"] == "forms"), {})
    return TEMPLATES.TemplateResponse("admin/forms_index.html", {
        "request": request, "user": user, "feature": feature, "forms": forms,
        "current_view": "_admin", "in_detail": False, "search": "",
        **sb,
    })


@app.get("/admin/forms/new", response_class=HTMLResponse)
@app.get("/admin/forms/{form_id}", response_class=HTMLResponse)
async def admin_form_edit(request: Request, form_id: int | None = None,
                          user: dict = Depends(require_user)):
    _require_admin(user)
    with db.conn() as c:
        sb = _sidebar_ctx(c, user)
        form = db.get_native_form(c, form_id) if form_id else None
        # Pull options too — needed so the conditional-visibility builder can
        # show a dropdown of the source field's values instead of a free-text input.
        all_fields_raw = c.execute("""
            SELECT id, title, type, required, options FROM ticket_fields
            WHERE type NOT IN ('subject','description','status','priority','group','assignee','custom_status','tickettype')
            ORDER BY title
        """).fetchall()
        all_fields = []
        for r in all_fields_raw:
            d = dict(r)
            try:
                d["options"] = json.loads(d.get("options") or "[]")
            except Exception:
                d["options"] = []
            all_fields.append(d)
        all_groups = c.execute("SELECT id, name FROM groups ORDER BY name").fetchall()
    return TEMPLATES.TemplateResponse("admin/forms_edit.html", {
        "request": request, "user": user,
        "form": form, "form_id": form_id,
        "all_fields": all_fields,
        "all_groups": [dict(r) for r in all_groups],
        "current_view": "_admin", "in_detail": False, "search": "",
        **sb,
    })


@app.post("/api/admin/forms")
async def admin_form_save(
    request: Request,
    form_id: int = Form(0),
    name: str = Form(...),
    description: str = Form(""),
    active: int = Form(1),
    group_ids: str = Form(""),               # comma-separated
    field_ids: str = Form(""),               # comma-separated, ordered
    required_field_ids: str = Form(""),
    conditions_json: str = Form("[]"),
    user: dict = Depends(require_user),
):
    _require_admin(user)
    def _ints(csv: str) -> list[int]:
        return [int(x) for x in csv.split(",") if x.strip().lstrip("-").isdigit()]
    try:
        conds = json.loads(conditions_json or "[]")
        assert isinstance(conds, list)
    except Exception:
        raise HTTPException(400, "conditions_json must be a JSON array")
    with db.conn() as c:
        fid = db.upsert_native_form(
            c, form_id=(form_id or None), name=name.strip(),
            description=description.strip(), active=bool(active),
            group_ids=_ints(group_ids), field_ids=_ints(field_ids),
            required_field_ids=_ints(required_field_ids),
            position=0, actor_email=user["email"],
        )
        n_conds = db.replace_form_conditions(c, fid, conds)
        db.audit(c, actor=user["email"], action="form_upsert",
                 target_type="native_form", target_id=str(fid),
                 detail=f"name={name} fields={len(_ints(field_ids))} conds={n_conds}")
    return JSONResponse({"ok": True, "id": fid, "conditions": n_conds})


@app.post("/api/admin/forms/import-from-zd")
async def admin_forms_import(user: dict = Depends(require_user)):
    """Pull every ticket_form from Zendesk and materialize it as a native form.
    Re-running is safe — matched by name 'ZD: <original>' and upserted."""
    _require_admin(user)
    from .. import zd_import
    with db.conn() as c:
        try:
            out = zd_import.import_forms_from_zd(c, actor_email=user["email"])
        except Exception as e:
            raise HTTPException(502, f"import failed: {type(e).__name__}: {e}")
        db.audit(c, actor=user["email"], action="forms_import_from_zd",
                 target_type="native_forms", target_id="*", detail=json.dumps(out))
    return JSONResponse({"ok": True, **out})


@app.post("/api/admin/automations/import-from-zd")
async def admin_automations_import(user: dict = Depends(require_user)):
    """Pull every Zendesk trigger and time-based automation, materialize as
    native automations. Editable here from that point on."""
    _require_admin(user)
    from .. import zd_import
    with db.conn() as c:
        try:
            t_out = zd_import.import_triggers_from_zd(c, actor_email=user["email"])
            a_out = zd_import.import_automations_from_zd(c, actor_email=user["email"])
        except Exception as e:
            raise HTTPException(502, f"import failed: {type(e).__name__}: {e}")
        db.audit(c, actor=user["email"], action="automations_import_from_zd",
                 target_type="automations", target_id="*",
                 detail=json.dumps({"triggers": t_out, "automations": a_out}))
    return JSONResponse({"ok": True, "triggers": t_out, "automations": a_out})


@app.post("/api/admin/forms/{form_id}/delete")
async def admin_form_delete(form_id: int, user: dict = Depends(require_user)):
    _require_admin(user)
    with db.conn() as c:
        c.execute("DELETE FROM native_form_conditions WHERE form_id=?", (form_id,))
        c.execute("DELETE FROM native_forms WHERE id=?", (form_id,))
        db.audit(c, actor=user["email"], action="form_delete",
                 target_type="native_form", target_id=str(form_id), detail="")
    return JSONResponse({"ok": True})


# ----- Reply-box admin --------------------------------------------------------

@app.get("/admin/reply-box", response_class=HTMLResponse)
async def admin_reply_box(request: Request, user: dict = Depends(require_user)):
    _require_admin(user)
    with db.conn() as c:
        sb = _sidebar_ctx(c, user)
        # Stats: native replies + uploads per day for last 14 days
        recent_replies = c.execute("""
            SELECT id, ticket_id, public, body_format, created_at
            FROM ticket_comments WHERE id < 0 ORDER BY id DESC LIMIT 20
        """).fetchall()
        public_count = c.execute("SELECT COUNT(*) AS n FROM ticket_comments WHERE id < 0 AND public=1").fetchone()["n"]
        internal_count = c.execute("SELECT COUNT(*) AS n FROM ticket_comments WHERE id < 0 AND public=0").fetchone()["n"]
        native_attachments = c.execute("SELECT COUNT(*) AS n, COALESCE(SUM(size_bytes),0) AS b FROM ticket_attachments WHERE source='native'").fetchone()
        feature = next((f for f in FEATURE_CATALOG if f["key"] == "reply_box"), {})
    return TEMPLATES.TemplateResponse("admin/reply_box.html", {
        "request": request, "user": user, "feature": feature,
        "recent_replies": [dict(r) for r in recent_replies],
        "public_count": public_count, "internal_count": internal_count,
        "native_attachments": dict(native_attachments),
        "languages": TRANSLATE_LANGUAGES,
        "max_upload_mb": MAX_UPLOAD_BYTES // 1024 // 1024,
        "current_view": "_admin", "in_detail": False, "search": "",
        **sb,
    })


# ----- Attachments admin (real, not a stub) ----------------------------------

@app.get("/admin/attachments", response_class=HTMLResponse)
async def admin_attachments(request: Request, user: dict = Depends(require_user)):
    _require_admin(user)
    with db.conn() as c:
        sb = _sidebar_ctx(c, user)
        summary = db.attachments_summary(c)
        # Sample 20 recent attachments so the agent can sanity-check
        recent = c.execute("""
            SELECT a.id, a.file_name, a.content_type, a.size_bytes,
                   a.ticket_id, a.local_path, a.created_at
            FROM ticket_attachments a
            ORDER BY a.id DESC LIMIT 20
        """).fetchall()
        feature = next((f for f in FEATURE_CATALOG if f["key"] == "attachments"), {})
    return TEMPLATES.TemplateResponse("admin/attachments.html", {
        "request": request, "user": user,
        "feature": feature,
        "summary": summary,
        "recent": [dict(r) for r in recent],
        "current_view": "_admin", "in_detail": False, "search": "",
        **sb,
    })


def _ab_is_running(pid: int | None) -> bool:
    if not pid: return False
    try:
        import os
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


@app.post("/api/admin/attachments/backfill")
async def admin_attachments_backfill(
    request: Request,
    limit: int = Form(0),
    throttle: float = Form(0.2),       # seconds of sleep between tickets
    user: dict = Depends(require_user),
):
    """Spawn the backfill as a detached subprocess so it doesn't block the web
    worker. Progress is written to data/attachment_backfill.heartbeat which the
    UI polls. The pid is stored in `meta` so the Stop endpoint can kill it.

    `throttle` is the inter-ticket sleep — raise it if the UI feels slow while
    backfill is running (default 0.2s = 5 tickets/sec)."""
    _require_admin(user)
    import subprocess, os, sys
    with db.conn() as c:
        prev_pid = db.get_meta(c, "attachment_backfill_pid")
        if prev_pid and _ab_is_running(int(prev_pid)):
            return JSONResponse({"ok": True, "already_running": True, "pid": int(prev_pid)})
    repo_root = Path(__file__).resolve().parent.parent.parent
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["ATTACHMENT_BACKFILL_THROTTLE"] = str(max(0.0, min(5.0, float(throttle))))
    log_path = repo_root / "data" / "attachment_backfill.log"
    log_path.parent.mkdir(exist_ok=True)
    args = [sys.executable, "-u", "-m", "src.sync_worker", "--backfill-attachments"]
    if limit and int(limit) > 0:
        args.append(str(int(limit)))
    try:
        with open(log_path, "ab") as lf:
            lf.write(f"\n=== started {datetime.now(timezone.utc).isoformat()} (limit={limit}) ===\n".encode())
            proc = subprocess.Popen(
                args, cwd=str(repo_root), env=env,
                stdout=lf, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
    except Exception as e:
        raise HTTPException(500, f"could not spawn backfill: {e}")
    with db.conn() as c:
        db.set_meta(c, "attachment_backfill_pid", str(proc.pid))
        db.audit(c, actor=user["email"], action="attachment_backfill_start",
                 target_type="attachment_backfill", target_id=str(proc.pid),
                 detail=f"limit={limit}")
    return JSONResponse({"ok": True, "pid": proc.pid, "log": str(log_path)})


@app.post("/api/admin/attachments/backfill/stop")
async def admin_attachments_backfill_stop(user: dict = Depends(require_user)):
    """Send SIGTERM to the running backfill process group. The worker catches
    KeyboardInterrupt-style termination and writes a final 'stopped' heartbeat."""
    _require_admin(user)
    import os, signal
    with db.conn() as c:
        pid_str = db.get_meta(c, "attachment_backfill_pid")
    pid = int(pid_str) if pid_str and pid_str.isdigit() else None
    if not pid or not _ab_is_running(pid):
        return JSONResponse({"ok": True, "was_running": False})
    try:
        # Kill the whole process group (covers the requests-library threads, etc.)
        try: os.killpg(os.getpgid(pid), signal.SIGTERM)
        except (PermissionError, ProcessLookupError): os.kill(pid, signal.SIGTERM)
    except Exception as e:
        raise HTTPException(500, f"could not stop backfill: {e}")
    with db.conn() as c:
        db.audit(c, actor=user["email"], action="attachment_backfill_stop",
                 target_type="attachment_backfill", target_id=str(pid), detail="")
    return JSONResponse({"ok": True, "was_running": True, "killed_pid": pid})


@app.get("/api/admin/attachments/backfill/status")
async def admin_attachments_backfill_status(user: dict = Depends(require_user)):
    """Read data/attachment_backfill.heartbeat — the progress file the running
    backfill subprocess writes every 25 tickets. Lets the UI show a live progress
    bar without blocking the web worker."""
    _require_admin(user)
    from datetime import datetime, timezone
    hb = Path(config.DB_PATH).parent / "attachment_backfill.heartbeat"
    out: dict = {"ok": True, "present": False, "is_running": False}
    if hb.exists():
        try:
            data = json.loads(hb.read_text())
            out.update({"present": True, **data})
            try:
                ts = datetime.fromisoformat(data["ts"].replace("Z", "+00:00"))
                out["age_seconds"] = int((datetime.now(timezone.utc) - ts).total_seconds())
            except Exception: pass
        except Exception as e:
            out["heartbeat_error"] = str(e)
    with db.conn() as c:
        pid_str = db.get_meta(c, "attachment_backfill_pid")
    if pid_str and pid_str.isdigit():
        pid = int(pid_str)
        out["pid"] = pid
        out["is_running"] = _ab_is_running(pid)
    return JSONResponse(out)


@app.get("/api/attachments/{att_id}/download")
async def download_attachment(att_id: int, user: dict = Depends(require_user)):
    """Stream the attachment binary. If we haven't downloaded it yet, fetch it
    from Zendesk's signed URL and (best-effort) cache to disk."""
    from .. import zendesk
    import os, urllib.parse
    from fastapi.responses import StreamingResponse, FileResponse
    with db.conn() as c:
        a = c.execute("SELECT * FROM ticket_attachments WHERE id=?", (att_id,)).fetchone()
    if not a:
        raise HTTPException(404, "Unknown attachment")
    if a["local_path"]:
        full = Path(config.DB_PATH).parent / a["local_path"]
        if full.exists():
            return FileResponse(str(full), filename=a["file_name"], media_type=a["content_type"] or "application/octet-stream")
    # Fall back to Zendesk content_url — it's signed but short-lived; if expired
    # we hit a 401/403, surface a clear error so the agent re-syncs.
    if not a["content_url"]:
        raise HTTPException(410, "Attachment URL not stored — re-sync the ticket")
    try:
        import requests as _rq
        r = _rq.get(a["content_url"], stream=True, timeout=30,
                    auth=(config.ZD_EMAIL + "/token", config.ZD_TOKEN) if a["content_url"].startswith(f"https://{config.ZD_SUBDOMAIN}.") else None)
        r.raise_for_status()
    except Exception as e:
        raise HTTPException(502, f"Could not fetch from Zendesk: {e}")
    # Cache to disk on the way through so subsequent reads are fast.
    base = Path(config.DB_PATH).parent / "attachments" / str(a["ticket_id"])
    base.mkdir(parents=True, exist_ok=True)
    safe = "".join(ch for ch in (a["file_name"] or f"a{att_id}") if ch.isalnum() or ch in "._- ")
    dest = base / f"{att_id}_{safe}"
    try:
        with open(dest, "wb") as fh:
            for chunk in r.iter_content(chunk_size=64 * 1024):
                fh.write(chunk)
        rel = str(dest.relative_to(Path(config.DB_PATH).parent))
        with db.conn() as c:
            c.execute("UPDATE ticket_attachments SET local_path=?, downloaded_at=? WHERE id=?",
                      (rel, db.now_iso(), att_id))
    except Exception:
        # If write fails (read-only fs, etc.) we still serve the bytes we already pulled.
        pass
    return FileResponse(str(dest), filename=a["file_name"], media_type=a["content_type"] or "application/octet-stream")


# ----- AI Worker admin (configurable, not just informational) ----------------

AI_WORKER_MODELS = [
    {"value": "haiku",   "name": "Claude Haiku 4.5",  "tier": "fast",     "note": "Cheapest, fastest. Good for high-volume batch analysis."},
    {"value": "sonnet",  "name": "Claude Sonnet 4.6", "tier": "balanced", "note": "Recommended default. Strong reasoning at moderate cost."},
    {"value": "opus",    "name": "Claude Opus 4.6",   "tier": "deep",     "note": "Best reasoning. Burns Team quota ~25× faster — use sparingly."},
]


def _ai_worker_is_running(pid: int | None) -> bool:
    """Check if a process with the stored pid is still alive. Cross-platform.
    Returns False if pid is None or the process is gone."""
    if not pid:
        return False
    try:
        import os, signal
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


@app.get("/admin/ai-worker", response_class=HTMLResponse)
async def admin_ai_worker(request: Request, user: dict = Depends(require_user)):
    _require_admin(user)
    with db.conn() as c:
        sb = _sidebar_ctx(c, user)
        cfg = db.get_ai_worker_config(c)
        cfg["is_running"] = _ai_worker_is_running(cfg.get("process_pid"))
        # Snapshot of recent activity for the page
        last_24h = c.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(cost_usd),0) AS c FROM ticket_insights "
            "WHERE created_at > datetime('now','-1 day')"
        ).fetchone()
        last_run = c.execute(
            "SELECT model, created_at, cost_usd FROM ticket_insights ORDER BY id DESC LIMIT 1"
        ).fetchone()
        unanalyzed = c.execute(
            "SELECT COUNT(*) AS n FROM tickets WHERE last_analyzed_updated_at IS NULL "
            "AND status IN ('new','open','pending','hold') AND source='zendesk'"
        ).fetchone()
        # MTD spend split by model
        by_model = c.execute("""
            SELECT model, COUNT(*) AS n, COALESCE(SUM(cost_usd),0) AS c FROM spend_log
            WHERE created_at >= datetime('now','start of month')
            GROUP BY model ORDER BY c DESC
        """).fetchall()
        feature = next((f for f in FEATURE_CATALOG if f["key"] == "ai_worker"), {})
    return TEMPLATES.TemplateResponse("admin/ai_worker.html", {
        "request": request, "user": user, "feature": feature, "cfg": cfg,
        "models": AI_WORKER_MODELS,
        "stats": {
            "last_24h_count": last_24h["n"], "last_24h_cost": last_24h["c"],
            "unanalyzed": unanalyzed["n"],
            "last_run_model": last_run["model"] if last_run else None,
            "last_run_at": last_run["created_at"] if last_run else None,
            "by_model": [dict(r) for r in by_model],
        },
        "current_view": "_admin", "in_detail": False, "search": "",
        **sb,
    })


@app.post("/api/admin/ai-worker/config")
async def admin_ai_worker_save(
    enabled: int = Form(0),
    model: str = Form("sonnet"),
    batch_size: int = Form(10),
    poll_interval_seconds: int = Form(60),
    daily_ticket_cap: int = Form(200),
    use_mcp: int = Form(1),
    user: dict = Depends(require_user),
):
    _require_admin(user)
    if model not in [m["value"] for m in AI_WORKER_MODELS]:
        raise HTTPException(400, f"bad model: {model}")
    with db.conn() as c:
        db.save_ai_worker_config(c,
            enabled=int(bool(enabled)),
            model=model,
            batch_size=max(1, min(int(batch_size), 100)),
            poll_interval_seconds=max(15, int(poll_interval_seconds)),
            daily_ticket_cap=max(0, int(daily_ticket_cap)),
            use_mcp=int(bool(use_mcp)),
        )
        db.audit(c, actor=user["email"], action="ai_worker_config",
                 target_type="ai_worker_config", target_id="1",
                 detail=f"enabled={enabled} model={model} batch={batch_size} mcp={use_mcp}")
    return JSONResponse({"ok": True})


@app.post("/api/admin/ai-worker/start")
async def admin_ai_worker_start(user: dict = Depends(require_user)):
    """Launch the worker loop as a detached subprocess. Stores its pid so we
    can stop it later. Picks up the current /admin/ai-worker config.

    Critical bits for getting live logs:
      - `python -u` + PYTHONUNBUFFERED=1 force unbuffered stdout. Without this
        Python buffers print() in 4KB chunks while writing to a file, so logs
        stay invisible until the buffer fills or the process exits.
      - PATH is augmented with the common npm-global / Homebrew / pipx
        locations so the worker can find the `claude` CLI when spawned from
        uvicorn (which often has a sparse PATH).
    """
    _require_admin(user)
    import subprocess, os, sys, pathlib
    with db.conn() as c:
        cfg = db.get_ai_worker_config(c)
        if _ai_worker_is_running(cfg.get("process_pid")):
            return JSONResponse({"ok": True, "already_running": True, "pid": cfg["process_pid"]})
        repo_root = Path(__file__).resolve().parent.parent.parent
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["CLAUDE_CODE_WORKER_MODEL"] = cfg.get("model", "sonnet")
        env["AI_WORKER_BATCH_SIZE"] = str(cfg.get("batch_size", 10))
        env["AI_WORKER_DAILY_CAP"] = str(cfg.get("daily_ticket_cap", 200))
        env["AI_WORKER_POLL_SECONDS"] = str(cfg.get("poll_interval_seconds", 60))
        # Augment PATH so `claude` CLI is findable even when uvicorn was launched
        # without ~/.npm-global/bin in scope.
        home = pathlib.Path.home()
        extra_paths = [
            str(home / ".npm-global" / "bin"),
            str(home / ".nvm" / "versions" / "node" / "current" / "bin"),
            str(home / ".local" / "bin"),
            "/usr/local/bin",
            "/opt/homebrew/bin",
        ]
        env["PATH"] = ":".join([p for p in extra_paths if Path(p).exists()] + [env.get("PATH", "")])

        # MCP vs metered API
        if cfg.get("use_mcp", 1):
            for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
                env.pop(k, None)
            module = "src.claude_code_worker"
        else:
            module = "src.ai_worker"

        log_path = repo_root / "data" / "ai_worker.log"
        log_path.parent.mkdir(exist_ok=True)
        # Truncate-and-write header so the page always shows current run on top
        try:
            with open(log_path, "ab") as lf:
                lf.write((
                    f"\n=== started {datetime.now(timezone.utc).isoformat()} ===\n"
                    f"    module={module}\n"
                    f"    model={cfg.get('model','sonnet')}\n"
                    f"    batch_size={cfg.get('batch_size',10)}\n"
                    f"    poll_interval_seconds={cfg.get('poll_interval_seconds',60)}\n"
                    f"    daily_ticket_cap={cfg.get('daily_ticket_cap',200)}\n"
                    f"    use_mcp={cfg.get('use_mcp',1)}\n"
                    f"    PATH={env['PATH'][:200]}...\n"
                ).encode())
                lf.flush()
                # -u = force stdin/stdout/stderr to be totally unbuffered. The
                # PYTHONUNBUFFERED env covers stdio that's opened later by the
                # worker; -u covers the boot prints from Python itself.
                proc = subprocess.Popen(
                    [sys.executable, "-u", "-m", module, "--loop"],
                    cwd=str(repo_root), env=env,
                    stdout=lf, stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
        except Exception as e:
            raise HTTPException(500, f"could not spawn worker: {e}")
        db.save_ai_worker_config(c,
            enabled=1, process_pid=proc.pid,
            last_started_at=db.now_iso(), last_stopped_at=None,
        )
        db.audit(c, actor=user["email"], action="ai_worker_start",
                 target_type="ai_worker", target_id=str(proc.pid),
                 detail=f"module={module} model={cfg.get('model')}")
    return JSONResponse({"ok": True, "pid": proc.pid, "log": str(log_path)})


@app.get("/api/admin/ai-worker/heartbeat")
async def admin_ai_worker_heartbeat(user: dict = Depends(require_user)):
    """Read the worker's heartbeat file. Independent of stdout buffering, so
    even if logging stutters the page can still tell the worker is alive."""
    _require_admin(user)
    from datetime import datetime, timezone
    hb_path = Path(config.DB_PATH).parent / "ai_worker.heartbeat"
    if not hb_path.exists():
        return JSONResponse({"ok": True, "present": False})
    try:
        text = hb_path.read_text().strip()
        data = json.loads(text)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})
    # Compute age in seconds
    age = None
    try:
        ts = datetime.fromisoformat(data["ts"].replace("Z", "+00:00"))
        age = int((datetime.now(timezone.utc) - ts).total_seconds())
    except Exception:
        pass
    # Most recent insight, for "is real work happening?"
    with db.conn() as c:
        last_ins = c.execute(
            "SELECT created_at, model FROM ticket_insights ORDER BY id DESC LIMIT 1"
        ).fetchone()
    last_insight_age = None
    if last_ins:
        try:
            t = datetime.fromisoformat(last_ins["created_at"].replace("Z", "+00:00"))
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            last_insight_age = int((datetime.now(timezone.utc) - t).total_seconds())
        except Exception:
            pass
    return JSONResponse({
        "ok": True, "present": True, **data, "age_seconds": age,
        "last_insight_at": last_ins["created_at"] if last_ins else None,
        "last_insight_model": last_ins["model"] if last_ins else None,
        "last_insight_age_seconds": last_insight_age,
    })


@app.get("/api/admin/ai-worker/log")
async def admin_ai_worker_log(lines: int = 200, user: dict = Depends(require_user)):
    """Return the last N lines of data/ai_worker.log so the admin page can
    live-tail without ssh-ing into the box."""
    _require_admin(user)
    log_path = Path(config.DB_PATH).parent / "ai_worker.log"
    if not log_path.exists():
        return JSONResponse({"ok": True, "text": "(no log yet — start the worker)"})
    # tail-style read: jump to last ~64KB which is plenty for 200 lines
    try:
        with open(log_path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            seek_to = max(0, size - 64 * 1024)
            fh.seek(seek_to)
            data = fh.read().decode("utf-8", errors="replace")
        out_lines = data.splitlines()[-lines:]
    except Exception as e:
        return JSONResponse({"ok": False, "text": f"read failed: {e}"})
    return JSONResponse({"ok": True, "text": "\n".join(out_lines), "bytes": size})


@app.post("/api/admin/ai-worker/stop")
async def admin_ai_worker_stop(user: dict = Depends(require_user)):
    _require_admin(user)
    import os, signal
    with db.conn() as c:
        cfg = db.get_ai_worker_config(c)
        pid = cfg.get("process_pid")
    if not pid or not _ai_worker_is_running(pid):
        with db.conn() as c:
            db.save_ai_worker_config(c, process_pid=None, last_stopped_at=db.now_iso(), enabled=0)
        return JSONResponse({"ok": True, "was_running": False})
    try:
        # Send SIGTERM to the process group so we kill child claude-code subprocesses too
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except (PermissionError, ProcessLookupError):
            os.kill(pid, signal.SIGTERM)
    except Exception as e:
        raise HTTPException(500, f"could not stop worker: {e}")
    with db.conn() as c:
        db.save_ai_worker_config(c, process_pid=None, last_stopped_at=db.now_iso(), enabled=0)
        db.audit(c, actor=user["email"], action="ai_worker_stop",
                 target_type="ai_worker", target_id=str(pid), detail="")
    return JSONResponse({"ok": True, "was_running": True, "killed_pid": pid})


# ----- Resync field defs from ZD (used by /admin/fields button) --------------

@app.post("/api/admin/fields/{field_id}/required-override")
async def admin_field_required_override(
    field_id: int, request: Request,
    required: int = Form(...),                # 1 = force required, 0 = force optional, -1 = clear
    user: dict = Depends(require_user),
):
    """Override the required flag on a ZD-synced ticket field. -1 clears to fall
    through to ZD's flag. Stays local — never pushed back to Zendesk."""
    _require_admin(user)
    val = None if int(required) < 0 else (1 if int(required) else 0)
    with db.conn() as c:
        c.execute("UPDATE ticket_fields SET required_override=? WHERE id=?", (val, field_id))
        db.audit(c, actor=user["email"], action="field_required_override",
                 target_type="ticket_field", target_id=str(field_id),
                 detail=f"set required_override={val}")
    return JSONResponse({"ok": True, "field_id": field_id, "required_override": val})


@app.post("/api/admin/resync-custom-statuses")
async def admin_resync_custom_statuses(user: dict = Depends(require_user)):
    """Re-pull just the custom_statuses list from Zendesk. Faster than the
    full Resync-fields and idempotent (ON CONFLICT updates in place)."""
    _require_admin(user)
    from .. import zendesk
    try:
        statuses = zendesk.list_custom_statuses() or []
    except Exception as e:
        raise HTTPException(502, f"Zendesk fetch failed: {e}")
    with db.conn() as c:
        for s in statuses:
            db.upsert_custom_status(c, s)
        db.audit(c, actor=user["email"], action="resync_custom_statuses",
                 target_type="custom_statuses", target_id="*",
                 detail=f"count={len(statuses)}")
        _invalidate_sidebar_cache()
    return JSONResponse({"ok": True, "custom_statuses": len(statuses)})


@app.post("/api/admin/custom-statuses")
async def admin_custom_status_save(
    request: Request,
    status_id: int = Form(0),
    status_category: str = Form("open"),
    agent_label: str = Form(...),
    end_user_label: str = Form(""),
    description: str = Form(""),
    active: int = Form(1),
    user: dict = Depends(require_user),
):
    """Create or edit a custom status. Native statuses get IDs starting at
    9_500_000_000 to never collide with ZD's 14-digit ids. Edits to ZD-synced
    statuses ARE allowed — they stay local (the next ZD sync would overwrite,
    but we mark them so the user knows)."""
    _require_admin(user)
    if status_category not in ("new", "open", "pending", "hold", "solved", "closed"):
        raise HTTPException(400, f"bad category: {status_category}")
    with db.conn() as c:
        if status_id:
            c.execute("""
                UPDATE custom_statuses SET status_category=?, agent_label=?,
                    end_user_label=?, description=?, active=?
                WHERE id=?
            """, (status_category, agent_label.strip(),
                  end_user_label.strip(), description.strip(),
                  1 if active else 0, status_id))
            sid = status_id
        else:
            row = c.execute("SELECT MAX(id) AS m FROM custom_statuses WHERE id > 9000000000").fetchone()
            base = 9_500_000_000
            sid = max(base, (row["m"] or base - 1) + 1)
            c.execute("""
                INSERT INTO custom_statuses (id, status_category, agent_label,
                    end_user_label, description, active, raw)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (sid, status_category, agent_label.strip(),
                  end_user_label.strip(), description.strip(),
                  1 if active else 0,
                  json.dumps({"source": "native", "created_by": user["email"]})))
        db.audit(c, actor=user["email"], action="custom_status_upsert",
                 target_type="custom_status", target_id=str(sid),
                 detail=f"label={agent_label} category={status_category}")
    return JSONResponse({"ok": True, "id": sid})


@app.post("/api/admin/custom-statuses/{sid}/delete")
async def admin_custom_status_delete(sid: int, user: dict = Depends(require_user)):
    _require_admin(user)
    with db.conn() as c:
        c.execute("DELETE FROM custom_statuses WHERE id=?", (sid,))
        db.audit(c, actor=user["email"], action="custom_status_delete",
                 target_type="custom_status", target_id=str(sid), detail="")
    return JSONResponse({"ok": True})


@app.post("/api/admin/resync-fields")
async def admin_resync_fields(user: dict = Depends(require_user)):
    """Pull all ticket_fields from Zendesk and re-upsert. Picks up the
    corrected required-flag handling (agent + portal OR'd). Idempotent.

    Also re-pulls ticket_forms because field display order changes when ZD's
    form definitions change."""
    _require_admin(user)
    from .. import zendesk
    try:
        fields = zendesk.list_ticket_fields()
        forms = zendesk.list_ticket_forms() or []
        statuses = zendesk.list_custom_statuses() or []
    except Exception as e:
        raise HTTPException(502, f"Zendesk fetch failed: {e}")
    with db.conn() as c:
        for f in fields:
            db.upsert_field_def(c, f)
        for f in forms:
            db.upsert_form(c, f)
        for s in statuses:
            db.upsert_custom_status(c, s)
        db.audit(c, actor=user["email"], action="resync_fields",
                 target_type="ticket_fields", target_id="*",
                 detail=f"fields={len(fields)} forms={len(forms)} statuses={len(statuses)}")
        # Bust the sidebar cache so the agent's next page reflects new state.
        _invalidate_sidebar_cache()
    return JSONResponse({"ok": True, "fields": len(fields), "forms": len(forms), "custom_statuses": len(statuses)})


# ----- Native agents admin (Block #4) ----------------------------------------

@app.get("/admin/agents", response_class=HTMLResponse)
async def admin_agents(request: Request, user: dict = Depends(require_user)):
    _require_admin(user)
    with db.conn() as c:
        sb = _sidebar_ctx(c, user)
        agents = db.list_native_agents(c)
        # All users with role 'agent' or 'admin' (from ZD) — for the "promote to native agent" dropdown
        all_agents = c.execute("""
            SELECT id, name, email, role FROM users
            WHERE role IN ('agent','admin') ORDER BY name
        """).fetchall()
        groups = c.execute("SELECT id, name FROM groups ORDER BY name").fetchall()
        feature = next((f for f in FEATURE_CATALOG if f["key"] == "users_admin"), {})
    return TEMPLATES.TemplateResponse("admin/agents.html", {
        "request": request, "user": user, "feature": feature,
        "agents": agents, "all_agents": [dict(r) for r in all_agents],
        "groups": [dict(g) for g in groups],
        "current_view": "_admin", "in_detail": False, "search": "",
        **sb,
    })


@app.post("/api/admin/agents/{user_id}/delete")
async def admin_agents_delete(user_id: int, user: dict = Depends(require_user)):
    """Remove someone from the native-agents roster. The underlying users row
    (ZD-synced) stays; we just drop the native_agents record so they're no
    longer eligible for round-robin / availability tracking."""
    _require_admin(user)
    with db.conn() as c:
        c.execute("DELETE FROM native_agents WHERE user_id=?", (user_id,))
        db.audit(c, actor=user["email"], action="native_agent_remove",
                 target_type="native_agent", target_id=str(user_id),
                 detail="removed from native agents roster")
    return JSONResponse({"ok": True})


@app.post("/api/admin/agents")
async def admin_agents_save(
    user_id: int = Form(...),
    display_name: str = Form(""),
    availability: str = Form("offline"),
    max_open_tickets: int = Form(20),
    group_ids: str = Form(""),
    active: int = Form(1),
    user: dict = Depends(require_user),
):
    _require_admin(user)
    gids = [int(x) for x in group_ids.split(",") if x.strip().isdigit()]
    if availability not in ("online", "away", "busy", "offline"):
        availability = "offline"
    with db.conn() as c:
        db.upsert_native_agent(c, user_id=user_id, display_name=display_name.strip(),
                                availability=availability, max_open_tickets=max_open_tickets,
                                group_ids=gids, active=bool(active))
        db.audit(c, actor=user["email"], action="agent_upsert",
                 target_type="native_agent", target_id=str(user_id),
                 detail=f"avail={availability} groups={gids} active={active}")
    return JSONResponse({"ok": True, "user_id": user_id})


# ----- Round-robin assignment admin (Block #5) -------------------------------

@app.get("/admin/assignment", response_class=HTMLResponse)
async def admin_assignment(request: Request, user: dict = Depends(require_user)):
    _require_admin(user)
    with db.conn() as c:
        sb = _sidebar_ctx(c, user)
        rules = [dict(r) for r in c.execute(
            "SELECT * FROM assignment_rules ORDER BY position, id"
        ).fetchall()]
        groups = c.execute("SELECT id, name FROM groups ORDER BY name").fetchall()
        # Quick eligibility snapshot per group
        eligibility = []
        for g in groups:
            agents = c.execute("""
                SELECT na.user_id, u.name, na.availability,
                       (SELECT COUNT(*) FROM tickets WHERE assignee_id=na.user_id
                          AND status IN ('new','open','pending','hold')) AS load
                FROM native_agents na JOIN users u ON u.id=na.user_id
                WHERE na.active=1
            """).fetchall()
            relevant = []
            for a in agents:
                # Filter by group_ids JSON
                gids_row = c.execute("SELECT group_ids FROM native_agents WHERE user_id=?", (a["user_id"],)).fetchone()
                gids = json.loads(gids_row["group_ids"] or "[]") if gids_row else []
                if g["id"] in gids:
                    relevant.append(dict(a))
            eligibility.append({"group": dict(g), "agents": relevant})
        feature = next((f for f in FEATURE_CATALOG if f["key"] == "round_robin"), {})
    return TEMPLATES.TemplateResponse("admin/assignment.html", {
        "request": request, "user": user, "feature": feature,
        "rules": rules, "groups": [dict(g) for g in groups],
        "eligibility": eligibility,
        "current_view": "_admin", "in_detail": False, "search": "",
        **sb,
    })


@app.post("/api/admin/assignment/rules")
async def admin_assignment_save(
    rule_id: int = Form(0),
    name: str = Form(...),
    scope_group_id: int = Form(0),
    strategy: str = Form("round_robin"),
    only_online: int = Form(1),
    active: int = Form(1),
    position: int = Form(100),
    user: dict = Depends(require_user),
):
    _require_admin(user)
    if strategy not in ("round_robin", "least_loaded", "manual"):
        strategy = "round_robin"
    now = db.now_iso()
    with db.conn() as c:
        if rule_id:
            c.execute("""
                UPDATE assignment_rules SET name=?, scope_group_id=?, strategy=?,
                    only_online=?, active=?, position=?, updated_at=?
                WHERE id=?
            """, (name, scope_group_id or None, strategy, only_online, active, position, now, rule_id))
            rid = rule_id
        else:
            c.execute("""
                INSERT INTO assignment_rules (name, scope_group_id, strategy, only_online,
                    active, position, created_by, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (name, scope_group_id or None, strategy, only_online, active, position,
                  user["email"], now, now))
            rid = c.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        db.audit(c, actor=user["email"], action="assignment_rule_upsert",
                 target_type="assignment_rule", target_id=str(rid),
                 detail=f"name={name} group={scope_group_id} strategy={strategy}")
    return JSONResponse({"ok": True, "id": rid})


@app.post("/api/admin/assignment/test")
async def admin_assignment_test(group_id: int = Form(...),
                                user: dict = Depends(require_user)):
    """Dry-run pick for the given group — useful for the admin page sanity check."""
    _require_admin(user)
    with db.conn() as c:
        uid = db.pick_next_agent_for_group(c, group_id)
        if not uid:
            return JSONResponse({"ok": True, "picked": None,
                                 "reason": "no eligible online agent for this group"})
        u = c.execute("SELECT id, name, email FROM users WHERE id=?", (uid,)).fetchone()
    return JSONResponse({"ok": True, "picked": dict(u) if u else None})


# ----- Automations admin (Block #6) ------------------------------------------

@app.get("/admin/automations", response_class=HTMLResponse)
async def admin_automations(request: Request, category: str = "trigger",
                            user: dict = Depends(require_user)):
    """Visual rule builder. Two categories (selectable via ?category=…):
       - trigger   : event-driven rules (fires on ticket events)
       - scheduler : time-driven rules (fires on schedule / interval)
    """
    _require_admin(user)
    from .. import automations_catalog as cat
    if category not in ("trigger", "scheduler"):
        category = "trigger"
    with db.conn() as c:
        sb = _sidebar_ctx(c, user)
        autos = db.list_automations(c)
        for a in autos:
            try: a["conditions_json"] = json.loads(a.get("conditions_json") or "[]")
            except Exception: a["conditions_json"] = []
            try: a["actions_json"] = json.loads(a.get("actions_json") or "[]")
            except Exception: a["actions_json"] = []
            try: a["trigger_params"] = json.loads(a.get("trigger_params") or "{}")
            except Exception: a["trigger_params"] = {}
            try: a["schedule_json"] = json.loads(a.get("schedule_json") or "{}")
            except Exception: a["schedule_json"] = {}

        # Counts per tab so we can show "Triggers (12)" / "Schedulers (3)"
        cat_counts = {"trigger": 0, "scheduler": 0}
        for a in autos:
            cat_counts[a.get("category") or "trigger"] = cat_counts.get(a.get("category") or "trigger", 0) + 1
        visible_autos = [a for a in autos if (a.get("category") or "trigger") == category]

        # Resource data for the builder dropdowns
        groups = [dict(r) for r in c.execute("SELECT id, name FROM groups ORDER BY name").fetchall()]
        agents = [dict(r) for r in c.execute("""
            SELECT u.id, u.name, u.email FROM users u
            WHERE u.role IN ('agent','admin') ORDER BY u.name
        """).fetchall()]
        custom_statuses = [dict(r) for r in c.execute(
            "SELECT id, agent_label, status_category FROM custom_statuses WHERE active=1 ORDER BY agent_label"
        ).fetchall()]
        # All fields (ZD-synced + native) with options for the option_picker
        all_fields_raw = c.execute("""
            SELECT id, title, type, options FROM ticket_fields
            WHERE type NOT IN ('subject','description','status','priority','group','assignee','custom_status','tickettype')
            ORDER BY title
        """).fetchall()
        all_fields = []
        for r in all_fields_raw:
            d = dict(r)
            try: d["options"] = json.loads(d.get("options") or "[]")
            except Exception: d["options"] = []
            all_fields.append(d)
        try:
            native_fields_raw = c.execute(
                "SELECT id, title, type, options FROM native_fields WHERE active=1 ORDER BY title"
            ).fetchall()
            for r in native_fields_raw:
                d = dict(r)
                try: d["options"] = json.loads(d.get("options") or "[]")
                except Exception: d["options"] = []
                all_fields.append(d)
        except Exception:
            pass
        # Customer options from the BetterPlace customer field
        cust_field = c.execute("SELECT options FROM ticket_fields WHERE id=15315331275025").fetchone()
        customers = []
        if cust_field:
            try:
                opts = json.loads(cust_field["options"] or "[]")
                customers = sorted([{"value": o["value"], "name": o["name"]} for o in opts if o.get("value")],
                                   key=lambda x: (x["name"] or "").lower())
            except Exception:
                customers = []
        native_forms_list = []
        try:
            native_forms_list = [{"id": r["id"], "name": r["name"]} for r in c.execute(
                "SELECT id, name FROM native_forms WHERE active=1 ORDER BY name").fetchall()]
        except Exception:
            pass
        auto_replies_list = []
        try:
            auto_replies_list = [{"id": r["id"], "name": r["name"]} for r in c.execute(
                "SELECT id, name FROM auto_replies WHERE active=1 ORDER BY name").fetchall()]
        except Exception:
            pass
        sla_policies_list = []
        try:
            sla_policies_list = [{"id": r["id"], "name": r["name"]} for r in c.execute(
                "SELECT id, name FROM sla_policies ORDER BY name").fetchall()]
        except Exception:
            pass
        feature = next((f for f in FEATURE_CATALOG if f["key"] == "automations"), {})
    return TEMPLATES.TemplateResponse("admin/automations.html", {
        "request": request, "user": user, "feature": feature,
        "automations": visible_autos, "all_automations": autos,
        "current_category": category, "cat_counts": cat_counts,
        # Catalog
        "trigger_events": cat.TRIGGER_EVENTS,
        "trigger_event_groups": cat.trigger_event_groups(),
        "scheduler_kinds": cat.SCHEDULER_KINDS,
        "condition_fields": cat.CONDITION_FIELDS,
        "condition_field_groups": cat.condition_field_groups(),
        "ops_by_type": cat.OPS_BY_TYPE,
        "action_types": cat.ACTION_TYPES,
        "action_type_groups": cat.action_type_groups(),
        # Resource data
        "groups": groups, "agents": agents, "custom_statuses": custom_statuses,
        "all_fields": all_fields, "customers": customers,
        "native_forms": native_forms_list, "auto_replies": auto_replies_list,
        "sla_policies": sla_policies_list,
        "translate_languages": TRANSLATE_LANGUAGES,
        "current_view": "_admin", "in_detail": False, "search": "",
        **sb,
    })


@app.post("/api/admin/automations")
async def admin_auto_save(
    automation_id: int = Form(0),
    name: str = Form(...),
    description: str = Form(""),
    active: int = Form(1),
    category: str = Form("trigger"),         # 'trigger' | 'scheduler'
    trigger_type: str = Form(""),            # for category=trigger: event key (e.g. 'ticket.created')
    trigger_params_json: str = Form("{}"),
    schedule_json: str = Form("{}"),         # for category=scheduler: {kind, params…}
    conditions_json: str = Form("[]"),
    actions_json: str = Form("[]"),
    position: int = Form(100),
    user: dict = Depends(require_user),
):
    _require_admin(user)
    from .. import automations_catalog as cat
    if category not in ("trigger", "scheduler"):
        raise HTTPException(400, f"bad category: {category}")
    valid_events = {e["key"] for e in cat.TRIGGER_EVENTS}
    if category == "trigger" and trigger_type and trigger_type not in valid_events:
        raise HTTPException(400, f"unknown trigger event: {trigger_type}")
    try:
        params = json.loads(trigger_params_json)
        schedule = json.loads(schedule_json)
        conds  = json.loads(conditions_json)
        acts   = json.loads(actions_json)
        assert isinstance(params, dict) and isinstance(schedule, dict)
        assert isinstance(conds, list) and isinstance(acts, list)
    except Exception:
        raise HTTPException(400, "bad JSON in trigger/schedule/conditions/actions")
    with db.conn() as c:
        # We still use db.upsert_automation but also write category + schedule_json
        aid = db.upsert_automation(
            c, automation_id=(automation_id or None), name=name.strip(),
            description=description.strip(), active=bool(active),
            trigger_type=(trigger_type or ("scheduler" if category == "scheduler" else "on_create")),
            trigger_params=params, conditions=conds, actions=acts,
            position=position, actor_email=user["email"],
        )
        c.execute("UPDATE automations SET category=?, schedule_json=? WHERE id=?",
                  (category, json.dumps(schedule), aid))
        db.audit(c, actor=user["email"], action="automation_upsert",
                 target_type="automation", target_id=str(aid),
                 detail=f"name={name} cat={category} trig={trigger_type} conds={len(conds)} acts={len(acts)}")
        from .. import rules_engine
        rules_engine.invalidate_rules_cache()
    return JSONResponse({"ok": True, "id": aid})


@app.post("/api/admin/automations/{aid}/delete")
async def admin_auto_delete(aid: int, user: dict = Depends(require_user)):
    _require_admin(user)
    with db.conn() as c:
        c.execute("DELETE FROM automations WHERE id=?", (aid,))
        from .. import rules_engine
        rules_engine.invalidate_rules_cache()
    return JSONResponse({"ok": True})


@app.post("/api/admin/automations/reorder")
async def admin_auto_reorder(
    request: Request,
    ordered_ids: str = Form(...),     # comma-separated id list in the new order
    category: str = Form("trigger"),  # only re-positions rules in this category
    user: dict = Depends(require_user),
):
    """Drag-reorder updates positions in bulk. We restamp each id with its
    index × 10 so future inserts can slot in between without renumbering."""
    _require_admin(user)
    ids = [int(x) for x in ordered_ids.split(",") if x.strip().lstrip("-").isdigit()]
    if not ids:
        return JSONResponse({"ok": True, "updated": 0})
    with db.conn() as c:
        for i, aid in enumerate(ids):
            c.execute(
                "UPDATE automations SET position=? WHERE id=? AND COALESCE(category,'trigger')=?",
                (i * 10, aid, category),
            )
        from .. import rules_engine
        rules_engine.invalidate_rules_cache()
        db.audit(c, actor=user["email"], action="automation_reorder",
                 target_type="automation", target_id="*",
                 detail=f"category={category} order={ids[:20]}{'…' if len(ids)>20 else ''}")
    return JSONResponse({"ok": True, "updated": len(ids)})


@app.post("/api/tickets/{ticket_id}/import-zd-audits")
async def import_zd_audits(ticket_id: int, user: dict = Depends(require_user)):
    """Backfill historical events for this ticket from Zendesk's audits API.
    Idempotent — re-running it won't create duplicates."""
    from .. import zd_import
    with db.conn() as c:
        # Permit any logged-in user — audit pull is read-only
        if not c.execute("SELECT 1 FROM tickets WHERE id=?", (ticket_id,)).fetchone():
            raise HTTPException(404, "ticket not found")
        try:
            out = zd_import.import_ticket_audits_from_zd(c, ticket_id)
        except Exception as e:
            raise HTTPException(502, f"ZD audit pull failed: {e}")
        db.audit(c, actor=user["email"], action="zd_audits_import",
                 target_type="ticket", target_id=str(ticket_id), detail=str(out))
    return JSONResponse({"ok": True, **out})


@app.get("/api/placeholders/catalog")
async def placeholders_catalog(user: dict = Depends(require_user)):
    """Return the full placeholder catalog for autocomplete UIs. Cached per request."""
    from .. import placeholders as _ph
    with db.conn() as c:
        items = _ph.catalog(c)
    return JSONResponse({"ok": True, "items": items})


def _reanalyze_spawn(args: list[str], log_filename: str, meta_key: str) -> dict:
    """Spawn the claude_code_worker as a subprocess for re-analyze modes
    (one or bulk). Returns the pid. Used by both single-ticket Re-analyze
    and bulk Re-analyze endpoints.

    Same env-stripping trick as the live AI worker so OAuth/MCP is used
    instead of metered API."""
    import subprocess, os, sys
    repo_root = Path(__file__).resolve().parent.parent.parent
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    # Strip API key so claude-code uses OAuth
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        env.pop(k, None)
    # Pick up the same model setting the live worker uses
    with db.conn() as c:
        cfg = db.get_ai_worker_config(c)
    env["CLAUDE_CODE_WORKER_MODEL"] = cfg.get("model", "sonnet")
    # Extend PATH so `claude` CLI is findable even from uvicorn
    import pathlib
    home = pathlib.Path.home()
    extra_paths = [
        str(home / ".npm-global" / "bin"),
        str(home / ".local" / "bin"),
        "/usr/local/bin",
        "/opt/homebrew/bin",
    ]
    env["PATH"] = ":".join([p for p in extra_paths if Path(p).exists()] + [env.get("PATH", "")])

    log_path = repo_root / "data" / log_filename
    log_path.parent.mkdir(exist_ok=True)
    with open(log_path, "ab") as lf:
        lf.write(f"\n=== started {datetime.now(timezone.utc).isoformat()} {' '.join(args)} ===\n".encode())
        proc = subprocess.Popen(
            [sys.executable, "-u", "-m", "src.claude_code_worker"] + args,
            cwd=str(repo_root), env=env,
            stdout=lf, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    with db.conn() as c:
        db.set_meta(c, meta_key, str(proc.pid))
    return {"pid": proc.pid, "log": str(log_path)}


def _reanalyze_is_running() -> tuple[bool, int | None]:
    with db.conn() as c:
        pid_str = db.get_meta(c, "reanalyze_worker_pid")
    if not pid_str or not pid_str.isdigit():
        return False, None
    pid = int(pid_str)
    return _ai_worker_is_running(pid), pid


@app.post("/api/tickets/{ticket_id}/reanalyze")
async def reanalyze_ticket(ticket_id: int,
                           user: dict = Depends(auth_mod.require("ai.request_reanalyze"))):
    """Spawn a one-shot subprocess that re-analyzes JUST this ticket. Returns
    immediately with the pid — the page polls /api/admin/reanalyze/status to
    watch progress. The web worker does not block."""
    with db.conn() as c:
        row = c.execute("SELECT id FROM tickets WHERE id=?", (ticket_id,)).fetchone()
        if not row:
            raise HTTPException(404, "ticket not found")
        # Reject if either the bulk re-analyze or another single is already in flight
        if _reanalyze_is_running()[0]:
            return JSONResponse(
                {"ok": False, "msg": "Another re-analyze run is already in progress. "
                                     "Wait for it to finish or use Stop on /admin/ai-worker."},
                status_code=409)
        db.audit(c, actor=user["email"], action="reanalyze_one_spawned",
                 target_type="ticket", target_id=str(ticket_id),
                 detail="re-analyze subprocess started")
        db.audit_ticket(c, ticket_id=ticket_id, event_type="ai.queued",
                        event_summary="Re-analysis started (history-aware)",
                        actor_email=user["email"], actor_type="agent")
    try:
        out = _reanalyze_spawn(
            ["--reanalyze-one", str(ticket_id)],
            "reanalyze.log", "reanalyze_worker_pid")
    except Exception as e:
        raise HTTPException(500, f"could not spawn re-analyze worker: {e}")
    return JSONResponse({"ok": True, "ticket_id": ticket_id, "pid": out["pid"],
                          "msg": "Re-analysis spawned. Poll /api/admin/reanalyze/status "
                                 "for progress."})


@app.post("/api/admin/reanalyze/start-bulk")
async def admin_reanalyze_start_bulk(
    request: Request,
    scope: str = Form("legacy_only"),
    limit: int = Form(0),
    throttle: float = Form(2.0),
    user: dict = Depends(require_user),
):
    """Spawn the bulk re-analyze worker subprocess. Replaces the older
    "queue tickets, hope live worker picks them up" flow with a dedicated
    one-shot worker that exits when the queue is drained."""
    _require_admin(user)
    if scope not in ("legacy_only", "open", "all", "no_insight"):
        raise HTTPException(400, f"bad scope: {scope}")
    if _reanalyze_is_running()[0]:
        return JSONResponse({"ok": False, "msg": "A re-analyze run is already in progress."},
                             status_code=409)
    args = ["--reanalyze-bulk", "--reanalyze-scope", scope,
            "--reanalyze-throttle", str(max(0.0, min(60.0, float(throttle))))]
    if limit and int(limit) > 0:
        args += ["--max", str(int(limit))]
    try:
        out = _reanalyze_spawn(args, "reanalyze.log", "reanalyze_worker_pid")
    except Exception as e:
        raise HTTPException(500, f"could not spawn bulk re-analyze: {e}")
    with db.conn() as c:
        db.audit(c, actor=user["email"], action="reanalyze_bulk_spawned",
                 target_type="tickets", target_id="*",
                 detail=f"scope={scope} limit={limit} throttle={throttle} pid={out['pid']}")
    return JSONResponse({"ok": True, "pid": out["pid"], "scope": scope})


@app.post("/api/admin/reanalyze/stop")
async def admin_reanalyze_stop(user: dict = Depends(require_user)):
    """SIGTERM the re-analyze worker (single or bulk — same pid)."""
    _require_admin(user)
    import os, signal
    running, pid = _reanalyze_is_running()
    if not running:
        return JSONResponse({"ok": True, "was_running": False})
    try:
        try: os.killpg(os.getpgid(pid), signal.SIGTERM)
        except (PermissionError, ProcessLookupError): os.kill(pid, signal.SIGTERM)
    except Exception as e:
        raise HTTPException(500, f"could not stop: {e}")
    with db.conn() as c:
        db.audit(c, actor=user["email"], action="reanalyze_stopped",
                 target_type="reanalyze_worker", target_id=str(pid), detail="")
    return JSONResponse({"ok": True, "was_running": True, "killed_pid": pid})


@app.get("/api/admin/reanalyze/status")
async def admin_reanalyze_status(user: dict = Depends(require_user)):
    """Read data/reanalyze.heartbeat — the progress file the subprocess writes
    every 5 tickets. Lets both the per-ticket button and the bulk page poll
    without ssh-ing to the box."""
    _require_admin(user)
    from datetime import datetime, timezone
    hb = Path(config.DB_PATH).parent / "reanalyze.heartbeat"
    out: dict = {"ok": True, "present": False, "is_running": False}
    if hb.exists():
        try:
            data = json.loads(hb.read_text())
            out.update({"present": True, **data})
            try:
                ts = datetime.fromisoformat(data["ts"].replace("Z", "+00:00"))
                out["age_seconds"] = int((datetime.now(timezone.utc) - ts).total_seconds())
            except Exception: pass
        except Exception as e:
            out["heartbeat_error"] = str(e)
    running, pid = _reanalyze_is_running()
    out["is_running"] = running
    if pid: out["pid"] = pid
    return JSONResponse(out)


@app.get("/api/admin/reanalyze/log")
async def admin_reanalyze_log(lines: int = 200, user: dict = Depends(require_user)):
    _require_admin(user)
    log_path = Path(config.DB_PATH).parent / "reanalyze.log"
    if not log_path.exists():
        return JSONResponse({"ok": True, "text": "(no log yet)"})
    try:
        with open(log_path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 64 * 1024))
            data = fh.read().decode("utf-8", errors="replace")
        out_lines = data.splitlines()[-lines:]
    except Exception as e:
        return JSONResponse({"ok": False, "text": f"read failed: {e}"})
    return JSONResponse({"ok": True, "text": "\n".join(out_lines)})


@app.post("/api/admin/reanalyze-bulk")
async def admin_reanalyze_bulk(
    request: Request,
    scope: str = Form("open"),       # 'open' | 'all' | 'legacy_only'
    limit: int = Form(0),
    user: dict = Depends(require_user),
):
    """Bulk-queue tickets for re-analysis. `scope`:
       - open: only status IN ('new','open','pending','hold')
       - all: every ticket
       - legacy_only: tickets whose latest insight predates the history-aware
         prompt (issue_summary IS NULL). Useful for catching up old rows
         without re-doing already-rich ones."""
    _require_admin(user)
    where_parts = []
    if scope == "open":
        where_parts.append("status IN ('new','open','pending','hold')")
    elif scope == "legacy_only":
        # Tickets whose most recent insight has no issue_summary
        where_parts.append("""
            id IN (
                SELECT ti.ticket_id FROM ticket_insights ti
                WHERE ti.id IN (
                    SELECT MAX(id) FROM ticket_insights GROUP BY ticket_id
                ) AND (ti.issue_summary IS NULL OR ti.issue_summary = '')
            )
        """)
    # 'all' adds no filter
    where_sql = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""
    limit_sql = f" LIMIT {int(limit)}" if limit and limit > 0 else ""
    with db.conn() as c:
        n = c.execute(
            f"UPDATE tickets SET last_analyzed_updated_at=NULL "
            f"WHERE id IN (SELECT id FROM tickets{where_sql} ORDER BY updated_at DESC{limit_sql})"
        ).rowcount
        db.audit(c, actor=user["email"], action="reanalyze_bulk_queued",
                 target_type="tickets", target_id="*",
                 detail=f"scope={scope} limit={limit} queued={n}")
    return JSONResponse({"ok": True, "queued": n, "scope": scope})


@app.get("/api/tickets/{ticket_id}/history")
async def ticket_history(ticket_id: int, limit: int = 500, user: dict = Depends(require_user)):
    with db.conn() as c:
        events = db.list_ticket_audit(c, ticket_id, limit=limit)
    return JSONResponse({"ok": True, "events": events, "count": len(events)})


@app.get("/api/admin/tickets/search")
async def admin_ticket_search(q: str = "", limit: int = 30, user: dict = Depends(require_user)):
    """Search tickets for the rule-test picker. Returns id, local_id, subject,
    status, customer_label. Empty q returns recent open tickets so the picker
    is useful even before the user types anything."""
    _require_admin(user)
    q = (q or "").strip()
    with db.conn() as c:
        params: list = []
        if q:
            # Numeric ID, BP-NNNNNN, or substring of subject/local_id
            if q.isdigit():
                where = "id = ?"
                params = [int(q)]
            elif q.upper().startswith("BP-"):
                where = "local_id = ?"
                params = [q.upper()]
            else:
                where = "(CAST(id AS TEXT) LIKE ? OR LOWER(subject) LIKE ? OR local_id LIKE ?)"
                like = f"%{q.lower()}%"
                params = [f"%{q}%", like, f"%{q.upper()}%"]
            sql = f"""
                SELECT id, local_id, subject, status, priority, custom_fields
                FROM tickets WHERE {where}
                ORDER BY updated_at DESC LIMIT ?
            """
            params.append(int(limit))
        else:
            sql = """
                SELECT id, local_id, subject, status, priority, custom_fields
                FROM tickets WHERE status IN ('new','open','pending','hold')
                ORDER BY updated_at DESC LIMIT ?
            """
            params = [int(limit)]
        rows = c.execute(sql, params).fetchall()
        # Resolve customer label
        cust_field = c.execute("SELECT options FROM ticket_fields WHERE id=15315331275025").fetchone()
        names_by_value = {}
        if cust_field:
            try:
                for o in json.loads(cust_field["options"] or "[]"):
                    names_by_value[o.get("value")] = o.get("name") or o.get("value")
            except Exception:
                pass
        out = []
        for r in rows:
            try: cfs = json.loads(r["custom_fields"] or "{}")
            except Exception: cfs = {}
            cust_value = cfs.get("15315331275025")
            out.append({
                "id": r["id"], "local_id": r["local_id"],
                "display_id": r["local_id"] or f"#{r['id']}",
                "subject": (r["subject"] or "")[:120],
                "status": r["status"], "priority": r["priority"],
                "customer": names_by_value.get(cust_value, cust_value or ""),
            })
    return JSONResponse({"ok": True, "tickets": out, "q": q})


@app.post("/api/admin/automations/{aid}/test")
async def admin_auto_test(
    aid: int,
    ticket_id: str = Form(...),
    execute: int = Form(0),                    # 0 = dry-run (visual test), 1 = execute for real
    user: dict = Depends(require_user),
):
    """Run a rule against a specific ticket. Returns a structured breakdown so
    the admin UI can show condition-by-condition pass/fail and what actions
    would (or did) run."""
    _require_admin(user)
    from .. import rules_engine
    with db.conn() as c:
        # Resolve ticket id (accepts numeric or BP-NNNNNN)
        tid = _resolve_ticket_id(c, ticket_id.strip())
        if not tid:
            raise HTTPException(404, f"Ticket not found: {ticket_id}")
        # Load the rule (full row)
        r = c.execute("SELECT * FROM automations WHERE id=?", (aid,)).fetchone()
        if not r:
            raise HTTPException(404, f"Rule {aid} not found")
        rule = dict(r)
        try: rule["conditions"] = json.loads(rule.get("conditions_json") or "[]")
        except Exception: rule["conditions"] = []
        try: rule["actions"]    = json.loads(rule.get("actions_json") or "[]")
        except Exception: rule["actions"] = []
        # Evaluate conditions
        ctx = rules_engine._ticket_context(c, tid)
        all_passed, breakdown = rules_engine.evaluate(c, rule, tid, ctx=ctx)
        # Decorate breakdown with the "action" labels (e.g. "Status equals open")
        for r0 in breakdown:
            r0["op_label"] = _op_label(r0.get("op", ""))
        # If conditions pass, run actions (dry-run or real)
        actions_results = []
        if all_passed:
            actions_results = rules_engine.execute_actions(
                c, rule, tid,
                actor_email=user["email"] if execute else "preview@local",
                dry_run=(not execute),
            )
            # Decorate with friendly labels
            from .. import automations_catalog as cat
            label_by_key = {a["key"]: a["label"] for a in cat.ACTION_TYPES}
            for ar in actions_results:
                ar["label"] = label_by_key.get(ar.get("action"), ar.get("action"))
    return JSONResponse({
        "ok": True,
        "ticket_id": tid,
        "rule_id": aid,
        "rule_name": rule.get("name"),
        "all_conditions_passed": all_passed,
        "conditions": breakdown,
        "actions": actions_results,
        "executed": bool(execute and all_passed),
    })


def _op_label(op: str) -> str:
    labels = {"is":"is","is_not":"is not","in":"is any of","not_in":"is none of",
              "eq":"equals","neq":"not equals","contains":"contains","not_contains":"does not contain",
              "starts_with":"starts with","ends_with":"ends with","regex":"matches regex",
              "is_empty":"is empty","is_not_empty":"is not empty",
              "gt":">","gte":"≥","lt":"<","lte":"≤","between":"between",
              "before":"before","after":"after","within_last":"within last","older_than":"older than",
              "is_set":"is set","is_unset":"is not set",
              "is_true":"is true","is_false":"is false",
              "has":"has","has_any":"has any of","has_all":"has all of","has_none":"has none of",
              "is_current_user":"is current user","is_in_group":"is in ticket group"}
    return labels.get(op, op)


# ----- Auto-replies admin (Block #7) -----------------------------------------

@app.get("/admin/auto-replies", response_class=HTMLResponse)
async def admin_auto_replies(request: Request, user: dict = Depends(require_user)):
    _require_admin(user)
    with db.conn() as c:
        sb = _sidebar_ctx(c, user)
        replies = db.list_auto_replies(c)
        groups = c.execute("SELECT id, name FROM groups ORDER BY name").fetchall()
        from .. import sla as _sla
        bh = _sla.list_business_hours(c)
        feature = next((f for f in FEATURE_CATALOG if f["key"] == "auto_replies"), {})
    return TEMPLATES.TemplateResponse("admin/auto_replies.html", {
        "request": request, "user": user, "feature": feature, "replies": replies,
        "groups": [dict(g) for g in groups], "business_hours": bh,
        "current_view": "_admin", "in_detail": False, "search": "",
        **sb,
    })


@app.post("/api/admin/auto-replies")
async def admin_auto_reply_save(
    reply_id: int = Form(0),
    name: str = Form(...),
    active: int = Form(1),
    scope_group_id: int = Form(0),
    scope_customer_value: str = Form(""),
    body: str = Form(...),
    after_hours: str = Form("always"),
    business_hours_id: int = Form(0),
    fire_on: str = Form("create"),
    user: dict = Depends(require_user),
):
    _require_admin(user)
    if after_hours not in ("always", "business_only", "after_hours_only"):
        after_hours = "always"
    if fire_on not in ("create", "first_response_late", "business_open"):
        fire_on = "create"
    with db.conn() as c:
        rid = db.upsert_auto_reply(
            c, reply_id=(reply_id or None), name=name.strip(),
            active=bool(active), scope_group_id=(scope_group_id or None),
            scope_customer_value=scope_customer_value.strip(),
            body=body.strip(), after_hours=after_hours,
            business_hours_id=(business_hours_id or None),
            fire_on=fire_on, actor_email=user["email"],
        )
    return JSONResponse({"ok": True, "id": rid})


# ----- Native custom fields admin (Block #3) ---------------------------------

NATIVE_FIELD_TYPES = ["text", "textarea", "tagger", "multiselect", "integer", "decimal", "date", "checkbox"]


@app.get("/admin/fields", response_class=HTMLResponse)
async def admin_fields(request: Request, user: dict = Depends(require_user)):
    _require_admin(user)
    with db.conn() as c:
        sb = _sidebar_ctx(c, user)
        nfs = db.list_native_fields(c)
        for f in nfs:
            try: f["options"] = json.loads(f.get("options") or "[]")
            except Exception: f["options"] = []
        # Imported ZD fields — required flag is editable via required_override.
        zd_fields = c.execute("""
            SELECT id, title, type, required, required_override,
                   COALESCE(required_override, required) AS effective_required
            FROM ticket_fields
            ORDER BY effective_required DESC, title LIMIT 200
        """).fetchall()
        # Custom statuses — both ZD-synced and native. Native ids ≥ 9_500_000_000.
        custom_statuses = c.execute("""
            SELECT id, status_category, agent_label, end_user_label, description, active
            FROM custom_statuses
            ORDER BY active DESC, status_category, agent_label
        """).fetchall()
        feature = next((f for f in FEATURE_CATALOG if f["key"] == "fields_admin"), {})
    return TEMPLATES.TemplateResponse("admin/fields.html", {
        "request": request, "user": user, "feature": feature,
        "native_fields": nfs, "zd_fields": [dict(r) for r in zd_fields],
        "custom_statuses": [dict(r) for r in custom_statuses],
        "field_types": NATIVE_FIELD_TYPES,
        "current_view": "_admin", "in_detail": False, "search": "",
        **sb,
    })


@app.post("/api/admin/fields")
async def admin_field_save(
    field_id: int = Form(0),
    title: str = Form(...),
    type_: str = Form("text"),
    required: int = Form(0),
    description: str = Form(""),
    options_json: str = Form("[]"),
    active: int = Form(1),
    position: int = Form(100),
    user: dict = Depends(require_user),
):
    _require_admin(user)
    if type_ not in NATIVE_FIELD_TYPES:
        raise HTTPException(400, f"bad type: {type_}")
    try:
        opts = json.loads(options_json)
        assert isinstance(opts, list)
    except Exception:
        raise HTTPException(400, "options_json must be JSON array")
    with db.conn() as c:
        nid = db.upsert_native_field(
            c, field_id=(field_id or None), title=title.strip(),
            type_=type_, required=bool(required), options=opts,
            description=description.strip(), active=bool(active),
            position=position, actor_email=user["email"],
        )
        db.audit(c, actor=user["email"], action="native_field_upsert",
                 target_type="native_field", target_id=str(nid),
                 detail=f"title={title} type={type_} options={len(opts)}")
    return JSONResponse({"ok": True, "id": nid})


# ----- Gmail intake admin (Block #2) -----------------------------------------

@app.get("/admin/gmail", response_class=HTMLResponse)
async def admin_gmail(request: Request, user: dict = Depends(require_user)):
    _require_admin(user)
    with db.conn() as c:
        sb = _sidebar_ctx(c, user)
        cfg = db.get_gmail_config(c)
        groups = c.execute("SELECT id, name FROM groups ORDER BY name").fetchall()
        threads_count = c.execute("SELECT COUNT(*) AS n FROM gmail_threads").fetchone()["n"]
        feature = next((f for f in FEATURE_CATALOG if f["key"] == "gmail_intake"), {})
    return TEMPLATES.TemplateResponse("admin/gmail.html", {
        "request": request, "user": user, "feature": feature, "cfg": cfg,
        "groups": [dict(g) for g in groups], "threads_count": threads_count,
        "current_view": "_admin", "in_detail": False, "search": "",
        **sb,
    })


@app.post("/api/admin/gmail")
async def admin_gmail_save(
    enabled: int = Form(0),
    mailbox_email: str = Form(""),
    label_in: str = Form("INBOX"),
    label_processed: str = Form("cowork-processed"),
    default_group_id: int = Form(0),
    default_customer_value: str = Form(""),
    poll_interval_seconds: int = Form(60),
    user: dict = Depends(require_user),
):
    _require_admin(user)
    with db.conn() as c:
        db.save_gmail_config(
            c,
            enabled=int(bool(enabled)),
            mailbox_email=mailbox_email.strip(),
            label_in=label_in.strip() or "INBOX",
            label_processed=label_processed.strip() or "cowork-processed",
            default_group_id=(default_group_id or None),
            default_customer_value=default_customer_value.strip(),
            poll_interval_seconds=max(15, int(poll_interval_seconds)),
        )
        db.audit(c, actor=user["email"], action="gmail_config_save",
                 target_type="gmail_config", target_id="1",
                 detail=f"enabled={enabled} mailbox={mailbox_email}")
    return JSONResponse({"ok": True})


# ----- Cloudflare Tunnel admin (Rollout) -------------------------------------

# ===========================================================================
# F3 · /admin/tunnel — Cloudflare tunnel wizard
# ===========================================================================
# Two modes:
#  1. Quick Tunnel (zero-setup): cloudflared tunnel --url http://127.0.0.1:<port>
#     gives you a random *.trycloudflare.com URL in 5-10s. Perfect for soft
#     launching to engineers. Survives until the process is killed.
#  2. Named Tunnel (permanent URL): requires cloudflared login + DNS route.
#     Shown as Advanced; instructions only for now.
#
# The Quick Tunnel runs as a managed subprocess (same pattern as the
# attachments backfill / AI re-analyze workers). Heartbeat file at
# data/tunnel.heartbeat with the current public URL parsed from stdout.

import shutil as _shutil

_TUNNEL_LOG = config.DATA_DIR / "tunnel.log"
_TUNNEL_HEARTBEAT = config.DATA_DIR / "tunnel.heartbeat"
_TUNNEL_PID_FILE = config.DATA_DIR / "tunnel.pid"


def _detect_tunnel_state() -> dict:
    """What's the state of cloudflared + our managed tunnel right now?"""
    bin_path = _shutil.which("cloudflared")
    state = {
        "installed": bool(bin_path),
        "binary_path": bin_path or "",
        "platform": "macOS" if (Path("/usr/bin/sw_vers").exists() or Path("/Applications").exists()) else "Linux",
        "running": False,
        "pid": None,
        "public_url": None,
        "started_at": None,
        "age_seconds": None,
    }
    # Pid file → is it alive?
    pid = None
    if _TUNNEL_PID_FILE.exists():
        try:
            pid = int(_TUNNEL_PID_FILE.read_text().strip())
        except (ValueError, OSError):
            pid = None
    if pid:
        try:
            os.kill(pid, 0)  # signal 0 = check existence
            state["running"] = True
            state["pid"] = pid
        except (ProcessLookupError, PermissionError, OSError):
            state["running"] = False
    # Heartbeat → public URL
    if _TUNNEL_HEARTBEAT.exists():
        try:
            hb = json.loads(_TUNNEL_HEARTBEAT.read_text())
            state["public_url"] = hb.get("public_url")
            state["started_at"] = hb.get("started_at")
            if hb.get("started_at"):
                from datetime import datetime as _dt
                try:
                    started = _dt.fromisoformat(hb["started_at"].replace("Z", "+00:00"))
                    state["age_seconds"] = int((_dt.now(started.tzinfo) - started).total_seconds())
                except (ValueError, TypeError):
                    pass
        except (json.JSONDecodeError, OSError):
            pass
    return state


def _write_tunnel_heartbeat(**fields) -> None:
    _TUNNEL_HEARTBEAT.write_text(json.dumps(fields))


# ===========================================================================
# F6 · /admin/user-automations + endpoints
# ===========================================================================

@app.get("/admin/user-automations", response_class=HTMLResponse)
async def admin_user_automations(
    request: Request,
    user: dict = Depends(auth_mod.require("admin.automations")),
):
    from .. import user_automations_catalog as ucat
    with db.conn() as c:
        sb = _sidebar_ctx(c, user)
        rules = db.list_user_automations(c)
        feature = next((f for f in FEATURE_CATALOG if f["key"] == "user_automations"), {})
        # Stats per rule
        for r in rules:
            try: r["_conditions"] = json.loads(r.get("conditions_json") or "{}")
            except json.JSONDecodeError: r["_conditions"] = {}
            try: r["_actions"] = json.loads(r.get("actions_json") or "[]")
            except json.JSONDecodeError: r["_actions"] = []
        roles_list = [{"id": r["id"], "name": r["name"]} for r in db.list_roles(c)]
        groups_list = [{"id": g["id"], "name": g["name"]} for g in db.list_groups(c, active_only=True)]
    return TEMPLATES.TemplateResponse("admin/user_automations.html", {
        "request": request, "user": user, "feature": feature,
        "rules": rules,
        "trigger_events": ucat.TRIGGER_EVENTS,
        "condition_fields": ucat.CONDITION_FIELDS,
        "ops_by_type": ucat.OPS_BY_TYPE,
        "action_types": ucat.ACTION_TYPES,
        "roles_list": roles_list,
        "groups_list": groups_list,
        "scheduler_state": _user_sched.status(),
        "current_view": "_admin", "in_detail": False, "search": "",
        **sb,
    })


@app.post("/api/admin/user-automations")
async def admin_user_automations_create(
    name: str = Form(...),
    description: str = Form(""),
    trigger_event: str = Form(...),
    conditions_json: str = Form('{"match":"all","rules":[]}'),
    actions_json: str = Form("[]"),
    active: int = Form(1),
    category: str = Form("trigger"),
    interval_minutes: int = Form(5),
    user: dict = Depends(auth_mod.require("admin.automations")),
):
    # Validate JSON
    try:
        json.loads(conditions_json); json.loads(actions_json)
    except json.JSONDecodeError as e:
        raise HTTPException(400, f"Invalid JSON: {e}")
    with db.conn() as c:
        rid = db.upsert_user_automation(
            c, automation_id=None,
            name=name, description=description,
            trigger_event=trigger_event,
            conditions_json=conditions_json,
            actions_json=actions_json,
            active=int(active),
            category=category,
            interval_minutes=int(interval_minutes) if category == "scheduler" else 0,
            actor_email=user["email"],
        )
    from .. import user_rules_engine as ure
    ure.invalidate_rules_cache()
    return JSONResponse({"ok": True, "id": rid})


@app.post("/api/admin/user-automations/{rid}")
async def admin_user_automations_update(
    rid: int,
    name: str = Form(...),
    description: str = Form(""),
    trigger_event: str = Form(...),
    conditions_json: str = Form('{"match":"all","rules":[]}'),
    actions_json: str = Form("[]"),
    active: int = Form(1),
    category: str = Form("trigger"),
    interval_minutes: int = Form(5),
    user: dict = Depends(auth_mod.require("admin.automations")),
):
    try:
        json.loads(conditions_json); json.loads(actions_json)
    except json.JSONDecodeError as e:
        raise HTTPException(400, f"Invalid JSON: {e}")
    with db.conn() as c:
        db.upsert_user_automation(
            c, automation_id=rid,
            name=name, description=description,
            trigger_event=trigger_event,
            conditions_json=conditions_json,
            actions_json=actions_json,
            active=int(active),
            category=category,
            interval_minutes=int(interval_minutes),
            actor_email=user["email"],
        )
        # On interval change OR re-activation, reset next_fire_at so the rule
        # gets picked up on the next scheduler tick (not 30 minutes from now).
        c.execute("UPDATE user_automations SET next_fire_at=NULL WHERE id=?", (rid,))
    from .. import user_rules_engine as ure
    ure.invalidate_rules_cache()
    return JSONResponse({"ok": True})


@app.post("/api/admin/user-automations/{rid}/delete")
async def admin_user_automations_delete(
    rid: int,
    user: dict = Depends(auth_mod.require("admin.automations")),
):
    with db.conn() as c:
        db.delete_user_automation(c, rid)
    from .. import user_rules_engine as ure
    ure.invalidate_rules_cache()
    return JSONResponse({"ok": True})


@app.post("/api/admin/user-automations/{rid}/toggle")
async def admin_user_automations_toggle(
    rid: int,
    active: int = Form(...),
    user: dict = Depends(auth_mod.require("admin.automations")),
):
    with db.conn() as c:
        c.execute("UPDATE user_automations SET active=?, updated_at=?, "
                  "next_fire_at=NULL WHERE id=?",
                  (int(active), db.now_iso(), rid))
    from .. import user_rules_engine as ure
    ure.invalidate_rules_cache()
    return JSONResponse({"ok": True, "active": bool(int(active))})


# ===========================================================================
# F6+ · Per-rule user automation scheduler (in-process thread)
# ===========================================================================
# The old master subprocess is gone. Each scheduler-type rule has its own
# interval_minutes + next_fire_at + active flag — so pause/resume is per-rule.
# The thread itself just dispatches due rules; it's started on app boot and
# needs no manual management.

from .. import user_scheduler as _user_sched

@app.on_event("startup")
async def _start_user_scheduler():
    """Boot the per-rule scheduler thread once the app is up. Idempotent."""
    try:
        _user_sched.start()
    except Exception as e:
        print(f"[startup] failed to start user_scheduler: {e}")


@app.on_event("shutdown")
async def _on_shutdown():
    """Flush WAL on clean shutdown so the next startup has no pending
    uncommitted writes. Mitigates the 'machine slept mid-write → corruption'
    failure mode from 14 May 2026."""
    try:
        db.checkpoint_wal()
        print("[shutdown] WAL checkpoint complete")
    except Exception as e:
        print(f"[shutdown] WAL checkpoint failed: {e}")


# ===========================================================================
# F9 · Release / rollback admin page
# ===========================================================================

from .. import release as _release  # noqa: E402

@app.get("/admin/releases", response_class=HTMLResponse)
async def admin_releases(
    request: Request,
    user: dict = Depends(auth_mod.require("admin.feature_flags")),
):
    with db.conn() as c:
        sb = _sidebar_ctx(c, user)
    releases = _release.list_releases(limit=100)
    info = _release.runtime_info()
    return TEMPLATES.TemplateResponse("admin/releases.html", {
        "request": request, "user": user,
        "releases": releases, "runtime": info,
        "current_view": "_admin", "in_detail": False, "search": "",
        **sb,
    })


@app.post("/api/admin/releases")
async def admin_release_create(
    part: str = Form("patch"),
    notes: str = Form(""),
    require_clean_tree: int = Form(1),
    user: dict = Depends(auth_mod.require("admin.feature_flags")),
):
    if part not in ("patch", "minor", "major"):
        raise HTTPException(400, "part must be patch/minor/major")
    try:
        r = _release.create_release(
            part=part, notes=notes, actor_email=user["email"],
            require_clean_tree=bool(int(require_clean_tree)),
        )
    except (RuntimeError, ValueError) as e:
        raise HTTPException(400, str(e))
    return JSONResponse({"ok": True, **r})


@app.get("/api/admin/releases/{version}/rollback-script")
async def admin_release_rollback_script(
    version: str,
    user: dict = Depends(auth_mod.require("admin.feature_flags")),
):
    """Returns the shell script to run for rollback. UI shows it in a modal
    for copy-paste — we don't auto-execute because it kills the running
    server (the same one serving this request)."""
    try:
        out = _release.prepare_rollback(version)
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(404, str(e))
    return JSONResponse(out)


@app.post("/api/admin/releases/{version}/mark-rolled-back")
async def admin_release_mark_rolled_back(
    version: str,
    user: dict = Depends(auth_mod.require("admin.feature_flags")),
):
    """After the admin runs the rollback script manually, call this to
    record it in the DB. (After restart, the app reads VERSION from the
    file — this just stamps the audit trail.)"""
    _release.mark_rolled_back(version, actor_email=user["email"])
    return JSONResponse({"ok": True})


@app.post("/api/admin/db/backup")
async def admin_db_backup(
    user: dict = Depends(auth_mod.require("admin.feature_flags")),
):
    """Trigger an immediate SQLite online backup. Returns the path written.
    Used by /admin/ai-worker (or anywhere) for on-demand snapshots."""
    try:
        path = db.backup()
    except Exception as e:
        raise HTTPException(500, f"Backup failed: {e}")
    return JSONResponse({"ok": True, "path": str(path)})


@app.post("/api/admin/user-automations/{rid}/run-now")
async def admin_user_automations_run_now(
    rid: int,
    user: dict = Depends(auth_mod.require("admin.automations")),
):
    """Force one immediate fire of this rule against currently-eligible users.
    Useful for testing — same path the scheduler uses, just without waiting
    for next_fire_at."""
    out = _user_sched.run_rule_now(rid)
    return JSONResponse(out)


@app.get("/api/admin/user-scheduler/heartbeat")
async def admin_user_scheduler_heartbeat(
    user: dict = Depends(auth_mod.require("admin.automations")),
):
    """Lightweight read of the in-process thread state. No process management
    needed — the thread auto-starts with uvicorn."""
    return JSONResponse(_user_sched.status())


@app.get("/admin/tunnel", response_class=HTMLResponse)
async def admin_tunnel(request: Request,
                        user: dict = Depends(auth_mod.require("admin.tunnel"))):
    state = _detect_tunnel_state()
    with db.conn() as c:
        sb = _sidebar_ctx(c, user)
        feature = next((f for f in FEATURE_CATALOG if f["key"] == "tunnel"), {})
    return TEMPLATES.TemplateResponse("admin/tunnel.html", {
        "request": request, "user": user, "feature": feature,
        "state": state,
        "current_view": "_admin", "in_detail": False, "search": "",
        **sb,
    })


@app.post("/api/admin/tunnel/start-quick")
async def admin_tunnel_start_quick(
    user: dict = Depends(auth_mod.require("admin.tunnel")),
):
    """Start a Cloudflare Quick Tunnel pointed at our local FastAPI port.
    Returns immediately; the worker writes the public URL to the heartbeat
    once cloudflared prints it (usually within 5-10s)."""
    if not _shutil.which("cloudflared"):
        raise HTTPException(400, "cloudflared not installed. Install it first (see the page).")
    state = _detect_tunnel_state()
    if state["running"]:
        return JSONResponse({"ok": True, "already_running": True, "pid": state["pid"],
                             "public_url": state["public_url"]})
    import subprocess
    # Wipe previous heartbeat so the UI doesn't show stale info
    if _TUNNEL_HEARTBEAT.exists():
        try: _TUNNEL_HEARTBEAT.unlink()
        except OSError: pass
    # Open log fresh
    log_f = open(_TUNNEL_LOG, "w")
    cmd = ["cloudflared", "tunnel", "--url", f"http://127.0.0.1:{config.APP_PORT}",
            "--no-autoupdate", "--metrics", "127.0.0.1:0"]
    proc = subprocess.Popen(
        cmd, stdout=log_f, stderr=log_f,
        start_new_session=True,  # survive uvicorn reloads
    )
    _TUNNEL_PID_FILE.write_text(str(proc.pid))
    _write_tunnel_heartbeat(state="starting", pid=proc.pid,
                             started_at=db.now_iso(), public_url=None)
    # Kick off a background URL-parser thread that tails the log and updates heartbeat.
    import threading, re, time
    def _parse_url_from_log():
        deadline = time.time() + 60  # cloudflared usually prints URL in <10s
        url_re = re.compile(r"https?://[a-zA-Z0-9.-]+\.trycloudflare\.com")
        while time.time() < deadline:
            try:
                if _TUNNEL_LOG.exists():
                    text = _TUNNEL_LOG.read_text(errors="ignore")
                    m = url_re.search(text)
                    if m:
                        _write_tunnel_heartbeat(state="running", pid=proc.pid,
                                                  started_at=db.now_iso(),
                                                  public_url=m.group(0))
                        with db.conn() as c:
                            db.set_meta(c, "tunnel_public_url", m.group(0))
                            db.log_access(c, actor_email=user["email"],
                                          event_type="tunnel.start",
                                          target_kind="system", target_id="",
                                          detail={"url": m.group(0), "pid": proc.pid})
                        return
            except Exception as e:
                print(f"[tunnel parse] {e}")
            time.sleep(1)
        # Didn't find a URL → write a 'failed' heartbeat so the UI shows error state
        _write_tunnel_heartbeat(state="error", pid=proc.pid,
                                  started_at=db.now_iso(), public_url=None,
                                  error="No trycloudflare URL appeared in 60s — check tunnel.log")
    threading.Thread(target=_parse_url_from_log, daemon=True).start()
    return JSONResponse({"ok": True, "pid": proc.pid,
                          "note": "Tunnel starting — poll /api/admin/tunnel/status for the public URL"})


@app.post("/api/admin/tunnel/stop")
async def admin_tunnel_stop(
    user: dict = Depends(auth_mod.require("admin.tunnel")),
):
    state = _detect_tunnel_state()
    if not state["running"]:
        return JSONResponse({"ok": True, "was_running": False})
    import signal
    try:
        os.kill(state["pid"], signal.SIGTERM)
    except (ProcessLookupError, PermissionError) as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    # Give it 3s to exit cleanly
    import time
    for _ in range(30):
        try:
            os.kill(state["pid"], 0)
            time.sleep(0.1)
        except ProcessLookupError:
            break
    else:
        # Still alive → SIGKILL
        try: os.kill(state["pid"], signal.SIGKILL)
        except ProcessLookupError: pass
    _write_tunnel_heartbeat(state="stopped", pid=None,
                              public_url=None, started_at=None)
    try: _TUNNEL_PID_FILE.unlink()
    except FileNotFoundError: pass
    with db.conn() as c:
        db.log_access(c, actor_email=user["email"],
                      event_type="tunnel.stop", target_kind="system", target_id="",
                      detail={"pid": state["pid"]})
    return JSONResponse({"ok": True, "was_running": True})


@app.get("/api/admin/tunnel/status")
async def admin_tunnel_status(
    user: dict = Depends(auth_mod.require("admin.tunnel")),
):
    return JSONResponse(_detect_tunnel_state())


@app.get("/api/admin/tunnel/log")
async def admin_tunnel_log(
    lines: int = 200,
    user: dict = Depends(auth_mod.require("admin.tunnel")),
):
    """Tail the cloudflared log. Useful for diagnosing 'didn't start' issues."""
    if not _TUNNEL_LOG.exists():
        return JSONResponse({"present": False, "lines": []})
    try:
        text = _TUNNEL_LOG.read_text(errors="ignore")
        tail = text.splitlines()[-int(max(10, min(lines, 2000))):]
        return JSONResponse({"present": True, "lines": tail})
    except OSError as e:
        return JSONResponse({"present": False, "error": str(e)})


# ===========================================================================
# F0 · Access Control admin pages
# ===========================================================================
# Both pages require admin.users / admin.roles respectively (not just generic
# admin.view) so a custom role with admin.view but NOT admin.users can't
# escalate by granting themselves more roles.

from .. import permissions as PERMS  # noqa: E402

@app.get("/admin/users", response_class=HTMLResponse)
async def admin_users(request: Request,
                      user: dict = Depends(auth_mod.require("admin.users"))):
    """List all app users with their roles, groups, and ZD user mapping."""
    with db.conn() as c:
        sb = _sidebar_ctx(c, user)
        users = db.list_app_users(c, include_disabled=True)
        roles = db.list_roles(c)
        groups = db.list_groups(c, active_only=True)
        # Decorate each user with their group_ids + zd_user_id + zd_user_name
        for u in users:
            u["group_ids"] = db.get_user_group_ids(c, u["email"])
            u["zd_user_id"] = u.get("zd_user_id")
            if u.get("zd_user_id"):
                zd = c.execute("SELECT name, email FROM users WHERE id=?",
                                 (u["zd_user_id"],)).fetchone()
                u["zd_user_name"] = zd["name"] if zd else None
                u["zd_user_email"] = zd["email"] if zd else None
            else:
                u["zd_user_name"] = None
                u["zd_user_email"] = None
        # All ZD agents (for the override dropdown)
        zd_agents = [dict(r) for r in c.execute("""
            SELECT id, name, email FROM users
            WHERE role IN ('agent','admin') ORDER BY name LIMIT 500
        """).fetchall()]
        feature = next((f for f in FEATURE_CATALOG if f["key"] == "access_users"), {})
    return TEMPLATES.TemplateResponse("admin/users.html", {
        "request": request, "user": user, "feature": feature,
        "users": users, "roles": roles, "groups": groups, "zd_agents": zd_agents,
        "current_view": "_admin", "in_detail": False, "search": "",
        **sb,
    })


@app.post("/api/admin/users")
async def admin_users_invite(
    email: str = Form(...),
    name: str = Form(""),
    role_ids: str = Form(""),
    user: dict = Depends(auth_mod.require("admin.users")),
):
    """Invite a user by email. role_ids is a comma-separated string of role IDs.
    The user can sign in immediately with Google OAuth as long as their email
    matches the invite. If they're not yet in the DB this creates them; if
    they're already there, this just adjusts roles."""
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        raise HTTPException(400, "Valid email required")
    if email.split("@", 1)[1] not in ALLOWED_DOMAINS:
        raise HTTPException(400, f"Email must be @{next(iter(ALLOWED_DOMAINS))}")
    role_id_list = [int(x) for x in (role_ids or "").split(",") if x.strip().isdigit()]
    with db.conn() as c:
        db.upsert_app_user(c, email=email, name=name or None,
                           created_by=user["email"])
        db.log_access(c, actor_email=user["email"], event_type="user.invite",
                      target_kind="user", target_id=email,
                      detail={"name": name, "role_ids": role_id_list})
        for rid in role_id_list:
            try:
                db.grant_role_to_user(c, user_email=email, role_id=rid,
                                      actor_email=user["email"])
            except ValueError as e:
                # Don't fail the whole invite if one role grant fails — log and continue.
                print(f"[admin_users_invite] skip role {rid}: {e}")
    return JSONResponse({"ok": True, "email": email,
                         "roles_granted": len(role_id_list)})


@app.post("/api/admin/users/{email}/roles")
async def admin_users_set_roles(
    email: str,
    role_ids: str = Form(""),
    user: dict = Depends(auth_mod.require("admin.users")),
):
    """Replace the set of roles on a user. role_ids = comma-separated list of
    role IDs (empty string = strip all roles, useful for 'demote to no role').
    Bootstrap-protected: refuses if it would leave nobody holding admin.users
    or admin.roles."""
    email = (email or "").strip().lower()
    new_role_ids = {int(x) for x in (role_ids or "").split(",")
                     if x.strip().isdigit()}
    with db.conn() as c:
        u = db.get_app_user(c, email)
        if not u:
            raise HTTPException(404, f"No user {email}")
        current = {r["id"] for r in u["roles"]}
        to_revoke = current - new_role_ids
        to_grant = new_role_ids - current
        # Do revokes FIRST so we hit the bootstrap-check before grants.
        errs: list[str] = []
        for rid in to_revoke:
            try:
                db.revoke_role_from_user(c, user_email=email, role_id=rid,
                                          actor_email=user["email"])
            except ValueError as e:
                errs.append(str(e))
        for rid in to_grant:
            try:
                db.grant_role_to_user(c, user_email=email, role_id=rid,
                                       actor_email=user["email"])
            except ValueError as e:
                errs.append(str(e))
    if errs:
        return JSONResponse({"ok": False, "errors": errs}, status_code=409)
    return JSONResponse({"ok": True, "email": email,
                         "granted": sorted(to_grant), "revoked": sorted(to_revoke)})


@app.post("/api/admin/users/{email}/groups")
async def admin_users_set_groups(
    email: str,
    group_ids: str = Form(""),
    user: dict = Depends(auth_mod.require("admin.users")),
):
    """Replace the set of groups a user belongs to. Comma-separated group_ids."""
    email = (email or "").strip().lower()
    new_ids = [int(x) for x in (group_ids or "").split(",") if x.strip().lstrip("-").isdigit()]
    try:
        with db.conn() as c:
            db.set_user_groups(c, user_email=email, group_ids=new_ids,
                                actor_email=user["email"])
    except ValueError as e:
        raise HTTPException(400, str(e))
    return JSONResponse({"ok": True, "email": email, "group_ids": new_ids})


@app.post("/api/admin/users/{email}/zd-user")
async def admin_users_set_zd_mapping(
    email: str,
    zd_user_id: str = Form(""),
    user: dict = Depends(auth_mod.require("admin.users")),
):
    """Manual ZD user mapping override. Pass '' or 'auto' to clear (next login
    will re-auto-fill by email)."""
    email = (email or "").strip().lower()
    zd_id: int | None = None
    if zd_user_id and zd_user_id != "auto":
        try:
            zd_id = int(zd_user_id)
        except ValueError:
            raise HTTPException(400, "zd_user_id must be an integer or empty")
    try:
        with db.conn() as c:
            db.set_zd_user_mapping(c, user_email=email, zd_user_id=zd_id,
                                     actor_email=user["email"])
    except ValueError as e:
        raise HTTPException(400, str(e))
    return JSONResponse({"ok": True, "email": email, "zd_user_id": zd_id})


@app.post("/api/admin/users/{email}/status")
async def admin_users_set_status(
    email: str,
    status: str = Form(...),
    user: dict = Depends(auth_mod.require("admin.users")),
):
    """Active/disabled toggle. Refuses to disable yourself or the last admin."""
    email = (email or "").strip().lower()
    if status not in ("active", "disabled"):
        raise HTTPException(400, "status must be 'active' or 'disabled'")
    if email == user["email"] and status == "disabled":
        raise HTTPException(400, "You can't disable yourself. Ask another admin.")
    try:
        with db.conn() as c:
            db.set_app_user_status(c, email, status, actor_email=user["email"])
    except ValueError as e:
        raise HTTPException(409, str(e))
    return JSONResponse({"ok": True, "email": email, "status": status})


@app.get("/admin/roles", response_class=HTMLResponse)
async def admin_roles(request: Request,
                      user: dict = Depends(auth_mod.require("admin.roles"))):
    """Roles index — list every role with its user-count + perm-count."""
    with db.conn() as c:
        sb = _sidebar_ctx(c, user)
        roles = db.list_roles(c)
        feature = next((f for f in FEATURE_CATALOG if f["key"] == "access_roles"), {})
    return TEMPLATES.TemplateResponse("admin/roles.html", {
        "request": request, "user": user, "feature": feature,
        "roles": roles,
        "current_view": "_admin", "in_detail": False, "search": "",
        **sb,
    })


@app.get("/admin/roles/new", response_class=HTMLResponse)
async def admin_role_new(request: Request,
                          user: dict = Depends(auth_mod.require("admin.roles"))):
    """Blank role-create page reusing the role_edit.html template."""
    with db.conn() as c:
        sb = _sidebar_ctx(c, user)
    role = {
        "id": None, "name": "", "description": "",
        "is_system_default": 0, "permissions": [], "users": [],
    }
    return TEMPLATES.TemplateResponse("admin/role_edit.html", {
        "request": request, "user": user,
        "role": role, "is_new": True,
        "permissions_by_group": PERMS.PERMISSIONS_BY_GROUP,
        "groups": PERMS.GROUPS,
        "current_view": "_admin", "in_detail": False, "search": "",
        **sb,
    })


@app.get("/admin/roles/{role_id}", response_class=HTMLResponse)
async def admin_role_edit(role_id: int, request: Request,
                          user: dict = Depends(auth_mod.require("admin.roles"))):
    """Per-role page with the permission matrix (grouped checkboxes)."""
    with db.conn() as c:
        sb = _sidebar_ctx(c, user)
        role = db.get_role(c, role_id)
        if not role:
            raise HTTPException(404, f"No role {role_id}")
    return TEMPLATES.TemplateResponse("admin/role_edit.html", {
        "request": request, "user": user,
        "role": role, "is_new": False,
        "permissions_by_group": PERMS.PERMISSIONS_BY_GROUP,
        "groups": PERMS.GROUPS,
        "current_view": "_admin", "in_detail": False, "search": "",
        **sb,
    })


@app.post("/api/admin/roles")
async def admin_role_create(
    name: str = Form(...),
    description: str = Form(""),
    permission_keys: str = Form(""),
    user: dict = Depends(auth_mod.require("admin.roles")),
):
    """Create a new (non-system) role with an initial permission set."""
    name = (name or "").strip()
    if not name:
        raise HTTPException(400, "Role name required")
    keys = [k.strip() for k in permission_keys.split(",") if k.strip()]
    with db.conn() as c:
        # Reject duplicate names early — friendlier than the UNIQUE constraint error.
        existing = c.execute("SELECT id FROM roles WHERE name=?", (name,)).fetchone()
        if existing:
            raise HTTPException(409, f"A role named '{name}' already exists")
        try:
            rid = db.upsert_role(c, role_id=None, name=name, description=description,
                                  is_system_default=0, actor_email=user["email"])
            if keys:
                db.set_role_permissions(c, rid, keys, actor_email=user["email"],
                                         valid_keys=PERMS.ALL_KEYS)
        except ValueError as e:
            raise HTTPException(400, str(e))
    return JSONResponse({"ok": True, "role_id": rid})


@app.post("/api/admin/roles/{role_id}")
async def admin_role_update(
    role_id: int,
    name: str = Form(...),
    description: str = Form(""),
    permission_keys: str = Form(""),
    user: dict = Depends(auth_mod.require("admin.roles")),
):
    """Rename + re-permission a role. Permissions replace, not merge."""
    name = (name or "").strip()
    if not name:
        raise HTTPException(400, "Role name required")
    keys = [k.strip() for k in permission_keys.split(",") if k.strip()]
    try:
        with db.conn() as c:
            db.upsert_role(c, role_id=role_id, name=name, description=description,
                            actor_email=user["email"])
            db.set_role_permissions(c, role_id, keys, actor_email=user["email"],
                                     valid_keys=PERMS.ALL_KEYS)
    except ValueError as e:
        raise HTTPException(409, str(e))
    return JSONResponse({"ok": True, "role_id": role_id})


@app.post("/api/admin/roles/{role_id}/delete")
async def admin_role_delete(role_id: int,
                             user: dict = Depends(auth_mod.require("admin.roles"))):
    try:
        with db.conn() as c:
            db.delete_role(c, role_id, actor_email=user["email"])
    except ValueError as e:
        raise HTTPException(409, str(e))
    return JSONResponse({"ok": True})


# ===========================================================================
# F0+ T2 · /admin/groups
# ===========================================================================

@app.get("/admin/groups", response_class=HTMLResponse)
async def admin_groups(request: Request,
                       user: dict = Depends(auth_mod.require("admin.groups"))):
    with db.conn() as c:
        sb = _sidebar_ctx(c, user)
        groups = db.list_groups(c)
        feature = next((f for f in FEATURE_CATALOG if f["key"] == "groups"), {})
    return TEMPLATES.TemplateResponse("admin/groups.html", {
        "request": request, "user": user, "feature": feature,
        "groups": groups,
        "current_view": "_admin", "in_detail": False, "search": "",
        **sb,
    })


# ===========================================================================
# F2 · /admin/auth — Google OAuth setup wizard
# ===========================================================================

def _env_file_path() -> Path:
    """Path to the .env file at the repo root."""
    return Path(__file__).resolve().parents[2] / ".env"


def _detect_oauth_state() -> dict:
    """Inspect config + DB to figure out where we are in the OAuth setup."""
    has_client_id = bool(config.GOOGLE_CLIENT_ID and not config.GOOGLE_CLIENT_ID.startswith("your-"))
    has_client_secret = bool(config.GOOGLE_CLIENT_SECRET and not config.GOOGLE_CLIENT_SECRET.startswith("your-"))
    has_session_secret = (config.SESSION_SECRET and
                          config.SESSION_SECRET != "dev-only-not-secure-replace-me")
    public_url = (config.APP_PUBLIC_URL or "").rstrip("/")
    public_url_set = bool(public_url and not public_url.startswith("http://127.0.0.1") and not public_url.startswith("http://localhost"))
    state = {
        "auth_enabled": config.AUTH_ENABLED,
        "has_client_id": has_client_id,
        "has_client_secret": has_client_secret,
        "has_session_secret": bool(has_session_secret),
        "public_url": public_url or "http://127.0.0.1:8000",
        "public_url_set": public_url_set,
        "callback_url": (public_url or "http://127.0.0.1:8000") + "/auth/callback",
        "localhost_callback_url": "http://localhost:8000/auth/callback",
        "env_path": str(_env_file_path()),
        "env_exists": _env_file_path().exists(),
    }
    # What's the next step the user should take?
    if not has_client_id or not has_client_secret:
        state["next_step"] = "create_oauth_client"
    elif not has_session_secret:
        state["next_step"] = "rotate_session_secret"
    elif not state["auth_enabled"]:
        state["next_step"] = "restart_uvicorn"
    else:
        state["next_step"] = "test_login"
    return state


@app.get("/admin/auth", response_class=HTMLResponse)
async def admin_auth(request: Request,
                      user: dict = Depends(auth_mod.require_any("admin.users", "admin.view"))):
    state = _detect_oauth_state()
    with db.conn() as c:
        sb = _sidebar_ctx(c, user)
        feature = next((f for f in FEATURE_CATALOG if f["key"] == "auth"), {})
        # Recent logins for visibility — confirms the path is actually working
        recent_logins = [dict(r) for r in c.execute("""
            SELECT email, name, last_login_at FROM app_users
            WHERE last_login_at IS NOT NULL
            ORDER BY last_login_at DESC LIMIT 10
        """).fetchall()]
    return TEMPLATES.TemplateResponse("admin/auth.html", {
        "request": request, "user": user, "feature": feature,
        "state": state, "recent_logins": recent_logins,
        "current_view": "_admin", "in_detail": False, "search": "",
        **sb,
    })


@app.post("/api/admin/auth/save-env")
async def admin_auth_save_env(
    google_client_id: str = Form(...),
    google_client_secret: str = Form(...),
    app_public_url: str = Form(""),
    rotate_session_secret: int = Form(0),
    user: dict = Depends(auth_mod.require("admin.users")),
):
    """Atomically write OAuth credentials to .env. Preserves all other keys.
    Returns next steps. The app does NOT pick up changes until uvicorn is
    restarted (env is loaded once at boot) — the response says so."""
    import os
    import secrets as _secrets
    import tempfile
    env_path = _env_file_path()
    if not env_path.exists():
        # Bootstrap a fresh .env from the example if missing
        ex = env_path.parent / ".env.example"
        if ex.exists():
            env_path.write_text(ex.read_text())
        else:
            env_path.write_text("# zd-copilot env\n")
    text = env_path.read_text()
    lines = text.splitlines()

    def _set(key: str, value: str) -> None:
        """In-place replace (or append) a KEY=VALUE line."""
        nonlocal lines
        # Quote values that contain spaces or special chars
        quoted = value if "=" not in value and " " not in value else f'"{value}"'
        found = False
        for i, line in enumerate(lines):
            stripped = line.lstrip()
            if stripped.startswith(f"{key}="):
                lines[i] = f"{key}={quoted}"
                found = True
                break
        if not found:
            lines.append(f"{key}={quoted}")

    gid = google_client_id.strip()
    gsec = google_client_secret.strip()
    if not gid or not gsec:
        raise HTTPException(400, "Both Google client id and secret are required")
    _set("GOOGLE_CLIENT_ID", gid)
    _set("GOOGLE_CLIENT_SECRET", gsec)
    if app_public_url.strip():
        _set("APP_PUBLIC_URL", app_public_url.strip().rstrip("/"))
    if rotate_session_secret:
        _set("SESSION_SECRET", _secrets.token_urlsafe(48))

    # Atomic write — temp file + rename so a crash mid-write doesn't corrupt .env
    new_text = "\n".join(lines)
    if not new_text.endswith("\n"):
        new_text += "\n"
    fd, tmp = tempfile.mkstemp(prefix=".env.", dir=str(env_path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            f.write(new_text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, env_path)
    except Exception:
        try: os.remove(tmp)
        except OSError: pass
        raise
    with db.conn() as c:
        db.log_access(c, actor_email=user["email"], event_type="auth.env_saved",
                      target_kind="system", target_id="",
                      detail={"client_id_set": True,
                              "public_url_set": bool(app_public_url.strip()),
                              "session_rotated": bool(rotate_session_secret)})
    return JSONResponse({
        "ok": True,
        "env_path": str(env_path),
        "note": "Saved. Restart uvicorn (Ctrl-C then run again) for the new env to load. "
                "The OAuth login will work after restart.",
    })


@app.post("/api/admin/groups/sync-from-zd")
async def admin_groups_sync_from_zd(
    user: dict = Depends(auth_mod.require("admin.groups")),
):
    """Pull EVERY group from Zendesk into our DB. Idempotent.
    NOTE: must be registered BEFORE the /{group_id} routes — otherwise
    FastAPI's path matcher catches /sync-from-zd as group_id='sync-from-zd'
    and 422s when coercing to int."""
    _invalidate_group_name_cache()  # groups changing → drop the warm cache
    from .. import zendesk
    try:
        zd_groups = zendesk.list_groups()
    except Exception as e:
        raise HTTPException(502, f"Zendesk list_groups failed: {e}")
    created = 0
    updated = 0
    with db.conn() as c:
        for g in zd_groups:
            existing = c.execute("SELECT name FROM groups WHERE id=?",
                                  (g.get("id"),)).fetchone()
            db.upsert_group(c, g)
            if existing is None:
                created += 1
            elif existing["name"] != g.get("name"):
                updated += 1
        db.log_access(c, actor_email=user["email"],
                      event_type="group.sync_zd",
                      target_kind="system", target_id="",
                      detail={"pulled": len(zd_groups),
                              "created": created, "updated": updated})
    _SIDEBAR_CACHE.clear()
    return JSONResponse({"ok": True,
                         "pulled": len(zd_groups),
                         "created": created, "updated": updated})


@app.post("/api/admin/groups")
async def admin_groups_create(
    name: str = Form(...),
    description: str = Form(""),
    user: dict = Depends(auth_mod.require("admin.groups")),
):
    try:
        with db.conn() as c:
            new_id = db.upsert_native_group(c, group_id=None, name=name,
                                              description=description,
                                              actor_email=user["email"])
    except ValueError as e:
        raise HTTPException(400, str(e))
    _invalidate_group_name_cache()
    return JSONResponse({"ok": True, "group_id": new_id})


@app.post("/api/admin/groups/{group_id}")
async def admin_groups_update(
    group_id: int,
    name: str = Form(...),
    description: str = Form(""),
    user: dict = Depends(auth_mod.require("admin.groups")),
):
    try:
        with db.conn() as c:
            db.upsert_native_group(c, group_id=group_id, name=name,
                                     description=description,
                                     actor_email=user["email"])
    except ValueError as e:
        raise HTTPException(400, str(e))
    _invalidate_group_name_cache()
    return JSONResponse({"ok": True})


@app.post("/api/admin/groups/{group_id}/archive")
async def admin_groups_archive(
    group_id: int,
    active: int = Form(...),
    user: dict = Depends(auth_mod.require("admin.groups")),
):
    try:
        with db.conn() as c:
            db.set_group_active(c, group_id, active, actor_email=user["email"])
    except ValueError as e:
        raise HTTPException(400, str(e))
    return JSONResponse({"ok": True, "active": bool(active)})


# ===========================================================================
# F0+ T5 · Custom views — list / create / edit / share / reorder
# ===========================================================================

# Standard fields (built-in ticket columns). Every custom field from
# ticket_fields gets appended at runtime by _build_filter_catalog().
VIEW_FILTER_STANDARD = [
    # (key, label, type_kind, options_or_None)
    ("status",          "Status",            "select", ["new","open","pending","hold","solved","closed"]),
    ("priority",        "Priority",          "select", ["low","normal","high","urgent"]),
    ("type",            "Type",              "select", ["question","incident","problem","task"]),
    ("group_name",      "Group",             "select_groups",  None),
    ("group_id",        "Group (by ID)",     "number",         None),
    ("assignee_id",     "Assignee",          "agent_or_me",    None),
    ("requester_id",    "Requester",         "agent_or_me",    None),
    ("subject",         "Subject contains",  "text",           None),
    # Note: tickets table has no `description` column — ZD stores the
    # description as the first ticket comment. We don't expose a
    # description-text filter here; use the search page's "Search comment
    # bodies" option to scan conversation text.
    ("created_at",      "Created",           "date_relative",  None),
    ("updated_at",      "Updated",           "date_relative",  None),
    ("solved_at",       "Solved",            "date_relative",  None),
]

VIEW_FILTER_OPS = {
    "select":         ["eq", "ne", "in", "not_in", "is_null", "not_null"],
    "select_groups":  ["eq", "in"],
    "agent_or_me":    ["is_me", "eq", "is_null", "not_null"],
    "date_relative":  ["within_days", "older_than_days"],
    "text":           ["eq", "ne", "is_null", "not_null"],
    "number":         ["eq", "ne", "in", "not_in", "is_null", "not_null"],
    "custom_text":    ["eq", "ne", "is_null", "not_null"],
    "custom_select":  ["eq", "ne", "in", "not_in", "is_null", "not_null"],
}


def _build_filter_catalog(c) -> tuple[list, dict]:
    """Return (filter_fields, options_by_key). Combines hardcoded standard
    columns with every active custom field. Options for selects (tagger,
    multiselect, partial_select) are inlined so the editor JS can render
    proper dropdowns without an extra fetch."""
    catalog = list(VIEW_FILTER_STANDARD)
    options_by_key: dict[str, list] = {}
    # Inline standard select options for JS
    for entry in catalog:
        if entry[2] == "select" and entry[3]:
            options_by_key[entry[0]] = [{"value": o, "label": o} for o in entry[3]]
    # Append every custom field. Skip fields without titles, archived ones.
    rows = c.execute("""
        SELECT id, title, type, options FROM ticket_fields
        WHERE title IS NOT NULL AND title != ''
        ORDER BY title
    """).fetchall()
    for row in rows:
        key = f"cf.{row['id']}"
        label = f"⚙ {row['title']}"
        ftype = row["type"] or "text"
        # Map ZD field types → our filter type kinds
        if ftype in ("tagger", "multiselect", "partialcredit", "checkbox"):
            kind = "custom_select"
            try:
                opts = json.loads(row["options"] or "[]")
            except (json.JSONDecodeError, TypeError):
                opts = []
            options_by_key[key] = [
                {"value": o.get("value"), "label": o.get("name") or o.get("value")}
                for o in opts if o.get("value")
            ]
        elif ftype in ("integer", "decimal"):
            kind = "number"
        elif ftype in ("date", "regexp"):
            kind = "custom_text"
        else:
            kind = "custom_text"
        catalog.append((key, label, kind, None))
    # Custom statuses (separate dropdown of status labels for the status filter)
    try:
        custom_statuses = c.execute(
            "SELECT label FROM custom_statuses WHERE active=1 ORDER BY label"
        ).fetchall()
        if custom_statuses:
            extra_status_opts = [{"value": s["label"], "label": s["label"]}
                                  for s in custom_statuses]
            # Augment status options with custom status labels too
            options_by_key.setdefault("status", []).extend(extra_status_opts)
    except Exception:
        pass
    return catalog, options_by_key


def _build_column_catalog(c) -> list:
    """Available columns for the view's display. Standard columns + every
    custom field. Returns list of {key, label, group}."""
    standard = [
        {"key": "id",             "label": "Ticket ID",     "group": "Standard"},
        {"key": "subject",        "label": "Subject",       "group": "Standard"},
        {"key": "status",         "label": "Status",        "group": "Standard"},
        {"key": "priority",       "label": "Priority",      "group": "Standard"},
        {"key": "type",           "label": "Type",          "group": "Standard"},
        {"key": "group_name",     "label": "Group",         "group": "Standard"},
        {"key": "assignee_name",  "label": "Assignee",      "group": "Standard"},
        {"key": "requester_name", "label": "Requester",     "group": "Standard"},
        {"key": "customer",       "label": "Customer",      "group": "Standard"},
        {"key": "created_at",     "label": "Created",       "group": "Standard"},
        {"key": "updated_at",     "label": "Last update",   "group": "Standard"},
        {"key": "solved_at",      "label": "Solved at",     "group": "Standard"},
        {"key": "sla_status",     "label": "SLA status",    "group": "Standard"},
        {"key": "tags",           "label": "Tags",          "group": "Standard"},
    ]
    rows = c.execute("""
        SELECT id, title FROM ticket_fields
        WHERE title IS NOT NULL AND title != ''
        ORDER BY title
    """).fetchall()
    for r in rows:
        standard.append({
            "key": f"cf.{r['id']}",
            "label": r["title"],
            "group": "Custom fields",
        })
    return standard


def _can_edit_view(c, view_row: dict, user: dict) -> bool:
    """Owner can always edit. Anyone with views.manage_all can edit anything."""
    if "views.manage_all" in user.get("permissions", set()):
        return True
    return view_row.get("owner_email") == user.get("email")


@app.get("/admin/views", response_class=HTMLResponse)
async def admin_views(request: Request,
                      user: dict = Depends(auth_mod.require_any("views.manage_all", "admin.view"))):
    """Admin index of ALL views in the system. Personal views are shown for
    inventory only — owners manage them via the sidebar."""
    with db.conn() as c:
        sb = _sidebar_ctx(c, user)
        rows = [dict(r) for r in c.execute("""
            SELECT v.*,
                   (SELECT COUNT(*) FROM view_shares WHERE view_id = v.id) AS share_count
            FROM native_views v
            ORDER BY v.scope, v.default_position, v.name
        """).fetchall()]
        feature = next((f for f in FEATURE_CATALOG if f["key"] == "views"), {})
    return TEMPLATES.TemplateResponse("admin/views.html", {
        "request": request, "user": user, "feature": feature,
        "views": rows,
        "current_view": "_admin", "in_detail": False, "search": "",
        **sb,
    })


@app.get("/view-builder/new", response_class=HTMLResponse)
async def view_new(request: Request,
                   user: dict = Depends(auth_mod.require("views.create_personal"))):
    """Create-a-view form, available to anyone with views.create_personal.
    Lives under /view-builder/ to avoid colliding with the /views/{view_name}
    catch-all that lists tickets in a saved view."""
    return _render_view_editor(request, user, view=None)


@app.get("/view-builder/edit/{view_id}", response_class=HTMLResponse)
async def view_edit(view_id: int, request: Request,
                     user: dict = Depends(auth_mod.require("views.create_personal"))):
    with db.conn() as c:
        v = db.get_view(c, view_id)
    if not v:
        raise HTTPException(404, f"No view {view_id}")
    if not _can_edit_view(None, v, user):
        raise HTTPException(403, "You don't own this view — ask the owner or an admin to edit it.")
    return _render_view_editor(request, user, view=v)


def _render_view_editor(request: Request, user: dict, view: dict | None):
    with db.conn() as c:
        sb = _sidebar_ctx(c, user)
        groups = db.list_groups(c, active_only=True)
        # Build filter + column catalogs dynamically (standard cols + every
        # custom field). This is what makes the editor able to filter by ANY
        # field, not just the hand-picked few.
        filter_fields, filter_options = _build_filter_catalog(c)
        column_choices = _build_column_catalog(c)
        # If editing a shared view, load its current shares for prefill
        shared_user_emails: list[str] = []
        shared_group_ids: list[int] = []
        if view:
            for s in view.get("shares", []):
                if s["kind"] == "user":
                    shared_user_emails.append(s["id"])
                elif s["kind"] == "group":
                    try: shared_group_ids.append(int(s["id"]))
                    except ValueError: pass
        # ZD agents for assignee picker dropdown
        zd_agents = [dict(r) for r in c.execute("""
            SELECT id, name, email FROM users WHERE role IN ('agent','admin')
            ORDER BY name LIMIT 500
        """).fetchall()]
        # App users for "share with users" picker
        app_users = db.list_app_users(c, include_disabled=False)
    # Default filter object for new views
    default_filter = view["filter_json"] if view else json.dumps({"match": "all", "rules": []})
    default_cols = view["column_ids_json"] if view else json.dumps(["id", "subject", "status", "priority", "group_name", "assignee_name", "updated_at"])
    default_sort = view["sort_json"] if view else json.dumps({"field": "updated_at", "dir": "desc"})
    return TEMPLATES.TemplateResponse("views/edit.html", {
        "request": request, "user": user,
        "view": view, "is_new": view is None,
        "filter_fields": filter_fields,
        "filter_options": filter_options,
        "filter_ops": VIEW_FILTER_OPS,
        "groups": groups,
        "zd_agents": zd_agents,
        "app_users": app_users,
        "column_choices": column_choices,
        "shared_user_emails": shared_user_emails,
        "shared_group_ids": shared_group_ids,
        "default_filter_json": default_filter,
        "default_columns_json": default_cols,
        "default_sort_json": default_sort,
        "current_view": "_views_edit",
        "in_detail": False, "search": "",
        **sb,
    })


@app.post("/api/views")
async def view_create(
    name: str = Form(...),
    description: str = Form(""),
    scope: str = Form("personal"),
    filter_json: str = Form("{}"),
    column_ids_json: str = Form("[]"),
    sort_json: str = Form("{}"),
    color: str = Form("indigo"),
    icon: str = Form(""),
    share_user_emails: str = Form(""),   # comma-separated emails
    share_group_ids: str = Form(""),     # comma-separated group ids
    user: dict = Depends(auth_mod.require("views.create_personal")),
):
    if scope == "shared" and "views.create_shared" not in user["permissions"]:
        raise HTTPException(403, "Missing permission: views.create_shared")
    # Validate JSON shapes
    try:
        json.loads(filter_json); json.loads(column_ids_json); json.loads(sort_json)
    except json.JSONDecodeError as e:
        raise HTTPException(400, f"Invalid JSON: {e}")
    try:
        with db.conn() as c:
            new_id = db.upsert_native_view(c, view_id=None,
                name=name, description=description,
                owner_email=user["email"], scope=scope,
                filter_json=filter_json, column_ids_json=column_ids_json,
                sort_json=sort_json, color=color, icon=icon,
                actor_email=user["email"])
            if scope == "shared":
                shares = _parse_shares(share_user_emails, share_group_ids)
                db.set_view_shares(c, new_id, shares, user["email"])
    except ValueError as e:
        raise HTTPException(400, str(e))
    _invalidate_view_cache()
    # Clear ONLY the saving user's sidebar cache. Shared views will pick up
    # for other users on their next sidebar refresh (or within TTL). This is
    # massively faster than clearing all users' caches on every save.
    _SIDEBAR_CACHE.pop(user["email"], None)
    return JSONResponse({"ok": True, "view_id": new_id})


@app.post("/api/views/{view_id}")
async def view_update(
    view_id: int,
    name: str = Form(...),
    description: str = Form(""),
    scope: str = Form("personal"),
    filter_json: str = Form("{}"),
    column_ids_json: str = Form("[]"),
    sort_json: str = Form("{}"),
    color: str = Form("indigo"),
    icon: str = Form(""),
    share_user_emails: str = Form(""),
    share_group_ids: str = Form(""),
    user: dict = Depends(auth_mod.require("views.create_personal")),
):
    with db.conn() as c:
        v = db.get_view(c, view_id)
        if not v:
            raise HTTPException(404, f"No view {view_id}")
        if not _can_edit_view(c, v, user):
            raise HTTPException(403, "You don't own this view")
        if scope == "shared" and "views.create_shared" not in user["permissions"]:
            raise HTTPException(403, "Missing permission: views.create_shared")
        if v["scope"] == "system" and "views.manage_all" not in user["permissions"]:
            # Don't let non-admins re-scope system views
            scope = "system"
        try:
            json.loads(filter_json); json.loads(column_ids_json); json.loads(sort_json)
        except json.JSONDecodeError as e:
            raise HTTPException(400, f"Invalid JSON: {e}")
        try:
            db.upsert_native_view(c, view_id=view_id, name=name, description=description,
                owner_email=v["owner_email"], scope=scope,
                filter_json=filter_json, column_ids_json=column_ids_json,
                sort_json=sort_json, color=color, icon=icon,
                actor_email=user["email"])
            if scope == "shared":
                shares = _parse_shares(share_user_emails, share_group_ids)
                db.set_view_shares(c, view_id, shares, user["email"])
            elif v["scope"] == "shared" and scope != "shared":
                # Re-scoped away from shared — drop all shares
                db.set_view_shares(c, view_id, [], user["email"])
        except ValueError as e:
            raise HTTPException(400, str(e))
    _invalidate_view_cache()
    # Clear ONLY the saving user's sidebar cache (others pick up within TTL)
    _SIDEBAR_CACHE.pop(user["email"], None)
    return JSONResponse({"ok": True, "view_id": view_id})


def _parse_shares(emails_csv: str, gids_csv: str) -> list[dict]:
    shares: list[dict] = []
    for e in (emails_csv or "").split(","):
        e = e.strip().lower()
        if e and "@" in e:
            shares.append({"kind": "user", "id": e})
    for g in (gids_csv or "").split(","):
        g = g.strip()
        if g and g.lstrip("-").isdigit():
            shares.append({"kind": "group", "id": g})
    return shares


@app.post("/api/views/{view_id}/delete")
async def view_delete(view_id: int,
                       user: dict = Depends(auth_mod.require("views.create_personal"))):
    with db.conn() as c:
        v = db.get_view(c, view_id)
        if not v:
            raise HTTPException(404, "No such view")
        if not _can_edit_view(c, v, user):
            raise HTTPException(403, "You don't own this view")
        try:
            db.delete_view(c, view_id, actor_email=user["email"])
        except ValueError as e:
            raise HTTPException(409, str(e))
    _invalidate_view_cache()
    _SIDEBAR_CACHE.pop(user["email"], None)
    return JSONResponse({"ok": True})


@app.post("/api/views/reorder")
async def view_reorder(view_ids: str = Form(...),
                        user: dict = Depends(require_user)):
    """Per-user reorder. Saves the user's preferred view ordering."""
    ids = [int(x) for x in view_ids.split(",") if x.strip().lstrip("-").isdigit()]
    with db.conn() as c:
        db.set_user_view_order(c, user["email"], ids)
    # Only clear THIS user's cache — reordering is per-user, no one else affected.
    _SIDEBAR_CACHE.pop(user["email"], None)
    return JSONResponse({"ok": True, "count": len(ids)})


# ===========================================================================
# F0+ T6 · Agent list endpoint (used by inline assignment widget on ticket detail)
# ===========================================================================

@app.get("/api/agents/list")
async def agents_list(for_ticket: int | None = None,
                      user: dict = Depends(auth_mod.require_any(
                          "tickets.assign_self", "tickets.assign_others"))):
    """Return the list of agents eligible to be assigned. If for_ticket is
    passed, prioritizes agents in the ticket's group (but still includes
    all agents below so cross-group reassignment works)."""
    out_agents: list[dict] = []
    with db.conn() as c:
        target_group_id = None
        if for_ticket is not None:
            row = c.execute("SELECT group_id FROM tickets WHERE id=?",
                              (for_ticket,)).fetchone()
            if row:
                target_group_id = row["group_id"]
        # All ZD agents + admins (real Zendesk users — these are who can actually
        # be assigned in ZD). Filter by group_id when we know the ticket's group.
        # We just expose them all for the MVP; ranking by group can come later.
        agents = c.execute("""
            SELECT id, name, email, role FROM users
            WHERE role IN ('agent', 'admin')
            ORDER BY name
            LIMIT 500
        """).fetchall()
        out_agents = [{"id": a["id"], "name": a["name"],
                       "email": a["email"], "role": a["role"]}
                      for a in agents]
    return JSONResponse({"agents": out_agents, "for_ticket": for_ticket})


@app.get("/admin/{section}", response_class=HTMLResponse)
async def admin_section(section: str, request: Request, user: dict = Depends(require_user)):
    _require_admin(user)
    feature = next((f for f in FEATURE_CATALOG if (f.get("setup_url") or "").rstrip("/") == f"/admin/{section}"), None)
    if not feature:
        raise HTTPException(404, f"Unknown admin section: {section}")
    with db.conn() as c:
        sb = _sidebar_ctx(c, user)
        stat = ""
        if feature.get("stat_fn"):
            try:
                stat = feature["stat_fn"](c)
            except Exception as e:
                stat = f"(error: {e})"
    return TEMPLATES.TemplateResponse("admin/stub.html", {
        "request": request, "user": user,
        "feature": {**feature, "stat": stat},
        "description": ADMIN_STUB_DESCRIPTIONS.get(section, ""),
        "current_view": "_admin", "in_detail": False, "search": "",
        **sb,
    })


@app.get("/health")
async def health():
    return {"ok": True}
