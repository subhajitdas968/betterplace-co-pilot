"""F10 · Named Cloudflare Tunnel management.

The Quick Tunnel (in src/web/app.py) gives us a random *.trycloudflare.com URL
that changes every restart — fine for soft-launch, painful for a permanent
team rollout because OAuth redirect URIs and bookmarks break each time.

A Named Tunnel is a registered tunnel under our own Cloudflare account that
maps a stable hostname (e.g. copilot.betterplace.co.in) to http://127.0.0.1:8000.
The URL never changes. We can also (optionally) put Cloudflare Access in front
for defense-in-depth on top of the app's Google OAuth.

This module owns:
  • Detection: is cloudflared installed? is `cloudflared tunnel login` done?
    which tunnels exist? is config.yml written? is the DNS route in place?
  • Mutation: create tunnel, write config.yml, add DNS route, start/stop
    a managed `cloudflared tunnel run <name>` subprocess (pid+heartbeat files).
  • Tail: read the named tunnel log for the admin UI.

We deliberately stop short of automating Cloudflare Access policy creation —
that lives in the Cloudflare dashboard and varies wildly per org. We just
explain it in the UI.

Files used (all in config.DATA_DIR):
  named_tunnel.pid       — child PID for `cloudflared tunnel run`
  named_tunnel.heartbeat — JSON: state, started_at, hostname, tunnel_name
  named_tunnel.log       — cloudflared stdout/stderr (same pattern as Quick)
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
from datetime import datetime
from pathlib import Path

from . import config

# Where cloudflared keeps its config + credentials + login cert.
# This is fixed by the cloudflared tool; we don't override it.
CLOUDFLARED_DIR = Path.home() / ".cloudflared"
CERT_PATH = CLOUDFLARED_DIR / "cert.pem"
CONFIG_PATH = CLOUDFLARED_DIR / "config.yml"

# Our managed-subprocess bookkeeping (alongside the Quick Tunnel files).
PID_FILE = config.DATA_DIR / "named_tunnel.pid"
HEARTBEAT_FILE = config.DATA_DIR / "named_tunnel.heartbeat"
LOG_FILE = config.DATA_DIR / "named_tunnel.log"


# ---------------------------------------------------------------------------
# Pure detection — no side effects.
# ---------------------------------------------------------------------------

def _which() -> str | None:
    return shutil.which("cloudflared")


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def _read_heartbeat() -> dict:
    if not HEARTBEAT_FILE.exists():
        return {}
    try:
        return json.loads(HEARTBEAT_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _read_pid() -> int | None:
    if not PID_FILE.exists():
        return None
    try:
        return int(PID_FILE.read_text().strip())
    except (OSError, ValueError):
        return None


def list_tunnels() -> list[dict]:
    """Return existing named tunnels from `cloudflared tunnel list --output json`.

    Returns [] if not logged in or the command fails. Each entry is a dict
    like {id, name, created_at, deleted_at, connections, ...}.
    """
    bin_path = _which()
    if not bin_path or not CERT_PATH.exists():
        return []
    try:
        r = subprocess.run(
            [bin_path, "tunnel", "list", "--output", "json"],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode != 0:
            return []
        return json.loads(r.stdout or "[]")
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
        return []


def find_tunnel(name: str) -> dict | None:
    """Look up a tunnel by name. Returns None if missing."""
    for t in list_tunnels():
        if t.get("name") == name and not t.get("deleted_at"):
            return t
    return None


def _parse_config_yml() -> dict:
    """Cheap YAML reader — we only care about `tunnel:`, `credentials-file:`,
    and the first ingress hostname. We don't want a yaml dep for one file."""
    out: dict = {"tunnel": None, "credentials_file": None, "hostname": None,
                 "service": None, "present": False, "raw": ""}
    if not CONFIG_PATH.exists():
        return out
    out["present"] = True
    try:
        raw = CONFIG_PATH.read_text()
    except OSError:
        return out
    out["raw"] = raw
    for line in raw.splitlines():
        s = line.strip()
        if s.startswith("tunnel:"):
            out["tunnel"] = s.split(":", 1)[1].strip().strip('"\'')
        elif s.startswith("credentials-file:"):
            out["credentials_file"] = s.split(":", 1)[1].strip().strip('"\'')
        elif s.startswith("- hostname:") and not out["hostname"]:
            out["hostname"] = s.split(":", 1)[1].strip().strip('"\'')
        elif s.startswith("service:") and out["hostname"] and not out["service"]:
            # second `service:` line — the one under the hostname
            out["service"] = s.split(":", 1)[1].strip().strip('"\'')
    return out


