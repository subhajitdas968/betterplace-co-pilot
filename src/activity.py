"""
User activity logger. Designed to be called from anywhere — endpoints,
middleware, automations — without ever breaking the calling code.

Two paths:
  1. log(...) — direct call from inside an endpoint when you have rich context
                (before/after values, ticket id, etc.)
  2. Middleware below — wraps every request and logs page views automatically
                (cheap, append-only, swallows errors)

The session_id is a stable random string per browser session that lets us
correlate events when building reports ("Subhajit's 14 May session had X
ticket views and Y edits").
"""

from __future__ import annotations

import json
import secrets
import time
from typing import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from . import db


# ---- Top-level helper -----------------------------------------------------

def log(*, user_email: str | None,
        event_type: str, event_subtype: str,
        target_kind: str | None = None,
        target_id: str | None = None,
        detail: dict | None = None,
        request: Request | None = None) -> None:
    """Fire-and-forget activity log. Never raises. Anonymous events (no
    user_email) are dropped silently — we only track signed-in activity."""
    if not user_email:
        return
    ip = None
    ua = None
    sid = None
    if request is not None:
        ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (
            request.client.host if request.client else None)
        ua = request.headers.get("user-agent")
        try:
            sid = request.session.get("session_id")
        except (AssertionError, Exception):
            # No SessionMiddleware installed (e.g. during static tests)
            sid = None
    try:
        with db.conn() as c:
            db.log_activity(
                c, user_email=user_email,
                event_type=event_type, event_subtype=event_subtype,
                target_kind=target_kind, target_id=target_id,
                detail=detail, ip_address=ip, user_agent=ua, session_id=sid,
            )
    except Exception as e:
        print(f"[activity.log] swallowed error: {e}")


# ---- Middleware -----------------------------------------------------------

class ActivityMiddleware(BaseHTTPMiddleware):
    """Lightweight session-id middleware. PERF NOTE: this used to ALSO log
    every page view + UPDATE app_users.last_login_at on every request, which
    cost 50-200ms per page (two extra db.conn() + WAL pragmas per request).

    Now: the only DB work in the request hot path is on the FIRST request of
    a fresh session — to record session.start and assign a session_id. All
    other activity is logged explicitly from inside endpoints via
    activity.log() when meaningful (login, availability change, profile save,
    search, etc.) — not blanket page-view logging.

    The `log_pages` kwarg is kept for backwards compat but defaults to False
    now so the middleware adds ~0ms overhead per request.
    """

    def __init__(self, app, *, log_pages: bool = False):
        super().__init__(app)
        self.log_pages = log_pages

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # Cheap session_id assignment + session.start (once per browser session).
        # No DB call for established sessions — just a session-cookie read.
        try:
            sess = request.session
            if sess.get("user") and not sess.get("session_id"):
                sess["session_id"] = secrets.token_urlsafe(12)
                user_email = (sess.get("user") or {}).get("email")
                if user_email:
                    log(user_email=user_email, event_type="session",
                        event_subtype="start", request=request)
        except (AssertionError, Exception):
            pass

        response = await call_next(request)

        # Optional page-view logging — disabled by default. Re-enable with
        # log_pages=True if you want a full nav trail (writes 1 row per page).
        if self.log_pages:
            path = request.url.path
            if (request.method == "GET"
                    and response.status_code < 400
                    and not path.startswith(("/static/", "/api/", "/auth/", "/favicon"))):
                try:
                    user = request.session.get("user") or {}
                    if user.get("email"):
                        log(user_email=user["email"],
                            event_type="navigation", event_subtype="page_view",
                            target_kind="path", target_id=path,
                            detail={"query": dict(request.query_params)},
                            request=request)
                except Exception as e:
                    print(f"[ActivityMiddleware] {e}")
        return response


# ---- Common event-type constants (for grep-ability + autocomplete) ------

class Event:
    """Light enum for the event_type field. Use these instead of magic strings
    so reports can group reliably."""
    SESSION = "session"
    PROFILE = "profile"
    NAVIGATION = "navigation"
    TICKET = "ticket"
    AI = "ai"
    ADMIN = "admin"
    AUTOMATION = "automation"
    SYSTEM = "system"
