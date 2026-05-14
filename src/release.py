"""
Release + rollback system.

Drives `make release` and `make rollback` plus the /admin/releases page.
Every release captures:
  - A semver bump in the VERSION file at repo root
  - A git tag (e.g. v1.7.0) on the current commit
  - A DB backup at data/backups/copilot-v1.7.0.db (online backup API)
  - A row in the `releases` table with notes + sha + paths

Rollback flow (manual + UI button both call into this):
  - Read the target release row
  - git checkout <tag>     (user does this in their terminal; UI displays the command)
  - Restore the paired DB backup over data/copilot.db
  - Restart uvicorn
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import db, config


REPO_ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = REPO_ROOT / "VERSION"


# ---- Version utilities ---------------------------------------------------

def read_version() -> str:
    try:
        return VERSION_FILE.read_text().strip() or "0.0.0"
    except FileNotFoundError:
        return "0.0.0"


def write_version(v: str) -> None:
    VERSION_FILE.write_text(v.strip() + "\n")


def bump(part: str = "patch", current: str | None = None) -> str:
    """Bump major/minor/patch. Returns the new version string without writing."""
    current = current or read_version()
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)$", current.strip())
    if not m:
        raise ValueError(f"Invalid current version: {current!r}")
    maj, mn, pt = (int(x) for x in m.groups())
    if part == "major":
        return f"{maj + 1}.0.0"
    if part == "minor":
        return f"{maj}.{mn + 1}.0"
    if part == "patch":
        return f"{maj}.{mn}.{pt + 1}"
    raise ValueError("part must be major | minor | patch")


# ---- Git helpers ---------------------------------------------------------

def _git(*args: str) -> str:
    """Run a git command and return stdout (stripped). Empty string on error."""
    try:
        r = subprocess.run(["git", *args], cwd=str(REPO_ROOT),
                            capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            return ""
        return (r.stdout or "").strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""


def current_git_sha() -> str:
    return _git("rev-parse", "--short", "HEAD")


def current_git_branch() -> str:
    return _git("rev-parse", "--abbrev-ref", "HEAD")


def files_changed_since(prev_tag: str | None) -> int:
    """How many files changed since the previous release's tag.
    0 if there's no previous tag yet."""
    if not prev_tag:
        return 0
    out = _git("diff", "--name-only", prev_tag, "HEAD")
    if not out:
        return 0
    return len([line for line in out.splitlines() if line.strip()])


def is_git_clean() -> bool:
    """True if working tree has no uncommitted changes."""
    out = _git("status", "--porcelain")
    return out == ""


# ---- Release lifecycle ---------------------------------------------------

def create_release(*, part: str = "patch",
                    notes: str = "",
                    actor_email: str = "system",
                    require_clean_tree: bool = True,
                    tag_git: bool = True) -> dict:
    """Run the release sequence:
      1. Bump VERSION
      2. Capture DB backup at data/backups/copilot-v<ver>.db
      3. git tag v<ver>
      4. Mark all previous rows is_current=0, insert new row is_current=1

    Returns the new release dict. Raises if working tree is dirty (override
    with require_clean_tree=False)."""
    if require_clean_tree and not is_git_clean():
        raise RuntimeError(
            "Working tree has uncommitted changes — commit or stash first, "
            "or pass require_clean_tree=False."
        )

    old_version = read_version()
    new_version = bump(part, old_version)
    sha = current_git_sha() or "unknown"
    prev_tag = f"v{old_version}"
    files_n = files_changed_since(prev_tag if old_version != "0.0.0" else None)

    # 1. Write VERSION first so the running app reflects the new number
    write_version(new_version)

    # 2. Backup DB with version-labelled filename
    backup_dir = config.DATA_DIR / "backups"
    backup_dir.mkdir(exist_ok=True)
    backup_path = backup_dir / f"copilot-v{new_version}.db"
    try:
        db.backup(target_dir=backup_dir)  # also rotates the rolling 7-day set
        # Make a separate, NAMED copy that won't get rotated out
        src = config.DATA_DIR / "copilot.db"
        shutil.copyfile(src, backup_path)
    except Exception as e:
        # Roll back the version bump if backup fails
        write_version(old_version)
        raise RuntimeError(f"DB backup failed; aborted release. {e}") from e

    # 3. git tag (best-effort — release succeeds even if user lacks git creds)
    git_tag = f"v{new_version}"
    if tag_git:
        tag_msg = f"Release v{new_version}\n\n{notes}" if notes else f"Release v{new_version}"
        out = _git("tag", "-a", git_tag, "-m", tag_msg)
        if out is None:  # subprocess error
            print(f"[release] git tag failed (continuing without tag)")

    # 4. Record in DB
    with db.conn() as c:
        c.execute("UPDATE releases SET is_current=0 WHERE is_current=1")
        c.execute("""
            INSERT INTO releases (version, git_sha, git_tag, notes,
                db_backup_path, code_files_changed, is_current,
                created_at, created_by)
            VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
        """, (new_version, sha, git_tag, notes, str(backup_path),
              files_n, db.now_iso(), actor_email))

    return {
        "version": new_version,
        "git_sha": sha,
        "git_tag": git_tag,
        "db_backup_path": str(backup_path),
        "code_files_changed": files_n,
        "previous_version": old_version,
        "notes": notes,
    }