def state() -> dict:
    """Snapshot of everything the admin UI needs to render the page."""
    bin_path = _which()
    cfg = _parse_config_yml()
    pid = _read_pid()
    alive = _pid_alive(pid)
    hb = _read_heartbeat()

    started_at = hb.get("started_at")
    age_seconds = None
    if started_at:
        try:
            t0 = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
            age_seconds = int((datetime.now(t0.tzinfo) - t0).total_seconds())
        except (ValueError, TypeError):
            pass

    # What tunnel name we expect, if any: prefer the heartbeat (last started)
    # over the config file (might exist without us ever starting it).
    expected_name = hb.get("tunnel_name") or cfg.get("tunnel")
    tunnel_meta = find_tunnel(expected_name) if expected_name else None

    return {
        "installed": bool(bin_path),
        "binary_path": bin_path or "",
        "logged_in": CERT_PATH.exists(),
        "cert_path": str(CERT_PATH),
        "config_path": str(CONFIG_PATH),
        "config_present": cfg["present"],
        "config_tunnel": cfg["tunnel"],
        "config_hostname": cfg["hostname"],
        "config_service": cfg["service"],
        "config_credentials_file": cfg["credentials_file"],
        "config_raw": cfg["raw"],
        "tunnel_exists": tunnel_meta is not None,
        "tunnel_id": tunnel_meta.get("id") if tunnel_meta else None,
        "tunnel_name": expected_name,
        "running": alive,
        "pid": pid if alive else None,
        "started_at": started_at if alive else None,
        "age_seconds": age_seconds if alive else None,
        "hostname": hb.get("hostname") or cfg.get("hostname"),
        "public_url": (
            f"https://{hb['hostname']}" if hb.get("hostname")
            else (f"https://{cfg['hostname']}" if cfg.get("hostname") else None)
        ),
        "last_error": hb.get("error"),
    }


# ---------------------------------------------------------------------------
# Mutating ops. Each returns a dict the UI surfaces directly.
# ---------------------------------------------------------------------------

