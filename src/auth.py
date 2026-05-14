"""
Permission resolution + FastAPI dependency factories.

This module is the bridge between the auth session (which carries identity)
and the DB-backed permissions (which carry authority). Routes call
`require('some.perm')` as a FastAPI dependency to 403 unauthorized requests.

Templates call `has_perm(user, 'some.perm')` (registered as a Jinja global)
to hide buttons/links the user can't use.

This module does NOT do any login/OAuth — that lives in src/web/app.py. We
just consume the session dict and look up permissions in the DB.
"""

from __future__ import annotations

from typing import Iterable

from fastapi import HTTPException, Request

from . import db, config
from . import permissions as P


# Used when AUTH_ENABLED is False (no Google client configured). Lets you
# develop locally without OAuth credentials.
DEV_OWNER_EMAIL = db.OWNER_EMAIL_DEFAULT


# ---- Core resolver --------------------------------------------------------

def resolve_user(email: str) -> dict:
    """Look up an app_user + their effective permissions. Returns a dict
    suitable for stuffing into request.state / passing to templates.

    If the email doesn't exist in app_users, returns an empty/anonymous shape
    with no permissions. Callers (require_user, /auth/callback) are responsible
    for deciding what to do with an unknown email.
    """
    with db.conn() as c:
        row = db.get_app_user(c, email)
        perms = db.get_user_permissions(c, email)
    if not row:
        return {
            "email": email, "name": email, "picture_url": None,
            "status": "unknown", "roles": [], "permissions": set(),
            "is_admin": False,
        }
    role_names = [r["name"] for r in row.get("roles", [])]
    return {
        "email": row["email"],
        "name": row.get("name") or row["email"],
        "picture_url": row.get("picture_url"),
        # F0 user status = active/disabled (account state). NOT to be confused
        # with F5 availability ("online"/"away"/etc.) below.
        "status": row.get("status", "active"),
        "roles": role_names,
        "permissions": perms,
        # F0+ T3: ZD user mapping (used by "Assign to me" / "Assigned to me" view)
        "zd_user_id": row.get("zd_user_id"),
        # F5 · Profile fields for the avatar pill + dropdown
        "title": row.get("title"),
        "availability": row.get("availability") if row.get("availability") in db.VALID_AVAILABILITY else "offline",
        "availability_emoji": row.get("availability_emoji"),
        "availability_label": row.get("availability_label"),
        "availability_until": row.get("availability_until"),
        "timezone": row.get("timezone") or "Asia/Kolkata",
        # Convenience flag: any of the two CRITICAL perms makes you "an admin"
        # for UI purposes. Don't use this for actual enforcement — always
        # check the specific permission key.
        "is_admin": bool(perms & db.CRITICAL_PERMS),
    }


def ensure_user_on_login(*, email: str, name: str | None,
                         picture_url: str | None) -> dict:
    """Called from /auth/callback after Google confirms the email. Upserts
    the app_user and, if this is their first login:
      - Grants the default View-only role
      - Auto-fills zd_user_id by matching email against the synced ZD users table

    Returns the resolved user dict (same shape as resolve_user).
    """
    email = (email or "").lower().strip()
    with db.conn() as c:
        existed = c.execute(
            "SELECT 1 FROM app_users WHERE email=?", (email,)
        ).fetchone() is not None
        db.upsert_app_user(c, email=email, name=name,
                           picture_url=picture_url, mark_login=True,
                           created_by=None if existed else "self-signup")
        if not existed:
            # First time we've seen this email — auto-assign View-only.
            row = c.execute(
                "SELECT id FROM roles WHERE name='View-only'"
            ).fetchone()
            if row:
                c.execute("""
                    INSERT OR IGNORE INTO user_roles
                        (user_email, role_id, granted_at, granted_by)
                    VALUES (?, ?, ?, 'self-signup')
                """, (email, row["id"], db.now_iso()))
                db.log_access(c, actor_email="system",
                              event_type="role.grant",
                              target_kind="user", target_id=email,
                              detail={"role_id": row["id"],
                                       "role_name": "View-only",
                                       "reason": "auto-assigned on first login"})
        # Always try to auto-map ZD user_id (idempotent — only writes if NULL).
        # Engineers who exist in ZD as agents will get linked here so
        # "Assigned to me" works out of the box.
        try:
            db.auto_map_zd_user(c, email)
        except Exception as e:
            print(f"[auth] auto_map_zd_user failed for {email}: {e}")
    return resolve_user(email)