def list_releases(limit: int = 50) -> list[dict]:
    with db.conn() as c:
        rows = c.execute("""
            SELECT * FROM releases ORDER BY id DESC LIMIT ?
        """, (limit,)).fetchall()
    return [dict(r) for r in rows]


def get_release(version: str) -> dict | None:
    with db.conn() as c:
        row = c.execute("SELECT * FROM releases WHERE version=?",
                         (version,)).fetchone()
    return dict(row) if row else None


def prepare_rollback(version: str) -> dict:
    """Returns the steps needed to roll back to the given version. We DON'T
    actually execute them on the server because that would mid-restart the
    DB we're querying. Instead we hand the admin a copy-pasteable script."""
    rel = get_release(version)
    if not rel:
        raise ValueError(f"No release with version {version!r}")
    if not rel.get("db_backup_path") or not Path(rel["db_backup_path"]).exists():
        raise FileNotFoundError(
            f"DB backup for v{version} is missing at {rel.get('db_backup_path')!r}. "
            f"Cannot safely roll back."
        )
    current = read_version()
    script = f"""# Roll back from v{current} → v{version}
# Run these in your terminal (zd-copilot folder)

# 1. Stop the app
lsof -ti:8000 | xargs kill -9 2>/dev/null
pkill -f cloudflared 2>/dev/null

# 2. Backup the CURRENT state before overwriting (in case you change your mind)
cp data/copilot.db data/copilot.db.before-rollback-{datetime.now().strftime('%Y%m%d-%H%M')}

# 3. Restore the v{version} DB snapshot
cp '{rel["db_backup_path"]}' data/copilot.db
rm -f data/copilot.db-shm data/copilot.db-wal

# 4. Check out the v{version} code
git checkout {rel.get('git_tag') or f'v{version}'}

# 5. Restart
make web
"""
    return {
        "target_version": version,
        "current_version": current,
        "db_backup_path": rel["db_backup_path"],
        "git_tag": rel.get("git_tag"),
        "git_sha": rel.get("git_sha"),
        "script": script,
    }


def mark_rolled_back(version: str, actor_email: str) -> None:
    """Called after a successful rollback to record it in the DB. Doesn't
    actually do the rollback — that's the admin's job in their terminal."""
    with db.conn() as c:
        c.execute("""
            UPDATE releases
            SET rolled_back_at=?, rolled_back_by=?
            WHERE version=?
        """, (db.now_iso(), actor_email, version))


def runtime_info() -> dict:
    """For the footer chip + /admin/releases header. Cheap — read once on
    each page load."""
    return {
        "version": read_version(),
        "git_sha": current_git_sha() or "—",
        "git_branch": current_git_branch() or "—",
        "git_clean": is_git_clean(),
    }