def _run(cmd: list[str], timeout: int = 30) -> tuple[int, str, str]:
    """Wrapper for subprocess.run that never raises — easier to surface
    failure to the admin."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout or "", r.stderr or ""
    except subprocess.TimeoutExpired:
        return 124, "", f"Timed out after {timeout}s"
    except OSError as e:
        return 127, "", str(e)


def create_tunnel(name: str) -> dict:
    """Run `cloudflared tunnel create <name>`. Idempotent: if the name
    already exists, returns it instead of erroring."""
    bin_path = _which()
    if not bin_path:
        return {"ok": False, "error": "cloudflared not installed"}
    if not CERT_PATH.exists():
        return {"ok": False, "error": "Not logged in — run `cloudflared tunnel login` first"}
    existing = find_tunnel(name)
    if existing:
        return {"ok": True, "already_exists": True, "id": existing["id"], "name": name}
    rc, out, err = _run([bin_path, "tunnel", "create", name], timeout=60)
    if rc != 0:
        return {"ok": False, "error": (err or out).strip()[:500]}
    # cloudflared writes ~/.cloudflared/<UUID>.json — find it from the listing
    t = find_tunnel(name)
    if not t:
        return {"ok": False, "error": "Tunnel created but `tunnel list` doesn't show it. Check cloudflared output."}
    return {"ok": True, "created": True, "id": t["id"], "name": name}


def route_dns(name: str, hostname: str) -> dict:
    """Add a DNS CNAME so traffic to <hostname> hits this tunnel.

    Idempotent: cloudflared treats "already exists" as a soft error (rc != 0,
    stderr contains 'already exists') — we surface it as ok=True.
    """
    bin_path = _which()
    if not bin_path:
        return {"ok": False, "error": "cloudflared not installed"}
    rc, out, err = _run([bin_path, "tunnel", "route", "dns", name, hostname], timeout=30)
    combined = (err + out).lower()
    if rc == 0:
        return {"ok": True, "added": True, "hostname": hostname}
    if "already exists" in combined or "cname already exists" in combined:
        return {"ok": True, "already_exists": True, "hostname": hostname}
    return {"ok": False, "error": (err or out).strip()[:500]}


def write_config(name: str, hostname: str, port: int | None = None) -> dict:
    """Write ~/.cloudflared/config.yml pointing <hostname> to local app.

    We pin the credentials-file path explicitly so `cloudflared tunnel run`
    doesn't have to guess. The tunnel must already exist (so we can find its
    UUID-named credentials JSON)."""
    if port is None:
        port = config.APP_PORT
    bin_path = _which()
    if not bin_path:
        return {"ok": False, "error": "cloudflared not installed"}
    t = find_tunnel(name)
    if not t:
        return {"ok": False, "error": f"Tunnel `{name}` doesn't exist yet. Create it first."}
    creds_path = CLOUDFLARED_DIR / f"{t['id']}.json"
    if not creds_path.exists():
        # cloudflared sometimes uses ~/.cloudflared/<UUID>.json relative; we
        # just check the canonical spot. If it's missing the user moved it —
        # we still write config.yml and let cloudflared complain explicitly.
        pass
    body = (
        f"tunnel: {name}\n"
        f"credentials-file: {creds_path}\n"
        f"ingress:\n"
        f"  - hostname: {hostname}\n"
        f"    service: http://127.0.0.1:{port}\n"
        f"  - service: http_status:404\n"
    )
    try:
        CLOUDFLARED_DIR.mkdir(exist_ok=True)
        # Don't blindly clobber an existing non-trivial config — back it up.
        if CONFIG_PATH.exists():
            backup = CONFIG_PATH.with_suffix(
                f".yml.bak.{datetime.now().strftime('%Y%m%d-%H%M%S')}")
            try:
                CONFIG_PATH.rename(backup)
            except OSError:
                pass
        CONFIG_PATH.write_text(body)
        return {"ok": True, "path": str(CONFIG_PATH), "credentials_file": str(creds_path)}
    except OSError as e:
        return {"ok": False, "error": str(e)}


def _write_heartbeat(**fields) -> None:
    try:
        HEARTBEAT_FILE.write_text(json.dumps(fields))
    except OSError:
        pass


def start(name: str | None = None, hostname: str | None = None) -> dict:
    """Start `cloudflared tunnel run <name>` as a detached subprocess.

    If `name` is omitted, falls back to the value in config.yml. We tolerate
    the heartbeat file pointing at a dead PID — that's just a stale crash."""
    bin_path = _which()
    if not bin_path:
        return {"ok": False, "error": "cloudflared not installed"}
    cur = state()
    if cur["running"]:
        return {"ok": True, "already_running": True, "pid": cur["pid"],
                "public_url": cur["public_url"]}
    chosen = name or cur["config_tunnel"]
    if not chosen:
        return {"ok": False, "error": "No tunnel selected. Provide a name or write config.yml first."}
    chosen_host = hostname or cur["config_hostname"]
    # Fresh log + heartbeat
    try:
        if HEARTBEAT_FILE.exists():
            HEARTBEAT_FILE.unlink()
    except OSError:
        pass
    try:
        log_f = open(LOG_FILE, "w")
    except OSError as e:
        return {"ok": False, "error": f"Cannot open log: {e}"}
    cmd = [bin_path, "tunnel", "run", chosen]
    try:
        proc = subprocess.Popen(
            cmd, stdout=log_f, stderr=log_f,
            start_new_session=True,  # survive uvicorn --reload restarts
        )
    except OSError as e:
        return {"ok": False, "error": str(e)}
    try:
        PID_FILE.write_text(str(proc.pid))
    except OSError:
        pass
    _write_heartbeat(state="starting", pid=proc.pid,
                     started_at=datetime.now().isoformat(timespec="seconds"),
                     tunnel_name=chosen, hostname=chosen_host)

    # Give it ~6s to either fail fast or print the registration message,
    # then mark heartbeat as running. We don't need to tail forever; the log
    # endpoint shows the live tail.
    deadline = time.time() + 6
    while time.time() < deadline:
        if proc.poll() is not None:
            tail = ""
            try:
                tail = LOG_FILE.read_text(errors="ignore")[-500:]
            except OSError:
                pass
            _write_heartbeat(state="error", pid=proc.pid,
                             started_at=datetime.now().isoformat(timespec="seconds"),
                             tunnel_name=chosen, hostname=chosen_host,
                             error=f"cloudflared exited early (rc={proc.returncode}). Tail: {tail}")
            try:
                PID_FILE.unlink()
            except OSError:
                pass
            return {"ok": False, "error": "cloudflared exited immediately — check log.",
                    "tail": tail}
        time.sleep(0.5)

    _write_heartbeat(state="running", pid=proc.pid,
                     started_at=datetime.now().isoformat(timespec="seconds"),
                     tunnel_name=chosen, hostname=chosen_host)
    return {"ok": True, "started": True, "pid": proc.pid,
            "tunnel_name": chosen, "hostname": chosen_host}