# ---- Dev-mode fallback ---------------------------------------------------

def _dev_user() -> dict:
    """When AUTH_ENABLED is False, every request runs as the owner with full
    Admin permissions. Lets you develop locally with no OAuth credentials."""
    return resolve_user(DEV_OWNER_EMAIL)


# ---- FastAPI dependencies ------------------------------------------------

def current_user(request: Request) -> dict | None:
    """Returns the resolved user dict (with permissions) or None if not
    logged in. Reads identity from the session, looks up permissions in DB
    so role changes take effect immediately (no re-login needed)."""
    if not config.AUTH_ENABLED:
        return _dev_user()
    sess = request.session.get("user") if hasattr(request, "session") else None
    if not sess or not sess.get("email"):
        return None
    return resolve_user(sess["email"])


def require_user(request: Request) -> dict:
    """403 / redirect-to-login if no session. Used as a base dependency on
    every authenticated route."""
    u = current_user(request)
    if not u:
        # 303 → /auth/login; FastAPI's HTTPException with headers works for
        # GETs. For XHR/JSON we'd ideally 401, but our routes are mostly HTML
        # so 303 is right for the common case.
        raise HTTPException(status_code=303, headers={"Location": "/auth/login"})
    if u.get("status") == "disabled":
        raise HTTPException(status_code=403,
                            detail="Your account has been disabled. Contact an admin.")
    return u


def require(permission_key: str):
    """Dependency factory. Use as:
        @app.post(...)
        async def edit(user = Depends(require('tickets.edit_fields'))):
            ...
    403s with a message naming the missing permission so the user knows what
    to ask their admin for.
    """
    if permission_key not in P.ALL_KEYS:
        # Catch typos at app import time instead of mysterious 403s in prod.
        raise RuntimeError(
            f"require(): unknown permission key {permission_key!r}. "
            f"Add it to src/permissions.py PERMISSIONS list first."
        )

    def _dep(request: Request) -> dict:
        u = require_user(request)
        if permission_key not in u["permissions"]:
            raise HTTPException(
                status_code=403,
                detail=(f"Missing permission: '{permission_key}' "
                        f"({P.describe(permission_key)}). "
                        f"Ask an admin to grant you a role that includes this.")
            )
        return u
    return _dep


def require_any(*permission_keys: str):
    """Dependency factory — passes if the user has AT LEAST ONE of the keys.
    Use for routes that any of several roles can hit (e.g. /admin landing,
    where any admin.* perm should let you in)."""
    for k in permission_keys:
        if k not in P.ALL_KEYS:
            raise RuntimeError(f"require_any(): unknown permission key {k!r}")
    keys = set(permission_keys)

    def _dep(request: Request) -> dict:
        u = require_user(request)
        if not (u["permissions"] & keys):
            raise HTTPException(
                status_code=403,
                detail=(f"Need at least one of: {sorted(keys)}. "
                        f"Ask an admin to grant you the right role.")
            )
        return u
    return _dep


# ---- Template helpers ----------------------------------------------------

def has_perm(user: dict | None, permission_key: str) -> bool:
    """Used as a Jinja global. Returns True iff user has the key. Tolerates
    None / partial dicts so templates don't blow up during render of error
    pages."""
    if not user:
        return False
    perms = user.get("permissions") or set()
    if isinstance(perms, list):
        perms = set(perms)
    return permission_key in perms


def has_any_perm(user: dict | None, *permission_keys: str) -> bool:
    if not user:
        return False
    perms = user.get("permissions") or set()
    if isinstance(perms, list):
        perms = set(perms)
    return bool(perms & set(permission_keys))
