#!/usr/bin/env python3
"""
zd_pull.py — pull 5 open + 5 solved tickets (with comments + side-loads)
from your Zendesk into a single JSON file you can hand back to the AI.

Run inside your virtualenv:

    source myenv/bin/activate
    python scripts/zd_pull.py            # full pull
    python scripts/zd_pull.py --check    # auth + group lookup only

Reads credentials from `.env` (same folder as project root). Output goes to
`data/zd_pull_output.json`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Locate project root, regardless of where the script is invoked from.
# ---------------------------------------------------------------------------
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DATA_DIR = ROOT / "data"
ENV_FILE = ROOT / ".env"


def warn_if_no_venv() -> None:
    """Friendly nudge if running outside a virtualenv."""
    in_venv = (
        hasattr(sys, "real_prefix")  # legacy virtualenv
        or sys.prefix != sys.base_prefix  # standard venv
    )
    if not in_venv:
        print(
            "⚠  You don't appear to be in a virtualenv.\n"
            "   Run:  source myenv/bin/activate\n"
            "   then re-run this script.\n",
            file=sys.stderr,
        )


def load_env() -> None:
    """Load .env into os.environ. Falls back gracefully if file missing."""
    if not ENV_FILE.exists():
        return
    try:
        from dotenv import load_dotenv  # type: ignore
        load_dotenv(ENV_FILE)
    except ImportError:
        # Tiny manual parser so the script works even without python-dotenv.
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def require_deps() -> None:
    try:
        import requests  # noqa: F401
    except ImportError:
        print(
            "Missing dependency: requests\n"
            "Run:  pip install -r requirements.txt",
            file=sys.stderr,
        )
        sys.exit(1)


# ---------------------------------------------------------------------------
warn_if_no_venv()
load_env()
require_deps()

import requests  # noqa: E402

SUBDOMAIN = os.environ.get("ZD_SUBDOMAIN")
EMAIL = os.environ.get("ZD_EMAIL")
TOKEN = os.environ.get("ZD_TOKEN")


def _looks_like_placeholder(val: str | None) -> bool:
    """Detect unedited .env.example values like 'your-zendesk-api-token-here'."""
    if not val:
        return True
    v = val.strip().lower()
    return v.startswith("your-") or v in {
        "<your-token>", "changeme", "todo", "xxx", "...", "",
    }


_problems: list[str] = []
if not SUBDOMAIN or _looks_like_placeholder(SUBDOMAIN):
    _problems.append("ZD_SUBDOMAIN")
if not EMAIL or "@" not in (EMAIL or "") or _looks_like_placeholder(EMAIL):
    _problems.append("ZD_EMAIL")
if not TOKEN or _looks_like_placeholder(TOKEN):
    _problems.append("ZD_TOKEN")

if _problems:
    print(
        "✗ Your .env still has placeholder or missing values for: "
        + ", ".join(_problems)
        + f"\n  Edit:  {ENV_FILE}\n"
        "  Each line should look like  KEY=actual-value  (no quotes needed).",
        file=sys.stderr,
    )
    sys.exit(1)


def _mask(s: str, keep: int = 4) -> str:
    if not s:
        return ""
    return s[:keep] + "…" + s[-keep:] if len(s) > keep * 2 + 1 else "***"


print(
    f"Loaded creds → subdomain={SUBDOMAIN}  email={EMAIL}  token={_mask(TOKEN)}"
)

BASE = f"https://{SUBDOMAIN}.zendesk.com/api/v2"
session = requests.Session()
session.auth = (f"{EMAIL}/token", TOKEN)
session.headers.update({"Accept": "application/json"})

# Names of the two groups we care about (case-insensitive substring match).
TARGET_GROUP_NAMES = ["product support", "managed services"]
N_OPEN = 5
N_SOLVED = 5


def get(path: str, params: dict | None = None) -> dict:
    """GET with simple retry on 429 and friendlier error on 401/403/404."""
    url = path if path.startswith("http") else f"{BASE}/{path}"
    for _ in range(5):
        r = session.get(url, params=params, timeout=30)
        if r.status_code == 429:
            wait = int(r.headers.get("Retry-After", "5"))
            print(f"  rate-limited; sleeping {wait}s")
            time.sleep(wait)
            continue
        if r.status_code == 401:
            print(
                "✗ 401 Unauthorized from Zendesk.\n"
                "  Likely causes:\n"
                "    1. The token in .env is wrong, expired, or revoked.\n"
                "    2. ZD_EMAIL doesn't match the account that owns the token.\n"
                "    3. API token access is disabled in Admin Center.\n"
                "  Manual test:\n"
                f"    curl -u \"{EMAIL}/token:$ZD_TOKEN\" {BASE}/users/me.json",
                file=sys.stderr,
            )
            sys.exit(1)
        if r.status_code == 403:
            print(
                "✗ 403 Forbidden — auth worked but this user lacks permission.\n"
                "  Need an admin or agent with API access for this endpoint.",
                file=sys.stderr,
            )
            sys.exit(1)
        if r.status_code == 404 and "subdomain" in r.text.lower():
            print(
                f"✗ 404 — subdomain '{SUBDOMAIN}' not found. "
                "Double-check ZD_SUBDOMAIN in .env.",
                file=sys.stderr,
            )
            sys.exit(1)
        r.raise_for_status()
        return r.json()
    raise RuntimeError("too many retries")


def whoami() -> dict:
    me = get("users/me.json")["user"]
    print(f"Authed as: {me['name']} <{me['email']}>  · role={me['role']}")
    return me


def list_groups() -> list[dict]:
    out: list[dict] = []
    page = get("groups.json")
    out.extend(page.get("groups", []))
    while page.get("next_page"):
        page = get(page["next_page"])
        out.extend(page.get("groups", []))
    return out


def find_target_groups() -> list[dict]:
    groups = list_groups()
    matched = [
        g for g in groups
        if any(name in g["name"].lower() for name in TARGET_GROUP_NAMES)
    ]
    print(f"Found {len(matched)} target group(s):")
    for g in matched:
        print(f"  - {g['name']} (id={g['id']})")
    return matched


def search_tickets(query: str, limit: int = 100) -> list[dict]:
    res = get(
        "search.json",
        params={
            "query": query,
            "sort_by": "created_at",
            "sort_order": "desc",
            "per_page": 100,
        },
    )
    items = [r for r in res.get("results", []) if r.get("result_type") == "ticket"]
    return items[:limit]


OPEN_STATUSES = {"new", "open", "pending", "hold"}
SOLVED_STATUSES = {"solved", "closed"}


def collect_recent_tickets_per_group(target_groups: list[dict]) -> list[dict]:
    """Search each group separately (more reliable than OR-of-groups), no status filter."""
    all_tickets: list[dict] = []
    seen: set[int] = set()
    for g in target_groups:
        q = f"type:ticket group:{g['id']}"
        print(f"   query: {q}")
        results = search_tickets(q, limit=100)
        print(f"     {len(results)} tickets returned (most-recent 100 in group)")
        for t in results:
            if t["id"] in seen:
                continue
            seen.add(t["id"])
            all_tickets.append(t)
    all_tickets.sort(key=lambda t: t.get("created_at", ""), reverse=True)
    return all_tickets


def fallback_recent_tickets() -> list[dict]:
    """Last resort: pull /api/v2/tickets.json sorted desc (works when search index is stale)."""
    res = get(
        "tickets.json",
        params={"sort_by": "created_at", "sort_order": "desc", "per_page": 100},
    )
    return res.get("tickets", [])


def fetch_ticket_full(tid: int) -> dict:
    t = get(f"tickets/{tid}.json", params={"include": "users,organizations,groups"})
    side = {
        "users": {u["id"]: u for u in t.get("users", [])},
        "organizations": {o["id"]: o for o in t.get("organizations", [])},
        "groups": {g["id"]: g for g in t.get("groups", [])},
    }
    cmts = get(f"tickets/{tid}/comments.json", params={"include": "users"})
    for u in cmts.get("users", []):
        side["users"].setdefault(u["id"], u)
    return {"ticket": t["ticket"], "comments": cmts.get("comments", []), "side": side}


def list_ticket_fields() -> list[dict]:
    out: list[dict] = []
    page = get("ticket_fields.json")
    out.extend(page.get("ticket_fields", []))
    while page.get("next_page"):
        page = get(page["next_page"])
        out.extend(page.get("ticket_fields", []))
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Pull recent tickets from Zendesk.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Test auth and group lookup only; don't fetch tickets.",
    )
    args = parser.parse_args()

    print(f"=> Subdomain: {SUBDOMAIN}.zendesk.com")
    me = whoami()

    print("\n=> Looking up groups …")
    target_groups = find_target_groups()
    group_filter = ""
    if target_groups:
        group_filter = "(" + " OR ".join(f"group:{g['id']}" for g in target_groups) + ")"

    if args.check:
        print("\nCheck OK — auth works and target groups located.")
        return

    print("\n=> Fetching recent tickets per target group …")
    pool = collect_recent_tickets_per_group(target_groups)
    print(f"   Combined pool: {len(pool)} unique tickets.")

    target_group_ids = {g["id"] for g in target_groups}

    open_results = [
        t for t in pool
        if t.get("status") in OPEN_STATUSES
        and t.get("group_id") in target_group_ids
    ][:N_OPEN]
    solved_results = [
        t for t in pool
        if t.get("status") in SOLVED_STATUSES
        and t.get("group_id") in target_group_ids
    ][:N_SOLVED]
    print(f"   Selected {len(open_results)} open + {len(solved_results)} solved.")

    # Fallback path: if search returned nothing at all, hit /tickets.json directly.
    if not pool:
        print("\n=> Search index empty — falling back to /tickets.json sorted desc …")
        recent = fallback_recent_tickets()
        print(f"   {len(recent)} most-recent tickets pulled.")
        in_groups = [t for t in recent if t.get("group_id") in target_group_ids]
        print(f"   {len(in_groups)} are in our target groups.")
        open_results = [t for t in in_groups if t.get("status") in OPEN_STATUSES][:N_OPEN]
        solved_results = [t for t in in_groups if t.get("status") in SOLVED_STATUSES][:N_SOLVED]
        print(f"   Selected {len(open_results)} open + {len(solved_results)} solved (fallback).")

    print("\n=> Fetching ticket field definitions …")
    field_defs = list_ticket_fields()
    print(f"   {len(field_defs)} fields.")

    full = []
    for label, batch in (("open", open_results), ("solved", solved_results)):
        for t in batch:
            print(f"   Pulling {label} ticket #{t['id']} …")
            payload = fetch_ticket_full(t["id"])
            payload["bucket"] = label
            full.append(payload)

    output = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "subdomain": SUBDOMAIN,
        "authed_as": {"id": me["id"], "name": me["name"], "email": me["email"]},
        "target_groups": [{"id": g["id"], "name": g["name"]} for g in target_groups],
        "ticket_field_definitions": [
            {
                "id": f["id"],
                "title": f.get("title"),
                "type": f.get("type"),
                "required_in_portal": f.get("required_in_portal"),
                "custom_field_options": [
                    {"id": o["id"], "name": o["name"], "value": o["value"]}
                    for o in (f.get("custom_field_options") or [])
                ],
            }
            for f in field_defs
        ],
        "tickets": full,
    }

    DATA_DIR.mkdir(exist_ok=True)
    out_path = DATA_DIR / "zd_pull_output.json"
    out_path.write_text(json.dumps(output, indent=2, default=str))
    size_kb = out_path.stat().st_size / 1024
    print(f"\nDone → {out_path}  ({size_kb:.1f} KB, {len(full)} tickets)")
    print("Upload this file back to the chat.")


if __name__ == "__main__":
    main()