def stop() -> dict:
    """SIGTERM the running named tunnel, then SIGKILL if it doesn't exit."""
    pid = _read_pid()
    if not pid or not _pid_alive(pid):
        # Clean up stale heartbeat regardless
        _write_heartbeat(state="stopped", pid=None, started_at=None,
                         tunnel_name=None, hostname=None)
        try:
            PID_FILE.unlink()
        except FileNotFoundError:
            pass
        return {"ok": True, "was_running": False}
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError) as e:
        return {"ok": False, "error": str(e)}
    for _ in range(30):
        if not _pid_alive(pid):
            break
        time.sleep(0.1)
    else:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    _write_heartbeat(state="stopped", pid=None, started_at=None,
                     tunnel_name=None, hostname=None)
    try:
        PID_FILE.unlink()
    except FileNotFoundError:
        pass
    return {"ok": True, "was_running": True, "pid": pid}


def log_tail(lines: int = 200) -> dict:
    if not LOG_FILE.exists():
        return {"present": False, "lines": []}
    try:
        text = LOG_FILE.read_text(errors="ignore")
        tail = text.splitlines()[-int(max(10, min(lines, 2000))):]
        return {"present": True, "lines": tail}
    except OSError as e:
        return {"present": False, "error": str(e)}


def setup(name: str, hostname: str, port: int | None = None) -> dict:
    """One-shot orchestration: create tunnel + DNS route + config.yml.

    Returns a list of step results so the UI can show what worked vs failed
    even on partial progress."""
    if port is None:
        port = config.APP_PORT
    steps: list[dict] = []

    c = create_tunnel(name)
    steps.append({"step": "create_tunnel", **c})
    if not c["ok"]:
        return {"ok": False, "steps": steps}

    d = route_dns(name, hostname)
    steps.append({"step": "route_dns", **d})
    if not d["ok"]:
        return {"ok": False, "steps": steps}

    w = write_config(name, hostname, port)
    steps.append({"step": "write_config", **w})
    if not w["ok"]:
        return {"ok": False, "steps": steps}

    return {"ok": True, "steps": steps, "name": name, "hostname": hostname}
