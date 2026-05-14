"""Central config — loads .env and exposes typed settings."""
from __future__ import annotations
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "copilot.db"


def _load_env() -> None:
    if not ENV_FILE.exists():
        return
    try:
        from dotenv import load_dotenv
        load_dotenv(ENV_FILE)
    except ImportError:
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()


def env(key: str, default: str | None = None, *, required: bool = False) -> str:
    val = os.environ.get(key, default)
    if required and (not val or val.startswith("your-") or val == "changeme"):
        print(f"✗ Missing or placeholder env var: {key}", file=sys.stderr)
        sys.exit(1)
    return val or ""


# --- Zendesk ---
ZD_SUBDOMAIN = env("ZD_SUBDOMAIN", required=True)
ZD_EMAIL = env("ZD_EMAIL", required=True)
ZD_TOKEN = env("ZD_TOKEN", required=True)
ZD_BASE = f"https://{ZD_SUBDOMAIN}.zendesk.com/api/v2"

# --- Anthropic ---
# When ENABLE_AI_WORKER=false, the metered ai_worker exits without making API calls;
# Claude Desktop (via MCP) becomes the source of insights. ANTHROPIC_API_KEY can stay
# set as a fallback / for one-off batches.
ENABLE_AI_WORKER = env("ENABLE_AI_WORKER", "false").strip().lower() in ("true", "1", "yes", "y")
ANTHROPIC_API_KEY = env("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = env("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
MONTHLY_BUDGET_USD = float(env("MONTHLY_BUDGET_USD", "20"))

# --- Sync ---
TARGET_GROUP_NAMES = [n.strip().lower() for n in env("TARGET_GROUP_NAMES", "product support,managed services").split(",") if n.strip()]
BACKFILL_DAYS = int(env("BACKFILL_DAYS", "60"))
SYNC_INTERVAL_SECONDS = int(env("SYNC_INTERVAL_SECONDS", "300"))

# --- Web ---
APP_HOST = env("APP_HOST", "127.0.0.1")
APP_PORT = int(env("APP_PORT", "8000"))
APP_PUBLIC_URL = env("APP_PUBLIC_URL", f"http://{APP_HOST}:{APP_PORT}")
SESSION_SECRET = env("SESSION_SECRET", "dev-only-not-secure-replace-me")

# --- Auth ---
GOOGLE_CLIENT_ID = env("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = env("GOOGLE_CLIENT_SECRET")
ALLOWED_EMAILS = {e.strip().lower() for e in env("ALLOWED_EMAILS", "").split(",") if e.strip()}
ADMIN_EMAILS = {e.strip().lower() for e in env("ADMIN_EMAILS", "").split(",") if e.strip()}

# Auth is enforced only when GOOGLE_CLIENT_ID is set; otherwise app runs in dev-open mode.
AUTH_ENABLED = bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)


# --- Pricing (Haiku 4.5; update if model changes) ---
# These are used for the budget cap only; not authoritative billing.
COST_PER_1M_INPUT = 0.80
COST_PER_1M_OUTPUT = 4.00
COST_PER_1M_CACHED_INPUT = 0.08  # 90% off cached input

DATA_DIR.mkdir(exist_ok=True)
